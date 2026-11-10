"""The feature manager of the plugin: one per VTherm, dispatching the hooks to the quirks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant

from .const import (
    CONF_ENTITIES,
    CONF_OPTIONS,
    CONF_QUIRK,
    CONF_QUIRKS,
    CONF_TARGET_VTHERM_UNIQUE_ID,
    DOMAIN,
    FACTORY_NAME,
)
from .quirks import BaseQuirk, compatible_quirks, get_quirk
from .repairs import ReportedIssue, async_clear_issues, async_sync_issues

if TYPE_CHECKING:
    from vtherm_api import (
        CalibrationOffsetRequest,
        InterfaceThermostatRuntime,
        UnderlyingPowerCommand,
        ValveCommand,
        ValveWriteDecision,
        ValveWriteRequest,
    )

_LOGGER = logging.getLogger(__name__)

_ACTIVE_STATES = (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)


def find_config_entry(hass: HomeAssistant, vtherm_unique_id: str) -> ConfigEntry | None:
    """Return the active entry of the plugin that targets a VTherm."""
    checked = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (
            entry.data.get(CONF_TARGET_VTHERM_UNIQUE_ID) == vtherm_unique_id
            and entry.state in _ACTIVE_STATES
            and not entry.disabled_by
        ):
            return entry
        checked.append(
            (
                entry.title,
                entry.data.get(CONF_TARGET_VTHERM_UNIQUE_ID),
                entry.state,
                entry.disabled_by,
            )
        )
    _LOGGER.debug(
        "No active plugin entry for VTherm %s, entries checked (title, target, state, disabled_by): %s",
        vtherm_unique_id,
        checked,
    )
    return None


def _missing_roles(quirk: BaseQuirk) -> list[str]:
    """Keys of the required entity roles a quirk does not have."""
    return [
        role.key
        for role in quirk.entity_roles()
        if role.required and not quirk.entities.get(role.key)
    ]


def _describe_args(hook: str, args: tuple[Any, ...]) -> str:
    """Short description of the arguments of a hook call, for the logs."""
    if hook == "async_before_underlying_hvac_mode":
        _, hvac_mode, current, cause = args
        return f"hvac_mode={hvac_mode}, current={current}, cause={cause}"
    command = args[0]
    if hook == "async_after_valve_command":
        return (
            f"cause={getattr(command, 'cause', None)}, "
            f"opening_degree={getattr(command, 'opening_degree', None)}, "
            f"closing_degree={getattr(command, 'closing_degree', None)}, "
            f"percent_open={getattr(command, 'percent_open', None)}, "
            f"hvac_mode={getattr(command, 'hvac_mode', None)}"
        )
    return str(command)


class QuirksManager:
    """The only feature manager of the plugin, one per VTherm.

    It implements the hooks of ``vtherm_api`` and the optional contracts of the
    core (valve write, calibration offset) and forwards
    each call to the quirk bound to the underlying concerned, only when the
    quirk class defines the method. A failing quirk is logged with its id and
    never disturbs the others nor the core.
    """

    def __init__(self, runtime: InterfaceThermostatRuntime) -> None:
        self._runtime = runtime
        self._hass: HomeAssistant = runtime.hass
        self._quirks: dict[str, BaseQuirk] = {}
        self._listeners: list[CALLBACK_TYPE] = []
        self._issue_signature: tuple | None = None
        self._started = False

    # ------------------------------------------------------------------
    # InterfaceFeatureManager
    # ------------------------------------------------------------------
    def post_init(self, entry_infos: dict[str, Any]) -> None:
        """Create the quirk instances from the config entry of the plugin."""
        self._quirks = {}
        entry = find_config_entry(self._hass, self._runtime.unique_id)
        if entry is None:
            _LOGGER.debug(
                "%s - post_init: no plugin entry, no quirk created", self._runtime.name
            )
            return
        allowed = compatible_quirks(entry_infos)
        _LOGGER.debug(
            "%s - post_init: entry '%s', compatible quirks %s, configured %s",
            self._runtime.name,
            entry.title,
            [q.quirk_id for q in allowed],
            {u: c.get(CONF_QUIRK) for u, c in (entry.data.get(CONF_QUIRKS) or {}).items()},
        )
        for underlying, conf in (entry.data.get(CONF_QUIRKS) or {}).items():
            quirk_cls = get_quirk(conf.get(CONF_QUIRK))
            if quirk_cls is None or quirk_cls not in allowed:
                _LOGGER.warning(
                    "%s - quirk '%s' of %s is unknown or not compatible, ignored",
                    self._runtime.name,
                    conf.get(CONF_QUIRK),
                    underlying,
                )
                continue
            try:
                options = quirk_cls.resolve_options(conf.get(CONF_OPTIONS))
            except vol.Invalid as err:
                _LOGGER.warning(
                    "%s - invalid options of quirk %s, using defaults: %s",
                    self._runtime.name,
                    quirk_cls.quirk_id,
                    err,
                )
                options = quirk_cls.resolve_options({})
            self._quirks[underlying] = quirk_cls(
                self._hass,
                self._runtime,
                underlying,
                conf.get(CONF_ENTITIES) or {},
                options,
            )
            _LOGGER.debug(
                "%s - post_init: quirk %s created for %s, entities=%s, options=%s",
                self._runtime.name,
                quirk_cls.quirk_id,
                underlying,
                conf.get(CONF_ENTITIES) or {},
                options,
            )
        for underlying, quirk in self._quirks.items():
            if not quirk.is_configured:
                _LOGGER.debug(
                    "%s - post_init: quirk %s of %s is not configured, required roles missing: %s",
                    self._runtime.name,
                    quirk.quirk_id,
                    underlying,
                    _missing_roles(quirk),
                )
        if not self._quirks:
            _LOGGER.debug("%s - post_init: no quirk created", self._runtime.name)

    async def start_listening(self, force: bool = False) -> None:
        """Start the quirks."""
        self._started = True
        _LOGGER.debug(
            "%s - start_listening: force=%s, quirks=%s",
            self._runtime.name,
            force,
            list(self._quirks),
        )
        for quirk in self._quirks.values():
            quirk.issue_sync_callback = self._sync_issues
            if not quirk.is_configured:
                _LOGGER.debug(
                    "%s - start_listening: quirk %s of %s is not configured (missing roles: %s), hooks will be ignored",
                    self._runtime.name,
                    quirk.quirk_id,
                    quirk.underlying_entity_id,
                    _missing_roles(quirk),
                )
            try:
                await quirk.async_start()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "%s - quirk %s failed to start", self._runtime.name, quirk.quirk_id
                )
        self._sync_issues(force=True)

    def stop_listening(self) -> bool:
        """Stop the quirks. They restore their device in a background task."""
        _LOGGER.debug(
            "%s - stop_listening: started=%s, quirks=%s",
            self._runtime.name,
            self._started,
            list(self._quirks),
        )
        for cancel in self._listeners:
            cancel()
        self._listeners.clear()
        if self._started:
            for quirk in self._quirks.values():
                quirk.issue_sync_callback = None
                self._hass.async_create_background_task(
                    self._async_stop_quirk(quirk),
                    f"vtherm_quirks stop {quirk.quirk_id} {quirk.underlying_entity_id}",
                )
            async_clear_issues(self._hass, self._scope)
        self._started = False
        return True

    async def _async_stop_quirk(self, quirk: BaseQuirk) -> None:
        try:
            await quirk.async_stop()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s - quirk %s failed to stop", self._runtime.name, quirk.quirk_id
            )

    async def refresh_state(self) -> bool:
        """Refresh the repairs. Called by the core at each cycle."""
        self._sync_issues()
        return False

    def restore_state(self, old_state: Any) -> None:
        """Nothing to restore."""

    def add_listener(self, func: CALLBACK_TYPE) -> None:
        """Register a callback to cancel at stop."""
        self._listeners.append(func)

    @property
    def is_configured(self) -> bool:
        """True when at least one quirk has all its required entities."""
        return any(quirk.is_configured for quirk in self._quirks.values())

    @property
    def is_detected(self) -> bool:
        """The manager has no condition to detect."""
        return self.is_configured

    @property
    def name(self) -> str:
        """Stable name, also the name of the factory."""
        return FACTORY_NAME

    @property
    def hass(self) -> HomeAssistant:
        """The Home Assistant instance."""
        return self._hass

    @property
    def quirks(self) -> dict[str, BaseQuirk]:
        """The quirk instances by underlying entity id."""
        return self._quirks

    # ------------------------------------------------------------------
    # Hooks of vtherm_api
    # ------------------------------------------------------------------
    async def async_before_underlying_hvac_mode(
        self,
        climate_entity_id: str,
        hvac_mode: str,
        current_hvac_mode: str | None,
        command_cause: str | None,
    ) -> None:
        """Forward to the quirk of the climate."""
        await self._dispatch(
            "async_before_underlying_hvac_mode",
            (climate_entity_id,),
            climate_entity_id,
            hvac_mode,
            current_hvac_mode,
            command_cause,
        )

    async def async_after_valve_command(self, command: ValveCommand) -> None:
        """Forward to the quirk of the climate that received the command."""
        await self._dispatch(
            "async_after_valve_command", (command.climate_entity_id,), command
        )

    async def async_on_underlying_power(self, command: UnderlyingPowerCommand) -> None:
        """Forward to the quirk of the underlying (or of its climate)."""
        await self._dispatch(
            "async_on_underlying_power",
            (command.underlying_entity_id, command.climate_entity_id),
            command,
        )

    # ------------------------------------------------------------------
    # Optional contracts of vtherm_api (0.10.0)
    # ------------------------------------------------------------------
    async def async_before_valve_write(
        self, request: ValveWriteRequest
    ) -> ValveWriteDecision | None:
        """Forward to the quirk of the climate, return its decision or None."""
        hook = "async_before_valve_write"
        quirk = self._quirk_for(hook, (request.climate_entity_id,))
        if quirk is None:
            return None
        try:
            decision = await getattr(quirk, hook)(request)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s - quirk %s failed in %s", self._runtime.name, quirk.quirk_id, hook
            )
            return None
        _LOGGER.debug(
            "%s - hook %s of quirk %s of %s answered %s",
            self._runtime.name,
            hook,
            quirk.quirk_id,
            quirk.underlying_entity_id,
            decision,
        )
        return decision

    def adjust_calibration_offset(self, request: CalibrationOffsetRequest) -> float | None:
        """Offset to use for the climate, None to keep the proposed one."""
        return self._call_sync(
            "adjust_calibration_offset", request.climate_entity_id, request
        )

    def _call_sync(self, method: str, climate_entity_id: str, *args: Any) -> Any:
        """Call a synchronous method of the quirk of a climate, None when it has none.

        These methods are called from properties and at each state change of
        the device: nothing is logged unless the quirk fails.
        """
        quirk = self._quirk_for(method, (climate_entity_id,), quiet=True)
        if quirk is None:
            return None
        try:
            return getattr(quirk, method)(*args)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s - quirk %s failed in %s", self._runtime.name, quirk.quirk_id, method
            )
            return None

    def _quirk_for(
        self, hook: str, entity_ids: tuple[str | None, ...], quiet: bool = False
    ) -> BaseQuirk | None:
        """The configured quirk of the first known entity, if its class defines the hook."""
        quirk = next(
            (self._quirks[e] for e in entity_ids if e and e in self._quirks), None
        )
        if quirk is None:
            if not quiet:
                _LOGGER.debug(
                    "%s - hook %s dropped: no quirk for %s, quirk keys: %s",
                    self._runtime.name,
                    hook,
                    entity_ids,
                    list(self._quirks),
                )
            return None
        if not quirk.is_configured:
            if not quiet:
                _LOGGER.debug(
                    "%s - hook %s dropped: quirk %s of %s is not configured, missing roles: %s",
                    self._runtime.name,
                    hook,
                    quirk.quirk_id,
                    quirk.underlying_entity_id,
                    _missing_roles(quirk),
                )
            return None
        # Only forward to a quirk whose class defines the hook.
        if getattr(type(quirk), hook, None) is None:
            if not quiet:
                _LOGGER.debug(
                    "%s - hook %s dropped: quirk %s does not define it",
                    self._runtime.name,
                    hook,
                    quirk.quirk_id,
                )
            return None
        return quirk

    async def _dispatch(
        self, hook: str, entity_ids: tuple[str | None, ...], *args: Any
    ) -> None:
        _LOGGER.debug(
            "%s - hook %s received for %s: %s",
            self._runtime.name,
            hook,
            entity_ids,
            _describe_args(hook, args),
        )
        quirk = self._quirk_for(hook, entity_ids)
        if quirk is None:
            return
        _LOGGER.debug(
            "%s - hook %s forwarded to quirk %s of %s",
            self._runtime.name,
            hook,
            quirk.quirk_id,
            quirk.underlying_entity_id,
        )
        try:
            await getattr(quirk, hook)(*args)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s - quirk %s failed in %s", self._runtime.name, quirk.quirk_id, hook
            )

    # ------------------------------------------------------------------
    # Repairs
    # ------------------------------------------------------------------
    @property
    def _scope(self) -> str:
        return f"vtherm_{self._runtime.unique_id}"

    def _collect_issues(self) -> list[ReportedIssue]:
        reported: list[ReportedIssue] = []
        for underlying, quirk in self._quirks.items():
            try:
                issues = quirk.check_configuration()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "%s - quirk %s failed in check_configuration",
                    self._runtime.name,
                    quirk.quirk_id,
                )
                continue
            for issue in issues:
                reported.append(
                    ReportedIssue(
                        issue_id=f"{self._runtime.unique_id}_{underlying}_{issue.key}",
                        translation_key=issue.key,
                        severity=issue.severity,
                        placeholders={
                            "name": self._runtime.name,
                            "underlying": underlying,
                            "quirk": quirk.title,
                            **issue.placeholders,
                        },
                    )
                )
        return reported

    def _sync_issues(self, force: bool = False) -> None:
        issues = self._collect_issues()
        signature = tuple(
            (i.issue_id, tuple(sorted(i.placeholders.items()))) for i in issues
        )
        if not force and signature == self._issue_signature:
            return
        self._issue_signature = signature
        async_sync_issues(self._hass, self._scope, issues)

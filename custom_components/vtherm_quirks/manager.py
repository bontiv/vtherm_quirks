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
        InterfaceThermostatRuntime,
        UnderlyingPowerCommand,
        ValveCommand,
    )

_LOGGER = logging.getLogger(__name__)

_ACTIVE_STATES = (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)


def find_config_entry(hass: HomeAssistant, vtherm_unique_id: str) -> ConfigEntry | None:
    """Return the active entry of the plugin that targets a VTherm."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (
            entry.data.get(CONF_TARGET_VTHERM_UNIQUE_ID) == vtherm_unique_id
            and entry.state in _ACTIVE_STATES
            and not entry.disabled_by
        ):
            return entry
    return None


class QuirksManager:
    """The only feature manager of the plugin, one per VTherm.

    It implements the three hooks of ``vtherm_api`` and forwards each call to
    the quirk bound to the underlying concerned, only when the quirk class
    defines the hook. A failing quirk is logged with its id and never disturbs
    the others nor the core.
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
            return
        allowed = compatible_quirks(entry_infos)
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

    async def start_listening(self, force: bool = False) -> None:
        """Start the quirks."""
        self._started = True
        for quirk in self._quirks.values():
            quirk.issue_sync_callback = self._sync_issues
            try:
                await quirk.async_start()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "%s - quirk %s failed to start", self._runtime.name, quirk.quirk_id
                )
        self._sync_issues(force=True)

    def stop_listening(self) -> bool:
        """Stop the quirks. They restore their device in a background task."""
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

    async def _dispatch(
        self, hook: str, entity_ids: tuple[str | None, ...], *args: Any
    ) -> None:
        quirk = next(
            (self._quirks[e] for e in entity_ids if e and e in self._quirks), None
        )
        if quirk is None or not quirk.is_configured:
            return
        # Only forward to a quirk whose class defines the hook.
        if getattr(type(quirk), hook, None) is None:
            return
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

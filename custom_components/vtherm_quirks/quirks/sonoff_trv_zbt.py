"""Quirk of the Sonoff TRV-ZBT (Gen2) under Zigbee2MQTT and ZHA.

The TRV-ZBT only moves its motor when its internal thermostat changes state
(idle <-> heating): a new ``heating_valve_position`` is ignored while the TRV is
idle, and when the TRV is ``off`` the valve goes to ``idle_valve_position``.

This quirk reproduces and replaces the "Sonoff TRV-ZBT valve kick" blueprint:

* after each valve command of VTherm it syncs ``idle_valve_position`` and
  forces the TRV through a heating transition with a fake external temperature
  (the "kick");
* before the TRV goes OFF it closes ``idle_valve_position``, while the TRV is
  still in heat, which the blueprint could not do in time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import (
    CAUSE_REGULATION,
    CAUSE_TURN_OFF,
    DEFAULT_DELTA,
    DEFAULT_PULSE_DELAY,
    HVAC_OFF,
    MAX_DELTA,
    MAX_PULSE_DELAY,
    MIN_DELTA,
    MIN_PULSE_DELAY,
    OPT_DELTA,
    OPT_HOT_STEP,
    OPT_PULSE_DELAY,
    OPT_TEMPERATURE_SOURCE,
    SCOPE_OVER_CLIMATE_VALVE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    TEMPERATURE_SOURCE_LOCAL,
    TEMPERATURE_SOURCE_REMOTE,
    TRV_ZBT_MANUFACTURER,
    TRV_ZBT_MAX_EXTERNAL_TEMPERATURE,
    TRV_ZBT_MIN_EXTERNAL_TEMPERATURE,
    TRV_ZBT_MODEL,
    UNDERLYING_CLIMATE,
    VT_CONF_CLOSING_DEGREE_LIST,
    VT_CONF_OPENING_DEGREE_LIST,
    VT_CONF_SYNC_DEVICE_INTERNAL_TEMP,
    Z2M_ADAPTIVE_CONTROL,
    Z2M_EXTERNAL_TEMPERATURE,
    Z2M_HEATING_POSITION,
    Z2M_IDLE_POSITION,
    Z2M_OPEN_WINDOW,
    Z2M_PLATFORM,
    Z2M_SENSOR_LOCAL_OPTION,
    Z2M_SENSOR_REMOTE_OPTION,
    Z2M_SENSOR_SELECT,
    ZHA_ADAPTIVE_CONTROL,
    ZHA_EXTERNAL_TEMPERATURE,
    ZHA_HEATING_POSITION,
    ZHA_IDLE_POSITION,
    ZHA_OPEN_WINDOW,
    ZHA_PLATFORM,
    ZHA_SENSOR_SWITCH,
    ZHA_SENSOR_SWITCH_ATTRIBUTE,
)
from .base import BaseQuirk, EntityRole, QuirkIssue

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry
    from vtherm_api import InterfaceThermostatRuntime, ValveCommand

_LOGGER = logging.getLogger(__name__)

# Roles of the quirk
ROLE_HEATING_POSITION = "heating_position"
ROLE_IDLE_POSITION = "idle_position"
ROLE_EXTERNAL_TEMPERATURE = "external_temperature"
ROLE_SENSOR_SELECT = "sensor_select"
ROLE_ADAPTIVE_CONTROL = "adaptive_control"
ROLE_OPEN_WINDOW = "open_window"


@dataclass(frozen=True, slots=True)
class _RoleSpec:
    """How to recognize the entity of a role on each integration."""

    role: EntityRole
    z2m_name: str
    zha_translation_key: str
    zha_attribute: str | None = None


_ROLE_SPECS: tuple[_RoleSpec, ...] = (
    _RoleSpec(
        EntityRole(ROLE_HEATING_POSITION, ("number",)),
        Z2M_HEATING_POSITION,
        ZHA_HEATING_POSITION,
        ZHA_HEATING_POSITION,
    ),
    _RoleSpec(
        EntityRole(ROLE_IDLE_POSITION, ("number",)),
        Z2M_IDLE_POSITION,
        ZHA_IDLE_POSITION,
        ZHA_IDLE_POSITION,
    ),
    _RoleSpec(
        EntityRole(ROLE_EXTERNAL_TEMPERATURE, ("number",)),
        Z2M_EXTERNAL_TEMPERATURE,
        ZHA_EXTERNAL_TEMPERATURE,
        ZHA_EXTERNAL_TEMPERATURE,
    ),
    _RoleSpec(
        EntityRole(ROLE_SENSOR_SELECT, ("select", "switch")),
        Z2M_SENSOR_SELECT,
        ZHA_SENSOR_SWITCH,
        ZHA_SENSOR_SWITCH_ATTRIBUTE,
    ),
    # Optional roles, only used by check_configuration.
    _RoleSpec(
        EntityRole(ROLE_ADAPTIVE_CONTROL, ("switch",), required=False),
        Z2M_ADAPTIVE_CONTROL,
        ZHA_ADAPTIVE_CONTROL,
    ),
    _RoleSpec(
        EntityRole(ROLE_OPEN_WINDOW, ("switch",), required=False),
        Z2M_OPEN_WINDOW,
        ZHA_OPEN_WINDOW,
    ),
)

_REQUIRED_ROLES = tuple(
    spec.role.key for spec in _ROLE_SPECS if spec.role.required
)


def _matches(entry: er.RegistryEntry, spec: _RoleSpec) -> bool:
    """Return True when a registry entry is the entity of a role."""
    if entry.domain not in spec.role.domains:
        return False
    unique_id = entry.unique_id or ""
    if entry.platform == Z2M_PLATFORM:
        return f"_{spec.z2m_name}_" in unique_id or unique_id.endswith(
            f"_{spec.z2m_name}"
        )
    if entry.platform == ZHA_PLATFORM:
        if entry.translation_key == spec.zha_translation_key:
            return True
        return bool(spec.zha_attribute) and unique_id.endswith(
            f"-{spec.zha_attribute}"
        )
    return False


def _to_float(value: Any) -> float | None:
    """Convert a state or an attribute to a float, None when impossible."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_temperature(value: float) -> float:
    """Keep a fake temperature in the accepted range of the TRV."""
    return round(
        max(
            TRV_ZBT_MIN_EXTERNAL_TEMPERATURE,
            min(TRV_ZBT_MAX_EXTERNAL_TEMPERATURE, value),
        ),
        1,
    )


class SonoffTrvZbtQuirk(BaseQuirk):
    """Makes the TRV-ZBT apply the valve opening written by VTherm."""

    quirk_id: ClassVar[str] = "sonoff_trv_zbt"
    title: ClassVar[str] = "Sonoff TRV-ZBT"
    underlying_types: ClassVar[frozenset[str]] = frozenset({UNDERLYING_CLIMATE})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({SCOPE_OVER_CLIMATE_VALVE})

    # ------------------------------------------------------------------
    # Discovery and configuration
    # ------------------------------------------------------------------
    @classmethod
    def match_device(cls, device: DeviceEntry) -> bool:
        """True for a SONOFF TRV-ZBT, under Zigbee2MQTT or ZHA.

        ZHA reports the model ``TRV-ZBT``; Zigbee2MQTT reports a description
        ending with ``(TRV-ZBT)``, so the model is searched as a substring.
        """
        manufacturer = (device.manufacturer or "").strip().upper()
        if manufacturer != TRV_ZBT_MANUFACTURER:
            return False
        models = [device.model or "", getattr(device, "model_id", None) or ""]
        return any(TRV_ZBT_MODEL in model.upper() for model in models)

    @classmethod
    def discover_entities(
        cls, hass: HomeAssistant, device: DeviceEntry
    ) -> dict[str, str | None]:
        """Find the entities of the device by role, disabled entities included.

        On ZHA the valve entities are disabled by default.
        """
        registry = er.async_get(hass)
        found: dict[str, str | None] = {spec.role.key: None for spec in _ROLE_SPECS}
        for entry in er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        ):
            for spec in _ROLE_SPECS:
                if found[spec.role.key] is None and _matches(entry, spec):
                    found[spec.role.key] = entry.entity_id
        return found

    @classmethod
    def entity_roles(cls) -> list[EntityRole]:
        """Roles shown in the config flow."""
        return [spec.role for spec in _ROLE_SPECS]

    @classmethod
    def options_schema(cls) -> vol.Schema:
        """Options taken from the blueprint."""
        return vol.Schema(
            {
                vol.Required(
                    OPT_TEMPERATURE_SOURCE, default=TEMPERATURE_SOURCE_LOCAL
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[TEMPERATURE_SOURCE_LOCAL, TEMPERATURE_SOURCE_REMOTE],
                        translation_key="temperature_source",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(OPT_DELTA, default=DEFAULT_DELTA): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_DELTA,
                        max=MAX_DELTA,
                        step=0.2,
                        unit_of_measurement="°C",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Required(
                    OPT_PULSE_DELAY, default=DEFAULT_PULSE_DELAY
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_PULSE_DELAY,
                        max=MAX_PULSE_DELAY,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Required(OPT_HOT_STEP, default=False): BooleanSelector(),
            }
        )

    # ------------------------------------------------------------------
    # Instance
    # ------------------------------------------------------------------
    def __init__(
        self,
        hass: HomeAssistant,
        runtime: InterfaceThermostatRuntime,
        underlying_entity_id: str,
        entities: Mapping[str, str | None],
        options: Mapping[str, Any],
    ) -> None:
        super().__init__(hass, runtime, underlying_entity_id, entities, options)
        self._kick_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._pending = False
        # Opening of the last kick started or pending: a regulation cycle that
        # sends the same opening again does not kick again.
        self._last_kick_opening: int | None = None
        self._latest_opening: int | None = None
        self._last_idle_written: float | None = None
        # True after a hook announced the TRV leaving OFF: the kick has no
        # effect while the TRV is off, so it waits for the TRV state.
        self._wait_heat = False
        self._unsub_state: Any = None
        self._unsub_started: Any = None

    # -- small helpers -------------------------------------------------
    def _entity(self, role: str) -> str | None:
        return self.entities.get(role) or None

    def _state(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    def _trv_state(self) -> State | None:
        return self.hass.states.get(self.underlying_entity_id)

    def _trv_is_unavailable(self) -> bool:
        state = self._trv_state()
        return state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    def _trv_is_off(self) -> bool:
        state = self._trv_state()
        return state is not None and state.state == HVAC_OFF

    @property
    def _source_is_remote(self) -> bool:
        return self.options.get(OPT_TEMPERATURE_SOURCE) == TEMPERATURE_SOURCE_REMOTE

    async def _call(
        self, domain: str, service: str, data: dict[str, Any], blocking: bool
    ) -> bool:
        """Call a service, a failure is logged and never raised."""
        try:
            await self.hass.services.async_call(
                domain, service, data, blocking=blocking
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning(
                "%s - %s.%s %s failed: %s",
                self.underlying_entity_id,
                domain,
                service,
                data,
                err,
            )
            return False
        return True

    async def _set_number(
        self, entity_id: str, value: float, *, blocking: bool = True
    ) -> bool:
        return await self._call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking,
        )

    def _is_external(self) -> bool | None:
        """True when the TRV uses the external temperature, None when unknown."""
        entity_id = self._entity(ROLE_SENSOR_SELECT)
        state = self._state(entity_id)
        if entity_id is None or state is None:
            return None
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        if entity_id.startswith("switch."):
            return state.state == "on"
        return state.state == Z2M_SENSOR_REMOTE_OPTION

    async def _set_sensor(self, external: bool, *, blocking: bool = True) -> bool:
        """Select the external (True) or the built-in (False) temperature sensor."""
        entity_id = self._entity(ROLE_SENSOR_SELECT)
        if entity_id is None:
            return False
        if entity_id.startswith("switch."):
            return await self._call(
                "switch",
                "turn_on" if external else "turn_off",
                {"entity_id": entity_id},
                blocking,
            )
        option = Z2M_SENSOR_REMOTE_OPTION if external else Z2M_SENSOR_LOCAL_OPTION
        return await self._call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
            blocking,
        )

    async def _write_idle(self, value: int, *, blocking: bool) -> None:
        """Write idle_valve_position when it differs from the current value."""
        entity_id = self._entity(ROLE_IDLE_POSITION)
        state = self._state(entity_id)
        if entity_id is None or state is None or state.state == STATE_UNAVAILABLE:
            return
        current = _to_float(state.state)
        if (
            current is not None
            and current == value
            and self._last_idle_written in (None, value)
        ):
            return
        if await self._set_number(entity_id, value, blocking=blocking):
            self._last_idle_written = value

    def _spawn(self, coro, name: str) -> None:
        """Run a short background job and keep a reference to cancel it at stop."""
        task = self.hass.async_create_background_task(
            coro, f"vtherm_quirks {name} {self.underlying_entity_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sleep(self, seconds: float) -> None:
        """Sleep with a Home Assistant timer (cancellable, driven by the HA clock)."""
        future: asyncio.Future[None] = self.hass.loop.create_future()

        @callback
        def _done(_now) -> None:
            if not future.done():
                future.set_result(None)

        cancel = async_call_later(self.hass, seconds, _done)
        try:
            await future
        finally:
            cancel()

    # -- lifecycle -----------------------------------------------------
    async def async_start(self) -> None:
        """Listen to the TRV and put the device back in a clean state."""
        if self._unsub_state is not None:
            return
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.underlying_entity_id], self._on_trv_state
        )
        if self.hass.is_running:
            await self._async_startup_check()
        else:
            from homeassistant.helpers.start import async_at_started  # noqa: PLC0415

            self._unsub_started = async_at_started(
                self.hass, self._async_startup_check_at_started
            )

    async def _async_startup_check_at_started(self, _hass: HomeAssistant) -> None:
        self._unsub_started = None
        await self._async_startup_check()

    async def _async_startup_check(self) -> None:
        """Restore a source left external and close the valve of an OFF TRV."""
        if not self._source_is_remote and self._is_external() is True:
            _LOGGER.info(
                "%s - temperature source left external, restoring it",
                self.underlying_entity_id,
            )
            await self._set_sensor(False, blocking=False)
        if self._trv_is_off():
            await self._write_idle(0, blocking=False)

    async def async_stop(self) -> None:
        """Stop listening, cancel the kick (the finally restores the source)."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        await self._cancel_kick()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.wait(list(self._tasks))

    # -- hooks ---------------------------------------------------------
    async def async_after_valve_command(self, command: ValveCommand) -> None:
        """Sync idle_valve_position and ask for a kick."""
        cause = str(command.cause)
        self._latest_opening = command.opening_degree
        if cause == CAUSE_TURN_OFF:
            await self._async_turn_off()
            return
        if self._trv_is_unavailable():
            _LOGGER.debug(
                "%s - TRV unavailable, valve command ignored",
                self.underlying_entity_id,
            )
            return
        trv_off = self._trv_is_off()
        idle_target = 0 if trv_off else max(0, int(command.opening_degree) - 1)
        await self._write_idle(idle_target, blocking=False)
        if not trv_off:
            self._request_kick(command.opening_degree, force=cause != CAUSE_REGULATION)

    async def async_before_underlying_hvac_mode(
        self,
        climate_entity_id: str,
        hvac_mode: str,
        current_hvac_mode: str | None,
        command_cause: str | None,
    ) -> None:
        """Close the idle position before the TRV goes OFF, while it still heats."""
        if hvac_mode == HVAC_OFF:
            if current_hvac_mode != HVAC_OFF:
                await self._async_turn_off()
        elif current_hvac_mode == HVAC_OFF:
            self._wait_heat = True

    async def _async_turn_off(self) -> None:
        """Cancel the kick, restore the source and close idle_valve_position.

        Returns only when everything is written: the writes use blocking=True
        because the core awaits the hook before it sends the OFF mode.
        """
        self._last_kick_opening = None
        await self._cancel_kick()
        await self._write_idle(0, blocking=True)

    # -- TRV state tracking --------------------------------------------
    @callback
    def _on_trv_state(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        old_state = event.data["old_state"]
        if new_state is None:
            return
        if new_state.state == HVAC_OFF:
            if old_state is None or old_state.state != HVAC_OFF:
                self._spawn(self._async_turn_off(), "turn off")
        elif new_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            was_off = old_state is not None and old_state.state == HVAC_OFF
            if self._wait_heat or was_off:
                self._wait_heat = False
                self._spawn(self._async_resume(), "resume")

    async def _async_resume(self) -> None:
        """The TRV left OFF: sync the idle position and kick."""
        if self._latest_opening is None:
            return
        await self._write_idle(max(0, int(self._latest_opening) - 1), blocking=False)
        self._request_kick(self._latest_opening, force=True)

    # -- kick ----------------------------------------------------------
    def _kick_running(self) -> bool:
        return self._kick_task is not None and not self._kick_task.done()

    def _request_kick(self, opening: int | None, *, force: bool = False) -> None:
        """Ask for a kick. Requests during a kick are merged into one pending kick."""
        if not force and opening is not None and opening == self._last_kick_opening:
            return
        self._last_kick_opening = opening
        if self._kick_running():
            self._pending = True
            return
        self._kick_task = self.hass.async_create_background_task(
            self._kick_loop(), f"vtherm_quirks kick {self.underlying_entity_id}"
        )

    async def _cancel_kick(self) -> None:
        """Cancel the kick and the pending request, return once the source is restored."""
        self._pending = False
        task = self._kick_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])
        self._kick_task = None

    async def _kick_loop(self) -> None:
        while True:
            self._pending = False
            await self._kick_once()
            if not self._pending:
                return

    async def _kick_once(self) -> None:
        """One kick: fake a cold room so the TRV switches to heating."""
        state = self._trv_state()
        if state is None or state.state in (HVAC_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                "%s - TRV off or unavailable, kick aborted", self.underlying_entity_id
            )
            self._last_kick_opening = None
            return
        setpoint = _to_float(state.attributes.get("temperature"))
        external_entity = self._entity(ROLE_EXTERNAL_TEMPERATURE)
        if (
            setpoint is None
            or external_entity is None
            or self._entity(ROLE_SENSOR_SELECT) is None
        ):
            _LOGGER.debug("%s - no setpoint, kick aborted", self.underlying_entity_id)
            self._last_kick_opening = None
            return

        delta = float(self.options.get(OPT_DELTA, DEFAULT_DELTA))
        pulse = float(self.options.get(OPT_PULSE_DELAY, DEFAULT_PULSE_DELAY))
        saved: float | None = None
        if self._source_is_remote:
            saved = _to_float(getattr(self._state(external_entity), "state", None))
        was_external = self._is_external()

        # From the first write on, the finally restores the temperature source,
        # whatever happens (end of the kick, cancellation, service error).
        try:
            await self._set_number(
                external_entity, _clamp_temperature(setpoint - delta)
            )
            if was_external is not True:
                await self._set_sensor(True)
            await self._sleep(pulse)
            if self.options.get(OPT_HOT_STEP):
                await self._set_number(
                    external_entity, _clamp_temperature(setpoint + delta)
                )
                await self._sleep(pulse)
        finally:
            await self._restore_source(external_entity, saved)

    async def _restore_source(self, external_entity: str, saved: float | None) -> None:
        """Last step of the kick, also run on cancellation."""
        if self._source_is_remote:
            if saved is not None:
                await self._set_number(external_entity, saved)
        else:
            await self._set_sensor(False)

    # -- checks --------------------------------------------------------
    def check_configuration(self) -> list[QuirkIssue]:
        """Report the settings that prevent the quirk from working."""
        issues: list[QuirkIssue] = []
        infos = self.runtime.entry_infos
        index = self.underlying_index

        if infos.get(VT_CONF_SYNC_DEVICE_INTERNAL_TEMP):
            issues.append(QuirkIssue("sync_device_internal_temp"))

        closing = infos.get(VT_CONF_CLOSING_DEGREE_LIST) or []
        if index is not None and index < len(closing) and closing[index]:
            issues.append(
                QuirkIssue(
                    "closing_entity_configured",
                    placeholders={"entity": str(closing[index])},
                )
            )

        opening = infos.get(VT_CONF_OPENING_DEGREE_LIST) or []
        heating = self._entity(ROLE_HEATING_POSITION)
        if (
            index is not None
            and index < len(opening)
            and opening[index]
            and heating
            and opening[index] != heating
        ):
            issues.append(
                QuirkIssue(
                    "opening_entity_mismatch",
                    placeholders={"entity": str(opening[index]), "expected": heating},
                )
            )

        registry = er.async_get(self.hass)
        for role in _REQUIRED_ROLES:
            entity_id = self._entity(role)
            entry = registry.async_get(entity_id) if entity_id else None
            if entry is not None and entry.disabled:
                issues.append(
                    QuirkIssue("entity_disabled", placeholders={"entity": entity_id})
                )

        for role, key in (
            (ROLE_ADAPTIVE_CONTROL, "adaptive_control_enabled"),
            (ROLE_OPEN_WINDOW, "open_window_enabled"),
        ):
            entity_id = self._entity(role)
            state = self._state(entity_id)
            if state is not None and state.state == "on":
                issues.append(QuirkIssue(key, placeholders={"entity": entity_id}))
        return issues

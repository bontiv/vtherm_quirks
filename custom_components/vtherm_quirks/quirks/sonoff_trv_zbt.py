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
from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
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
    HVAC_OFF,
    SCOPE_OVER_CLIMATE_VALVE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UNDERLYING_CLIMATE,
    VT_CONF_CLOSING_DEGREE_LIST,
    VT_CONF_OPENING_DEGREE_LIST,
    VT_CONF_SYNC_DEVICE_INTERNAL_TEMP,
)
from ._zigbee_roles import RoleSpec, discover_role_entities, is_sonoff
from .base import BaseQuirk, EntityRole, QuirkIssue, clamp_temperature, to_float

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry
    from vtherm_api import ValveCommand

# ---------------------------------------------------------------------------
# Sonoff TRV-ZBT
#
# Verified = read in the sources of ZHA (zigpy/zha device dump of the TRV-ZBT
# and zigpy/zha-device-handlers zhaquirks/sonoff/trvzb.py) and of Zigbee2MQTT
# (zigbee-herdsman-converters src/devices/sonoff.ts).
# ---------------------------------------------------------------------------
TRV_ZBT_MODEL = "TRV-ZBT"  # verified (ZHA model, Z2M zigbeeModel/model)

# Options of the Z2M temperature_sensor_select (ZHA uses an on/off switch).
Z2M_SENSOR_LOCAL_OPTION = "local_temperature"  # verified
Z2M_SENSOR_REMOTE_OPTION = "remote_temperature"  # verified

# Range of external_temperature_input of the TRV-ZBT.
TRV_ZBT_MIN_EXTERNAL_TEMPERATURE = 0.0
TRV_ZBT_MAX_EXTERNAL_TEMPERATURE = 99.9

# Quirk options
OPT_TEMPERATURE_SOURCE = "temperature_source"
OPT_DELTA = "delta"
OPT_PULSE_DELAY = "pulse_delay"
OPT_HOT_STEP = "hot_step"

TEMPERATURE_SOURCE_LOCAL = "local"
TEMPERATURE_SOURCE_REMOTE = "remote"

DEFAULT_DELTA = 2.0
MIN_DELTA = 1.2
MAX_DELTA = 5.0
DEFAULT_PULSE_DELAY = 5
MIN_PULSE_DELAY = 1
MAX_PULSE_DELAY = 60

# Roles of the quirk
ROLE_HEATING_POSITION = "heating_position"
ROLE_IDLE_POSITION = "idle_position"
ROLE_EXTERNAL_TEMPERATURE = "external_temperature"
ROLE_SENSOR_SELECT = "sensor_select"
ROLE_ADAPTIVE_CONTROL = "adaptive_control"
ROLE_OPEN_WINDOW = "open_window"


# The Z2M expose names and the ZHA translation keys are verified (TRV-ZBT
# device dump and quirk). The ZHA attribute (unique_id suffix) is a fallback
# that was not checked against a real entity registry.
_ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(
        EntityRole(ROLE_HEATING_POSITION, ("number",)),
        z2m_name="heating_valve_position",
        zha_translation_key="valve_opening_degree",  # "Valve opening degree"
        zha_attribute="valve_opening_degree",
    ),
    RoleSpec(
        EntityRole(ROLE_IDLE_POSITION, ("number",)),
        z2m_name="idle_valve_position",
        zha_translation_key="valve_closing_degree",  # "Valve closing degree"
        zha_attribute="valve_closing_degree",
    ),
    RoleSpec(
        EntityRole(ROLE_EXTERNAL_TEMPERATURE, ("number",)),
        z2m_name="external_temperature_input",
        zha_translation_key="external_temperature_sensor_value",
        zha_attribute="external_temperature_sensor_value",
    ),
    RoleSpec(
        EntityRole(ROLE_SENSOR_SELECT, ("select", "switch")),
        z2m_name="temperature_sensor_select",
        zha_translation_key="external_temperature_sensor",  # a switch
        zha_attribute="external_temperature_sensor_enable",
    ),
    # Optional roles, only used by check_configuration.
    RoleSpec(
        EntityRole(ROLE_ADAPTIVE_CONTROL, ("switch",), required=False),
        z2m_name="smart_temperature_control",  # from the Z2M docs, not re-read
        zha_translation_key="adaptive_mode",  # a switch, on = adaptive
    ),
    RoleSpec(
        EntityRole(ROLE_OPEN_WINDOW, ("switch",), required=False),
        z2m_name="open_window",
        zha_translation_key="open_window",
    ),
)

_REQUIRED_ROLES = tuple(spec.role.key for spec in _ROLE_SPECS if spec.role.required)


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
        if not is_sonoff(device):
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
        return discover_role_entities(hass, device, _ROLE_SPECS)

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
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._kick_task: asyncio.Task | None = None
        # A kick was requested while one was running: run one more after it.
        self._pending = False
        # Opening of the last kick started or pending: a regulation cycle that
        # sends the same opening again does not kick again.
        self._last_kick_opening: int | None = None
        self._latest_opening: int | None = None
        self._last_idle_written: float | None = None
        # True after a hook announced the TRV leaving OFF: the kick has no
        # effect while the TRV is off, so it waits for the TRV state.
        self._wait_heat = False

    # -- device access -------------------------------------------------
    def _entity(self, role: str) -> str | None:
        return self.entities.get(role) or None

    def _state(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    def _trv_is_off(self) -> bool:
        state = self.underlying_state
        return state is not None and state.state == HVAC_OFF

    @property
    def _source_is_remote(self) -> bool:
        return self.options.get(OPT_TEMPERATURE_SOURCE) == TEMPERATURE_SOURCE_REMOTE

    async def _set_number(self, entity_id: str, value: float, *, blocking: bool = True) -> bool:
        data = {"entity_id": entity_id, "value": value}
        return await self.call_service("number", "set_value", data, blocking=blocking)

    def _is_external(self) -> bool | None:
        """True when the TRV uses the external temperature, None when unknown."""
        entity_id = self._entity(ROLE_SENSOR_SELECT)
        state = self._state(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        if entity_id.startswith("switch."):  # ZHA
            return state.state == "on"
        return state.state == Z2M_SENSOR_REMOTE_OPTION

    async def _set_sensor(self, external: bool, *, blocking: bool = True) -> bool:
        """Select the external (True) or the built-in (False) temperature sensor."""
        entity_id = self._entity(ROLE_SENSOR_SELECT)
        if entity_id is None:
            return False
        if entity_id.startswith("switch."):  # ZHA
            service = "turn_on" if external else "turn_off"
            return await self.call_service(
                "switch", service, {"entity_id": entity_id}, blocking=blocking
            )
        option = Z2M_SENSOR_REMOTE_OPTION if external else Z2M_SENSOR_LOCAL_OPTION
        return await self.call_service(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=blocking,
        )

    async def _write_idle(self, value: int, *, blocking: bool) -> None:
        """Write idle_valve_position, unless it already has this value or is unavailable."""
        entity_id = self._entity(ROLE_IDLE_POSITION)
        state = self._state(entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            self.log.debug("idle %s not written: %s missing or unavailable", value, entity_id)
            return
        if to_float(state.state) == value and self._last_idle_written in (None, value):
            return
        if await self._set_number(entity_id, value, blocking=blocking):
            self._last_idle_written = value

    # -- lifecycle -----------------------------------------------------
    async def async_startup(self) -> None:
        """Restore a source left external and close the valve of an OFF TRV."""
        if not self._source_is_remote and self._is_external() is True:
            self.log.info("temperature source left external, restoring it")
            await self._set_sensor(False, blocking=False)
        if self._trv_is_off():
            await self._write_idle(0, blocking=False)

    # The default async_stop cancels the kick task, whose finally restores the
    # temperature source.

    # -- hooks ---------------------------------------------------------
    async def async_after_valve_command(self, command: ValveCommand) -> None:
        """Sync idle_valve_position and ask for a kick."""
        self._latest_opening = command.opening_degree
        # The core closes the valve after it sent the OFF mode: the TRV can
        # still read heat in Home Assistant, so trust the command first.
        if str(command.cause) == CAUSE_TURN_OFF or str(command.hvac_mode) == HVAC_OFF:
            await self._async_turn_off()
            return
        if not self.underlying_available:
            self.log.debug("TRV unavailable, valve command ignored")
            return
        if self._trv_is_off():
            self.log.debug("TRV off: idle position closed, no kick")
            await self._write_idle(0, blocking=False)
            return
        await self._write_idle(max(0, int(command.opening_degree) - 1), blocking=False)
        force = str(command.cause) != CAUSE_REGULATION
        self._request_kick(command.opening_degree, force=force)

    async def async_before_underlying_hvac_mode(
        self,
        climate_entity_id: str,
        hvac_mode: str,
        current_hvac_mode: str | None,
        command_cause: str | None,
    ) -> None:
        """Close the idle position before the TRV goes OFF, while it still heats."""
        if hvac_mode == HVAC_OFF and current_hvac_mode != HVAC_OFF:
            await self._async_turn_off()
        elif hvac_mode != HVAC_OFF and current_hvac_mode == HVAC_OFF:
            self.log.debug("leaving OFF: the kick waits for the TRV to heat")
            self._wait_heat = True

    async def _async_turn_off(self) -> None:
        """Cancel the kick, restore the source and close idle_valve_position.

        Returns only when everything is written: the writes use blocking=True
        because the core awaits the hook before it sends the OFF mode.
        """
        self.log.debug("turning off: kick cancelled, idle position closed")
        self._last_kick_opening = None
        await self._cancel_kick()
        await self._write_idle(0, blocking=True)

    @callback
    def on_underlying_state(self, old_state: State | None, new_state: State) -> None:
        """Turn off with the TRV, kick again when it leaves OFF."""
        old = None if old_state is None else old_state.state
        new = new_state.state
        if new == HVAC_OFF:
            if old != HVAC_OFF:
                self.spawn(self._async_turn_off(), "turn off")
        elif new not in (STATE_UNAVAILABLE, STATE_UNKNOWN) and (
            self._wait_heat or old == HVAC_OFF
        ):
            self._wait_heat = False
            self.spawn(self._async_resume(), "resume")

    async def _async_resume(self) -> None:
        """The TRV left OFF: sync the idle position and kick."""
        if self._latest_opening is None:
            return
        self.log.debug("TRV left OFF, resuming with opening %s", self._latest_opening)
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
        else:
            self._kick_task = self.spawn(self._kick_loop(), "kick")

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
        state = self.underlying_state
        setpoint = None if state is None else to_float(state.attributes.get("temperature"))
        external = self._entity(ROLE_EXTERNAL_TEMPERATURE)
        if (
            state is None
            or state.state in (HVAC_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN)
            or setpoint is None
            or external is None
            or self._entity(ROLE_SENSOR_SELECT) is None
        ):
            self.log.debug(
                "kick aborted: TRV state %s, setpoint %s, external temperature %s, sensor select %s",
                None if state is None else state.state,
                setpoint,
                external,
                self._entity(ROLE_SENSOR_SELECT),
            )
            self._last_kick_opening = None
            return

        delta = float(self.options.get(OPT_DELTA, DEFAULT_DELTA))
        pulse = float(self.options.get(OPT_PULSE_DELAY, DEFAULT_PULSE_DELAY))
        saved = None
        if self._source_is_remote:
            saved = to_float(getattr(self._state(external), "state", None))
        was_external = self._is_external()
        bounds = (TRV_ZBT_MIN_EXTERNAL_TEMPERATURE, TRV_ZBT_MAX_EXTERNAL_TEMPERATURE)
        cold = clamp_temperature(setpoint - delta, *bounds)
        hot = clamp_temperature(setpoint + delta, *bounds)
        self.log.debug("kick: setpoint %s, delta %s, pulse %ss", setpoint, delta, pulse)
        # From the first write on, the finally restores the temperature source,
        # whatever happens (end of the kick, cancellation, service error).
        try:
            await self._set_number(external, cold)
            if was_external is not True:
                await self._set_sensor(True)
            await self.async_sleep(pulse)
            if self.options.get(OPT_HOT_STEP):
                await self._set_number(external, hot)
                await self.async_sleep(pulse)
        finally:
            await self._restore_source(external, saved)

    async def _restore_source(self, external: str, saved: float | None) -> None:
        """Last step of the kick, also run on cancellation."""
        if not self._source_is_remote:
            await self._set_sensor(False)
        elif saved is not None:
            await self._set_number(external, saved)

    # -- checks --------------------------------------------------------
    def check_configuration(self) -> list[QuirkIssue]:
        """Report the settings that prevent the quirk from working."""
        issues: list[QuirkIssue] = []
        infos = self.runtime.entry_infos
        index = self.underlying_index

        def configured(key: str) -> str | None:
            """Entity of this underlying in a VTherm list of entities."""
            values = infos.get(key) or []
            return values[index] if index is not None and index < len(values) else None

        if infos.get(VT_CONF_SYNC_DEVICE_INTERNAL_TEMP):
            issues.append(QuirkIssue("sync_device_internal_temp"))

        if closing := configured(VT_CONF_CLOSING_DEGREE_LIST):
            issues.append(
                QuirkIssue("closing_entity_configured", placeholders={"entity": str(closing)})
            )

        opening = configured(VT_CONF_OPENING_DEGREE_LIST)
        heating = self._entity(ROLE_HEATING_POSITION)
        if opening and heating and opening != heating:
            issues.append(
                QuirkIssue(
                    "opening_entity_mismatch",
                    placeholders={"entity": str(opening), "expected": heating},
                )
            )

        registry = er.async_get(self.hass)
        for role in _REQUIRED_ROLES:
            entity_id = self._entity(role)
            entry = registry.async_get(entity_id) if entity_id else None
            if entry is not None and entry.disabled:
                issues.append(QuirkIssue("entity_disabled", placeholders={"entity": entity_id}))

        for role, key in (
            (ROLE_ADAPTIVE_CONTROL, "adaptive_control_enabled"),
            (ROLE_OPEN_WINDOW, "open_window_enabled"),
        ):
            entity_id = self._entity(role)
            state = self._state(entity_id)
            if state is not None and state.state == "on":
                issues.append(QuirkIssue(key, placeholders={"entity": entity_id}))
        return issues

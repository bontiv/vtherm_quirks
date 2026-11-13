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

It also closes the valve firmly (the "slam close"): the motor of the TRV-ZBT
loses its calibration and can stop before the seat when it closes from a small
opening. A close from an opening below ``pre_open`` first opens the valve to
``pre_open`` for ``slam_delay`` seconds:

* before the TRV goes OFF, inside the hook that the core awaits (and cuts after
  10 seconds): the heating position is set to ``pre_open`` and a cold fake
  temperature moves the valve there, then the OFF sent by the core moves it to
  the idle position, 0;
* when the regulation closes the valve, in a background task that takes over
  the write of the core (``handled=True``): after the pre-opening, the heating
  position is set to the requested opening and a hot fake temperature sends
  the TRV to idle, so the valve goes to the idle position.

The valve recalibration of VTherm writes the heating position to 100 then to 0
while the TRV is in heat, which does not move the motor. The quirk holds the
idle position at 0 and sends the TRV to heating (cold fake temperature), then
to idle (hot fake temperature).
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
from vtherm_api import ValveWriteDecision

from ..const import (
    CAUSE_RECALIBRATION,
    CAUSE_REGULATION,
    CAUSE_TURN_OFF,
    HVAC_OFF,
    SCOPE_OVER_CLIMATE_VALVE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UNDERLYING_CLIMATE,
    VT_CONF_CLOSING_DEGREE_LIST,
    VT_CONF_MAX_CLOSING_DEGREE,
    VT_CONF_OPENING_DEGREE_LIST,
    VT_CONF_SYNC_DEVICE_INTERNAL_TEMP,
)
from ._zigbee_roles import RoleSpec, discover_role_entities, is_sonoff
from .base import BaseQuirk, EntityRole, QuirkIssue, clamp_temperature, to_float

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry
    from vtherm_api import ValveCommand, ValveWriteRequest

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
OPT_PRE_OPEN = "pre_open"
OPT_SLAM_DELAY = "slam_delay"

TEMPERATURE_SOURCE_LOCAL = "local"
TEMPERATURE_SOURCE_REMOTE = "remote"

DEFAULT_DELTA = 2.0
MIN_DELTA = 1.2
MAX_DELTA = 5.0
DEFAULT_PULSE_DELAY = 5
MIN_PULSE_DELAY = 1
MAX_PULSE_DELAY = 60

# The slam close only runs for a close from an opening below the pre-opening.
DEFAULT_PRE_OPEN = 10
MAX_PRE_OPEN = 100
# At most 9 seconds: before OFF the slam close runs inside a hook that the core
# cuts after 10 seconds.
DEFAULT_SLAM_DELAY = 5
MIN_SLAM_DELAY = 1
MAX_SLAM_DELAY = 9

# The slam close before OFF returns within OFF_HOOK_BUDGET seconds: its wait is
# shortened when the writes were slow, keeping RESTORE_RESERVE seconds for the
# restoration of the temperature source.
OFF_HOOK_BUDGET = 9.5
RESTORE_RESERVE = 0.5
# Added to the duration of the slam close of the regulation when the core is
# told how long it lasts (``settle_sec``): the writes wait for the Zigbee network.
SETTLE_MARGIN = 5.0
# A recalibration followed by no command is ended after this delay (seconds).
RECALIBRATION_TIMEOUT = 900.0

# Cause of the commands that follow a change made on the device itself.
CAUSE_UNDERLYING_TRACKING = "underlying_tracking"

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
                vol.Required(OPT_PRE_OPEN, default=DEFAULT_PRE_OPEN): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=MAX_PRE_OPEN,
                        step=1,
                        unit_of_measurement="%",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Required(OPT_SLAM_DELAY, default=DEFAULT_SLAM_DELAY): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SLAM_DELAY,
                        max=MAX_SLAM_DELAY,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
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
        # Last opening commanded by VTherm: the opening a close starts from.
        self._latest_opening: int | None = None
        self._last_idle_written: float | None = None
        # True after a hook announced the TRV leaving OFF: the kick has no
        # effect while the TRV is off, so it waits for the TRV state.
        self._wait_heat = False
        # Slam close of the regulation: its task, the opening it ends on and
        # the time (loop clock) at which the core may repair the valve again.
        self._slam_task: asyncio.Task | None = None
        self._slam_target: int | None = None
        self._slam_deadline = 0.0
        # Valve recalibration of VTherm: running, its safety timer, and the
        # temperature source to restore once it ends.
        self._recalibrating = False
        self._recal_task: asyncio.Task | None = None
        self._recal_touched = False
        self._recal_saved: float | None = None

    # -- device access -------------------------------------------------
    def _now(self) -> float:
        return self.hass.loop.time()

    def _entity(self, role: str) -> str | None:
        return self.entities.get(role) or None

    def _state(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    def _writable(self, entity_id: str | None) -> bool:
        state = self._state(entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE

    def _trv_is_off(self) -> bool:
        state = self.underlying_state
        return state is not None and state.state == HVAC_OFF

    def _trv_is_on(self) -> bool:
        state = self.underlying_state
        return state is not None and state.state not in (HVAC_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN)

    def _runtime_mode(self) -> str | None:
        mode = getattr(self.runtime, "vtherm_hvac_mode", None)
        return None if mode is None else str(mode)

    @property
    def _source_is_remote(self) -> bool:
        return self.options.get(OPT_TEMPERATURE_SOURCE) == TEMPERATURE_SOURCE_REMOTE

    @property
    def _delta(self) -> float:
        return float(self.options.get(OPT_DELTA, DEFAULT_DELTA))

    @property
    def _pulse_delay(self) -> float:
        return float(self.options.get(OPT_PULSE_DELAY, DEFAULT_PULSE_DELAY))

    @property
    def _pre_open(self) -> int:
        return int(self.options.get(OPT_PRE_OPEN, DEFAULT_PRE_OPEN))

    @property
    def _slam_delay(self) -> float:
        return float(self.options.get(OPT_SLAM_DELAY, DEFAULT_SLAM_DELAY))

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

    @staticmethod
    def _idle_for(opening: float) -> int:
        """Idle position that goes with an opening: the valve ends there whatever the TRV state."""
        return max(0, int(opening) - 1)

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

    # -- fake temperature ----------------------------------------------
    def _fake_targets(self) -> tuple[float, str] | None:
        """Setpoint of the TRV and external temperature entity, None when no fake temperature can be sent."""
        state = self.underlying_state
        setpoint = None if state is None else to_float(state.attributes.get("temperature"))
        external = self._entity(ROLE_EXTERNAL_TEMPERATURE)
        if setpoint is None or external is None or self._entity(ROLE_SENSOR_SELECT) is None:
            return None
        return setpoint, external

    def _cold(self, setpoint: float) -> float:
        """Fake temperature below the setpoint: the TRV goes to heating."""
        bounds = (TRV_ZBT_MIN_EXTERNAL_TEMPERATURE, TRV_ZBT_MAX_EXTERNAL_TEMPERATURE)
        return clamp_temperature(setpoint - self._delta, *bounds)

    def _hot(self, setpoint: float) -> float:
        """Fake temperature above the setpoint: the TRV goes to idle."""
        bounds = (TRV_ZBT_MIN_EXTERNAL_TEMPERATURE, TRV_ZBT_MAX_EXTERNAL_TEMPERATURE)
        return clamp_temperature(setpoint + self._delta, *bounds)

    def _saved_external(self, external: str) -> float | None:
        """Value of the external temperature to restore, with the remote source."""
        if not self._source_is_remote:
            return None
        return to_float(getattr(self._state(external), "state", None))

    async def _send_fake(self, external: str, value: float, was_external: bool | None) -> None:
        """Write a fake temperature and select the external sensor if needed."""
        await self._set_number(external, value)
        if was_external is not True:
            await self._set_sensor(True)

    async def _restore_source(self, external: str, saved: float | None) -> None:
        """Last step of a kick or a slam close, also run on cancellation."""
        if not self._source_is_remote:
            await self._set_sensor(False)
        elif saved is not None:
            await self._set_number(external, saved)

    # -- lifecycle -----------------------------------------------------
    async def async_startup(self) -> None:
        """Restore a source left external and close the valve of an OFF TRV."""
        if not self._source_is_remote and self._is_external() is True:
            self.log.info("temperature source left external, restoring it")
            await self._set_sensor(False, blocking=False)
        if self._trv_is_off():
            await self._write_idle(0, blocking=False)

    async def async_stop(self) -> None:
        """End a recalibration, then cancel the tasks.

        The kick and the slam close restore the temperature source in their
        finally when they are cancelled.
        """
        await self._end_recalibration()
        await super().async_stop()

    # -- hooks ---------------------------------------------------------
    async def async_after_valve_command(self, command: ValveCommand) -> None:
        """Sync idle_valve_position and ask for a kick."""
        cause = str(command.cause)
        if cause == CAUSE_RECALIBRATION:
            await self._recalibration_step(int(command.percent_open))
            return
        if self._recalibrating:
            self.log.debug("command %s after the recalibration: recalibration ended", cause)
            await self._end_recalibration()
        self._latest_opening = command.opening_degree
        # The core closes the valve after it sent the OFF mode: the TRV can
        # still read heat in Home Assistant, so trust the command first.
        if cause == CAUSE_TURN_OFF or str(command.hvac_mode) == HVAC_OFF:
            await self._async_turn_off()
            return
        if command.written_by_plugin:
            # The slam close writes the idle and heating positions itself.
            return
        if not self.underlying_available:
            self.log.debug("TRV unavailable, valve command ignored")
            return
        if self._trv_is_off():
            self.log.debug("TRV off: idle position closed, no kick")
            await self._write_idle(0, blocking=False)
            return
        await self._write_idle(self._idle_for(command.opening_degree), blocking=False)
        force = cause != CAUSE_REGULATION
        self._request_kick(command.opening_degree, force=force)

    async def async_before_underlying_hvac_mode(
        self,
        climate_entity_id: str,
        hvac_mode: str,
        current_hvac_mode: str | None,
        command_cause: str | None,
    ) -> None:
        """Close the valve before the TRV goes OFF, while it still heats.

        A TRV turned on while VTherm stays OFF is the start of a recalibration.
        """
        if hvac_mode == HVAC_OFF and current_hvac_mode != HVAC_OFF:
            await self._async_turn_off(slam=True)
        elif hvac_mode != HVAC_OFF and current_hvac_mode == HVAC_OFF:
            if (
                self._runtime_mode() == HVAC_OFF
                and command_cause != CAUSE_UNDERLYING_TRACKING
            ):
                await self._start_recalibration()
                return
            self.log.debug("leaving OFF: the kick waits for the TRV to heat")
            self._wait_heat = True

    async def async_before_valve_write(
        self, request: ValveWriteRequest
    ) -> ValveWriteDecision | None:
        """Take over the write of the core when the regulation closes the valve.

        Only a close from an opening below ``pre_open`` is taken over, while the
        TRV heats. The closes of a turn off are done before the OFF mode, in
        :meth:`async_before_underlying_hvac_mode`. Any other write is left to
        the core (``None``).
        """
        opening = int(request.opening_degree)

        if not self._is_closed(opening):
            if self._slam_running():
                # A new opening during the slam close: drop it before the core
                # writes this opening, so that nothing of the slam comes after.
                self.log.debug("opening %s during the slam close, slam cancelled", opening)
                await self._cancel_slam()
            return None

        if self._slam_running():
            # A close sent again (repair): the slam close ends on it.
            self._slam_target = opening
            return self._handled(opening, max(self._slam_deadline - self._now(), SETTLE_MARGIN))

        if (
            str(request.cause) in (CAUSE_TURN_OFF, CAUSE_RECALIBRATION)
            or str(request.hvac_mode) == HVAC_OFF
            or self._recalibrating
        ):
            return None

        previous = self._latest_opening
        if not self._slam_wanted(previous) or not self._can_slam():
            return None

        duration = self._slam_delay + self._pulse_delay + SETTLE_MARGIN
        self.log.debug(
            "closing from %s: slam close through %s%% for %ss", previous, self._pre_open, duration
        )
        await self._cancel_kick()
        # A regulation cycle that sends the same close again does not kick.
        self._last_kick_opening = opening
        self._slam_target = opening
        self._slam_deadline = self._now() + duration
        self._slam_task = self.spawn(self._slam_close(), "slam close")
        return self._handled(opening, duration)

    @staticmethod
    def _handled(opening: int, settle_sec: float) -> ValveWriteDecision:
        """The quirk writes the valve: it ends at ``opening``, the core waits ``settle_sec``."""
        return ValveWriteDecision(handled=True, opening_degree=opening, settle_sec=settle_sec)

    async def _async_turn_off(self, *, slam: bool = False) -> None:
        """Cancel the kick, the slam close and the recalibration, then close idle_valve_position.

        With ``slam`` (called before the OFF mode is sent), a close from an
        opening below ``pre_open`` first goes through the pre-opening.

        Returns only when everything is written: the writes use blocking=True
        because the core awaits the hook before it sends the OFF mode.
        """
        deadline = self._now() + OFF_HOOK_BUDGET
        previous = self._latest_opening
        self.log.debug("turning off: kick cancelled, idle position closed")
        self._last_kick_opening = None
        await self._cancel_kick()
        await self._cancel_slam()
        await self._end_recalibration()
        if slam and self._slam_wanted(previous) and self._can_slam():
            await self._slam_before_off(previous, deadline)
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
            if self._recalibrating:
                return
            self.spawn(self._async_resume(), "resume")

    async def _async_resume(self) -> None:
        """The TRV left OFF: sync the idle position and kick."""
        if self._latest_opening is None:
            return
        self.log.debug("TRV left OFF, resuming with opening %s", self._latest_opening)
        await self._write_idle(self._idle_for(self._latest_opening), blocking=False)
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
        targets = self._fake_targets()
        if not self._trv_is_on() or targets is None:
            self.log.debug(
                "kick aborted: TRV state %s, setpoint %s, external temperature %s, sensor select %s",
                None if state is None else state.state,
                None if state is None else state.attributes.get("temperature"),
                self._entity(ROLE_EXTERNAL_TEMPERATURE),
                self._entity(ROLE_SENSOR_SELECT),
            )
            self._last_kick_opening = None
            return

        setpoint, external = targets
        pulse = self._pulse_delay
        saved = self._saved_external(external)
        was_external = self._is_external()
        self.log.debug("kick: setpoint %s, delta %s, pulse %ss", setpoint, self._delta, pulse)
        # From the first write on, the finally restores the temperature source,
        # whatever happens (end of the kick, cancellation, service error).
        try:
            await self._send_fake(external, self._cold(setpoint), was_external)
            await self.async_sleep(pulse)
            if self.options.get(OPT_HOT_STEP):
                await self._set_number(external, self._hot(setpoint))
                await self.async_sleep(pulse)
        finally:
            await self._restore_source(external, saved)

    # -- slam close ----------------------------------------------------
    def _closed_floor(self) -> float:
        """Highest opening that VTherm sends for a closed valve."""
        max_closing = to_float(self.runtime.entry_infos.get(VT_CONF_MAX_CLOSING_DEGREE))
        return 0.0 if max_closing is None else max(0.0, 100.0 - max_closing)

    def _is_closed(self, opening: float) -> bool:
        return opening <= self._closed_floor()

    def _slam_wanted(self, previous: float | None) -> bool:
        """True for a close from an opening below the pre-opening."""
        pre_open = self._pre_open
        return (
            pre_open > 0
            and previous is not None
            and not self._is_closed(previous)
            and previous < pre_open
        )

    def _can_slam(self) -> bool:
        """True when the TRV heats and every entity of the slam close can be written."""
        if not self._trv_is_on() or self._fake_targets() is None:
            self.log.debug("no slam close: TRV not heating or no fake temperature possible")
            return False
        if not (
            self._writable(self._entity(ROLE_HEATING_POSITION))
            and self._writable(self._entity(ROLE_IDLE_POSITION))
        ):
            self.log.debug("no slam close: valve entities missing or unavailable")
            return False
        return True

    def _slam_running(self) -> bool:
        return self._slam_task is not None and not self._slam_task.done()

    async def _slam_before_off(self, previous: int, deadline: float) -> None:
        """Pre-opening before the OFF mode: the OFF then moves the valve to idle, 0.

        The wait is shortened so that the hook returns before ``deadline``.
        """
        targets = self._fake_targets()
        heating = self._entity(ROLE_HEATING_POSITION)
        if targets is None or heating is None:
            return
        setpoint, external = targets
        pre_open = self._pre_open
        saved = self._saved_external(external)
        was_external = self._is_external()
        self.log.debug("turning off from %s: slam close through %s%%", previous, pre_open)
        try:
            await self._write_idle(0, blocking=True)
            await self._set_number(heating, pre_open)
            await self._send_fake(external, self._cold(setpoint), was_external)
            wait = min(self._slam_delay, deadline - RESTORE_RESERVE - self._now())
            if wait < self._slam_delay - 0.1:
                self.log.debug(
                    "pre-opening shortened to %.1fs to return before the core cuts the hook",
                    max(wait, 0.0),
                )
            if wait > 0:
                await self.async_sleep(wait)
        finally:
            await self._restore_source(external, saved)

    async def _slam_close(self) -> None:
        """Slam close of the regulation: pre-opening, then idle at the requested opening.

        The finally always restores the temperature source.
        """
        targets = self._fake_targets()
        heating = self._entity(ROLE_HEATING_POSITION)
        if targets is None or heating is None:
            return
        setpoint, external = targets
        saved = self._saved_external(external)
        was_external = self._is_external()
        try:
            await self._write_idle(self._idle_for(self._slam_target or 0), blocking=True)
            await self._set_number(heating, self._pre_open)
            await self._send_fake(external, self._cold(setpoint), was_external)
            await self.async_sleep(self._slam_delay)
            target = self._slam_target or 0
            await self._write_idle(self._idle_for(target), blocking=True)
            await self._set_number(heating, target)
            await self._set_number(external, self._hot(setpoint))
            await self.async_sleep(self._pulse_delay)
        finally:
            self._slam_target = None
            await self._restore_source(external, saved)

    async def _cancel_slam(self) -> None:
        """Drop the slam close of the regulation, return once the source is restored."""
        task = self._slam_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])
        self._slam_task = None
        self._slam_target = None

    # -- recalibration -------------------------------------------------
    async def _start_recalibration(self) -> None:
        """The TRV is turned on while VTherm is OFF: a recalibration starts."""
        if self._recalibrating:
            return
        self.log.debug("recalibration started: no kick until it ends")
        self._wait_heat = False
        self._last_kick_opening = None
        self._latest_opening = None
        await self._cancel_kick()
        await self._cancel_slam()
        self._recalibrating = True
        self._recal_touched = False
        self._recal_saved = None
        self._recal_task = self.spawn(self._recalibration_guard(), "recalibration")
        await self._write_idle(0, blocking=False)

    async def _recalibration_step(self, percent_open: int) -> None:
        """Full opening: idle 0 and cold, the TRV heats at 100. Full closing: hot, the TRV goes idle at 0.

        The temperature source is only restored when the recalibration ends.
        """
        if not self._recalibrating:
            await self._start_recalibration()
        targets = self._fake_targets()
        if targets is None:
            self.log.debug("recalibration step %s%% ignored: no fake temperature possible", percent_open)
            return
        setpoint, external = targets
        if not self._recal_touched:
            self._recal_saved = self._saved_external(external)
            self._recal_touched = True
        if percent_open > 0:
            await self._write_idle(0, blocking=True)
            value = self._cold(setpoint)
        else:
            value = self._hot(setpoint)
        self.log.debug("recalibration step %s%%: fake temperature %s", percent_open, value)
        await self._send_fake(external, value, self._is_external())

    async def _recalibration_guard(self) -> None:
        """Safety timer: ends a recalibration that no command ends."""
        await self.async_sleep(RECALIBRATION_TIMEOUT)
        self.log.warning(
            "no command %ss after the recalibration: temperature source restored",
            RECALIBRATION_TIMEOUT,
        )
        self._recal_task = None
        await self._finish_recalibration()

    async def _end_recalibration(self) -> None:
        """End the recalibration, if one runs, and restore the temperature source."""
        task = self._recal_task
        self._recal_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])
        await self._finish_recalibration()

    async def _finish_recalibration(self) -> None:
        if not self._recalibrating:
            return
        self._recalibrating = False
        if not self._recal_touched:
            return
        self._recal_touched = False
        external = self._entity(ROLE_EXTERNAL_TEMPERATURE)
        if external is not None:
            await self._restore_source(external, self._recal_saved)

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

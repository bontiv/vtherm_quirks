"""Quirk of the Moes TRV801 / Tuya TRV601 radiator valve under Zigbee2MQTT.

In Zigbee2MQTT the Moes TRV801_1 is a white label of the Tuya ``TRV601``. One
Tuya data point carries the mode of the device and the climate entity exposes
it twice: as the ``preset`` (``auto``, ``manual``, ``off``, ``on``, read and
write) and as the ``system_mode`` (read only). The hvac mode that VTherm sends
does not choose the mode of the TRV, so the TRV can stay in ``on`` (valve
forced open) while VTherm heats.

This quirk drives the preset of the climate itself, which the core neither
sends nor tracks:

* VTherm heats: the preset must be ``manual`` (regulation on the setpoint), or
  ``auto`` (weekly schedule of the TRV) with the option ``keep_schedule``;
* VTherm is off: the preset must be ``off``.

The preset is set before the hvac mode of VTherm is sent, then watched. A TRV
that keeps leaving the wanted preset is corrected three times, then reported in
the repairs and left alone until VTherm changes mode.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.helpers.selector import BooleanSelector

from ..const import (
    HVAC_OFF,
    SCOPE_OVER_CLIMATE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UNDERLYING_CLIMATE,
)
from .base import BaseQuirk, QuirkIssue

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry
    from vtherm_api import InterfaceThermostatRuntime

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Moes TRV801 / Tuya TRV601
#
# Verified = read in the sources of Zigbee2MQTT (zigbee-herdsman-converters
# src/devices/tuya.ts). How these names and fingerprints appear in the Home
# Assistant device registry has not been checked on a real device: both the
# model names and the fingerprints are searched in the model, the model id and
# the manufacturer.
# ---------------------------------------------------------------------------
# Z2M model names: TRV601 (Tuya), TRV801_1 (Moes), SBDV-00185 (Sber).
TRV601_MODELS = ("TRV601", "TRV801_1", "SBDV-00185")  # names verified, registry form not
# Zigbee fingerprints (manufacturer name): Moes TRV801_1 and Sber SBDV-00185.
TRV601_FINGERPRINTS = ("_TZE204_rtrmfadk", "_TZE204_cvcu2p6e")  # verified

Z2M_PLATFORM = "mqtt"  # Zigbee2MQTT entities come from the MQTT integration

# Presets of the climate entity of the TRV (the Tuya data point 1).
PRESET_MANUAL = "manual"  # verified: regulation on the setpoint
PRESET_OFF = "off"  # verified: valve closed
PRESET_ON = "on"  # verified: valve forced open
PRESET_AUTO = "auto"  # verified: weekly schedule of the TRV

# Quirk option
OPT_KEEP_SCHEDULE = "keep_schedule"

# Corrections in a row before the repair issue, as in the tracking of the core.
MAX_CORRECTIONS = 3
# A preset already sent is not sent again before this delay (seconds): the TRV
# needs time to report its new preset.
RESEND_DELAY = 10.0

ISSUE_PRESET_KEEPS_CHANGING = "preset_keeps_changing"
ISSUE_OUTDATED_CONVERTER = "outdated_converter"


class MoesTrv601Quirk(BaseQuirk):
    """Keeps the preset of the TRV in line with the mode of VTherm."""

    quirk_id: ClassVar[str] = "moes_trv601"
    title: ClassVar[str] = "Moes TRV801 / Tuya TRV601 (Zigbee2MQTT)"
    underlying_types: ClassVar[frozenset[str]] = frozenset({UNDERLYING_CLIMATE})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({SCOPE_OVER_CLIMATE})

    # ------------------------------------------------------------------
    # Discovery and configuration
    # ------------------------------------------------------------------
    @classmethod
    def match_device(cls, device: DeviceEntry) -> bool:
        """True for a TRV601 / TRV801 / SBDV-00185.

        The model names are searched in the model and the model id, the
        fingerprints in the model id and the manufacturer.
        """
        model = (device.model or "").upper()
        model_id = (getattr(device, "model_id", None) or "").upper()
        manufacturer = (device.manufacturer or "").upper()
        if any(name.upper() in text for name in TRV601_MODELS for text in (model, model_id)):
            return True
        return any(
            fingerprint.upper() in text
            for fingerprint in TRV601_FINGERPRINTS
            for text in (model_id, manufacturer)
        )

    @classmethod
    def options_schema(cls) -> vol.Schema:
        """Accept the schedule of the TRV when VTherm heats."""
        return vol.Schema(
            {vol.Required(OPT_KEEP_SCHEDULE, default=False): BooleanSelector()}
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
        self._tasks: set[Any] = set()
        self._unsub_state: Any = None
        self._unsub_started: Any = None
        # Mode announced by the last hook and the mode of VTherm at that time:
        # the hook mode only holds while VTherm keeps this mode (the core can
        # turn the underlying off, for a window, while VTherm stays in heat).
        self._hook_mode: str | None = None
        self._hook_runtime_mode: str | None = None
        # Preset wanted at the last check, corrections done for it.
        self._target: str | None = None
        self._corrections = 0
        self._stuck: QuirkIssue | None = None
        self._sent_target: str | None = None
        self._sent_at: float | None = None
        self._outdated = False

    # -- helpers -------------------------------------------------------
    @property
    def _keep_schedule(self) -> bool:
        return bool(self.options.get(OPT_KEEP_SCHEDULE))

    def _now(self) -> float:
        return self.hass.loop.time()

    def _trv_state(self) -> State | None:
        return self.hass.states.get(self.underlying_entity_id)

    def _runtime_mode(self) -> str | None:
        mode = getattr(self.runtime, "vtherm_hvac_mode", None)
        return None if mode is None else str(mode)

    def _wanted_mode(self) -> str | None:
        """Mode of the underlying wanted by VTherm, None when unknown."""
        runtime_mode = self._runtime_mode()
        if self._hook_mode is not None and runtime_mode == self._hook_runtime_mode:
            return self._hook_mode
        return runtime_mode

    @staticmethod
    def _target_preset(mode: str) -> str:
        return PRESET_OFF if mode == HVAC_OFF else PRESET_MANUAL

    def _accepted(self, target: str) -> tuple[str, ...]:
        if target == PRESET_MANUAL and self._keep_schedule:
            return (PRESET_MANUAL, PRESET_AUTO)
        return (target,)

    @staticmethod
    def _preset_of(state: State | None) -> str | None:
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        preset = state.attributes.get("preset_mode")
        return str(preset) if preset else None

    def _spawn(self, coro: Any, name: str) -> None:
        """Run a short background job and keep a reference to cancel it at stop."""
        task = self.hass.async_create_background_task(
            coro, f"vtherm_quirks {name} {self.underlying_entity_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _send_preset(self, preset: str) -> None:
        """Start the preset change in a background task, without waiting for it."""
        self._sent_target = preset
        self._sent_at = self._now()
        self._spawn(self._async_set_preset(preset), f"preset {preset}")

    async def _async_set_preset(self, preset: str) -> None:
        """Call climate.set_preset_mode, a failure is logged and never raised."""
        _LOGGER.debug("%s - setting the preset %s", self.underlying_entity_id, preset)
        try:
            await self.hass.services.async_call(
                "climate",
                "set_preset_mode",
                {"entity_id": self.underlying_entity_id, "preset_mode": preset},
                blocking=False,
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning(
                "%s - climate.set_preset_mode %s failed: %s",
                self.underlying_entity_id,
                preset,
                err,
            )

    def _change_target(self, target: str) -> None:
        """A new preset is wanted: forget the corrections and the stuck issue."""
        if target == self._target:
            return
        _LOGGER.debug(
            "%s - wanted preset changes from %s to %s, corrections reset",
            self.underlying_entity_id,
            self._target,
            target,
        )
        self._target = target
        self._corrections = 0
        self._sent_target = None
        if self._stuck is not None:
            self._stuck = None
            self.request_issue_sync()

    # -- lifecycle -----------------------------------------------------
    async def async_start(self) -> None:
        """Listen to the TRV and check its preset once at startup."""
        if self._unsub_state is not None:
            _LOGGER.debug("%s - async_start: already started", self.underlying_entity_id)
            return
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.underlying_entity_id], self._on_trv_state
        )
        if self.hass.is_running:
            _LOGGER.debug(
                "%s - async_start: listening to the TRV, checking the preset now",
                self.underlying_entity_id,
            )
            self._enforce()
        else:
            _LOGGER.debug(
                "%s - async_start: listening to the TRV, preset check deferred until Home Assistant has started",
                self.underlying_entity_id,
            )
            from homeassistant.helpers.start import async_at_started  # noqa: PLC0415

            self._unsub_started = async_at_started(
                self.hass, self._async_startup_check_at_started
            )

    async def _async_startup_check_at_started(self, _hass: HomeAssistant) -> None:
        self._unsub_started = None
        self._enforce()

    async def async_stop(self) -> None:
        """Stop listening and cancel the running calls."""
        _LOGGER.debug(
            "%s - async_stop: %s background task(s) to cancel",
            self.underlying_entity_id,
            len(self._tasks),
        )
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    # -- hook ----------------------------------------------------------
    async def async_before_underlying_hvac_mode(
        self,
        climate_entity_id: str,
        hvac_mode: str,
        current_hvac_mode: str | None,
        command_cause: str | None,
    ) -> None:
        """Set the preset that matches the mode, without waiting for the TRV."""
        if climate_entity_id != self.underlying_entity_id:
            _LOGGER.debug(
                "%s - before hvac mode ignored: climate %s is not this underlying",
                self.underlying_entity_id,
                climate_entity_id,
            )
            return
        mode = str(hvac_mode)
        self._hook_mode = mode
        self._hook_runtime_mode = self._runtime_mode()
        target = self._target_preset(mode)
        self._change_target(target)
        state = self._trv_state()
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                "%s - before hvac mode %s: TRV unavailable (state %s), preset left as is",
                self.underlying_entity_id,
                mode,
                None if state is None else state.state,
            )
            return
        preset = self._preset_of(state)
        if preset not in self._accepted(target):
            _LOGGER.debug(
                "%s - before hvac mode %s (current %s, cause %s): target preset %s, current preset %s, accepted %s: sending the preset",
                self.underlying_entity_id,
                mode,
                current_hvac_mode,
                command_cause,
                target,
                preset,
                self._accepted(target),
            )
            self._send_preset(target)
        else:
            _LOGGER.debug(
                "%s - before hvac mode %s (current %s, cause %s): target preset %s, current preset %s is accepted %s: nothing sent",
                self.underlying_entity_id,
                mode,
                current_hvac_mode,
                command_cause,
                target,
                preset,
                self._accepted(target),
            )

    # -- TRV state tracking --------------------------------------------
    @callback
    def _on_trv_state(self, event: Event[EventStateChangedData]) -> None:
        if event.data["new_state"] is None:
            return
        self._enforce()

    def _enforce(self) -> None:
        """Check the preset against the wanted mode and correct it if needed."""
        state = self._trv_state()
        preset = self._preset_of(state)
        if preset is None:
            _LOGGER.debug(
                "%s - enforce: no usable preset (state %s), nothing to check",
                self.underlying_entity_id,
                None if state is None else state.state,
            )
            return
        self._check_outdated(state, preset)
        mode = self._wanted_mode()
        if mode is None:
            _LOGGER.debug(
                "%s - enforce: preset %s, wanted mode unknown, nothing to check",
                self.underlying_entity_id,
                preset,
            )
            return
        target = self._target_preset(mode)
        self._change_target(target)
        if preset in self._accepted(target):
            _LOGGER.debug(
                "%s - enforce: ok, state %s, preset %s, wanted mode %s, target %s, accepted %s",
                self.underlying_entity_id,
                None if state is None else state.state,
                preset,
                mode,
                target,
                self._accepted(target),
            )
            return
        if self._stuck is not None:
            _LOGGER.debug(
                "%s - enforce: stuck, preset %s instead of %s, left alone until the mode changes",
                self.underlying_entity_id,
                preset,
                target,
            )
            return
        if (
            self._sent_target == target
            and self._sent_at is not None
            and self._now() - self._sent_at < RESEND_DELAY
        ):
            _LOGGER.debug(
                "%s - enforce: preset %s instead of %s, but %s was sent %.1fs ago (resend delay %ss), waiting",
                self.underlying_entity_id,
                preset,
                target,
                target,
                self._now() - self._sent_at,
                RESEND_DELAY,
            )
            return
        if self._corrections >= MAX_CORRECTIONS:
            _LOGGER.warning(
                "%s - the preset keeps leaving %s (now %s), corrections stopped",
                self.underlying_entity_id,
                target,
                preset,
            )
            self._stuck = QuirkIssue(
                ISSUE_PRESET_KEEPS_CHANGING,
                placeholders={"preset": target, "current": preset},
            )
            self.request_issue_sync()
            return
        self._corrections += 1
        _LOGGER.info(
            "%s - preset %s instead of %s, correction %s/%s",
            self.underlying_entity_id,
            preset,
            target,
            self._corrections,
            MAX_CORRECTIONS,
        )
        self._send_preset(target)

    def _check_outdated(self, state: State | None, preset: str) -> None:
        """Preset manual shown as auto: Zigbee2MQTT converter without the fix."""
        outdated = (
            state is not None and preset == PRESET_MANUAL and state.state == PRESET_AUTO
        )
        if outdated != self._outdated:
            _LOGGER.debug(
                "%s - outdated converter %s -> %s (state %s, preset %s)",
                self.underlying_entity_id,
                self._outdated,
                outdated,
                None if state is None else state.state,
                preset,
            )
            self._outdated = outdated
            self.request_issue_sync()

    # -- checks --------------------------------------------------------
    def check_configuration(self) -> list[QuirkIssue]:
        """Report the converter that is too old and a TRV that leaves its preset."""
        issues: list[QuirkIssue] = []
        state = self._trv_state()
        preset = self._preset_of(state)
        if preset is not None and state is not None:
            self._outdated = preset == PRESET_MANUAL and state.state == PRESET_AUTO
        if self._outdated:
            issues.append(
                QuirkIssue(ISSUE_OUTDATED_CONVERTER, severity=ir.IssueSeverity.ERROR)
            )
        if self._stuck is not None:
            issues.append(self._stuck)
        return issues

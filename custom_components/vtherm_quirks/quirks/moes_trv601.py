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

from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import State, callback
from homeassistant.helpers import issue_registry as ir
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
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        # Mode announced by the last hook and the mode of VTherm at that time:
        # the hook mode only holds while VTherm keeps this mode (the core can
        # turn the underlying off, for a window, while VTherm stays in heat).
        self._hook_mode: str | None = None
        self._hook_runtime_mode: str | None = None
        # Preset wanted at the last check, corrections done for it.
        self._target: str | None = None
        self._corrections = 0
        self._stuck: QuirkIssue | None = None
        # Last preset sent and when.
        self._sent_target: str | None = None
        self._sent_at: float | None = None
        # Outdated converter last reported to the repairs.
        self._outdated = False

    # -- helpers -------------------------------------------------------
    def _now(self) -> float:
        return self.hass.loop.time()

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
        if target == PRESET_MANUAL and self.options.get(OPT_KEEP_SCHEDULE):
            return (PRESET_MANUAL, PRESET_AUTO)
        return (target,)

    @staticmethod
    def _preset_of(state: State | None) -> str | None:
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        preset = state.attributes.get("preset_mode")
        return str(preset) if preset else None

    @staticmethod
    def _is_outdated(state: State, preset: str) -> bool:
        """Preset manual shown as state auto: Zigbee2MQTT converter without the fix."""
        return preset == PRESET_MANUAL and state.state == PRESET_AUTO

    def _send_preset(self, preset: str) -> None:
        """Set the preset in a background task, without waiting for the TRV."""
        self._sent_target = preset
        self._sent_at = self._now()
        data = {"entity_id": self.underlying_entity_id, "preset_mode": preset}
        self.spawn(
            self.call_service("climate", "set_preset_mode", data, blocking=False),
            f"preset {preset}",
        )

    def _change_target(self, target: str) -> None:
        """A new preset is wanted: forget the corrections and the stuck issue."""
        if target == self._target:
            return
        self.log.debug("wanted preset %s -> %s, corrections reset", self._target, target)
        self._target = target
        self._corrections = 0
        self._sent_target = None
        if self._stuck is not None:
            self._stuck = None
            self.request_issue_sync()

    # -- lifecycle and hook --------------------------------------------
    async def async_startup(self) -> None:
        """Check the preset once Home Assistant is running."""
        self._enforce()

    async def async_before_underlying_hvac_mode(
        self,
        climate_entity_id: str,
        hvac_mode: str,
        current_hvac_mode: str | None,
        command_cause: str | None,
    ) -> None:
        """Set the preset that matches the mode, without waiting for the TRV."""
        if climate_entity_id != self.underlying_entity_id:
            return
        mode = str(hvac_mode)
        self._hook_mode = mode
        self._hook_runtime_mode = self._runtime_mode()
        target = self._target_preset(mode)
        self._change_target(target)
        if not self.underlying_available:
            self.log.debug("TRV unavailable, preset %s not sent", target)
            return
        if self._preset_of(self.underlying_state) not in self._accepted(target):
            self._send_preset(target)

    # -- TRV state tracking --------------------------------------------
    @callback
    def on_underlying_state(self, old_state: State | None, new_state: State) -> None:
        self._enforce()

    def _enforce(self) -> None:
        """Check the preset against the wanted mode and correct it if needed."""
        state = self.underlying_state
        preset = self._preset_of(state)
        if state is None or preset is None:
            return
        if (outdated := self._is_outdated(state, preset)) != self._outdated:
            self._outdated = outdated
            self.request_issue_sync()

        mode = self._wanted_mode()
        if mode is None:
            return
        target = self._target_preset(mode)
        self._change_target(target)
        if preset in self._accepted(target) or self._stuck is not None:
            return
        if (
            self._sent_target == target
            and self._sent_at is not None
            and self._now() - self._sent_at < RESEND_DELAY
        ):
            self.log.debug("preset %s instead of %s, waiting for the TRV", preset, target)
            return
        if self._corrections >= MAX_CORRECTIONS:
            self.log.warning(
                "the preset keeps leaving %s (now %s), corrections stopped", target, preset
            )
            self._stuck = QuirkIssue(
                ISSUE_PRESET_KEEPS_CHANGING,
                placeholders={"preset": target, "current": preset},
            )
            self.request_issue_sync()
            return
        self._corrections += 1
        self.log.info(
            "preset %s instead of %s, correction %s/%s",
            preset,
            target,
            self._corrections,
            MAX_CORRECTIONS,
        )
        self._send_preset(target)

    # -- checks --------------------------------------------------------
    def check_configuration(self) -> list[QuirkIssue]:
        """Report the converter that is too old and a TRV that leaves its preset."""
        state = self.underlying_state
        preset = self._preset_of(state)
        if state is not None and preset is not None:
            self._outdated = self._is_outdated(state, preset)
        issues: list[QuirkIssue] = []
        if self._outdated:
            issues.append(
                QuirkIssue(ISSUE_OUTDATED_CONVERTER, severity=ir.IssueSeverity.ERROR)
            )
        if self._stuck is not None:
            issues.append(self._stuck)
        return issues

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

With the option ``legacy_converter``, for a Zigbee2MQTT converter that shows the
preset ``manual`` as the mode ``auto`` and the forced open valve as ``heat``, the
quirk also translates the hvac mode for the core (contract of ``vtherm_api``
0.10.0): ``heat`` is sent as ``auto``, ``auto`` is read as ``heat`` and ``heat``
is read as unsupported. ``off`` is never translated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import State
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import BooleanSelector
from vtherm_api import UNSUPPORTED_HVAC_MODE

from ..const import HVAC_OFF, SCOPE_OVER_CLIMATE, UNDERLYING_CLIMATE
from ._tuya_preset import (
    ISSUE_PRESET_KEEPS_CHANGING,
    MAX_CORRECTIONS,
    RESEND_DELAY,
    TuyaPresetQuirk,
)
from .base import QuirkIssue

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

# Modes of the climate entity of the TRV, as seen by the core.
MODE_HEAT = "heat"
MODE_AUTO = "auto"

# Quirk options
OPT_KEEP_SCHEDULE = "keep_schedule"
OPT_LEGACY_CONVERTER = "legacy_converter"

ISSUE_OUTDATED_CONVERTER = "outdated_converter"

# Shared with the other Tuya preset quirks, kept importable from this module.
__all__ = [
    "ISSUE_OUTDATED_CONVERTER",
    "ISSUE_PRESET_KEEPS_CHANGING",
    "MAX_CORRECTIONS",
    "RESEND_DELAY",
    "MoesTrv601Quirk",
]


class MoesTrv601Quirk(TuyaPresetQuirk):
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
        """Accept the schedule of the TRV when VTherm heats, translate the modes of an old converter."""
        return vol.Schema(
            {
                vol.Required(OPT_KEEP_SCHEDULE, default=False): BooleanSelector(),
                vol.Required(OPT_LEGACY_CONVERTER, default=False): BooleanSelector(),
            }
        )

    # ------------------------------------------------------------------
    # Instance
    # ------------------------------------------------------------------
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        # Outdated converter last reported to the repairs.
        self._outdated = False

    # -- extension points of TuyaPresetQuirk ---------------------------
    def _target_preset(self, mode: str) -> str:
        return PRESET_OFF if mode == HVAC_OFF else PRESET_MANUAL

    def _accepted(self, target: str) -> tuple[str, ...]:
        if target == PRESET_MANUAL and self.options.get(OPT_KEEP_SCHEDULE):
            return (PRESET_MANUAL, PRESET_AUTO)
        return (target,)

    @property
    def _legacy(self) -> bool:
        return bool(self.options.get(OPT_LEGACY_CONVERTER))

    def _is_outdated(self, state: State, preset: str) -> bool:
        """Preset manual shown as state auto: Zigbee2MQTT converter without the fix.

        With the option ``legacy_converter`` this display is expected, and the
        core reads it through the translation of the modes.
        """
        return (
            not self._legacy
            and preset == PRESET_MANUAL
            and state.state == PRESET_AUTO
        )

    # -- translation of the hvac modes (contract of vtherm_api 0.10.0) --
    def to_underlying_hvac_mode(self, climate_entity_id: str, hvac_mode: str) -> str | None:
        """Legacy converter: the mode that regulates is ``auto``, not ``heat``."""
        if not self._legacy or climate_entity_id != self.underlying_entity_id:
            return None
        return MODE_AUTO if hvac_mode == MODE_HEAT else None

    def from_underlying_hvac_mode(
        self, climate_entity_id: str, underlying_hvac_mode: str
    ) -> str | None:
        """Legacy converter: ``auto`` is the regulation, ``heat`` the valve forced open."""
        if not self._legacy or climate_entity_id != self.underlying_entity_id:
            return None
        if underlying_hvac_mode == MODE_AUTO:
            return MODE_HEAT
        if underlying_hvac_mode == MODE_HEAT:
            return UNSUPPORTED_HVAC_MODE
        return None

    def _check_state(self, state: State, preset: str) -> None:
        if (outdated := self._is_outdated(state, preset)) != self._outdated:
            self._outdated = outdated
            self.request_issue_sync()

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
        issues.extend(super().check_configuration())
        return issues

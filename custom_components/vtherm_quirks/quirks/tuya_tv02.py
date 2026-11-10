"""Quirk of the Tuya TV02-Zigbee radiator valve and its white labels.

The TV02 keeps its mode in a Tuya data point (DP2) shown as the ``preset`` of
the climate: ``auto`` (weekly schedule of the valve), ``manual`` (regulation on
the setpoint) and ``holiday``. Stopping goes through another data point
(``heating_stop``). Zigbee2MQTT shows ``heat`` whatever the preset, so a valve
back on its schedule (button, end of holiday, reset) is never put back in
manual by VTherm: the core does not send an hvac mode the climate already
has. The schedule then overwrites the setpoint of VTherm.

This quirk keeps the preset ``manual`` (``none`` under ZHA) while VTherm heats,
and after a correction from the schedule it writes the setpoint of VTherm again.
It imposes nothing while VTherm or the valve is off: writing ``manual`` would
cancel the heating stop and switch the valve back on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import State
from homeassistant.helpers.selector import BooleanSelector

from ..const import HVAC_OFF, SCOPE_OVER_CLIMATE, UNDERLYING_CLIMATE
from ._tuya_preset import TuyaPresetQuirk
from .base import EntityRole, QuirkIssue, clamp_temperature, to_float

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry

# ---------------------------------------------------------------------------
# Tuya TV02-Zigbee
#
# Verified = read in the sources of Zigbee2MQTT (zigbee-herdsman-converters
# src/devices/tuya.ts, TV02-Zigbee definition and its white labels) and of ZHA
# (zha-device-handlers, ZONNSMARTThermostat). How the names appear in the Home
# Assistant device registry has not been checked on a real device.
# ---------------------------------------------------------------------------
# Z2M model names (exact match): the model and its white labels.
TV02_MODELS = (
    "TV02-Zigbee",
    "TV01-ZB",
    "ZTRV-ZX-TV01-MS",
    "ZTRV-ZX-TV02",
    "HA-08_THERMO",
    "TV06",
    "ETT-8",
    "TSL-TRV-TV01ZG",
    "TSL-TRV-TV05ZG",
    "TRV06-1",
    "GTZ08",
)
# ZHA: model TS0601 with one of these Zigbee manufacturer names.
TV02_ZHA_MODEL = "TS0601"
TV02_FINGERPRINTS = tuple(
    f"_TZE200_{suffix}"
    for suffix in (
        "sur6q7ko",
        "hue3yfsn",
        "e9ba97vf",
        "husqqvux",
        "lnbfnyxd",
        "fsow0qsk",
        "lllliz3p",
        "mudxchsu",
        "7yoranx2",
        "kds0pmmv",
        "py4cm3he",
        "wsbfwodu",
        "x9axofse",
        "lhzapfg9",
        "k1tumq4t",
    )
)

# Presets under Zigbee2MQTT and under ZHA.
Z2M_MANUAL, Z2M_SCHEDULE = "manual", "auto"
ZHA_MANUAL, ZHA_SCHEDULE = "none", "schedule"
PRESET_HOLIDAY = "holiday"
# Presets after which the setpoint of the valve is not the one of VTherm.
_SETPOINT_CHANGING_PRESETS = (Z2M_SCHEDULE, ZHA_SCHEDULE, PRESET_HOLIDAY)

HVAC_HEAT = "heat"

# Quirk options
OPT_KEEP_SCHEDULE = "keep_schedule"
OPT_RESTORE_SETPOINT = "restore_setpoint"

ROLE_OPEN_WINDOW = "open_window"

# Gap between the setpoint of the valve and the one of VTherm under which the
# setpoint is not written again.
SETPOINT_TOLERANCE = 0.25

ISSUE_FOLLOW_UNDERLYING = "follow_underlying_enabled"


def _model_matches(text: str, name: str) -> bool:
    """Exact name, or a Zigbee2MQTT description ending with "(name)"."""
    return text == name or text.endswith(f"({name})")


class TuyaTv02Quirk(TuyaPresetQuirk):
    """Keeps the TV02 off its own schedule while VTherm heats."""

    quirk_id: ClassVar[str] = "tuya_tv02"
    title: ClassVar[str] = "Tuya TV02-Zigbee / Moes TV01-ZB"
    underlying_types: ClassVar[frozenset[str]] = frozenset({UNDERLYING_CLIMATE})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({SCOPE_OVER_CLIMATE})

    # ------------------------------------------------------------------
    # Discovery and configuration
    # ------------------------------------------------------------------
    @classmethod
    def match_device(cls, device: DeviceEntry) -> bool:
        """True for a TV02 or one of its white labels, under Zigbee2MQTT or ZHA.

        The model names are matched exactly (some are short, like TV06), the
        fingerprints only with the generic Tuya model TS0601.
        """
        model = (device.model or "").strip().upper()
        model_id = (getattr(device, "model_id", None) or "").strip().upper()
        manufacturer = (device.manufacturer or "").strip().upper()
        if any(
            _model_matches(text, name.upper())
            for name in TV02_MODELS
            for text in (model, model_id)
        ):
            return True
        if TV02_ZHA_MODEL not in (model, model_id):
            return False
        fingerprints = {fingerprint.upper() for fingerprint in TV02_FINGERPRINTS}
        return manufacturer in fingerprints or model_id in fingerprints

    @classmethod
    def entity_roles(cls) -> list[EntityRole]:
        """Only an optional switch, to report the open window detection of the valve."""
        return [EntityRole(ROLE_OPEN_WINDOW, ("switch",), required=False)]

    @classmethod
    def options_schema(cls) -> vol.Schema:
        """Keep the schedule of the valve, restore the setpoint after a correction."""
        return vol.Schema(
            {
                vol.Required(OPT_KEEP_SCHEDULE, default=False): BooleanSelector(),
                vol.Required(OPT_RESTORE_SETPOINT, default=True): BooleanSelector(),
            }
        )

    # ------------------------------------------------------------------
    # Instance
    # ------------------------------------------------------------------
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        # A correction from the schedule or holiday was sent: write the
        # setpoint of VTherm once the valve is back in manual.
        self._restore_pending = False

    # -- presets of the integration ------------------------------------
    def _is_zha(self) -> bool:
        state = self.underlying_state
        modes = [] if state is None else state.attributes.get("preset_modes") or []
        return ZHA_MANUAL in modes or ZHA_SCHEDULE in modes

    def _manual(self) -> str:
        return ZHA_MANUAL if self._is_zha() else Z2M_MANUAL

    def _schedule(self) -> str:
        return ZHA_SCHEDULE if self._is_zha() else Z2M_SCHEDULE

    # -- extension points of TuyaPresetQuirk ---------------------------
    def _target_preset(self, mode: str) -> str | None:
        # Off: nothing. Writing manual would cancel the heating stop.
        return None if mode == HVAC_OFF else self._manual()

    def _accepted(self, target: str) -> tuple[str, ...]:
        if self.options.get(OPT_KEEP_SCHEDULE):
            return (target, self._schedule())
        return (target,)

    def _may_enforce(self, state: State) -> bool:
        # A valve turned off by the core (window, power shedding) while VTherm
        # stays in heat is left alone.
        return state.state == HVAC_HEAT

    def _on_corrected(self, old: str, target: str) -> None:
        if (
            old in _SETPOINT_CHANGING_PRESETS
            and self.options.get(OPT_RESTORE_SETPOINT, True)
            and not self.options.get(OPT_KEEP_SCHEDULE)
        ):
            self._restore_pending = True

    def _after_accepted(self, state: State, target: str) -> None:
        if not self._restore_pending:
            return
        self._restore_pending = False
        expected = self._expected_setpoint()
        current = to_float(state.attributes.get("temperature"))
        if expected is None:
            return
        if current is not None and abs(current - expected) <= SETPOINT_TOLERANCE:
            return
        minimum = to_float(state.attributes.get("min_temp"))
        maximum = to_float(state.attributes.get("max_temp"))
        value = clamp_temperature(
            expected,
            float("-inf") if minimum is None else minimum,
            float("inf") if maximum is None else maximum,
        )
        self.log.info("setpoint %s left by the schedule, %s written again", current, value)
        data = {"entity_id": self.underlying_entity_id, "temperature": value}
        self.spawn(
            self.call_service("climate", "set_temperature", data, blocking=False),
            "restore setpoint",
        )

    # -- setpoint of VTherm --------------------------------------------
    def _over_climate_attributes(self) -> dict[str, Any]:
        """The ``vtherm_over_climate`` attribute of the VTherm (internal, not a contract)."""
        entity_id = getattr(self.runtime, "entity_id", None)
        state = self.hass.states.get(entity_id) if entity_id else None
        data = None if state is None else state.attributes.get("vtherm_over_climate")
        return data if isinstance(data, dict) else {}

    def _expected_setpoint(self) -> float | None:
        """Setpoint VTherm last sent to this valve, or its regulated or target temperature."""
        tracking = self._over_climate_attributes().get("underlying_temperature_tracking")
        underlyings = tracking.get("underlyings") if isinstance(tracking, dict) else None
        mine = underlyings.get(self.underlying_entity_id) if isinstance(underlyings, dict) else None
        if isinstance(mine, dict) and (sent := to_float(mine.get("last_sent_temperature"))) is not None:
            return sent
        for name in ("regulated_target_temperature", "target_temperature"):
            if (value := to_float(getattr(self.runtime, name, None))) is not None:
                return value
        return None

    # -- checks --------------------------------------------------------
    def check_configuration(self) -> list[QuirkIssue]:
        """Report the following of the underlying setpoint and the open window detection."""
        issues: list[QuirkIssue] = []
        if self._over_climate_attributes().get("follow_underlying_temp_change") is True:
            issues.append(QuirkIssue(ISSUE_FOLLOW_UNDERLYING))
        window = self.entities.get(ROLE_OPEN_WINDOW)
        state = self.hass.states.get(window) if window else None
        if state is not None and state.state == "on":
            issues.append(QuirkIssue("open_window_enabled", placeholders={"entity": window}))
        issues.extend(super().check_configuration())
        return issues

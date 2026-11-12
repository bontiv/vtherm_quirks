"""Quirk of the Eurotronic Spirit Zigbee (SPZB0001) under Zigbee2MQTT.

The Spirit only follows ``valve_position`` when its ``trv_mode`` is ``1``. In
mode ``2`` (the default) it regulates on its own setpoint and ignores the
position. Zigbee2MQTT does not configure the reporting of these two
attributes: their state in Home Assistant is what was last written, so a
device back in mode 2 by itself stays invisible.

This quirk keeps ``trv_mode`` on ``1`` when VTherm drives the valve directly
and on ``2`` otherwise. It writes it at startup, when the thermostat leaves
OFF (the device may have rebooted meanwhile), after the valve commands of
VTherm and when the select changes. After a switch to ``1`` it writes the last
opening of VTherm again.

ZHA does not expose ``trv_mode`` nor ``valve_position``: it is not covered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)

from ..const import (
    CAUSE_RECALIBRATION,
    CAUSE_REPAIR,
    CAUSE_STARTUP,
    HVAC_OFF,
    SCOPE_OVER_CLIMATE,
    STATE_UNAVAILABLE,
    UNDERLYING_CLIMATE,
    VT_AUTO_REGULATION_VALVE,
    VT_CONF_AUTO_REGULATION_MODE,
    VT_CONF_CLOSING_DEGREE_LIST,
    VT_CONF_OPENING_DEGREE_LIST,
)
from ._zigbee_roles import RoleSpec, discover_role_entities, is_z2m
from .base import BaseQuirk, EntityRole, QuirkIssue, to_float

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry
    from vtherm_api import ValveCommand

# ---------------------------------------------------------------------------
# Eurotronic Spirit Zigbee
#
# Verified = read in the sources of Zigbee2MQTT (zigbee-herdsman-converters
# src/devices/eurotronic.ts).
# ---------------------------------------------------------------------------
EUROTRONIC_MANUFACTURER = "EUROTRONIC"  # verified
SPZB0001_MODEL = "SPZB0001"  # verified: Z2M model id, or "(SPZB0001)" in older models

TRV_MODE_VALVE = "1"  # verified: the valve follows valve_position
TRV_MODE_AUTO = "2"  # verified: internal regulation (default)

ROLE_TRV_MODE = "trv_mode"
ROLE_VALVE_POSITION = "valve_position"

# A mode already written is not written again before this delay (seconds).
RESEND_DELAY = 10.0
# Corrections before the repair issue, in a sliding window (seconds).
MAX_CORRECTIONS = 3
CORRECTION_WINDOW = 3600.0
# Maximum of a percent scale: an opening entity with a higher max is not in percent.
PERCENT_MAX = 100.0

ISSUE_TRV_MODE_KEEPS_CHANGING = "trv_mode_keeps_changing"
ISSUE_VALVE_SCALE_PERCENT = "valve_scale_percent"
ISSUE_CLOSING_NOT_SUPPORTED = "closing_entity_not_supported"

_ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(EntityRole(ROLE_TRV_MODE, ("select",)), z2m_name="trv_mode"),
    RoleSpec(
        EntityRole(ROLE_VALVE_POSITION, ("number",), required=False),
        z2m_name="valve_position",
    ),
)


class EurotronicSpzb0001Quirk(BaseQuirk):
    """Keeps the trv_mode of the Spirit Zigbee in line with the valve control of VTherm."""

    quirk_id: ClassVar[str] = "eurotronic_spzb0001"
    title: ClassVar[str] = "Eurotronic Spirit Zigbee SPZB0001 (Zigbee2MQTT)"
    underlying_types: ClassVar[frozenset[str]] = frozenset({UNDERLYING_CLIMATE})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({SCOPE_OVER_CLIMATE})

    # ------------------------------------------------------------------
    # Discovery and configuration
    # ------------------------------------------------------------------
    @classmethod
    def match_device(cls, device: DeviceEntry) -> bool:
        """True for an Eurotronic SPZB0001 paired with Zigbee2MQTT."""
        if (device.manufacturer or "").strip().upper() != EUROTRONIC_MANUFACTURER:
            return False
        if not is_z2m(device):
            return False
        model = (device.model or "").upper()
        model_id = (getattr(device, "model_id", None) or "").upper()
        return SPZB0001_MODEL in model or SPZB0001_MODEL in model_id

    @classmethod
    def discover_entities(
        cls, hass: HomeAssistant, device: DeviceEntry
    ) -> dict[str, str | None]:
        return discover_role_entities(hass, device, _ROLE_SPECS)

    @classmethod
    def entity_roles(cls) -> list[EntityRole]:
        return [spec.role for spec in _ROLE_SPECS]

    # ------------------------------------------------------------------
    # Instance
    # ------------------------------------------------------------------
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._last_opening: int | None = None
        # Highest opening seen in a command of VTherm (scale check fallback).
        self._max_commanded: int | None = None
        # Last mode written and when.
        self._sent_mode: str | None = None
        self._sent_at: float | None = None
        self._corrections: list[float] = []
        self._stuck: QuirkIssue | None = None

    # -- helpers -------------------------------------------------------
    def _now(self) -> float:
        return self.hass.loop.time()

    def _entity(self, role: str) -> str | None:
        return self.entities.get(role) or None

    def _state(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    def _wanted_mode(self) -> str:
        """trv_mode 1 with the direct valve control of VTherm, 2 otherwise."""
        regulation = self.runtime.entry_infos.get(VT_CONF_AUTO_REGULATION_MODE)
        return TRV_MODE_VALVE if regulation == VT_AUTO_REGULATION_VALVE else TRV_MODE_AUTO

    def _reset_corrections(self) -> None:
        self._corrections.clear()
        self._sent_mode = None
        if self._stuck is not None:
            self._stuck = None
            self.request_issue_sync()

    # -- lifecycle -----------------------------------------------------
    async def async_start(self) -> None:
        """Also watch the trv_mode select."""
        started = self._started
        await super().async_start()
        select = self._entity(ROLE_TRV_MODE)
        if not started and select:
            self._unsubs.append(
                async_track_state_change_event(self.hass, [select], self._on_mode_event)
            )

    async def async_startup(self) -> None:
        """Write the wanted mode once Home Assistant is running."""
        self._enforce("startup", force=False, count=False)

    # -- hooks ---------------------------------------------------------
    async def async_before_underlying_hvac_mode(
        self,
        climate_entity_id: str,
        hvac_mode: str,
        current_hvac_mode: str | None,
        command_cause: str | None,
    ) -> None:
        """Leaving OFF: write the mode again, the device may have rebooted meanwhile."""
        if climate_entity_id != self.underlying_entity_id:
            return
        if str(hvac_mode) != HVAC_OFF and current_hvac_mode == HVAC_OFF:
            self._reset_corrections()
            self._enforce("leaving off", force=True, count=False)

    async def async_after_valve_command(self, command: ValveCommand) -> None:
        """Remember the opening and check the mode."""
        if str(command.cause) == CAUSE_RECALIBRATION:
            # Forced full strokes: not a regulation opening to remember.
            return
        self._last_opening = int(command.opening_degree)
        if self._max_commanded is None or self._last_opening > self._max_commanded:
            self._max_commanded = self._last_opening
            self.request_issue_sync()
        force = str(command.cause) in (CAUSE_STARTUP, CAUSE_REPAIR)
        self._enforce(f"valve command {command.cause}", force=force, count=not force)

    # -- mode enforcement ----------------------------------------------
    @callback
    def _on_mode_event(self, event: Event[EventStateChangedData]) -> None:
        if event.data["new_state"] is not None:
            self._enforce("trv_mode changed", force=False, count=True)

    def _enforce(self, reason: str, *, force: bool, count: bool) -> None:
        """Write the wanted trv_mode when it differs (or always with ``force``)."""
        entity_id = self._entity(ROLE_TRV_MODE)
        state = self._state(entity_id)
        if entity_id is None or state is None or state.state == STATE_UNAVAILABLE:
            return
        wanted = self._wanted_mode()
        current = state.state
        if not force:
            if current == wanted or self._stuck is not None:
                return
            if (
                self._sent_mode == wanted
                and self._sent_at is not None
                and self._now() - self._sent_at < RESEND_DELAY
            ):
                self.log.debug("trv_mode %s instead of %s, waiting for the device", current, wanted)
                return
        if count:
            now = self._now()
            self._corrections = [t for t in self._corrections if now - t < CORRECTION_WINDOW]
            if len(self._corrections) >= MAX_CORRECTIONS:
                self.log.warning(
                    "trv_mode keeps leaving %s (now %s), corrections stopped", wanted, current
                )
                self._stuck = QuirkIssue(
                    ISSUE_TRV_MODE_KEEPS_CHANGING,
                    placeholders={"mode": wanted, "current": current},
                )
                self.request_issue_sync()
                return
            self._corrections.append(now)
        self.log.info("trv_mode %s -> %s (%s)", current, wanted, reason)
        self._sent_mode = wanted
        self._sent_at = self._now()
        self.spawn(self._write_mode(entity_id, wanted), f"trv_mode {wanted}")

    async def _write_mode(self, entity_id: str, mode: str) -> None:
        data = {"entity_id": entity_id, "option": mode}
        if not await self.call_service("select", "select_option", data):
            return
        if mode == TRV_MODE_VALVE:
            await self._reapply_opening()

    async def _reapply_opening(self) -> None:
        """Write the last opening of VTherm again: the device ignored it in mode 2."""
        position = self._entity(ROLE_VALVE_POSITION)
        if position is None or self._last_opening is None:
            return
        state = self._state(position)
        if state is None or state.state == STATE_UNAVAILABLE:
            return
        await self.call_service(
            "number", "set_value", {"entity_id": position, "value": self._last_opening}
        )

    # -- checks --------------------------------------------------------
    def _effective_max_opening(self) -> float | None:
        """Max opening VTherm uses for this valve, from its state, else from the commands."""
        entity_id = getattr(self.runtime, "entity_id", None)
        state = self.hass.states.get(entity_id) if entity_id else None
        valve = None if state is None else state.attributes.get("vtherm_over_climate_valve")
        valves = valve.get("underlying_valves") if isinstance(valve, dict) else None
        opening = self._configured(VT_CONF_OPENING_DEGREE_LIST)
        mine = valves.get(opening) if isinstance(valves, dict) and opening else None
        if isinstance(mine, dict) and (value := to_float(mine.get("max_opening_degree"))) is not None:
            return value
        return None

    def _configured(self, key: str) -> str | None:
        """Entity of this underlying in a VTherm list of entities."""
        values = self.runtime.entry_infos.get(key) or []
        index = self.underlying_index
        return values[index] if index is not None and index < len(values) else None

    def _check_scale(self) -> QuirkIssue | None:
        """A valve_position up to 255 driven on a percent scale opens at about 39 % at most."""
        position = self._entity(ROLE_VALVE_POSITION)
        state = self._state(position)
        entity_max = None if state is None else to_float(state.attributes.get("max"))
        if entity_max is None or entity_max <= PERCENT_MAX:
            return None
        effective = self._effective_max_opening()
        if effective is None:
            # Without the attribute, a command at 100 shows the percent scale.
            effective = PERCENT_MAX if self._max_commanded == PERCENT_MAX else None
        if effective is None or effective > PERCENT_MAX:
            return None
        return QuirkIssue(
            ISSUE_VALVE_SCALE_PERCENT,
            placeholders={"max": f"{entity_max:g}", "entity": str(position)},
        )

    def check_configuration(self) -> list[QuirkIssue]:
        """Report the settings that prevent the quirk from working."""
        issues: list[QuirkIssue] = []
        direct_valve = self._wanted_mode() == TRV_MODE_VALVE

        registry = er.async_get(self.hass)
        for role in (ROLE_TRV_MODE, ROLE_VALVE_POSITION):
            entity_id = self._entity(role)
            entry = registry.async_get(entity_id) if entity_id else None
            if entry is not None and entry.disabled:
                issues.append(QuirkIssue("entity_disabled", placeholders={"entity": entity_id}))

        if direct_valve:
            opening = self._configured(VT_CONF_OPENING_DEGREE_LIST)
            position = self._entity(ROLE_VALVE_POSITION)
            if opening and position and opening != position:
                issues.append(
                    QuirkIssue(
                        "opening_entity_mismatch",
                        placeholders={"entity": str(opening), "expected": position},
                    )
                )
            if closing := self._configured(VT_CONF_CLOSING_DEGREE_LIST):
                issues.append(
                    QuirkIssue(ISSUE_CLOSING_NOT_SUPPORTED, placeholders={"entity": str(closing)})
                )
            if scale := self._check_scale():
                issues.append(scale)

        if self._stuck is not None:
            issues.append(self._stuck)
        return issues

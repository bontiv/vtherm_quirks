"""Quirk of the Sonoff TRVZB (Gen1) under Zigbee2MQTT and ZHA.

The TRVZB exposes two numbers: ``valve_opening_degree`` (the opening while the
TRV heats) and ``valve_closing_degree`` (how closed the valve is while the TRV
is idle). VTherm only drives the opening degree. This quirk:

* owns the closing degree: after each valve command of VTherm it writes
  ``closing = 100 - opening`` (the value the core computes) and puts it back
  when something else changes it;
* closes the valve firmly: some TRVZB motors lose their calibration and stop
  before the seat when they close from a small opening. On a transition from
  an open valve to a closed one, the quirk opens the valve wide for a few
  seconds, then closes it (the "slam close" of caiusseverus/sonoff_trv_slam_close).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from ..const import (
    CAUSE_REPAIR,
    CAUSE_STARTUP,
    SCOPE_OVER_CLIMATE_VALVE,
    STATE_UNAVAILABLE,
    UNDERLYING_CLIMATE,
    VT_CONF_CLOSING_DEGREE_LIST,
    VT_CONF_MAX_CLOSING_DEGREE,
    VT_CONF_OPENING_DEGREE_LIST,
)
from ._zigbee_roles import RoleSpec, discover_role_entities, is_sonoff
from .base import BaseQuirk, EntityRole, QuirkIssue, to_float

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry
    from vtherm_api import ValveCommand

# ---------------------------------------------------------------------------
# Sonoff TRVZB
#
# Verified = read in the sources of Zigbee2MQTT (zigbee-herdsman-converters
# src/devices/sonoff.ts) and of ZHA (zha-device-handlers zhaquirks/sonoff/trvzb.py).
# ---------------------------------------------------------------------------
TRVZB_MODEL = "TRVZB"  # verified: ZHA model, Z2M model id

# Roles of the quirk
ROLE_OPENING_DEGREE = "opening_degree"
ROLE_CLOSING_DEGREE = "closing_degree"
ROLE_ADAPTIVE_CONTROL = "adaptive_control"
ROLE_OPEN_WINDOW = "open_window"

# Quirk options
OPT_PRE_OPEN = "pre_open"
OPT_SLAM_DELAY = "slam_delay"

# 99 and not 100: the exact pair opening 100 / closing 0 can leave the TRVZB
# idle with the valve closed (Better Thermostat issue 2762, firmware 1.4.4).
DEFAULT_PRE_OPEN = 99
MAX_PRE_OPEN = 99
DEFAULT_SLAM_DELAY = 5
MIN_SLAM_DELAY = 2
MAX_SLAM_DELAY = 60

# A closing degree changed by something else is put back after this delay
# (seconds), at most MAX_CORRECTIONS times in CORRECTION_WINDOW seconds.
RESEND_DELAY = 10.0
MAX_CORRECTIONS = 3
CORRECTION_WINDOW = 3600.0

ISSUE_CLOSING_KEEPS_CHANGING = "closing_degree_keeps_changing"

_ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(
        EntityRole(ROLE_OPENING_DEGREE, ("number",)),
        z2m_name="valve_opening_degree",  # verified
        zha_translation_key="valve_opening_degree",  # verified, disabled by default
        zha_attribute="valve_opening_degree",
    ),
    RoleSpec(
        EntityRole(ROLE_CLOSING_DEGREE, ("number",)),
        z2m_name="valve_closing_degree",  # verified
        zha_translation_key="valve_closing_degree",  # verified, disabled by default
        zha_attribute="valve_closing_degree",
    ),
    # Optional roles, only used by check_configuration.
    RoleSpec(
        EntityRole(ROLE_ADAPTIVE_CONTROL, ("switch",), required=False),
        z2m_name="smart_temperature_control",
        zha_translation_key="adaptive_mode",
    ),
    RoleSpec(
        EntityRole(ROLE_OPEN_WINDOW, ("switch",), required=False),
        z2m_name="open_window",
        zha_translation_key="open_window",
    ),
)

_REQUIRED_ROLES = tuple(spec.role.key for spec in _ROLE_SPECS if spec.role.required)


def closing_for(opening: float) -> int:
    """Closing degree that matches an opening degree, as the core computes it."""
    return int(max(0, min(100, 100 - round(opening))))


class SonoffTrvzbQuirk(BaseQuirk):
    """Owns the closing degree of the TRVZB and closes its valve firmly."""

    quirk_id: ClassVar[str] = "sonoff_trvzb"
    title: ClassVar[str] = "Sonoff TRVZB"
    underlying_types: ClassVar[frozenset[str]] = frozenset({UNDERLYING_CLIMATE})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({SCOPE_OVER_CLIMATE_VALVE})

    # ------------------------------------------------------------------
    # Discovery and configuration
    # ------------------------------------------------------------------
    @classmethod
    def match_device(cls, device: DeviceEntry) -> bool:
        """True for a SONOFF TRVZB, under Zigbee2MQTT or ZHA.

        Zigbee2MQTT 2.x gives the model id ``TRVZB``, older releases a model
        ending with ``(TRVZB)``, ZHA the model ``TRVZB``. The description
        "Zigbee thermostatic radiator valve" alone is not enough: Zigbee2MQTT
        gives it to the TRV-ZBT too.
        """
        if not is_sonoff(device):
            return False
        model = (device.model or "").strip().upper()
        model_id = (getattr(device, "model_id", None) or "").strip().upper()
        return (
            model_id == TRVZB_MODEL
            or model == TRVZB_MODEL
            or model.endswith(f"({TRVZB_MODEL})")
        )

    @classmethod
    def discover_entities(
        cls, hass: HomeAssistant, device: DeviceEntry
    ) -> dict[str, str | None]:
        """Find the entities of the device by role. On ZHA the valve entities are disabled by default."""
        return discover_role_entities(hass, device, _ROLE_SPECS)

    @classmethod
    def entity_roles(cls) -> list[EntityRole]:
        """Roles shown in the config flow."""
        return [spec.role for spec in _ROLE_SPECS]

    @classmethod
    def options_schema(cls) -> vol.Schema:
        """Opening and duration of the pre-opening before a full close."""
        return vol.Schema(
            {
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
        # Last opening degree commanded by VTherm (or read at startup).
        self._last_opening: int | None = None
        # hvac mode of the last command: the corrections start again when it changes.
        self._last_mode: str | None = None
        # Slam close running, and the opening it ends on (None: end without writing).
        self._slam_task: asyncio.Task | None = None
        self._slam_target: int | None = None
        # Closing degree put back after an outside change.
        self._restore_task: asyncio.Task | None = None
        self._corrections: list[float] = []
        self._stuck: QuirkIssue | None = None
        self._stuck_mode: str | None = None

    # -- helpers -------------------------------------------------------
    def _now(self) -> float:
        return self.hass.loop.time()

    def _entity(self, role: str) -> str | None:
        return self.entities.get(role) or None

    def _state(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    def _writable(self, entity_id: str | None) -> bool:
        state = self._state(entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE

    @property
    def _pre_open(self) -> int:
        return int(self.options.get(OPT_PRE_OPEN, DEFAULT_PRE_OPEN))

    @property
    def _slam_delay(self) -> float:
        return float(self.options.get(OPT_SLAM_DELAY, DEFAULT_SLAM_DELAY))

    def _closed_floor(self) -> float:
        """Highest opening that VTherm sends for a closed valve."""
        max_closing = to_float(self.runtime.entry_infos.get(VT_CONF_MAX_CLOSING_DEGREE))
        return 0.0 if max_closing is None else max(0.0, 100.0 - max_closing)

    def _is_closed(self, opening: float) -> bool:
        return opening <= self._closed_floor()

    def _slam_running(self) -> bool:
        return self._slam_task is not None and not self._slam_task.done()

    async def _set_number(self, entity_id: str, value: float, *, blocking: bool = True) -> bool:
        data = {"entity_id": entity_id, "value": value}
        return await self.call_service("number", "set_value", data, blocking=blocking)

    async def _write_closing(self, value: int, *, force: bool = False, blocking: bool = False) -> None:
        """Write the closing degree, unless it already has this value or is unavailable."""
        entity_id = self._entity(ROLE_CLOSING_DEGREE)
        state = self._state(entity_id)
        if entity_id is None or state is None or state.state == STATE_UNAVAILABLE:
            self.log.debug("closing degree %s not written: %s missing or unavailable", value, entity_id)
            return
        if not force and to_float(state.state) == value:
            return
        await self._set_number(entity_id, value, blocking=blocking)

    # -- lifecycle -----------------------------------------------------
    async def async_start(self) -> None:
        """Also watch the closing degree, which VTherm does not repair."""
        started = self._started
        await super().async_start()
        closing = self._entity(ROLE_CLOSING_DEGREE)
        if not started and closing:
            self._unsubs.append(
                async_track_state_change_event(self.hass, [closing], self._on_closing_event)
            )

    async def async_startup(self) -> None:
        """Align the closing degree with the opening degree of the device."""
        opening = to_float(getattr(self._state(self._entity(ROLE_OPENING_DEGREE)), "state", None))
        if opening is None:
            return
        self._last_opening = round(opening)
        closing_state = self._state(self._entity(ROLE_CLOSING_DEGREE))
        target = closing_for(self._last_opening)
        if closing_state is not None and to_float(closing_state.state) != target:
            self.log.debug("closing degree %s at startup, set to %s", closing_state.state, target)
            await self._write_closing(target, force=True)

    # The default async_stop cancels the slam close, whose finally writes the
    # closing pair, and the pending restore.

    # -- hook ----------------------------------------------------------
    async def async_after_valve_command(self, command: ValveCommand) -> None:
        """Write the closing degree, or close the valve firmly on a closing edge."""
        opening = int(command.opening_degree)
        previous = self._last_opening
        self._last_opening = opening
        self._last_mode = str(command.hvac_mode)
        self._unstick_on_mode_change(self._last_mode)
        force = str(command.cause) in (CAUSE_STARTUP, CAUSE_REPAIR)
        pre_open = self._pre_open

        if not self._is_closed(opening):
            if self._slam_running():
                await self._supersede_slam(opening)
            await self._write_closing(closing_for(opening), force=force)
            return

        if self._slam_running():
            # A close sent again (repair, startup, turn off): the slam ends on it.
            self._slam_target = opening
            return

        if (
            previous is None
            or self._is_closed(previous)
            or previous >= pre_open
            or pre_open <= 0
        ):
            await self._write_closing(closing_for(opening), force=force)
            return

        if not (
            self._writable(self._entity(ROLE_OPENING_DEGREE))
            and self._writable(self._entity(ROLE_CLOSING_DEGREE))
        ):
            self.log.debug("valve entities unavailable, no slam close")
            await self._write_closing(closing_for(opening), force=force)
            return

        self.log.debug("closing from %s: slam close through %s%%", previous, pre_open)
        self._slam_target = opening
        self._slam_task = self.spawn(self._slam_close(pre_open), "slam close")

    # -- slam close ----------------------------------------------------
    async def _slam_close(self, pre_open: int) -> None:
        """Open the valve wide, wait, then close it. The finally always closes."""
        opening_entity = self._entity(ROLE_OPENING_DEGREE)
        closing_entity = self._entity(ROLE_CLOSING_DEGREE)
        if opening_entity is None or closing_entity is None:
            return
        try:
            await self._set_number(opening_entity, pre_open)
            await self._set_number(closing_entity, closing_for(pre_open))
            await self.async_sleep(self._slam_delay)
        finally:
            target = self._slam_target
            self._slam_target = None
            if target is not None:
                await self._set_number(closing_entity, closing_for(target))
                await self._set_number(opening_entity, target)

    async def _supersede_slam(self, opening: int) -> None:
        """A new opening arrived during a slam close: drop it and write the opening again."""
        self._slam_target = None
        task = self._slam_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])
        self._slam_task = None
        # The slam may have written its pre-opening after VTherm wrote this opening.
        if (entity_id := self._entity(ROLE_OPENING_DEGREE)) is not None:
            await self._set_number(entity_id, opening, blocking=False)

    # -- closing degree watching ---------------------------------------
    @callback
    def _on_closing_event(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        if new_state is None or self._last_opening is None:
            return
        value = to_float(new_state.state)
        if value is None or self._slam_running() or self._stuck is not None:
            return
        if value == closing_for(self._last_opening):
            return
        # Wait before putting it back: our own writes may still be on their way.
        if self._restore_task is not None and not self._restore_task.done():
            return
        self._restore_task = self.spawn(self._restore_closing(), "restore closing degree")

    async def _restore_closing(self) -> None:
        await self.async_sleep(RESEND_DELAY)
        if self._last_opening is None or self._slam_running() or self._stuck is not None:
            return
        entity_id = self._entity(ROLE_CLOSING_DEGREE)
        state = self._state(entity_id)
        value = None if state is None else to_float(state.state)
        target = closing_for(self._last_opening)
        if value is None or value == target:
            return
        now = self._now()
        self._corrections = [t for t in self._corrections if now - t < CORRECTION_WINDOW]
        if len(self._corrections) >= MAX_CORRECTIONS:
            self.log.warning(
                "the closing degree keeps leaving %s (now %s), corrections stopped", target, value
            )
            self._stuck = QuirkIssue(
                ISSUE_CLOSING_KEEPS_CHANGING,
                placeholders={"entity": str(entity_id), "expected": str(target), "current": str(state.state)},
            )
            self._stuck_mode = self._last_mode
            self.request_issue_sync()
            return
        self._corrections.append(now)
        self.log.info(
            "closing degree %s instead of %s, correction %s/%s",
            state.state,
            target,
            len(self._corrections),
            MAX_CORRECTIONS,
        )
        await self._write_closing(target, force=True)

    def _unstick_on_mode_change(self, hvac_mode: str) -> None:
        """The thermostat changed mode: corrections start again."""
        if self._stuck is None or hvac_mode == self._stuck_mode:
            return
        self._stuck = None
        self._stuck_mode = None
        self._corrections.clear()
        self.request_issue_sync()

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

        if closing := configured(VT_CONF_CLOSING_DEGREE_LIST):
            issues.append(
                QuirkIssue("closing_entity_configured", placeholders={"entity": str(closing)})
            )

        opening = configured(VT_CONF_OPENING_DEGREE_LIST)
        expected = self._entity(ROLE_OPENING_DEGREE)
        if opening and expected and opening != expected:
            issues.append(
                QuirkIssue(
                    "opening_entity_mismatch",
                    placeholders={"entity": str(opening), "expected": expected},
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

        if self._stuck is not None:
            issues.append(self._stuck)
        return issues

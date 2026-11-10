"""Tests of the Eurotronic Spirit Zigbee quirk."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from vtherm_api import ValveCommand, ValveCommandCause

from custom_components.vtherm_quirks.quirks import detect_quirk
from custom_components.vtherm_quirks.quirks.eurotronic_spzb0001 import (
    MAX_CORRECTIONS,
    RESEND_DELAY,
    EurotronicSpzb0001Quirk,
)

from ..common import CLIMATE, VTHERM_ENTITY_ID, FakeRuntime, flush, vtherm_entry_infos

TRV_MODE = "select.spirit_trv_mode"
VALVE_POSITION = "number.spirit_valve_position"
ENTITIES = {"trv_mode": TRV_MODE, "valve_position": VALVE_POSITION}


def spirit_infos(**overrides: Any) -> dict[str, Any]:
    infos = vtherm_entry_infos(opening_degree_entity_ids=[VALVE_POSITION])
    infos.update(overrides)
    return infos


NO_VALVE = {"auto_regulation_mode": "auto_regulation_slow", "opening_degree_entity_ids": []}


class SpiritSim:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple] = []
        self.fail = False

    def setup(self, mode: str = "1", position: str = "0", max_position: float = 255) -> SpiritSim:
        self.hass.states.async_set(CLIMATE, "heat", {"temperature": 20.0})
        self.hass.states.async_set(TRV_MODE, mode, {"options": ["1", "2"]})
        self.hass.states.async_set(VALVE_POSITION, position, {"min": 0, "max": max_position})

        async def select_option(call: ServiceCall) -> None:
            self.calls.append(("mode", call.data["option"]))
            if self.fail:
                raise ValueError("refused")
            self.hass.states.async_set(TRV_MODE, call.data["option"], {"options": ["1", "2"]})

        async def set_value(call: ServiceCall) -> None:
            self.calls.append(("position", call.data["value"]))

        self.hass.services.async_register("select", "select_option", select_option)
        self.hass.services.async_register("number", "set_value", set_value)
        return self

    def device_sets(self, mode: str) -> None:
        """The device (or something else) changes the select."""
        self.hass.states.async_set(TRV_MODE, mode, {"options": ["1", "2"]})


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def pass_delay(self) -> None:
        self.value += RESEND_DELAY + 1


def make_quirk(
    hass: HomeAssistant, entry_infos: dict | None = None, entities: dict | None = None
) -> tuple[EurotronicSpzb0001Quirk, Clock]:
    runtime = FakeRuntime(hass, entry_infos if entry_infos is not None else spirit_infos())
    quirk = EurotronicSpzb0001Quirk(hass, runtime, CLIMATE, dict(entities or ENTITIES), {})
    clock = Clock()
    quirk._now = clock  # type: ignore[method-assign]
    return quirk, clock


def command(opening: int, cause: ValveCommandCause = ValveCommandCause.REGULATION) -> ValveCommand:
    return ValveCommand(
        climate_entity_id=CLIMATE,
        opening_degree_entity_id=VALVE_POSITION,
        closing_degree_entity_id=None,
        percent_open=opening,
        opening_degree=opening,
        closing_degree=None,
        hvac_mode="heat",
        cause=cause,
    )


def make_device(
    hass: HomeAssistant,
    manufacturer: str | None = "Eurotronic",
    model: str | None = "Spirit Zigbee wireless heater thermostat",
    model_id: str | None = "SPZB0001",
    domain: str = "mqtt",
) -> dr.DeviceEntry:
    config_entry = MockConfigEntry(domain=domain)
    config_entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(domain, "0x0015bc001b02")},
        manufacturer=manufacturer,
        model=model,
        model_id=model_id,
    )


# ---------------------------------------------------------------------------
# Detection and configuration
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "model,model_id",
    [
        ("Spirit Zigbee wireless heater thermostat", "SPZB0001"),
        ("Spirit Zigbee wireless heater thermostat (SPZB0001)", None),
    ],
)
async def test_match_device(hass: HomeAssistant, model, model_id) -> None:
    device = make_device(hass, model=model, model_id=model_id)
    assert EurotronicSpzb0001Quirk.match_device(device)
    assert detect_quirk(device, spirit_infos()) is EurotronicSpzb0001Quirk
    assert detect_quirk(device, spirit_infos(**NO_VALVE)) is EurotronicSpzb0001Quirk


@pytest.mark.parametrize(
    "manufacturer,model,model_id,domain",
    [
        ("Eurotronic", "SPZB0001", None, "zha"),  # ZHA exposes neither entity
        ("Eurotronic", "Spirit Z-Wave", None, "mqtt"),
        ("SONOFF", "TRVZB", "SPZB0001", "mqtt"),
        (None, None, None, "mqtt"),
    ],
)
async def test_match_device_rejects(hass: HomeAssistant, manufacturer, model, model_id, domain) -> None:
    device = make_device(hass, manufacturer, model, model_id, domain)
    assert not EurotronicSpzb0001Quirk.match_device(device)


async def test_discover_entities(hass: HomeAssistant) -> None:
    device = make_device(hass)
    config_entry_id = device.config_entry_id
    registry = er.async_get(hass)

    def register(domain: str, name: str, **kwargs: Any) -> str:
        return registry.async_get_or_create(
            domain,
            "mqtt",
            f"0x0015bc001b02_{name}_zigbee2mqtt",
            config_entry=hass.config_entries.async_get_entry(config_entry_id),
            device_id=device.id,
            suggested_object_id=f"spirit_{name}",
            **kwargs,
        ).entity_id

    mode = register("select", "trv_mode")
    position = register("number", "valve_position", disabled_by=er.RegistryEntryDisabler.USER)
    register("number", "local_temperature_calibration")
    assert EurotronicSpzb0001Quirk.discover_entities(hass, device) == {
        "trv_mode": mode,
        "valve_position": position,
    }


def test_roles_and_options() -> None:
    roles = {role.key: role for role in EurotronicSpzb0001Quirk.entity_roles()}
    assert {key: role.required for key, role in roles.items()} == {
        "trv_mode": True,
        "valve_position": False,
    }
    assert EurotronicSpzb0001Quirk.resolve_options({}) == {}


# ---------------------------------------------------------------------------
# Startup and hooks
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "infos,initial,expected",
    [
        (spirit_infos(), "2", ["1"]),
        (spirit_infos(), "unknown", ["1"]),
        (spirit_infos(), "1", []),
        (spirit_infos(**NO_VALVE), "1", ["2"]),
        (spirit_infos(**NO_VALVE), "2", []),
    ],
)
async def test_startup(hass: HomeAssistant, infos, initial: str, expected: list[str]) -> None:
    sim = SpiritSim(hass).setup(mode=initial)
    quirk, _ = make_quirk(hass, infos)
    await quirk.async_start()
    await flush()
    assert [value for kind, value in sim.calls if kind == "mode"] == expected


async def test_leaving_off_rewrites_the_mode(hass: HomeAssistant) -> None:
    sim = SpiritSim(hass).setup(mode="1")
    quirk, _ = make_quirk(hass)
    await quirk.async_start()
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    await flush()
    assert sim.calls == []
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    await flush()
    assert sim.calls == [("mode", "1")]


async def test_valve_command(hass: HomeAssistant) -> None:
    """A regulation command checks the mode, repair and startup write it again."""
    sim = SpiritSim(hass).setup(mode="1")
    quirk, _ = make_quirk(hass)
    await quirk.async_start()
    await quirk.async_after_valve_command(command(120))
    await flush()
    assert sim.calls == []
    await quirk.async_after_valve_command(command(120, ValveCommandCause.REPAIR))
    await flush()
    assert sim.calls == [("mode", "1"), ("position", 120)]


async def test_valve_command_corrects_the_mode(hass: HomeAssistant) -> None:
    """A device found in mode 2 at a valve command is put back in 1 with the opening."""
    sim = SpiritSim(hass).setup(mode="2")
    quirk, _ = make_quirk(hass)  # not started: no watcher, no startup check
    await quirk.async_after_valve_command(command(80))
    await flush()
    assert sim.calls == [("mode", "1"), ("position", 80)]


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------
async def test_watcher_corrects_and_reapplies_opening(hass: HomeAssistant) -> None:
    sim = SpiritSim(hass).setup(mode="1")
    quirk, _ = make_quirk(hass)
    await quirk.async_start()
    await quirk.async_after_valve_command(command(90))
    await flush()
    sim.device_sets("2")
    await flush()
    assert sim.calls == [("mode", "1"), ("position", 90)]


async def test_watcher_waits_resend_delay(hass: HomeAssistant) -> None:
    sim = SpiritSim(hass).setup(mode="1")
    quirk, clock = make_quirk(hass)
    await quirk.async_start()
    sim.device_sets("2")
    await flush()
    sim.device_sets("1")
    sim.device_sets("2")
    await flush()
    assert sim.calls == [("mode", "1")]
    clock.pass_delay()
    sim.device_sets("1")
    sim.device_sets("2")
    await flush()
    assert sim.calls == [("mode", "1"), ("mode", "1")]


async def test_watcher_stops_after_max_corrections(hass: HomeAssistant) -> None:
    sim = SpiritSim(hass).setup(mode="1")
    quirk, clock = make_quirk(hass)
    await quirk.async_start()
    for _ in range(MAX_CORRECTIONS):
        clock.pass_delay()
        sim.device_sets("2")
        await flush()
    assert sim.calls == [("mode", "1")] * MAX_CORRECTIONS
    clock.pass_delay()
    sim.device_sets("2")
    await flush()
    assert len(sim.calls) == MAX_CORRECTIONS
    issues = {issue.key: issue for issue in quirk.check_configuration()}
    assert issues["trv_mode_keeps_changing"].placeholders == {"mode": "1", "current": "2"}

    # Leaving OFF starts again.
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    await flush()
    assert "trv_mode_keeps_changing" not in [i.key for i in quirk.check_configuration()]
    assert sim.calls[-1] == ("mode", "1")


async def test_unavailable_select(hass: HomeAssistant) -> None:
    sim = SpiritSim(hass).setup(mode="unavailable")
    quirk, _ = make_quirk(hass)
    await quirk.async_start()
    await quirk.async_after_valve_command(command(50, ValveCommandCause.REPAIR))
    await flush()
    assert sim.calls == []


async def test_service_error_no_reapply(hass: HomeAssistant) -> None:
    sim = SpiritSim(hass).setup(mode="2")
    sim.fail = True
    quirk, _ = make_quirk(hass)
    quirk._last_opening = 50
    await quirk.async_start()
    await flush()
    assert sim.calls == [("mode", "1")]


async def test_stop_during_write(hass: HomeAssistant) -> None:
    SpiritSim(hass).setup(mode="2")
    quirk, _ = make_quirk(hass)
    await quirk.async_start()
    await quirk.async_stop()
    await flush()


# ---------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------
async def test_check_configuration_ok(hass: HomeAssistant) -> None:
    SpiritSim(hass).setup()
    quirk, _ = make_quirk(hass)
    assert quirk.check_configuration() == []


async def test_check_configuration_vtherm_settings(hass: HomeAssistant) -> None:
    SpiritSim(hass).setup()
    quirk, _ = make_quirk(
        hass,
        spirit_infos(
            opening_degree_entity_ids=["number.other"],
            closing_degree_entity_ids=["number.closing"],
        ),
    )
    keys = sorted(issue.key for issue in quirk.check_configuration())
    assert keys == ["closing_entity_not_supported", "opening_entity_mismatch"]
    # Without direct valve control, nothing is checked on the valve.
    quirk, _ = make_quirk(hass, spirit_infos(**NO_VALVE))
    assert quirk.check_configuration() == []


async def test_check_configuration_disabled(hass: HomeAssistant) -> None:
    SpiritSim(hass).setup()
    registry = er.async_get(hass)
    config_entry = MockConfigEntry(domain="mqtt")
    config_entry.add_to_hass(hass)
    entry = registry.async_get_or_create(
        "select", "mqtt", "x_trv_mode_z2m", config_entry=config_entry,
        suggested_object_id="spirit_mode", disabled_by=er.RegistryEntryDisabler.USER,
    )
    quirk, _ = make_quirk(hass, entities={"trv_mode": entry.entity_id, "valve_position": VALVE_POSITION})
    issues = quirk.check_configuration()
    assert [(i.key, dict(i.placeholders)) for i in issues] == [
        ("entity_disabled", {"entity": entry.entity_id})
    ]


async def test_scale_from_vtherm_attribute(hass: HomeAssistant) -> None:
    SpiritSim(hass).setup()
    quirk, _ = make_quirk(hass)
    hass.states.async_set(
        VTHERM_ENTITY_ID,
        "heat",
        {"vtherm_over_climate_valve": {"underlying_valves": {VALVE_POSITION: {"max_opening_degree": 100}}}},
    )
    issues = {issue.key: issue for issue in quirk.check_configuration()}
    assert issues["valve_scale_percent"].placeholders == {"max": "255", "entity": VALVE_POSITION}
    hass.states.async_set(
        VTHERM_ENTITY_ID,
        "heat",
        {"vtherm_over_climate_valve": {"underlying_valves": {VALVE_POSITION: {"max_opening_degree": 255}}}},
    )
    assert quirk.check_configuration() == []


async def test_scale_from_commands(hass: HomeAssistant) -> None:
    SpiritSim(hass).setup()
    quirk, _ = make_quirk(hass)
    await quirk.async_after_valve_command(command(100))
    assert [i.key for i in quirk.check_configuration()] == ["valve_scale_percent"]
    await quirk.async_after_valve_command(command(255))
    assert quirk.check_configuration() == []


async def test_no_scale_issue_for_a_percent_entity(hass: HomeAssistant) -> None:
    SpiritSim(hass).setup(max_position=100)
    quirk, _ = make_quirk(hass)
    await quirk.async_after_valve_command(command(100))
    assert quirk.check_configuration() == []

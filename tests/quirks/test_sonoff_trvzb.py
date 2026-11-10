"""Tests of the Sonoff TRVZB quirk."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from vtherm_api import ValveCommand, ValveCommandCause

from custom_components.vtherm_quirks.quirks import detect_quirk
from custom_components.vtherm_quirks.quirks.sonoff_trv_zbt import SonoffTrvZbtQuirk
from custom_components.vtherm_quirks.quirks.sonoff_trvzb import (
    MAX_CORRECTIONS,
    RESEND_DELAY,
    SonoffTrvzbQuirk,
)

from ..common import (
    CLIMATE,
    TRVZB_CLOSING,
    TRVZB_OPENING,
    FakeRuntime,
    TrvzbSim,
    add_device,
    add_trvzb_device,
    advance,
    device_of,
    flush,
    vtherm_entry_infos,
)


def trvzb_infos(**overrides: Any) -> dict[str, Any]:
    infos = vtherm_entry_infos(opening_degree_entity_ids=[TRVZB_OPENING])
    infos.update(overrides)
    return infos


def make_quirk(
    hass: HomeAssistant,
    entry_infos: dict | None = None,
    entities: dict | None = None,
    mode: str = "heat",
    **options: Any,
) -> SonoffTrvzbQuirk:
    runtime = FakeRuntime(hass, entry_infos if entry_infos is not None else trvzb_infos())
    runtime.vtherm_hvac_mode = mode
    return SonoffTrvzbQuirk(
        hass,
        runtime,
        CLIMATE,
        dict(TrvzbSim.entities if entities is None else entities),
        SonoffTrvzbQuirk.resolve_options(options),
    )


def command(
    opening: int,
    cause: ValveCommandCause = ValveCommandCause.REGULATION,
    hvac_mode: str = "heat",
) -> ValveCommand:
    return ValveCommand(
        climate_entity_id=CLIMATE,
        opening_degree_entity_id=TRVZB_OPENING,
        closing_degree_entity_id=None,
        percent_open=opening,
        opening_degree=opening,
        closing_degree=None,
        hvac_mode=hvac_mode,
        cause=cause,
    )


async def started(hass: HomeAssistant, sim: TrvzbSim, **kwargs: Any) -> SonoffTrvzbQuirk:
    quirk = make_quirk(hass, **kwargs)
    await quirk.async_start()
    await flush()
    sim.clear()
    return quirk


async def send(quirk: SonoffTrvzbQuirk, *args: Any, **kwargs: Any) -> None:
    await quirk.async_after_valve_command(command(*args, **kwargs))
    await flush()


# ---------------------------------------------------------------------------
# Detection and configuration
# ---------------------------------------------------------------------------
def make_device(
    hass: HomeAssistant, manufacturer: str | None, model: str | None, model_id: str | None, key: str
) -> dr.DeviceEntry:
    config_entry = MockConfigEntry(domain="mqtt")
    config_entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("mqtt", key)},
        manufacturer=manufacturer,
        model=model,
        model_id=model_id,
    )


@pytest.mark.parametrize(
    "manufacturer,model,model_id",
    [
        ("SONOFF", "Zigbee thermostatic radiator valve", "TRVZB"),  # Z2M 2.x
        ("SONOFF", "Zigbee thermostatic radiator valve (TRVZB)", None),  # older Z2M
        ("SONOFF", "TRVZB", None),  # ZHA
        (" sonoff ", "trvzb", None),
    ],
)
async def test_match_device(hass: HomeAssistant, manufacturer, model, model_id) -> None:
    """A TRVZB is recognized under Zigbee2MQTT and ZHA."""
    assert SonoffTrvzbQuirk.match_device(make_device(hass, manufacturer, model, model_id, "dev"))


@pytest.mark.parametrize(
    "manufacturer,model,model_id",
    [
        ("SONOFF", "Zigbee thermostatic radiator valve", None),  # description only
        ("SONOFF", "Zigbee thermostatic radiator valve (TRV-ZBT)", None),
        ("SONOFF", "Zigbee thermostatic radiator valve", "TRV-ZBT"),
        ("SONOFF", "TRV-ZBT", None),
        ("Other", "TRVZB", "TRVZB"),
        (None, None, None),
    ],
)
async def test_match_device_rejects_other_models(hass: HomeAssistant, manufacturer, model, model_id) -> None:
    """The TRV-ZBT, a description alone and other manufacturers are rejected."""
    assert not SonoffTrvzbQuirk.match_device(make_device(hass, manufacturer, model, model_id, "dev"))


@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_detect_only_one_sonoff_quirk(hass: HomeAssistant, kind: str) -> None:
    """A TRVZB gets this quirk, a TRV-ZBT keeps its own."""
    trvzb = device_of(hass, add_trvzb_device(hass, kind, "climate.trvzb"))
    trv_zbt = device_of(hass, add_device(hass, kind, "climate.trv"))
    assert detect_quirk(trvzb, vtherm_entry_infos()) is SonoffTrvzbQuirk
    assert detect_quirk(trv_zbt, vtherm_entry_infos()) is SonoffTrvZbtQuirk
    assert detect_quirk(trvzb, vtherm_entry_infos(auto_regulation_mode="auto_regulation_none")) is None


@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_discover_entities(hass: HomeAssistant, kind: str) -> None:
    """The entities are found by role, disabled ZHA entities included."""
    expected = add_trvzb_device(hass, kind)
    found = SonoffTrvzbQuirk.discover_entities(hass, device_of(hass, expected))
    for role in ("opening_degree", "closing_degree", "adaptive_control", "open_window"):
        assert found[role] == expected[role], role
    if kind == "zha":
        assert er.async_get(hass).async_get(found["closing_degree"]).disabled


def test_roles_and_options() -> None:
    """Roles and options are declared."""
    roles = {role.key: role for role in SonoffTrvzbQuirk.entity_roles()}
    assert {key for key, role in roles.items() if role.required} == {
        "opening_degree",
        "closing_degree",
    }
    assert SonoffTrvzbQuirk.resolve_options({}) == {"pre_open": 99, "slam_delay": 5}


# ---------------------------------------------------------------------------
# Closing degree owned by the quirk
# ---------------------------------------------------------------------------
async def test_closing_written_after_command(hass: HomeAssistant) -> None:
    """closing = 100 - opening after each command, VTherm has no closing entity."""
    sim = TrvzbSim(hass).setup(opening="0", closing="100")
    quirk = await started(hass, sim)

    await send(quirk, 40)
    assert sim.calls == [sim.closing(60)]
    await send(quirk, 75)
    assert sim.calls == [sim.closing(60), sim.closing(25)]


async def test_closing_not_rewritten_when_equal(hass: HomeAssistant) -> None:
    """A closing degree already right is not written again, except on repair or startup."""
    sim = TrvzbSim(hass).setup(opening="40", closing="60")
    quirk = await started(hass, sim)

    await send(quirk, 40)
    assert sim.calls == []
    await send(quirk, 40, ValveCommandCause.REPAIR)
    await send(quirk, 40, ValveCommandCause.STARTUP)
    assert sim.calls == [sim.closing(60), sim.closing(60)]


async def test_startup_aligns_closing(hass: HomeAssistant) -> None:
    """At startup an unknown or wrong closing degree is set from the opening."""
    sim = TrvzbSim(hass).setup(opening="30", closing="unknown")
    make = make_quirk(hass)
    await make.async_start()
    await flush()
    assert sim.calls == [sim.closing(70)]


async def test_startup_leaves_a_right_closing(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = make_quirk(hass)
    await quirk.async_start()
    await flush()
    assert sim.calls == []


async def test_watcher_restores_closing(hass: HomeAssistant) -> None:
    """A closing degree changed by something else is put back after a delay."""
    sim = TrvzbSim(hass).setup(opening="40", closing="60")
    quirk = await started(hass, sim)
    await send(quirk, 40)

    hass.states.async_set(TRVZB_CLOSING, "20")
    await flush()
    assert sim.calls == []
    await advance(hass, RESEND_DELAY + 1)
    assert sim.calls == [sim.closing(60)]


async def test_watcher_ignores_a_value_that_comes_back(hass: HomeAssistant) -> None:
    """A transient value (an old write arriving late) is not corrected."""
    sim = TrvzbSim(hass).setup(opening="40", closing="60")
    quirk = await started(hass, sim)
    await send(quirk, 40)

    hass.states.async_set(TRVZB_CLOSING, "70")
    await flush()
    hass.states.async_set(TRVZB_CLOSING, "60")
    await advance(hass, RESEND_DELAY + 1)
    assert sim.calls == []


async def test_watcher_stops_after_max_corrections(hass: HomeAssistant) -> None:
    """After MAX_CORRECTIONS the quirk stops and reports, until the mode changes."""
    sim = TrvzbSim(hass).setup(opening="40", closing="60")
    quirk = await started(hass, sim)
    await send(quirk, 40)

    for _ in range(MAX_CORRECTIONS):
        hass.states.async_set(TRVZB_CLOSING, "20")
        await advance(hass, RESEND_DELAY + 1)
    assert sim.calls == [sim.closing(60)] * MAX_CORRECTIONS
    assert "closing_degree_keeps_changing" not in [i.key for i in quirk.check_configuration()]

    hass.states.async_set(TRVZB_CLOSING, "20")
    await advance(hass, RESEND_DELAY + 1)
    assert len(sim.calls) == MAX_CORRECTIONS
    assert "closing_degree_keeps_changing" in [i.key for i in quirk.check_configuration()]

    # Same mode: still stuck. Another mode: corrections start again.
    await send(quirk, 40)
    assert "closing_degree_keeps_changing" in [i.key for i in quirk.check_configuration()]
    await send(quirk, 0, ValveCommandCause.TURN_OFF, hvac_mode="off")
    assert "closing_degree_keeps_changing" not in [i.key for i in quirk.check_configuration()]


async def test_vtherm_closing_entity_still_configured(hass: HomeAssistant) -> None:
    """During the coexistence the quirk reports the VTherm closing entity and keeps writing."""
    sim = TrvzbSim(hass).setup(opening="0", closing="100")
    quirk = await started(hass, sim, entry_infos=trvzb_infos(closing_degree_entity_ids=[TRVZB_CLOSING]))
    issues = {issue.key: issue for issue in quirk.check_configuration()}
    assert issues["closing_entity_configured"].placeholders == {"entity": TRVZB_CLOSING}
    await send(quirk, 40)
    assert sim.calls == [sim.closing(60)]


# ---------------------------------------------------------------------------
# Slam close
# ---------------------------------------------------------------------------
async def test_slam_sequence(hass: HomeAssistant) -> None:
    """Open -> closed: pre-open, wait, then closing 100 and opening 0."""
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = await started(hass, sim)

    await send(quirk, 0)
    assert sim.calls == [sim.opening(99), sim.closing(1)]
    await advance(hass, 3)
    assert len(sim.calls) == 2
    await advance(hass, 6)
    assert sim.calls == [sim.opening(99), sim.closing(1), sim.closing(100), sim.opening(0)]


async def test_slam_on_turn_off(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = await started(hass, sim)
    await send(quirk, 0, ValveCommandCause.TURN_OFF, hvac_mode="off")
    await advance(hass, 6)
    assert sim.calls == [sim.opening(99), sim.closing(1), sim.closing(100), sim.opening(0)]


async def test_no_slam_when_previous_ge_pre_open(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="99", closing="1")
    quirk = await started(hass, sim)
    await send(quirk, 0)
    assert sim.calls == [sim.closing(100)]


async def test_no_slam_pre_open_zero(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = await started(hass, sim, pre_open=0)
    await send(quirk, 0)
    assert sim.calls == [sim.closing(100)]


async def test_first_command_after_startup_no_slam(hass: HomeAssistant) -> None:
    """Without a known previous opening there is no closing edge."""
    sim = TrvzbSim(hass).setup(opening="unknown", closing="unknown")
    quirk = await started(hass, sim)
    await send(quirk, 0, ValveCommandCause.STARTUP)
    assert sim.calls == [sim.closing(100)]


async def test_repeated_zero_no_slam(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = await started(hass, sim)
    await send(quirk, 0)
    await advance(hass, 6)
    sim.clear()
    await send(quirk, 0)
    assert sim.calls == []


async def test_floor_with_max_closing_degree(hass: HomeAssistant) -> None:
    """With max_closing_degree 90, an opening of 10 is a closed valve."""
    sim = TrvzbSim(hass).setup(opening="50", closing="50")
    quirk = await started(hass, sim, entry_infos=trvzb_infos(max_closing_degree=90))
    await send(quirk, 10)
    await advance(hass, 6)
    assert sim.calls == [sim.opening(99), sim.closing(1), sim.closing(90), sim.opening(10)]
    sim.clear()
    await send(quirk, 10)
    assert sim.calls == []


async def test_reopen_during_slam_cancels_and_rewrites(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = await started(hass, sim)
    await send(quirk, 0)
    await send(quirk, 40)
    assert sim.calls == [sim.opening(99), sim.closing(1), sim.opening(40), sim.closing(60)]
    await advance(hass, 6)
    assert len(sim.calls) == 4


async def test_zero_during_slam_keeps_running(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = await started(hass, sim)
    await send(quirk, 0)
    await send(quirk, 0, ValveCommandCause.REPAIR)
    assert sim.calls == [sim.opening(99), sim.closing(1)]
    await advance(hass, 6)
    assert sim.calls[2:] == [sim.closing(100), sim.opening(0)]


async def test_watcher_ignores_slam_writes(hass: HomeAssistant) -> None:
    """The pre-opening of the slam is not taken for an outside change."""
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = await started(hass, sim)
    await send(quirk, 0)
    await advance(hass, 6)
    await advance(hass, RESEND_DELAY + 1)
    assert sim.calls == [sim.opening(99), sim.closing(1), sim.closing(100), sim.opening(0)]


async def test_stop_mid_slam_writes_target(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = await started(hass, sim)
    await send(quirk, 0)
    await quirk.async_stop()
    assert sim.calls == [sim.opening(99), sim.closing(1), sim.closing(100), sim.opening(0)]


async def test_service_failure_still_writes_target(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    sim.fail_values = {99}
    quirk = await started(hass, sim)
    await send(quirk, 0)
    await advance(hass, 6)
    assert sim.calls[-2:] == [sim.closing(100), sim.opening(0)]


async def test_unavailable_entity_no_slam(hass: HomeAssistant) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="unavailable")
    quirk = await started(hass, sim)
    await send(quirk, 0)
    await advance(hass, 6)
    assert sim.calls == []


# ---------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------
async def test_check_configuration_ok(hass: HomeAssistant) -> None:
    TrvzbSim(hass).setup()
    assert make_quirk(hass).check_configuration() == []


async def test_check_configuration_opening_mismatch(hass: HomeAssistant) -> None:
    TrvzbSim(hass).setup()
    quirk = make_quirk(hass, entry_infos=trvzb_infos(opening_degree_entity_ids=["number.other"]))
    issues = {issue.key: issue for issue in quirk.check_configuration()}
    assert issues["opening_entity_mismatch"].placeholders == {
        "entity": "number.other",
        "expected": TRVZB_OPENING,
    }


async def test_check_configuration_device_settings(hass: HomeAssistant) -> None:
    """Disabled valve entities and device settings are reported."""
    found = add_trvzb_device(hass, "zha")
    hass.states.async_set(found["adaptive_control"], "on")
    hass.states.async_set(found["open_window"], "on")
    entities = {role: found[role] for role in ("opening_degree", "closing_degree", "adaptive_control", "open_window")}
    quirk = make_quirk(
        hass,
        entry_infos=trvzb_infos(opening_degree_entity_ids=[found["opening_degree"]]),
        entities=entities,
    )
    keys = sorted(issue.key for issue in quirk.check_configuration())
    assert keys == ["adaptive_control_enabled", "entity_disabled", "entity_disabled", "open_window_enabled"]
    hass.states.async_set(found["adaptive_control"], "off")
    hass.states.async_set(found["open_window"], "off")
    er.async_get(hass).async_update_entity(found["opening_degree"], disabled_by=None)
    er.async_get(hass).async_update_entity(found["closing_degree"], disabled_by=None)
    assert quirk.check_configuration() == []

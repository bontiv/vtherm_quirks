"""Tests of the Sonoff TRV-ZBT quirk."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from vtherm_api import ValveCommand, ValveCommandCause

from custom_components.vtherm_quirks.quirks.sonoff_trv_zbt import SonoffTrvZbtQuirk

from ..common import (
    CLIMATE,
    EXTERNAL,
    HEATING,
    IDLE,
    FakeRuntime,
    TrvSim,
    add_device,
    advance,
    device_of,
    flush,
    vtherm_entry_infos,
)


def make_quirk(
    hass: HomeAssistant,
    sim: TrvSim,
    entry_infos: dict | None = None,
    **options,
) -> SonoffTrvZbtQuirk:
    resolved = SonoffTrvZbtQuirk.resolve_options(options)
    entities = dict(sim.entities)
    return SonoffTrvZbtQuirk(
        hass,
        FakeRuntime(hass, entry_infos),
        CLIMATE,
        entities,
        resolved,
    )


def valve_command(
    opening: int = 50,
    cause: ValveCommandCause = ValveCommandCause.REGULATION,
    hvac_mode: str = "heat",
) -> ValveCommand:
    return ValveCommand(
        climate_entity_id=CLIMATE,
        opening_degree_entity_id=HEATING,
        closing_degree_entity_id=None,
        percent_open=opening,
        opening_degree=opening,
        closing_degree=None,
        hvac_mode=hvac_mode,
        cause=cause,
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_match_device(hass: HomeAssistant, kind: str) -> None:
    """A TRV-ZBT is recognized under Zigbee2MQTT and ZHA."""
    device = device_of(hass, add_device(hass, kind))
    assert SonoffTrvZbtQuirk.match_device(device)


async def test_match_device_rejects_other_models(hass: HomeAssistant) -> None:
    """Other Sonoff models and other manufacturers are rejected."""
    registry = dr.async_get(hass)
    from pytest_homeassistant.common import MockConfigEntry

    config_entry = MockConfigEntry(domain="zha")
    config_entry.add_to_hass(hass)
    for index, (manufacturer, model) in enumerate(
        [("SONOFF", "TRVZB"), ("SONOFF", "TRV-ZBL"), ("Other", "TRV-ZBT"), (None, None)]
    ):
        device = registry.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={("zha", f"dev{index}")},
            manufacturer=manufacturer,
            model=model,
        )
        assert not SonoffTrvZbtQuirk.match_device(device)


@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_discover_entities(hass: HomeAssistant, kind: str) -> None:
    """The entities are found by role, disabled ZHA entities included."""
    expected = add_device(hass, kind)
    device = device_of(hass, expected)
    found = SonoffTrvZbtQuirk.discover_entities(hass, device)
    for role in (
        "heating_position",
        "idle_position",
        "external_temperature",
        "sensor_select",
        "adaptive_control",
        "open_window",
    ):
        assert found[role] == expected[role], role
    if kind == "zha":
        registry = er.async_get(hass)
        assert registry.async_get(found["heating_position"]).disabled


def test_roles_and_options() -> None:
    """Roles and options are declared."""
    roles = {role.key: role for role in SonoffTrvZbtQuirk.entity_roles()}
    assert {r for r, v in roles.items() if v.required} == {
        "heating_position",
        "idle_position",
        "external_temperature",
        "sensor_select",
    }
    assert roles["sensor_select"].domains == ("select", "switch")
    options = SonoffTrvZbtQuirk.resolve_options({})
    assert options == {
        "temperature_source": "local",
        "delta": 2.0,
        "pulse_delay": 5,
        "hot_step": False,
    }


# ---------------------------------------------------------------------------
# Kick sequence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_kick_local_sequence(hass: HomeAssistant, kind: str) -> None:
    """Local source: idle sync, cold temperature, external sensor, wait, local sensor."""
    sim = TrvSim(hass, kind).setup(setpoint=20.0)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()

    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    assert sim.calls == [
        ("number", IDLE, 59),
        ("number", EXTERNAL, 18.0),
        sim.external_on,
    ]

    await advance(hass, 4)
    assert len(sim.calls) == 3  # still waiting

    await advance(hass, 5)
    assert sim.calls[3:] == [sim.external_off]
    await quirk.async_stop()


async def test_kick_remote_restores_saved_value(hass: HomeAssistant) -> None:
    """Remote source: the external value is saved and written back, the sensor stays external."""
    sim = TrvSim(hass).setup(setpoint=19.5, external=21.3)
    hass.states.async_set("select.trv_temperature_sensor_select", "remote_temperature")
    quirk = make_quirk(hass, sim, temperature_source="remote", delta=3.0)
    await quirk.async_start()

    await quirk.async_after_valve_command(valve_command(40))
    await flush()
    assert sim.calls == [("number", IDLE, 39), ("number", EXTERNAL, 16.5)]
    # The sensor was already external: no select call.
    await advance(hass, 5)
    assert sim.calls[2:] == [("number", EXTERNAL, 21.3)]
    await quirk.async_stop()


async def test_kick_remote_switches_to_external_when_local(hass: HomeAssistant) -> None:
    """Remote source with the local sensor selected: switched to external, left external."""
    sim = TrvSim(hass).setup(setpoint=20.0, external=22.0)
    quirk = make_quirk(hass, sim, temperature_source="remote")
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(30))
    await flush()
    assert sim.external_on in sim.calls
    await advance(hass, 5)
    assert sim.calls[-1] == ("number", EXTERNAL, 22.0)
    assert sim.external_off not in sim.calls
    await quirk.async_stop()


@pytest.mark.parametrize("hot_step", [False, True])
async def test_kick_hot_step(hass: HomeAssistant, hot_step: bool) -> None:
    """The hot step writes setpoint + delta then waits again."""
    sim = TrvSim(hass).setup(setpoint=20.0)
    quirk = make_quirk(hass, sim, hot_step=hot_step, delta=1.4, pulse_delay=3)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(10))
    await flush()
    await advance(hass, 3)
    if hot_step:
        assert sim.calls[-1] == ("number", EXTERNAL, 21.4)
        assert sim.external_off not in sim.calls
        await advance(hass, 3)
    assert sim.calls[-1] == sim.external_off
    assert [c for c in sim.calls if c[1:2] == (EXTERNAL,)][0] == ("number", EXTERNAL, 18.6)
    await quirk.async_stop()


async def test_kick_temperature_is_clamped(hass: HomeAssistant) -> None:
    """The fake temperature stays in 0..99.9."""
    sim = TrvSim(hass).setup(setpoint=1.0)
    quirk = make_quirk(hass, sim, delta=5.0, hot_step=True)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(10))
    await flush()
    assert ("number", EXTERNAL, 0.0) in sim.calls
    await quirk.async_stop()


async def test_idle_not_rewritten_when_unchanged(hass: HomeAssistant) -> None:
    """idle_valve_position is written only when its value changes."""
    sim = TrvSim(hass).setup(idle=49)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(50))
    await flush()
    assert not [c for c in sim.calls if c[:2] == ("number", IDLE)]
    await quirk.async_stop()


async def test_idle_is_zero_when_trv_off(hass: HomeAssistant) -> None:
    """With the TRV off the idle position is closed and there is no kick."""
    sim = TrvSim(hass).setup(hvac="off", idle=12)
    quirk = make_quirk(hass, sim)
    await quirk.async_after_valve_command(valve_command(50))
    await flush()
    assert sim.calls == [("number", IDLE, 0)]


async def test_same_opening_does_not_kick_again(hass: HomeAssistant) -> None:
    """A regulation cycle that repeats the opening does not kick; a repair does."""
    sim = TrvSim(hass).setup()
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(50))
    await flush()
    await advance(hass, 5)
    sim.clear()

    await quirk.async_after_valve_command(valve_command(50))
    await flush()
    assert sim.calls == []

    await quirk.async_after_valve_command(
        valve_command(50, ValveCommandCause.REPAIR)
    )
    await flush()
    assert sim.external_on in sim.calls
    await quirk.async_stop()


async def test_recalibration_does_not_kick(hass: HomeAssistant) -> None:
    """The forced strokes of the recalibration start no kick and write no idle position."""
    sim = TrvSim(hass).setup()
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    sim.clear()
    await quirk.async_after_valve_command(valve_command(100, ValveCommandCause.RECALIBRATION))
    await quirk.async_after_valve_command(valve_command(0, ValveCommandCause.RECALIBRATION))
    await flush()
    await advance(hass, 5)
    assert sim.calls == []
    await quirk.async_stop()


async def test_requests_during_a_kick_are_coalesced(hass: HomeAssistant) -> None:
    """Three commands during a kick give a single extra kick."""
    sim = TrvSim(hass).setup()
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(10))
    await flush()
    assert sim.calls.count(("number", EXTERNAL, 18.0)) == 1

    for opening in (20, 30, 40):
        await quirk.async_after_valve_command(valve_command(opening))
    await flush()
    # The kick is still running: nothing more yet, but the idle follows the latest opening.
    assert sim.calls.count(("number", EXTERNAL, 18.0)) == 1
    assert [c for c in sim.calls if c[:2] == ("number", IDLE)][-1] == ("number", IDLE, 39)

    await advance(hass, 5)  # end of the first kick, start of the pending one
    assert sim.calls.count(("number", EXTERNAL, 18.0)) == 2
    await advance(hass, 5)  # end of the second kick
    await advance(hass, 5)
    assert sim.calls.count(("number", EXTERNAL, 18.0)) == 2
    assert sim.calls.count(sim.external_off) == 2
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# OFF
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_before_off_restores_before_returning(hass: HomeAssistant, kind: str) -> None:
    """The OFF hook returns once the kick is cancelled, the source restored and idle closed."""
    sim = TrvSim(hass, kind).setup(idle=0)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    assert sim.external_on in sim.calls  # the kick is waiting
    sim.clear()

    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)

    # Everything is written when the hook returns, the TRV is still in heat.
    assert sim.calls == [sim.external_off, ("number", IDLE, 0)]
    assert hass.states.get(CLIMATE).state == "heat"
    await advance(hass, 10)
    assert len(sim.calls) == 2  # no pending kick restarted
    await quirk.async_stop()


async def test_before_off_without_running_kick(hass: HomeAssistant) -> None:
    """Without kick, going OFF only closes idle_valve_position."""
    sim = TrvSim(hass).setup(idle=30)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    assert sim.calls == [("number", IDLE, 0)]
    await quirk.async_stop()


async def test_turn_off_valve_command(hass: HomeAssistant) -> None:
    """A TURN_OFF valve command cancels the kick and closes the idle position."""
    sim = TrvSim(hass).setup()
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    sim.clear()

    await quirk.async_after_valve_command(valve_command(0, ValveCommandCause.TURN_OFF))
    assert sim.calls == [sim.external_off, ("number", IDLE, 0)]
    await advance(hass, 10)
    assert len(sim.calls) == 2
    await quirk.async_stop()


async def test_regulation_command_off_while_trv_still_heat(
    hass: HomeAssistant,
) -> None:
    """A REGULATION command with hvac_mode off writes idle 0 and never kicks.

    The core closes the valve after sending the OFF mode, so the TRV can still
    read heat in Home Assistant when the command arrives.
    """
    sim = TrvSim(hass).setup(idle=30)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    assert not quirk._trv_is_off()

    await quirk.async_after_valve_command(
        valve_command(0, ValveCommandCause.REGULATION, hvac_mode="off")
    )
    await flush()
    assert sim.calls == [("number", IDLE, 0)]
    await advance(hass, 10)
    assert sim.calls == [("number", IDLE, 0)]
    await quirk.async_stop()


async def test_pending_kick_is_dropped_on_off(hass: HomeAssistant) -> None:
    """The pending request is cancelled with the running kick."""
    sim = TrvSim(hass).setup()
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(10))
    await flush()
    await quirk.async_after_valve_command(valve_command(20))
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    sim.clear()
    await advance(hass, 20)
    assert sim.calls == []
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# Back to HEAT
# ---------------------------------------------------------------------------
async def test_kick_waits_for_trv_heat(hass: HomeAssistant) -> None:
    """Leaving OFF: the kick starts only when the TRV reports heat."""
    sim = TrvSim(hass).setup(idle=0)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    await advance(hass, 5)
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    sim.set_hvac("off")
    await flush()
    sim.clear()

    # OFF -> heat announced: nothing happens while the TRV is still off.
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    await flush()
    assert sim.calls == []

    sim.set_hvac("heat", setpoint=21.0)
    await flush()
    assert ("number", IDLE, 59) in sim.calls
    assert ("number", EXTERNAL, 19.0) in sim.calls
    assert sim.external_on in sim.calls
    await advance(hass, 5)
    assert sim.calls[-1] == sim.external_off
    await quirk.async_stop()


async def test_trv_turned_off_on_the_device(hass: HomeAssistant) -> None:
    """The TRV going off by itself closes the idle position and cancels the kick."""
    sim = TrvSim(hass).setup(idle=0)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    sim.clear()
    sim.set_hvac("off")
    await flush()
    assert sim.calls == [sim.external_off, ("number", IDLE, 0)]
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# Startup, stop, degraded states
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_startup_restores_external_source(hass: HomeAssistant, kind: str) -> None:
    """A source left external with the local source configured is restored at startup."""
    sim = TrvSim(hass, kind).setup()
    if kind == "z2m":
        hass.states.async_set("select.trv_temperature_sensor_select", "remote_temperature")
    else:
        hass.states.async_set("switch.trv_external_temperature_sensor", "on")
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    assert sim.calls == [sim.external_off]
    await quirk.async_stop()


async def test_startup_keeps_external_source_in_remote_mode(hass: HomeAssistant) -> None:
    """With the remote source configured, an external sensor is the normal state."""
    sim = TrvSim(hass).setup()
    hass.states.async_set("select.trv_temperature_sensor_select", "remote_temperature")
    quirk = make_quirk(hass, sim, temperature_source="remote")
    await quirk.async_start()
    assert sim.calls == []
    await quirk.async_stop()


async def test_startup_closes_idle_of_an_off_trv(hass: HomeAssistant) -> None:
    """An OFF TRV with a non-zero idle position gets 0."""
    sim = TrvSim(hass).setup(hvac="off", idle=25)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    assert sim.calls == [("number", IDLE, 0)]
    await quirk.async_stop()


async def test_stop_cancels_and_restores(hass: HomeAssistant) -> None:
    """Stopping during a kick restores the temperature source."""
    sim = TrvSim(hass).setup()
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    sim.clear()
    await quirk.async_stop()
    assert sim.calls == [sim.external_off]


async def test_cancellation_restores_remote_value(hass: HomeAssistant) -> None:
    """Cancelling a remote-mode kick writes back the saved external value."""
    sim = TrvSim(hass).setup(external=23.0)
    quirk = make_quirk(hass, sim, temperature_source="remote")
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    sim.clear()
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    assert sim.calls == [("number", EXTERNAL, 23.0), ("number", IDLE, 0)]
    await quirk.async_stop()


async def test_no_setpoint_aborts_the_kick(hass: HomeAssistant) -> None:
    """Without a setpoint nothing is sent to the external temperature."""
    sim = TrvSim(hass).setup(setpoint=None)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    assert sim.calls == [("number", IDLE, 59)]
    await quirk.async_stop()


async def test_unavailable_trv_is_ignored(hass: HomeAssistant) -> None:
    """An unavailable TRV gets no write at all."""
    sim = TrvSim(hass).setup()
    hass.states.async_set(CLIMATE, "unavailable")
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    assert sim.calls == []
    await quirk.async_stop()


async def test_unavailable_idle_entity_is_ignored(hass: HomeAssistant) -> None:
    """An unavailable idle entity is not written, the kick still runs."""
    sim = TrvSim(hass).setup()
    hass.states.async_set(IDLE, "unavailable")
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    assert not [c for c in sim.calls if c[:2] == ("number", IDLE)]
    assert sim.external_on in sim.calls
    await quirk.async_stop()


async def test_service_failure_still_restores(hass: HomeAssistant) -> None:
    """A failing write during the kick does not prevent the restoration."""
    sim = TrvSim(hass).setup()
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    hass.services.async_remove("select", "select_option")
    await quirk.async_after_valve_command(valve_command(60))
    await flush()
    await advance(hass, 5)
    # The kick ended without exception and a new kick is possible.
    sim.clear()
    await quirk.async_after_valve_command(valve_command(70))
    await flush()
    assert ("number", EXTERNAL, 18.0) in sim.calls
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# check_configuration
# ---------------------------------------------------------------------------
def issue_keys(quirk: SonoffTrvZbtQuirk) -> set[str]:
    return {issue.key for issue in quirk.check_configuration()}


async def test_check_configuration_ok(hass: HomeAssistant) -> None:
    """A correct configuration reports nothing."""
    sim = TrvSim(hass).setup()
    assert issue_keys(make_quirk(hass, sim)) == set()


async def test_check_configuration_vtherm_settings(hass: HomeAssistant) -> None:
    """The VTherm settings incompatible with the quirk are reported."""
    sim = TrvSim(hass).setup()
    infos = vtherm_entry_infos(
        sync_device_internal_temp=True,
        closing_degree_entity_ids=[IDLE],
        opening_degree_entity_ids=["number.other_opening"],
    )
    keys = issue_keys(make_quirk(hass, sim, infos))
    assert keys == {
        "sync_device_internal_temp",
        "closing_entity_configured",
        "opening_entity_mismatch",
    }


async def test_check_configuration_device_settings(hass: HomeAssistant) -> None:
    """Adaptive control and open window enabled on the device are reported."""
    sim = TrvSim(hass).setup()
    quirk = make_quirk(hass, sim)
    quirk.entities["adaptive_control"] = "switch.trv_adaptive"
    quirk.entities["open_window"] = "switch.trv_open_window"
    hass.states.async_set("switch.trv_adaptive", "on")
    hass.states.async_set("switch.trv_open_window", "on")
    assert issue_keys(quirk) == {"adaptive_control_enabled", "open_window_enabled"}
    hass.states.async_set("switch.trv_adaptive", "off")
    hass.states.async_set("switch.trv_open_window", "off")
    assert issue_keys(quirk) == set()


async def test_check_configuration_disabled_entity(hass: HomeAssistant) -> None:
    """A disabled required entity (default on ZHA) is reported."""
    found = add_device(hass, "zha")
    sim = TrvSim(hass, "zha").setup()
    quirk = make_quirk(hass, sim)
    quirk.entities.update(found)
    assert "entity_disabled" in issue_keys(quirk)

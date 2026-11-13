"""Tests of the Sonoff TRV-ZBT quirk."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from vtherm_api import (
    ValveCommand,
    ValveCommandCause,
    ValveWriteDecision,
    ValveWriteRequest,
)

from custom_components.vtherm_quirks.quirks.sonoff_trv_zbt import (
    RECALIBRATION_TIMEOUT,
    SETTLE_MARGIN,
    SonoffTrvZbtQuirk,
)

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
    written_by_plugin: bool = False,
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
        written_by_plugin=written_by_plugin,
    )


def write_request(
    opening: int,
    cause: ValveCommandCause = ValveCommandCause.REGULATION,
    hvac_mode: str = "heat",
) -> ValveWriteRequest:
    return ValveWriteRequest(
        climate_entity_id=CLIMATE,
        opening_degree_entity_id=HEATING,
        closing_degree_entity_id=None,
        percent_open=opening,
        opening_degree=opening,
        requested_opening_degree=opening,
        max_opening_degree=100,
        hvac_mode=hvac_mode,
        cause=cause,
    )


async def send(
    quirk: SonoffTrvZbtQuirk,
    opening: int,
    cause: ValveCommandCause = ValveCommandCause.REGULATION,
    hvac_mode: str = "heat",
) -> ValveWriteDecision | None:
    """One valve command of the core: the hook before the write, then the hook after it.

    The write of the core itself is not simulated: the calls of ``sim`` are the
    writes of the quirk.
    """
    decision = await quirk.async_before_valve_write(write_request(opening, cause, hvac_mode))
    handled = decision is not None and decision.handled
    await quirk.async_after_valve_command(
        valve_command(opening, cause, hvac_mode, written_by_plugin=handled)
    )
    await flush()
    return decision


async def opened_at(hass: HomeAssistant, sim: TrvSim, opening: int, **options) -> SonoffTrvZbtQuirk:
    """A started quirk whose last command is ``opening``, its kick ended, calls cleared."""
    quirk = make_quirk(hass, sim, **options)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(opening))
    await flush()
    await advance(hass, 5)
    sim.clear()
    return quirk


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
        "pre_open": 10,
        "slam_delay": 5,
    }
    schema = SonoffTrvZbtQuirk.options_schema().schema
    selectors = {str(key): value.config for key, value in schema.items()}
    assert (selectors["pre_open"]["min"], selectors["pre_open"]["max"]) == (0, 100)
    assert (selectors["slam_delay"]["min"], selectors["slam_delay"]["max"]) == (1, 9)


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
# Slam close before OFF
# ---------------------------------------------------------------------------
def turn_off_hook(hass: HomeAssistant, quirk: SonoffTrvZbtQuirk):
    """The OFF hook as the core awaits it, in a task so that the test drives the clock."""
    return hass.async_create_task(
        quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    )


@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_slam_before_off(hass: HomeAssistant, kind: str) -> None:
    """A turn off from 3 %: idle 0, heating 10, cold, wait, restore, then the hook returns."""
    sim = TrvSim(hass, kind).setup(setpoint=20.0)
    quirk = await opened_at(hass, sim, 3)

    task = turn_off_hook(hass, quirk)
    await flush()
    assert not task.done()
    assert sim.calls == [
        ("number", IDLE, 0),
        ("number", HEATING, 10),
        ("number", EXTERNAL, 18.0),
        sim.external_on,
    ]

    await advance(hass, 4)
    assert not task.done()
    await advance(hass, 5)  # advance() counts from now, not from the previous call
    assert task.done()
    assert sim.calls[4:] == [sim.external_off]
    assert hass.states.get(CLIMATE).state == "heat"  # the OFF is sent by the core afterwards
    await quirk.async_stop()


@pytest.mark.parametrize(
    ("opening", "options"),
    [(50, {}), (10, {}), (0, {}), (3, {"pre_open": 0}), (30, {"pre_open": 40, "slam_delay": 1})],
)
async def test_slam_before_off_only_from_below_pre_open(
    hass: HomeAssistant, opening: int, options: dict
) -> None:
    """No slam close from an opening at or above the pre-opening, from 0, or with pre_open 0."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, opening, **options)
    task = turn_off_hook(hass, quirk)
    await flush()
    slammed = ("number", HEATING, options.get("pre_open", 10)) in sim.calls
    assert slammed == (opening == 30)
    if not slammed:
        assert task.done()
        assert sim.calls in ([], [("number", IDLE, 0)])
    await advance(hass, 1)
    assert task.done()
    await quirk.async_stop()


async def test_slam_before_off_without_last_opening(hass: HomeAssistant) -> None:
    """Without a command since the start there is no opening to close from: no slam close."""
    sim = TrvSim(hass).setup(idle=5)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    assert sim.calls == [("number", IDLE, 0)]
    await quirk.async_stop()


async def test_slam_before_off_needs_a_setpoint(hass: HomeAssistant) -> None:
    """Without setpoint no fake temperature can be sent: only the idle position is closed."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3)
    sim.set_hvac("heat", setpoint=None)
    await flush()
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    assert sim.calls == [("number", IDLE, 0)]
    await quirk.async_stop()


async def test_slam_before_off_wait_is_shortened(hass: HomeAssistant) -> None:
    """Slow writes shorten the wait so that the hook returns within its budget."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3, slam_delay=9)
    clock = [1000.0]
    sleeps: list[float] = []
    write = quirk._set_number

    async def slow_write(entity_id: str, value: float, *, blocking: bool = True) -> bool:
        clock[0] += 1.5
        return await write(entity_id, value, blocking=blocking)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    quirk._now = lambda: clock[0]
    quirk._set_number = slow_write
    quirk.async_sleep = sleep

    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    # Three numbers written (4.5 s), 0.5 s kept for the restoration: 9.5 - 0.5 - 4.5.
    assert sleeps == [pytest.approx(4.5)]
    assert sim.calls[-1] == sim.external_off
    await quirk.async_stop()


async def test_slam_before_off_full_delay_with_fast_writes(hass: HomeAssistant) -> None:
    """With instant writes the 9 seconds are waited."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3, slam_delay=9)
    clock = [1000.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    quirk._now = lambda: clock[0]
    quirk.async_sleep = sleep
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    assert sleeps == [9.0]
    await quirk.async_stop()


async def test_slam_before_off_cut_by_the_core_restores(hass: HomeAssistant) -> None:
    """A hook cancelled by the timeout of the core still restores the temperature source."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3)
    task = turn_off_hook(hass, quirk)
    await flush()
    assert sim.external_on in sim.calls
    task.cancel()
    await flush()
    assert task.done()
    assert sim.calls[-1] == sim.external_off
    await quirk.async_stop()


async def test_slam_before_off_remote_source(hass: HomeAssistant) -> None:
    """With the remote source, the saved external temperature is written back."""
    sim = TrvSim(hass).setup(external=22.5)
    hass.states.async_set("select.trv_temperature_sensor_select", "remote_temperature")
    quirk = await opened_at(hass, sim, 3, temperature_source="remote")
    task = turn_off_hook(hass, quirk)
    await flush()
    assert sim.external_on not in sim.calls  # already external
    await advance(hass, 5)
    assert task.done()
    assert sim.calls[-1] == ("number", EXTERNAL, 22.5)
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# Slam close of the regulation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_regulation_slam_close(hass: HomeAssistant, kind: str) -> None:
    """A regulation close from 3 %: pre-opening, then heating 0 and a hot step to idle."""
    sim = TrvSim(hass, kind).setup(setpoint=20.0)
    quirk = await opened_at(hass, sim, 3)

    decision = await send(quirk, 0)
    assert decision == ValveWriteDecision(
        handled=True, opening_degree=0, settle_sec=5 + 5 + SETTLE_MARGIN
    )
    assert sim.calls == [
        ("number", IDLE, 0),
        ("number", HEATING, 10),
        ("number", EXTERNAL, 18.0),
        sim.external_on,
    ]

    await advance(hass, 5)
    assert sim.calls[4:] == [("number", HEATING, 0), ("number", EXTERNAL, 22.0)]
    await advance(hass, 5)
    assert sim.calls[6:] == [sim.external_off]
    await advance(hass, 30)
    assert len(sim.calls) == 7  # no kick after the slam close
    await quirk.async_stop()


async def test_regulation_slam_settle_follows_the_delays(hass: HomeAssistant) -> None:
    """settle_sec is the pre-opening plus the kick duration plus the margin."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3, slam_delay=8, pulse_delay=5)
    decision = await send(quirk, 0)
    assert decision.settle_sec == 8 + 5 + SETTLE_MARGIN
    await quirk.async_stop()


async def test_regulation_close_from_above_pre_open_kicks(hass: HomeAssistant) -> None:
    """A close from 50 % is left to the core: idle 0 and a kick, as before."""
    sim = TrvSim(hass).setup(idle=49)
    quirk = await opened_at(hass, sim, 50)
    decision = await send(quirk, 0)
    assert decision is None
    assert sim.calls == [("number", IDLE, 0), ("number", EXTERNAL, 18.0), sim.external_on]
    await quirk.async_stop()


async def test_regulation_slam_uses_the_closed_floor(hass: HomeAssistant) -> None:
    """With max_closing_degree 95 an opening of 5 is a close: the slam ends at 5."""
    sim = TrvSim(hass).setup()
    infos = vtherm_entry_infos(max_closing_degree=95)
    quirk = make_quirk(hass, sim, infos)
    await quirk.async_start()
    await quirk.async_after_valve_command(valve_command(8))
    await flush()
    await advance(hass, 5)
    sim.clear()

    decision = await send(quirk, 5)
    assert decision.handled and decision.opening_degree == 5
    assert sim.calls[:2] == [("number", IDLE, 4), ("number", HEATING, 10)]
    await advance(hass, 5)
    assert ("number", HEATING, 5) in sim.calls
    await quirk.async_stop()


async def test_regulation_slam_cancelled_by_an_opening(hass: HomeAssistant) -> None:
    """An opening during the slam close drops it before the core writes."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3)
    await send(quirk, 0)
    sim.clear()

    decision = await send(quirk, 40)
    assert decision is None
    assert sim.calls == [
        sim.external_off,
        ("number", IDLE, 39),
        ("number", EXTERNAL, 18.0),
        sim.external_on,
    ]
    await advance(hass, 5)
    assert ("number", HEATING, 0) not in sim.calls
    await quirk.async_stop()


async def test_regulation_slam_cancelled_by_off(hass: HomeAssistant) -> None:
    """OFF during the slam close: source restored, idle 0, no second slam close."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3)
    await send(quirk, 0)
    sim.clear()

    task = turn_off_hook(hass, quirk)
    await flush()
    assert task.done()
    assert sim.calls == [sim.external_off]  # idle is already 0
    await advance(hass, 20)
    assert sim.calls == [sim.external_off]
    await quirk.async_stop()


async def test_regulation_close_sent_again_during_the_slam(hass: HomeAssistant) -> None:
    """A repair that sends the close again is taken over too, without new writes."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3)
    await send(quirk, 0)
    sim.clear()
    decision = await send(quirk, 0, ValveCommandCause.REPAIR)
    assert decision.handled
    assert SETTLE_MARGIN <= decision.settle_sec <= 5 + 5 + SETTLE_MARGIN
    assert sim.calls == []
    await quirk.async_stop()


async def test_regulation_same_close_after_the_slam(hass: HomeAssistant) -> None:
    """The next cycle sends 0 again: nothing is written and no kick runs."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3)
    await send(quirk, 0)
    await advance(hass, 5)
    await advance(hass, 5)
    sim.clear()
    assert await send(quirk, 0) is None
    await advance(hass, 10)
    assert sim.calls == []
    await quirk.async_stop()


@pytest.mark.parametrize(
    ("cause", "hvac_mode"),
    [(ValveCommandCause.TURN_OFF, "off"), (ValveCommandCause.REGULATION, "off")],
)
async def test_regulation_slam_not_for_a_turn_off(
    hass: HomeAssistant, cause: ValveCommandCause, hvac_mode: str
) -> None:
    """The close that follows the OFF mode is left to the core."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3)
    assert await quirk.async_before_valve_write(write_request(0, cause, hvac_mode)) is None
    await quirk.async_stop()


async def test_regulation_slam_not_when_trv_off(hass: HomeAssistant) -> None:
    """A TRV already off cannot go through the pre-opening."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3)
    sim.set_hvac("off")
    await flush()
    assert await quirk.async_before_valve_write(write_request(0)) is None
    await quirk.async_stop()


async def test_stop_during_the_regulation_slam_restores(hass: HomeAssistant) -> None:
    """Stopping the quirk during the slam close restores the temperature source."""
    sim = TrvSim(hass).setup()
    quirk = await opened_at(hass, sim, 3)
    await send(quirk, 0)
    sim.clear()
    await quirk.async_stop()
    assert sim.calls == [sim.external_off]


# ---------------------------------------------------------------------------
# Recalibration
# ---------------------------------------------------------------------------
async def recalibration_started(hass: HomeAssistant, sim: TrvSim) -> SonoffTrvZbtQuirk:
    """VTherm turned OFF from 50 %, then the core turns the TRV on for the recalibration."""
    quirk = await opened_at(hass, sim, 50)
    quirk.runtime.vtherm_hvac_mode = "off"
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    sim.set_hvac("off")
    await flush()
    await quirk.async_after_valve_command(
        valve_command(0, ValveCommandCause.TURN_OFF, hvac_mode="off")
    )
    sim.clear()
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    sim.set_hvac("heat", setpoint=20.0)
    await flush()
    return quirk


def recalibration(percent: int) -> ValveCommand:
    return valve_command(percent, ValveCommandCause.RECALIBRATION, hvac_mode="off")


@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_recalibration_sequence(hass: HomeAssistant, kind: str) -> None:
    """Cold at the full opening, hot at the full closing, source restored by the next command."""
    sim = TrvSim(hass, kind).setup(idle=49)
    quirk = await recalibration_started(hass, sim)
    assert sim.calls == []  # the TRV turned on: no resume, no kick

    await quirk.async_after_valve_command(recalibration(100))
    assert sim.calls == [("number", EXTERNAL, 18.0), sim.external_on]
    await advance(hass, 60)
    assert len(sim.calls) == 2  # the source stays external

    await quirk.async_after_valve_command(recalibration(0))
    assert sim.calls[2:] == [("number", EXTERNAL, 22.0)]  # no heating write, no sensor write

    quirk.runtime.vtherm_hvac_mode = "heat"
    await quirk.async_after_valve_command(valve_command(30))
    await flush()
    assert sim.calls[3:] == [
        sim.external_off,
        ("number", IDLE, 29),
        ("number", EXTERNAL, 18.0),
        sim.external_on,
    ]
    await quirk.async_stop()


async def test_recalibration_idle_closed(hass: HomeAssistant) -> None:
    """A recalibration that starts with an open idle position closes it."""
    sim = TrvSim(hass).setup()
    quirk = make_quirk(hass, sim)
    quirk.runtime.vtherm_hvac_mode = "off"
    await quirk.async_start()
    hass.states.async_set(IDLE, "40")
    await quirk.async_after_valve_command(recalibration(100))
    await flush()
    assert sim.calls[0] == ("number", IDLE, 0)
    assert sim.calls[1:] == [("number", EXTERNAL, 18.0), sim.external_on]
    await quirk.async_stop()


async def test_recalibration_ended_by_off(hass: HomeAssistant) -> None:
    """A recalibration that ends with VTherm OFF restores the source before the OFF."""
    sim = TrvSim(hass).setup()
    quirk = await recalibration_started(hass, sim)
    await quirk.async_after_valve_command(recalibration(100))
    await quirk.async_after_valve_command(recalibration(0))
    sim.clear()
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    assert sim.calls == [sim.external_off]  # idle already 0, no slam close
    await quirk.async_stop()


async def test_recalibration_timeout(hass: HomeAssistant) -> None:
    """Without any command after it, the recalibration ends after the safety delay."""
    sim = TrvSim(hass).setup()
    quirk = await recalibration_started(hass, sim)
    await quirk.async_after_valve_command(recalibration(100))
    await quirk.async_after_valve_command(recalibration(0))
    sim.clear()
    await advance(hass, RECALIBRATION_TIMEOUT - 10)
    assert sim.calls == []
    await advance(hass, RECALIBRATION_TIMEOUT + 1)
    assert sim.calls == [sim.external_off]
    assert not quirk._recalibrating
    await quirk.async_stop()


async def test_recalibration_stop_restores(hass: HomeAssistant) -> None:
    """Stopping the quirk during a recalibration restores the source."""
    sim = TrvSim(hass).setup()
    quirk = await recalibration_started(hass, sim)
    await quirk.async_after_valve_command(recalibration(100))
    sim.clear()
    await quirk.async_stop()
    assert sim.calls == [sim.external_off]


async def test_recalibration_remote_source(hass: HomeAssistant) -> None:
    """With the remote source the external value saved at the first step is written back."""
    sim = TrvSim(hass).setup(external=21.0)
    hass.states.async_set("select.trv_temperature_sensor_select", "remote_temperature")
    quirk = make_quirk(hass, sim, temperature_source="remote")
    quirk.runtime.vtherm_hvac_mode = "off"
    await quirk.async_start()
    await quirk.async_after_valve_command(recalibration(100))
    await quirk.async_after_valve_command(recalibration(0))
    sim.clear()
    await quirk.async_after_valve_command(valve_command(0, ValveCommandCause.REPAIR))
    assert sim.calls[0] == ("number", EXTERNAL, 21.0)
    await quirk.async_stop()


async def test_recalibration_without_setpoint(hass: HomeAssistant) -> None:
    """Without setpoint the steps send nothing, and nothing is restored."""
    sim = TrvSim(hass).setup(setpoint=None)
    quirk = make_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(recalibration(100))
    await quirk.async_after_valve_command(recalibration(0))
    await quirk.async_after_valve_command(valve_command(0, ValveCommandCause.REPAIR, hvac_mode="off"))
    assert sim.external_on not in sim.calls
    assert sim.external_off not in sim.calls
    await quirk.async_stop()


@pytest.mark.parametrize(
    ("vtherm_mode", "cause"),
    [("heat", None), ("off", "underlying_tracking")],
)
async def test_leaving_off_is_not_always_a_recalibration(
    hass: HomeAssistant, vtherm_mode: str, cause: str | None
) -> None:
    """VTherm turned on, or the TRV turned on at the device: the kick resumes."""
    sim = TrvSim(hass).setup(idle=0)
    quirk = await opened_at(hass, sim, 60)
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    sim.set_hvac("off")
    await flush()
    sim.clear()
    quirk.runtime.vtherm_hvac_mode = vtherm_mode
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", cause)
    sim.set_hvac("heat", setpoint=20.0)
    await flush()
    assert not quirk._recalibrating
    assert sim.external_on in sim.calls
    await advance(hass, 5)
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

"""Preservation tests of spec trvzb-duplicate-quirk-instances (requirements 3.1 to 3.11).

Observation-first: these tests describe the behavior of the code BEFORE the
fix for inputs where the bug condition is false. They must pass on the
unfixed code and keep passing after the fix.

No hypothesis: parametrize and seeded ``random.Random`` sequences.
"""

from __future__ import annotations

import random

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from vtherm_api import ValveCommandCause

from custom_components.vtherm_quirks.const import DOMAIN
from custom_components.vtherm_quirks.quirks.sonoff_trvzb import (
    MAX_CORRECTIONS,
    RESEND_DELAY,
    closing_for,
)

from .common import (
    CLIMATE,
    HEATING,
    TRVZB_CLOSING,
    TRVZB_OPENING,
    TrvSim,
    TrvzbSim,
    advance,
    flush,
)
from .quirks.test_sonoff_trv_zbt import make_quirk as make_zbt_quirk
from .quirks.test_sonoff_trv_zbt import valve_command as zbt_command
from .quirks.test_sonoff_trvzb import command, send, started
from .test_duplicate_instances import Plugin, closing_writes, listening, quirk_of

ISSUE_ID = f"vtherm_salon_{CLIMATE}_closing_degree_keeps_changing"
SEEDS = [1, 2, 3, 4, 5]


def closing_state(hass: HomeAssistant) -> str:
    return hass.states.get(TRVZB_CLOSING).state


def keeps_changing_issue(hass: HomeAssistant):
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)


async def make_stuck(hass: HomeAssistant) -> None:
    """Change the closing alone until the quirk gives up and reports."""
    for _ in range(MAX_CORRECTIONS + 1):
        hass.states.async_set(TRVZB_CLOSING, "20")
        await advance(hass, RESEND_DELAY + 1)


@pytest.fixture
async def plugin(hass: HomeAssistant):
    plug = Plugin(hass)
    await plug.setup()
    yield plug
    for manager in plug.managers:
        manager.stop_listening()
    await flush()


# ---------------------------------------------------------------------------
# 3.1: closing = 100 - opening after each command
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", SEEDS)
async def test_closing_follows_opening_after_each_command(
    hass: HomeAssistant, seed: int
) -> None:
    rng = random.Random(seed)
    sim = TrvzbSim(hass).setup(opening="0", closing="100")
    quirk = await started(hass, sim)
    causes = list(ValveCommandCause)
    for _ in range(10):
        opening = rng.randint(0, 100)
        cause = rng.choice(causes)
        mode = "off" if cause == ValveCommandCause.TURN_OFF else rng.choice(["heat", "cool"])
        hass.states.async_set(TRVZB_OPENING, str(opening))
        await send(quirk, opening, cause, hvac_mode=mode)
        await advance(hass, 6)  # lets a slam close finish
        assert closing_state(hass) == str(closing_for(opening)), (seed, opening, cause)
        assert quirk._last_opening == opening
        assert quirk._stuck is None
    await quirk.async_stop()


async def test_closing_written_through_the_manager_hook(
    hass: HomeAssistant, plugin: Plugin
) -> None:
    sim = TrvzbSim(hass).setup(opening="0", closing="100")
    manager = plugin.manager()
    await manager.start_listening()
    await flush()
    sim.clear()
    await manager.async_after_valve_command(command(40))
    await flush()
    assert sim.calls == [sim.closing(60)]


# ---------------------------------------------------------------------------
# 3.2: closing changed alone is restored, then the repair (the existing
# test_watcher_* tests cover the nominal case). Here: restoration by manager.
# ---------------------------------------------------------------------------
async def test_closing_alone_is_restored_then_reported_through_the_manager(
    hass: HomeAssistant, plugin: Plugin
) -> None:
    sim = TrvzbSim(hass).setup(opening="40", closing="60")
    manager = plugin.manager()
    await manager.start_listening()
    await manager.async_after_valve_command(command(40))
    await flush()
    sim.clear()

    for _ in range(MAX_CORRECTIONS):
        hass.states.async_set(TRVZB_CLOSING, "20")
        await advance(hass, RESEND_DELAY + 1)
    assert closing_writes(sim) == [sim.closing(60)] * MAX_CORRECTIONS
    assert keeps_changing_issue(hass) is None

    hass.states.async_set(TRVZB_CLOSING, "20")
    await advance(hass, RESEND_DELAY + 1)
    assert len(closing_writes(sim)) == MAX_CORRECTIONS
    assert keeps_changing_issue(hass) is not None


# ---------------------------------------------------------------------------
# 3.3: slam close and _supersede_slam
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("previous", [1, 30, 98])
async def test_slam_close_writes(hass: HomeAssistant, previous: int) -> None:
    sim = TrvzbSim(hass).setup(opening=str(previous), closing=str(100 - previous))
    quirk = await started(hass, sim)
    await send(quirk, 0)
    assert sim.calls == [sim.opening(99), sim.closing(1)]
    await advance(hass, 3)
    assert len(sim.calls) == 2
    await advance(hass, 6)
    assert sim.calls == [sim.opening(99), sim.closing(1), sim.closing(100), sim.opening(0)]
    await advance(hass, RESEND_DELAY + 1)
    assert len(sim.calls) == 4
    await quirk.async_stop()


@pytest.mark.parametrize("reopen", [1, 40, 100])
async def test_supersede_slam_writes(hass: HomeAssistant, reopen: int) -> None:
    sim = TrvzbSim(hass).setup(opening="30", closing="70")
    quirk = await started(hass, sim)
    await send(quirk, 0)
    await send(quirk, reopen)
    assert sim.calls == [
        sim.opening(99),
        sim.closing(1),
        sim.opening(reopen),
        sim.closing(100 - reopen),
    ]
    await advance(hass, 6)
    assert len(sim.calls) == 4
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# 3.4, 3.5: normal reload with a single manager
# ---------------------------------------------------------------------------
async def test_single_manager_lifecycle(hass: HomeAssistant, plugin: Plugin) -> None:
    sim = TrvzbSim(hass).setup(opening="40", closing="60")
    manager = plugin.manager()
    quirk = quirk_of(manager)
    await manager.start_listening()
    await flush()
    assert quirk._started
    assert quirk._unsubs
    assert quirk.issue_sync_callback is not None

    await manager.async_after_valve_command(command(70))
    await flush()
    assert closing_state(hass) == "30"

    await make_stuck(hass)
    assert keeps_changing_issue(hass) is not None

    assert manager.stop_listening() is True
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not quirk._started
    assert not quirk._unsubs
    assert not quirk._tasks
    assert quirk.issue_sync_callback is None
    assert keeps_changing_issue(hass) is None
    assert not manager._listeners

    # No more reaction once stopped.
    sim.clear()
    hass.states.async_set(TRVZB_CLOSING, "5")
    await advance(hass, RESEND_DELAY + 1)
    assert sim.calls == []


async def test_reload_replaces_the_manager(hass: HomeAssistant, plugin: Plugin) -> None:
    """The old manager stops, the new one starts: one listening quirk, hooks reach it."""
    sim = TrvzbSim(hass).setup(opening="40", closing="60")
    old = plugin.manager()
    await old.start_listening()
    await flush()
    old.stop_listening()
    await hass.async_block_till_done(wait_background_tasks=True)
    new = plugin.manager()
    await new.start_listening()
    await flush()
    assert [m for m in (old, new) if listening(quirk_of(m))] == [new]

    sim.clear()
    await new.async_after_valve_command(command(80))
    await flush()
    assert sim.calls == [sim.closing(20)]
    await advance(hass, 3 * (RESEND_DELAY + 1))
    assert sim.calls == [sim.closing(20)]


# ---------------------------------------------------------------------------
# 3.6, 3.11: other quirks unchanged (their own suites cover the rest)
# ---------------------------------------------------------------------------
async def test_trv_zbt_ignores_an_opening_changed_outside_the_hook(
    hass: HomeAssistant,
) -> None:
    sim = TrvSim(hass).setup(idle=0)
    quirk = make_zbt_quirk(hass, sim)
    await quirk.async_start()
    await quirk.async_after_valve_command(zbt_command(60))
    await flush()
    await advance(hass, 10)
    sim.clear()

    hass.states.async_set(HEATING, "100")
    await advance(hass, 3 * (RESEND_DELAY + 1))
    assert sim.calls == []

    # The same command again writes and kicks nothing.
    await quirk.async_after_valve_command(zbt_command(60))
    await flush()
    assert sim.calls == []
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# 3.7: random sequences on a single instance with a consistent opening
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", SEEDS)
async def test_random_sequences_on_a_single_instance(hass: HomeAssistant, seed: int) -> None:
    """Writes and correction counters follow the reference model of the original code."""
    rng = random.Random(seed)
    sim = TrvzbSim(hass).setup(opening="50", closing="50")
    quirk = await started(hass, sim)
    corrections = 0
    stuck = False
    last = quirk._last_opening
    assert last == 50

    for step in range(14):
        sim.clear()
        if rng.random() < 0.5:
            # VTherm command; openings from 1 to 100 never trigger a slam close.
            opening = rng.randint(1, 100)
            hass.states.async_set(TRVZB_OPENING, str(opening))
            before = closing_state(hass)
            await send(quirk, opening)
            expected = [] if before == str(closing_for(opening)) else [sim.closing(closing_for(opening))]
            assert sim.calls == expected, (seed, step)
            last = opening
            await advance(hass, RESEND_DELAY + 1)
            assert closing_writes(sim) == expected
        else:
            # Something else changes the closing alone.
            current = closing_state(hass)
            value = rng.choice(
                [v for v in range(0, 101) if str(v) != current and v != closing_for(last)]
            )
            hass.states.async_set(TRVZB_CLOSING, str(value))
            await advance(hass, RESEND_DELAY + 1)
            if stuck:
                assert sim.calls == [], (seed, step)
            elif corrections < MAX_CORRECTIONS:
                corrections += 1
                assert sim.calls == [sim.closing(closing_for(last))], (seed, step)
            else:
                stuck = True
                assert sim.calls == [], (seed, step)
        assert len(quirk._corrections) == corrections, (seed, step)
        assert (quirk._stuck is not None) == stuck, (seed, step)
        assert quirk._last_opening == last
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# 3.8: stop_listening() of a manager that never started clears nothing
# ---------------------------------------------------------------------------
async def test_stop_of_a_never_started_manager_keeps_the_repairs(
    hass: HomeAssistant, plugin: Plugin
) -> None:
    TrvzbSim(hass).setup(opening="40", closing="60")
    running, never_started = plugin.manager(), plugin.manager()
    await running.start_listening()
    await running.async_after_valve_command(command(40))
    await flush()
    await make_stuck(hass)
    assert keeps_changing_issue(hass) is not None

    never_started.stop_listening()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert keeps_changing_issue(hass) is not None
    assert listening(quirk_of(running))


# ---------------------------------------------------------------------------
# 3.9: unreadable opening, or opening and closing both inconsistent
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("opening_state", ["unavailable", "unknown", None, "not_a_number"])
@pytest.mark.parametrize("closing_value", ["0", "20", "90"])
async def test_unreadable_opening_restores_to_last_opening(
    hass: HomeAssistant, opening_state: str | None, closing_value: str
) -> None:
    sim = TrvzbSim(hass).setup(opening="40", closing="60")
    quirk = await started(hass, sim)
    await send(quirk, 40)
    if opening_state is None:
        hass.states.async_remove(TRVZB_OPENING)
    else:
        hass.states.async_set(TRVZB_OPENING, opening_state)
    hass.states.async_set(TRVZB_CLOSING, closing_value)
    await advance(hass, RESEND_DELAY + 1)

    assert sim.calls == [sim.closing(60)]
    assert quirk._last_opening == 40
    assert len(quirk._corrections) == 1
    await quirk.async_stop()


@pytest.mark.parametrize("seed", SEEDS)
async def test_inconsistent_opening_and_closing_restore_to_last_opening(
    hass: HomeAssistant, seed: int
) -> None:
    rng = random.Random(seed)
    last = rng.randint(1, 100)
    sim = TrvzbSim(hass).setup(opening=str(last), closing=str(closing_for(last)))
    quirk = await started(hass, sim)
    await send(quirk, last, ValveCommandCause.REPAIR)
    sim.clear()

    real = rng.choice([v for v in range(0, 101) if v != last])
    bad = rng.choice([v for v in range(0, 101) if v not in (closing_for(real), closing_for(last))])
    hass.states.async_set(TRVZB_OPENING, str(real))
    hass.states.async_set(TRVZB_CLOSING, str(bad))
    await advance(hass, RESEND_DELAY + 1)

    assert sim.calls == [sim.closing(closing_for(last))], (last, real, bad)
    assert quirk._last_opening == last
    assert len(quirk._corrections) == 1
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# 3.10: opening state older than the last knowledge of the quirk
# ---------------------------------------------------------------------------
async def test_opening_state_older_than_the_command_is_not_a_reference(
    hass: HomeAssistant,
) -> None:
    sim = TrvzbSim(hass).setup(opening="0", closing="100")
    quirk = await started(hass, sim)
    # The opening state predates the command (write of the core arriving late).
    hass.states.async_set(TRVZB_OPENING, "100")
    await flush()
    await send(quirk, 40)
    sim.clear()

    hass.states.async_set(TRVZB_CLOSING, str(closing_for(100)))
    await advance(hass, RESEND_DELAY + 1)

    assert sim.calls == [sim.closing(60)]
    assert quirk._last_opening == 40
    assert len(quirk._corrections) == 1
    await quirk.async_stop()

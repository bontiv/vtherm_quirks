"""Bug condition exploration: duplicate SonoffTrvzbQuirk instances.

These tests encode the EXPECTED behavior of spec trvzb-duplicate-quirk-instances.
They are meant to FAIL on the unfixed code and to pass once the fix is in.

Case A: a stopped manager must not start anything.
Case B: only one instance may listen to the closing degree of an underlying.
Case C: an opening changed outside the hook must not trigger a wrong correction.
Edge case: a stop during the startup must not leave a subscription behind.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vtherm_quirks.const import DOMAIN
from custom_components.vtherm_quirks.factory import QuirksManagerFactory
from custom_components.vtherm_quirks.manager import QuirksManager
from custom_components.vtherm_quirks.quirks.sonoff_trvzb import (
    MAX_CORRECTIONS,
    RESEND_DELAY,
    SonoffTrvzbQuirk,
    closing_for,
)

from .common import (
    CLIMATE,
    TRVZB_CLOSING,
    TRVZB_OPENING,
    FakeRuntime,
    TrvzbSim,
    add_vtherm_entry,
    advance,
    flush,
    vtherm_entry_infos,
)
from .quirks.test_sonoff_trvzb import make_quirk, send, started

VTHERM = {"opening_degree_entity_ids": [TRVZB_OPENING]}
QUIRKS_DATA = {
    CLIMATE: {"quirk": "sonoff_trvzb", "entities": TrvzbSim.entities, "options": {}}
}


def closing_writes(sim: TrvzbSim) -> list[tuple]:
    return [call for call in sim.calls if call[1] == TRVZB_CLOSING]


def listening(quirk: SonoffTrvzbQuirk) -> bool:
    """True when the quirk is started and holds subscriptions."""
    return quirk._started or bool(quirk._unsubs)


class Plugin:
    """A plugin entry plus a way to build several managers like the core does."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.managers: list[QuirksManager] = []

    async def setup(self) -> None:
        add_vtherm_entry(self.hass, **VTHERM)
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Salon",
            unique_id="vtherm_salon",
            data={"target_vtherm_unique_id": "vtherm_salon", "quirks": QUIRKS_DATA},
        )
        entry.add_to_hass(self.hass)
        assert await self.hass.config_entries.async_setup(entry.entry_id)
        await self.hass.async_block_till_done()

    def manager(self) -> QuirksManager:
        runtime = FakeRuntime(self.hass, vtherm_entry_infos(**VTHERM))
        manager = QuirksManagerFactory().create(runtime)
        manager.post_init(runtime.entry_infos)
        self.managers.append(manager)
        return manager


@pytest.fixture
async def plugin(hass: HomeAssistant):
    plug = Plugin(hass)
    await plug.setup()
    yield plug
    for manager in plug.managers:
        manager.stop_listening()
    await flush()


def quirk_of(manager: QuirksManager) -> SonoffTrvzbQuirk:
    quirk = manager.quirks[CLIMATE]
    assert isinstance(quirk, SonoffTrvzbQuirk)
    return quirk


def assert_nothing_started(manager: QuirksManager) -> None:
    quirk = quirk_of(manager)
    assert not quirk._started, "quirk started by a stopped manager"
    assert not quirk._unsubs, "subscriptions created by a stopped manager"
    assert not quirk._tasks
    assert manager._issue_signature is None, "repairs synchronized by a stopped manager"


# ---------------------------------------------------------------------------
# Case A: a stopped manager is started
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "stops", [1, 2], ids=["stop_then_start", "stop_twice_then_start"]
)
async def test_case_a_stopped_manager_starts_nothing(
    hass: HomeAssistant, plugin: Plugin, stops: int
) -> None:
    TrvzbSim(hass).setup(opening="10", closing="90")
    manager = plugin.manager()
    for _ in range(stops):
        manager.stop_listening()
    await manager.start_listening()
    await flush()
    assert_nothing_started(manager)


class SlowStartQuirk(SonoffTrvzbQuirk):
    """Its startup waits for a gate, to stop the manager in the middle."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.gate = asyncio.Event()

    async def async_startup(self) -> None:
        await self.gate.wait()
        await super().async_startup()


async def test_case_a_stop_during_startup_leaves_no_subscription(
    hass: HomeAssistant, plugin: Plugin
) -> None:
    """stop_listening() in the middle of start_listening() leaves nothing running."""
    TrvzbSim(hass).setup(opening="10", closing="90")
    manager = plugin.manager()
    original = quirk_of(manager)
    slow = SlowStartQuirk(
        hass, original.runtime, CLIMATE, dict(TrvzbSim.entities), original.options
    )
    manager.quirks[CLIMATE] = slow

    start = hass.async_create_task(manager.start_listening())
    await flush()
    assert not start.done()
    manager.stop_listening()
    await flush()
    slow.gate.set()
    await start
    await flush()

    assert not slow._started
    assert not slow._unsubs, "closing subscription left after the stop"
    assert not slow._tasks


async def test_quirk_stopped_during_start_does_not_subscribe(hass: HomeAssistant) -> None:
    """SonoffTrvzbQuirk.async_start() interrupted by async_stop() must not subscribe."""
    TrvzbSim(hass).setup(opening="10", closing="90")
    base = make_quirk(hass)
    slow = SlowStartQuirk(
        hass, base.runtime, CLIMATE, dict(TrvzbSim.entities), base.options
    )
    start = hass.async_create_task(slow.async_start())
    await flush()
    assert not start.done()
    await slow.async_stop()
    slow.gate.set()
    await start
    await flush()
    assert not slow._unsubs, "closing subscription added after async_stop"


# ---------------------------------------------------------------------------
# Case B: two instances on the same underlying (issue #5)
# ---------------------------------------------------------------------------
async def idle_for_a_while(hass: HomeAssistant, windows: int = 2 * (MAX_CORRECTIONS + 1)) -> None:
    for _ in range(windows):
        await advance(hass, RESEND_DELAY + 1)


async def test_case_b_second_instance_replaces_the_first(
    hass: HomeAssistant, plugin: Plugin
) -> None:
    """Issue #5: opening 10 then 100 between two starts, no command afterwards."""
    sim = TrvzbSim(hass).setup(opening="10", closing="90")
    first, second = plugin.manager(), plugin.manager()
    await first.start_listening()
    await flush()
    assert quirk_of(first)._last_opening == 10

    hass.states.async_set(TRVZB_OPENING, "100")
    await second.start_listening()
    await flush()
    assert quirk_of(second)._last_opening == 100
    assert hass.states.get(TRVZB_CLOSING).state == "0"
    sim.clear()

    await idle_for_a_while(hass)

    assert closing_writes(sim) == [], f"closing written without command: {closing_writes(sim)}"
    assert hass.states.get(TRVZB_CLOSING).state == "0"
    assert [m for m in (first, second) if listening(quirk_of(m))] == [second]
    assert quirk_of(first)._stuck is None
    assert quirk_of(second)._stuck is None


@pytest.mark.parametrize(
    "stale_starts", ["first", "second"], ids=["stale_first", "stale_second"]
)
async def test_case_b_stopped_manager_never_competes(
    hass: HomeAssistant, plugin: Plugin, stale_starts: str
) -> None:
    """The manager of the removed entity starts before or after the legitimate one."""
    sim = TrvzbSim(hass).setup(opening="10", closing="90")
    stale, legit = plugin.manager(), plugin.manager()
    stale.stop_listening()  # remove_thermostat() before async_startup()

    if stale_starts == "first":
        await stale.start_listening()
        await flush()
        hass.states.async_set(TRVZB_OPENING, "100")
        await legit.start_listening()
    else:
        await legit.start_listening()
        await flush()
        hass.states.async_set(TRVZB_OPENING, "100")
        await stale.start_listening()
    await flush()
    sim.clear()

    await idle_for_a_while(hass)

    assert not listening(quirk_of(stale)), "stopped manager started its quirk"
    assert listening(quirk_of(legit)), "legitimate instance was stopped"
    assert closing_writes(sim) == [], f"closing written without command: {closing_writes(sim)}"
    assert quirk_of(legit)._stuck is None


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
async def test_case_b_random_start_stop_sequences(
    hass: HomeAssistant, plugin: Plugin, seed: int
) -> None:
    """At most one instance listens, and a stopped manager never has an active quirk."""
    rng = random.Random(seed)
    sim = TrvzbSim(hass).setup(opening="10", closing="90")
    managers = [plugin.manager() for _ in range(3)]
    stopped: set[int] = set()
    for _ in range(8):
        manager = rng.choice(managers)
        if rng.random() < 0.5:
            manager.stop_listening()
            stopped.add(id(manager))
        else:
            hass.states.async_set(TRVZB_OPENING, str(rng.choice([0, 10, 40, 100])))
            await manager.start_listening()
        await flush()
        live = [m for m in managers if listening(quirk_of(m))]
        assert len(live) <= 1, f"{len(live)} instances listen"
        for m in managers:
            if id(m) in stopped:
                assert not listening(quirk_of(m)), "a stopped manager has an active quirk"
    sim.clear()
    await idle_for_a_while(hass)
    assert closing_writes(sim) == []


# ---------------------------------------------------------------------------
# Case C: opening changed outside the hook (recalibrate_valves)
# ---------------------------------------------------------------------------
GRID = [0, 25, 40, 60, 99, 100]
PAIRS = [(real, last) for real in GRID for last in GRID if real != last]
_rng = random.Random(42)
PAIRS += [
    tuple(_rng.sample(range(0, 101), 2)) for _ in range(10)
]


@pytest.mark.parametrize(("real", "last"), PAIRS)
async def test_case_c_consistent_external_opening_resyncs(
    hass: HomeAssistant, real: int, last: int
) -> None:
    sim = TrvzbSim(hass).setup(opening="0", closing="100")
    quirk = await started(hass, sim)
    # VTherm writes the opening degree, then calls the hook.
    hass.states.async_set(TRVZB_OPENING, str(last))
    await send(quirk, last)
    sim.clear()

    # What recalibrate_valves does: write the pair directly, without the hook.
    hass.states.async_set(TRVZB_OPENING, str(real))
    hass.states.async_set(TRVZB_CLOSING, str(closing_for(real)))
    await advance(hass, RESEND_DELAY + 1)

    assert quirk._last_opening == real
    assert closing_writes(sim) == [], f"wrong correction: {closing_writes(sim)}"
    await quirk.async_stop()

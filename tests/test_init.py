"""Tests of the setup and the unload of the integration."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry
from vtherm_api import VThermAPI

import custom_components.vtherm_quirks as plugin
from custom_components.vtherm_quirks.const import DOMAIN

from .common import add_vtherm_entry


def plugin_entry(vtherm_id: str, quirks: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=vtherm_id,
        unique_id=vtherm_id,
        data={"target_vtherm_unique_id": vtherm_id, "quirks": quirks or {}},
    )


async def test_setup_registers_the_factory_and_unload_removes_it(hass: HomeAssistant) -> None:
    """The factory lives as long as one entry is loaded."""
    add_vtherm_entry(hass, "vt1", name="One")
    add_vtherm_entry(hass, "vt2", name="Two")
    first, second = plugin_entry("vt1"), plugin_entry("vt2")
    first.add_to_hass(hass)
    second.add_to_hass(hass)

    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    api = VThermAPI.get_vtherm_api(hass)
    assert api.get_feature_manager("vtherm_quirks") is not None
    assert [f.name for f in api.get_feature_manager_factories()] == ["vtherm_quirks"]

    # Home Assistant sets up all the entries of the domain together.
    assert second.state is ConfigEntryState.LOADED
    # A single factory for all the entries.
    assert api.list_feature_managers() == ["vtherm_quirks"]

    assert await hass.config_entries.async_unload(first.entry_id)
    assert api.get_feature_manager("vtherm_quirks") is not None
    assert await hass.config_entries.async_unload(second.entry_id)
    assert api.get_feature_manager("vtherm_quirks") is None


async def test_reload_touches_only_the_concerned_vtherm(hass: HomeAssistant) -> None:
    """Loading or unloading an entry reloads the VTherm it targets, not the others."""
    vt1 = add_vtherm_entry(hass, "vt1", name="One")
    vt2 = add_vtherm_entry(hass, "vt2", name="Two")
    for entry in (vt1, vt2):
        entry.mock_state(hass, ConfigEntryState.LOADED)
    first = plugin_entry("vt1")
    first.add_to_hass(hass)

    # No climate entity exists in the test: the VTherm is not "missing a manager"
    # so the setup does not reload it. Unload always does.
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload:
        assert await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()
        assert reload.call_count == 0
        assert await hass.config_entries.async_unload(first.entry_id)
        await hass.async_block_till_done()
        assert [call.args[0] for call in reload.call_args_list] == ["vt1"]


async def test_setup_reloads_a_vtherm_without_manager(hass: HomeAssistant) -> None:
    """A running VTherm that lacks the manager is reloaded when its entry is set up."""

    class FakeEntity:
        unique_id = "vt1"
        is_ready = True

        def get_feature_manager(self, name: str):
            return None

    class FakeComponent:
        entities = [FakeEntity()]

    hass.data["climate"] = FakeComponent()
    vt1 = add_vtherm_entry(hass, "vt1", name="One")
    vt1.mock_state(hass, ConfigEntryState.LOADED)
    first = plugin_entry("vt1")
    first.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload:
        assert await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()
        assert [call.args[0] for call in reload.call_args_list] == ["vt1"]


async def test_setup_fails_when_hooks_are_unavailable(hass: HomeAssistant) -> None:
    """Without the hooks contract, the entry does not load and a repair is raised."""
    add_vtherm_entry(hass, "vt1", name="One")
    entry = plugin_entry("vt1")
    entry.add_to_hass(hass)
    with patch.object(plugin, "hooks_available", return_value=False):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(DOMAIN, "hooks_unavailable") is not None
    api = VThermAPI.get_vtherm_api(hass)
    assert api.get_feature_manager("vtherm_quirks") is None


def test_hooks_available_with_the_installed_api() -> None:
    """The installed vtherm_api exposes the hooks contract."""
    assert plugin.hooks_available()


async def test_unload_after_started_does_not_remove_the_listener_twice(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """The started callback is already gone when the entry unloads: no error is logged."""
    add_vtherm_entry(hass, "vt1", name="One").mock_state(hass, ConfigEntryState.LOADED)
    entry = plugin_entry("vt1")
    entry.add_to_hass(hass)

    hass.set_state(CoreState.not_running)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.async_start()
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert "Unable to remove unknown job listener" not in caplog.text


# --- Bug condition exploration: premature reload of a VTherm still starting up ---
# These tests encode the EXPECTED behavior: at the started event, a VTherm whose
# async_startup() is not finished (is_ready False, manager not attached yet) must
# not be reloaded. reload_called = (startup done after wait AND manager still
# missing after re-read).
# The constants of the fix do not exist yet on unfixed code: patched with create=True.


class _StartingEntity:
    """Climate entity stub: not ready and without manager until startup() is called."""

    def __init__(self, unique_id: str) -> None:
        self.unique_id = unique_id
        self.is_ready = False
        self._manager = None

    def get_feature_manager(self, name: str):
        return self._manager

    def startup(self, with_manager: bool = True) -> None:
        """Simulate the end of async_startup(): manager attached, then ready."""
        if with_manager:
            self._manager = object()
        self.is_ready = True


class _FakeClimateComponent:
    def __init__(self, entities: list) -> None:
        self.entities = entities


async def _start_with_entries(hass: HomeAssistant, ids: list[str]) -> list[MockConfigEntry]:
    """Set up one plugin entry per VTherm before HA is started (entities not ready)."""
    entries = []
    for vid in ids:
        add_vtherm_entry(hass, vid, name=vid).mock_state(hass, ConfigEntryState.LOADED)
        entry = plugin_entry(vid)
        entry.add_to_hass(hass)
        entries.append(entry)
    hass.set_state(CoreState.not_running)
    # Home Assistant sets up all the entries of the domain together.
    assert await hass.config_entries.async_setup(entries[0].entry_id)
    await hass.async_block_till_done()
    return entries


async def test_bug_room_not_started_is_not_reloaded(hass: HomeAssistant) -> None:
    """Entity without manager and not ready at the callback; manager attached later."""
    entity = _StartingEntity("vt1")
    hass.data["climate"] = _FakeClimateComponent([entity])
    with (
        patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01, create=True),
        patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 5, create=True),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload,
    ):
        await _start_with_entries(hass, ["vt1"])
        await hass.async_start()
        await asyncio.sleep(0.1)  # the room is still starting up
        entity.startup()  # manager attached by async_startup()
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert reload.call_count == 0, f"reloaded while is_ready was False: {reload.call_args_list}"


async def test_bug_sequential_rooms_are_not_reloaded(hass: HomeAssistant) -> None:
    """Three rooms start one after the other: none of them must be reloaded."""
    entities = [_StartingEntity(f"vt{i}") for i in (1, 2, 3)]
    hass.data["climate"] = _FakeClimateComponent(entities)
    with (
        patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01, create=True),
        patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 5, create=True),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload,
    ):
        await _start_with_entries(hass, ["vt1", "vt2", "vt3"])
        await hass.async_start()
        for entity in entities:
            await asyncio.sleep(0.1)
            entity.startup()
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert reload.call_count == 0, f"reloaded: {[c.args[0] for c in reload.call_args_list]}"


async def test_bug_entity_replaced_while_waiting_is_not_reloaded(hass: HomeAssistant) -> None:
    """The entity is rebuilt during the wait: the new one has its manager, no reload."""
    old = _StartingEntity("vt1")
    component = _FakeClimateComponent([old])
    hass.data["climate"] = component
    with (
        patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01, create=True),
        patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 5, create=True),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload,
    ):
        await _start_with_entries(hass, ["vt1"])
        await hass.async_start()
        await asyncio.sleep(0.1)
        new = _StartingEntity("vt1")
        new.startup()
        component.entities = [new]
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert reload.call_count == 0, f"reloaded the stale entity: {reload.call_args_list}"


async def test_bug_startup_timeout_is_not_reloaded(hass: HomeAssistant) -> None:
    """is_ready stays False past the (small) timeout: no reload."""
    entity = _StartingEntity("vt1")
    hass.data["climate"] = _FakeClimateComponent([entity])
    with (
        patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01, create=True),
        patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 0.1, create=True),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload,
    ):
        await _start_with_entries(hass, ["vt1"])
        await hass.async_start()
        await asyncio.sleep(0.4)
        await hass.async_block_till_done()
        assert reload.call_count == 0, f"reloaded although never ready: {reload.call_args_list}"


# --- Preservation: behaviors outside the bug condition (must pass before and after the fix) ---
# hypothesis is not available in the project: the state combinations are enumerated
# exhaustively with itertools.product / parametrize.

import itertools  # noqa: E402

import vtherm_api  # noqa: E402

from custom_components.vtherm_quirks.const import FACTORY_NAME  # noqa: E402

_VT_DOMAIN = "versatile_thermostat"


class _ReadyEntity:
    """Climate entity stub whose startup is finished (outside the bug condition)."""

    is_ready = True

    def __init__(self, unique_id: str, with_manager: bool) -> None:
        self.unique_id = unique_id
        self._manager = object() if with_manager else None

    def get_feature_manager(self, name: str):
        return self._manager


def _add_target_entry(hass: HomeAssistant, unique_id: str, domain: str | None, state: ConfigEntryState) -> None:
    """Add the targeted VTherm entry (domain None: no entry at all)."""
    if domain is None:
        return
    entry = MockConfigEntry(domain=domain, entry_id=unique_id, title=unique_id, data={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)


_TARGET_CASES = list(
    itertools.product(
        (True, False),  # climate entity present
        (True, False),  # manager present
        (_VT_DOMAIN, "other_domain", None),  # domain of the target entry (None: not found)
        (ConfigEntryState.LOADED, ConfigEntryState.NOT_LOADED),
    )
)


def _expected_reload(entity_present: bool, manager: bool, domain: str | None, state: ConfigEntryState) -> bool:
    return entity_present and not manager and domain == _VT_DOMAIN and state is ConfigEntryState.LOADED


@pytest.mark.parametrize(("entity_present", "manager", "domain", "state"), _TARGET_CASES)
async def test_preservation_setup_while_running(hass: HomeAssistant, entity_present: bool, manager: bool, domain: str | None, state: ConfigEntryState) -> None:
    """HA running, VTherm already ready: one reload of the target iff the manager is missing."""
    hass.data["climate"] = _FakeClimateComponent([_ReadyEntity("vt1", manager)] if entity_present else [])
    _add_target_entry(hass, "vt1", domain, state)
    entry = plugin_entry("vt1")
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        expected = ["vt1"] if _expected_reload(entity_present, manager, domain, state) else []
        assert [c.args[0] for c in reload.call_args_list] == expected


@pytest.mark.parametrize(("entity_present", "manager", "domain", "state"), _TARGET_CASES)
async def test_preservation_started_event_with_ready_vtherm(hass: HomeAssistant, entity_present: bool, manager: bool, domain: str | None, state: ConfigEntryState) -> None:
    """Entry set up before HA started, VTherm already ready at the started event."""
    hass.data["climate"] = _FakeClimateComponent([_ReadyEntity("vt1", manager)] if entity_present else [])
    _add_target_entry(hass, "vt1", domain, state)
    entry = plugin_entry("vt1")
    entry.add_to_hass(hass)
    hass.set_state(CoreState.not_running)
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert reload.call_count == 0  # nothing before the started event
        await hass.async_start()
        await hass.async_block_till_done()
        expected = ["vt1"] if _expected_reload(entity_present, manager, domain, state) else []
        assert [c.args[0] for c in reload.call_args_list] == expected


@pytest.mark.parametrize(
    ("stopping", "domain", "state", "manager"),
    list(
        itertools.product(
            (False, True),
            (_VT_DOMAIN, "other_domain", None),
            (ConfigEntryState.LOADED, ConfigEntryState.NOT_LOADED),
            (True, False),
        )
    ),
)
async def test_preservation_unload(hass: HomeAssistant, stopping: bool, domain: str | None, state: ConfigEntryState, manager: bool) -> None:
    """Unload reloads only the target, unconditionally, except while HA stops."""
    hass.data["climate"] = _FakeClimateComponent([_ReadyEntity("vt1", manager)])
    _add_target_entry(hass, "vt1", domain, state)
    _add_target_entry(hass, "vt2", _VT_DOMAIN, ConfigEntryState.LOADED)
    entry = plugin_entry("vt1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload:
        if stopping:
            hass.set_state(CoreState.stopping)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        expected = ["vt1"] if (not stopping and domain == _VT_DOMAIN and state is ConfigEntryState.LOADED) else []
        assert [c.args[0] for c in reload.call_args_list] == expected


@pytest.mark.parametrize(
    ("domain", "state"),
    [
        (_VT_DOMAIN, ConfigEntryState.LOADED),
        (_VT_DOMAIN, ConfigEntryState.NOT_LOADED),
        ("other_domain", ConfigEntryState.LOADED),
        (None, ConfigEntryState.LOADED),
    ],
)
async def test_preservation_quirks_update_reloads_only_the_target(hass: HomeAssistant, domain: str | None, state: ConfigEntryState) -> None:
    """A quirks modification reloads only the VTherm targeted by that entry."""
    hass.data["climate"] = _FakeClimateComponent([_ReadyEntity("vt1", True), _ReadyEntity("vt2", True)])
    _add_target_entry(hass, "vt1", domain, state)
    _add_target_entry(hass, "vt2", _VT_DOMAIN, ConfigEntryState.LOADED)
    first, second = plugin_entry("vt1"), plugin_entry("vt2")
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload:
        hass.config_entries.async_update_entry(first, data={**first.data, "quirks": {"climate.x": {"quirk": "unknown_quirk"}}})
        await hass.async_block_till_done()
        expected = ["vt1"] if (domain == _VT_DOMAIN and state is ConfigEntryState.LOADED) else []
        assert [c.args[0] for c in reload.call_args_list] == expected


@pytest.mark.parametrize("missing", ["__all__", *_REQUIRED_NAMES] if (_REQUIRED_NAMES := plugin._REQUIRED_API_NAMES) else [])
async def test_preservation_missing_hooks_refuse_the_load(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """Whatever required hook is absent, the entry is refused and the repair issue created."""
    names = _REQUIRED_NAMES if missing == "__all__" else (missing,)
    for name in names:
        monkeypatch.delattr(vtherm_api, name)
    assert not plugin.hooks_available()
    entry = plugin_entry("vt1")
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert reload.call_count == 0
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(DOMAIN, "hooks_unavailable") is not None
    assert VThermAPI.get_vtherm_api(hass).get_feature_manager(FACTORY_NAME) is None


@pytest.mark.parametrize(
    "unload_order",
    [order for n in (1, 2, 3) for order in itertools.permutations(range(n))],
)
async def test_preservation_factory_registered_once_removed_with_last_entry(hass: HomeAssistant, unload_order: tuple[int, ...]) -> None:
    """One factory for N entries, removed only when the last entry unloads."""
    count = len(unload_order)
    entries = []
    for i in range(count):
        add_vtherm_entry(hass, f"vt{i}", name=f"vt{i}").mock_state(hass, ConfigEntryState.LOADED)
        entry = plugin_entry(f"vt{i}")
        entry.add_to_hass(hass)
        entries.append(entry)
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)):
        assert await hass.config_entries.async_setup(entries[0].entry_id)
        await hass.async_block_till_done()
        api = VThermAPI.get_vtherm_api(hass)
        assert all(e.state is ConfigEntryState.LOADED for e in entries)
        assert [f.name for f in api.get_feature_manager_factories()] == [FACTORY_NAME]
        for position, index in enumerate(unload_order):
            assert await hass.config_entries.async_unload(entries[index].entry_id)
            await hass.async_block_till_done()
            last = position == count - 1
            assert (api.get_feature_manager(FACTORY_NAME) is None) is last


# --- Unit and integration tests of the startup wait and of the only_if_missing reload ---


async def test_wait_returns_immediately_when_ready(hass: HomeAssistant) -> None:
    """A VTherm already ready is returned without waiting."""
    entity = _ReadyEntity("vt1", False)
    hass.data["climate"] = _FakeClimateComponent([entity])
    _add_target_entry(hass, "vt1", _VT_DOMAIN, ConfigEntryState.LOADED)
    with patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 10):
        result = await asyncio.wait_for(plugin._async_wait_vtherm_started(hass, "vt1"), 1)
    assert result is entity


async def test_wait_returns_the_entity_once_ready(hass: HomeAssistant) -> None:
    """The wait polls until is_ready becomes true, then returns the entity."""
    entity = _StartingEntity("vt1")
    hass.data["climate"] = _FakeClimateComponent([entity])
    _add_target_entry(hass, "vt1", _VT_DOMAIN, ConfigEntryState.LOADED)
    with patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01), patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 5):
        task = asyncio.ensure_future(plugin._async_wait_vtherm_started(hass, "vt1"))
        await asyncio.sleep(0.1)
        assert not task.done()
        entity.startup()
        assert await asyncio.wait_for(task, 1) is entity


async def test_wait_returns_none_when_entity_not_found(hass: HomeAssistant) -> None:
    hass.data["climate"] = _FakeClimateComponent([])
    _add_target_entry(hass, "vt1", _VT_DOMAIN, ConfigEntryState.LOADED)
    assert await plugin._async_wait_vtherm_started(hass, "vt1") is None


async def test_wait_returns_none_when_entry_not_loaded(hass: HomeAssistant) -> None:
    hass.data["climate"] = _FakeClimateComponent([_StartingEntity("vt1")])
    _add_target_entry(hass, "vt1", _VT_DOMAIN, ConfigEntryState.NOT_LOADED)
    assert await plugin._async_wait_vtherm_started(hass, "vt1") is None


async def test_wait_returns_none_on_timeout(hass: HomeAssistant) -> None:
    hass.data["climate"] = _FakeClimateComponent([_StartingEntity("vt1")])
    _add_target_entry(hass, "vt1", _VT_DOMAIN, ConfigEntryState.LOADED)
    with patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01), patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 0.1):
        assert await plugin._async_wait_vtherm_started(hass, "vt1") is None


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("manager_present_after_wait", []),
        ("manager_missing_after_wait", ["vt1"]),
        ("entity_not_found", []),
    ],
)
async def test_reload_only_if_missing(hass: HomeAssistant, scenario: str, expected: list[str]) -> None:
    """_reload waits for the startup, then reloads only when the manager is still missing."""
    entity = _StartingEntity("vt1")
    hass.data["climate"] = _FakeClimateComponent([] if scenario == "entity_not_found" else [entity])
    _add_target_entry(hass, "vt1", _VT_DOMAIN, ConfigEntryState.LOADED)
    with (
        patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01),
        patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 5),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload,
    ):
        plugin._schedule_vtherm_reload(hass, "vt1", only_if_missing=True)
        await asyncio.sleep(0.1)
        assert reload.call_count == 0  # still waiting for the startup
        entity.startup(with_manager=scenario == "manager_present_after_wait")
        await asyncio.sleep(0.1)
        await hass.async_block_till_done()
        assert [c.args[0] for c in reload.call_args_list] == expected


async def test_wait_cancelled_at_stop_logs_no_error(hass: HomeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    """Cancelling the pending reload task (HA stop) ends it without a logged error."""
    hass.data["climate"] = _FakeClimateComponent([_StartingEntity("vt1")])
    _add_target_entry(hass, "vt1", _VT_DOMAIN, ConfigEntryState.LOADED)
    with (
        patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01),
        patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 60),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload,
    ):
        plugin._schedule_vtherm_reload(hass, "vt1", only_if_missing=True)
        await asyncio.sleep(0.1)
        tasks = [t for t in asyncio.all_tasks() if "vtherm_quirks reload vt1" in repr(t)]
        assert len(tasks) == 1
        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await hass.async_block_till_done()
        assert tasks[0].cancelled()
        assert reload.call_count == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_integration_several_vtherm_each_with_plugin_entry_no_reload(hass: HomeAssistant) -> None:
    """Rooms start one after the other, each already has its manager: no reload."""
    entities = [_StartingEntity(f"vt{i}") for i in (1, 2, 3)]
    hass.data["climate"] = _FakeClimateComponent(entities)
    with (
        patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01),
        patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 5),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload,
    ):
        await _start_with_entries(hass, ["vt1", "vt2", "vt3"])
        await hass.async_start()
        for entity in entities:
            await asyncio.sleep(0.05)
            entity.startup()
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert reload.call_count == 0


async def test_integration_plugin_entry_loaded_after_vtherm_reloads_once(hass: HomeAssistant) -> None:
    """The VTherm started without manager before the plugin entry: exactly one reload."""
    entities = [_StartingEntity("vt1"), _StartingEntity("vt2")]
    entities[0].startup(with_manager=False)  # started before the plugin entry
    hass.data["climate"] = _FakeClimateComponent(entities)
    with (
        patch.object(plugin, "_STARTUP_POLL_INTERVAL_SEC", 0.01),
        patch.object(plugin, "_STARTUP_WAIT_TIMEOUT_SEC", 5),
        patch.object(hass.config_entries, "async_reload", new=AsyncMock(return_value=True)) as reload,
    ):
        await _start_with_entries(hass, ["vt1", "vt2"])
        await hass.async_start()
        await asyncio.sleep(0.1)
        entities[1].startup()  # vt2 attaches its manager by itself
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert [c.args[0] for c in reload.call_args_list] == ["vt1"]

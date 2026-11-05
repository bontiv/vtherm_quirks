"""Tests of the setup and the unload of the integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
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
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload:
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

        def get_feature_manager(self, name: str):
            return None

    class FakeComponent:
        entities = [FakeEntity()]

    hass.data["climate"] = FakeComponent()
    vt1 = add_vtherm_entry(hass, "vt1", name="One")
    vt1.mock_state(hass, ConfigEntryState.LOADED)
    first = plugin_entry("vt1")
    first.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload:
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

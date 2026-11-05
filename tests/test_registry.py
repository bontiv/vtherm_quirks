"""Tests of the registry of quirks."""

from __future__ import annotations

import pytest
import voluptuous as vol

from custom_components.vtherm_quirks.quirks import (
    QUIRKS,
    BaseQuirk,
    compatible_quirks,
    detect_quirk,
    get_quirk,
    underlying_type_of,
    vtherm_scopes_of,
)
from custom_components.vtherm_quirks.quirks.sonoff_trv_zbt import SonoffTrvZbtQuirk

from .common import add_device, vtherm_entry_infos


def test_registry_has_no_duplicate_id() -> None:
    """Each quirk_id is unique."""
    ids = [quirk.quirk_id for quirk in QUIRKS]
    assert len(ids) == len(set(ids))
    assert "sonoff_trv_zbt" in ids


@pytest.mark.parametrize("quirk", QUIRKS, ids=lambda q: q.quirk_id)
def test_every_quirk_declares_its_contract(quirk: type[BaseQuirk]) -> None:
    """Every quirk declares its identity, roles and options."""
    assert issubclass(quirk, BaseQuirk)
    assert quirk.quirk_id and quirk.title
    assert quirk.underlying_types
    assert quirk.vtherm_scopes
    roles = quirk.entity_roles()
    assert roles
    assert any(role.required for role in roles)
    assert len({role.key for role in roles}) == len(roles)
    for role in roles:
        assert role.domains
    schema = quirk.options_schema()
    assert isinstance(schema, vol.Schema)
    # Every option has a default: an empty input is valid.
    quirk.resolve_options({})


@pytest.mark.parametrize("quirk", QUIRKS, ids=lambda q: q.quirk_id)
def test_quirks_do_not_define_hooks_they_do_not_need(quirk: type[BaseQuirk]) -> None:
    """The base class defines no hook: the manager forwards only defined hooks."""
    for hook in (
        "async_before_underlying_hvac_mode",
        "async_after_valve_command",
        "async_on_underlying_power",
    ):
        assert not hasattr(BaseQuirk, hook)


def test_get_quirk() -> None:
    """A quirk is found by id, an unknown id gives None."""
    assert get_quirk("sonoff_trv_zbt") is SonoffTrvZbtQuirk
    assert get_quirk("nope") is None
    assert get_quirk(None) is None


def test_scopes_and_underlying_type() -> None:
    """The scopes come from the VTherm configuration."""
    assert vtherm_scopes_of(vtherm_entry_infos()) == {"over_climate", "over_climate_valve"}
    assert vtherm_scopes_of(
        vtherm_entry_infos(auto_regulation_mode="auto_regulation_none")
    ) == {"over_climate"}
    assert vtherm_scopes_of({"thermostat_type": "thermostat_over_switch"}) == {"over_switch"}
    assert vtherm_scopes_of({"thermostat_type": "thermostat_over_valve"}) == {"over_valve"}
    assert vtherm_scopes_of({"thermostat_type": "thermostat_central_config"}) == frozenset()
    assert underlying_type_of(vtherm_entry_infos()) == "climate"
    assert underlying_type_of({"thermostat_type": "thermostat_over_switch"}) == "switch"
    assert underlying_type_of({}) is None


def test_compatible_quirks() -> None:
    """Only quirks matching the type and the scope are compatible."""
    assert compatible_quirks(vtherm_entry_infos()) == [SonoffTrvZbtQuirk]
    assert compatible_quirks(vtherm_entry_infos(auto_regulation_mode="auto_regulation_slow")) == []
    assert compatible_quirks({"thermostat_type": "thermostat_over_switch"}) == []


@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_detect_quirk(hass, kind: str) -> None:
    """The quirk of a device is detected when it is compatible with the VTherm."""
    from homeassistant.helpers import device_registry as dr

    add_device(hass, kind)
    device = next(iter(dr.async_get(hass).devices.values()))
    assert detect_quirk(device, vtherm_entry_infos()) is SonoffTrvZbtQuirk
    assert detect_quirk(device, vtherm_entry_infos(auto_regulation_mode="auto_regulation_none")) is None
    assert detect_quirk(None, vtherm_entry_infos()) is None

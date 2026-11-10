"""Tests of the registry of quirks."""

from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

import pytest
import voluptuous as vol

from custom_components.vtherm_quirks.quirks import (
    QUIRKS,
    _discover_quirks,
    BaseQuirk,
    compatible_quirks,
    detect_quirk,
    get_quirk,
    underlying_type_of,
    vtherm_scopes_of,
)
from custom_components.vtherm_quirks.quirks.moes_trv601 import MoesTrv601Quirk
from custom_components.vtherm_quirks.quirks.sonoff_trv_zbt import SonoffTrvZbtQuirk

from .common import add_device, device_of, vtherm_entry_infos


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
    assert compatible_quirks(vtherm_entry_infos()) == [MoesTrv601Quirk, SonoffTrvZbtQuirk]
    # Without direct valve control only the quirks of the plain over_climate remain.
    assert compatible_quirks(vtherm_entry_infos(auto_regulation_mode="auto_regulation_slow")) == [
        MoesTrv601Quirk
    ]
    assert compatible_quirks({"thermostat_type": "thermostat_over_switch"}) == []


@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_detect_quirk(hass, kind: str) -> None:
    """The quirk of a device is detected when it is compatible with the VTherm."""
    device = device_of(hass, add_device(hass, kind))
    assert detect_quirk(device, vtherm_entry_infos()) is SonoffTrvZbtQuirk
    assert detect_quirk(device, vtherm_entry_infos(auto_regulation_mode="auto_regulation_none")) is None
    assert detect_quirk(None, vtherm_entry_infos()) is None


QUIRK_SOURCE = """
from typing import ClassVar

from custom_components.vtherm_quirks.quirks.base import BaseQuirk


class {cls}(BaseQuirk):
    quirk_id: ClassVar[str] = "{quirk_id}"
    title: ClassVar[str] = "Fake"
    underlying_types: ClassVar[frozenset[str]] = frozenset({{"switch"}})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({{"over_switch"}})
"""


@pytest.fixture
def fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create a package of quirks under tmp_path and return a writer of modules."""
    package = tmp_path / "fakequirks"
    package.mkdir()
    (package / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    before = set(sys.modules)

    def write(name: str, source: str) -> None:
        (package / f"{name}.py").write_text(textwrap.dedent(source))

    yield write
    for module in set(sys.modules) - before:
        del sys.modules[module]


def discover() -> tuple[type[BaseQuirk], ...]:
    return _discover_quirks("fakequirks", [str(Path(sys.modules["fakequirks"].__file__).parent)])


def test_real_package_discovers_sonoff() -> None:
    """The real package gives the Sonoff quirk, sorted by id."""
    assert SonoffTrvZbtQuirk in QUIRKS
    assert [q.quirk_id for q in QUIRKS] == sorted(q.quirk_id for q in QUIRKS)
    assert BaseQuirk not in QUIRKS


def test_discovery_picks_up_a_module(fake_package) -> None:
    """A module defining a quirk is discovered, sorted by quirk_id."""
    fake_package("zeta", QUIRK_SOURCE.format(cls="ZetaQuirk", quirk_id="zeta"))
    fake_package("alpha", QUIRK_SOURCE.format(cls="AlphaQuirk", quirk_id="alpha"))
    import fakequirks  # noqa: F401

    assert [q.quirk_id for q in discover()] == ["alpha", "zeta"]


def test_discovery_ignores_imported_and_abstract_classes(fake_package) -> None:
    """Only classes defined in the module with a quirk_id count."""
    fake_package(
        "reuse",
        """
        from custom_components.vtherm_quirks.quirks.base import BaseQuirk
        from custom_components.vtherm_quirks.quirks.sonoff_trv_zbt import SonoffTrvZbtQuirk


        class AbstractQuirk(BaseQuirk):
            pass
        """,
    )
    import fakequirks  # noqa: F401

    assert discover() == ()


def test_discovery_skips_base_and_private_modules(fake_package) -> None:
    """The base module and the modules starting with _ are not imported."""
    fake_package("base", QUIRK_SOURCE.format(cls="BaseLike", quirk_id="base_like"))
    fake_package("_private", QUIRK_SOURCE.format(cls="PrivateQuirk", quirk_id="private"))
    fake_package("public", QUIRK_SOURCE.format(cls="PublicQuirk", quirk_id="public"))
    import fakequirks  # noqa: F401

    assert [q.quirk_id for q in discover()] == ["public"]
    assert "fakequirks.base" not in sys.modules
    assert "fakequirks._private" not in sys.modules


def test_discovery_skips_a_broken_module(fake_package, caplog: pytest.LogCaptureFixture) -> None:
    """A module failing to import is logged and the others are kept."""
    fake_package("broken", "raise RuntimeError('boom')")
    fake_package("good", QUIRK_SOURCE.format(cls="GoodQuirk", quirk_id="good"))
    import fakequirks  # noqa: F401

    with caplog.at_level(logging.ERROR):
        found = discover()

    assert [q.quirk_id for q in found] == ["good"]
    assert any("fakequirks.broken" in r.getMessage() and r.exc_info for r in caplog.records)


def test_discovery_keeps_the_first_duplicate(fake_package, caplog: pytest.LogCaptureFixture) -> None:
    """Two quirks with the same id: an error is logged and the first one is kept."""
    fake_package("first", QUIRK_SOURCE.format(cls="FirstQuirk", quirk_id="same"))
    fake_package("second", QUIRK_SOURCE.format(cls="SecondQuirk", quirk_id="same"))
    import fakequirks  # noqa: F401

    with caplog.at_level(logging.ERROR):
        found = discover()

    assert [q.__name__ for q in found] == ["FirstQuirk"]
    assert any("SecondQuirk" in r.getMessage() for r in caplog.records)


async def test_detect_quirk_warns_when_several_match(
    hass, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The first quirk wins and a warning names every matching quirk."""
    from custom_components.vtherm_quirks import quirks as quirks_module

    class OtherQuirk(SonoffTrvZbtQuirk):
        quirk_id = "other"

    monkeypatch.setattr(quirks_module, "QUIRKS", (SonoffTrvZbtQuirk, OtherQuirk))
    device = device_of(hass, add_device(hass, "z2m"))
    with caplog.at_level(logging.WARNING):
        assert detect_quirk(device, vtherm_entry_infos()) is SonoffTrvZbtQuirk
    assert any("sonoff_trv_zbt" in r.getMessage() and "other" in r.getMessage() for r in caplog.records)

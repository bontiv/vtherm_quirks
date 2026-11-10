"""Tests of the manager, the factory and the repairs."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry
from vtherm_api import (
    UNSUPPORTED_HVAC_MODE,
    CalibrationOffsetRequest,
    InterfaceCalibrationOffsetModifier,
    InterfaceFeatureManager,
    InterfaceUnderlyingHvacModeMapper,
    InterfaceValveWriteHook,
    ValveWriteDecision,
    ValveWriteRequest,
    InterfaceUnderlyingHvacModeHook,
    InterfaceUnderlyingPowerHook,
    InterfaceValveCommandHook,
    PowerCommandKind,
    UnderlyingPowerCommand,
    ValveCommand,
    ValveCommandCause,
)

import custom_components.vtherm_quirks.quirks as quirks_module
from custom_components.vtherm_quirks.const import DOMAIN
from custom_components.vtherm_quirks.factory import QuirksManagerFactory
from custom_components.vtherm_quirks.manager import QuirksManager, find_config_entry
from custom_components.vtherm_quirks.quirks import BaseQuirk, EntityRole, QuirkIssue

from .common import FakeRuntime, add_vtherm_entry, vtherm_entry_infos

A = "climate.a"
B = "climate.b"


class RecordingQuirk(BaseQuirk):
    """Defines the three hooks and records the calls."""

    quirk_id: ClassVar[str] = "fake_all"
    title: ClassVar[str] = "Fake all hooks"
    underlying_types: ClassVar[frozenset[str]] = frozenset({"climate"})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({"over_climate"})
    issues: ClassVar[list[QuirkIssue]] = []

    @classmethod
    def entity_roles(cls) -> list[EntityRole]:
        return [EntityRole("main", ("sensor",))]

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.calls: list[tuple] = []
        self.started = 0
        self.stopped = 0

    async def async_start(self) -> None:
        self.started += 1

    async def async_stop(self) -> None:
        self.stopped += 1

    def check_configuration(self) -> list[QuirkIssue]:
        return list(type(self).issues)

    async def async_before_underlying_hvac_mode(self, *args: Any) -> None:
        self.calls.append(("hvac", *args))

    async def async_after_valve_command(self, command: ValveCommand) -> None:
        self.calls.append(("valve", command))

    async def async_on_underlying_power(self, command: UnderlyingPowerCommand) -> None:
        self.calls.append(("power", command))


class PowerOnlyQuirk(BaseQuirk):
    """Only defines the power hook."""

    quirk_id: ClassVar[str] = "fake_power"
    title: ClassVar[str] = "Fake power"
    underlying_types: ClassVar[frozenset[str]] = frozenset({"climate"})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({"over_climate"})

    @classmethod
    def entity_roles(cls) -> list[EntityRole]:
        return [EntityRole("main", ("sensor",))]

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.calls: list[tuple] = []

    async def async_on_underlying_power(self, command: UnderlyingPowerCommand) -> None:
        self.calls.append(("power", command))


class ContractQuirk(BaseQuirk):
    """Defines the four optional contracts of vtherm_api 0.10.0 and records the calls."""

    quirk_id: ClassVar[str] = "fake_contracts"
    title: ClassVar[str] = "Fake contracts"
    underlying_types: ClassVar[frozenset[str]] = frozenset({"climate"})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({"over_climate"})

    @classmethod
    def entity_roles(cls) -> list[EntityRole]:
        return [EntityRole("main", ("sensor",))]

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.calls: list[tuple] = []

    async def async_before_valve_write(self, request: ValveWriteRequest) -> ValveWriteDecision | None:
        self.calls.append(("write", request))
        return ValveWriteDecision(opening_degree=7, handled=True, settle_sec=12.0)

    def to_underlying_hvac_mode(self, climate_entity_id: str, hvac_mode: str) -> str | None:
        self.calls.append(("to", climate_entity_id, hvac_mode))
        return "auto" if hvac_mode == "heat" else None

    def from_underlying_hvac_mode(self, climate_entity_id: str, underlying_hvac_mode: str) -> str | None:
        self.calls.append(("from", climate_entity_id, underlying_hvac_mode))
        return {"auto": "heat", "heat": UNSUPPORTED_HVAC_MODE}.get(underlying_hvac_mode)

    def adjust_calibration_offset(self, request: CalibrationOffsetRequest) -> float | None:
        self.calls.append(("offset", request))
        return -request.offset


class BoomContractQuirk(ContractQuirk):
    """Every contract method raises."""

    quirk_id: ClassVar[str] = "fake_contracts_boom"

    async def async_before_valve_write(self, request: ValveWriteRequest) -> ValveWriteDecision | None:
        raise RuntimeError("boom")

    def to_underlying_hvac_mode(self, climate_entity_id: str, hvac_mode: str) -> str | None:
        raise RuntimeError("boom")

    def from_underlying_hvac_mode(self, climate_entity_id: str, underlying_hvac_mode: str) -> str | None:
        raise RuntimeError("boom")

    def adjust_calibration_offset(self, request: CalibrationOffsetRequest) -> float | None:
        raise RuntimeError("boom")


class BoomQuirk(RecordingQuirk):
    """Every hook raises."""

    quirk_id: ClassVar[str] = "fake_boom"

    async def async_before_underlying_hvac_mode(self, *args: Any) -> None:
        raise RuntimeError("boom")

    async def async_after_valve_command(self, command: ValveCommand) -> None:
        raise RuntimeError("boom")

    async def async_on_underlying_power(self, command: UnderlyingPowerCommand) -> None:
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def fake_quirks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the fake quirks next to the real ones."""
    monkeypatch.setattr(
        quirks_module,
        "QUIRKS",
        (RecordingQuirk, PowerOnlyQuirk, BoomQuirk, ContractQuirk, BoomContractQuirk),
    )
    RecordingQuirk.issues = []


def valve_command(climate: str) -> ValveCommand:
    return ValveCommand(
        climate_entity_id=climate,
        opening_degree_entity_id="number.o",
        closing_degree_entity_id=None,
        percent_open=50,
        opening_degree=50,
        closing_degree=None,
        hvac_mode="heat",
        cause=ValveCommandCause.REGULATION,
    )


def power_command(underlying: str, climate: str | None = None) -> UnderlyingPowerCommand:
    return UnderlyingPowerCommand(
        underlying_entity_id=underlying,
        underlying_type="valve_regulation" if climate else "switch",
        climate_entity_id=climate,
        hvac_mode="heat",
        kind=PowerCommandKind.UPDATE,
        requested_on_percent=0.5,
        applied_on_percent=0.5,
        on_time_sec=30,
        off_time_sec=30,
        cycle_duration_sec=60,
        start_offset_sec=None,
        is_repeat=False,
        cycle_start_monotonic=None,
        monotonic=0.0,
    )


async def make_manager(
    hass: HomeAssistant, quirks_data: dict[str, dict], **vtherm: Any
) -> tuple[QuirksManager, FakeRuntime, MockConfigEntry]:
    """Set up a plugin entry and build the manager like the core does."""
    add_vtherm_entry(hass, **vtherm)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Salon",
        unique_id="vtherm_salon",
        data={"target_vtherm_unique_id": "vtherm_salon", "quirks": quirks_data},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = FakeRuntime(hass, vtherm_entry_infos(**vtherm))
    factory = QuirksManagerFactory()
    assert factory.supports(runtime)
    manager = factory.create(runtime)
    manager.post_init(runtime.entry_infos)
    return manager, runtime, entry


def conf(quirk: str, main: str | None = "sensor.x") -> dict:
    return {"quirk": quirk, "entities": {"main": main} if main else {}, "options": {}}


async def test_manager_satisfies_the_contracts(hass: HomeAssistant) -> None:
    """The manager is a feature manager implementing the three hooks."""
    manager, _, _ = await make_manager(hass, {A: conf("fake_all")})
    assert isinstance(manager, InterfaceFeatureManager)
    assert isinstance(manager, InterfaceUnderlyingHvacModeHook)
    assert isinstance(manager, InterfaceValveCommandHook)
    assert isinstance(manager, InterfaceUnderlyingPowerHook)
    assert manager.name == "vtherm_quirks"
    assert manager.is_configured
    assert manager.is_detected


async def test_hooks_reach_only_the_quirk_of_the_underlying(hass: HomeAssistant) -> None:
    """Each hook is forwarded to the quirk bound to the concerned underlying only."""
    manager, _, _ = await make_manager(
        hass, {A: conf("fake_all"), B: conf("fake_all")}
    )
    quirk_a, quirk_b = manager.quirks[A], manager.quirks[B]

    await manager.async_before_underlying_hvac_mode(A, "off", "heat", None)
    command = valve_command(B)
    await manager.async_after_valve_command(command)
    await manager.async_on_underlying_power(power_command("switch.s", A))

    assert quirk_a.calls == [
        ("hvac", A, "off", "heat", None),
        ("power", power_command("switch.s", A)),
    ]
    assert quirk_b.calls == [("valve", command)]


async def test_power_hook_matches_underlying_entity(hass: HomeAssistant) -> None:
    """The power hook finds the quirk by underlying entity id."""
    manager, _, _ = await make_manager(hass, {"sensor.self": conf("fake_all")})
    command = power_command("sensor.self")
    await manager.async_on_underlying_power(command)
    assert manager.quirks["sensor.self"].calls == [("power", command)]


async def test_quirk_without_the_hook_is_not_called(hass: HomeAssistant) -> None:
    """A hook is forwarded only to a quirk class that defines it."""
    manager, _, _ = await make_manager(hass, {A: conf("fake_power")})
    quirk = manager.quirks[A]
    assert not hasattr(type(quirk), "async_after_valve_command")
    await manager.async_after_valve_command(valve_command(A))
    await manager.async_before_underlying_hvac_mode(A, "off", "heat", None)
    assert quirk.calls == []
    await manager.async_on_underlying_power(power_command("switch.s", A))
    assert len(quirk.calls) == 1


async def test_a_failing_quirk_does_not_disturb_the_others(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The exception of a quirk is logged with its id and swallowed."""
    manager, _, _ = await make_manager(
        hass, {A: conf("fake_boom"), B: conf("fake_all")}
    )
    await manager.async_before_underlying_hvac_mode(A, "off", "heat", None)
    await manager.async_after_valve_command(valve_command(A))
    await manager.async_on_underlying_power(power_command("x", A))
    assert "fake_boom" in caplog.text
    await manager.async_before_underlying_hvac_mode(B, "off", "heat", None)
    assert manager.quirks[B].calls == [("hvac", B, "off", "heat", None)]


async def test_unconfigured_quirk_is_not_called(hass: HomeAssistant) -> None:
    """A quirk missing a required entity receives nothing and the manager is not configured."""
    manager, _, _ = await make_manager(hass, {A: conf("fake_all", main=None)})
    assert not manager.is_configured
    await manager.async_before_underlying_hvac_mode(A, "off", "heat", None)
    assert manager.quirks[A].calls == []


async def test_start_and_stop_quirks(hass: HomeAssistant) -> None:
    """start_listening starts every quirk, stop_listening stops them."""
    manager, _, _ = await make_manager(hass, {A: conf("fake_all"), B: conf("fake_power")})
    await manager.start_listening()
    assert manager.quirks[A].started == 1
    assert manager.stop_listening() is True
    await hass.async_block_till_done(wait_background_tasks=True)
    assert manager.quirks[A].stopped == 1


async def test_issues_are_reported_and_cleared(hass: HomeAssistant) -> None:
    """check_configuration problems appear in the repairs and disappear when fixed."""
    manager, runtime, _ = await make_manager(hass, {A: conf("fake_all")})
    registry = ir.async_get(hass)
    RecordingQuirk.issues = [QuirkIssue("sync_device_internal_temp")]
    await manager.start_listening()
    issue_id = f"{runtime.unique_id}_{A}_sync_device_internal_temp"
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "sync_device_internal_temp"
    assert issue.translation_placeholders["name"] == "Salon"
    assert issue.translation_placeholders["underlying"] == A

    RecordingQuirk.issues = []
    assert await manager.refresh_state() is False
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    RecordingQuirk.issues = [QuirkIssue("open_window_enabled")]
    await manager.refresh_state()
    assert registry.async_get_issue(DOMAIN, f"{runtime.unique_id}_{A}_open_window_enabled")
    manager.stop_listening()
    assert registry.async_get_issue(DOMAIN, f"{runtime.unique_id}_{A}_open_window_enabled") is None


async def test_unknown_quirk_is_ignored_and_reported(hass: HomeAssistant) -> None:
    """A quirk removed from the plugin is ignored by the manager and reported."""
    add_vtherm_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="vtherm_salon",
        title="Salon",
        data={
            "target_vtherm_unique_id": "vtherm_salon",
            "quirks": {A: conf("fake_all"), B: conf("ghost")},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{entry.entry_id}_{B}_unknown_quirk"
    )
    assert issue is not None
    assert issue.translation_placeholders["quirk"] == "ghost"

    runtime = FakeRuntime(hass)
    manager = QuirksManagerFactory().create(runtime)
    manager.post_init(runtime.entry_infos)
    assert set(manager.quirks) == {A}


async def test_factory_supports(hass: HomeAssistant) -> None:
    """supports is true only for a VTherm with an entry and a compatible quirk."""
    manager, runtime, entry = await make_manager(hass, {A: conf("fake_all")})
    factory = QuirksManagerFactory()
    assert factory.name == "vtherm_quirks"
    assert factory.supports(runtime)
    # Another VTherm has no entry.
    other = FakeRuntime(hass, unique_id="other")
    assert not factory.supports(other)
    # Incompatible VTherm type.
    switch = FakeRuntime(hass, {"thermostat_type": "thermostat_over_switch"})
    assert not factory.supports(switch)
    # Not loaded entry: ignored.
    await hass.config_entries.async_unload(entry.entry_id)
    assert find_config_entry(hass, "vtherm_salon") is None
    assert not factory.supports(runtime)


async def test_quirk_can_request_an_issue_sync(hass: HomeAssistant) -> None:
    """request_issue_sync updates the repairs without a cycle, and stops after stop_listening."""
    manager, runtime, _ = await make_manager(hass, {A: conf("fake_all")})
    registry = ir.async_get(hass)
    await manager.start_listening()
    quirk = manager.quirks[A]
    issue_id = f"{runtime.unique_id}_{A}_open_window_enabled"

    RecordingQuirk.issues = [QuirkIssue("open_window_enabled")]
    quirk.request_issue_sync()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    RecordingQuirk.issues = []
    quirk.request_issue_sync()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    manager.stop_listening()
    RecordingQuirk.issues = [QuirkIssue("open_window_enabled")]
    quirk.request_issue_sync()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


# ---------------------------------------------------------------------------
# Optional contracts of vtherm_api 0.10.0
# ---------------------------------------------------------------------------
CONTRACT_METHODS = (
    "async_before_valve_write",
    "to_underlying_hvac_mode",
    "from_underlying_hvac_mode",
    "adjust_calibration_offset",
)


def write_request(climate: str, opening: int = 0) -> ValveWriteRequest:
    return ValveWriteRequest(
        climate_entity_id=climate,
        opening_degree_entity_id="number.o",
        closing_degree_entity_id=None,
        percent_open=opening,
        opening_degree=opening,
        requested_opening_degree=opening,
        max_opening_degree=100,
        hvac_mode="heat",
        cause=ValveCommandCause.REGULATION,
    )


def offset_request(climate: str, offset: float = 1.5) -> CalibrationOffsetRequest:
    return CalibrationOffsetRequest(
        climate_entity_id=climate,
        calibration_entity_id="number.c",
        offset=offset,
        requested_offset=offset,
        current_offset=0.0,
        local_temperature=20.0,
        room_temperature=21.0,
        min_offset=-9.0,
        max_offset=9.0,
        step=0.1,
    )


async def test_manager_satisfies_the_optional_contracts(hass: HomeAssistant) -> None:
    manager, _, _ = await make_manager(hass, {A: conf("fake_contracts")})
    assert isinstance(manager, InterfaceValveWriteHook)
    assert isinstance(manager, InterfaceUnderlyingHvacModeMapper)
    assert isinstance(manager, InterfaceCalibrationOffsetModifier)


async def test_contracts_reach_only_the_quirk_of_the_climate(hass: HomeAssistant) -> None:
    """Each method is forwarded to the quirk of the climate concerned, and its answer returned."""
    manager, _, _ = await make_manager(hass, {A: conf("fake_contracts"), B: conf("fake_contracts")})
    quirk_a, quirk_b = manager.quirks[A], manager.quirks[B]

    request = write_request(A)
    assert await manager.async_before_valve_write(request) == ValveWriteDecision(
        opening_degree=7, handled=True, settle_sec=12.0
    )
    assert manager.to_underlying_hvac_mode(B, "heat") == "auto"
    assert manager.to_underlying_hvac_mode(B, "cool") is None
    assert manager.from_underlying_hvac_mode(B, "auto") == "heat"
    assert manager.from_underlying_hvac_mode(B, "heat") == UNSUPPORTED_HVAC_MODE
    offset = offset_request(A)
    assert manager.adjust_calibration_offset(offset) == -1.5

    assert quirk_a.calls == [("write", request), ("offset", offset)]
    assert quirk_b.calls == [
        ("to", B, "heat"),
        ("to", B, "cool"),
        ("from", B, "auto"),
        ("from", B, "heat"),
    ]


async def test_contracts_without_quirk_return_none(hass: HomeAssistant) -> None:
    """An unknown climate, or an unconfigured quirk, leaves the core unchanged."""
    manager, _, _ = await make_manager(hass, {A: conf("fake_contracts"), B: conf("fake_contracts", main=None)})
    for climate in ("climate.unknown", B):
        assert await manager.async_before_valve_write(write_request(climate)) is None
        assert manager.to_underlying_hvac_mode(climate, "heat") is None
        assert manager.from_underlying_hvac_mode(climate, "auto") is None
        assert manager.adjust_calibration_offset(offset_request(climate)) is None
    assert manager.quirks[B].calls == []


async def test_contracts_ignore_a_class_without_the_method(hass: HomeAssistant) -> None:
    """A quirk class that does not define the methods is not called."""
    manager, _, _ = await make_manager(hass, {A: conf("fake_power")})
    quirk = manager.quirks[A]
    for name in CONTRACT_METHODS:
        assert not hasattr(type(quirk), name)
    assert await manager.async_before_valve_write(write_request(A)) is None
    assert manager.to_underlying_hvac_mode(A, "heat") is None
    assert manager.from_underlying_hvac_mode(A, "auto") is None
    assert manager.adjust_calibration_offset(offset_request(A)) is None
    assert quirk.calls == []


async def test_a_failing_contract_method_is_logged_and_gives_none(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    manager, _, _ = await make_manager(hass, {A: conf("fake_contracts_boom")})
    assert await manager.async_before_valve_write(write_request(A)) is None
    assert manager.to_underlying_hvac_mode(A, "heat") is None
    assert manager.from_underlying_hvac_mode(A, "auto") is None
    assert manager.adjust_calibration_offset(offset_request(A)) is None
    for name in CONTRACT_METHODS:
        assert f"fake_contracts_boom failed in {name}" in caplog.text

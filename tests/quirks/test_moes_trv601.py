"""Tests of the Moes TRV801 / Tuya TRV601 quirk."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtherm_quirks.const import DOMAIN
from custom_components.vtherm_quirks.manager import QuirksManager
from custom_components.vtherm_quirks.quirks import QUIRKS, detect_quirk
from custom_components.vtherm_quirks.quirks.moes_trv601 import (
    MAX_CORRECTIONS,
    RESEND_DELAY,
    MoesTrv601Quirk,
)
from custom_components.vtherm_quirks.quirks.sonoff_trv_zbt import SonoffTrvZbtQuirk

from ..common import (
    CLIMATE,
    VTHERM_ENTRY_ID,
    FakeRuntime,
    add_device,
    add_vtherm_entry,
    flush,
    vtherm_entry_infos,
)


class PresetSim:
    """A simulated TRV: a climate state with a preset and a recording service."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        hass.services.async_register("climate", "set_preset_mode", self._set_preset_mode)

    async def _set_preset_mode(self, call: ServiceCall) -> None:
        assert call.data["entity_id"] == CLIMATE
        if self.gate is not None:
            await self.gate.wait()
        self.calls.append(call.data["preset_mode"])

    def set(self, preset: str | None, hvac: str = "heat") -> None:
        """Publish a state of the TRV, as Zigbee2MQTT does."""
        attrs: dict[str, Any] = {"temperature": 20.0}
        if preset is not None:
            attrs["preset_mode"] = preset
        self.hass.states.async_set(CLIMATE, hvac, attrs)


class Clock:
    """Controllable replacement of the quirk clock."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def pass_delay(self) -> None:
        self.value += RESEND_DELAY + 1


def make_quirk(
    hass: HomeAssistant, mode: str | None = "heat", **options: Any
) -> tuple[MoesTrv601Quirk, PresetSim, Clock]:
    sim = PresetSim(hass)
    runtime = FakeRuntime(hass)
    runtime.vtherm_hvac_mode = mode
    quirk = MoesTrv601Quirk(
        hass, runtime, CLIMATE, {}, MoesTrv601Quirk.resolve_options(options)
    )
    clock = Clock()
    quirk._now = clock  # type: ignore[method-assign]
    return quirk, sim, clock


def make_device(
    hass: HomeAssistant,
    model: str | None = None,
    model_id: str | None = None,
    manufacturer: str | None = None,
    key: str = "dev",
) -> DeviceEntry:
    config_entry = MockConfigEntry(domain="mqtt")
    config_entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("mqtt", key)},
        manufacturer=manufacturer,
        model=model,
        model_id=model_id,
    )


async def fight(sim: PresetSim, clock: Clock, times: int) -> None:
    """The TRV leaves manual for on ``times`` times and is put back each time."""
    for _ in range(times):
        clock.pass_delay()
        sim.set("on")
        await flush()
        sim.set("manual")
        await flush()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "model,model_id,manufacturer",
    [
        ("TRV601", None, "Tuya"),
        ("Thermostatic radiator valve (TRV601)", None, "Tuya"),
        ("TRV801_1", None, "Moes"),
        (None, "TRV801_1", "Moes"),
        ("SBDV-00185", None, "Sber"),
        (None, "SBDV-00185", "Sber"),
        ("Radiator valve", "_TZE204_rtrmfadk", None),
        ("Radiator valve", None, "_TZE204_rtrmfadk"),
        ("Radiator valve", None, "_TZE204_cvcu2p6e"),
        ("Radiator valve", "_TZE204_CVCU2P6E", None),
    ],
)
async def test_match_device(
    hass: HomeAssistant, model: str | None, model_id: str | None, manufacturer: str | None
) -> None:
    """The three model names and the two fingerprints are recognized."""
    device = make_device(hass, model, model_id, manufacturer)
    assert MoesTrv601Quirk.match_device(device)
    assert not SonoffTrvZbtQuirk.match_device(device)


@pytest.mark.parametrize(
    "model,model_id,manufacturer",
    [
        ("TS0601", "TS0601", "_TZE200_ckud7u2l"),
        ("Thermostat", "TS0601", "_TZE204_other"),
        ("Zigbee thermostatic radiator valve (TRV-ZBT)", None, "SONOFF"),
        ("TRV-ZBT", "TRV-ZBT", "SONOFF"),
        (None, None, None),
    ],
)
async def test_match_device_rejects_other_devices(
    hass: HomeAssistant, model: str | None, model_id: str | None, manufacturer: str | None
) -> None:
    """Another TS0601 and the TRV-ZBT are not a TRV601."""
    assert not MoesTrv601Quirk.match_device(make_device(hass, model, model_id, manufacturer))


async def test_discovery_and_detection(hass: HomeAssistant) -> None:
    """The registry finds the quirk and detects one quirk per device."""
    assert MoesTrv601Quirk in QUIRKS
    moes = make_device(hass, "TRV801_1", manufacturer="_TZE204_rtrmfadk", key="moes")
    sonoff = make_device(hass, "TRV-ZBT", manufacturer="SONOFF", key="sonoff")
    no_valve = vtherm_entry_infos(auto_regulation_mode="auto_regulation_none")
    assert detect_quirk(moes, vtherm_entry_infos()) is MoesTrv601Quirk
    assert detect_quirk(moes, no_valve) is MoesTrv601Quirk
    assert detect_quirk(sonoff, vtherm_entry_infos()) is SonoffTrvZbtQuirk
    # Without direct valve control the TRV-ZBT quirk is not compatible.
    assert detect_quirk(sonoff, no_valve) is None


def test_roles_and_options() -> None:
    """No entity role, one option that defaults to False."""
    assert MoesTrv601Quirk.entity_roles() == []
    assert MoesTrv601Quirk.resolve_options({}) == {"keep_schedule": False}
    assert MoesTrv601Quirk.resolve_options({"keep_schedule": True}) == {"keep_schedule": True}
    quirk = MoesTrv601Quirk(None, None, CLIMATE, {}, {})  # type: ignore[arg-type]
    assert quirk.is_configured


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset", ["on", "auto", "off"])
async def test_hook_heat_sets_manual(hass: HomeAssistant, preset: str) -> None:
    """Heat: one set_preset_mode(manual) when the preset is not manual."""
    quirk, sim, _ = make_quirk(hass)
    sim.set(preset, hvac="off" if preset == "off" else "heat")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    await flush()
    assert sim.calls == ["manual"]


async def test_hook_heat_manual_does_nothing(hass: HomeAssistant) -> None:
    """Heat with the preset already manual: no call."""
    quirk, sim, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "heat", None)
    await flush()
    assert sim.calls == []


async def test_hook_heat_keep_schedule(hass: HomeAssistant) -> None:
    """With keep_schedule, auto is accepted, on is still corrected."""
    quirk, sim, _ = make_quirk(hass, keep_schedule=True)
    sim.set("auto")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "heat", None)
    await flush()
    assert sim.calls == []
    sim.set("on")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "heat", None)
    await flush()
    assert sim.calls == ["manual"]


@pytest.mark.parametrize("preset,calls", [("manual", ["off"]), ("on", ["off"]), ("off", [])])
async def test_hook_off(hass: HomeAssistant, preset: str, calls: list[str]) -> None:
    """Off: set_preset_mode(off) unless the preset is already off."""
    quirk, sim, _ = make_quirk(hass, mode="off")
    sim.set(preset)
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    await flush()
    assert sim.calls == calls


async def test_hook_does_not_wait_for_the_service(hass: HomeAssistant) -> None:
    """The hook returns while the service call is still running."""
    quirk, sim, _ = make_quirk(hass)
    sim.gate = asyncio.Event()
    sim.set("on")
    await asyncio.wait_for(
        quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None), 1
    )
    await flush()
    assert sim.calls == []
    sim.gate.set()
    await flush()
    assert sim.calls == ["manual"]


async def test_hook_ignores_other_climates(hass: HomeAssistant) -> None:
    """A hook for another climate does nothing."""
    quirk, sim, _ = make_quirk(hass)
    sim.set("on")
    await quirk.async_before_underlying_hvac_mode("climate.other", "heat", "off", None)
    await flush()
    assert sim.calls == []


async def test_hook_does_not_call_an_unavailable_trv(hass: HomeAssistant) -> None:
    """An unavailable or unknown TRV is left alone."""
    quirk, sim, _ = make_quirk(hass)
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    sim.set("on", hvac="unavailable")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    await flush()
    assert sim.calls == []


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
async def test_state_change_corrects_on_to_manual(hass: HomeAssistant) -> None:
    """A TRV forced open while VTherm heats goes back to manual."""
    quirk, sim, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    sim.set("on")
    await flush()
    assert sim.calls == ["manual"]
    await quirk.async_stop()


async def test_state_change_corrects_auto_unless_keep_schedule(hass: HomeAssistant) -> None:
    """auto is corrected by default, accepted with keep_schedule."""
    quirk, sim, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    sim.set("auto")
    await flush()
    assert sim.calls == ["manual"]
    await quirk.async_stop()

    hass.services.async_remove("climate", "set_preset_mode")
    quirk, sim, _ = make_quirk(hass, keep_schedule=True)
    await quirk.async_start()
    sim.set("auto")
    await flush()
    assert sim.calls == []
    await quirk.async_stop()


async def test_state_change_corrects_to_off_when_vtherm_is_off(hass: HomeAssistant) -> None:
    """A TRV that is not in the off preset while VTherm is off is turned off."""
    quirk, sim, _ = make_quirk(hass, mode="off", keep_schedule=True)
    sim.set("off", hvac="off")
    await quirk.async_start()
    sim.set("auto")
    await flush()
    assert sim.calls == ["off"]
    await quirk.async_stop()


async def test_no_correction_while_unavailable(hass: HomeAssistant) -> None:
    """An unavailable TRV is not corrected, its return is."""
    quirk, sim, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    sim.set("on", hvac="unavailable")
    await flush()
    assert sim.calls == []
    sim.set("on")
    await flush()
    assert sim.calls == ["manual"]
    await quirk.async_stop()


async def test_no_double_correction_while_the_trv_answers(hass: HomeAssistant) -> None:
    """Other state changes before the TRV reports its new preset do not resend."""
    quirk, sim, clock = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    sim.set("on")
    await flush()
    hass.states.async_set(CLIMATE, "heat", {"preset_mode": "on", "temperature": 21.0})
    await flush()
    assert sim.calls == ["manual"]
    clock.pass_delay()
    hass.states.async_set(CLIMATE, "heat", {"preset_mode": "on", "temperature": 22.0})
    await flush()
    assert sim.calls == ["manual", "manual"]
    await quirk.async_stop()


async def test_stops_after_max_corrections_and_raises_issue(hass: HomeAssistant) -> None:
    """After 3 corrections in a row the issue is raised and nothing is sent anymore."""
    quirk, sim, clock = make_quirk(hass)
    syncs: list[None] = []
    quirk.issue_sync_callback = lambda: syncs.append(None)
    sim.set("manual")
    await quirk.async_start()
    await fight(sim, clock, MAX_CORRECTIONS)
    assert sim.calls == ["manual"] * MAX_CORRECTIONS
    assert quirk.check_configuration() == []

    clock.pass_delay()
    sim.set("on")
    await flush()
    assert sim.calls == ["manual"] * MAX_CORRECTIONS
    issues = quirk.check_configuration()
    assert [issue.key for issue in issues] == ["preset_keeps_changing"]
    assert issues[0].placeholders == {"preset": "manual", "current": "on"}
    assert len(syncs) == 1

    clock.pass_delay()
    sim.set("auto")
    await flush()
    assert sim.calls == ["manual"] * MAX_CORRECTIONS
    await quirk.async_stop()


async def test_counter_resets_when_vtherm_changes_mode(hass: HomeAssistant) -> None:
    """A new mode of VTherm clears the issue and allows new corrections."""
    quirk, sim, clock = make_quirk(hass)
    syncs: list[None] = []
    quirk.issue_sync_callback = lambda: syncs.append(None)
    sim.set("manual")
    await quirk.async_start()
    await fight(sim, clock, MAX_CORRECTIONS)
    clock.pass_delay()
    sim.set("on")
    await flush()
    assert quirk.check_configuration()
    assert len(syncs) == 1
    sim.calls.clear()

    # VTherm goes off: the issue is cleared as soon as the hook is called.
    quirk.runtime.vtherm_hvac_mode = "off"
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    await flush()
    assert sim.calls == ["off"]
    assert quirk.check_configuration() == []
    assert len(syncs) == 2

    # Back to heat: three new corrections are possible.
    quirk.runtime.vtherm_hvac_mode = "heat"
    sim.set("manual")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    sim.calls.clear()
    await fight(sim, clock, MAX_CORRECTIONS)
    assert sim.calls == ["manual"] * MAX_CORRECTIONS
    await quirk.async_stop()


async def test_counter_resets_without_hook(hass: HomeAssistant) -> None:
    """A change of the VTherm mode seen only in the runtime also resets the counter."""
    quirk, sim, clock = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    await fight(sim, clock, MAX_CORRECTIONS)
    clock.pass_delay()
    sim.set("on")
    await flush()
    assert quirk.check_configuration()
    quirk.runtime.vtherm_hvac_mode = "off"
    sim.set("manual")
    await flush()
    assert sim.calls[-1] == "off"
    assert quirk.check_configuration() == []
    await quirk.async_stop()


async def test_underlying_off_while_vtherm_heats_is_not_fought(hass: HomeAssistant) -> None:
    """The core can turn the underlying off (window) while VTherm stays in heat."""
    quirk, sim, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    await flush()
    assert sim.calls == ["off"]
    sim.set("off", hvac="off")
    await flush()
    assert sim.calls == ["off"]

    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    await flush()
    assert sim.calls == ["off", "manual"]
    await quirk.async_stop()


async def test_unknown_vtherm_mode_is_not_enforced(hass: HomeAssistant) -> None:
    """Without a mode from VTherm nothing is corrected."""
    quirk, sim, _ = make_quirk(hass, mode=None)
    sim.set("on")
    await quirk.async_start()
    sim.set("auto")
    await flush()
    assert sim.calls == []
    await quirk.async_stop()


async def test_stop_unsubscribes(hass: HomeAssistant) -> None:
    """After async_stop the state changes are ignored."""
    quirk, sim, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    await quirk.async_stop()
    sim.set("on")
    await flush()
    assert sim.calls == []


# ---------------------------------------------------------------------------
# Outdated converter
# ---------------------------------------------------------------------------
async def test_outdated_converter(hass: HomeAssistant) -> None:
    """Preset manual with the state auto reports the converter, without correction."""
    quirk, sim, _ = make_quirk(hass)
    syncs: list[None] = []
    quirk.issue_sync_callback = lambda: syncs.append(None)
    sim.set("manual", hvac="heat")
    await quirk.async_start()
    assert quirk.check_configuration() == []

    sim.set("manual", hvac="auto")
    await flush()
    issues = quirk.check_configuration()
    assert [(i.key, i.severity) for i in issues] == [
        ("outdated_converter", ir.IssueSeverity.ERROR)
    ]
    assert len(syncs) == 1
    assert sim.calls == []

    sim.set("manual", hvac="heat")
    await flush()
    assert quirk.check_configuration() == []
    assert len(syncs) == 2
    await quirk.async_stop()


async def test_outdated_converter_reaches_the_repairs(hass: HomeAssistant) -> None:
    """With the manager, the issue appears and disappears without waiting for a cycle."""
    add_vtherm_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "target_vtherm_unique_id": VTHERM_ENTRY_ID,
            "quirks": {CLIMATE: {"quirk": "moes_trv601", "entities": {}, "options": {}}},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sim = PresetSim(hass)
    sim.set("manual", hvac="auto")
    runtime = FakeRuntime(hass)
    runtime.vtherm_hvac_mode = "heat"
    manager = QuirksManager(runtime)  # type: ignore[arg-type]
    manager.post_init(runtime.entry_infos)
    assert manager.quirks[CLIMATE].quirk_id == "moes_trv601"
    await manager.start_listening()
    issue_id = f"{runtime.unique_id}_{CLIMATE}_outdated_converter"
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR

    sim.set("manual", hvac="heat")
    await flush()
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    manager.stop_listening()
    await flush()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
async def test_startup_corrects_forced_preset(hass: HomeAssistant) -> None:
    """At startup, a preset on while VTherm heats is corrected."""
    quirk, sim, _ = make_quirk(hass)
    sim.set("on")
    await quirk.async_start()
    await flush()
    assert sim.calls == ["manual"]
    await quirk.async_stop()


async def test_startup_waits_for_home_assistant_started(hass: HomeAssistant) -> None:
    """Before Home Assistant is running, the check waits for the start event."""
    hass.set_state(CoreState.not_running)
    quirk, sim, _ = make_quirk(hass)
    sim.set("on")
    await quirk.async_start()
    await flush()
    assert sim.calls == []
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await flush()
    assert sim.calls == ["manual"]
    await quirk.async_stop()


async def test_startup_leaves_a_good_preset(hass: HomeAssistant) -> None:
    """At startup, a good preset is left alone."""
    quirk, sim, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    await flush()
    assert sim.calls == []
    await quirk.async_stop()


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------
async def test_config_flow_without_entity_step(hass: HomeAssistant) -> None:
    """A quirk without entity role goes from the quirk step to the options."""
    device = make_device(hass, "TRV801_1", manufacturer="_TZE204_rtrmfadk", key="flow")
    climate = add_device(hass, "z2m")["climate"]
    er.async_get(hass).async_update_entity(climate, device_id=device.id)
    add_vtherm_entry(hass, underlying_entity_ids=[climate])

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_vtherm_unique_id": VTHERM_ENTRY_ID}
    )
    assert result["data_schema"]({}) == {"quirk": "moes_trv601"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"quirk": "moes_trv601"}
    )
    assert result["step_id"] == "options"
    assert result["data_schema"]({}) == {"keep_schedule": False}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"keep_schedule": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["quirks"] == {
        climate: {"quirk": "moes_trv601", "entities": {}, "options": {"keep_schedule": True}}
    }

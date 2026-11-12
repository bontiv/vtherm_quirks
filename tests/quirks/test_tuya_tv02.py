"""Tests of the Tuya TV02-Zigbee quirk."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vtherm_quirks.quirks import detect_quirk
from custom_components.vtherm_quirks.quirks._tuya_preset import (
    MAX_CORRECTIONS,
    RESEND_DELAY,
)
from custom_components.vtherm_quirks.quirks.moes_trv601 import MoesTrv601Quirk
from custom_components.vtherm_quirks.quirks.tuya_tv02 import TuyaTv02Quirk

from ..common import CLIMATE, VTHERM_ENTITY_ID, FakeRuntime, flush, vtherm_entry_infos

Z2M_PRESETS = ["manual", "auto", "holiday"]
ZHA_PRESETS = ["none", "schedule", "holiday", "frost protect"]


class Tv02Sim:
    """A simulated TV02: a climate with a preset, recording services."""

    def __init__(self, hass: HomeAssistant, presets: list[str] | None = None) -> None:
        self.hass = hass
        self.presets = presets or Z2M_PRESETS
        self.calls: list[tuple] = []
        hass.services.async_register("climate", "set_preset_mode", self._set_preset_mode)
        hass.services.async_register("climate", "set_temperature", self._set_temperature)

    async def _set_preset_mode(self, call: ServiceCall) -> None:
        assert call.data["entity_id"] == CLIMATE
        self.calls.append(("preset", call.data["preset_mode"]))

    async def _set_temperature(self, call: ServiceCall) -> None:
        assert call.data["entity_id"] == CLIMATE
        self.calls.append(("temperature", call.data["temperature"]))

    def set(
        self,
        preset: str | None,
        hvac: str = "heat",
        temperature: float = 20.0,
        **attrs: Any,
    ) -> None:
        """Publish a state of the valve."""
        attributes: dict[str, Any] = {
            "temperature": temperature,
            "preset_modes": self.presets,
            "min_temp": 5.0,
            "max_temp": 30.0,
        }
        attributes.update(attrs)
        if preset is not None:
            attributes["preset_mode"] = preset
        self.hass.states.async_set(CLIMATE, hvac, attributes)

    def presets_sent(self) -> list[str]:
        return [value for kind, value in self.calls if kind == "preset"]

    def temperatures_sent(self) -> list[float]:
        return [value for kind, value in self.calls if kind == "temperature"]


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def pass_delay(self) -> None:
        self.value += RESEND_DELAY + 1


def make_quirk(
    hass: HomeAssistant,
    mode: str | None = "heat",
    presets: list[str] | None = None,
    entities: dict | None = None,
    **options: Any,
) -> tuple[TuyaTv02Quirk, Tv02Sim, Clock, FakeRuntime]:
    sim = Tv02Sim(hass, presets)
    runtime = FakeRuntime(hass)
    runtime.vtherm_hvac_mode = mode
    quirk = TuyaTv02Quirk(
        hass, runtime, CLIMATE, entities or {}, TuyaTv02Quirk.resolve_options(options)
    )
    clock = Clock()
    quirk._now = clock  # type: ignore[method-assign]
    return quirk, sim, clock, runtime


def set_vtherm(hass: HomeAssistant, last_sent: float | None = 19.5, follow: bool = False) -> None:
    """Publish the VTherm state with its over_climate attributes."""
    hass.states.async_set(
        VTHERM_ENTITY_ID,
        "heat",
        {
            "vtherm_over_climate": {
                "follow_underlying_temp_change": follow,
                "underlying_temperature_tracking": {
                    "underlyings": {CLIMATE: {"last_sent_temperature": last_sent}}
                },
            }
        },
    )


def make_device(
    hass: HomeAssistant,
    model: str | None = None,
    model_id: str | None = None,
    manufacturer: str | None = None,
    domain: str = "mqtt",
) -> DeviceEntry:
    config_entry = MockConfigEntry(domain=domain)
    config_entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(domain, "dev")},
        manufacturer=manufacturer,
        model=model,
        model_id=model_id,
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "model,model_id,manufacturer,domain",
    [
        ("Thermostatic radiator valve", "TV02-Zigbee", "Tuya", "mqtt"),
        ("Thermostatic radiator valve (TV01-ZB)", None, "Moes", "mqtt"),
        ("TV06", None, "Tuya", "mqtt"),
        ("TS0601", None, "_TZE200_hue3yfsn", "zha"),
        ("TS0601", "_TZE200_e9ba97vf", "Tuya", "zha"),
    ],
)
async def test_match_device(hass: HomeAssistant, model, model_id, manufacturer, domain) -> None:
    device = make_device(hass, model, model_id, manufacturer, domain)
    assert TuyaTv02Quirk.match_device(device)
    assert not MoesTrv601Quirk.match_device(device)


@pytest.mark.parametrize(
    "model,model_id,manufacturer",
    [
        ("TRV601", None, "Tuya"),
        ("TS0601", None, "_TZE204_rtrmfadk"),
        ("TS0601", None, "_TZE200_aaaaaaaa"),
        ("ATV06", None, "Tuya"),
        ("TV06X", None, "Tuya"),
        ("Radiator valve", None, "_TZE200_hue3yfsn"),  # fingerprint without TS0601
        (None, None, None),
    ],
)
async def test_match_device_rejects_other_models(hass: HomeAssistant, model, model_id, manufacturer) -> None:
    assert not TuyaTv02Quirk.match_device(make_device(hass, model, model_id, manufacturer))


async def test_detect_quirk(hass: HomeAssistant) -> None:
    device = make_device(hass, "Thermostatic radiator valve", "TV02-Zigbee", "Tuya")
    infos = vtherm_entry_infos(auto_regulation_mode="auto_regulation_slow")
    assert detect_quirk(device, infos) is TuyaTv02Quirk


def test_roles_and_options() -> None:
    roles = TuyaTv02Quirk.entity_roles()
    assert [(role.key, role.required) for role in roles] == [("open_window", False)]
    assert TuyaTv02Quirk.resolve_options({}) == {"keep_schedule": False, "restore_setpoint": True}


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------
async def test_hook_heat_sends_manual(hass: HomeAssistant) -> None:
    quirk, sim, _, _ = make_quirk(hass)
    sim.set("auto")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "heat", None)
    await flush()
    assert sim.presets_sent() == ["manual"]


async def test_hook_heat_sends_none_under_zha(hass: HomeAssistant) -> None:
    quirk, sim, _, _ = make_quirk(hass, presets=ZHA_PRESETS)
    sim.set("schedule")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "heat", None)
    await flush()
    assert sim.presets_sent() == ["none"]


async def test_hook_leaves_an_off_valve(hass: HomeAssistant) -> None:
    """Off -> heat: the valve is still off, the state tracking corrects it later."""
    quirk, sim, _, _ = make_quirk(hass)
    sim.set("auto", hvac="off")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "off", None)
    await flush()
    assert sim.calls == []
    await quirk.async_start()
    sim.set("auto", hvac="heat")
    await flush()
    assert sim.presets_sent() == ["manual"]


async def test_hook_off_sends_nothing(hass: HomeAssistant) -> None:
    quirk, sim, _, _ = make_quirk(hass)
    sim.set("auto")
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "off", "heat", None)
    await flush()
    assert sim.calls == []


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("preset", ["auto", "holiday"])
async def test_correction(hass: HomeAssistant, preset: str) -> None:
    quirk, sim, _, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    sim.set(preset)
    await flush()
    assert sim.presets_sent() == ["manual"]


async def test_keep_schedule(hass: HomeAssistant) -> None:
    quirk, sim, clock, _ = make_quirk(hass, keep_schedule=True)
    sim.set("manual")
    await quirk.async_start()
    sim.set("auto")
    await flush()
    assert sim.calls == []
    clock.pass_delay()
    sim.set("holiday")
    await flush()
    assert sim.presets_sent() == ["manual"]


async def test_nothing_while_vtherm_or_valve_off(hass: HomeAssistant) -> None:
    quirk, sim, _, runtime = make_quirk(hass, mode="off")
    sim.set("manual")
    await quirk.async_start()
    sim.set("auto", hvac="off")
    await flush()
    assert sim.calls == []
    # VTherm heats but the core turned the valve off (window).
    runtime.vtherm_hvac_mode = "heat"
    sim.set("auto", hvac="off", temperature=19.0)
    await flush()
    assert sim.calls == []


async def test_corrections_stop_and_report(hass: HomeAssistant) -> None:
    quirk, sim, clock, runtime = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    for _ in range(MAX_CORRECTIONS):
        clock.pass_delay()
        sim.set("auto")
        await flush()
        sim.set("manual")
        await flush()
    assert sim.presets_sent() == ["manual"] * MAX_CORRECTIONS
    clock.pass_delay()
    sim.set("auto")
    await flush()
    assert len(sim.presets_sent()) == MAX_CORRECTIONS
    keys = [issue.key for issue in quirk.check_configuration()]
    assert "preset_keeps_changing" in keys

    # A new mode of VTherm starts the corrections again.
    runtime.vtherm_hvac_mode = "off"
    sim.set("auto", temperature=18.0)  # a new state, so that it is tracked
    await flush()
    assert "preset_keeps_changing" not in [issue.key for issue in quirk.check_configuration()]


async def test_unavailable_valve(hass: HomeAssistant) -> None:
    quirk, sim, _, _ = make_quirk(hass)
    hass.states.async_set(CLIMATE, "unavailable", {})
    await quirk.async_start()
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", None, None)
    await flush()
    assert sim.calls == []


# ---------------------------------------------------------------------------
# Setpoint restore
# ---------------------------------------------------------------------------
async def test_restore_setpoint_after_schedule(hass: HomeAssistant) -> None:
    """Back from the schedule, the setpoint VTherm sent is written once."""
    set_vtherm(hass, last_sent=19.5)
    quirk, sim, _, _ = make_quirk(hass)
    sim.set("manual", temperature=19.5)
    await quirk.async_start()
    sim.set("auto", temperature=17.0)
    await flush()
    sim.set("manual", temperature=17.0)
    await flush()
    assert sim.calls == [("preset", "manual"), ("temperature", 19.5)]
    sim.set("manual", temperature=17.0)
    await flush()
    assert sim.temperatures_sent() == [19.5]


async def test_restore_from_the_hook(hass: HomeAssistant) -> None:
    set_vtherm(hass, last_sent=19.5)
    quirk, sim, _, _ = make_quirk(hass)
    sim.set("auto", temperature=17.0)
    await quirk.async_before_underlying_hvac_mode(CLIMATE, "heat", "heat", None)
    await quirk.async_start()
    sim.set("manual", temperature=17.0)
    await flush()
    assert sim.temperatures_sent() == [19.5]


async def test_no_restore_when_setpoint_equal(hass: HomeAssistant) -> None:
    set_vtherm(hass, last_sent=19.5)
    quirk, sim, _, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    sim.set("auto", temperature=19.6)
    await flush()
    sim.set("manual", temperature=19.6)
    await flush()
    assert sim.temperatures_sent() == []


@pytest.mark.parametrize("options", [{"restore_setpoint": False}, {"keep_schedule": True}])
async def test_no_restore_with_options(hass: HomeAssistant, options: dict) -> None:
    set_vtherm(hass, last_sent=19.5)
    quirk, sim, _, _ = make_quirk(hass, **options)
    sim.set("manual")
    await quirk.async_start()
    sim.set("holiday", temperature=12.0)
    await flush()
    sim.set("manual", temperature=12.0)
    await flush()
    assert sim.temperatures_sent() == []


async def test_restore_fallback_and_clamp(hass: HomeAssistant) -> None:
    """Without the VTherm attribute the regulated temperature is used, clamped to the valve range."""
    quirk, sim, _, runtime = make_quirk(hass)
    runtime.regulated_target_temperature = 32.0
    sim.set("manual")
    await quirk.async_start()
    sim.set("auto", temperature=17.0)
    await flush()
    sim.set("manual", temperature=17.0)
    await flush()
    assert sim.temperatures_sent() == [30.0]


async def test_stop_during_restore(hass: HomeAssistant) -> None:
    set_vtherm(hass, last_sent=19.5)
    quirk, sim, _, _ = make_quirk(hass)
    sim.set("manual")
    await quirk.async_start()
    sim.set("auto", temperature=17.0)
    sim.set("manual", temperature=17.0)
    await quirk.async_stop()
    await flush()
    assert sim.temperatures_sent() in ([], [19.5])


# ---------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------
async def test_follow_underlying_issue(hass: HomeAssistant) -> None:
    quirk, _, _, _ = make_quirk(hass)
    assert quirk.check_configuration() == []
    set_vtherm(hass, follow=True)
    assert [issue.key for issue in quirk.check_configuration()] == ["follow_underlying_enabled"]
    set_vtherm(hass, follow=False)
    assert quirk.check_configuration() == []


async def test_open_window_issue(hass: HomeAssistant) -> None:
    quirk, _, _, _ = make_quirk(hass, entities={"open_window": "switch.tv02_window"})
    hass.states.async_set("switch.tv02_window", "on")
    issues = quirk.check_configuration()
    assert [(issue.key, dict(issue.placeholders)) for issue in issues] == [
        ("open_window_enabled", {"entity": "switch.tv02_window"})
    ]

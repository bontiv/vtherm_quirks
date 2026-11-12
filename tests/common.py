"""Test helpers: fake runtime, simulated TRV-ZBT, simulated devices."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

CLIMATE = "climate.trv"
HEATING = "number.trv_heating_valve_position"
IDLE = "number.trv_idle_valve_position"
EXTERNAL = "number.trv_external_temperature_input"
SENSOR_Z2M = "select.trv_temperature_sensor_select"
SENSOR_ZHA = "switch.trv_external_temperature_sensor"

VTHERM_ENTRY_ID = "vtherm_salon"
VTHERM_ENTITY_ID = "climate.salon"


def vtherm_entry_infos(**overrides: Any) -> dict[str, Any]:
    """Configuration of an over_climate VTherm with direct valve control."""
    infos = {
        "name": "Salon",
        "thermostat_type": "thermostat_over_climate",
        "auto_regulation_mode": "auto_regulation_valve",
        "underlying_entity_ids": [CLIMATE],
        "opening_degree_entity_ids": [HEATING],
        "closing_degree_entity_ids": [],
        "sync_device_internal_temp": False,
    }
    infos.update(overrides)
    return infos


class FakeRuntime:
    """The part of InterfaceThermostatRuntime used by the plugin."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_infos: dict[str, Any] | None = None,
        unique_id: str = VTHERM_ENTRY_ID,
        name: str = "Salon",
    ) -> None:
        self.hass = hass
        self.entry_infos = entry_infos if entry_infos is not None else vtherm_entry_infos()
        self.unique_id = unique_id
        self.name = name
        self.entity_id = VTHERM_ENTITY_ID


@dataclass
class TrvSim:
    """A simulated TRV-ZBT: states and recording services.

    ``kind`` is ``z2m`` (a select) or ``zha`` (a switch) for the sensor entity.
    """

    hass: HomeAssistant
    kind: str = "z2m"
    calls: list[tuple] = field(default_factory=list)

    @property
    def sensor(self) -> str:
        return SENSOR_Z2M if self.kind == "z2m" else SENSOR_ZHA

    @property
    def entities(self) -> dict[str, str]:
        return {
            "heating_position": HEATING,
            "idle_position": IDLE,
            "external_temperature": EXTERNAL,
            "sensor_select": self.sensor,
        }

    def setup(
        self,
        hvac: str = "heat",
        setpoint: float | None = 20.0,
        idle: float = 0,
        external: float = 21.5,
    ) -> TrvSim:
        attrs = {} if setpoint is None else {"temperature": setpoint}
        self.hass.states.async_set(CLIMATE, hvac, attrs)
        self.hass.states.async_set(HEATING, "0")
        self.hass.states.async_set(IDLE, str(idle))
        self.hass.states.async_set(EXTERNAL, str(external))
        if self.kind == "z2m":
            self.hass.states.async_set(SENSOR_Z2M, "local_temperature")
        else:
            self.hass.states.async_set(SENSOR_ZHA, "off")

        async def set_value(call: ServiceCall) -> None:
            entity_id = call.data["entity_id"]
            self.calls.append(("number", entity_id, call.data["value"]))
            self.hass.states.async_set(entity_id, str(call.data["value"]))

        async def select_option(call: ServiceCall) -> None:
            self.calls.append(("select", call.data["entity_id"], call.data["option"]))
            self.hass.states.async_set(call.data["entity_id"], call.data["option"])

        async def turn_on(call: ServiceCall) -> None:
            self.calls.append(("switch_on", call.data["entity_id"]))
            self.hass.states.async_set(call.data["entity_id"], "on")

        async def turn_off(call: ServiceCall) -> None:
            self.calls.append(("switch_off", call.data["entity_id"]))
            self.hass.states.async_set(call.data["entity_id"], "off")

        self.hass.services.async_register("number", "set_value", set_value)
        self.hass.services.async_register("select", "select_option", select_option)
        self.hass.services.async_register("switch", "turn_on", turn_on)
        self.hass.services.async_register("switch", "turn_off", turn_off)
        return self

    def set_hvac(self, hvac: str, setpoint: float | None = 20.0) -> None:
        attrs = {} if setpoint is None else {"temperature": setpoint}
        self.hass.states.async_set(CLIMATE, hvac, attrs)

    @property
    def external_on(self) -> tuple:
        """The call that selects the external temperature sensor."""
        if self.kind == "z2m":
            return ("select", SENSOR_Z2M, "remote_temperature")
        return ("switch_on", SENSOR_ZHA)

    @property
    def external_off(self) -> tuple:
        """The call that selects the built-in temperature sensor."""
        if self.kind == "z2m":
            return ("select", SENSOR_Z2M, "local_temperature")
        return ("switch_off", SENSOR_ZHA)

    def clear(self) -> None:
        self.calls.clear()


async def flush(count: int = 40) -> None:
    """Let the background tasks run."""
    for _ in range(count):
        await asyncio.sleep(0)


async def advance(hass: HomeAssistant, seconds: float) -> None:
    """Fire the timers due in ``seconds`` seconds and let the tasks run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await flush()


def add_vtherm_entry(
    hass: HomeAssistant,
    entry_id: str = VTHERM_ENTRY_ID,
    **overrides: Any,
) -> MockConfigEntry:
    """Add a Versatile Thermostat config entry (not set up)."""
    entry = MockConfigEntry(
        domain="versatile_thermostat",
        entry_id=entry_id,
        title=overrides.get("name", "Salon"),
        data=vtherm_entry_infos(**overrides),
    )
    entry.add_to_hass(hass)
    return entry


def add_device(
    hass: HomeAssistant,
    kind: str,
    climate_entity_id: str = CLIMATE,
    ieee: str = "0x00124b0001",
) -> dict[str, str]:
    """Register a TRV-ZBT device and its entities in the registries.

    ``kind`` is ``z2m`` or ``zha``. The climate entity is registered under
    ``climate_entity_id``. Returns the entity ids by role.
    """
    platform = "mqtt" if kind == "z2m" else "zha"
    config_entry = MockConfigEntry(domain=platform)
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(platform, ieee)},
        manufacturer="SONOFF",
        model="Zigbee thermostatic radiator valve (TRV-ZBT)"
        if kind == "z2m"
        else "TRV-ZBT",
    )
    registry = er.async_get(hass)

    def register(domain: str, unique_id: str, object_id: str, **kwargs) -> str:
        return registry.async_get_or_create(
            domain,
            platform,
            unique_id,
            config_entry=config_entry,
            device_id=device.id,
            suggested_object_id=object_id,
            **kwargs,
        ).entity_id

    found: dict[str, str] = {}
    if kind == "z2m":
        found["climate"] = register("climate", f"{ieee}_climate_zigbee2mqtt", "trv")
        found["heating_position"] = register(
            "number", f"{ieee}_heating_valve_position_zigbee2mqtt", "trv_heating_valve_position"
        )
        found["idle_position"] = register(
            "number", f"{ieee}_idle_valve_position_zigbee2mqtt", "trv_idle_valve_position"
        )
        found["external_temperature"] = register(
            "number",
            f"{ieee}_external_temperature_input_zigbee2mqtt",
            "trv_external_temperature_input",
        )
        found["sensor_select"] = register(
            "select",
            f"{ieee}_temperature_sensor_select_zigbee2mqtt",
            "trv_temperature_sensor_select",
        )
        found["adaptive_control"] = register(
            "switch", f"{ieee}_smart_temperature_control_zigbee2mqtt", "trv_smart_temperature_control"
        )
        found["open_window"] = register(
            "switch", f"{ieee}_open_window_zigbee2mqtt", "trv_open_window"
        )
    else:
        # ZHA: the valve entities are disabled by default.
        disabled = er.RegistryEntryDisabler.INTEGRATION
        found["climate"] = register("climate", f"{ieee}-1-513", "trv", translation_key="thermostat")
        found["heating_position"] = register(
            "number",
            f"{ieee}-1-64529-valve_opening_degree",
            "trv_valve_opening_degree",
            translation_key="valve_opening_degree",
            disabled_by=disabled,
        )
        found["idle_position"] = register(
            "number",
            f"{ieee}-1-64529-valve_closing_degree",
            "trv_valve_closing_degree",
            translation_key="valve_closing_degree",
            disabled_by=disabled,
        )
        found["external_temperature"] = register(
            "number",
            f"{ieee}-1-64529-external_temperature_sensor_value",
            "trv_external_temperature_sensor_value",
            translation_key="external_temperature_sensor_value",
        )
        found["sensor_select"] = register(
            "switch",
            f"{ieee}-1-64529-external_temperature_sensor_enable",
            "trv_external_temperature_sensor",
            translation_key="external_temperature_sensor",
        )
        found["adaptive_control"] = register(
            "switch", f"{ieee}-1-64529-temperature_control_mode", "trv_adaptive_mode",
            translation_key="adaptive_mode",
        )
        found["open_window"] = register(
            "switch", f"{ieee}-1-64529-open_window", "trv_open_window",
            translation_key="open_window",
        )
    return found


TRVZB_OPENING = "number.trvzb_valve_opening_degree"
TRVZB_CLOSING = "number.trvzb_valve_closing_degree"


class TrvzbSim:
    """A simulated Sonoff TRVZB: the two valve numbers and a recording service."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple] = []
        self.fail_values: set[float] = set()

    entities: ClassVar[dict[str, str]] = {
        "opening_degree": TRVZB_OPENING,
        "closing_degree": TRVZB_CLOSING,
    }

    def setup(self, opening: str = "0", closing: str = "100", hvac: str = "heat") -> TrvzbSim:
        self.hass.states.async_set(CLIMATE, hvac, {"temperature": 20.0})
        self.hass.states.async_set(TRVZB_OPENING, opening)
        self.hass.states.async_set(TRVZB_CLOSING, closing)

        async def set_value(call: ServiceCall) -> None:
            entity_id = call.data["entity_id"]
            value = call.data["value"]
            self.calls.append(("number", entity_id, value))
            if value in self.fail_values:
                raise ValueError(f"refused {value}")
            self.hass.states.async_set(entity_id, str(value))

        self.hass.services.async_register("number", "set_value", set_value)
        return self

    def opening(self, value: float) -> tuple:
        return ("number", TRVZB_OPENING, value)

    def closing(self, value: float) -> tuple:
        return ("number", TRVZB_CLOSING, value)

    def clear(self) -> None:
        self.calls.clear()


def add_trvzb_device(
    hass: HomeAssistant,
    kind: str,
    climate_entity_id: str = CLIMATE,
    ieee: str = "0x00124b0002",
) -> dict[str, str]:
    """Register a Sonoff TRVZB and its entities. ``kind`` is ``z2m`` or ``zha``."""
    platform = "mqtt" if kind == "z2m" else "zha"
    config_entry = MockConfigEntry(domain=platform)
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(platform, ieee)},
        manufacturer="SONOFF",
        model="Zigbee thermostatic radiator valve" if kind == "z2m" else "TRVZB",
        model_id="TRVZB" if kind == "z2m" else None,
    )
    registry = er.async_get(hass)

    def register(domain: str, unique_id: str, object_id: str, **kwargs) -> str:
        return registry.async_get_or_create(
            domain,
            platform,
            unique_id,
            config_entry=config_entry,
            device_id=device.id,
            suggested_object_id=object_id,
            **kwargs,
        ).entity_id

    found: dict[str, str] = {}
    if kind == "z2m":
        found["climate"] = register("climate", f"{ieee}_climate_zigbee2mqtt", "trvzb")
        found["opening_degree"] = register(
            "number", f"{ieee}_valve_opening_degree_zigbee2mqtt", "trvzb_valve_opening_degree"
        )
        found["closing_degree"] = register(
            "number", f"{ieee}_valve_closing_degree_zigbee2mqtt", "trvzb_valve_closing_degree"
        )
        found["adaptive_control"] = register(
            "switch", f"{ieee}_smart_temperature_control_zigbee2mqtt", "trvzb_smart_temperature_control"
        )
        found["open_window"] = register("switch", f"{ieee}_open_window_zigbee2mqtt", "trvzb_open_window")
    else:
        disabled = er.RegistryEntryDisabler.INTEGRATION
        found["climate"] = register("climate", f"{ieee}-1-513", "trvzb", translation_key="thermostat")
        found["opening_degree"] = register(
            "number",
            f"{ieee}-1-64529-valve_opening_degree",
            "trvzb_valve_opening_degree",
            translation_key="valve_opening_degree",
            disabled_by=disabled,
        )
        found["closing_degree"] = register(
            "number",
            f"{ieee}-1-64529-valve_closing_degree",
            "trvzb_valve_closing_degree",
            translation_key="valve_closing_degree",
            disabled_by=disabled,
        )
        found["adaptive_control"] = register(
            "switch", f"{ieee}-1-64529-temperature_control_mode", "trvzb_adaptive_mode",
            translation_key="adaptive_mode",
        )
        found["open_window"] = register(
            "switch", f"{ieee}-1-64529-open_window", "trvzb_open_window",
            translation_key="open_window",
        )
    return found


def device_of(hass: HomeAssistant, entities: dict[str, str]) -> dr.DeviceEntry:
    """Return the device registered by ``add_device``, found from its climate entity."""
    device_id = er.async_get(hass).async_get(entities["climate"]).device_id
    device = dr.async_get(hass).async_get(device_id)
    assert device is not None
    return device

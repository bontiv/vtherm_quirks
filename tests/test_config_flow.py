"""Tests of the config flow and the options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vtherm_quirks.const import DOMAIN

from .common import VTHERM_ENTRY_ID, add_device, add_vtherm_entry


def suggested(schema, key: str):
    """Suggested value of a field of a form schema."""
    for marker in schema.schema:
        if marker == key:
            return (marker.description or {}).get("suggested_value")
    raise KeyError(key)


def field_keys(schema) -> list[str]:
    return [str(marker) for marker in schema.schema]


async def start_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


@pytest.mark.parametrize("kind", ["z2m", "zha"])
async def test_full_flow_with_detected_quirk(hass: HomeAssistant, kind: str) -> None:
    """The detected quirk is proposed, the entities are prefilled, the entry is created."""
    found = add_device(hass, kind)
    add_vtherm_entry(hass, underlying_entity_ids=[found["climate"]])

    result = await start_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_vtherm_unique_id": VTHERM_ENTRY_ID}
    )

    # Quirk step: the detected quirk is the default, the choices are compatible ones only.
    assert result["step_id"] == "quirk"
    schema = result["data_schema"]
    assert schema({}) == {"quirk": "sonoff_trv_zbt"}
    selector_config = next(iter(schema.schema.values())).config
    assert list(selector_config["options"]) == [
        "none",
        "eurotronic_spzb0001",
        "moes_trv601",
        "sonoff_trv_zbt",
        "sonoff_trvzb",
        "tuya_tv02",
    ]
    assert result["description_placeholders"]["detected"] == "Sonoff TRV-ZBT"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"quirk": "sonoff_trv_zbt"}
    )

    # Entities step: prefilled by the discovery.
    assert result["step_id"] == "entities"
    schema = result["data_schema"]
    for role in ("heating_position", "idle_position", "external_temperature", "sensor_select"):
        assert suggested(schema, role) == found[role]
    entities = {
        role: found[role]
        for role in ("heating_position", "idle_position", "external_temperature", "sensor_select")
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], entities)

    # Options step with the defaults of the blueprint.
    assert result["step_id"] == "options"
    assert result["data_schema"]({}) == {
        "temperature_source": "local",
        "delta": 2.0,
        "pulse_delay": 5,
        "hot_step": False,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"temperature_source": "remote", "delta": 3.0, "pulse_delay": 8, "hot_step": True},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Salon"
    assert result["data"] == {
        "target_vtherm_unique_id": VTHERM_ENTRY_ID,
        "quirks": {
            found["climate"]: {
                "quirk": "sonoff_trv_zbt",
                "entities": entities,
                "options": {
                    "temperature_source": "remote",
                    "delta": 3.0,
                    "pulse_delay": 8,
                    "hot_step": True,
                },
            }
        },
    }
    assert result["result"].unique_id == VTHERM_ENTRY_ID


async def test_choose_none_aborts(hass: HomeAssistant) -> None:
    """Choosing no quirk for every underlying does not create an entry."""
    found = add_device(hass, "z2m")
    add_vtherm_entry(hass, underlying_entity_ids=[found["climate"]])
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_vtherm_unique_id": VTHERM_ENTRY_ID}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"quirk": "none"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_quirk_selected"


async def test_no_detection_defaults_to_none(hass: HomeAssistant) -> None:
    """An underlying that is not a known device defaults to no quirk."""
    add_vtherm_entry(hass, underlying_entity_ids=["climate.unknown"])
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_vtherm_unique_id": VTHERM_ENTRY_ID}
    )
    assert result["step_id"] == "quirk"
    assert result["data_schema"]({}) == {"quirk": "none"}
    assert result["description_placeholders"]["detected"] == "-"


async def test_each_underlying_has_its_own_quirk_step(hass: HomeAssistant) -> None:
    """Two underlyings give two quirk steps, one entry with only the chosen one."""
    found = add_device(hass, "z2m")
    add_vtherm_entry(
        hass, underlying_entity_ids=["climate.other", found["climate"]]
    )
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_vtherm_unique_id": VTHERM_ENTRY_ID}
    )
    assert result["description_placeholders"]["underlying"] == "climate.other"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"quirk": "none"}
    )
    assert result["description_placeholders"]["underlying"] == found["climate"]
    assert result["data_schema"]({}) == {"quirk": "sonoff_trv_zbt"}


async def test_only_compatible_vtherms_are_listed(hass: HomeAssistant) -> None:
    """Switch VTherms and central configuration are not proposed."""
    add_vtherm_entry(hass, "vt_ok", name="Ok")
    add_vtherm_entry(hass, "vt_switch", name="Switch", thermostat_type="thermostat_over_switch")
    add_vtherm_entry(hass, "vt_central", name="Central", thermostat_type="thermostat_central_config")
    result = await start_flow(hass)
    selector_config = next(iter(result["data_schema"].schema.values())).config
    assert [option["value"] for option in selector_config["options"]] == ["vt_ok"]


async def test_abort_without_compatible_vtherm(hass: HomeAssistant) -> None:
    """No compatible VTherm aborts the flow."""
    add_vtherm_entry(hass, thermostat_type="thermostat_over_switch")
    result = await start_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_vtherm"


async def test_configured_vtherm_is_not_proposed_again(hass: HomeAssistant) -> None:
    """A VTherm with an entry is excluded."""
    add_vtherm_entry(hass)
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=VTHERM_ENTRY_ID,
        data={"target_vtherm_unique_id": VTHERM_ENTRY_ID, "quirks": {}},
    ).add_to_hass(hass)
    result = await start_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_vtherm"


async def test_options_flow_updates_the_entry_and_reloads_only_its_vtherm(
    hass: HomeAssistant,
) -> None:
    """Changing the options reloads the VTherm of the entry and no other."""
    found = add_device(hass, "z2m")
    vtherm = add_vtherm_entry(hass, underlying_entity_ids=[found["climate"]])
    other = add_vtherm_entry(hass, "vt_other", name="Other")
    for entry in (vtherm, other):
        entry.mock_state(hass, ConfigEntryState.LOADED)

    entities = {
        role: found[role]
        for role in ("heating_position", "idle_position", "external_temperature", "sensor_select")
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Salon",
        unique_id=VTHERM_ENTRY_ID,
        data={
            "target_vtherm_unique_id": VTHERM_ENTRY_ID,
            "quirks": {
                found["climate"]: {
                    "quirk": "sonoff_trv_zbt",
                    "entities": entities,
                    "options": {"temperature_source": "local", "delta": 2.0, "pulse_delay": 5, "hot_step": False},
                }
            },
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "quirk"
        # The stored quirk is the default.
        assert result["data_schema"]({}) == {"quirk": "sonoff_trv_zbt"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"quirk": "sonoff_trv_zbt"}
        )
        assert result["step_id"] == "entities"
        assert suggested(result["data_schema"], "heating_position") == found["heating_position"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], entities)
        assert result["step_id"] == "options"
        assert suggested(result["data_schema"], "delta") == 2.0
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"temperature_source": "local", "delta": 4.0, "pulse_delay": 5, "hot_step": True},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    quirk_conf = entry.data["quirks"][found["climate"]]
    assert quirk_conf["options"]["delta"] == 4.0
    assert quirk_conf["options"]["hot_step"] is True
    reloaded = [call.args[0] for call in reload.call_args_list]
    assert reloaded == [VTHERM_ENTRY_ID]


async def test_options_flow_can_switch_to_none(hass: HomeAssistant) -> None:
    """Removing every quirk through the options flow aborts, the entry must be deleted."""
    found = add_device(hass, "z2m")
    add_vtherm_entry(hass, underlying_entity_ids=[found["climate"]])
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VTHERM_ENTRY_ID,
        data={
            "target_vtherm_unique_id": VTHERM_ENTRY_ID,
            "quirks": {found["climate"]: {"quirk": "sonoff_trv_zbt", "entities": {}, "options": {}}},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"quirk": "none"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_quirk_selected"


async def test_incompatible_quirk_is_reported(hass: HomeAssistant) -> None:
    """A stored quirk that does not fit the VTherm anymore is reported."""
    found = add_device(hass, "z2m")
    add_vtherm_entry(
        hass,
        underlying_entity_ids=[found["climate"]],
        auto_regulation_mode="auto_regulation_none",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VTHERM_ENTRY_ID,
        data={
            "target_vtherm_unique_id": VTHERM_ENTRY_ID,
            "quirks": {found["climate"]: {"quirk": "sonoff_trv_zbt", "entities": {}, "options": {}}},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{entry.entry_id}_{found['climate']}_incompatible_quirk"
    )
    assert issue is not None

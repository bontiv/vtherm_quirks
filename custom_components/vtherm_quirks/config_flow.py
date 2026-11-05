"""Config flow: one entry per VTherm, with a quirk and its entities per underlying."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ENTITIES,
    CONF_OPTIONS,
    CONF_QUIRK,
    CONF_QUIRKS,
    CONF_TARGET_VTHERM_UNIQUE_ID,
    DOMAIN,
    QUIRK_NONE,
    VT_CONF_NAME,
    VT_CONF_THERMOSTAT_TYPE,
    VT_CONF_UNDERLYING_LIST,
    VT_THERMOSTAT_CENTRAL_CONFIG,
    VTHERM_DOMAIN,
)
from .quirks import BaseQuirk, compatible_quirks, detect_quirk
from .quirks.base import get_device_of_entity


def _vtherm_infos(entry: ConfigEntry) -> dict[str, Any]:
    """Merged configuration of a VTherm entry."""
    return {**entry.data, **entry.options}


class _QuirkFlowMixin:
    """Steps shared by the config flow and the options flow.

    For each underlying of the VTherm: ``quirk`` (choice of the quirk, the
    detected one is the default), then ``entities`` (roles prefilled by the
    discovery) and ``options`` of the quirk.
    """

    _vtherm_infos_data: dict[str, Any]
    _existing: dict[str, dict[str, Any]]
    _queue: list[str]
    _current: str
    _current_quirk: type[BaseQuirk]
    _current_entities: dict[str, str]
    _result: dict[str, dict[str, Any]]

    def _init_quirk_flow(
        self, vtherm_entry: ConfigEntry, existing: dict[str, dict[str, Any]] | None
    ) -> None:
        self._vtherm_infos_data = _vtherm_infos(vtherm_entry)
        self._existing = existing or {}
        self._result = {}
        self._queue = list(self._vtherm_infos_data.get(VT_CONF_UNDERLYING_LIST) or [])

    async def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def _async_next_underlying(self) -> ConfigFlowResult:
        """Go to the next underlying that can have a quirk, or finish."""
        while self._queue:
            self._current = self._queue.pop(0)
            if compatible_quirks(self._vtherm_infos_data):
                return await self.async_step_quirk()
        return await self._async_finish()

    def _existing_for_current(self) -> dict[str, Any]:
        return self._existing.get(self._current) or {}

    async def async_step_quirk(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the quirk of the current underlying."""
        infos = self._vtherm_infos_data
        choices = compatible_quirks(infos)
        if user_input is not None:
            selected = user_input[CONF_QUIRK]
            if selected == QUIRK_NONE:
                return await self._async_next_underlying()
            self._current_quirk = next(q for q in choices if q.quirk_id == selected)
            return await self.async_step_entities()

        device = get_device_of_entity(self.hass, self._current)  # type: ignore[attr-defined]
        detected = detect_quirk(device, infos)
        default = QUIRK_NONE
        existing_id = self._existing_for_current().get(CONF_QUIRK)
        if existing_id in {q.quirk_id for q in choices}:
            default = existing_id
        elif detected is not None:
            default = detected.quirk_id
        schema = vol.Schema(
            {
                vol.Required(CONF_QUIRK, default=default): SelectSelector(
                    SelectSelectorConfig(
                        options=[QUIRK_NONE, *(q.quirk_id for q in choices)],
                        translation_key="quirk",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="quirk",
            data_schema=schema,
            description_placeholders={
                "underlying": self._current,
                "detected": detected.title if detected else "-",
            },
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check the entities of the quirk, prefilled by the discovery."""
        quirk = self._current_quirk
        if user_input is not None or not quirk.entity_roles():
            # A quirk without entity role has nothing to ask.
            self._current_entities = {k: v for k, v in (user_input or {}).items() if v}
            return await self.async_step_options()

        device = get_device_of_entity(self.hass, self._current)  # type: ignore[attr-defined]
        values: dict[str, Any] = {}
        if device is not None:
            values.update(quirk.discover_entities(self.hass, device))  # type: ignore[attr-defined]
        existing = self._existing_for_current()
        if existing.get(CONF_QUIRK) == quirk.quirk_id:
            values.update(existing.get(CONF_ENTITIES) or {})

        fields: dict[Any, Any] = {}
        for role in quirk.entity_roles():
            marker = vol.Required if role.required else vol.Optional
            fields[
                marker(role.key, description={"suggested_value": values.get(role.key)})
            ] = EntitySelector(EntitySelectorConfig(domain=list(role.domains)))
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="entities",
            data_schema=vol.Schema(fields),
            description_placeholders={
                "underlying": self._current,
                "quirk": quirk.title,
            },
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the options of the quirk."""
        quirk = self._current_quirk
        schema = quirk.options_schema()
        if user_input is not None or not schema.schema:
            self._result[self._current] = {
                CONF_QUIRK: quirk.quirk_id,
                CONF_ENTITIES: self._current_entities,
                CONF_OPTIONS: dict(user_input or {}),
            }
            return await self._async_next_underlying()

        existing = self._existing_for_current()
        suggested = (
            existing.get(CONF_OPTIONS) or {}
            if existing.get(CONF_QUIRK) == quirk.quirk_id
            else {}
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="options",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),  # type: ignore[attr-defined]
            description_placeholders={
                "underlying": self._current,
                "quirk": quirk.title,
            },
        )


class VThermQuirksConfigFlow(_QuirkFlowMixin, ConfigFlow, domain=DOMAIN):
    """Create the entry of a VTherm."""

    VERSION = 1

    _target: str
    _title: str

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return VThermQuirksOptionsFlow()

    def _candidate_vtherms(self) -> dict[str, str]:
        """VTherm entries without an entry of the plugin and with a usable quirk."""
        configured = {
            entry.data.get(CONF_TARGET_VTHERM_UNIQUE_ID)
            for entry in self._async_current_entries(include_ignore=False)
        }
        candidates: dict[str, str] = {}
        for entry in self.hass.config_entries.async_entries(VTHERM_DOMAIN):
            infos = _vtherm_infos(entry)
            if (
                entry.entry_id in configured
                or infos.get(VT_CONF_THERMOSTAT_TYPE) == VT_THERMOSTAT_CENTRAL_CONFIG
                or not compatible_quirks(infos)
            ):
                continue
            candidates[entry.entry_id] = infos.get(VT_CONF_NAME) or entry.title
        return candidates

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the VTherm."""
        candidates = self._candidate_vtherms()
        if not candidates:
            return self.async_abort(reason="no_vtherm")
        if user_input is not None:
            self._target = user_input[CONF_TARGET_VTHERM_UNIQUE_ID]
            self._title = candidates[self._target]
            await self.async_set_unique_id(self._target)
            self._abort_if_unique_id_configured()
            vtherm_entry = self.hass.config_entries.async_get_entry(self._target)
            assert vtherm_entry is not None
            self._init_quirk_flow(vtherm_entry, None)
            return await self._async_next_underlying()
        schema = vol.Schema(
            {
                vol.Required(CONF_TARGET_VTHERM_UNIQUE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=entry_id, label=label)
                            for entry_id, label in candidates.items()
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def _async_finish(self) -> ConfigFlowResult:
        if not self._result:
            return self.async_abort(reason="no_quirk_selected")
        return self.async_create_entry(
            title=self._title,
            data={
                CONF_TARGET_VTHERM_UNIQUE_ID: self._target,
                CONF_QUIRKS: self._result,
            },
        )


class VThermQuirksOptionsFlow(_QuirkFlowMixin, OptionsFlow):
    """Change the quirks of a VTherm. Only this VTherm is reloaded."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start again the quirk steps with the stored values as defaults."""
        target = self.config_entry.data[CONF_TARGET_VTHERM_UNIQUE_ID]
        vtherm_entry = self.hass.config_entries.async_get_entry(target)
        if vtherm_entry is None:
            return self.async_abort(reason="vtherm_not_found")
        self._init_quirk_flow(
            vtherm_entry, dict(self.config_entry.data.get(CONF_QUIRKS) or {})
        )
        return await self._async_next_underlying()

    async def _async_finish(self) -> ConfigFlowResult:
        if not self._result:
            return self.async_abort(reason="no_quirk_selected")
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_QUIRKS: self._result},
        )
        return self.async_create_entry(data={})


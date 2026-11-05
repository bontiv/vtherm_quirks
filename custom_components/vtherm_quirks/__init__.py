"""Versatile Thermostat Quirks.

A collection of per-device behavior fixes ("quirks") plugged into Versatile
Thermostat through the underlying hooks of ``vtherm_api``.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_TARGET_VTHERM_UNIQUE_ID,
    DATA_FACTORY,
    DOMAIN,
    FACTORY_NAME,
    VTHERM_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Names the core needs from vtherm_api to call the hooks (vtherm_api >= 0.9.0).
_REQUIRED_API_NAMES = (
    "InterfaceUnderlyingHvacModeHook",
    "InterfaceValveCommandHook",
    "InterfaceUnderlyingPowerHook",
    "ValveCommand",
    "ValveCommandCause",
    "UNDERLYING_HOOK_TIMEOUT_SEC",
)


def hooks_available() -> bool:
    """True when the installed vtherm_api exposes the underlying hooks contract."""
    try:
        import vtherm_api  # noqa: PLC0415
    except ImportError:
        return False
    return all(hasattr(vtherm_api, name) for name in _REQUIRED_API_NAMES)


def _find_vtherm_entity(hass: HomeAssistant, unique_id: str):
    """Return the climate entity of a VTherm, or None when it is not built yet."""
    component = hass.data.get("climate")
    if component is None:
        return None
    for entity in list(component.entities):
        if getattr(entity, "unique_id", None) == unique_id:
            return entity
    return None


async def _async_reload_vtherm(hass: HomeAssistant, unique_id: str) -> None:
    """Reload a VTherm so that it rebuilds its managers (the unique id is its entry id)."""
    vtherm_entry = hass.config_entries.async_get_entry(unique_id)
    if (
        vtherm_entry is None
        or vtherm_entry.domain != VTHERM_DOMAIN
        or vtherm_entry.state is not ConfigEntryState.LOADED
    ):
        _LOGGER.debug(
            "VTherm reload skipped for %s: entry found=%s, domain=%s (expected %s), state=%s (expected %s)",
            unique_id,
            vtherm_entry is not None,
            vtherm_entry.domain if vtherm_entry is not None else None,
            VTHERM_DOMAIN,
            vtherm_entry.state if vtherm_entry is not None else None,
            ConfigEntryState.LOADED,
        )
        return
    _LOGGER.debug("Reloading VTherm %s", vtherm_entry.title)
    await hass.config_entries.async_reload(vtherm_entry.entry_id)


@callback
def _schedule_vtherm_reload(
    hass: HomeAssistant, unique_id: str, *, only_if_missing: bool = False
) -> None:
    """Reload the VTherm in the background, only the concerned one."""

    async def _reload() -> None:
        if only_if_missing:
            entity = _find_vtherm_entity(hass, unique_id)
            if entity is None:
                _LOGGER.debug(
                    "VTherm %s: climate entity not found, reload skipped (only_if_missing)",
                    unique_id,
                )
                return
            if entity.get_feature_manager(FACTORY_NAME) is not None:
                _LOGGER.debug(
                    "VTherm %s: manager %s already present, reload skipped (only_if_missing)",
                    unique_id,
                    FACTORY_NAME,
                )
                return
            _LOGGER.debug(
                "VTherm %s: manager %s missing, reloading it", unique_id, FACTORY_NAME
            )
        else:
            _LOGGER.debug("VTherm %s: reload requested", unique_id)
        await _async_reload_vtherm(hass, unique_id)

    hass.async_create_task(_reload(), f"vtherm_quirks reload {unique_id}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the factory and make the targeted VTherm pick up its manager."""
    _LOGGER.debug(
        "Setting up entry '%s' (%s), target VTherm %s",
        entry.title,
        entry.entry_id,
        entry.data.get(CONF_TARGET_VTHERM_UNIQUE_ID),
    )
    available = hooks_available()
    _LOGGER.debug("hooks_available=%s", available)
    if not available:
        ir.async_create_issue(
            hass,
            DOMAIN,
            "hooks_unavailable",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="hooks_unavailable",
        )
        _LOGGER.error(
            "vtherm_api does not expose the underlying hooks (0.9.0 or later is required)"
        )
        return False
    ir.async_delete_issue(hass, DOMAIN, "hooks_unavailable")

    from vtherm_api import VThermAPI  # noqa: PLC0415

    from .factory import QuirksManagerFactory  # noqa: PLC0415
    from .repairs import async_check_entry  # noqa: PLC0415

    api = VThermAPI.get_vtherm_api(hass)
    if api is None:
        _LOGGER.debug("VThermAPI not found, setup of '%s' aborted", entry.title)
        return False

    domain_data = hass.data.setdefault(DOMAIN, {})
    if DATA_FACTORY not in domain_data:
        factory = QuirksManagerFactory()
        if api.get_feature_manager(factory.name) is None:
            api.register_feature_manager(factory)
            _LOGGER.debug("Factory %s created and registered in the API", factory.name)
        else:
            _LOGGER.debug(
                "Factory %s created but already registered in the API, not registered again",
                factory.name,
            )
        domain_data[DATA_FACTORY] = factory
    else:
        _LOGGER.debug("Factory %s reused", FACTORY_NAME)

    async_check_entry(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    target = entry.data[CONF_TARGET_VTHERM_UNIQUE_ID]
    if hass.is_running:
        _LOGGER.debug(
            "Home Assistant is running: scheduling the reload of VTherm %s if its manager is missing",
            target,
        )
        _schedule_vtherm_reload(hass, target, only_if_missing=True)
    else:
        _LOGGER.debug(
            "Home Assistant is not running: the reload of VTherm %s is deferred to the started event",
            target,
        )
        # The VTherm picks the manager up by itself when it starts. If it
        # started before this entry, reload it once Home Assistant is up.
        @callback
        def _on_started(_event: Event) -> None:
            _LOGGER.debug(
                "Home Assistant started: checking the manager of VTherm %s", target
            )
            _schedule_vtherm_reload(hass, target, only_if_missing=True)

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unregister the factory with the last entry and release the VTherm."""
    from vtherm_api import VThermAPI  # noqa: PLC0415

    from .repairs import async_clear_issues, entry_scope  # noqa: PLC0415

    _LOGGER.debug("Unloading entry '%s' (%s)", entry.title, entry.entry_id)
    async_clear_issues(hass, entry_scope(entry))

    others_loaded = any(
        other.entry_id != entry.entry_id and other.state is ConfigEntryState.LOADED
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if not others_loaded:
        api = VThermAPI.get_vtherm_api(hass)
        if api is not None:
            api.unregister_feature_manager(FACTORY_NAME)
        hass.data.get(DOMAIN, {}).pop(DATA_FACTORY, None)
        _LOGGER.debug("No other entry loaded: factory %s unregistered", FACTORY_NAME)
    else:
        _LOGGER.debug("Other entries still loaded: factory %s kept", FACTORY_NAME)

    if not hass.is_stopping:
        _schedule_vtherm_reload(hass, entry.data[CONF_TARGET_VTHERM_UNIQUE_ID])
    else:
        _LOGGER.debug("Home Assistant is stopping: no reload of the VTherm")
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The quirks of an entry changed: reload only the VTherm it targets."""
    from .repairs import async_check_entry  # noqa: PLC0415

    _LOGGER.debug("Entry '%s' updated, reloading its VTherm", entry.title)
    async_check_entry(hass, entry)
    _schedule_vtherm_reload(hass, entry.data[CONF_TARGET_VTHERM_UNIQUE_ID])

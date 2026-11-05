"""Factory registered in VThermAPI: creates one QuirksManager per eligible VTherm."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import (
    CONF_QUIRK,
    CONF_QUIRKS,
    CONF_TARGET_VTHERM_UNIQUE_ID,
    DOMAIN,
    FACTORY_NAME,
    VT_CONF_AUTO_REGULATION_MODE,
    VT_CONF_THERMOSTAT_TYPE,
)
from .manager import QuirksManager, find_config_entry
from .quirks import compatible_quirks, get_quirk, underlying_type_of, vtherm_scopes_of

if TYPE_CHECKING:
    from vtherm_api import InterfaceFeatureManager, InterfaceThermostatRuntime

_LOGGER = logging.getLogger(__name__)


class QuirksManagerFactory:
    """Creates the manager of the plugin for the VTherms that have quirks configured."""

    @property
    def name(self) -> str:
        """Stable name of the factory."""
        return FACTORY_NAME

    def supports(self, thermostat: InterfaceThermostatRuntime) -> bool:
        """True when an entry targets this VTherm with at least one compatible quirk."""
        entry = find_config_entry(thermostat.hass, thermostat.unique_id)
        if entry is None:
            _LOGGER.debug(
                "supports(%s) = False: no active plugin entry for unique_id %s, plugin entries: %s",
                thermostat.name,
                thermostat.unique_id,
                [
                    (e.title, e.data.get(CONF_TARGET_VTHERM_UNIQUE_ID), e.state, e.disabled_by)
                    for e in thermostat.hass.config_entries.async_entries(DOMAIN)
                ],
            )
            return False
        infos = thermostat.entry_infos
        allowed = compatible_quirks(infos)
        configured = {
            underlying: conf.get(CONF_QUIRK)
            for underlying, conf in (entry.data.get(CONF_QUIRKS) or {}).items()
        }
        result = any(get_quirk(quirk_id) in allowed for quirk_id in configured.values())
        if result:
            _LOGGER.debug(
                "supports(%s) = True: configured quirks %s, compatible quirks %s",
                thermostat.name,
                configured,
                [q.quirk_id for q in allowed],
            )
        else:
            _LOGGER.debug(
                "supports(%s) = False: no compatible quirk, thermostat_type=%s, "
                "auto_regulation_mode=%s, scopes=%s, underlying type=%s, "
                "compatible quirks=%s, configured quirks per underlying=%s",
                thermostat.name,
                infos.get(VT_CONF_THERMOSTAT_TYPE),
                infos.get(VT_CONF_AUTO_REGULATION_MODE),
                sorted(vtherm_scopes_of(infos)),
                underlying_type_of(infos),
                [q.quirk_id for q in allowed],
                configured,
            )
        return result

    def create(self, thermostat: InterfaceThermostatRuntime) -> InterfaceFeatureManager:
        """Create the manager bound to the VTherm."""
        _LOGGER.debug("create(): quirks manager for %s", thermostat.name)
        return QuirksManager(thermostat)

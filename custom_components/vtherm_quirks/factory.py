"""Factory registered in VThermAPI: creates one QuirksManager per eligible VTherm."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import CONF_QUIRK, CONF_QUIRKS, FACTORY_NAME
from .manager import QuirksManager, find_config_entry
from .quirks import compatible_quirks, get_quirk

if TYPE_CHECKING:
    from vtherm_api import InterfaceFeatureManager, InterfaceThermostatRuntime


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
            return False
        allowed = compatible_quirks(thermostat.entry_infos)
        return any(
            get_quirk(conf.get(CONF_QUIRK)) in allowed
            for conf in (entry.data.get(CONF_QUIRKS) or {}).values()
        )

    def create(self, thermostat: InterfaceThermostatRuntime) -> InterfaceFeatureManager:
        """Create the manager bound to the VTherm."""
        return QuirksManager(thermostat)

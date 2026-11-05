"""Registry of the quirks.

Adding a quirk means adding a module in this package and a line in ``QUIRKS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.helpers.device_registry import DeviceEntry

from ..const import (
    SCOPE_OVER_CLIMATE,
    SCOPE_OVER_CLIMATE_VALVE,
    SCOPE_OVER_SWITCH,
    SCOPE_OVER_VALVE,
    UNDERLYING_CLIMATE,
    UNDERLYING_SWITCH,
    UNDERLYING_VALVE,
    VT_AUTO_REGULATION_VALVE,
    VT_CONF_AUTO_REGULATION_MODE,
    VT_CONF_THERMOSTAT_TYPE,
    VT_THERMOSTAT_CLIMATE,
    VT_THERMOSTAT_SWITCH,
    VT_THERMOSTAT_VALVE,
)
from .base import BaseQuirk, EntityRole, QuirkIssue

QUIRKS: list[type[BaseQuirk]] = []

__all__ = [
    "QUIRKS",
    "BaseQuirk",
    "EntityRole",
    "QuirkIssue",
    "compatible_quirks",
    "detect_quirk",
    "get_quirk",
    "underlying_type_of",
    "vtherm_scopes_of",
]


def get_quirk(quirk_id: str | None) -> type[BaseQuirk] | None:
    """Return the quirk class registered under an id, or None when unknown."""
    for quirk in QUIRKS:
        if quirk.quirk_id == quirk_id:
            return quirk
    return None


def vtherm_scopes_of(entry_infos: Mapping[str, Any]) -> frozenset[str]:
    """Return the scopes of a VTherm from its merged configuration."""
    thermostat_type = entry_infos.get(VT_CONF_THERMOSTAT_TYPE)
    if thermostat_type == VT_THERMOSTAT_CLIMATE:
        scopes = {SCOPE_OVER_CLIMATE}
        if entry_infos.get(VT_CONF_AUTO_REGULATION_MODE) == VT_AUTO_REGULATION_VALVE:
            scopes.add(SCOPE_OVER_CLIMATE_VALVE)
        return frozenset(scopes)
    if thermostat_type == VT_THERMOSTAT_SWITCH:
        return frozenset({SCOPE_OVER_SWITCH})
    if thermostat_type == VT_THERMOSTAT_VALVE:
        return frozenset({SCOPE_OVER_VALVE})
    return frozenset()


def underlying_type_of(entry_infos: Mapping[str, Any]) -> str | None:
    """Return the type of the underlyings of a VTherm, or None."""
    return {
        VT_THERMOSTAT_CLIMATE: UNDERLYING_CLIMATE,
        VT_THERMOSTAT_SWITCH: UNDERLYING_SWITCH,
        VT_THERMOSTAT_VALVE: UNDERLYING_VALVE,
    }.get(entry_infos.get(VT_CONF_THERMOSTAT_TYPE))


def compatible_quirks(entry_infos: Mapping[str, Any]) -> list[type[BaseQuirk]]:
    """Return the quirks usable with a VTherm (underlying type and scope)."""
    underlying_type = underlying_type_of(entry_infos)
    scopes = vtherm_scopes_of(entry_infos)
    return [
        quirk
        for quirk in QUIRKS
        if underlying_type in quirk.underlying_types and quirk.vtherm_scopes & scopes
    ]


def detect_quirk(
    device: DeviceEntry | None, entry_infos: Mapping[str, Any]
) -> type[BaseQuirk] | None:
    """Return the first compatible quirk matching the device, or None."""
    if device is None:
        return None
    for quirk in compatible_quirks(entry_infos):
        if quirk.match_device(device):
            return quirk
    return None

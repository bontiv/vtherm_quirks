"""Registry of the quirks.

Adding a quirk means adding a module in this package that defines a
``BaseQuirk`` subclass with a ``quirk_id``. The modules are discovered once,
when this package is imported (Home Assistant imports the integration in an
executor, so the imports do not block the event loop).
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from collections.abc import Iterable, Mapping
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

_LOGGER = logging.getLogger(__name__)


def _discover_quirks(package_name: str, package_path: Iterable[str]) -> tuple[type[BaseQuirk], ...]:
    """Return the quirks defined in the modules of a package, sorted by quirk_id.

    The ``base`` module and the modules starting with ``_`` are skipped. A module
    that fails to import is logged and skipped. When two quirks have the same
    ``quirk_id``, the first one found is kept.
    """
    found: dict[str, type[BaseQuirk]] = {}
    for module_info in pkgutil.iter_modules(package_path):
        name = module_info.name
        if name == "base" or name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{package_name}.{name}")
        except Exception:  # noqa: BLE001 - one broken quirk must not disable the others
            _LOGGER.exception("Cannot import the quirk module %s.%s", package_name, name)
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                not issubclass(cls, BaseQuirk)
                or cls.__module__ != module.__name__
                or not getattr(cls, "quirk_id", None)
            ):
                continue
            if cls.quirk_id in found:
                _LOGGER.error(
                    "The quirk %s.%s uses the quirk_id %r of %s, it is ignored",
                    cls.__module__,
                    cls.__name__,
                    cls.quirk_id,
                    found[cls.quirk_id].__name__,
                )
                continue
            found[cls.quirk_id] = cls
    return tuple(found[quirk_id] for quirk_id in sorted(found))


QUIRKS: tuple[type[BaseQuirk], ...] = _discover_quirks(__name__, __path__)

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
    """Return the first compatible quirk matching the device, or None.

    A warning is logged when several quirks match the same device.
    """
    if device is None:
        return None
    matching = [quirk for quirk in compatible_quirks(entry_infos) if quirk.match_device(device)]
    if len(matching) > 1:
        _LOGGER.warning(
            "Several quirks match the device %s: %s. The first one is used",
            device.id,
            ", ".join(quirk.quirk_id for quirk in matching),
        )
    return matching[0] if matching else None

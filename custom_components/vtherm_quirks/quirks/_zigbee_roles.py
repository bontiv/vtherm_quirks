"""How the quirks find the entities of a Zigbee device, under Zigbee2MQTT and ZHA.

The name starts with ``_``: the registry does not look for quirks in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .base import EntityRole

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry

SONOFF_MANUFACTURER = "SONOFF"  # verified (ZHA and Z2M)

Z2M_PLATFORM = "mqtt"
ZHA_PLATFORM = "zha"


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """How to recognize the entity of a role on each integration.

    Zigbee2MQTT: the unique_id of an entity is "<ieee>_<expose name>_<base
    topic>" (from the MQTT discovery of Z2M), matched by "contains _<name>_"
    or "ends with _<name>".

    ZHA: the translation key of the entity, or as a fallback the unique_id
    suffix ("<ieee>-<endpoint>-<cluster>-<attribute>"). A role without
    ``zha_translation_key`` nor ``zha_attribute`` is never found under ZHA.
    """

    role: EntityRole
    z2m_name: str
    zha_translation_key: str | None = None
    zha_attribute: str | None = None


def is_sonoff(device: DeviceEntry) -> bool:
    """True when the manufacturer of the device is SONOFF."""
    return (device.manufacturer or "").strip().upper() == SONOFF_MANUFACTURER


def is_z2m(device: DeviceEntry) -> bool:
    """True when the device comes from Zigbee2MQTT (MQTT identifiers)."""
    return any(identifier[0] == Z2M_PLATFORM for identifier in device.identifiers)


def matches(entry: er.RegistryEntry, spec: RoleSpec) -> bool:
    """Return True when a registry entry is the entity of a role."""
    if entry.domain not in spec.role.domains:
        return False
    unique_id = entry.unique_id or ""
    if entry.platform == Z2M_PLATFORM:
        name = spec.z2m_name
        return f"_{name}_" in unique_id or unique_id.endswith(f"_{name}")
    if entry.platform == ZHA_PLATFORM:
        if spec.zha_translation_key and entry.translation_key == spec.zha_translation_key:
            return True
        return bool(spec.zha_attribute) and unique_id.endswith(f"-{spec.zha_attribute}")
    return False


def discover_role_entities(
    hass: HomeAssistant, device: DeviceEntry, specs: tuple[RoleSpec, ...]
) -> dict[str, str | None]:
    """Find the entities of a device by role, disabled entities included."""
    registry = er.async_get(hass)
    found: dict[str, str | None] = {spec.role.key: None for spec in specs}
    for entry in er.async_entries_for_device(registry, device.id, include_disabled_entities=True):
        for spec in specs:
            if found[spec.role.key] is None and matches(entry, spec):
                found[spec.role.key] = entry.entity_id
    return found

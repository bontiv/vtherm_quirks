"""Base class and helpers of the quirks.

A quirk is a behavior fix for one underlying model. It is plugged into
Versatile Thermostat through the hooks of ``vtherm_api`` (see
``vtherm_api.underlying_hooks``). The :class:`~..manager.QuirksManager`
creates one quirk instance per configured underlying and forwards the hooks to
it.

A quirk only defines the hooks it needs. The base class deliberately does not
define any hook: the manager forwards a hook to a quirk only when its class
defines it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from ..const import VT_CONF_UNDERLYING_LIST

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry
    from vtherm_api import InterfaceThermostatRuntime


@dataclass(frozen=True, slots=True)
class EntityRole:
    """An entity needed by a quirk, shown as a field in the config flow."""

    key: str
    """Name of the role, also the key of the field in the config flow."""
    domains: tuple[str, ...]
    """Entity domains accepted for this role."""
    required: bool = True
    """A quirk is configured only when all its required roles are filled."""


@dataclass(frozen=True, slots=True)
class QuirkIssue:
    """A configuration problem reported by a quirk.

    ``key`` is the translation key of the repair issue (``issues`` section of
    strings.json). The manager adds the ``name``, ``underlying`` and ``quirk``
    placeholders.
    """

    key: str
    severity: ir.IssueSeverity = ir.IssueSeverity.WARNING
    placeholders: Mapping[str, str] = field(default_factory=dict)


def get_device_of_entity(hass: HomeAssistant, entity_id: str) -> DeviceEntry | None:
    """Return the device registry entry of an entity, or None."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None
    return dr.async_get(hass).async_get(entry.device_id)


class BaseQuirk:
    """A behavior fix for one underlying model.

    Optional hooks, with the same signatures as in ``vtherm_api``::

        async def async_before_underlying_hvac_mode(
            self, climate_entity_id, hvac_mode, current_hvac_mode, command_cause
        ) -> None: ...
        async def async_after_valve_command(self, command: ValveCommand) -> None: ...
        async def async_on_underlying_power(self, command: UnderlyingPowerCommand) -> None: ...

    Hooks must return quickly (the core cuts them after 10 seconds): a long
    treatment runs in a background task owned by the quirk.
    """

    quirk_id: ClassVar[str]
    """Stable identifier stored in the config entry, for example ``sonoff_trv_zbt``."""
    title: ClassVar[str]
    """Human readable name, used in logs and repairs."""
    underlying_types: ClassVar[frozenset[str]]
    """Underlying types handled: ``climate``, ``switch`` or ``valve``."""
    vtherm_scopes: ClassVar[frozenset[str]]
    """VTherm scopes handled: ``over_climate_valve``, ``over_climate``, ``over_switch``..."""

    # ------------------------------------------------------------------
    # Discovery and configuration (class level, used by the config flow)
    # ------------------------------------------------------------------
    @classmethod
    def match_device(cls, device: DeviceEntry) -> bool:
        """Return True when the device is the model handled by this quirk."""
        raise NotImplementedError

    @classmethod
    def discover_entities(
        cls, hass: HomeAssistant, device: DeviceEntry
    ) -> dict[str, str | None]:
        """Find the entities the quirk needs on the device, by role key."""
        return {}

    @classmethod
    def entity_roles(cls) -> list[EntityRole]:
        """Return the roles shown in the config flow."""
        return []

    @classmethod
    def options_schema(cls) -> vol.Schema:
        """Return the schema of the options of the quirk, with defaults."""
        return vol.Schema({})

    @classmethod
    def resolve_options(cls, options: Mapping[str, Any] | None) -> dict[str, Any]:
        """Validate the stored options and fill the defaults."""
        return dict(cls.options_schema()(dict(options or {})))

    # ------------------------------------------------------------------
    # Instance
    # ------------------------------------------------------------------
    def __init__(
        self,
        hass: HomeAssistant,
        runtime: InterfaceThermostatRuntime,
        underlying_entity_id: str,
        entities: Mapping[str, str | None],
        options: Mapping[str, Any],
    ) -> None:
        self.hass = hass
        self.runtime = runtime
        self.underlying_entity_id = underlying_entity_id
        self.entities: dict[str, str | None] = dict(entities)
        self.options: dict[str, Any] = dict(options)
        self.issue_sync_callback: Callable[[], None] | None = None
        """Set by the manager: asks it to read ``check_configuration`` again."""

    @property
    def is_configured(self) -> bool:
        """True when all the required entities are set."""
        return all(
            self.entities.get(role.key)
            for role in self.entity_roles()
            if role.required
        )

    @property
    def underlying_index(self) -> int | None:
        """Position of the underlying in the VTherm list of underlyings."""
        underlyings = self.runtime.entry_infos.get(VT_CONF_UNDERLYING_LIST) or []
        try:
            return list(underlyings).index(self.underlying_entity_id)
        except ValueError:
            return None

    async def async_start(self) -> None:
        """Put the device back in a clean state at startup and start listening."""

    async def async_stop(self) -> None:
        """Cancel the running work and restore the device."""

    def check_configuration(self) -> list[QuirkIssue]:
        """Return the configuration problems to report. Must be cheap and synchronous.

        The manager calls it at each cycle of the VTherm. A quirk that finds a
        problem at runtime keeps it and calls :meth:`request_issue_sync` so that
        the repairs are updated without waiting for the next cycle.
        """
        return []

    def request_issue_sync(self) -> None:
        """Ask the manager to read ``check_configuration`` again and update the repairs."""
        if self.issue_sync_callback is not None:
            self.issue_sync_callback()

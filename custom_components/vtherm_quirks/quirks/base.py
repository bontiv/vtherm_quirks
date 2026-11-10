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

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.start import async_at_started

from ..const import STATE_UNAVAILABLE, STATE_UNKNOWN, VT_CONF_UNDERLYING_LIST

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry
    from vtherm_api import InterfaceThermostatRuntime

# Public API for the quirks. Import the names explicitly:
# ``from .base import BaseQuirk, QuirkIssue, to_float``.
__all__ = [
    "BaseQuirk",
    "EntityRole",
    "QuirkIssue",
    "clamp_temperature",
    "get_device_of_entity",
    "to_float",
]


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
    translations/en.json). The manager adds the ``name``, ``underlying`` and
    ``quirk`` placeholders.
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


def to_float(value: Any) -> float | None:
    """Convert a state or an attribute to a float, None when impossible."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clamp_temperature(
    value: float, minimum: float, maximum: float, digits: int = 1
) -> float:
    """Keep a temperature in the range a device accepts, rounded to its precision."""
    return round(max(minimum, min(maximum, value)), digits)


class _UnderlyingLogger(logging.LoggerAdapter):
    """Prefixes every message with the underlying entity id."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        return f"{self.extra['underlying']} - {msg}", kwargs


class BaseQuirk:
    """A behavior fix for one underlying model.

    Optional hooks, with the same signatures as in ``vtherm_api``::

        async def async_before_underlying_hvac_mode(
            self, climate_entity_id, hvac_mode, current_hvac_mode, command_cause
        ) -> None: ...
        async def async_after_valve_command(self, command: ValveCommand) -> None: ...
        async def async_on_underlying_power(self, command: UnderlyingPowerCommand) -> None: ...

    Hooks must return quickly (the core cuts them after 10 seconds): a long
    treatment runs in a background task started with :meth:`spawn`.

    Optional methods of the contracts of ``vtherm_api`` 0.10.0, which, unlike
    the hooks above, can change what the core does::

        async def async_before_valve_write(
            self, request: ValveWriteRequest
        ) -> ValveWriteDecision | None: ...
        def to_underlying_hvac_mode(self, climate_entity_id, hvac_mode) -> str | None: ...
        def from_underlying_hvac_mode(self, climate_entity_id, underlying_hvac_mode) -> str | None: ...
        def adjust_calibration_offset(self, request: CalibrationOffsetRequest) -> float | None: ...

    ``async_before_valve_write`` is awaited before the core writes the valve:
    it returns a decision (a new opening degree, or ``handled=True`` when the
    quirk writes the valve itself, with ``settle_sec`` for a long sequence) or
    ``None`` to leave the value of the core. The three other methods are plain
    functions without input or output, called from properties: ``None`` means
    no translation or no adjustment, ``UNSUPPORTED_HVAC_MODE`` marks a device
    mode with no VTherm equivalent.

    The default :meth:`async_start` calls :meth:`on_underlying_state` on each
    state change of the underlying (only when the quirk defines it) and
    :meth:`async_startup` once Home Assistant is running. The default
    :meth:`async_stop` undoes that and cancels the tasks started with
    :meth:`spawn`.
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
        self.log = _UnderlyingLogger(
            logging.getLogger(type(self).__module__),
            {"underlying": underlying_entity_id},
        )
        self._started = False
        self._unsubs: list[Callable[[], None]] = []
        self._tasks: set[asyncio.Task] = set()

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        """Listen to the underlying, then run :meth:`async_startup`."""
        if self._started:
            return
        self._started = True
        if type(self).on_underlying_state is not BaseQuirk.on_underlying_state:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self.underlying_entity_id], self._on_state_event
                )
            )
        if self.hass.is_running:
            await self.async_startup()
        else:
            # Its cancel function is safe to call after the callback ran.
            self._unsubs.append(async_at_started(self.hass, self._async_at_started))

    async def _async_at_started(self, _hass: HomeAssistant) -> None:
        await self.async_startup()

    async def async_startup(self) -> None:
        """Put the device back in a clean state, once Home Assistant is running."""

    async def async_stop(self) -> None:
        """Stop listening and cancel the tasks started with :meth:`spawn`."""
        self._started = False
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    @callback
    def _on_state_event(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        if new_state is not None:
            self.on_underlying_state(event.data["old_state"], new_state)

    @callback
    def on_underlying_state(self, old_state: State | None, new_state: State) -> None:
        """Called on each state change of the underlying, when the quirk defines it."""

    # ------------------------------------------------------------------
    # Repairs
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Helpers for the quirks
    # ------------------------------------------------------------------
    @property
    def underlying_state(self) -> State | None:
        """Current state of the underlying."""
        return self.hass.states.get(self.underlying_entity_id)

    @property
    def underlying_available(self) -> bool:
        """True when the underlying has a state other than unavailable or unknown."""
        state = self.underlying_state
        return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        """Run a background job, cancelled by :meth:`async_stop`."""
        task = self.hass.async_create_background_task(
            coro, f"vtherm_quirks {name} {self.underlying_entity_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any], *, blocking: bool = True
    ) -> bool:
        """Call a service. A failure is logged and returns False, it never raises."""
        self.log.debug("calling %s.%s %s (blocking=%s)", domain, service, data, blocking)
        try:
            await self.hass.services.async_call(domain, service, data, blocking=blocking)
        except Exception as err:  # noqa: BLE001 - a quirk must never break the core
            self.log.warning("%s.%s %s failed: %s", domain, service, data, err)
            return False
        return True

    async def async_sleep(self, seconds: float) -> None:
        """Sleep on a Home Assistant timer: cancellable, and driven by the test clock."""
        future: asyncio.Future[None] = self.hass.loop.create_future()

        @callback
        def _done(_now: Any) -> None:
            if not future.done():
                future.set_result(None)

        cancel = async_call_later(self.hass, seconds, _done)
        try:
            await future
        finally:
            cancel()

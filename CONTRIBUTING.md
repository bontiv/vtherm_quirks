# Contributing a quirk

A quirk is a behavior fix for one device model, plugged into Versatile Thermostat (VTherm) through the underlying hooks of `vtherm_api`. Contributions of quirks are welcome as pull requests. This guide explains how the plugin is built and what a quirk must provide.

## How the pieces fit

```text
custom_components/vtherm_quirks/
  __init__.py        # setup / unload: registers the factory, reloads the concerned VTherm
  config_flow.py     # one entry per VTherm: quirk and entities per underlying, quirk options
  factory.py         # QuirksManagerFactory (name "vtherm_quirks")
  manager.py         # QuirksManager: dispatches the hooks to the quirks
  repairs.py         # issues reported by check_configuration
  const.py           # constants shared by the framework and several quirks
  quirks/
    __init__.py      # discovers the quirk modules (QUIRKS) and the helpers
    base.py          # BaseQuirk, EntityRole, QuirkIssue
    moes_trv601.py
    sonoff_trv_zbt.py
```

- The **registry** (`quirks/__init__.py`) discovers the quirk classes when the package is imported: it imports every module of `quirks/` except `base` and the ones starting with `_`, and keeps the `BaseQuirk` subclasses defined there that have a `quirk_id`. Adding a quirk means adding a module, there is no list to edit. A module that fails to import is logged and skipped, and a duplicate `quirk_id` is logged and ignored.
- The **`QuirksManager`** is the only feature manager of the plugin, one per VTherm. It implements the three hooks and forwards each call to the quirk instance bound to the underlying concerned. It only forwards a hook to a quirk whose class defines it.
- A **quirk instance** exists for each configured underlying. It keeps its own state (tasks, locks) and only implements the hooks it needs.

The plugin never imports the private modules of the VTherm core. The VTherm configuration is read from `runtime.entry_infos`, with the keys declared in `const.py` (prefix `VT_CONF_`).

`const.py` only keeps what the framework and several quirks share: the integration keys, the `VT_CONF_` keys, the scopes, the underlying types and the command causes. Everything specific to a device (manufacturer and model, Zigbee2MQTT and ZHA entity names, option keys, defaults and limits) is a constant of the module of its quirk.

## The hooks

They come from `vtherm_api` (0.9.0 or later). They are notifications: none of them modifies or cancels the command of the core.

```python
async def async_before_underlying_hvac_mode(
    self, climate_entity_id: str, hvac_mode: str, current_hvac_mode: str | None, command_cause: str | None
) -> None: ...

async def async_after_valve_command(self, command: ValveCommand) -> None: ...

async def async_on_underlying_power(self, command: UnderlyingPowerCommand) -> None: ...
```

Rules every quirk must respect:

- The core awaits the hook, then sends its command. Return fast. The core cuts the call after `UNDERLYING_HOOK_TIMEOUT_SEC` (10 seconds) and sends its command anyway.
- A long treatment (waiting, retries) runs in a background task owned by the quirk, created with `hass.async_create_background_task`. Keep a reference to cancel it in `async_stop`.
- Do not queue work: when a request arrives while the task is busy, remember it and run one more round with the latest data at the end (coalescing).
- Anything that must be done before the core command (for example before a device goes OFF) is written inside the hook, with `blocking=True`, because the core goes on when the hook returns.
- Restore what you changed in a `finally` block, so that a cancellation leaves the device in a clean state.
- Do not catch `asyncio.CancelledError`. Other exceptions that escape a hook are logged by the manager with the `quirk_id`.

## The `BaseQuirk` contract

```python
class BaseQuirk:
    quirk_id: ClassVar[str]                      # "sonoff_trv_zbt", stored in the config entry
    title: ClassVar[str]                         # shown in the config flow and the repairs
    underlying_types: ClassVar[frozenset[str]]   # {"climate"}, {"switch"}, {"valve"}
    vtherm_scopes: ClassVar[frozenset[str]]      # {"over_climate_valve"}, {"over_climate"}, {"over_switch"}, {"over_valve"}

    @classmethod
    def match_device(cls, device: DeviceEntry) -> bool: ...
    @classmethod
    def discover_entities(cls, hass, device) -> dict[str, str | None]: ...
    @classmethod
    def entity_roles(cls) -> list[EntityRole]: ...
    @classmethod
    def options_schema(cls) -> vol.Schema: ...

    def __init__(self, hass, runtime, underlying_entity_id, entities, options): ...
    async def async_start(self) -> None: ...
    async def async_stop(self) -> None: ...
    def check_configuration(self) -> list[QuirkIssue]: ...
```

- `match_device` is true when the device registry entry is the model handled. Match on the manufacturer and the model, under every integration you support (the model string differs between Zigbee2MQTT and ZHA).
- `discover_entities` finds the entities the quirk needs on the device, by role key. Include disabled entities (`include_disabled_entities=True`): some integrations disable them by default. Return `None` for a role that is not found.
- `entity_roles` lists the roles shown in the config flow, with the accepted domains and whether each one is required. A quirk is configured, and receives hooks, only when all its required roles are filled.
- `options_schema` describes the options with defaults, so that `options_schema()({})` is valid. Use selectors, they are rendered by the config flow.
- `async_start` runs at startup: put the device back in a clean state and subscribe to what you need. `async_stop` cancels the running work and restores the device.
- `check_configuration` is synchronous and cheap, it runs at every VTherm cycle. It returns the problems to report as `QuirkIssue(key, placeholders=...)`. A problem found at runtime, for example in a state listener, is kept by the quirk and returned by the next `check_configuration`. Call `self.request_issue_sync()` so that the repairs are updated at once, without waiting for the next cycle.

`quirk_id`, `underlying_types` and `vtherm_scopes` decide when the quirk is offered: the VTherm must have an underlying of one of the types and one of the scopes. `over_climate_valve` is an `over_climate` with the direct valve control (`auto_regulation_valve`).

## Minimal example

A quirk that only follows the power requested from a switch and logs it:

```python
"""Quirk example: log the power requested from a switch."""

from __future__ import annotations

import logging
from typing import ClassVar

from homeassistant.helpers.device_registry import DeviceEntry
from vtherm_api import UnderlyingPowerCommand

from ..const import SCOPE_OVER_SWITCH, UNDERLYING_SWITCH
from .base import BaseQuirk, EntityRole

_LOGGER = logging.getLogger(__name__)


class AcmeRelayQuirk(BaseQuirk):
    quirk_id: ClassVar[str] = "acme_relay"
    title: ClassVar[str] = "Acme relay"
    underlying_types: ClassVar[frozenset[str]] = frozenset({UNDERLYING_SWITCH})
    vtherm_scopes: ClassVar[frozenset[str]] = frozenset({SCOPE_OVER_SWITCH})

    @classmethod
    def match_device(cls, device: DeviceEntry) -> bool:
        return (device.manufacturer or "").upper() == "ACME" and device.model == "RELAY-1"

    @classmethod
    def entity_roles(cls) -> list[EntityRole]:
        return [EntityRole("relay", ("switch",))]

    # Only the hook the quirk needs.
    async def async_on_underlying_power(self, command: UnderlyingPowerCommand) -> None:
        _LOGGER.debug("%s requested %.0f%%", self.underlying_entity_id, command.requested_on_percent * 100)
```

Save it as `quirks/acme_relay.py`. It is discovered automatically, there is nothing to register. Keep its device identifiers, option keys and limits as constants at the top of this module.

For a quirk with `entity_roles` or `options_schema` of its own, also add:

- the labels of the role keys and of the option keys under `config.step.entities.data`, `config.step.options.data` and the same keys under `options.step` in `strings.json`, and in `translations/en.json`, `fr.json` and `de.json`;
- the label of the quirk under `selector.quirk.options.<quirk_id>`;
- the translations of every issue key your `check_configuration` can return, under `issues`, with the placeholders `name`, `underlying`, `quirk` and the ones you add.

## Expected tests

Tests use `pytest-homeassistant-custom-component`. A pull request for a quirk includes, under `tests/quirks/test_<quirk_id>.py`:

- the detection of the device (`match_device`) for each integration supported, and of models that must not match;
- `discover_entities` on simulated devices registered in the device and entity registries;
- for every hook, the exact service calls and values, in order, with the time advanced by `async_fire_time_changed` when the quirk waits;
- the cases that go wrong: unavailable entity, missing attribute, cancellation in the middle of a treatment, an error from a service call;
- `check_configuration`: each issue appears and disappears;
- `async_start` and `async_stop`.

The registry tests (`tests/test_registry.py`) already check that identifiers are unique and that every quirk declares its roles and options. Look at `tests/quirks/test_sonoff_trv_zbt.py` and `tests/common.py` (simulated device, fake runtime) as a model.

Run the whole suite before opening the pull request:

```bash
pip install -r requirements_test.txt
pytest
```

## Documentation

Add `docs/quirks/<quirk_id>.md` (why the quirk exists, supported setups, device and VTherm settings, options, how to verify it) and a line in the quirks table of `README.md`, `README.fr.md` and `README.de.md`. If a section of the page would only hold a single callout, write it as plain text instead.

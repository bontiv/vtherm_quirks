# Versatile Thermostat Quirks

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

*Lire en français : [README-fr.md](README-fr.md)*

Some devices do not behave the way a generic thermostat expects. A valve that ignores a new opening while it is idle, a TRV that goes to a fixed position when it is switched off, a setpoint that must be sent again after a mode change. These are **quirks**.

This integration is a collection of quirks for [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm). VTherm stays generic. Each particular device has its own quirk here, plugged into VTherm through the underlying hooks of [`vtherm_api`](https://github.com/jmcollin78/vtherm_api).

## How it works

VTherm calls three optional hooks around the commands it sends to its underlying entities:

| Hook | When |
|---|---|
| `async_before_underlying_hvac_mode` | Before `climate.set_hvac_mode` is sent to an underlying climate |
| `async_after_valve_command` | After the opening and closing degrees are sent (`over_climate` with direct valve control) |
| `async_on_underlying_power` | At the start of a cycle, on each valve update, and on stop, for each underlying |

This integration registers one feature manager per VTherm. The manager forwards each hook to the quirk bound to the underlying concerned. A quirk only implements the hooks it needs, and an error in a quirk is logged without disturbing VTherm or the other quirks.

## Quirks

| Quirk | Device | Integrations | Page |
|---|---|---|---|
| `moes_trv601` | Moes TRV801 / Tuya TRV601 | Zigbee2MQTT | [docs/quirks/moes_trv601.md](docs/quirks/moes_trv601.md) |
| `sonoff_trv_zbt` | Sonoff TRV-ZBT (Gen2) | Zigbee2MQTT, ZHA | [docs/quirks/sonoff_trv_zbt.md](docs/quirks/sonoff_trv_zbt.md) |

It replaces the [Sonoff TRV-ZBT valve kick blueprint](https://gist.github.com/bontiv/af2d59adb15bf3d522282edd662a75e4). Other quirks are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Requirements

- Home Assistant 2025.1 or later.
- A Versatile Thermostat release that calls the underlying hooks, which means a release depending on `vtherm_api` 0.9.0 or later. Home Assistant installs `vtherm_api` for this integration.

If the installed `vtherm_api` does not provide the hooks, the integration does not load and a repair issue tells you to update.

## Installation

With [HACS](https://hacs.xyz):

1. Open HACS, then the menu, then **Custom repositories**.
2. Add `https://github.com/bontiv/vtherm_quirks` with the category **Integration**.
3. Install **Versatile Thermostat Quirks** and restart Home Assistant.

Manually: copy `custom_components/vtherm_quirks` into the `custom_components` folder of your Home Assistant configuration and restart.

## Configuration

Go to **Settings, Devices & services, Add integration, Versatile Thermostat Quirks**. There is one entry per VTherm.

1. Choose the VTherm. Only thermostats that can use a quirk are listed, for example an `over_climate` with direct valve control for the TRV-ZBT.
2. For each underlying entity of the VTherm, the integration looks up its device and proposes the quirk that matches it. You can also choose **None**, or another quirk compatible with the thermostat.
3. For each underlying with a quirk, check the entities (prefilled from the device) and set the options of the quirk.

Use the **Configure** button of the entry to change these choices later. Only the VTherm of the entry is reloaded.

## Repairs

Each quirk checks the configuration of the VTherm and of the device and reports what prevents it from working in the Home Assistant repairs: a VTherm setting that conflicts with the quirk, a disabled entity, a device feature to turn off. Issues disappear as soon as the problem is fixed. A quirk that does not exist anymore in the installed version is ignored and reported.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The tests use `pytest-homeassistant-custom-component`. The architecture and the guide to add a quirk are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).

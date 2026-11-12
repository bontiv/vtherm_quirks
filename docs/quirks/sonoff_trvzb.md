# Sonoff TRVZB

The `sonoff_trvzb` quirk lets Versatile Thermostat (VTherm) drive the Sonoff TRVZB (Gen1) in direct valve control. It manages the closing degree of the valve, which VTherm does not need to know about, and closes the valve firmly when VTherm closes it.

## Why a quirk

The TRVZB has two valve settings:

- `valve_opening_degree`: the opening while the TRV heats;
- `valve_closing_degree`: how closed the valve is while the TRV is idle (100 = fully closed).

The closing degree is specific to this model. The quirk owns it: after each valve command of VTherm it writes `closing = 100 - opening`, so that the valve ends at the requested opening whatever the state of the TRV. VTherm only needs the opening degree entity.

Some TRVZB motors also lose their calibration: when they close from a small opening, they stop before the seat and the radiator keeps heating (see [Better Thermostat](https://github.com/KartoffelToby/better_thermostat) and [sonoff_trv_slam_close](https://github.com/caiusseverus/sonoff_trv_slam_close)). When VTherm is about to close the valve from an open position, the quirk takes the write over: it opens the valve wide for a few seconds, then closes it. This is the **slam close**. It needs a Versatile Thermostat release that calls the before-write contract of `vtherm_api` 0.10.0.

## Supported setups

| Integration | Requirement |
|---|---|
| Zigbee2MQTT | Device `TRVZB` with its `valve_opening_degree` and `valve_closing_degree` entities |
| ZHA | The `Valve opening degree` and `Valve closing degree` entities, disabled by default: enable them first |

The two valve entities need the TRVZB firmware 1.1.4 or later.

## Settings of the TRV

- Turn off `smart_temperature_control` (called "adaptive mode" in ZHA): while it is on, Zigbee2MQTT makes the opening degree unavailable.
- Turn off `open_window`.

## Settings of the VTherm

- Thermostat type: `over_climate`, with the self-regulation **direct valve control**.
- Opening degree entity: `valve_opening_degree` (ZHA: Valve opening degree).
- **No closing degree entity**: the quirk manages it.
- Max opening degree: 99 is recommended. With the exact pair opening 100 / closing 0, some firmwares (1.4.4) leave the TRV idle with the valve closed.

The device temperature synchronization is compatible. VTherm does not switch `temperature_sensor_select` to `remote_temperature`: do it yourself if you synchronize through `external_temperature_input`. The TRV goes back to its built-in sensor after about 2 hours without an update.

The plugin reports in the repairs a closing degree entity configured in the VTherm and an opening degree entity that is not the `opening_degree` of the quirk.

## Configuration of the plugin

Add the integration **Versatile Thermostat Quirks**, choose the VTherm and accept the `Sonoff TRVZB` quirk proposed for the TRV (detected from the manufacturer and the model). Check the entities, prefilled from the device, then the options:

| Option | Default | Meaning |
|---|---|---|
| Pre-opening (`pre_open`) | 99 % | Opening of the slam close, from 0 to 99. 0 disables the slam close. |
| Pre-opening duration (`slam_delay`) | 5 s | Time the valve stays at the pre-opening, from 2 to 60 s. |

## Closing degree

- After each valve command, the quirk writes `closing = 100 - opening` when the entity has another value. A startup or repair command of VTherm always writes it.
- During the valve recalibration of VTherm, the quirk aligns the closing degree with each forced stroke: `0` for the full opening, `100` for the full closing. These writes are never skipped, and they cancel a slam close still running from the OFF that began the recalibration.
- At startup, an unknown or wrong closing degree is set from the current opening.
- A closing degree changed by something else is put back after 10 s. After 3 corrections in an hour the quirk stops and raises the repair `closing_degree_keeps_changing`. It starts again when the thermostat changes mode.

## The slam close

It runs when VTherm is about to write a closed valve (0, or the floor set by the max closing degree of VTherm) after an open one. The quirk answers VTherm that it writes the valve itself, so VTherm writes nothing, and runs in the background:

1. Write opening = pre-opening, then closing = 100 - pre-opening.
2. Wait the pre-opening duration.
3. Write closing = 100, then opening = 0.

The quirk also tells VTherm how long the sequence lasts: the pre-opening duration plus 5 seconds for the writes. VTherm does not repair the valve during that time, so it does not take the pre-opening for a fault.

Step 3 also runs when the slam close is cancelled by a restart of the integration or when a write fails: the valve is never left open.

There is no slam close, and VTherm writes the valve as usual, when the previous opening was already at the pre-opening or more, when the valve was already closed, when one of the two valve entities is unavailable, or at the first command after a restart. A new opening during a slam close cancels it before VTherm writes this opening. A close sent again during a slam close does not restart it: the slam close ends on the last close received.

## Migration

1. Remove the closing degree entity from the VTherm.
2. Turn off any automation or blueprint that writes the valve degrees (for example `sonoff_trv_slam_close`).
3. Add the plugin entry for the VTherm and check the repairs.

## Troubleshooting

- The valve still does not close: increase the pre-opening duration.
- The radiator heats briefly when the valve closes: during the pre-opening, VTherm sees an open valve. With a central boiler, the boiler may start for a few seconds.
- Nothing happens: check the logs of `custom_components.vtherm_quirks` and that both valve entities exist and are enabled.

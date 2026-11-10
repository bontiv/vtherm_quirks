# Eurotronic Spirit Zigbee

The `eurotronic_spzb0001` quirk keeps the TRV mode of the Eurotronic Spirit Zigbee (SPZB0001) in line with the valve control of Versatile Thermostat (VTherm), under Zigbee2MQTT.

## Why a quirk

The Spirit has two modes, set by its `trv_mode` select:

| `trv_mode` | Effect |
|---|---|
| `1` | The valve follows `valve_position` (0 to 255) |
| `2` | The TRV regulates on its own setpoint and ignores `valve_position` (default) |

In direct valve control, VTherm writes `valve_position`, which does nothing in mode 2. Zigbee2MQTT does not report these two values from the device: their state in Home Assistant is the last value written, so a TRV back in mode 2 by itself stays invisible.

## Supported setups

| Integration | Requirement |
|---|---|
| Zigbee2MQTT | Device `SPZB0001` with its `trv_mode` and `valve_position` entities |

ZHA exposes neither `trv_mode` nor `valve_position`: it is not covered.

## Behavior

The quirk keeps `trv_mode` on `1` when the VTherm uses the direct valve control, on `2` otherwise. It checks and writes it:

- at startup;
- when the thermostat leaves OFF (always written: the TRV may have rebooted or had its batteries changed while off);
- after the valve commands of VTherm (always written on a startup or repair command);
- when the select changes.

After a switch to `1`, the last opening of VTherm is written again on `valve_position`. While VTherm is off, the mode stays `1` and VTherm closes the valve.

A mode written is not written again within 10 s. After 3 corrections in an hour, the quirk stops and raises a repair issue, until the thermostat leaves OFF.

## Settings of the VTherm

- Thermostat type: `over_climate`, with or without the direct valve control.
- With the direct valve control:
  - opening degree entity: `valve_position`;
  - no closing degree entity;
  - max opening degree: empty, or 255. VTherm then scales the opening to the maximum of `valve_position`. With 100, the valve never opens more than about 39 %.

If you used the SPZB0001 fix of Better Thermostat, remove it: both would write `trv_mode`.

## Repair issues

| Issue | Meaning |
|---|---|
| `trv_mode_keeps_changing` | The TRV left the wanted mode again after 3 corrections in an hour. |
| `valve_scale_percent` | VTherm drives `valve_position` on a 0 to 100 scale while it goes up to 255. Empty the max opening degree of the VTherm. |
| `closing_entity_not_supported` | A closing degree entity is configured in the VTherm: the Spirit has none. |
| `opening_entity_mismatch` | The opening degree entity of the VTherm is not `valve_position`. |
| `entity_disabled` | `trv_mode` or `valve_position` is disabled. |

## Limits

Not checked on a real device yet:

- whether the TRV goes back to mode 2 by itself (reboot, batteries, setpoint or system mode change);
- whether `system_mode = heat`, which Zigbee2MQTT writes as the boost flag, opens the valve fully in mode 1;
- whether the Comet `COZB0001`, which shares the converter, behaves the same: it is not detected.

The scale check reads an internal attribute of VTherm (`vtherm_over_climate_valve`). Without it, it falls back to the highest opening VTherm sent.

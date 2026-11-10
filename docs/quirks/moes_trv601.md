# Moes TRV801 / Tuya TRV601

The `moes_trv601` quirk lets Versatile Thermostat (VTherm) turn the Moes TRV801 radiator valve on and off under Zigbee2MQTT. Without it, the valve can stay fully open while VTherm heats.

## Covered models

In Zigbee2MQTT these valves share one definition, `TRV601`:

| Z2M model | Brand | Zigbee manufacturer name |
|---|---|---|
| `TRV601` | Tuya | |
| `TRV801_1` | Moes | `_TZE204_rtrmfadk` |
| `SBDV-00185` | Sber | `_TZE204_cvcu2p6e` |

The quirk looks for these names in the model and the model id of the device, and for the manufacturer names in the model id and the manufacturer. The way Home Assistant stores them is not confirmed on a real device yet. If the quirk is not proposed for your valve, choose it by hand in the configuration and report the manufacturer, model and model id of your device.

ZHA is not covered.

## Why a quirk

The valve has one mode, set by one Tuya data point. Zigbee2MQTT shows it twice on the climate entity:

- as a **preset**: `auto` (weekly schedule of the valve), `manual` (regulation on the setpoint), `off` (valve closed) and `on` (valve forced open). It can be written.
- as the **system mode** (`auto`, `heat`, `off`). It is read only, so the hvac mode sent by VTherm may do nothing.

The preset is the only reliable way to drive the valve. VTherm does not send it and does not follow it. The quirk does.

## Behavior

- VTherm heats: the preset must be `manual`. The quirk sets it before VTherm sends its hvac mode, in a background task that does not delay VTherm.
- VTherm is off: the preset must be `off`.
- The state of the valve is watched. When the preset is wrong, for example `on` (valve forced open) while VTherm heats, the quirk sets the right one again.
- At startup, the preset is checked once.
- Nothing is sent while the valve is unavailable. It is corrected when it comes back.

After 3 corrections in a row, the quirk stops and raises a repair issue, because something else keeps changing the preset. It starts again when VTherm changes mode.

The quirk never changes the hvac mode of the valve, only its preset.

## Zigbee2MQTT version

The `manual` preset is shown as `heat` only with the converter fixed by [zigbee-herdsman-converters#13695](https://github.com/Koenkk/zigbee-herdsman-converters/pull/13695), merged on 2026-11-04. It is not confirmed yet which Zigbee2MQTT release contains it. Check the release notes before you install the quirk.

With an older converter, the preset `manual` is shown as `auto`, and VTherm reads the wrong mode.

## Option

| Option | Default | Meaning |
|---|---|---|
| Keep the schedule of the TRV (`keep_schedule`) | off | When VTherm heats, also accept the preset `auto`, the weekly schedule of the valve. The preset `on` is always corrected. |

The quirk has no entity to set. The preset is read on the climate entity itself.

## Repair issues

| Issue | Severity | Meaning |
|---|---|---|
| `outdated_converter` | error | The preset is `manual` but the state is `auto`. Update Zigbee2MQTT. The quirk does not correct anything in this case. |
| `preset_keeps_changing` | warning | The valve left the wanted preset again after 3 corrections. Look for what changes the preset: the valve itself, an automation or another integration. |

## Settings of the VTherm

- Thermostat type: `over_climate`, with or without direct valve control. The valve has no adjustable opening, only a read-only `position`.
- No other setting is needed.

## Limits

- Only Zigbee2MQTT is covered.
- The identifiers of the device in the Home Assistant registry are not confirmed on a real device.
- Whether `climate.set_hvac_mode` does anything on this valve is not confirmed. If it does nothing, the preset is the only way to drive the valve, and the quirk is needed even with the fixed converter.
- A user who changes the preset on the valve while VTherm heats is corrected, up to 3 times in a row.

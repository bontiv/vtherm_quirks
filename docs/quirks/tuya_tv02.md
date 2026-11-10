# Tuya TV02-Zigbee

The `tuya_tv02` quirk keeps the Tuya TV02-Zigbee radiator valve, and its white labels, off its own weekly schedule while Versatile Thermostat (VTherm) heats.

## Covered models

| Integration | Model |
|---|---|
| Zigbee2MQTT | `TV02-Zigbee`, `TV01-ZB`, `ZTRV-ZX-TV01-MS`, `ZTRV-ZX-TV02`, `HA-08_THERMO`, `TV06`, `ETT-8`, `TSL-TRV-TV01ZG`, `TSL-TRV-TV05ZG`, `TRV06-1`, `GTZ08` (exact model name) |
| ZHA | Model `TS0601` with the manufacturer `_TZE200_` + `sur6q7ko`, `hue3yfsn`, `e9ba97vf`, `husqqvux`, `lnbfnyxd`, `fsow0qsk`, `lllliz3p`, `mudxchsu`, `7yoranx2`, `kds0pmmv`, `py4cm3he`, `wsbfwodu`, `x9axofse`, `lhzapfg9`, `k1tumq4t` |

The way Home Assistant stores these names has not been checked on a real device. If the quirk is not proposed for your valve, choose it by hand and report the manufacturer, model and model id of your device.

## Why a quirk

The valve keeps its mode in its preset: `auto` (weekly schedule), `manual` (regulation on the setpoint) or `holiday`. Under ZHA they are `schedule`, `none` and `holiday`. Zigbee2MQTT shows the hvac mode `heat` whatever the preset.

VTherm does not send an hvac mode the valve already has. A valve back on its schedule (button, end of holiday, reset) is never put back in manual, and its schedule overwrites the setpoint of VTherm.

## Behavior

- VTherm heats: the preset must be `manual` (`none` under ZHA). The quirk sets it before VTherm sends its hvac mode, and corrects it each time the valve changes it.
- VTherm is off, or the valve is off while VTherm heats (open window, power shedding): the quirk imposes nothing. Writing `manual` would switch the valve back on.
- After a correction from `auto` or `holiday`, the setpoint VTherm sent last is written again if the schedule changed it (by more than 0.25 °C), once, within the range of the valve.
- After 3 corrections in a row, the quirk stops and raises a repair issue. It starts again when VTherm changes mode.

The quirk never changes the hvac mode of the valve.

## Settings of the VTherm

- Thermostat type: `over_climate`.
- Disable **Follow underlying temperature changes**: otherwise a setpoint written by the schedule can be taken as a user change before the quirk corrects it.

## Options

| Option | Default | Meaning |
|---|---|---|
| Keep the schedule of the TRV (`keep_schedule`) | off | When VTherm heats, also accept the schedule preset (`auto`, `schedule`). `holiday` is always corrected. |
| Restore the setpoint (`restore_setpoint`) | on | After a correction from the schedule or holiday, write the setpoint of VTherm again. No effect with `keep_schedule`. |

The only entity is optional: the open window switch of the valve, used to report it when it is on.

## Repair issues

| Issue | Meaning |
|---|---|
| `preset_keeps_changing` | The valve left `manual` again after 3 corrections. Look for what changes the preset: the valve itself, an automation or another integration. |
| `follow_underlying_enabled` | The VTherm follows the setpoint changes of the valve. Disable it. |
| `open_window_enabled` | The open window detection of the valve is on. Turn it off: VTherm handles the windows. |

## Limits

- Not checked on a real valve yet: whether the valve stays in `manual` after a battery change, whether the frost protection or the boost change the published preset.
- The setpoint written again is read from an internal attribute of VTherm (`vtherm_over_climate`). If a VTherm release changes it, the quirk falls back to the regulated or target temperature of VTherm.

# Sonoff TRV-ZBT

The `sonoff_trv_zbt` quirk lets Versatile Thermostat (VTherm) drive the Sonoff TRV-ZBT (Gen2) in direct valve control. It replaces the [Sonoff TRV-ZBT valve kick](https://gist.github.com/bontiv/af2d59adb15bf3d522282edd662a75e4) blueprint.

## Why a quirk

The TRV-ZBT only moves its motor when its internal thermostat changes state, from idle to heating or back. A new `heating_valve_position` is ignored while the TRV is idle. When the TRV is `off`, the valve goes to `idle_valve_position`.

The quirk handles both:

- After each valve command of VTherm, it sets `idle_valve_position` to the opening minus 1, so the valve ends at the requested opening whatever the state of the TRV. Then it forces a heating transition with a fake external temperature below the setpoint: this is the **kick**.
- Before the TRV goes OFF, while it is still in heat, it sets `idle_valve_position` to 0. The blueprint reacted after the TRV was already `off`, so the valve stayed open and the radiator kept heating.
- Everything runs in the plugin, in a background task with a single pending kick, instead of queued automations that race with the commands of VTherm.

The setpoint of the TRV and the `heating_valve_position` entity are never changed by the kick, so the VTherm regulation is not disturbed.

## Supported setups

| Integration | Requirement |
|---|---|
| Zigbee2MQTT | Device `TRV-ZBT` with its `heating_valve_position`, `idle_valve_position`, `external_temperature_input` and `temperature_sensor_select` entities |
| ZHA | `zha-device-handlers` (zha-quirks) 2.4.0 or later, with the quirk of the TRV-ZBT |

Under ZHA the valve entities are disabled by default. Enable them in the entity settings of the device before configuring the quirk: `Valve opening degree`, `Valve closing degree`. On the TRV-ZBT, ZHA's "Valve opening degree" is the heating position and "Valve closing degree" is the idle position, not a closing degree.

## Settings of the TRV

- Turn off `smart_temperature_control` (called "adaptive mode" in ZHA). Otherwise the TRV does not follow the fake temperature.
- Turn off `open_window`.
- Set `temperature_accuracy` to -0.2 °C, so that the TRV follows the setpoint more closely.

The plugin reports `smart_temperature_control` and `open_window` in the repairs when they are on and the entities are found.

## Settings of the VTherm

- Thermostat type: `over_climate`, with the self-regulation **direct valve control**.
- Opening degree entity: `heating_valve_position` (ZHA: Valve opening degree).
- No closing degree entity. On the TRV-ZBT, the closing degree entity of ZHA is the idle opening, which the quirk manages.
- Device temperature synchronization disabled: the quirk uses the external temperature of the device.
- The TRV only accepts setpoints by steps of 0.5 °C: set a temperature step of 0.5 °C.

"Follow underlying temperature changes" can stay on: the kick does not change the setpoint.

The plugin reports in the repairs a device synchronization that is on, a closing degree entity configured, and an opening degree entity that is not the `heating_position` of the quirk.

## Configuration of the plugin

Add the integration **Versatile Thermostat Quirks**, choose the VTherm, and accept the `Sonoff TRV-ZBT` quirk that is proposed for the TRV (detected from the manufacturer and the model). Check the four entities, prefilled from the device, then the options:

| Option | Default | Meaning |
|---|---|---|
| Normal temperature source (`temperature_source`) | `local` | Source restored after the kick. `local` is the built-in sensor. Use `remote` only if another automation feeds `external_temperature_input`: its last value is restored. |
| Temperature delta (`delta`) | 2 °C | Gap between the TRV setpoint and the fake temperature, from 1.2 to 5 °C. It must be higher than the temperature accuracy of the TRV. |
| Kick duration (`pulse_delay`) | 5 s | Time left to the TRV to react before the temperature source is restored, from 1 to 60 s. |
| Hot step (`hot_step`) | off | After the cold step, also send a fake temperature above the setpoint to force an idle transition. Enable it only if the valve does not reach the requested opening without it. |

## The kick

1. Read the setpoint of the TRV (`temperature` attribute). If it is missing, or if the TRV is `off` or unavailable, the kick is abandoned.
2. With `temperature_source` set to `remote`, save the value of `external_temperature_input`.
3. Write `external_temperature_input` = setpoint minus delta, limited to 0 to 99.9.
4. Switch the temperature source to external (select `remote_temperature`, or the ZHA switch on), if it is not already.
5. Wait `pulse_delay`.
6. With the hot step: write setpoint plus delta, then wait `pulse_delay` again.
7. Restore: the built-in sensor with the `local` source, or the saved value with the `remote` source.

The restoration of step 7 also runs when the kick is cancelled at any step.

Commands that arrive while a kick is running do not queue: they mark one pending kick, which starts at the end of the running one. A regulation cycle that sends the same opening again does not kick again. A repair or startup command always kicks. The forced strokes of the valve recalibration start no kick.

## Behavior around OFF

- When VTherm turns the TRV OFF, the quirk cancels the kick and the pending request, restores the temperature source and writes `idle_valve_position` = 0, all before `off` is sent to the TRV. The writes are confirmed before VTherm continues.
- When the TRV goes back from `off` to `heat`, the kick waits until the TRV reports its new state, because it has no effect while the TRV is off. The idle position is synchronized with the last opening at that moment.
- A TRV turned off on the device itself gets the same treatment.

At startup, a temperature source left external (for example after a restart during a kick) is restored when the `local` source is configured, and the idle position of an OFF TRV is set to 0.

## Migration from the blueprint

1. Turn off or delete the automation created from the blueprint.
2. Remove the closing degree entity from the VTherm if there is one, and use `heating_valve_position` as the opening degree entity.
3. Add the plugin entry for the VTherm and choose the same options as in the blueprint (they have the same names and defaults).
4. Check the repairs for remaining settings to fix.

## Troubleshooting

- The valve does not reach the requested opening: increase the kick duration, then try the hot step.
- The valve keeps heating after OFF: check that the VTherm is a recent release that calls the hooks, and that no repair issue is open for the quirk.
- Nothing happens: check the logs of `custom_components.vtherm_quirks`, and that the four entities of the quirk exist and are enabled.

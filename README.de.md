<p align="center"><img src="custom_components/vtherm_quirks/brand/logo@2x.png" alt="VTherm Quirks" width="400"></p>

# Versatile Thermostat Quirks

[![Lizenz: MIT](https://img.shields.io/badge/Lizenz-MIT-blue.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

*Read in English: [README.md](README.md) · Lire en français : [README.fr.md](README.fr.md)*

Manche Geräte verhalten sich nicht so, wie ein generischer Thermostat es erwartet. Ein Ventil, das eine neue Öffnung ignoriert, solange es im Ruhezustand ist, ein Thermostatventil (TRV), das beim Ausschalten in eine feste Position fährt, ein Sollwert, der nach einem Moduswechsel erneut gesendet werden muss. Das sind **Quirks**.

Diese Integration ist eine Sammlung von Quirks für [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm). VTherm bleibt generisch. Jedes besondere Gerät hat hier seinen eigenen Quirk, der über die Hooks der zugrunde liegenden Entitäten von [`vtherm_api`](https://github.com/jmcollin78/vtherm_api) an VTherm angebunden wird.

## Funktionsweise

VTherm ruft drei optionale Hooks rund um die Befehle auf, die es an seine zugrunde liegenden Entitäten sendet:

| Hook | Zeitpunkt |
|---|---|
| `async_before_underlying_hvac_mode` | Bevor `climate.set_hvac_mode` an ein zugrunde liegendes Climate gesendet wird |
| `async_after_valve_command` | Nachdem die Öffnungs- und Schließgrade gesendet wurden (`over_climate` mit direkter Ventilsteuerung) |
| `async_on_underlying_power` | Zu Beginn eines Zyklus, bei jeder Ventilaktualisierung und beim Stopp, für jede zugrunde liegende Entität |

Diese Integration registriert einen Feature-Manager pro VTherm. Der Manager leitet jeden Hook an den Quirk weiter, der an die betroffene Entität gebunden ist. Ein Quirk implementiert nur die Hooks, die er braucht, und ein Fehler in einem Quirk wird protokolliert, ohne VTherm oder die anderen Quirks zu stören.

## Quirks

| Quirk | Gerät | Integrationen | Seite |
|---|---|---|---|
| `eurotronic_spzb0001` | Eurotronic Spirit Zigbee (SPZB0001) | Zigbee2MQTT | [docs/quirks/eurotronic_spzb0001.md](docs/quirks/eurotronic_spzb0001.md) |
| `moes_trv601` | Moes TRV801 / Tuya TRV601 | Zigbee2MQTT | [docs/quirks/moes_trv601.md](docs/quirks/moes_trv601.md) |
| `sonoff_trv_zbt` | Sonoff TRV-ZBT (Gen2) | Zigbee2MQTT, ZHA | [docs/quirks/sonoff_trv_zbt.md](docs/quirks/sonoff_trv_zbt.md) |
| `sonoff_trvzb` | Sonoff TRVZB (Gen1) | Zigbee2MQTT, ZHA | [docs/quirks/sonoff_trvzb.md](docs/quirks/sonoff_trvzb.md) |
| `tuya_tv02` | Tuya TV02-Zigbee, Moes TV01-ZB | Zigbee2MQTT, ZHA | [docs/quirks/tuya_tv02.md](docs/quirks/tuya_tv02.md) |

Der Quirk `sonoff_trv_zbt` ersetzt den Blueprint [Sonoff TRV-ZBT valve kick](https://gist.github.com/bontiv/af2d59adb15bf3d522282edd662a75e4). Weitere Quirks sind willkommen, siehe [CONTRIBUTING.md](CONTRIBUTING.md) (auf Englisch). Die Seiten der Quirks sind auf Englisch.

## Voraussetzungen

- Home Assistant 2025.1 oder neuer.
- Eine Version von Versatile Thermostat, die die Hooks der zugrunde liegenden Entitäten aufruft, also eine Version, die von `vtherm_api` 0.10.0 oder neuer abhängt. Home Assistant installiert `vtherm_api` für diese Integration.

Wenn das installierte `vtherm_api` die Hooks nicht bereitstellt, wird die Integration nicht geladen, und ein Eintrag unter Reparaturen fordert zur Aktualisierung auf.

## Installation

Mit [HACS](https://hacs.xyz):

1. Öffne HACS, dann das Menü, dann **Benutzerdefinierte Repositories**.
2. Füge `https://github.com/bontiv/vtherm_quirks` mit der Kategorie **Integration** hinzu.
3. Installiere **Versatile Thermostat Quirks** und starte Home Assistant neu.

Manuell: Kopiere `custom_components/vtherm_quirks` in den Ordner `custom_components` deiner Home-Assistant-Konfiguration und starte neu.

## Konfiguration

Gehe zu **Einstellungen, Geräte & Dienste, Integration hinzufügen, Versatile Thermostat Quirks**. Es gibt einen Eintrag pro VTherm.

1. Wähle das VTherm. Es werden nur Thermostate angezeigt, die einen Quirk verwenden können, zum Beispiel ein `over_climate` mit direkter Ventilsteuerung für das TRV-ZBT.
2. Für jede zugrunde liegende Entität des VTherm sucht die Integration das Gerät und schlägt den passenden Quirk vor. Du kannst auch **Keiner** wählen oder einen anderen Quirk, der mit dem Thermostat kompatibel ist.
3. Prüfe für jede Entität mit einem Quirk die Entitäten (vom Gerät vorausgefüllt) und stelle die Optionen des Quirks ein.

Mit der Schaltfläche **Konfigurieren** des Eintrags kannst du diese Auswahl später ändern. Nur das VTherm des Eintrags wird neu geladen.

## Reparaturen

Jeder Quirk prüft die Konfiguration des VTherm und des Geräts und meldet unter den Reparaturen von Home Assistant, was ihn am Funktionieren hindert: eine VTherm-Einstellung, die mit dem Quirk in Konflikt steht, eine deaktivierte Entität, eine Gerätefunktion, die abgeschaltet werden muss. Die Meldungen verschwinden, sobald das Problem behoben ist. Ein Quirk, den es in der installierten Version nicht mehr gibt, wird ignoriert und gemeldet.

## Entwicklung

```bash
pip install -r requirements_test.txt
pytest
```

Die Tests verwenden `pytest-homeassistant`. Die Architektur und die Anleitung zum Hinzufügen eines Quirks stehen in [CONTRIBUTING.md](CONTRIBUTING.md) (auf Englisch).

## Lizenz

MIT, siehe [LICENSE](LICENSE).

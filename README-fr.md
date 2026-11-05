# Versatile Thermostat Quirks

[![Licence : MIT](https://img.shields.io/badge/Licence-MIT-blue.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

*Read in English: [README.md](README.md)*

Certains appareils ne se comportent pas comme un thermostat générique l'attend. Une vanne qui ignore une nouvelle ouverture tant qu'elle est au repos, une TRV qui se place sur une position fixe quand on l'éteint, une consigne à renvoyer après un changement de mode. Ce sont des **quirks**.

Cette intégration est une collection de quirks pour [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm). VTherm reste générique. Chaque appareil particulier a ici son propre quirk, branché sur VTherm grâce aux hooks des sous-jacents de [`vtherm_api`](https://github.com/jmcollin78/vtherm_api).

## Principe

VTherm appelle trois hooks facultatifs autour des commandes qu'il envoie à ses entités sous-jacentes :

| Hook | Moment |
|---|---|
| `async_before_underlying_hvac_mode` | Avant l'envoi de `climate.set_hvac_mode` à un climate sous-jacent |
| `async_after_valve_command` | Après l'envoi des degrés d'ouverture et de fermeture (`over_climate` à contrôle direct de la vanne) |
| `async_on_underlying_power` | Au début d'un cycle, à chaque mise à jour de vanne et à l'arrêt, pour chaque sous-jacent |

Cette intégration enregistre un feature manager par VTherm. Le manager transmet chaque hook au quirk lié au sous-jacent concerné. Un quirk n'implémente que les hooks dont il a besoin, et une erreur dans un quirk est journalisée sans gêner VTherm ni les autres quirks.

## Quirks

| Quirk | Appareil | Intégrations | Page |
|---|---|---|---|
| `sonoff_trv_zbt` | Sonoff TRV-ZBT (Gen2) | Zigbee2MQTT, ZHA | [docs/quirks/sonoff_trv_zbt.md](docs/quirks/sonoff_trv_zbt.md) |

Il remplace le blueprint [Sonoff TRV-ZBT valve kick](https://gist.github.com/bontiv/af2d59adb15bf3d522282edd662a75e4). Les autres quirks sont bienvenus, voir [CONTRIBUTING.md](CONTRIBUTING.md) (en anglais).

## Prérequis

- Home Assistant 2025.1 ou plus récent.
- Une version de Versatile Thermostat qui appelle les hooks des sous-jacents, c'est-à-dire une version qui dépend de `vtherm_api` 0.9.0 ou plus. Home Assistant installe `vtherm_api` pour cette intégration.

Si le `vtherm_api` installé ne fournit pas les hooks, l'intégration ne se charge pas et un problème dans les réparations demande la mise à jour.

## Installation

Avec [HACS](https://hacs.xyz) :

1. Ouvrez HACS, puis le menu, puis **Dépôts personnalisés**.
2. Ajoutez `https://github.com/bontiv/vtherm_quirks` avec la catégorie **Intégration**.
3. Installez **Versatile Thermostat Quirks** et redémarrez Home Assistant.

À la main : copiez `custom_components/vtherm_quirks` dans le dossier `custom_components` de votre configuration Home Assistant et redémarrez.

## Configuration

Allez dans **Paramètres, Appareils et services, Ajouter une intégration, Versatile Thermostat Quirks**. Il y a une entrée par VTherm.

1. Choisissez le VTherm. Seuls les thermostats qui peuvent utiliser un quirk sont listés, par exemple un `over_climate` à contrôle direct de la vanne pour la TRV-ZBT.
2. Pour chaque entité sous-jacente du VTherm, l'intégration cherche son appareil et propose le quirk qui lui correspond. Vous pouvez aussi choisir **Aucun**, ou un autre quirk compatible avec le thermostat.
3. Pour chaque sous-jacent avec un quirk, vérifiez les entités (préremplies depuis l'appareil) et réglez les options du quirk.

Le bouton **Configurer** de l'entrée permet de modifier ces choix plus tard. Seul le VTherm de l'entrée est rechargé.

## Réparations

Chaque quirk vérifie la configuration du VTherm et de l'appareil, et signale dans les réparations de Home Assistant ce qui l'empêche de fonctionner : un réglage du VTherm en conflit avec le quirk, une entité désactivée, une fonction de l'appareil à couper. Les problèmes disparaissent dès qu'ils sont corrigés. Un quirk qui n'existe plus dans la version installée est ignoré et signalé.

## Développement

```bash
pip install -r requirements_test.txt
pytest
```

Les tests utilisent `pytest-homeassistant-custom-component`. L'architecture et le guide d'ajout d'un quirk sont dans [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

MIT, voir [LICENSE](LICENSE).

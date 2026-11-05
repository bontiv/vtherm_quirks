"""Constants of the Versatile Thermostat Quirks integration."""

from __future__ import annotations

DOMAIN = "vtherm_quirks"
FACTORY_NAME = "vtherm_quirks"

# Domain of the Versatile Thermostat integration (the core).
VTHERM_DOMAIN = "versatile_thermostat"

# Config entry data of this integration. One entry per VTherm:
# {
#     "target_vtherm_unique_id": "<VTherm config entry id>",
#     "quirks": {
#         "<underlying entity id>": {
#             "quirk": "<quirk id>",
#             "entities": {"<role>": "<entity id>"},
#             "options": {"<option>": <value>},
#         }
#     },
# }
CONF_TARGET_VTHERM_UNIQUE_ID = "target_vtherm_unique_id"
CONF_QUIRKS = "quirks"
CONF_QUIRK = "quirk"
CONF_ENTITIES = "entities"
CONF_OPTIONS = "options"

# Value of the "no quirk" choice in the config flow.
QUIRK_NONE = "none"

# hass.data keys
DATA_FACTORY = "factory"
DATA_ISSUES = "issues"

# ---------------------------------------------------------------------------
# Versatile Thermostat configuration keys, read from the runtime `entry_infos`.
# They are duplicated here on purpose: the plugin never imports the private
# modules of the core. Keep them in sync with versatile_thermostat/const.py.
# ---------------------------------------------------------------------------
VT_CONF_NAME = "name"
VT_CONF_THERMOSTAT_TYPE = "thermostat_type"
VT_CONF_UNDERLYING_LIST = "underlying_entity_ids"
VT_CONF_AUTO_REGULATION_MODE = "auto_regulation_mode"
VT_CONF_OPENING_DEGREE_LIST = "opening_degree_entity_ids"
VT_CONF_CLOSING_DEGREE_LIST = "closing_degree_entity_ids"
VT_CONF_SYNC_DEVICE_INTERNAL_TEMP = "sync_device_internal_temp"

VT_THERMOSTAT_SWITCH = "thermostat_over_switch"
VT_THERMOSTAT_CLIMATE = "thermostat_over_climate"
VT_THERMOSTAT_VALVE = "thermostat_over_valve"
VT_THERMOSTAT_CENTRAL_CONFIG = "thermostat_central_config"

VT_AUTO_REGULATION_VALVE = "auto_regulation_valve"

# ---------------------------------------------------------------------------
# VTherm scopes and underlying types used by the quirks.
# ---------------------------------------------------------------------------
SCOPE_OVER_SWITCH = "over_switch"
SCOPE_OVER_VALVE = "over_valve"
SCOPE_OVER_CLIMATE = "over_climate"
# over_climate with the direct valve control (auto_regulation_valve)
SCOPE_OVER_CLIMATE_VALVE = "over_climate_valve"

UNDERLYING_CLIMATE = "climate"
UNDERLYING_SWITCH = "switch"
UNDERLYING_VALVE = "valve"

# ---------------------------------------------------------------------------
# HVAC modes as passed by the hooks (plain strings).
# ---------------------------------------------------------------------------
HVAC_OFF = "off"
STATE_UNAVAILABLE = "unavailable"
STATE_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Sonoff TRV-ZBT
#
# Verified = read in the sources of ZHA (zigpy/zha device dump of the TRV-ZBT
# and zigpy/zha-device-handlers zhaquirks/sonoff/trvzb.py) and of Zigbee2MQTT
# (zigbee-herdsman-converters src/devices/sonoff.ts).
# ---------------------------------------------------------------------------
TRV_ZBT_MANUFACTURER = "SONOFF"  # verified (ZHA and Z2M)
TRV_ZBT_MODEL = "TRV-ZBT"  # verified (ZHA model, Z2M zigbeeModel/model)

# Zigbee2MQTT: the exposes names are verified. The entity registry unique_id of
# a Z2M entity is "<ieee>_<expose name>_<base topic>" (from the Home Assistant
# MQTT discovery of Z2M, not re-read in the Z2M sources): it is matched by
# "contains _<name>_" or "ends with _<name>".
Z2M_PLATFORM = "mqtt"
Z2M_HEATING_POSITION = "heating_valve_position"  # verified
Z2M_IDLE_POSITION = "idle_valve_position"  # verified
Z2M_EXTERNAL_TEMPERATURE = "external_temperature_input"  # verified
Z2M_SENSOR_SELECT = "temperature_sensor_select"  # verified
Z2M_SENSOR_LOCAL_OPTION = "local_temperature"  # verified
Z2M_SENSOR_REMOTE_OPTION = "remote_temperature"  # verified
Z2M_ADAPTIVE_CONTROL = "smart_temperature_control"  # name taken from the Z2M docs, not re-read
Z2M_OPEN_WINDOW = "open_window"  # verified

# ZHA: translation keys of the entities (verified in the TRV-ZBT device dump and
# in the quirk). The attribute names are the unique_id suffix of these entities
# (ZHA unique_id = "<ieee>-<endpoint>-<cluster>-<attribute>"): used as a
# fallback only, not verified against a real entity registry.
ZHA_PLATFORM = "zha"
ZHA_HEATING_POSITION = "valve_opening_degree"  # verified: "Valve opening degree"
ZHA_IDLE_POSITION = "valve_closing_degree"  # verified: "Valve closing degree"
ZHA_EXTERNAL_TEMPERATURE = "external_temperature_sensor_value"  # verified
ZHA_SENSOR_SWITCH = "external_temperature_sensor"  # verified (a switch)
ZHA_SENSOR_SWITCH_ATTRIBUTE = "external_temperature_sensor_enable"  # fallback, quirk attribute
ZHA_ADAPTIVE_CONTROL = "adaptive_mode"  # verified (a switch, on = adaptive)
ZHA_OPEN_WINDOW = "open_window"  # verified (a switch)

# Quirk options
OPT_TEMPERATURE_SOURCE = "temperature_source"
OPT_DELTA = "delta"
OPT_PULSE_DELAY = "pulse_delay"
OPT_HOT_STEP = "hot_step"

TEMPERATURE_SOURCE_LOCAL = "local"
TEMPERATURE_SOURCE_REMOTE = "remote"

DEFAULT_DELTA = 2.0
MIN_DELTA = 1.2
MAX_DELTA = 5.0
DEFAULT_PULSE_DELAY = 5
MIN_PULSE_DELAY = 1
MAX_PULSE_DELAY = 60
# Range of external_temperature_input of the TRV-ZBT.
TRV_ZBT_MIN_EXTERNAL_TEMPERATURE = 0.0
TRV_ZBT_MAX_EXTERNAL_TEMPERATURE = 99.9

# Values of ValveCommandCause (a StrEnum in vtherm_api) compared as strings so
# that the quirks do not depend on the import of the enum.
CAUSE_REGULATION = "regulation"
CAUSE_TURN_OFF = "turn_off"
CAUSE_STARTUP = "startup"
CAUSE_REPAIR = "repair"

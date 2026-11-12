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
VT_CONF_MAX_CLOSING_DEGREE = "max_closing_degree"
VT_CONF_MAX_OPENING_DEGREES = "max_opening_degrees"
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

# Values of ValveCommandCause (a StrEnum in vtherm_api) compared as strings so
# that the quirks do not depend on the import of the enum.
CAUSE_REGULATION = "regulation"
CAUSE_TURN_OFF = "turn_off"
CAUSE_STARTUP = "startup"
CAUSE_REPAIR = "repair"
CAUSE_RECALIBRATION = "recalibration"

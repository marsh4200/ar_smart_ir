from __future__ import annotations

DOMAIN = "ar_smart_ir"
VERSION = "1.2.1"

CONF_PLATFORM = "platform"
CONF_UNIQUE_ID = "unique_id"
CONF_DEVICE_CODE = "device_code"
CONF_CONTROLLER = "controller"
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_POWER_SENSOR = "power_sensor"
CONF_POWER_SENSOR_RESTORE_STATE = "power_sensor_restore_state"
CONF_SOURCE_NAMES = "source_names"
CONF_DEVICE_CLASS = "device_class"
CONF_COMMAND_OVERRIDES = "command_overrides"
CONF_OVERRIDE_COMMAND = "override_command"
CONF_OVERRIDE_TYPE = "override_type"
CONF_OVERRIDE_REPEAT_COUNT = "override_repeat_count"
CONF_OVERRIDE_REPEAT_DELAY = "override_repeat_delay_secs"
CONF_OVERRIDE_STEP_DELAY = "override_step_delay_secs"
CONF_OVERRIDE_SEQUENCE_STEP_1 = "override_sequence_step_1"
CONF_OVERRIDE_SEQUENCE_STEP_2 = "override_sequence_step_2"
CONF_OVERRIDE_SEQUENCE_STEP_3 = "override_sequence_step_3"
CONF_OVERRIDE_SEQUENCE_STEP_4 = "override_sequence_step_4"
CONF_OVERRIDE_SEQUENCE_STEP_5 = "override_sequence_step_5"
CONF_OVERRIDE_REMOVE = "override_remove"
CONF_GO_BACK = "go_back"
CONF_TEST_COMMAND = "test_command"
CONF_TEST_DEVICE = "test_device"

# Learn command
CONF_LEARN_COMMAND = "learn_command"
CONF_LEARN_BROADLINK_ENTITY = "learn_broadlink_entity"
CONF_LEARN_TIMEOUT = "learn_timeout"

DEFAULT_DELAY = 0.5
DEFAULT_DEVICE_CLASS = "tv"

PLATFORMS = ["climate", "fan", "light", "media_player"]

PLATFORM_TITLES = {
    "climate": "Climate",
    "fan": "Fan",
    "light": "Light",
    "media_player": "Media Player",
}

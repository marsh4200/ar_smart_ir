import asyncio
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
    HVAC_MODES,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    PRECISION_WHOLE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_COMMAND_OVERRIDES,
    CONF_CONTROLLER,
    CONF_HUMIDITY_SENSOR,
    CONF_POWER_SENSOR,
    CONF_POWER_SENSOR_RESTORE_STATE,
    CONF_TEMPERATURE_SENSOR,
)
from .controller import get_controller
from .helpers import async_load_device_data

_LOGGER = logging.getLogger(__name__)

CONF_UNIQUE_ID = "unique_id"
CONF_NAME = "name"
CONF_DEVICE_CODE = "device_code"
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"

DEFAULT_DELAY = 0.5
SENSOR_STATES_INVALID = {STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""}

SUPPORT_FLAGS = (
    ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.FAN_MODE
)


async def async_setup_entry(hass, entry, async_add_entities):
    config = {**entry.data, **entry.options}

    device_code = config.get(CONF_DEVICE_CODE)

    device_data = await async_load_device_data(
        device_code,
        "climate",
        config.get(CONF_COMMAND_OVERRIDES),
    )

    entity = SmartIRClimate(hass, config, device_data)

    async_add_entities([entity], update_before_add=True)


class SmartIRClimate(ClimateEntity, RestoreEntity):
    def __init__(self, hass, config, device_data):
        self.hass = hass

        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)

        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY, DEFAULT_DELAY)
        self._temperature_sensor = config.get(CONF_TEMPERATURE_SENSOR)
        self._humidity_sensor = config.get(CONF_HUMIDITY_SENSOR)
        self._power_sensor = config.get(CONF_POWER_SENSOR)
        self._power_sensor_restore_state = config.get(
            CONF_POWER_SENSOR_RESTORE_STATE,
            False,
        )

        self._supported_controller = config.get(
            CONF_CONTROLLER,
            device_data["supportedController"],
        )
        self._commands_encoding = device_data["commandsEncoding"]

        self._manufacturer = device_data["manufacturer"]
        self._supported_models = device_data["supportedModels"]
        self._min_temperature = device_data["minTemperature"]
        self._max_temperature = device_data["maxTemperature"]
        self._precision = device_data["precision"]

        valid_modes = [x for x in device_data["operationModes"] if x in HVAC_MODES]

        self._operation_modes = [HVACMode.OFF] + valid_modes

        self._fan_modes = device_data["fanModes"]
        self._swing_modes = device_data.get("swingModes")

        self._commands = device_data["commands"]

        self._target_temperature = self._min_temperature
        self._hvac_mode = HVACMode.OFF

        self._current_fan_mode = self._fan_modes[0]
        self._current_swing_mode = None
        self._last_on_operation = None

        self._current_temperature = None
        self._current_humidity = None

        self._support_flags = SUPPORT_FLAGS
        self._support_swing = False

        if self._swing_modes:
            self._support_flags |= ClimateEntityFeature.SWING_MODE
            self._current_swing_mode = self._swing_modes[0]
            self._support_swing = True

        self._temp_lock = asyncio.Lock()
        self._on_by_remote = False

        self._controller = get_controller(
            hass,
            self._supported_controller,
            self._commands_encoding,
            self._controller_data,
            self._delay,
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()

        if last_state:
            self._hvac_mode = last_state.state
            self._target_temperature = last_state.attributes.get(
                "temperature",
                self._target_temperature,
            )
            self._current_fan_mode = last_state.attributes.get(
                "fan_mode",
                self._current_fan_mode,
            )
            self._current_swing_mode = last_state.attributes.get(
                "swing_mode",
                self._current_swing_mode,
            )
            self._last_on_operation = last_state.attributes.get("last_on_operation")

        self._update_current_temperature()
        self._update_current_humidity()

        tracked_entities = [
            entity_id
            for entity_id in (self._temperature_sensor, self._humidity_sensor)
            if entity_id
        ]
        if tracked_entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    tracked_entities,
                    self._async_sensor_state_changed,
                )
            )

        if self._power_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    self._power_sensor,
                    self._async_power_sensor_changed,
                )
            )

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        return self._name

    @property
    def temperature_unit(self):
        return UnitOfTemperature.CELSIUS

    @property
    def should_poll(self):
        return False

    @property
    def hvac_mode(self):
        return self._hvac_mode

    @property
    def hvac_modes(self):
        return self._operation_modes

    @property
    def target_temperature(self):
        return self._target_temperature

    @property
    def current_temperature(self):
        return self._current_temperature

    @property
    def current_humidity(self):
        return self._current_humidity

    @property
    def min_temp(self):
        return self._min_temperature

    @property
    def max_temp(self):
        return self._max_temperature

    @property
    def fan_modes(self):
        return self._fan_modes

    @property
    def fan_mode(self):
        return self._current_fan_mode

    @property
    def swing_modes(self):
        return self._swing_modes

    @property
    def swing_mode(self):
        return self._current_swing_mode

    @property
    def supported_features(self):
        return self._support_flags

    @property
    def extra_state_attributes(self):
        return {
            "last_on_operation": self._last_on_operation,
            "device_code": self._device_code,
            "manufacturer": self._manufacturer,
            "supported_models": self._supported_models,
            "supported_controller": self._supported_controller,
            "commands_encoding": self._commands_encoding,
        }

    @callback
    def _async_sensor_state_changed(self, event):
        entity_id = event.data.get("entity_id")

        if entity_id == self._temperature_sensor:
            self._update_current_temperature()

        if entity_id == self._humidity_sensor:
            self._update_current_humidity()

        self.async_write_ha_state()

    def _update_current_temperature(self):
        self._current_temperature = self._get_temperature_sensor_value()

    def _update_current_humidity(self):
        self._current_humidity = self._get_sensor_numeric_state(self._humidity_sensor)

    def _get_temperature_sensor_value(self):
        if not self._temperature_sensor:
            return None

        state = self.hass.states.get(self._temperature_sensor)
        if state is None or state.state in SENSOR_STATES_INVALID:
            return None

        try:
            value = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Unable to parse temperature sensor state for %s: %s",
                self._temperature_sensor,
                state.state,
            )
            return None

        sensor_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if sensor_unit == UnitOfTemperature.FAHRENHEIT:
            return round(
                TemperatureConverter.convert(
                    value,
                    UnitOfTemperature.FAHRENHEIT,
                    UnitOfTemperature.CELSIUS,
                ),
                1,
            )

        return value

    def _get_sensor_numeric_state(self, entity_id):
        if not entity_id:
            return None

        state = self.hass.states.get(entity_id)
        if state is None or state.state in SENSOR_STATES_INVALID:
            return None

        try:
            return float(state.state)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Unable to parse sensor state for %s: %s",
                entity_id,
                state.state,
            )
            return None

    async def async_set_temperature(self, **kwargs):
        temperature = kwargs.get(ATTR_TEMPERATURE)

        if temperature is None:
            return

        if self._precision == PRECISION_WHOLE:
            self._target_temperature = round(temperature)
        else:
            self._target_temperature = round(temperature, 1)

        if self._hvac_mode != HVACMode.OFF:
            await self.send_command()

        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode):
        self._hvac_mode = hvac_mode
        if hvac_mode != HVACMode.OFF:
            self._last_on_operation = hvac_mode

        await self.send_command()

        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode):
        self._current_fan_mode = fan_mode

        if self._hvac_mode != HVACMode.OFF:
            await self.send_command()

        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode):
        self._current_swing_mode = swing_mode

        if self._hvac_mode != HVACMode.OFF:
            await self.send_command()

        self.async_write_ha_state()

    async def async_turn_on(self):
        target_mode = self._last_on_operation
        if target_mode is None:
            target_mode = self._operation_modes[1] if len(self._operation_modes) > 1 else HVACMode.COOL
        await self.async_set_hvac_mode(target_mode)

    async def async_turn_off(self):
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def send_command(self):
        async with self._temp_lock:
            try:
                operation_mode = self._hvac_mode
                fan_mode = self._current_fan_mode
                temp = f"{self._target_temperature:g}"

                if operation_mode == HVACMode.OFF:
                    await self._controller.send(self._commands["off"])
                    return

                if "on" in self._commands:
                    await self._controller.send(self._commands["on"])
                    await asyncio.sleep(self._delay)

                if self._support_swing:
                    await self._controller.send(
                        self._commands[operation_mode][fan_mode][self._current_swing_mode][temp]
                    )
                else:
                    await self._controller.send(
                        self._commands[operation_mode][fan_mode][temp]
                    )

            except Exception as err:
                _LOGGER.exception("SmartIR send command failed: %s", err)

    @callback
    def _async_power_sensor_changed(self, event):
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        old_state = event.data.get("old_state")
        if old_state is not None and new_state.state == old_state.state:
            return

        if new_state.state == "on" and self._hvac_mode == HVACMode.OFF:
            self._on_by_remote = True
            if self._power_sensor_restore_state and self._last_on_operation is not None:
                self._hvac_mode = self._last_on_operation
            else:
                self._hvac_mode = (
                    self._operation_modes[1]
                    if len(self._operation_modes) > 1
                    else HVACMode.COOL
                )
            self.async_write_ha_state()
        elif new_state.state == "off":
            self._on_by_remote = False
            if self._hvac_mode != HVACMode.OFF:
                self._hvac_mode = HVACMode.OFF
            self.async_write_ha_state()

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
    CONF_PASSIVE_MODE,
    CONF_POWER_SENSOR,
    CONF_POWER_SENSOR_RESTORE_STATE,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_UNIT,
    TEMPERATURE_UNIT_AUTO,
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
PRESET_NONE = "none"

# No Celsius air-conditioner codeset goes anywhere near this; every Fahrenheit
# one starts well above it. Used to infer the unit of a codeset that declares
# none, and to reject a user override the codeset's own range contradicts.
FAHRENHEIT_INFERENCE_THRESHOLD = 45
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


def _normalise_unit(value):
    """Map a loose unit string onto a UnitOfTemperature, or None."""
    if value is None:
        return None

    token = str(value).strip().upper().lstrip("°")

    if token in {"F", "FAHRENHEIT", "DEGF", "\u00b0F"}:
        return UnitOfTemperature.FAHRENHEIT
    if token in {"C", "CELSIUS", "CENTIGRADE", "DEGC", "\u00b0C"}:
        return UnitOfTemperature.CELSIUS

    return None


def _codeset_max_temperature(device_data):
    """The codeset's top setpoint as a float, or None if unusable."""
    try:
        return float(device_data["maxTemperature"])
    except (KeyError, TypeError, ValueError):
        return None


def _override_contradicts_codeset(override, device_data):
    """True if an override cannot possibly describe this codeset's range.

    A codeset's own numbers give its unit away. No air conditioner has a
    Fahrenheit setpoint range topping out below ~45 °F, and none has a Celsius
    range reaching it. An override that contradicts the range this badly is a
    misconfiguration rather than an intent, and obeying it does real damage:
    a 16-30 °C codeset forced to Fahrenheit renders as -8.9 to -1.1 °C, and
    every setpoint the user then picks is converted back into a *different*
    but still valid codeset key, so the wrong command is transmitted silently.
    """
    maximum = _codeset_max_temperature(device_data)
    if maximum is None:
        return False

    if override == UnitOfTemperature.FAHRENHEIT:
        return maximum < FAHRENHEIT_INFERENCE_THRESHOLD

    return maximum >= FAHRENHEIT_INFERENCE_THRESHOLD


def resolve_temperature_unit(device_data, config=None):
    """Work out what unit a climate codeset's temperatures are actually in.

    Precedence (issue #33):
      1. The user's explicit override in the config entry, unless the
         codeset's range flatly contradicts it.
      2. A "temperatureUnit" key in the codeset JSON.
      3. Inference from the temperature range.
      4. Celsius, matching every codeset shipped before 1.7.1.
    """
    if config:
        override = _normalise_unit(
            config.get(CONF_TEMPERATURE_UNIT, TEMPERATURE_UNIT_AUTO)
        )
        if override is not None:
            if not _override_contradicts_codeset(override, device_data):
                return override

            _LOGGER.warning(
                "Ignoring the '%s' temperature unit override for the %s codeset: "
                "its setpoints run %s-%s, which cannot be %s. This option "
                "declares the unit the codeset's own numbers are written in - it "
                "does not change how temperatures are displayed. For that, use "
                "Home Assistant's unit system or the entity's unit override, and "
                "set this option back to '%s'.",
                override,
                device_data.get("manufacturer", "unknown"),
                device_data.get("minTemperature"),
                device_data.get("maxTemperature"),
                override,
                TEMPERATURE_UNIT_AUTO,
            )

    declared = _normalise_unit(
        device_data.get("temperatureUnit") or device_data.get("temperature_unit")
    )
    if declared is not None:
        return declared

    # Nothing declared: a codeset topping out above ~45 can only be Fahrenheit.
    maximum = _codeset_max_temperature(device_data)
    if maximum is not None and maximum >= FAHRENHEIT_INFERENCE_THRESHOLD:
        return UnitOfTemperature.FAHRENHEIT

    return UnitOfTemperature.CELSIUS


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

        # The unit the codeset's own temperature keys are written in (issue
        # #33). Reporting this as the entity's native unit stops Home Assistant
        # treating a Fahrenheit codeset as Celsius and converting it again.
        self._temperature_unit = resolve_temperature_unit(device_data, config)

        valid_modes = [x for x in device_data["operationModes"] if x in HVAC_MODES]

        self._operation_modes = [HVACMode.OFF] + valid_modes

        self._fan_modes = device_data["fanModes"]
        self._swing_modes = device_data.get("swingModes")

        # Optional AC presets (issue #28). Older codesets without a
        # "presetModes" key are completely unaffected.
        raw_presets = device_data.get("presetModes") or []
        self._preset_modes = None
        if raw_presets:
            presets = [p for p in raw_presets if p != PRESET_NONE]
            self._preset_modes = [PRESET_NONE] + presets

        self._commands = device_data["commands"]

        self._target_temperature = self._min_temperature
        self._hvac_mode = HVACMode.OFF

        self._current_fan_mode = self._fan_modes[0]
        self._current_swing_mode = None
        self._current_preset_mode = PRESET_NONE if self._preset_modes else None
        self._last_on_operation = None

        # Passive mode (issue #31): when enabled, identical state commands are
        # never re-sent, so the AC unit's own thermostat manages temperature.
        self._passive_mode = bool(config.get(CONF_PASSIVE_MODE, False))
        self._last_sent_state = None

        self._current_temperature = None
        self._current_humidity = None

        self._support_flags = SUPPORT_FLAGS
        self._support_swing = False

        if self._swing_modes:
            self._support_flags |= ClimateEntityFeature.SWING_MODE
            self._current_swing_mode = self._swing_modes[0]
            self._support_swing = True

        if self._preset_modes:
            self._support_flags |= ClimateEntityFeature.PRESET_MODE

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
            self._target_temperature = self._restore_target_temperature(
                last_state.attributes.get("temperature")
            )
            self._current_fan_mode = last_state.attributes.get(
                "fan_mode",
                self._current_fan_mode,
            )
            self._current_swing_mode = last_state.attributes.get(
                "swing_mode",
                self._current_swing_mode,
            )
            if self._preset_modes:
                restored_preset = last_state.attributes.get("preset_mode")
                if restored_preset in self._preset_modes:
                    self._current_preset_mode = restored_preset
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

    def _restore_target_temperature(self, value):
        """Bring a restored target temperature back into the codeset's unit and range.

        Home Assistant serialises a climate entity's "temperature" attribute
        in the *display* unit (hass.config.units.temperature_unit), not the
        entity's native unit, so on an imperial system a 22 °C target is
        stored as 72. Convert it back into the codeset's unit before doing
        anything else; without that, 72 reads as 72 °C and clamps to the
        codeset maximum.

        A state saved by an older version - or before the unit override was
        changed - can still be in a different unit entirely (issue #33: a
        65 °C target was written out as 149 °F). Sending that would look up a
        command key that doesn't exist, so clamp it into the codeset's own
        range instead of failing on the next command.
        """
        if value is None:
            return self._target_temperature

        try:
            value = float(value)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "%s: unusable restored target temperature %s, using %s",
                self._name,
                value,
                self._min_temperature,
            )
            return self._min_temperature

        display_unit = self.hass.config.units.temperature_unit
        if display_unit != self._temperature_unit:
            value = TemperatureConverter.convert(
                value,
                display_unit,
                self._temperature_unit,
            )

        if self._precision == PRECISION_WHOLE:
            value = round(value)
        else:
            value = round(value, 1)

        clamped = min(max(value, self._min_temperature), self._max_temperature)

        if clamped != value:
            _LOGGER.info(
                "%s: restored target temperature %s is outside the codeset "
                "range %s-%s %s, clamping to %s",
                self._name,
                value,
                self._min_temperature,
                self._max_temperature,
                self._temperature_unit,
                clamped,
            )

        return clamped

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        return self._name

    @property
    def temperature_unit(self):
        return self._temperature_unit

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
    def precision(self):
        return self._precision

    @property
    def target_temperature_step(self):
        return self._precision

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
    def preset_modes(self):
        return self._preset_modes

    @property
    def preset_mode(self):
        return self._current_preset_mode

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
            "passive_mode": self._passive_mode,
            "codeset_temperature_unit": self._temperature_unit,
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

        sensor_unit = _normalise_unit(
            state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        )

        # Convert the sensor into the codeset's unit, not blindly into Celsius.
        if sensor_unit is not None and sensor_unit != self._temperature_unit:
            return round(
                TemperatureConverter.convert(
                    value,
                    sensor_unit,
                    self._temperature_unit,
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

    async def async_set_preset_mode(self, preset_mode):
        if not self._preset_modes or preset_mode not in self._preset_modes:
            return

        self._current_preset_mode = preset_mode

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

    def _resolve_state_command(self, operation_mode, fan_mode, temp):
        """Resolve the command for the current state.

        Standard layout (unchanged): commands[mode][fan][(swing)][temp]
        With presets (optional):     commands[mode][fan][(swing)][preset][temp]

        Codesets without a preset layer keep working exactly as before.
        """
        node = self._commands[operation_mode][fan_mode]

        if self._support_swing:
            node = node[self._current_swing_mode]

        if self._preset_modes:
            preset = self._current_preset_mode or PRESET_NONE
            if isinstance(node, dict):
                if preset in node and isinstance(node[preset], dict):
                    node = node[preset]
                elif PRESET_NONE in node and isinstance(node[PRESET_NONE], dict):
                    node = node[PRESET_NONE]
                # else: codeset has no preset layer here — fall through to temps

        return node[temp]

    async def send_command(self):
        async with self._temp_lock:
            try:
                operation_mode = self._hvac_mode
                fan_mode = self._current_fan_mode
                temp = f"{self._target_temperature:g}"

                if operation_mode == HVACMode.OFF:
                    await self._controller.send(self._commands["off"])
                    self._last_sent_state = None
                    return

                state_key = (
                    operation_mode,
                    fan_mode,
                    self._current_swing_mode,
                    self._current_preset_mode,
                    temp,
                )

                if self._passive_mode and state_key == self._last_sent_state:
                    # Passive mode: nothing changed, let the AC unit's own
                    # thermostat do its job — don't re-send.
                    return

                if "on" in self._commands:
                    # In passive mode only send the discrete "on" when we are
                    # actually turning the unit on, not on every adjustment.
                    if not self._passive_mode or self._last_sent_state is None:
                        await self._controller.send(self._commands["on"])
                        await asyncio.sleep(self._delay)

                await self._controller.send(
                    self._resolve_state_command(operation_mode, fan_mode, temp)
                )

                self._last_sent_state = state_key

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

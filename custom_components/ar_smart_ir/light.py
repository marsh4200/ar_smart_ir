import asyncio
import logging

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .controller import get_controller
from .helpers import async_load_device_data
from .const import CONF_COMMAND_OVERRIDES, CONF_CONTROLLER

_LOGGER = logging.getLogger(__name__)

CONF_UNIQUE_ID = "unique_id"
CONF_NAME = "name"
CONF_DEVICE_CODE = "device_code"
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_POWER_SENSOR = "power_sensor"

DEFAULT_DELAY = 0.5

CMD_BRIGHTNESS_INCREASE = "brighten"
CMD_BRIGHTNESS_DECREASE = "dim"
CMD_COLORMODE_COLDER = "colder"
CMD_COLORMODE_WARMER = "warmer"
CMD_POWER_ON = "on"
CMD_POWER_OFF = "off"
CMD_NIGHTLIGHT = "night"


def closest_match(value, options):
    prev_val = None
    for index, entry in enumerate(options):
        if entry > (value or 0):
            if prev_val is None:
                return index
            diff_lo = value - prev_val
            diff_hi = entry - value
            if diff_lo < diff_hi:
                return index - 1
            return index
        prev_val = entry

    return len(options) - 1


async def async_setup_entry(hass, entry, async_add_entities):

    config = {**entry.data, **entry.options}

    device_code = config.get(CONF_DEVICE_CODE)

    device_data = await async_load_device_data(
        device_code,
        "light",
        config.get(CONF_COMMAND_OVERRIDES),
    )

    async_add_entities(
        [
            SmartIRLight(
                hass,
                config,
                device_data,
            )
        ],
        True,
    )


class SmartIRLight(LightEntity, RestoreEntity):

    def __init__(self, hass, config, device_data):

        self.hass = hass

        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)

        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY, DEFAULT_DELAY)
        self._power_sensor = config.get(CONF_POWER_SENSOR)

        self._supported_controller = config.get(
            CONF_CONTROLLER,
            device_data["supportedController"],
        )
        self._commands_encoding = device_data["commandsEncoding"]

        self._manufacturer = device_data["manufacturer"]
        self._supported_models = device_data["supportedModels"]
        self._brightnesses = device_data.get("brightness")
        self._colortemps = device_data.get("colorTemperature")
        self._commands = device_data["commands"]

        self._power = STATE_ON
        self._brightness = None
        self._colortemp = None

        self._on_by_remote = False
        self._color_mode = ColorMode.UNKNOWN

        if self._colortemps:
            self._colortemp = self.max_color_temp_kelvin
            self._color_mode = ColorMode.COLOR_TEMP

        if (
            CMD_NIGHTLIGHT in self._commands
            or (
                CMD_BRIGHTNESS_INCREASE in self._commands
                and CMD_BRIGHTNESS_DECREASE in self._commands
            )
        ):
            self._brightness = 100
            self._support_brightness = True
            if self._color_mode == ColorMode.UNKNOWN:
                self._color_mode = ColorMode.BRIGHTNESS
        else:
            self._support_brightness = False

        if (
            CMD_POWER_OFF in self._commands
            and CMD_POWER_ON in self._commands
            and self._color_mode == ColorMode.UNKNOWN
        ):
            self._color_mode = ColorMode.ONOFF

        if self._brightnesses and self._color_mode == ColorMode.ONOFF:
            self._color_mode = ColorMode.BRIGHTNESS

        self._temp_lock = asyncio.Lock()

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
            self._power = last_state.state

            if ATTR_BRIGHTNESS in last_state.attributes:
                self._brightness = last_state.attributes[ATTR_BRIGHTNESS]

            if ATTR_COLOR_TEMP_KELVIN in last_state.attributes:
                self._colortemp = last_state.attributes[ATTR_COLOR_TEMP_KELVIN]

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
    def supported_color_modes(self):
        return [self._color_mode]

    @property
    def color_mode(self):
        return self._color_mode

    @property
    def is_on(self):
        return self._power == STATE_ON or self._on_by_remote

    @property
    def brightness(self):
        return self._brightness

    @property
    def color_temp_kelvin(self):
        return self._colortemp

    @property
    def extra_state_attributes(self):
        return {
            "device_code": self._device_code,
            "manufacturer": self._manufacturer,
            "supported_models": self._supported_models,
            "supported_controller": self._supported_controller,
            "commands_encoding": self._commands_encoding,
            "on_by_remote": self._on_by_remote,
        }

    @property
    def min_color_temp_kelvin(self):
        if self._colortemps:
            return self._colortemps[0]
        return None

    @property
    def max_color_temp_kelvin(self):
        if self._colortemps:
            return self._colortemps[-1]
        return None

    async def async_turn_on(self, **kwargs):
        did_something = False

        if self._power != STATE_ON and not self._on_by_remote:
            await self.send_command(CMD_POWER_ON)
            self._power = STATE_ON
            did_something = True

        if (
            ATTR_COLOR_TEMP_KELVIN in kwargs
            and self._colortemps
            and CMD_COLORMODE_COLDER in self._commands
            and CMD_COLORMODE_WARMER in self._commands
        ):
            target = kwargs[ATTR_COLOR_TEMP_KELVIN]
            old_step = closest_match(self._colortemp, self._colortemps)
            new_step = closest_match(target, self._colortemps)
            steps = new_step - old_step
            if steps != 0:
                did_something = True
                if steps < 0:
                    command = CMD_COLORMODE_WARMER
                    steps = abs(steps)
                else:
                    command = CMD_COLORMODE_COLDER

                if new_step in (0, len(self._colortemps) - 1):
                    steps = len(self._colortemps)

                self._colortemp = self._colortemps[new_step]
                await self.send_command(command, steps)

        if ATTR_BRIGHTNESS in kwargs and self._support_brightness:
            target = kwargs[ATTR_BRIGHTNESS]

            if target == 1 and CMD_NIGHTLIGHT in self._commands:
                self._brightness = 1
                self._power = STATE_ON
                did_something = True
                await self.send_command(CMD_NIGHTLIGHT)
            elif self._brightnesses:
                old_step = closest_match(self._brightness, self._brightnesses)
                new_step = closest_match(target, self._brightnesses)
                steps = new_step - old_step
                if steps != 0:
                    did_something = True
                    if steps < 0:
                        command = CMD_BRIGHTNESS_DECREASE
                        steps = abs(steps)
                    else:
                        command = CMD_BRIGHTNESS_INCREASE

                    if new_step in (0, len(self._brightnesses) - 1):
                        steps = len(self._brightnesses)

                    self._brightness = self._brightnesses[new_step]
                    await self.send_command(command, steps)

        if not did_something and not self._on_by_remote:
            self._power = STATE_ON
            await self.send_command(CMD_POWER_ON)

        self.async_write_ha_state()

    async def async_turn_off(self):
        await self.send_command(CMD_POWER_OFF)

        self._power = STATE_OFF

        self.async_write_ha_state()

    async def async_toggle(self):
        await (self.async_turn_on() if not self.is_on else self.async_turn_off())

    async def send_command(self, command_name, count=1):
        command = self._commands.get(command_name)
        if command is None:
            _LOGGER.error("Unknown command '%s'", command_name)
            return

        async with self._temp_lock:
            self._on_by_remote = False
            try:
                for _ in range(max(1, count)):
                    await self._controller.send(command)
            except Exception as err:
                _LOGGER.exception(err)

    @callback
    def _async_power_sensor_changed(self, event):
        new_state = event.data["new_state"]
        if new_state is None:
            return

        old_state = event.data["old_state"]
        if old_state is not None and new_state.state == old_state.state:
            return

        if new_state.state == STATE_ON:
            self._on_by_remote = True
            self.async_write_ha_state()
        elif new_state.state == STATE_OFF:
            self._on_by_remote = False
            self._power = STATE_OFF
            self.async_write_ha_state()

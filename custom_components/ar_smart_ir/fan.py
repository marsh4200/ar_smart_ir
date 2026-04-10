import asyncio
import logging

from homeassistant.components.fan import (
    FanEntity,
    FanEntityFeature,
    DIRECTION_REVERSE,
    DIRECTION_FORWARD,
)

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

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
SPEED_OFF = "off"


async def async_setup_entry(hass, entry, async_add_entities):

    config = {**entry.data, **entry.options}

    device_code = config.get(CONF_DEVICE_CODE)

    device_data = await async_load_device_data(
        device_code,
        "fan",
        config.get(CONF_COMMAND_OVERRIDES),
    )

    async_add_entities(
        [
            SmartIRFan(
                hass,
                config,
                device_data,
            )
        ],
        True,
    )


class SmartIRFan(FanEntity, RestoreEntity):

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
        self._speed_list = device_data["speed"]
        self._commands = device_data["commands"]

        self._speed = SPEED_OFF
        self._direction = None
        self._last_on_speed = None
        self._oscillating = False
        self._on_by_remote = False

        self._support_flags = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )

        if (
            DIRECTION_REVERSE in self._commands
            and DIRECTION_FORWARD in self._commands
        ):
            self._direction = DIRECTION_FORWARD
            self._support_flags |= FanEntityFeature.DIRECTION

        if "oscillate" in self._commands:
            self._support_flags |= FanEntityFeature.OSCILLATE

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
            if "speed" in last_state.attributes:
                self._speed = last_state.attributes["speed"]
            if (
                "direction" in last_state.attributes
                and self._support_flags & FanEntityFeature.DIRECTION
            ):
                self._direction = last_state.attributes["direction"]
            if "last_on_speed" in last_state.attributes:
                self._last_on_speed = last_state.attributes["last_on_speed"]
            if "oscillating" in last_state.attributes:
                self._oscillating = last_state.attributes["oscillating"]

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
    def percentage(self):

        if self._speed == SPEED_OFF:
            return 0

        return ordered_list_item_to_percentage(
            self._speed_list,
            self._speed,
        )

    @property
    def speed_count(self):
        return len(self._speed_list)

    @property
    def is_on(self):
        return self._on_by_remote or self._speed != SPEED_OFF

    @property
    def oscillating(self):
        return self._oscillating

    @property
    def current_direction(self):
        return self._direction

    @property
    def extra_state_attributes(self):
        return {
            "last_on_speed": self._last_on_speed,
            "device_code": self._device_code,
            "manufacturer": self._manufacturer,
            "supported_models": self._supported_models,
            "supported_controller": self._supported_controller,
            "commands_encoding": self._commands_encoding,
        }

    @property
    def supported_features(self):
        return self._support_flags

    async def async_set_percentage(self, percentage: int):

        if percentage == 0:
            self._speed = SPEED_OFF
        else:
            self._speed = percentage_to_ordered_list_item(
                self._speed_list,
                percentage,
            )
            self._last_on_speed = self._speed

        await self.send_command()

        self.async_write_ha_state()

    async def async_oscillate(self, oscillating: bool) -> None:
        self._oscillating = oscillating

        await self.send_command()

        self.async_write_ha_state()

    async def async_set_direction(self, direction: str):
        self._direction = direction

        if self._speed != SPEED_OFF:
            await self.send_command()

        self.async_write_ha_state()

    async def async_turn_on(self, percentage=None, **kwargs):

        if percentage is None:
            percentage = ordered_list_item_to_percentage(
                self._speed_list,
                self._last_on_speed or self._speed_list[0],
            )

        await self.async_set_percentage(percentage)

    async def async_turn_off(self):

        await self.async_set_percentage(0)

    async def send_command(self):

        async with self._temp_lock:
            self._on_by_remote = False

            speed = self._speed

            if speed.lower() == SPEED_OFF:
                command = self._commands["off"]
            elif self._oscillating and "oscillate" in self._commands:
                command = self._commands["oscillate"]
            elif (
                self._direction is not None
                and isinstance(self._commands.get(self._direction), dict)
            ):
                command = self._commands[self._direction][speed]
            else:
                command = self._commands[speed]

            try:
                await self._controller.send(command)

            except Exception as e:
                _LOGGER.exception(e)

    @callback
    def _async_power_sensor_changed(self, event) -> None:
        new_state = event.data["new_state"]
        if new_state is None:
            return

        old_state = event.data["old_state"]
        if old_state is not None and new_state.state == old_state.state:
            return

        if new_state.state == STATE_ON and self._speed == SPEED_OFF:
            self._on_by_remote = True
            self.async_write_ha_state()
        elif new_state.state == STATE_OFF:
            self._on_by_remote = False
            self._speed = SPEED_OFF
            self.async_write_ha_state()

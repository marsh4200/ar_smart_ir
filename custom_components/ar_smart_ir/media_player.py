import asyncio
import json
import logging

from homeassistant.components.media_player import MediaPlayerEntity
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaType,
)

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .controller import get_controller
from .helpers import async_load_device_data
from .const import (
    CONF_COMMAND_OVERRIDES,
    CONF_CONTROLLER,
    DEFAULT_DEVICE_CLASS,
)

_LOGGER = logging.getLogger(__name__)

CONF_UNIQUE_ID = "unique_id"
CONF_NAME = "name"
CONF_DEVICE_CODE = "device_code"
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_POWER_SENSOR = "power_sensor"
CONF_SOURCE_NAMES = "source_names"
CONF_DEVICE_CLASS = "device_class"

DEFAULT_DELAY = 0.5


def _parse_source_names(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


async def async_setup_entry(hass, entry, async_add_entities):

    config = {**entry.data, **entry.options}

    device_code = config.get(CONF_DEVICE_CODE)

    device_data = await async_load_device_data(
        device_code,
        "media_player",
        config.get(CONF_COMMAND_OVERRIDES),
    )

    async_add_entities(
        [
            SmartIRMediaPlayer(
                hass,
                config,
                device_data,
            )
        ],
        True,
    )


class SmartIRMediaPlayer(MediaPlayerEntity, RestoreEntity):

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
        self._commands = device_data["commands"]
        self._device_class = config.get(CONF_DEVICE_CLASS, DEFAULT_DEVICE_CLASS)

        self._state = STATE_OFF
        self._source = None
        self._sources_list = []
        self._source_names = _parse_source_names(config.get(CONF_SOURCE_NAMES))

        self._support_flags = 0

        if "off" in self._commands:
            self._support_flags |= MediaPlayerEntityFeature.TURN_OFF

        if "on" in self._commands:
            self._support_flags |= MediaPlayerEntityFeature.TURN_ON

        if "previousChannel" in self._commands:
            self._support_flags |= MediaPlayerEntityFeature.PREVIOUS_TRACK

        if "nextChannel" in self._commands:
            self._support_flags |= MediaPlayerEntityFeature.NEXT_TRACK

        if "volumeDown" in self._commands or "volumeUp" in self._commands:
            self._support_flags |= MediaPlayerEntityFeature.VOLUME_STEP

        if "mute" in self._commands:
            self._support_flags |= MediaPlayerEntityFeature.VOLUME_MUTE

        if "sources" in self._commands:
            for source, new_name in self._source_names.items():
                if source in self._commands["sources"] and new_name:
                    self._commands["sources"][new_name] = self._commands["sources"][source]
                    del self._commands["sources"][source]

            self._support_flags |= (
                MediaPlayerEntityFeature.SELECT_SOURCE
                | MediaPlayerEntityFeature.PLAY_MEDIA
            )

            self._sources_list = list(self._commands["sources"].keys())

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
            self._state = last_state.state
            self._source = last_state.attributes.get("source")

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
    def device_class(self):
        return self._device_class

    @property
    def state(self):
        return self._state

    @property
    def source_list(self):
        return self._sources_list

    @property
    def source(self):
        return self._source

    @property
    def supported_features(self):
        return self._support_flags

    @property
    def extra_state_attributes(self):
        return {
            "device_code": self._device_code,
            "manufacturer": self._manufacturer,
            "supported_models": self._supported_models,
            "supported_controller": self._supported_controller,
            "commands_encoding": self._commands_encoding,
        }

    @property
    def media_content_type(self):
        return MediaType.CHANNEL

    async def async_turn_on(self):
        await self.send_command(self._commands["on"])
        if self._power_sensor is None:
            self._state = STATE_ON

        self.async_write_ha_state()

    async def async_turn_off(self):
        await self.send_command(self._commands["off"])
        if self._power_sensor is None:
            self._state = STATE_OFF
            self._source = None

        self.async_write_ha_state()

    async def async_media_previous_track(self):

        await self.send_command(self._commands["previousChannel"])

    async def async_media_next_track(self):

        await self.send_command(self._commands["nextChannel"])

    async def async_volume_down(self):

        await self.send_command(self._commands["volumeDown"])

    async def async_volume_up(self):

        await self.send_command(self._commands["volumeUp"])

    async def async_mute_volume(self, mute):

        await self.send_command(self._commands["mute"])

    async def async_select_source(self, source):

        self._source = source

        await self.send_command(self._commands["sources"][source])

        self.async_write_ha_state()

    async def async_play_media(self, media_type, media_id, **kwargs):
        if media_type != MediaType.CHANNEL:
            return

        media_id = str(media_id)
        if not media_id.isdigit():
            return

        if self._state == STATE_OFF and "on" in self._commands:
            await self.async_turn_on()

        for digit in media_id:
            await self.send_command(
                self._commands["sources"].get(f"Channel {digit}")
            )

        self._source = f"Channel {media_id}"
        self.async_write_ha_state()

    async def send_command(self, command):

        async with self._temp_lock:

            try:

                await self._controller.send(command)

            except Exception as e:

                _LOGGER.exception(e)

    @callback
    def _async_power_sensor_changed(self, event):
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        if new_state.state == STATE_OFF:
            self._state = STATE_OFF
            self._source = None
        elif new_state.state not in {STATE_UNKNOWN, "unavailable"}:
            self._state = STATE_ON

        self.async_write_ha_state()

import asyncio
from abc import ABC, abstractmethod
from base64 import b64encode
import binascii
import requests
import logging
import json

from homeassistant.const import ATTR_ENTITY_ID

# FIXED IMPORT
from .helpers import Helper

_LOGGER = logging.getLogger(__name__)

BROADLINK_CONTROLLER = "Broadlink"
XIAOMI_CONTROLLER = "Xiaomi"
MQTT_CONTROLLER = "MQTT"
LOOKIN_CONTROLLER = "LOOKin"
ESPHOME_CONTROLLER = "ESPHome"
TUYA_CONTROLLER = "Tuya"

ENC_BASE64 = "Base64"
ENC_HEX = "Hex"
ENC_PRONTO = "Pronto"
ENC_RAW = "Raw"

BROADLINK_COMMANDS_ENCODING = [ENC_BASE64, ENC_HEX, ENC_PRONTO]
XIAOMI_COMMANDS_ENCODING = [ENC_PRONTO, ENC_RAW]
MQTT_COMMANDS_ENCODING = [ENC_RAW]
LOOKIN_COMMANDS_ENCODING = [ENC_PRONTO, ENC_RAW]
ESPHOME_COMMANDS_ENCODING = [ENC_RAW]
TUYA_COMMANDS_ENCODING = [ENC_RAW]


def get_controller(hass, controller, encoding, controller_data, delay):
    """Return a controller compatible with the specification provided."""

    controllers = {
        BROADLINK_CONTROLLER: BroadlinkController,
        XIAOMI_CONTROLLER: XiaomiController,
        MQTT_CONTROLLER: MQTTController,
        LOOKIN_CONTROLLER: LookinController,
        ESPHOME_CONTROLLER: ESPHomeController,
        TUYA_CONTROLLER: TuyaController,
    }

    try:
        return controllers[controller](
            hass, controller, encoding, controller_data, delay
        )
    except KeyError:
        raise Exception("The controller is not supported.")


class AbstractController(ABC):
    """Representation of a controller."""

    def __init__(self, hass, controller, encoding, controller_data, delay):

        self.check_encoding(encoding)

        self.hass = hass
        self._controller = controller
        self._encoding = encoding
        self._controller_data = controller_data
        self._delay = delay

    @abstractmethod
    def check_encoding(self, encoding):
        pass

    @abstractmethod
    async def send(self, command):
        pass

    def _get_command_spec(self, command):

        code = command
        repeat_count = 1
        repeat_delay_secs = None

        if isinstance(command, dict):
            code = (
                command.get("code")
                or command.get("command")
                or command.get("value")
            )

            repeat_count = command.get(
                "repeat_count",
                command.get("repeats", repeat_count),
            )

            if repeat_count == 1 and "num_repeats" in command:
                try:
                    repeat_count = int(command["num_repeats"]) + 1
                except (TypeError, ValueError):
                    repeat_count = 1

            repeat_delay_secs = command.get(
                "repeat_delay_secs",
                command.get("repeat_delay", command.get("delay_secs")),
            )

        try:
            repeat_count = max(1, int(repeat_count))
        except (TypeError, ValueError):
            repeat_count = 1

        try:
            if repeat_delay_secs is not None:
                repeat_delay_secs = max(0.0, float(repeat_delay_secs))
        except (TypeError, ValueError):
            repeat_delay_secs = None

        return code, repeat_count, repeat_delay_secs

    def _get_command_list(self, command):

        code, repeat_count, repeat_delay_secs = self._get_command_spec(command)

        if isinstance(code, list):
            commands = code
        else:
            commands = [code]

        return commands, repeat_count, repeat_delay_secs

    async def _repeat_with_delay(self, action, repeat_count, repeat_delay_secs):

        delay = self._delay if repeat_delay_secs is None else repeat_delay_secs

        for index in range(repeat_count):
            await action()

            if index < repeat_count - 1 and delay > 0:
                await asyncio.sleep(delay)


class BroadlinkController(AbstractController):

    def check_encoding(self, encoding):

        if encoding not in BROADLINK_COMMANDS_ENCODING:
            raise Exception(
                "The encoding is not supported by the Broadlink controller."
            )

    async def send(self, command):

        commands = []
        raw_commands, repeat_count, repeat_delay_secs = self._get_command_list(command)

        for _command in raw_commands:

            if self._encoding == ENC_HEX:

                try:
                    _command = binascii.unhexlify(_command)
                    _command = b64encode(_command).decode("utf-8")

                except Exception:
                    raise Exception("Error converting HEX → Base64")

            if self._encoding == ENC_PRONTO:

                try:

                    _command = _command.replace(" ", "")
                    _command = bytearray.fromhex(_command)

                    _command = Helper.pronto2lirc(_command)
                    _command = Helper.lirc2broadlink(_command)

                    _command = b64encode(_command).decode("utf-8")

                except Exception:
                    raise Exception("Error converting PRONTO → Base64")

            commands.append("b64:" + _command)

        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            "command": commands,
            "delay_secs": (
                self._delay if repeat_delay_secs is None else repeat_delay_secs
            ),
        }

        if repeat_count > 1:
            service_data["num_repeats"] = repeat_count

        await self.hass.services.async_call(
            "remote",
            "send_command",
            service_data,
        )


class XiaomiController(AbstractController):

    def check_encoding(self, encoding):

        if encoding not in XIAOMI_COMMANDS_ENCODING:
            raise Exception(
                "The encoding is not supported by the Xiaomi controller."
            )

    async def send(self, command):

        code, repeat_count, repeat_delay_secs = self._get_command_spec(command)

        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            "command": self._encoding.lower() + ":" + code,
        }

        if repeat_count > 1:
            service_data["num_repeats"] = repeat_count

        if repeat_delay_secs is not None:
            service_data["delay_secs"] = repeat_delay_secs

        await self.hass.services.async_call(
            "remote",
            "send_command",
            service_data,
        )


class MQTTController(AbstractController):

    def check_encoding(self, encoding):

        if encoding not in MQTT_COMMANDS_ENCODING:
            raise Exception("The encoding is not supported by MQTT controller.")

    async def send(self, command):

        commands, repeat_count, repeat_delay_secs = self._get_command_list(command)

        async def publish_once():
            for index, payload in enumerate(commands):
                service_data = {
                    "topic": self._controller_data,
                    "payload": payload,
                }

                await self.hass.services.async_call(
                    "mqtt",
                    "publish",
                    service_data,
                )

                if index < len(commands) - 1 and self._delay > 0:
                    await asyncio.sleep(self._delay)

        await self._repeat_with_delay(
            publish_once,
            repeat_count,
            repeat_delay_secs,
        )


class LookinController(AbstractController):

    def check_encoding(self, encoding):

        if encoding not in LOOKIN_COMMANDS_ENCODING:
            raise Exception("Encoding not supported by LOOKin controller.")

    async def send(self, command):

        commands, repeat_count, repeat_delay_secs = self._get_command_list(command)

        async def send_once():
            encoding = self._encoding.lower().replace("pronto", "prontohex")

            for index, current_command in enumerate(commands):
                url = (
                    f"http://{self._controller_data}/commands/ir/"
                    f"{encoding}/{current_command}"
                )

                await self.hass.async_add_executor_job(requests.get, url)

                if index < len(commands) - 1 and self._delay > 0:
                    await asyncio.sleep(self._delay)

        await self._repeat_with_delay(
            send_once,
            repeat_count,
            repeat_delay_secs,
        )


class ESPHomeController(AbstractController):

    def check_encoding(self, encoding):

        if encoding not in ESPHOME_COMMANDS_ENCODING:
            raise Exception("Encoding not supported by ESPHome controller.")

    async def send(self, command):

        commands, repeat_count, repeat_delay_secs = self._get_command_list(command)

        async def send_once():
            for index, current_command in enumerate(commands):
                service_data = {"command": json.loads(current_command)}

                await self.hass.services.async_call(
                    "esphome",
                    self._controller_data,
                    service_data,
                )

                if index < len(commands) - 1 and self._delay > 0:
                    await asyncio.sleep(self._delay)

        await self._repeat_with_delay(
            send_once,
            repeat_count,
            repeat_delay_secs,
        )


class TuyaController(AbstractController):

    def check_encoding(self, encoding):

        if encoding not in TUYA_COMMANDS_ENCODING:
            raise Exception("Encoding not supported by Tuya controller.")

    async def send(self, command):

        code, repeat_count, repeat_delay_secs = self._get_command_spec(command)

        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            "command": code,
        }

        if repeat_count > 1:
            service_data["num_repeats"] = repeat_count

        if repeat_delay_secs is not None:
            service_data["delay_secs"] = repeat_delay_secs

        await self.hass.services.async_call(
            "remote",
            "send_command",
            service_data,
        )

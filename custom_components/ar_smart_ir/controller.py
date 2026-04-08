import asyncio
from abc import ABC, abstractmethod
from base64 import b64decode, b64encode
import binascii
import json
import logging

import requests

from homeassistant.const import ATTR_ENTITY_ID

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

BROADLINK_COMMANDS_ENCODING = [ENC_BASE64, ENC_HEX, ENC_PRONTO, ENC_RAW]
XIAOMI_COMMANDS_ENCODING = [ENC_PRONTO, ENC_RAW, ENC_BASE64, ENC_HEX]
MQTT_COMMANDS_ENCODING = [ENC_RAW, ENC_PRONTO, ENC_BASE64, ENC_HEX]
LOOKIN_COMMANDS_ENCODING = [ENC_PRONTO, ENC_RAW, ENC_BASE64, ENC_HEX]
ESPHOME_COMMANDS_ENCODING = [ENC_RAW, ENC_PRONTO, ENC_BASE64, ENC_HEX]
TUYA_COMMANDS_ENCODING = [ENC_RAW, ENC_PRONTO, ENC_BASE64, ENC_HEX]


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
    except KeyError as err:
        raise Exception("The controller is not supported.") from err


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

    def _normalize_command(self, command, target_encoding):
        if target_encoding == ENC_BASE64:
            return self._to_base64(command)
        if target_encoding == ENC_RAW:
            return self._to_raw(command)
        raise Exception(f"Unsupported target encoding: {target_encoding}")

    def _to_base64(self, command):
        if self._encoding == ENC_BASE64:
            return command

        if self._encoding == ENC_HEX:
            try:
                return b64encode(binascii.unhexlify(command)).decode("utf-8")
            except (binascii.Error, ValueError) as err:
                raise Exception("Error converting HEX to Base64") from err

        if self._encoding == ENC_PRONTO:
            try:
                pronto = bytearray.fromhex(command.replace(" ", ""))
                lirc = Helper.pronto2lirc(pronto)
                return b64encode(Helper.lirc2broadlink(lirc)).decode("utf-8")
            except ValueError as err:
                raise Exception("Error converting Pronto to Base64") from err

        if self._encoding == ENC_RAW:
            try:
                lirc = Helper.raw2lirc(command)
                return b64encode(Helper.lirc2broadlink(lirc)).decode("utf-8")
            except ValueError as err:
                raise Exception("Error converting Raw to Base64") from err

        raise Exception(f"Unsupported source encoding: {self._encoding}")

    def _to_raw(self, command):
        if self._encoding == ENC_RAW:
            return command if isinstance(command, str) else json.dumps(command)

        if self._encoding == ENC_PRONTO:
            try:
                pronto = bytearray.fromhex(command.replace(" ", ""))
                return Helper.lirc2raw(Helper.pronto2lirc(pronto))
            except ValueError as err:
                raise Exception("Error converting Pronto to Raw") from err

        if self._encoding in (ENC_BASE64, ENC_HEX):
            try:
                packet = (
                    b64decode(command)
                    if self._encoding == ENC_BASE64
                    else binascii.unhexlify(command)
                )
                return Helper.lirc2raw(Helper.broadlink2lirc(packet))
            except (binascii.Error, ValueError) as err:
                raise Exception(
                    f"Error converting {self._encoding} to Raw"
                ) from err

        raise Exception(f"Unsupported source encoding: {self._encoding}")


class BroadlinkController(AbstractController):
    def check_encoding(self, encoding):
        if encoding not in BROADLINK_COMMANDS_ENCODING:
            raise Exception(
                "The encoding is not supported by the Broadlink controller."
            )

    async def send(self, command):
        commands = []
        raw_commands, repeat_count, repeat_delay_secs = self._get_command_list(command)

        for current_command in raw_commands:
            commands.append("b64:" + self._normalize_command(current_command, ENC_BASE64))

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
        code = self._normalize_command(code, ENC_RAW)

        service_data = {
            ATTR_ENTITY_ID: self._controller_data,
            "command": "raw:" + code,
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
                    "payload": self._normalize_command(payload, ENC_RAW),
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
            for index, current_command in enumerate(commands):
                normalized_command = self._normalize_command(current_command, ENC_RAW)
                url = (
                    f"http://{self._controller_data}/commands/ir/"
                    f"raw/{normalized_command}"
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

    def _get_service_call_target(self):
        controller_data = str(self._controller_data).strip()
        if not controller_data:
            raise ValueError("ESPHome service name is required.")

        if "." in controller_data:
            domain, service = controller_data.split(".", 1)
            if domain != "esphome" or not service:
                raise ValueError(
                    "ESPHome controller data must be a service name or "
                    "'esphome.<service_name>'."
                )
            return domain, service

        return "esphome", controller_data

    async def send(self, command):
        commands, repeat_count, repeat_delay_secs = self._get_command_list(command)
        domain, service = self._get_service_call_target()

        async def send_once():
            for index, current_command in enumerate(commands):
                normalized_command = self._normalize_command(current_command, ENC_RAW)
                service_data = {"command": json.loads(normalized_command)}

                await self.hass.services.async_call(
                    domain,
                    service,
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
        code = self._normalize_command(code, ENC_RAW)

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

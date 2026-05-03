"""
AR Smart IR — controller layer.

This is the file that the user reported was "not transcoding" anything except
for the Broadlink path. It has been substantially rewritten so that every
controller class:

  * Correctly converts whatever encoding the device-code JSON file uses
    (Base64 / Hex / Pronto / Raw / Tuya) into the wire format that the
    *target* service / endpoint actually expects.
  * Calls the right Home Assistant service or HTTP endpoint with the right
    payload shape — not just a `remote.send_command` with whatever string
    falls out of the converter.

The conversion functions in helpers.Helper (broadlink↔lirc, pronto→lirc,
lirc→raw etc.) are unchanged and still correct; they were never the
problem.

Wire-format summary used by each controller below:

  Broadlink         remote.send_command  command="b64:<broadlink-base64>"
  Xiaomi (miio)     remote.send_command  command="raw:<xiaomi-base64>:<freq>"
  MQTT generic      mqtt.publish         payload=<signed-int JSON> (raw lirc)
  MQTT zigbee2mqtt  mqtt.publish         payload={"ir_code_to_send":"<tuya-b64>"}
  LOOKin            HTTP GET             /commands/ir/prontohex/<HEX>
  ESPHome           esphome.<svc>        {<arg>: [signed-int...], ...}
  Tuya cloud        tuya.send_ir_code    {device_id, code:"<tuya-b64>"}
  LocalTuya         localtuya.set_dp     {entity_id|device_id, dp, value}
  Tuya / generic    remote.send_command  (legacy fallthrough)

Encoding lists below tell SmartIR which source encodings each controller
supports — if the JSON declares Tuya and the controller is BroadlinkController
this used to silently raise; the encoding lists now reflect what the
transcoder can actually do.
"""

from __future__ import annotations

import asyncio
import binascii
import json
import logging
from abc import ABC, abstractmethod
from base64 import b64decode, b64encode

import requests

from homeassistant.const import ATTR_ENTITY_ID

from .helpers import Helper
from .tuya_codec import decode_tuya, encode_tuya

_LOGGER = logging.getLogger(__name__)

# ── controller / encoding tags ──────────────────────────────────────────────

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
ENC_TUYA = "Tuya"

# Every controller's transcoder can now ingest any of these source encodings —
# whether the device-code JSON declared Base64, Hex, Pronto, Raw, or Tuya.
ALL_ENCODINGS = [ENC_BASE64, ENC_HEX, ENC_PRONTO, ENC_RAW, ENC_TUYA]

BROADLINK_COMMANDS_ENCODING = ALL_ENCODINGS
XIAOMI_COMMANDS_ENCODING = ALL_ENCODINGS
MQTT_COMMANDS_ENCODING = ALL_ENCODINGS
LOOKIN_COMMANDS_ENCODING = ALL_ENCODINGS
ESPHOME_COMMANDS_ENCODING = ALL_ENCODINGS
TUYA_COMMANDS_ENCODING = ALL_ENCODINGS

# Default IR carrier frequency (Hz). Can be overridden per-controller via
# controller_data when the user knows their device uses something different.
DEFAULT_IR_FREQUENCY_HZ = 38000


# ── factory ─────────────────────────────────────────────────────────────────


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
        raise Exception(f"The controller '{controller}' is not supported.") from err


# ── base class ──────────────────────────────────────────────────────────────


class AbstractController(ABC):
    """Representation of an IR/RF controller."""

    def __init__(self, hass, controller, encoding, controller_data, delay):
        self.check_encoding(encoding)

        self.hass = hass
        self._controller = controller
        self._encoding = encoding
        self._controller_data = controller_data
        self._delay = delay

    @abstractmethod
    def check_encoding(self, encoding):
        ...

    @abstractmethod
    async def send(self, command):
        ...

    # ── common helpers ──────────────────────────────────────────────────────

    def _parse_positive_float(self, value):
        try:
            if value is not None:
                return max(0.0, float(value))
        except (TypeError, ValueError):
            return None
        return None

    def _get_command_spec(self, command):
        code = command
        repeat_count = 1
        repeat_delay_secs = None

        if isinstance(command, dict):
            code = command.get("code") or command.get("command") or command.get("value")

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
        commands = code if isinstance(code, list) else [code]
        return commands, repeat_count, repeat_delay_secs

    def _get_sequence_spec(self, command):
        if isinstance(command, dict):
            sequence = command.get("sequence", command.get("steps"))
            if isinstance(sequence, list):
                _code, repeat_count, repeat_delay_secs = self._get_command_spec(command)
                step_delay_secs = self._parse_positive_float(
                    command.get(
                        "step_delay_secs",
                        command.get("sequence_delay_secs", self._delay),
                    )
                )
                if step_delay_secs is None:
                    step_delay_secs = self._delay
                return sequence, repeat_count, repeat_delay_secs, step_delay_secs

        commands, repeat_count, repeat_delay_secs = self._get_command_list(command)
        return commands, repeat_count, repeat_delay_secs, self._delay

    async def _repeat_with_delay(self, action, repeat_count, repeat_delay_secs):
        delay = self._delay if repeat_delay_secs is None else repeat_delay_secs
        for index in range(repeat_count):
            await action()
            if index < repeat_count - 1 and delay > 0:
                await asyncio.sleep(delay)

    async def _run_sequence(self, command, send_step):
        steps, repeat_count, repeat_delay_secs, step_delay_secs = self._get_sequence_spec(
            command
        )

        async def run_once():
            for index, step in enumerate(steps):
                await send_step(step)
                if index < len(steps) - 1 and step_delay_secs > 0:
                    await asyncio.sleep(step_delay_secs)

        await self._repeat_with_delay(run_once, repeat_count, repeat_delay_secs)

    # ── universal transcoder ────────────────────────────────────────────────
    #
    # Every send() goes through one of these. `_normalize_command` picks the
    # path based on the *target* encoding the controller wants on the wire.

    def _normalize_command(self, command, target_encoding):
        if target_encoding == ENC_BASE64:
            return self._to_base64(command)
        if target_encoding == ENC_RAW:
            return self._to_raw(command)
        if target_encoding == ENC_PRONTO:
            return self._to_pronto_hex(command)
        if target_encoding == ENC_TUYA:
            return self._to_tuya_b64(command)
        raise Exception(f"Unsupported target encoding: {target_encoding}")

    # source → broadlink-base64 (the original SmartIR wire format)
    def _to_base64(self, command):
        if self._encoding == ENC_BASE64:
            return command

        if self._encoding == ENC_HEX:
            try:
                return b64encode(
                    binascii.unhexlify(Helper.normalize_hex_string(command))
                ).decode("utf-8")
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

        if self._encoding == ENC_TUYA:
            try:
                pulses = decode_tuya(command)
                return b64encode(Helper.lirc2broadlink(pulses)).decode("utf-8")
            except ValueError as err:
                raise Exception("Error converting Tuya to Base64") from err

        raise Exception(f"Unsupported source encoding: {self._encoding}")

    # source → signed-int "raw" (LIRC-style positive=mark/negative=space JSON)
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
                if self._encoding == ENC_BASE64:
                    packet = b64decode(command)
                else:
                    packet = binascii.unhexlify(Helper.normalize_hex_string(command))
                return Helper.lirc2raw(Helper.broadlink2lirc(packet))
            except (binascii.Error, ValueError) as err:
                if self._encoding == ENC_HEX:
                    try:
                        return Helper.lirc2raw(
                            Helper.compact_nec_hex_to_lirc(command)
                        )
                    except ValueError:
                        pass
                raise Exception(f"Error converting {self._encoding} to Raw") from err

        if self._encoding == ENC_TUYA:
            try:
                # Tuya pulses are unsigned alternating mark/space — exactly
                # the "lirc" representation lirc2raw expects.
                return Helper.lirc2raw(decode_tuya(command))
            except ValueError as err:
                raise Exception("Error converting Tuya to Raw") from err

        raise Exception(f"Unsupported source encoding: {self._encoding}")

    # source → Tuya FastLZ Base64
    def _to_tuya_b64(self, command):
        if self._encoding == ENC_TUYA:
            return command

        # Get the unsigned pulse list, regardless of input format.
        pulses = self._to_pulse_list(command)
        return encode_tuya(pulses)

    # source → Pronto hex (LOOKin native format)
    def _to_pronto_hex(self, command):
        if self._encoding == ENC_PRONTO:
            return command.replace(" ", "").upper()

        # Build a pronto code from the underlying pulse list.
        pulses = self._to_pulse_list(command)
        return _lirc_to_pronto_hex(pulses, DEFAULT_IR_FREQUENCY_HZ)

    # Helper: get an unsigned alternating mark/space pulse list from any
    # supported source encoding. Used by _to_tuya_b64 and _to_pronto_hex.
    def _to_pulse_list(self, command) -> list[int]:
        if self._encoding == ENC_TUYA:
            return decode_tuya(command)

        if self._encoding == ENC_PRONTO:
            try:
                pronto = bytearray.fromhex(command.replace(" ", ""))
                return Helper.pronto2lirc(pronto)
            except ValueError as err:
                raise Exception("Error converting Pronto to pulse list") from err

        if self._encoding == ENC_RAW:
            lirc = Helper.raw2lirc(command)
            return [abs(int(v)) for v in lirc]

        if self._encoding in (ENC_BASE64, ENC_HEX):
            try:
                if self._encoding == ENC_BASE64:
                    packet = b64decode(command)
                else:
                    packet = binascii.unhexlify(Helper.normalize_hex_string(command))
                return [abs(int(v)) for v in Helper.broadlink2lirc(packet)]
            except (binascii.Error, ValueError) as err:
                if self._encoding == ENC_HEX:
                    try:
                        return [
                            abs(int(v))
                            for v in Helper.compact_nec_hex_to_lirc(command)
                        ]
                    except ValueError:
                        pass
                raise Exception(
                    f"Error converting {self._encoding} to pulse list"
                ) from err

        raise Exception(f"Unsupported source encoding: {self._encoding}")


# ── tiny helper: LIRC pulse list → Pronto hex string ────────────────────────
#
# Used by LookinController and XiaomiController.
#
# Pronto encoding refresher (matches helpers.Helper.pronto2lirc):
#   word 0 = 0x0000   (learnt code prefix)
#   word 1 = carrier  (such that frequency_MHz = 1 / (word1 * 0.241246))
#   word 2 = burst-pair-1 count
#   word 3 = burst-pair-2 count (0 here; we put everything in burst-1)
#   words 4.. = alternating mark/space lengths in carrier-period ticks
#
# So: carrier_word = round( 1 / (freq_MHz * 0.241246) )
#     pulse_us     = ticks / freq_MHz
# i.e. ticks = round( pulse_us * freq_MHz )

def _lirc_to_pronto_hex(pulses, frequency_hz: int) -> str:
    if not pulses:
        raise Exception("Cannot build Pronto from empty pulse list.")

    freq_mhz = frequency_hz / 1_000_000.0
    if freq_mhz <= 0:
        raise Exception(f"Invalid IR carrier frequency: {frequency_hz} Hz")

    carrier_word = max(1, int(round(1.0 / (freq_mhz * 0.241246))))

    pair_count = len(pulses) // 2  # discard a trailing odd pulse
    code = [0x0000, carrier_word, pair_count, 0x0000]

    for i in range(pair_count * 2):
        ticks = max(1, int(round(abs(pulses[i]) * freq_mhz)))
        if ticks > 0xFFFF:
            ticks = 0xFFFF
        code.append(ticks)

    return " ".join(f"{w:04X}" for w in code)


# ── Broadlink ───────────────────────────────────────────────────────────────


class BroadlinkController(AbstractController):
    def check_encoding(self, encoding):
        if encoding not in BROADLINK_COMMANDS_ENCODING:
            raise Exception(
                "The encoding is not supported by the Broadlink controller."
            )

    async def send(self, command):
        async def send_step(step):
            code, repeat_count, repeat_delay_secs = self._get_command_spec(step)
            service_data = {
                ATTR_ENTITY_ID: self._controller_data,
                "command": ["b64:" + self._normalize_command(code, ENC_BASE64)],
            }
            if repeat_count > 1:
                service_data["num_repeats"] = repeat_count
            if repeat_delay_secs is not None:
                service_data["delay_secs"] = repeat_delay_secs

            await self.hass.services.async_call(
                "remote", "send_command", service_data
            )

        await self._run_sequence(command, send_step)


# ── Xiaomi (miio) ───────────────────────────────────────────────────────────
#
# Xiaomi's chuangmi_ir wants `raw:<base64>:<frequency>`. The base64 here is
# *not* the Broadlink format — it's just base64-encoded pronto bytes. To keep
# it format-agnostic, we go via Pronto.

class XiaomiController(AbstractController):
    def check_encoding(self, encoding):
        if encoding not in XIAOMI_COMMANDS_ENCODING:
            raise Exception("The encoding is not supported by the Xiaomi controller.")

    async def send(self, command):
        async def send_step(step):
            code, repeat_count, repeat_delay_secs = self._get_command_spec(step)
            pronto_hex = self._normalize_command(code, ENC_PRONTO).replace(" ", "")
            try:
                pronto_b64 = b64encode(bytes.fromhex(pronto_hex)).decode("ascii")
            except ValueError as err:
                raise Exception("Failed to base64-encode Pronto for Xiaomi") from err

            service_data = {
                ATTR_ENTITY_ID: self._controller_data,
                "command": f"raw:{pronto_b64}:{DEFAULT_IR_FREQUENCY_HZ}",
            }
            if repeat_count > 1:
                service_data["num_repeats"] = repeat_count
            if repeat_delay_secs is not None:
                service_data["delay_secs"] = repeat_delay_secs

            await self.hass.services.async_call(
                "remote", "send_command", service_data
            )

        await self._run_sequence(command, send_step)


# ── MQTT ────────────────────────────────────────────────────────────────────
#
# Two distinct profiles handled here:
#
#   1. Generic MQTT IR bridge (Tasmota-IRHVAC, custom firmwares): publish a
#      raw signed-int JSON array to the configured topic. Caller controls
#      payload shape via a JSON `controller_data` blob.
#
#   2. Zigbee2MQTT Tuya blasters (UFO-R11 / ZS06): publish
#      `{"ir_code_to_send": "<tuya-base64>"}` to `zigbee2mqtt/<dev>/set`.
#      Auto-detected from topic prefix, but can also be selected explicitly.

class MQTTController(AbstractController):
    def check_encoding(self, encoding):
        if encoding not in MQTT_COMMANDS_ENCODING:
            raise Exception("The encoding is not supported by MQTT controller.")

    def _get_mqtt_target(self):
        """Return (topic, payload_format, payload_key, qos, retain).

        payload_format ∈ {"raw", "tuya", "passthrough"}
            raw         signed-int JSON array
            tuya        {payload_key: tuya-base64}
            passthrough send the source encoding string verbatim
        """
        topic = self._controller_data
        payload_key: str | None = None
        payload_format = "raw"
        qos: int = 0
        retain: bool = False

        if isinstance(self._controller_data, str):
            data = self._controller_data.strip()
            if data.startswith("{"):
                try:
                    cfg = json.loads(data)
                except json.JSONDecodeError:
                    cfg = None
                if isinstance(cfg, dict):
                    topic = cfg.get("topic", topic)
                    payload_key = cfg.get("payload_key") or cfg.get("payloadProperty")
                    fmt = cfg.get("payload_format") or cfg.get("format")
                    if fmt:
                        payload_format = str(fmt).lower()
                    qos = int(cfg.get("qos", 0))
                    retain = bool(cfg.get("retain", False))

        # Auto-detection for Zigbee2MQTT-style Tuya blasters.
        if (
            isinstance(topic, str)
            and topic.startswith("zigbee2mqtt/")
            and topic.endswith("/set")
        ):
            if payload_key is None:
                payload_key = "ir_code_to_send"
            if payload_format == "raw":
                payload_format = "tuya"

        if not isinstance(topic, str) or not topic.strip():
            raise Exception("MQTT controller data must include a valid topic.")

        return topic.strip(), payload_format, payload_key, qos, retain

    async def send(self, command):
        topic, payload_format, payload_key, qos, retain = self._get_mqtt_target()

        async def send_step(step):
            code, repeat_count, repeat_delay_secs = self._get_command_spec(step)

            if payload_format == "tuya":
                wire_value = self._normalize_command(code, ENC_TUYA)
            elif payload_format == "passthrough":
                # Don't transcode at all — assume the JSON code already matches
                # whatever the topic expects.
                wire_value = code if isinstance(code, str) else json.dumps(code)
            else:
                wire_value = self._normalize_command(code, ENC_RAW)

            if payload_key:
                payload = json.dumps({payload_key: wire_value})
            else:
                payload = wire_value  # already a string

            service_data = {"topic": topic, "payload": payload, "qos": qos, "retain": retain}

            async def publish_once():
                await self.hass.services.async_call("mqtt", "publish", service_data)

            await self._repeat_with_delay(publish_once, repeat_count, repeat_delay_secs)

        await self._run_sequence(command, send_step)


# ── LOOKin ──────────────────────────────────────────────────────────────────
#
# LOOKin Remote 2's local HTTP API has multiple IR endpoints:
#   /commands/ir/prontohex/<HEX>
#   /commands/ir/raw/<unit>;<pair1>;<pair2>;...
# The "raw" endpoint is NOT a JSON array — it's a custom `unit;pair;pair` shape.
# Going through prontohex is universal and well-supported, so we use that.

class LookinController(AbstractController):
    def check_encoding(self, encoding):
        if encoding not in LOOKIN_COMMANDS_ENCODING:
            raise Exception("Encoding not supported by LOOKin controller.")

    async def send(self, command):
        async def send_step(step):
            code, repeat_count, repeat_delay_secs = self._get_command_spec(step)
            pronto_hex = self._normalize_command(code, ENC_PRONTO).replace(" ", "")
            url = (
                f"http://{self._controller_data}/commands/ir/prontohex/{pronto_hex}"
            )

            async def send_once():
                await self.hass.async_add_executor_job(requests.get, url)

            await self._repeat_with_delay(send_once, repeat_count, repeat_delay_secs)

        await self._run_sequence(command, send_step)


# ── ESPHome ─────────────────────────────────────────────────────────────────
#
# ESPHome exposes user-defined services. The user has to write something like:
#
#   api:
#     services:
#       - service: send_raw_command
#         variables:
#           command: int[]
#         then:
#           - remote_transmitter.transmit_raw:
#               code: !lambda 'return command;'
#               carrier_frequency: 38kHz
#
# The service name AND the parameter name are both user-chosen. We therefore
# accept controller_data in three shapes:
#
#   1. "my_espir_send_raw_command"
#         → calls esphome.my_espir_send_raw_command with {"command": [...]}
#
#   2. "esphome.my_espir_send_raw_command"
#         → same, explicit form.
#
#   3. JSON: {"service": "...", "command_arg": "command",
#            "frequency_arg": "carrier_frequency", "frequency": 38000,
#            "extra": {...}}
#         → fully customisable; lets the user match their actual YAML.

class ESPHomeController(AbstractController):
    def check_encoding(self, encoding):
        if encoding not in ESPHOME_COMMANDS_ENCODING:
            raise Exception("Encoding not supported by ESPHome controller.")

    def _parse_target(self):
        cd = self._controller_data
        domain = "esphome"
        service: str | None = None
        command_arg = "command"
        frequency_arg: str | None = None
        frequency: int | None = None
        extra: dict = {}

        if isinstance(cd, dict):
            cfg = cd
        elif isinstance(cd, str) and cd.strip().startswith("{"):
            try:
                cfg = json.loads(cd.strip())
            except json.JSONDecodeError as err:
                raise ValueError(
                    f"ESPHome controller_data is not valid JSON: {err}"
                ) from err
        else:
            cfg = None

        if cfg is not None:
            raw_service = str(cfg.get("service", "")).strip()
            if not raw_service:
                raise ValueError(
                    "ESPHome controller_data JSON must include a 'service' key."
                )
            if "." in raw_service:
                domain, service = raw_service.split(".", 1)
            else:
                service = raw_service
            command_arg = str(cfg.get("command_arg", command_arg))
            frequency_arg = cfg.get("frequency_arg")
            if "frequency" in cfg:
                try:
                    frequency = int(cfg["frequency"])
                except (TypeError, ValueError):
                    frequency = None
            extra = dict(cfg.get("extra") or {})
        else:
            text = str(cd).strip()
            if not text:
                raise ValueError("ESPHome service name is required.")
            if "." in text:
                domain, service = text.split(".", 1)
                if domain != "esphome" or not service:
                    raise ValueError(
                        "ESPHome controller_data must be 'esphome.<service>' or just '<service>'."
                    )
            else:
                service = text

        return domain, service, command_arg, frequency_arg, frequency, extra

    async def send(self, command):
        (
            domain,
            service,
            command_arg,
            frequency_arg,
            frequency,
            extra,
        ) = self._parse_target()

        async def send_step(step):
            code, repeat_count, repeat_delay_secs = self._get_command_spec(step)
            normalized = self._normalize_command(code, ENC_RAW)
            pulses = json.loads(normalized)

            # ESPHome's int[] service variable historically struggles with
            # very long arrays; warn the user instead of silently failing.
            if len(pulses) > 1024:
                _LOGGER.warning(
                    "ESPHome service '%s.%s' is being called with %d pulses; "
                    "very long arrays may exceed the API frame size. Consider "
                    "splitting the IR signal or using transmit_pronto on the "
                    "ESPHome side.",
                    domain,
                    service,
                    len(pulses),
                )

            service_data: dict = {command_arg: pulses, **extra}
            if frequency_arg and frequency is not None:
                service_data[frequency_arg] = int(frequency)

            async def send_once():
                await self.hass.services.async_call(domain, service, service_data)

            await self._repeat_with_delay(send_once, repeat_count, repeat_delay_secs)

        await self._run_sequence(command, send_step)


# ── Tuya ────────────────────────────────────────────────────────────────────
#
# Tuya IR blasters in HA come in three flavours; the user picks via
# controller_data:
#
#   1. JSON: {"mode": "cloud", "remote_id": "<tuya-device-id>"}
#         Calls tuya.send_ir_code with {device_id, code: "<tuya-b64>"}.
#
#   2. JSON: {"mode": "localtuya", "entity_id": "remote.living_room",
#            "dp": 201}
#         Writes the Tuya-Base64 string to the configured DP via
#         localtuya.set_dp.
#
#   3. Plain entity_id string (e.g. "remote.tuya_living_room"):
#         Falls back to remote.send_command with the Tuya-Base64 string —
#         useful for blueprint-style remotes from third-party Tuya
#         integrations that present a standard remote.send_command interface.

class TuyaController(AbstractController):
    def check_encoding(self, encoding):
        if encoding not in TUYA_COMMANDS_ENCODING:
            raise Exception("Encoding not supported by Tuya controller.")

    def _parse_target(self):
        cd = self._controller_data
        if isinstance(cd, dict):
            cfg = cd
        elif isinstance(cd, str) and cd.strip().startswith("{"):
            try:
                cfg = json.loads(cd.strip())
            except json.JSONDecodeError as err:
                raise ValueError(
                    f"Tuya controller_data is not valid JSON: {err}"
                ) from err
        else:
            cfg = None

        if cfg is None:
            return {"mode": "remote", "entity_id": str(cd).strip()}

        mode = str(cfg.get("mode", "")).strip().lower() or "remote"
        return {**cfg, "mode": mode}

    async def send(self, command):
        target = self._parse_target()
        mode = target.get("mode", "remote")

        async def send_step(step):
            code, repeat_count, repeat_delay_secs = self._get_command_spec(step)
            tuya_b64 = self._normalize_command(code, ENC_TUYA)

            if mode == "cloud":
                remote_id = target.get("remote_id") or target.get("device_id")
                if not remote_id:
                    raise Exception(
                        "Tuya cloud mode needs 'remote_id' (the Tuya IR blaster device id)."
                    )
                service_data = {
                    "device_id": remote_id,
                    "code": tuya_b64,
                }
                # Optional: category/key fields supported by some Tuya blueprints.
                for opt_key in ("category_id", "key_id", "key_name"):
                    if opt_key in target:
                        service_data[opt_key] = target[opt_key]

                async def send_once():
                    await self.hass.services.async_call(
                        "tuya", "send_ir_code", service_data
                    )

                await self._repeat_with_delay(
                    send_once, repeat_count, repeat_delay_secs
                )

            elif mode == "localtuya":
                dp = target.get("dp") or target.get("send_dp")
                if dp is None:
                    raise Exception(
                        "LocalTuya mode needs 'dp' (the IR send datapoint)."
                    )
                service_data = {"value": tuya_b64, "dp": int(dp)}
                if "entity_id" in target:
                    service_data[ATTR_ENTITY_ID] = target["entity_id"]
                elif "device_id" in target:
                    service_data["device_id"] = target["device_id"]
                else:
                    raise Exception(
                        "LocalTuya mode needs 'entity_id' or 'device_id'."
                    )

                async def send_once():
                    await self.hass.services.async_call(
                        "localtuya", "set_dp", service_data
                    )

                await self._repeat_with_delay(
                    send_once, repeat_count, repeat_delay_secs
                )

            else:  # mode == "remote" — pass-through to remote.send_command
                entity_id = target.get("entity_id", self._controller_data)
                service_data = {
                    ATTR_ENTITY_ID: entity_id,
                    "command": tuya_b64,
                }
                if repeat_count > 1:
                    service_data["num_repeats"] = repeat_count
                if repeat_delay_secs is not None:
                    service_data["delay_secs"] = repeat_delay_secs

                await self.hass.services.async_call(
                    "remote", "send_command", service_data
                )

        await self._run_sequence(command, send_step)

from __future__ import annotations

import aiofiles
import json
import logging
import os
from collections import defaultdict
from typing import Any

from .const import PLATFORMS

_LOGGER = logging.getLogger(__name__)
COMPONENT_ABS_DIR = os.path.dirname(os.path.abspath(__file__))


def get_codes_dir(platform: str) -> str:
    return os.path.join(COMPONENT_ABS_DIR, "codes", platform)


COMMAND_META_KEYS = {
    "code",
    "command",
    "value",
    "repeat_count",
    "repeat_delay_secs",
    "repeat_delay",
    "delay_secs",
    "repeats",
    "num_repeats",
}


def _is_command_meta_dict(value: Any) -> bool:
    return isinstance(value, dict) and bool(COMMAND_META_KEYS.intersection(value))


def _merge_command_tree(base: Any, override: Any) -> Any:
    if override is None:
        return base

    if _is_command_meta_dict(override):
        if isinstance(base, dict) and _is_command_meta_dict(base):
            merged = dict(base)
        elif base is not None:
            merged = {"code": base}
        else:
            merged = {}
        merged.update(override)
        return merged

    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _merge_command_tree(base.get(key), value)
        return merged

    return override


def parse_command_overrides(value: Any) -> dict[str, Any]:
    if not value:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as err:
            raise ValueError(f"Invalid command overrides JSON: {err}") from err

        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Command overrides must be a JSON object.")


def _is_command_leaf(value: Any) -> bool:
    return isinstance(value, str) or _is_command_meta_dict(value)


def flatten_command_paths(commands: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []

    if not isinstance(commands, dict):
        return paths

    for key, value in commands.items():
        current = prefix + (str(key),)
        if _is_command_leaf(value):
            paths.append(current)
            continue
        if isinstance(value, dict):
            paths.extend(flatten_command_paths(value, current))

    return paths


def command_path_to_key(path: tuple[str, ...]) -> str:
    return " / ".join(path)


def get_command_value_at_path(commands: Any, path: tuple[str, ...]) -> Any:
    current = commands
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def set_command_override_at_path(
    overrides: dict[str, Any],
    path: tuple[str, ...],
    repeat_count: int,
    repeat_delay_secs: float,
) -> dict[str, Any]:
    current = overrides
    for part in path[:-1]:
        node = current.get(part)
        if not isinstance(node, dict) or _is_command_meta_dict(node):
            node = {}
            current[part] = node
        current = node

    current[path[-1]] = {
        "repeat_count": max(1, int(repeat_count)),
        "repeat_delay_secs": max(0.0, float(repeat_delay_secs)),
    }
    return overrides


def remove_command_override_at_path(
    overrides: dict[str, Any],
    path: tuple[str, ...],
) -> dict[str, Any]:
    def _prune(node: Any, parts: tuple[str, ...], depth: int = 0) -> bool:
        if not isinstance(node, dict):
            return False

        key = parts[depth]
        if key not in node:
            return not node

        if depth == len(parts) - 1:
            node.pop(key, None)
        else:
            child = node.get(key)
            should_delete = _prune(child, parts, depth + 1)
            if should_delete:
                node.pop(key, None)

        return not node

    _prune(overrides, path)
    return overrides


async def async_load_device_data(
    device_code: int | str,
    platform: str,
    command_overrides: Any = None,
) -> dict[str, Any]:
    path = os.path.join(get_codes_dir(platform), f"{device_code}.json")
    async with aiofiles.open(path, mode="r") as jfile:
        device_data = json.loads(await jfile.read())

    overrides = parse_command_overrides(command_overrides)
    if overrides:
        device_data["commands"] = _merge_command_tree(
            device_data.get("commands", {}),
            overrides,
        )

    return device_data


def load_catalog(platform: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    directory = get_codes_dir(platform)
    if not os.path.isdir(directory):
        return items

    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        code = filename[:-5]
        path = os.path.join(directory, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as err:
            _LOGGER.warning("Skipping invalid SmartIR code file %s: %s", filename, err)
            continue

        manufacturer = data.get("manufacturer", "Unknown")
        models = data.get("supportedModels") or ["Unknown"]
        model_label = ", ".join(models[:3])
        if len(models) > 3:
            model_label += "…"
        label = f"{code} — {manufacturer} — {model_label}"
        items.append({
            "code": code,
            "manufacturer": manufacturer,
            "models": models,
            "label": label,
            "supported_controller": data.get("supportedController"),
            "commands_encoding": data.get("commandsEncoding"),
        })
    return items


def get_manufacturers(platform: str) -> list[str]:
    return sorted({item["manufacturer"] for item in load_catalog(platform)})


def get_models_for_manufacturer(platform: str, manufacturer: str) -> list[dict[str, Any]]:
    return [item for item in load_catalog(platform) if item["manufacturer"] == manufacturer]


def infer_title(data: dict[str, Any]) -> str:
    platform = data.get("platform", "device")
    name = data.get("name")
    if name:
        return name
    return f"SmartIR {platform.replace('_', ' ').title()} {data.get('device_code', '')}".strip()
    
import struct
import binascii


class Helper:

    @staticmethod
    def pronto2lirc(pronto):

        codes = [
            int(binascii.hexlify(pronto[i:i + 2]), 16)
            for i in range(0, len(pronto), 2)
        ]

        if codes[0]:
            raise ValueError("Pronto code should start with 0000")

        if len(codes) != 4 + 2 * (codes[2] + codes[3]):
            raise ValueError("Number of pulse widths does not match")

        frequency = 1 / (codes[1] * 0.241246)

        return [int(round(code / frequency)) for code in codes[4:]]

    @staticmethod
    def lirc2broadlink(pulses):

        array = bytearray()

        for pulse in pulses:

            pulse = int(pulse * 269 / 8192)

            if pulse < 256:
                array += bytearray(struct.pack(">B", pulse))
            else:
                array += bytearray([0x00])
                array += bytearray(struct.pack(">H", pulse))

        packet = bytearray([0x26, 0x00])
        packet += bytearray(struct.pack("<H", len(array)))
        packet += array
        packet += bytearray([0x0D, 0x05])

        remainder = (len(packet) + 4) % 16

        if remainder:
            packet += bytearray(16 - remainder)

        return packet

    @staticmethod
    def raw2lirc(raw):
        if isinstance(raw, str):
            try:
                values = json.loads(raw)
            except json.JSONDecodeError as err:
                raise ValueError("Raw command must be valid JSON.") from err
        elif isinstance(raw, list):
            values = raw
        else:
            raise ValueError("Raw command must be a JSON array or list.")

        try:
            return [abs(int(round(float(value)))) for value in values]
        except (TypeError, ValueError) as err:
            raise ValueError("Raw command contains invalid pulse values.") from err

    @staticmethod
    def lirc2raw(pulses):
        raw = []

        for index, pulse in enumerate(pulses):
            value = abs(int(round(float(pulse))))
            raw.append(value if index % 2 == 0 else -value)

        return json.dumps(raw)

    @staticmethod
    def broadlink2lirc(packet):
        if not isinstance(packet, (bytes, bytearray)):
            raise ValueError("Broadlink packet must be bytes.")

        data = bytes(packet)
        if len(data) < 4:
            raise ValueError("Broadlink packet is too short.")

        if data[:2] != b"\x26\x00":
            raise ValueError("Broadlink packet has an unsupported header.")

        length = int.from_bytes(data[2:4], byteorder="little")
        payload = data[4:4 + length]

        if len(payload) < length:
            raise ValueError("Broadlink packet payload is truncated.")

        pulses = []
        index = 0

        while index < len(payload):
            chunk = payload[index]
            index += 1

            if chunk == 0:
                if index + 1 >= len(payload):
                    raise ValueError("Broadlink packet ended mid-pulse.")
                chunk = int.from_bytes(payload[index:index + 2], byteorder="big")
                index += 2

            pulses.append(int(round(chunk * 8192 / 269)))

        return pulses

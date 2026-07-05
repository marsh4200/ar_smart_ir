#!/usr/bin/env python3
"""flipper2smartir.py - Convert Flipper-IRDB .ir files to ar_smart_ir codesets.

Encodes Flipper 'parsed' protocol signals (NEC, NECext, Samsung32, SIRC,
SIRC15, SIRC20, RC5, RC6, RCA) and 'raw' signals to Broadlink Base64,
then maps buttons onto the SmartIR media_player / fan / light schemas.

Usage:
    python3 flipper2smartir.py --src <Flipper-IRDB dir> --dest <codes dir> \
        [--start-id 9000] [--category TVs=media_player ...]

Only devices that satisfy the minimum command set for their platform are
emitted; everything else is skipped and reported.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Broadlink packet encoding
# ---------------------------------------------------------------------------

def us_to_broadlink(us: float) -> int:
    return max(1, int(round(us * 269 / 8192)))


def _emit(val: int, out: bytearray) -> None:
    if val > 255:
        out.append(0x00)
        out.append((val >> 8) & 0xFF)
        out.append(val & 0xFF)
    else:
        out.append(val)


def timings_to_broadlink_b64(timings: list[int]) -> str:
    """timings: alternating mark/space durations in microseconds, mark first."""
    payload = bytearray()
    for us in timings:
        _emit(us_to_broadlink(us), payload)
    # terminating long space (0x0d05 units ~ 109 ms)
    payload += bytes([0x00, 0x0D, 0x05])
    packet = bytearray([0x26, 0x00, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF])
    packet += payload
    if len(packet) % 16:
        packet += bytes(16 - len(packet) % 16)
    return base64.b64encode(bytes(packet)).decode()


# ---------------------------------------------------------------------------
# Protocol encoders -> mark/space timing lists (microseconds, mark first)
# ---------------------------------------------------------------------------

def _bits_lsb(value: int, count: int) -> list[int]:
    return [(value >> i) & 1 for i in range(count)]


def _bits_msb(value: int, count: int) -> list[int]:
    return [(value >> i) & 1 for i in range(count - 1, -1, -1)]


def _pulse_distance(bits, hdr_mark, hdr_space, bit_mark, zero_space, one_space,
                    stop_mark) -> list[int]:
    t = [hdr_mark, hdr_space]
    for b in bits:
        t += [bit_mark, one_space if b else zero_space]
    t.append(stop_mark)
    return t


def enc_nec(addr: bytes, cmd: bytes) -> list[int]:
    a, c = addr[0], cmd[0]
    bits = (_bits_lsb(a, 8) + _bits_lsb(a ^ 0xFF, 8)
            + _bits_lsb(c, 8) + _bits_lsb(c ^ 0xFF, 8))
    return _pulse_distance(bits, 9000, 4500, 560, 560, 1690, 560)


def enc_necext(addr: bytes, cmd: bytes) -> list[int]:
    bits = (_bits_lsb(addr[0], 8) + _bits_lsb(addr[1], 8)
            + _bits_lsb(cmd[0], 8) + _bits_lsb(cmd[1], 8))
    return _pulse_distance(bits, 9000, 4500, 560, 560, 1690, 560)


def enc_samsung32(addr: bytes, cmd: bytes) -> list[int]:
    a, c = addr[0], cmd[0]
    bits = (_bits_lsb(a, 8) + _bits_lsb(a, 8)
            + _bits_lsb(c, 8) + _bits_lsb(c ^ 0xFF, 8))
    return _pulse_distance(bits, 4500, 4500, 560, 560, 1690, 560)


def enc_rca(addr: bytes, cmd: bytes) -> list[int]:
    a, c = addr[0] & 0x0F, cmd[0]
    bits = (_bits_msb(a, 4) + _bits_msb(c, 8)
            + _bits_msb(a ^ 0x0F, 4) + _bits_msb(c ^ 0xFF, 8))
    return _pulse_distance(bits, 4000, 4000, 500, 1000, 2000, 500)


def _enc_sirc(addr: bytes, cmd: bytes, addr_bits: int) -> list[int]:
    c = cmd[0] & 0x7F
    a = int.from_bytes(addr[:2], "little")
    bits = _bits_lsb(c, 7) + _bits_lsb(a, addr_bits)
    frame: list[int] = [2400, 600]
    for b in bits:
        frame += [1200 if b else 600, 600]
    # frame currently ends with a space; compute duration to pad to 45 ms
    dur = sum(frame)
    frame[-1] += max(10000, 45000 - dur)
    # Sony devices expect the frame at least 3 times
    out: list[int] = []
    for _ in range(3):
        out += frame
    out[-1] = 600  # trailing space irrelevant; terminator added later
    # timings must start with mark and alternate; frames concatenated keep that
    return out[:-1] + [600] if len(out) % 2 else out[:-1]


def enc_sirc(addr, cmd):
    return _enc_sirc(addr, cmd, 5)


def enc_sirc15(addr, cmd):
    return _enc_sirc(addr, cmd, 8)


def enc_sirc20(addr, cmd):
    return _enc_sirc(addr, cmd, 13)


def _manchester(levels_halves: list[tuple[int, int]]) -> list[int]:
    """Merge (level, duration) halves into mark-first mark/space list."""
    merged: list[tuple[int, int]] = []
    for level, dur in levels_halves:
        if merged and merged[-1][0] == level:
            merged[-1] = (level, merged[-1][1] + dur)
        else:
            merged.append((level, dur))
    # drop leading space(s)
    while merged and merged[0][0] == 0:
        merged.pop(0)
    # drop trailing space
    while merged and merged[-1][0] == 0:
        merged.pop()
    return [d for _, d in merged]


def enc_rc5(addr: bytes, cmd: bytes) -> list[int]:
    a, c = addr[0] & 0x1F, cmd[0] & 0x7F
    s2 = 0 if (c & 0x40) else 1  # field bit: inverted command bit 6
    bits = [1, s2, 0] + _bits_msb(a, 5) + _bits_msb(c & 0x3F, 6)
    halves: list[tuple[int, int]] = []
    for b in bits:  # RC5 logical 1 = space then mark
        if b:
            halves += [(0, 889), (1, 889)]
        else:
            halves += [(1, 889), (0, 889)]
    return _manchester(halves)


def enc_rc6(addr: bytes, cmd: bytes) -> list[int]:
    a, c = addr[0], cmd[0]
    halves: list[tuple[int, int]] = [(1, 2666), (0, 889)]

    def bit(b: int, half: int) -> None:  # RC6 logical 1 = mark then space
        if b:
            halves.extend([(1, half), (0, half)])
        else:
            halves.extend([(0, half), (1, half)])

    bit(1, 444)                      # start bit
    for b in (0, 0, 0):              # mode 0
        bit(b, 444)
    bit(0, 889)                      # toggle (double width)
    for b in _bits_msb(a, 8) + _bits_msb(c, 8):
        bit(b, 444)
    return _manchester(halves)


ENCODERS = {
    "NEC": enc_nec,
    "NECext": enc_necext,
    "Samsung32": enc_samsung32,
    "SIRC": enc_sirc,
    "SIRC15": enc_sirc15,
    "SIRC20": enc_sirc20,
    "RC5": enc_rc5,
    "RC6": enc_rc6,
    "RCA": enc_rca,
}

# ---------------------------------------------------------------------------
# Flipper .ir parsing
# ---------------------------------------------------------------------------

def parse_ir_file(path: str) -> dict[str, str]:
    """Return {normalized_button_name: broadlink_b64} for one .ir file."""
    signals: dict[str, str] = {}
    cur: dict[str, str] = {}

    def flush() -> None:
        name = cur.get("name")
        if not name:
            cur.clear()
            return
        key = norm(name)
        if key in signals:
            cur.clear()
            return
        b64 = None
        try:
            if cur.get("type") == "raw" and "data" in cur:
                data = [int(x) for x in cur["data"].split()]
                if len(data) % 2:
                    data = data[:-1] or None
                if data:
                    b64 = timings_to_broadlink_b64(data)
            elif cur.get("type") == "parsed":
                proto = cur.get("protocol", "")
                enc = ENCODERS.get(proto)
                if enc:
                    addr = bytes(int(x, 16) for x in cur["address"].split())
                    cmd = bytes(int(x, 16) for x in cur["command"].split())
                    b64 = timings_to_broadlink_b64(enc(addr, cmd))
        except Exception:
            b64 = None
        if b64:
            signals[key] = b64
        cur.clear()

    raw_key = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("#"):
                flush()
                continue
            if ":" not in line:
                if raw_key == "data" and cur.get("type") == "raw":
                    cur["data"] = cur.get("data", "") + " " + line.strip()
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k == "name":
                flush()
            cur[k] = v
            raw_key = k
    flush()
    return signals


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", name.lower())


# ---------------------------------------------------------------------------
# Button mapping
# ---------------------------------------------------------------------------

MP_MAP = {
    "on": ["poweron", "on"],
    "off": ["poweroff", "off"],
    "_powertoggle": ["power", "pwr", "powertoggle", "onoff"],
    "volumeUp": ["volup", "volumeup", "vol+", "volumeplus", "volu", "vol_up"],
    "volumeDown": ["voldown", "volumedown", "vol-", "volumeminus", "vold",
                   "voldn"],
    "mute": ["mute"],
    "nextChannel": ["chup", "ch+", "chnext", "channelup", "chu",
                    "channel+", "progup", "prog+"],
    "previousChannel": ["chdown", "ch-", "chprev", "channeldown", "chd",
                        "channel-", "progdown", "prog-"],
}

MP_SOURCES = {
    "hdmi1": "HDMI1", "hdmi2": "HDMI2", "hdmi3": "HDMI3", "hdmi4": "HDMI4",
    "hdmi": "HDMI", "av": "AV", "av1": "AV1", "av2": "AV2", "tv": "TV",
    "component": "Component", "composite": "Composite", "vga": "VGA",
    "pc": "PC", "usb": "USB", "aux": "AUX", "optical": "Optical",
    "coaxial": "Coaxial", "bluetooth": "Bluetooth", "bt": "Bluetooth",
    "tuner": "Tuner", "cd": "CD", "dvd": "DVD", "phono": "Phono",
    "net": "Network", "game": "Game", "source": "Source", "input": "Source",
}

FAN_SPEED_SETS = [
    {"low": ["low", "speedlow", "speed1", "1", "fan1"],
     "medium": ["med", "medium", "speedmed", "speedmedium", "speed2", "2",
                "fan2"],
     "high": ["high", "speedhigh", "speed3", "3", "fan3"]},
]

LIGHT_MAP = {
    "on": ["on", "poweron"],
    "off": ["off", "poweroff"],
    "_powertoggle": ["power", "onoff"],
    "brighten": ["brightnessup", "brightness+", "brightup", "brighten",
                 "bright+", "dimup"],
    "dim": ["brightnessdown", "brightness-", "brightdown", "dim", "dim-",
            "dimdown"],
    "warmer": ["warm", "warmer", "warmwhite"],
    "colder": ["cold", "colder", "cool", "coolwhite", "coldwhite"],
    "night": ["night", "nightlight", "sleep"],
}


def pick(signals: dict[str, str], aliases: list[str]) -> str | None:
    for a in aliases:
        if a in signals:
            return signals[a]
    return None


def build_media_player(signals):
    cmds: dict = {}
    for key, aliases in MP_MAP.items():
        if key.startswith("_"):
            continue
        v = pick(signals, aliases)
        if v:
            cmds[key] = v
    toggle = pick(signals, MP_MAP["_powertoggle"])
    if toggle:
        cmds.setdefault("on", toggle)
        cmds.setdefault("off", toggle)
    sources = {}
    for alias, label in MP_SOURCES.items():
        if alias in signals and label not in sources:
            sources[label] = signals[alias]
    if sources:
        cmds["sources"] = sources
    # minimum: power + at least one of volume/channel/source
    if "off" not in cmds:
        return None
    if not (set(cmds) & {"volumeUp", "volumeDown", "nextChannel",
                         "previousChannel", "sources", "mute"}):
        return None
    return {"commands": cmds}


def build_fan(signals):
    speeds = {}
    for name, aliases in FAN_SPEED_SETS[0].items():
        v = pick(signals, aliases)
        if v:
            speeds[name] = v
    power = pick(signals, ["power", "poweron", "on", "onoff"])
    off = pick(signals, ["poweroff", "off"]) or power
    osc = pick(signals, ["oscillate", "oscillation", "swing", "rotate"])
    if len(speeds) >= 2 and off:
        cmds = dict(speeds)
        cmds["off"] = off
        if osc:
            cmds["oscillate"] = osc
        return {"speed": list(speeds.keys()), "commands": cmds}
    # cycle-style remote -> toggleMode
    cycle = pick(signals, ["speed", "speedcycle", "fanspeed", "speed+",
                           "speedup"])
    if power and cycle:
        cmds = {"power": power, "speed_cycle": cycle}
        if osc:
            cmds["oscillate"] = osc
        return {"toggleMode": True, "speed": ["low", "medium", "high"],
                "commands": cmds}
    return None


def build_light(signals):
    cmds = {}
    for key, aliases in LIGHT_MAP.items():
        if key.startswith("_"):
            continue
        v = pick(signals, aliases)
        if v:
            cmds[key] = v
    toggle = pick(signals, LIGHT_MAP["_powertoggle"])
    if toggle:
        cmds.setdefault("on", toggle)
        cmds.setdefault("off", toggle)
    if "on" not in cmds or "off" not in cmds:
        return None
    return {
        "brightness": bool({"brighten", "dim"} & set(cmds)),
        "colorTemperature": bool({"warmer", "colder"} & set(cmds)),
        "commands": cmds,
    }


BUILDERS = {"media_player": build_media_player, "fan": build_fan,
            "light": build_light}

DEFAULT_CATEGORIES = {
    "TVs": "media_player",
    "SoundBars": "media_player",
    "Audio_and_Video_Receivers": "media_player",
    "Speakers": "media_player",
    "Projectors": "media_player",
    "Monitors": "media_player",
    "Streaming_Devices": "media_player",
    "Fans": "fan",
    "LED_Lighting": "light",
}


def clean_name(s: str) -> str:
    s = re.sub(r"\.ir$", "", s)
    return re.sub(r"[_]+", " ", s).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="Flipper-IRDB checkout")
    ap.add_argument("--dest", required=True, help="ar_smart_ir codes dir")
    ap.add_argument("--start-id", type=int, default=9000)
    ap.add_argument("--existing", help="existing codes dir for dedup "
                    "(defaults to --dest)")
    args = ap.parse_args()

    existing_dir = args.existing or args.dest
    existing_models: set[tuple[str, str, str]] = set()
    next_id: dict[str, int] = {}
    for plat in ("media_player", "fan", "light"):
        pdir = os.path.join(existing_dir, plat)
        max_id = 0
        if os.path.isdir(pdir):
            for f in os.listdir(pdir):
                if not f.endswith(".json"):
                    continue
                try:
                    max_id = max(max_id, int(f[:-5]))
                    d = json.load(open(os.path.join(pdir, f)))
                    man = str(d.get("manufacturer", "")).lower()
                    for m in d.get("supportedModels", []):
                        existing_models.add((plat, man, str(m).lower()))
                except Exception:
                    pass
        next_id[plat] = max(args.start_id, ((max_id // 100) + 1) * 100)

    written = defaultdict(int)
    skipped = defaultdict(int)
    seen_fp: set = set()

    for category, plat in DEFAULT_CATEGORIES.items():
        cdir = os.path.join(args.src, category)
        if not os.path.isdir(cdir):
            continue
        for root, _dirs, files in os.walk(cdir):
            for fname in sorted(files):
                if not fname.endswith(".ir"):
                    continue
                rel = os.path.relpath(root, cdir)
                brand = clean_name(rel.split(os.sep)[0]) if rel != "." \
                    else clean_name(fname).split(" ")[0]
                model = clean_name(fname)
                if model.lower().startswith(brand.lower()):
                    model_disp = model[len(brand):].strip(" -_") or model
                else:
                    model_disp = model
                if (plat, brand.lower(), model_disp.lower()) in existing_models:
                    skipped["duplicate_model"] += 1
                    continue
                signals = parse_ir_file(os.path.join(root, fname))
                if not signals:
                    skipped["no_signals"] += 1
                    continue
                built = BUILDERS[plat](signals)
                if not built:
                    skipped["insufficient_buttons"] += 1
                    continue
                fp = (plat, json.dumps(built["commands"], sort_keys=True))
                if fp in seen_fp:
                    skipped["duplicate_signals"] += 1
                    continue
                seen_fp.add(fp)
                doc = {
                    "manufacturer": brand,
                    "supportedModels": [model_disp],
                    "supportedController": "Broadlink",
                    "commandsEncoding": "Base64",
                }
                doc.update(built)
                out_dir = os.path.join(args.dest, plat)
                os.makedirs(out_dir, exist_ok=True)
                dev_id = next_id[plat]
                next_id[plat] += 1
                with open(os.path.join(out_dir, f"{dev_id}.json"), "w") as fh:
                    json.dump(doc, fh, indent=2)
                existing_models.add((plat, brand.lower(), model_disp.lower()))
                written[plat] += 1

    print("written:", dict(written))
    print("skipped:", dict(skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())

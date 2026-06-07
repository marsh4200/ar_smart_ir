#!/usr/bin/env python3
"""
irdb_import.py — pull IR codes from the irdb project (probonopd/irdb) and write
ar_smart_ir device-code JSON files.

irdb stores codes as protocol/device/subdevice/function. We render each function
to Pronto hex via ir_render.py and emit files in ar_smart_ir's native schema
with commandsEncoding "Pronto", so the controller-layer transcoder can fan them
out to Broadlink / Xiaomi / MQTT / LOOKin / ESPHome / Tuya unchanged.

Source is fetched from raw.githubusercontent.com (no API key, not rate-limited
like the GitHub contents API).

Examples
--------
# Grow the media_player library with one good code set per TV brand:
python3 tools/irdb_import.py --device-type TV --platform media_player \
    --start-code 40001 --per-brand 1 \
    --out custom_components/ar_smart_ir/codes/media_player

# Just a few named brands, more sets each:
python3 tools/irdb_import.py --device-type TV --platform media_player \
    --brands Samsung,LG,Sony,Panasonic,Hisense,TCL --per-brand 2 --start-code 40001
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ir_render as R  # noqa: E402

RAW = "https://raw.githubusercontent.com/probonopd/irdb/master/codes"
INDEX_URL = f"{RAW}/index"

# ── irdb function name → ar_smart_ir media_player command key ─────────────────

POWER_ON = {"POWER ON", "POWER_ON", "ON"}
POWER_OFF = {"POWER OFF", "POWER_OFF", "OFF", "STANDBY"}
POWER_TOGGLE = {"POWER", "POWER TOGGLE", "POWER/STANDBY"}
VOL_UP = {"VOLUME +", "VOLUME UP", "VOL +", "VOL+", "VOLUME_UP", "VOLUMEUP"}
VOL_DN = {"VOLUME -", "VOLUME DOWN", "VOL -", "VOL-", "VOLUME_DOWN", "VOLUMEDOWN"}
MUTE = {"MUTE", "MUTING", "MUTE/UNMUTE"}
CH_UP = {"CHANNEL +", "CHANNEL UP", "CH +", "CH+", "CHANNEL_UP", "PROG +", "P+"}
CH_DN = {"CHANNEL -", "CHANNEL DOWN", "CH -", "CH-", "CHANNEL_DOWN", "PROG -", "P-"}
SOURCE_KEYS = {
    "INPUT", "INPUT SOURCE", "SOURCE", "TV/AV", "AV", "VIDEO", "HDMI",
    "HDMI 1", "HDMI 2", "HDMI 3", "HDMI1", "HDMI2", "HDMI3", "COMPONENT",
    "PC", "ANTENNA", "TV", "TV/VIDEO", "TV/RADIO",
}


def normalize(name: str) -> str:
    return " ".join(name.strip().upper().split())


def build_commands(rows: list[dict]) -> tuple[dict, dict]:
    """Return (commands, stats). commands is the ar_smart_ir command tree."""
    commands: dict = {}
    sources: dict = {}
    stats = {"rendered": 0, "skipped": 0, "skipped_protocols": defaultdict(int)}

    have_on = have_off = toggle = None

    for row in rows:
        fn = normalize(row["functionname"])
        proto = row["protocol"]
        try:
            dev = int(row["device"])
            sub = int(row["subdevice"])
            func = int(row["function"])
        except (ValueError, KeyError):
            stats["skipped"] += 1
            continue

        try:
            pronto = R.render_pronto(proto, dev, sub, func)
        except NotImplementedError:
            stats["skipped"] += 1
            stats["skipped_protocols"][proto] += 1
            continue
        except Exception:
            stats["skipped"] += 1
            continue

        stats["rendered"] += 1

        # Power
        if fn in POWER_ON:
            have_on = pronto
        elif fn in POWER_OFF:
            have_off = pronto
        elif fn in POWER_TOGGLE:
            toggle = pronto
        # Volume / mute / channel
        elif fn in VOL_UP:
            commands["volumeUp"] = pronto
        elif fn in VOL_DN:
            commands["volumeDown"] = pronto
        elif fn in MUTE:
            commands["mute"] = pronto
        elif fn in CH_UP:
            commands["nextChannel"] = pronto
        elif fn in CH_DN:
            commands["previousChannel"] = pronto
        # Digit keys → "Channel N" so the play_media digit path works
        elif fn.isdigit() and len(fn) == 1:
            sources[f"Channel {fn}"] = pronto
        # Inputs/sources
        elif fn in SOURCE_KEYS:
            sources[fn.title()] = pronto
        else:
            # Keep everything else under its normalized name — harmless extra
            # keys, usable via overrides / future custom buttons.
            key = fn.replace(" ", "_").replace("/", "_")
            commands.setdefault(key, pronto)

    # Resolve power: prefer discrete on/off, fall back to toggle for both.
    commands["on"] = have_on or toggle
    commands["off"] = have_off or toggle
    if commands["on"] is None:
        commands.pop("on")
    if commands["off"] is None:
        commands.pop("off")

    if sources:
        commands["sources"] = sources

    return commands, stats


# ── irdb fetch ────────────────────────────────────────────────────────────────

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ar-smart-ir-importer"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_index() -> list[tuple[str, str, str]]:
    """Return list of (manufacturer, device_type, path)."""
    out = []
    for line in fetch(INDEX_URL).splitlines():
        line = line.strip()
        if not line or not line.endswith(".csv"):
            continue
        parts = line.split("/")
        if len(parts) != 3:
            continue
        out.append((parts[0], parts[1], line))
    return out


def fetch_codeset(path: str) -> list[dict]:
    text = fetch(f"{RAW}/{path}")
    reader = csv.DictReader(io.StringIO(text))
    return [r for r in reader if r.get("functionname")]


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device-type", default="TV",
                    help="irdb device type to import (default: TV)")
    ap.add_argument("--platform", default="media_player",
                    help="ar_smart_ir platform folder name")
    ap.add_argument("--brands", default="",
                    help="comma-separated brand allow-list (default: all)")
    ap.add_argument("--per-brand", type=int, default=1,
                    help="max code sets to import per brand")
    ap.add_argument("--start-code", type=int, default=40001,
                    help="first device_code number to allocate")
    ap.add_argument("--out", default="custom_components/ar_smart_ir/codes/media_player",
                    help="output directory")
    ap.add_argument("--min-commands", type=int, default=4,
                    help="skip code sets that render fewer than this many commands")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    brand_filter = {b.strip().lower() for b in args.brands.split(",") if b.strip()}

    print(f"Fetching irdb index ...", flush=True)
    index = load_index()
    dt = args.device_type.strip().lower()
    by_brand: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for manuf, devtype, path in index:
        if devtype.strip().lower() != dt:
            continue
        if brand_filter and manuf.strip().lower() not in brand_filter:
            continue
        by_brand[manuf].append((devtype, path))

    print(f"{len(by_brand)} matching brands for device type '{args.device_type}'.",
          flush=True)

    code = args.start_code
    written = 0
    total_skipped_protocols: dict[str, int] = defaultdict(int)

    for manuf in sorted(by_brand):
        taken = 0
        for devtype, path in by_brand[manuf]:
            if taken >= args.per_brand:
                break
            try:
                rows = fetch_codeset(path)
            except Exception as e:
                print(f"  ! {path}: fetch failed ({e})")
                continue

            commands, stats = build_commands(rows)
            for p, n in stats["skipped_protocols"].items():
                total_skipped_protocols[p] += n

            cmd_count = sum(1 for k in commands if k != "sources") + \
                len(commands.get("sources", {}))
            if cmd_count < args.min_commands:
                continue

            device, subdevice = (path.rsplit("/", 1)[-1][:-4].split(","))
            doc = {
                "manufacturer": manuf,
                "supportedModels": [f"{devtype} (irdb {device},{subdevice})"],
                "supportedController": "Broadlink",
                "commandsEncoding": "Pronto",
                "commands": commands,
            }

            out_path = os.path.join(args.out, f"{code}.json")
            if args.dry_run:
                print(f"  [dry] {code}.json  {manuf:18} cmds={cmd_count} "
                      f"(rendered {stats['rendered']}, skipped {stats['skipped']})")
            else:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(doc, f, indent=2, ensure_ascii=False)
                print(f"  + {code}.json  {manuf:18} cmds={cmd_count} "
                      f"(rendered {stats['rendered']}, skipped {stats['skipped']})")
            code += 1
            written += 1
            taken += 1

    print()
    print(f"Done. {written} device files {'would be' if args.dry_run else ''} "
          f"written to {args.out} (codes {args.start_code}..{code - 1}).")
    if total_skipped_protocols:
        top = sorted(total_skipped_protocols.items(), key=lambda x: -x[1])[:8]
        print("Unsupported protocols skipped (count):",
              ", ".join(f"{p}×{n}" for p, n in top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

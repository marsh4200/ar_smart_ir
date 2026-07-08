"""Self-healing codeset importer.

Anything dropped into <config>/ar_smart_ir_import/ is materialised into the
canonical store <config>/ar_smart_ir_codes/<platform>/<code>.json on startup,
on integration reload, and on demand via the ar_smart_ir.import_codes service.

Why this exists
---------------
The canonical store already survives HACS updates (it lives outside the
integration folder). This importer adds a single, obvious "drop a file and it
appears" inbox that also *self-heals*: if a codeset in the canonical store goes
missing (a bad restore, a manual delete), the next import re-creates it with the
SAME code number, so entities configured against that code keep working.

Design rules
------------
- The inbox is the *source*, the canonical store is the *truth*. If a codeset
  already exists in the store we DO NOT overwrite it — a user may have edited it
  (e.g. renamed the manufacturer). We only ever *create* what's missing.
- Code numbers are stable. Numeric-named inbox files (9001.json) keep their
  number. Everything else is assigned the next free 9000+ slot and remembered in
  a ledger that lives *with the inbox* (.assigned.json), so backing up the inbox
  backs up the numbering too.
- Platform is resolved deterministically: inbox sub-folder name > explicit
  "platform" key in the payload > detected from the codeset's shape.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from .helpers import CONFIG_ABS_DIR, get_codes_dir, get_custom_codes_dir

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ("climate", "fan", "light", "media_player")
IMPORT_ROOT = os.path.join(CONFIG_ABS_DIR, "ar_smart_ir_import")
_LEDGER_NAME = ".assigned.json"
_FIRST_CUSTOM_CODE = 9000

_README = """\
AR Smart IR — codeset import inbox
==================================

Drop ar_smart_ir codeset JSON files in here (optionally inside a platform
sub-folder: climate/ fan/ light/ media_player/). On the next Home Assistant
start, integration reload, or when you call the ar_smart_ir.import_codes
service, each file is installed into:

    /config/ar_smart_ir_codes/<platform>/<code>.json

...which is what the integration's add-device list reads from.

Notes
-----
* You do NOT edit files here to change a codeset. Edit the installed copy in
  /config/ar_smart_ir_codes/<platform>/<code>.json instead — this inbox never
  overwrites an existing installed codeset.
* Numeric filenames (e.g. 9001.json) keep that code. Any other name is given the
  next free number starting at 9000, remembered in .assigned.json.
* Safe to leave files here — re-running import won't create duplicates.
"""


class ImportError_(Exception):
    """A drop-in file can't be turned into a codeset."""


def detect_platform(payload: dict[str, Any]) -> str | None:
    """Best-effort platform from a codeset's shape."""
    if "operationModes" in payload or "minTemperature" in payload or "maxTemperature" in payload:
        return "climate"
    if "speed" in payload:
        return "fan"
    if "brightness" in payload or "colorTemperature" in payload:
        return "light"
    if isinstance(payload.get("commands"), dict):
        return "media_player"
    return None


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip importer control keys so the written codeset stays standard."""
    return {
        k: v
        for k, v in payload.items()
        if k != "platform" and not k.startswith("_")
    }


def _content_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _load_ledger() -> dict[str, dict[str, int]]:
    path = os.path.join(IMPORT_ROOT, _LEDGER_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # { platform: { hash: code } }
    return {p: v for p, v in data.items() if isinstance(v, dict)}


def _save_ledger(ledger: dict[str, dict[str, int]]) -> None:
    path = os.path.join(IMPORT_ROOT, _LEDGER_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except OSError as err:
        _LOGGER.warning("AR Smart IR import: couldn't write ledger %s: %s", path, err)


def _used_codes(platform: str, ledger: dict[str, dict[str, int]]) -> set[int]:
    used: set[int] = set()
    for directory in (get_codes_dir(platform), get_custom_codes_dir(platform)):
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            stem = filename[:-5] if filename.endswith(".json") else ""
            if stem.isdigit():
                used.add(int(stem))
    used.update(ledger.get(platform, {}).values())
    return used


def _next_free_code(used: set[int]) -> int:
    code = _FIRST_CUSTOM_CODE
    while code in used:
        code += 1
    return code


def _iter_dropins() -> list[tuple[str, str | None]]:
    """(path, platform_hint) for each JSON in the inbox, ledger/hidden aside."""
    found: list[tuple[str, str | None]] = []
    for root, _dirs, files in os.walk(IMPORT_ROOT):
        rel = os.path.relpath(root, IMPORT_ROOT)
        parts = [] if rel == "." else rel.split(os.sep)
        hint = parts[0] if parts and parts[0] in PLATFORMS else None
        for filename in sorted(files):
            if not filename.endswith(".json") or filename.startswith("."):
                continue
            found.append((os.path.join(root, filename), hint))
    return found


def import_dropins() -> dict[str, Any]:
    """Install everything in the inbox into the canonical store.

    Returns a summary dict: {imported, restored, skipped, errors}.
    Never raises — file/JSON problems are collected into `errors`.
    """
    summary: dict[str, Any] = {
        "imported": [],   # newly assigned a code and written
        "restored": [],   # a known codeset re-created because it was missing
        "skipped": [],    # already present, nothing to do
        "errors": [],     # couldn't be processed
    }

    try:
        os.makedirs(IMPORT_ROOT, exist_ok=True)
        readme = os.path.join(IMPORT_ROOT, "README.txt")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as f:
                f.write(_README)
    except OSError as err:
        _LOGGER.warning("AR Smart IR import: inbox not usable (%s)", IMPORT_ROOT)
        summary["errors"].append(f"{IMPORT_ROOT}: {err}")
        return summary

    dropins = _iter_dropins()
    if not dropins:
        return summary

    ledger = _load_ledger()
    ledger_dirty = False

    for path, hint in dropins:
        name = os.path.relpath(path, IMPORT_ROOT)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as err:
            summary["errors"].append(f"{name}: unreadable ({err})")
            continue

        if not isinstance(raw, dict) or not isinstance(raw.get("commands"), (dict, list)):
            summary["errors"].append(f"{name}: not an ar_smart_ir codeset")
            continue

        explicit = raw.get("platform")
        platform = hint or (explicit if explicit in PLATFORMS else None) or detect_platform(raw)
        if platform not in PLATFORMS:
            summary["errors"].append(
                f"{name}: can't tell which platform this is — put it in a "
                f"climate/ fan/ light/ media_player/ sub-folder"
            )
            continue

        payload = _clean_payload(raw)
        digest = _content_hash(payload)
        plat_ledger = ledger.setdefault(platform, {})
        known_before = digest in plat_ledger

        stem = os.path.splitext(os.path.basename(path))[0]
        if stem.isdigit():
            code = int(stem)
        elif known_before:
            code = plat_ledger[digest]
        else:
            code = _next_free_code(_used_codes(platform, ledger))

        if plat_ledger.get(digest) != code:
            plat_ledger[digest] = code
            ledger_dirty = True

        target_dir = get_custom_codes_dir(platform)
        target = os.path.join(target_dir, f"{code}.json")
        label = f"{platform}/{code} ({payload.get('manufacturer', 'Unknown')})"

        if os.path.isfile(target):
            # Never clobber an installed codeset — the user may have edited it.
            summary["skipped"].append(label)
            continue

        try:
            os.makedirs(target_dir, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError as err:
            summary["errors"].append(f"{name}: write failed ({err})")
            continue

        # Seen this exact codeset before but the installed file was gone -> a
        # self-heal restore. Never-seen content -> a fresh import.
        if known_before:
            summary["restored"].append(label)
        else:
            summary["imported"].append(label)

    if ledger_dirty:
        _save_ledger(ledger)

    total = len(summary["imported"]) + len(summary["restored"])
    if total or summary["errors"]:
        _LOGGER.info(
            "AR Smart IR import: %d installed, %d already present, %d error(s)",
            total,
            len(summary["skipped"]),
            len(summary["errors"]),
        )
    for err in summary["errors"]:
        _LOGGER.warning("AR Smart IR import: %s", err)

    return summary

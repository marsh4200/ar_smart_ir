#!/usr/bin/env python3
"""Repo guard checks for PRs: protected paths, brand assets, file hygiene,
duplicate codesets. Usage: repo_guard.py <changed-files-list> <is_maintainer>"""
import json, os, struct, sys, base64, glob

CODES_DIR = "custom_components/ar_smart_ir/codes"
BRAND_DIR = "custom_components/ar_smart_ir/brand"
PROTECTED = (".github/", "hacs.json", "LICENSE",
             "custom_components/ar_smart_ir/manifest.json", "tools/")
MAX_FILE_BYTES = 1_000_000
BRAND_SPEC = {"icon.png": 256, "icon@2x.png": 512,
              "logo.png": 256, "logo@2x.png": 512}

def png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return w, h

def main():
    changed = [l.strip() for l in open(sys.argv[1]) if l.strip()]
    is_maintainer = sys.argv[2].lower() == "true"
    errors, warnings = [], []

    # 1. protected paths (non-maintainer PRs only)
    if not is_maintainer:
        hits = [f for f in changed if f.startswith(PROTECTED) or f in PROTECTED]
        for f in hits:
            errors.append(f"protected path changed by non-maintainer: {f}")

    # 2. brand assets must exist and be well-formed square PNGs
    for name, size in BRAND_SPEC.items():
        p = os.path.join(BRAND_DIR, name)
        if not os.path.isfile(p):
            errors.append(f"missing brand asset: {p}")
            continue
        dims = png_size(p)
        if not dims:
            errors.append(f"brand asset is not a valid PNG: {p}")
        elif dims != (size, size):
            errors.append(f"brand asset {name} is {dims[0]}x{dims[1]}, expected {size}x{size}")

    # 3. file hygiene on changed files that still exist
    for f in changed:
        if not os.path.isfile(f):
            continue  # deleted in PR
        sz = os.path.getsize(f)
        if sz > MAX_FILE_BYTES:
            errors.append(f"file too large ({sz} bytes, max {MAX_FILE_BYTES}): {f}")
        if f.startswith(CODES_DIR) and not f.endswith(".json"):
            errors.append(f"only .json files allowed under codes/: {f}")

    # 4. duplicate codeset detection (changed codes vs whole repo)
    def signals(doc):
        out = set()
        def walk(v):
            if isinstance(v, dict):
                for x in v.values(): walk(x)
            elif isinstance(v, str) and len(v) > 16:
                out.add(v)
        walk(doc.get("commands", {}))
        return frozenset(out)

    changed_codes = [f for f in changed
                     if f.startswith(CODES_DIR) and f.endswith(".json")
                     and os.path.isfile(f)]
    if changed_codes:
        index = {}
        for p in glob.glob(f"{CODES_DIR}/*/*.json"):
            try:
                index.setdefault(signals(json.load(open(p))), []).append(p)
            except Exception:
                pass
        for f in changed_codes:
            try:
                sig = signals(json.load(open(f)))
            except Exception as e:
                errors.append(f"unparseable codeset {f}: {e}")
                continue
            twins = [p for p in index.get(sig, []) if p != f]
            if twins and sig:
                errors.append(f"{f} duplicates existing codeset(s): {', '.join(twins)}")

    for w in warnings: print(f"::warning::{w}")
    for e in errors: print(f"::error::{e}")
    print(f"repo guard: {len(errors)} error(s), {len(warnings)} warning(s), "
          f"{len(changed)} changed file(s) inspected")
    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main())

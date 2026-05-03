"""
Tuya IR-blaster codec.

Tuya IR codes are an opaque Base64 string that wraps a stream of unsigned
little-endian 16-bit microsecond durations. Cloud / Zigbee blasters compress
this stream with a Tuya-specific FastLZ variant; some newer Wi-Fi devices
(e.g. some S06 firmwares) ship the raw u16le stream Base64'd with no
compression at all.

This module exposes two things controller.py cares about:

    decode_tuya(code)   -> list[int]   unsigned us pulses (mark, space, ...)
    encode_tuya(pulses) -> str         Base64 Tuya code string

`decode_tuya` automatically falls back from FastLZ to plain u16le if the
header byte does not look like a valid FastLZ literal block, so it works
for both the compressed and uncompressed Tuya variants seen in the wild.

The FastLZ implementation is based on the public reverse-engineering work
by Alba "mildsunrise" Mendez (gist 1d576669b63a260d2cff35fda63ec0b5).

NOTE: Tuya pulses are strictly positive 16-bit values; a value of 0 or one
that overflows u16 is clamped before encoding. The first pulse is always a
mark (carrier on), then alternating space/mark, matching the SmartIR-style
LIRC representation used elsewhere in this integration.
"""

from __future__ import annotations

import base64
import io
import logging
from bisect import bisect
from struct import pack, unpack

_LOGGER = logging.getLogger(__name__)

# ── public API ───────────────────────────────────────────────────────────────


def decode_tuya(code: str) -> list[int]:
    """Decode a Tuya IR-blaster Base64 string to a list of unsigned us pulses."""
    if not isinstance(code, str):
        raise ValueError("Tuya IR code must be a string.")

    cleaned = code.strip().replace("\n", "").replace("\r", "")
    try:
        payload = base64.b64decode(cleaned, validate=False)
    except Exception as err:  # noqa: BLE001
        raise ValueError(f"Tuya IR code is not valid Base64: {err}") from err

    if len(payload) < 2:
        raise ValueError("Tuya IR payload is too short.")

    # First try FastLZ; if the header looks bogus or decompression fails,
    # fall back to treating the payload as plain u16le.
    decompressed: bytes | None = None
    if _looks_like_fastlz(payload):
        try:
            decompressed = _decompress(io.BytesIO(payload))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Tuya FastLZ decode failed, falling back to u16le: %s", err)
            decompressed = None

    if decompressed is None:
        decompressed = payload

    if len(decompressed) % 2:
        raise ValueError("Tuya IR pulse stream has an odd byte length.")

    return [unpack("<H", decompressed[i : i + 2])[0] for i in range(0, len(decompressed), 2)]


def encode_tuya(pulses, compression_level: int = 2) -> str:
    """Encode a list of unsigned us pulses to a Tuya IR Base64 string."""
    if not pulses:
        raise ValueError("Tuya IR pulse list is empty.")

    cleaned: list[int] = []
    for value in pulses:
        try:
            v = int(round(abs(float(value))))
        except (TypeError, ValueError) as err:
            raise ValueError(f"Invalid pulse value: {value!r}") from err
        # u16 clamp — Tuya can't represent gaps longer than 65535us
        if v < 1:
            v = 1
        elif v > 0xFFFF:
            v = 0xFFFF
        cleaned.append(v)

    payload = b"".join(pack("<H", v) for v in cleaned)
    out = io.BytesIO()
    _compress(out, payload, compression_level)
    return base64.b64encode(out.getvalue()).decode("ascii")


# ── implementation ──────────────────────────────────────────────────────────


def _looks_like_fastlz(payload: bytes) -> bool:
    """Cheap heuristic: a real FastLZ-Tuya stream begins with a literal block,
    i.e. the top 3 bits of byte 0 are 000 and the declared run fits in payload.
    """
    if not payload:
        return False
    head = payload[0]
    if head >> 5 != 0:
        return False
    literal_run = (head & 0b11111) + 1
    return len(payload) >= 1 + literal_run


def _decompress(inf: io.BytesIO) -> bytes:
    out = bytearray()
    while True:
        header = inf.read(1)
        if not header:
            break
        L = header[0] >> 5
        D = header[0] & 0b11111
        if L == 0:
            # literal block
            length = D + 1
            data = inf.read(length)
            if len(data) != length:
                raise ValueError("Truncated FastLZ literal block.")
            out.extend(data)
        else:
            # length-distance pair
            if L == 7:
                ext = inf.read(1)
                if not ext:
                    raise ValueError("Truncated FastLZ extended length.")
                L += ext[0]
            L += 2
            tail = inf.read(1)
            if not tail:
                raise ValueError("Truncated FastLZ distance byte.")
            D = (D << 8 | tail[0]) + 1
            if len(out) < D:
                raise ValueError("FastLZ back-reference points before start of stream.")
            data = bytearray()
            while len(data) < L:
                data.extend(out[-D:][: L - len(data)])
            out.extend(data)
    return bytes(out)


def _emit_literal_block(out: io.BytesIO, data: bytes) -> None:
    length = len(data) - 1
    if not 0 <= length < (1 << 5):
        raise ValueError("FastLZ literal block length out of range.")
    out.write(bytes([length]))
    out.write(data)


def _emit_literal_blocks(out: io.BytesIO, data: bytes) -> None:
    for i in range(0, len(data), 32):
        _emit_literal_block(out, data[i : i + 32])


def _emit_distance_block(out: io.BytesIO, length: int, distance: int) -> None:
    distance -= 1
    if not 0 <= distance < (1 << 13):
        raise ValueError("FastLZ distance out of range.")
    length -= 2
    if length <= 0:
        raise ValueError("FastLZ length out of range.")
    block = bytearray()
    if length >= 7:
        if length - 7 >= (1 << 8):
            raise ValueError("FastLZ extended length overflow.")
        block.append(length - 7)
        length = 7
    block.insert(0, length << 5 | distance >> 8)
    block.append(distance & 0xFF)
    out.write(block)


def _compress(out: io.BytesIO, data: bytes, level: int = 2) -> None:
    """Tuya FastLZ compressor.

    Levels:
        0 - copy literal blocks only
        1 - greedy first-match
        2 - greedy best-match using a sorted-suffix index (default)
    """
    if level == 0 or not data:
        _emit_literal_blocks(out, data)
        return

    W = 2**13  # 8 KB window
    L = 255 + 9  # max length
    # Defaults overridden per-level below
    pos = 0  # noqa: F841 — referenced via closure below

    def find_length_for_distance(start: int, current_pos: int) -> int:
        length = 0
        limit = min(L, len(data) - current_pos)
        while length < limit and data[current_pos + length] == data[start + length]:
            length += 1
        return length

    if level >= 2:
        suffixes: list[int] = []
        next_pos = 0

        def key(n: int):
            return data[n:]

        def find_idx(n: int) -> int:
            return bisect(suffixes, key(n), key=key)

        def distance_candidates(current_pos: int):
            nonlocal next_pos
            while next_pos <= current_pos:
                if len(suffixes) == W:
                    suffixes.pop(find_idx(next_pos - W))
                idx = find_idx(next_pos)
                suffixes.insert(idx, next_pos)
                next_pos += 1
            idxs = (idx + i for i in (+1, -1))
            return [
                current_pos - suffixes[i]
                for i in idxs
                if 0 <= i < len(suffixes)
            ]
    else:

        def distance_candidates(current_pos: int):  # type: ignore[no-redef]
            return range(1, min(current_pos, W) + 1)

    block_start = 0
    pos = 0
    while pos < len(data):
        best = None
        for d in distance_candidates(pos):
            ln = find_length_for_distance(pos - d, pos)
            if best is None or (ln, -d) > (best[0], -best[1]):
                best = (ln, d)
            if level == 1 and best and best[0] >= 3:
                break

        if best and best[0] >= 3:
            if pos > block_start:
                _emit_literal_blocks(out, data[block_start:pos])
            _emit_distance_block(out, best[0], best[1])
            pos += best[0]
            block_start = pos
        else:
            pos += 1

    if pos > block_start:
        _emit_literal_blocks(out, data[block_start:pos])

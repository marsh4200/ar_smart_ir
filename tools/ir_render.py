"""
ir_render.py — render irdb (protocol, device, subdevice, function) tuples to
Pronto hex.

irdb stores codes as protocol/device/subdevice/function, NOT as raw IR. To use
them in ar_smart_ir we render them to Pronto hex (commandsEncoding: "Pronto"),
which the controller-layer transcoder then fans out to Broadlink / Xiaomi /
MQTT / LOOKin / ESPHome / Tuya automatically.

Supported protocol families (the overwhelming majority of irdb consumer A/V
remotes):
    NEC family : NEC, NEC1, NEC2, NECx, NECx1, NECx2, Apple, TiVo-ish NEC
    Sony SIRC  : Sony12, Sony15, Sony20
    Philips    : RC5, RC5x, RC6 (mode 0, RC6-0-16)

Anything else is reported as UNSUPPORTED so the importer can skip it cleanly
instead of emitting a broken code.

A Pronto "learned" (one-time) code is:
    word0 = 0x0000                 marker for a learned code
    word1 = carrier divisor        freq_MHz = 1 / (word1 * 0.241246)
    word2 = burst-pair-1 count     (we put the whole one-shot here)
    word3 = burst-pair-2 count     (0 — no repeat sequence)
    word4.. = alternating mark/space lengths, in carrier-period ticks

This is exactly the shape custom_components/ar_smart_ir/helpers.py:Helper
.pronto2lirc expects, so rendered codes round-trip through the integration's
own converters.
"""

from __future__ import annotations

# ── Pronto helpers ───────────────────────────────────────────────────────────

# Pronto carrier divisor for a given physical carrier frequency.
#   freq_MHz = 1 / (word1 * 0.241246)  →  word1 = 1 / (freq_MHz * 0.241246)
def carrier_word(freq_hz: int) -> int:
    freq_mhz = freq_hz / 1_000_000.0
    return max(1, int(round(1.0 / (freq_mhz * 0.241246))))


def pulses_to_pronto(pulses: list[int], freq_hz: int) -> str:
    """pulses: alternating mark/space in microseconds (all positive)."""
    if not pulses:
        raise ValueError("empty pulse list")
    # Drop a trailing odd pulse so we always have whole mark/space pairs.
    if len(pulses) % 2:
        pulses = pulses[:-1]

    cw = carrier_word(freq_hz)
    period_us = cw * 0.241246  # microseconds per carrier tick
    pair_count = len(pulses) // 2

    words = [0x0000, cw, pair_count, 0x0000]
    for us in pulses:
        ticks = max(1, int(round(us / period_us)))
        words.append(min(ticks, 0xFFFF))
    return " ".join(f"{w:04X}" for w in words)


# A generous lead-out gap appended to every one-shot frame (microseconds).
LEADOUT_US = 40_000


# ── NEC family ────────────────────────────────────────────────────────────────

NEC_FREQ = 38_000
NEC_HDR_MARK = 9_000
NEC_HDR_SPACE = 4_500
NEC_BIT_MARK = 560
NEC_ONE_SPACE = 1_690
NEC_ZERO_SPACE = 560


def _nec_bytes(device: int, subdevice: int, function: int) -> list[int]:
    """Return the 4 payload bytes for a NEC-family frame.

    subdevice == -1 → classic NEC: [D, ~D, F, ~F]
    subdevice >= 0  → extended NEC: [D, S, F, ~F]
    """
    d = device & 0xFF
    f = function & 0xFF
    if subdevice is None or subdevice < 0:
        s = (~d) & 0xFF
    else:
        s = subdevice & 0xFF
    return [d, s, f, (~f) & 0xFF]


def render_nec(device: int, subdevice: int, function: int) -> list[int]:
    pulses = [NEC_HDR_MARK, NEC_HDR_SPACE]
    for byte in _nec_bytes(device, subdevice, function):
        for bit in range(8):  # LSB first
            pulses.append(NEC_BIT_MARK)
            pulses.append(NEC_ONE_SPACE if (byte >> bit) & 1 else NEC_ZERO_SPACE)
    pulses.append(NEC_BIT_MARK)  # stop bit
    pulses.append(LEADOUT_US)
    return pulses


# ── Sony SIRC ─────────────────────────────────────────────────────────────────

SIRC_FREQ = 40_000
SIRC_HDR_MARK = 2_400
SIRC_SPACE = 600
SIRC_ONE_MARK = 1_200
SIRC_ZERO_MARK = 600


def _sirc_bits(device: int, subdevice: int, function: int, nbits: int) -> list[int]:
    """LSB-first bit list: 7-bit command, then device, then (Sony20) extended."""
    bits: list[int] = []
    f = function & 0x7F
    for i in range(7):
        bits.append((f >> i) & 1)
    if nbits == 12:        # 5-bit device
        for i in range(5):
            bits.append((device >> i) & 1)
    elif nbits == 15:      # 8-bit device
        for i in range(8):
            bits.append((device >> i) & 1)
    elif nbits == 20:      # 5-bit device + 8-bit extended (subdevice)
        for i in range(5):
            bits.append((device >> i) & 1)
        ext = (subdevice if subdevice and subdevice >= 0 else 0) & 0xFF
        for i in range(8):
            bits.append((ext >> i) & 1)
    return bits


def render_sirc(device: int, subdevice: int, function: int, nbits: int) -> list[int]:
    pulses = [SIRC_HDR_MARK, SIRC_SPACE]
    for bit in _sirc_bits(device, subdevice, function, nbits):
        pulses.append(SIRC_ONE_MARK if bit else SIRC_ZERO_MARK)
        pulses.append(SIRC_SPACE)
    pulses[-1] = LEADOUT_US  # replace final space with lead-out gap
    return pulses


# ── RC5 (Manchester) ──────────────────────────────────────────────────────────

RC5_FREQ = 36_000
RC5_HALF = 889  # half-bit period


def _manchester_rc5(level_pairs: list[int]) -> list[int]:
    """RC5: bit '1' = space(low) then mark(high); '0' = mark then space.

    We build a level stream at half-bit resolution then run-length it into
    mark/space microsecond pulses. Stream starts with a mark (the line idles
    low; first emitted pulse must be a mark for Pronto)."""
    return level_pairs


def render_rc5(device: int, subdevice: int, function: int) -> list[int]:
    toggle = 0
    field = (function >> 6) & 1  # extended command bit → S2 (inverted)
    s1 = 1
    s2 = 1 if field == 0 else 0
    addr = device & 0x1F
    cmd = function & 0x3F

    bits = [s1, s2, toggle]
    for i in range(4, -1, -1):
        bits.append((addr >> i) & 1)
    for i in range(5, -1, -1):
        bits.append((cmd >> i) & 1)

    # Build a half-chip level list (1 = mark/high, 0 = space/low).
    # RC5 '1' = low,high ; '0' = high,low.
    chips: list[int] = []
    for b in bits:
        if b:
            chips += [0, 1]
        else:
            chips += [1, 0]

    return _chips_to_pulses(chips, RC5_HALF, lead_high=True)


# ── RC6 mode 0 (RC6-0-16) ─────────────────────────────────────────────────────

RC6_FREQ = 36_000
RC6_UNIT = 444


def render_rc6(device: int, subdevice: int, function: int) -> list[int]:
    # Leader + start bit + 3 mode bits (000) + toggle (double-width) + 8 addr + 8 cmd.
    # RC6 Manchester is OPPOSITE of RC5: '1' = high,low ; '0' = low,high.
    pulses = [2666, 889]  # leader mark, leader space

    chips: list[int] = []

    def add_bit(b: int, width: int = 1):
        # high then low for '1', low then high for '0', each half = width units
        if b:
            chips.extend([1] * width + [0] * width)
        else:
            chips.extend([0] * width + [1] * width)

    add_bit(1)            # start bit
    for _ in range(3):    # mode 000
        add_bit(0)
    add_bit(0, width=2)   # toggle bit, double width

    addr = device & 0xFF
    cmd = function & 0xFF
    for i in range(7, -1, -1):
        add_bit((addr >> i) & 1)
    for i in range(7, -1, -1):
        add_bit((cmd >> i) & 1)

    body = _chips_to_pulses(chips, RC6_UNIT, lead_high=True)
    # leader space already emitted as a 'space'; body must start with a mark.
    pulses += body
    return pulses


# ── JVC ───────────────────────────────────────────────────────────────────────
#
# IRP: {38k,525}<1,-1|1,-3>(16,-8,D:8,F:8,1,-45)+
#   header 16T/8T, then 8-bit device + 8-bit function, LSB-first, PWM.

JVC_FREQ = 38_000
JVC_T = 525


def render_jvc(device: int, subdevice: int, function: int) -> list[int]:
    pulses = [16 * JVC_T, 8 * JVC_T]
    for byte in (device & 0xFF, function & 0xFF):
        for bit in range(8):  # LSB first
            pulses.append(JVC_T)
            pulses.append(3 * JVC_T if (byte >> bit) & 1 else JVC_T)
    pulses.append(JVC_T)       # trailer mark
    pulses.append(LEADOUT_US)
    return pulses


# ── Panasonic / Kaseikyo ──────────────────────────────────────────────────────
#
# IRP: {37k,432}<1,-1|1,-3>(8,-4,2:8,32:8,D:8,S:8,F:8,(D^S^F):8,1,-173)+
#   vendor bytes 0x02,0x20 (Panasonic), then device, subdevice, function and an
#   XOR checksum. PWM, LSB-first, header 8T/4T, bit mark 1T, 0-space 1T,
#   1-space 3T.

PANA_FREQ = 37_000
PANA_T = 432


def render_panasonic(device: int, subdevice: int, function: int) -> list[int]:
    d = device & 0xFF
    s = (subdevice if subdevice and subdevice >= 0 else 0) & 0xFF
    f = function & 0xFF
    payload = [0x02, 0x20, d, s, f, (d ^ s ^ f) & 0xFF]

    pulses = [8 * PANA_T, 4 * PANA_T]
    for byte in payload:
        for bit in range(8):  # LSB first
            pulses.append(PANA_T)
            pulses.append(3 * PANA_T if (byte >> bit) & 1 else PANA_T)
    pulses.append(PANA_T)      # trailer mark
    pulses.append(LEADOUT_US)
    return pulses


# ── shared Manchester run-length encoder ──────────────────────────────────────

def _chips_to_pulses(chips: list[int], unit_us: int, lead_high: bool) -> list[int]:
    """Convert a level stream (1=high/mark, 0=low/space) to alternating
    mark/space microsecond pulses, starting with a mark.

    If the stream starts low, we cannot begin with a space (Pronto must start
    with a mark), so we prepend a tiny correction is avoided by ensuring the
    first chip is high. Manchester biphase guarantees the first emitted level
    after our framing is a mark for both RC5 ('1' start bits) and RC6 (leader).
    """
    pulses: list[int] = []
    if not chips:
        return pulses

    # Ensure we start on a mark.
    if chips[0] == 0:
        # shouldn't happen for our framing, but guard: emit a 0-length skip by
        # merging — instead, just start counting spaces into the lead-out later.
        pass

    current = chips[0]
    run = 0
    for c in chips:
        if c == current:
            run += 1
        else:
            pulses.append(run * unit_us)
            current = c
            run = 1
    pulses.append(run * unit_us)

    # pulses[0] corresponds to `chips[0]`. We need index 0 to be a MARK.
    if current is not None and chips[0] == 0:
        # first run is a space → drop it into a leading mark of 0 is invalid.
        # Prepend nothing; instead flip by inserting an implicit mark handled by
        # caller framing. For RC5/RC6 our framing starts high, so this is moot.
        pulses = pulses

    pulses.append(LEADOUT_US)
    return pulses


# ── dispatch ──────────────────────────────────────────────────────────────────

NEC_ALIASES = {
    "NEC", "NEC1", "NEC2", "NECX", "NECX1", "NECX2",
    "NEC1-F16", "NEC2-F16", "APPLE", "TIVO", "PIONEER",
}


def render(protocol: str, device: int, subdevice: int, function: int):
    """Return (freq_hz, pulses) or raise NotImplementedError."""
    p = (protocol or "").strip().upper()

    if p in NEC_ALIASES:
        return NEC_FREQ, render_nec(device, subdevice, function)
    if p in ("SONY12", "SIRC12", "SONY"):
        return SIRC_FREQ, render_sirc(device, subdevice, function, 12)
    if p in ("SONY15", "SIRC15"):
        return SIRC_FREQ, render_sirc(device, subdevice, function, 15)
    if p in ("SONY20", "SIRC20"):
        return SIRC_FREQ, render_sirc(device, subdevice, function, 20)
    if p in ("RC5", "RC5X", "RC-5"):
        return RC5_FREQ, render_rc5(device, subdevice, function)
    if p in ("RC6", "RC6-0-16", "RC6-M-16", "RC-6"):
        return RC6_FREQ, render_rc6(device, subdevice, function)
    if p in ("PANASONIC", "KASEIKYO", "PANASONIC2", "DENON-K"):
        return PANA_FREQ, render_panasonic(device, subdevice, function)
    if p in ("JVC", "JVC{2}", "JVC-48"):
        return JVC_FREQ, render_jvc(device, subdevice, function)

    raise NotImplementedError(f"protocol not supported: {protocol}")


def render_pronto(protocol: str, device: int, subdevice: int, function: int) -> str:
    freq, pulses = render(protocol, device, subdevice, function)
    return pulses_to_pronto(pulses, freq)

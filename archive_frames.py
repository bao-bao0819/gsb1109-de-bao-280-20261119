"""Read and correct collector archive frames with bit-interleaved channels.

Frame layout (recorder bit order - the stored string is never reversed as
a whole, only individual slots are adjusted):

    mode: u1    0 = dual channel, 1 = single channel     } 3-bit
    tag:  u2    recorder tag, plain order                } header
    payload     dual: 24 interleaved bits / single: a: u12
    flag: u1    independent trailing flag, plain order

Dual-channel interleave: slot pair ``i`` holds A's bit ``11 - i`` followed
by B's bit ``i``, so A advances MSB -> LSB while B advances LSB -> MSB,
each slot moving towards the opposite end. Each channel is 12 bits.

Single-channel mode stores A's 12 bits contiguously in their original
order, so the trailing flag sits at a different position per mode.
"""

from __future__ import annotations

from dataclasses import dataclass

from bitformat import Format, If, Tibs, TibsLike

__all__ = [
    "CHANNEL_BITS",
    "FrameReading",
    "make_frame_format",
    "interleave_channels",
    "deinterleave_channels",
    "build_archive_frame",
    "read_frame",
    "correct_channel_a",
    "channel_a_slots",
    "channel_b_slots",
    "flag_position",
]

CHANNEL_BITS = 12

MODE_DUAL = 0
MODE_SINGLE = 1

_HEADER_BITS = 3  # mode: u1 + tag: u2


def make_frame_format() -> Format:
    """Create the single Format that handles both archive frame modes."""
    dual_fields = []
    for i in range(CHANNEL_BITS):
        dual_fields.append(f"a{CHANNEL_BITS - 1 - i}: u1")
        dual_fields.append(f"b{i}: u1")
    return Format.from_params([
        "mode: u1",
        "tag: u2",
        If.from_params(
            "{mode == 0}",
            Format.from_params(dual_fields, name="dual_payload"),
            Format.from_params(["a: u12"], name="single_payload"),
        ),
        "flag: u1",
    ])


def interleave_channels(a: int, b: int) -> str:
    """Interleave two 12-bit channel values into recorder bit order.

    Slot pair ``i`` is A's bit ``11 - i`` then B's bit ``i``.
    """
    if not 0 <= a < 1 << CHANNEL_BITS or not 0 <= b < 1 << CHANNEL_BITS:
        raise ValueError("Channel values must fit in 12 bits.")
    slots = []
    for i in range(CHANNEL_BITS):
        slots.append(str((a >> (CHANNEL_BITS - 1 - i)) & 1))
        slots.append(str((b >> i) & 1))
    return "".join(slots)


def deinterleave_channels(payload: str) -> tuple[int, int]:
    """Split a 24-bit interleaved payload back into (A, B) values."""
    if len(payload) != 2 * CHANNEL_BITS:
        raise ValueError(f"Dual payload must be {2 * CHANNEL_BITS} bits.")
    a = 0
    b = 0
    for i in range(CHANNEL_BITS):
        a |= int(payload[2 * i]) << (CHANNEL_BITS - 1 - i)
        b |= int(payload[2 * i + 1]) << i
    return a, b


def build_archive_frame(mode: int, tag: int, a: int, b: int | None = None,
                        flag: int = 0) -> Tibs:
    """Build an archive frame bit string in the recorder's original order."""
    bits = f"{mode:01b}{tag:02b}"
    if mode == MODE_DUAL:
        if b is None:
            raise ValueError("Dual-channel frames need both A and B values.")
        bits += interleave_channels(a, b)
    else:
        bits += f"{a:0{CHANNEL_BITS}b}"
    bits += str(flag & 1)
    return Tibs.from_string("0b" + bits)


@dataclass
class FrameReading:
    """Measurement points read from one archive frame.

    ``b`` is None for single-channel frames - no fake empty B is invented.
    """

    mode: int
    tag: int
    a: int
    b: int | None
    flag: int


def _payload_format(frame: Format) -> Format:
    """Return the payload Format that was active when ``frame`` was parsed."""
    if_field = frame[2]
    if not isinstance(if_field, If):
        raise TypeError("Frame format is missing its mode If field.")
    return if_field.then_ if if_field.condition_value else if_field.else_


def read_frame(archive: TibsLike) -> FrameReading:
    """Parse an archive frame and return its measurement points."""
    frame = make_frame_format()
    frame.parse(archive)
    values = frame.vars
    if values["mode"] == MODE_DUAL:
        a = sum(values[f"a{k}"] << k for k in range(CHANNEL_BITS))
        b = sum(values[f"b{k}"] << k for k in range(CHANNEL_BITS))
    else:
        a = values["a"]
        b = None
    return FrameReading(mode=values["mode"], tag=values["tag"], a=a, b=b,
                        flag=values["flag"])


def correct_channel_a(archive: TibsLike, new_a: int) -> Tibs:
    """Return the archive frame with only channel A corrected to ``new_a``.

    Every other slot - B's interleaved bits, the header and the trailing
    flag - keeps its original bit, and the segment is never reversed.
    """
    if not 0 <= new_a < 1 << CHANNEL_BITS:
        raise ValueError("Channel A value must fit in 12 bits.")
    frame = make_frame_format()
    frame.parse(archive)
    payload = _payload_format(frame)
    if frame.vars["mode"] == MODE_DUAL:
        for k in range(CHANNEL_BITS):
            payload[f"a{k}"].value = (new_a >> k) & 1
    else:
        payload["a"].value = new_a
    return frame.to_tibs()


def channel_a_slots(mode: int) -> list[int]:
    """Archive bit positions holding channel A, in recorder order."""
    if mode == MODE_DUAL:
        return [_HEADER_BITS + 2 * i for i in range(CHANNEL_BITS)]
    return [_HEADER_BITS + i for i in range(CHANNEL_BITS)]


def channel_b_slots(mode: int) -> list[int]:
    """Archive bit positions holding channel B (empty for single mode)."""
    if mode == MODE_DUAL:
        return [_HEADER_BITS + 2 * i + 1 for i in range(CHANNEL_BITS)]
    return []


def flag_position(mode: int) -> int:
    """Archive bit position of the trailing flag; it follows the mode."""
    payload_bits = 2 * CHANNEL_BITS if mode == MODE_DUAL else CHANNEL_BITS
    return _HEADER_BITS + payload_bits

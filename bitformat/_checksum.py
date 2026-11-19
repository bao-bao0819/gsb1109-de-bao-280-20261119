"""A checksum trailer field for bitformat :class:`Format` objects.

The :class:`Checksum` is a custom :class:`~bitformat.FieldType` that appends a
fixed-width checksum trailer to a frame. Unlike a plain ``u8`` field it:

* calculates the checksum over an arbitrary, possibly non-byte-aligned span of
  the already encoded fields (set with ``start`` / ``end`` bit offsets),
* ignores any trailing alignment padding between the covered span and the
  checksum trailer, both when packing and parsing,
* regenerates the trailer on pack and verifies it on parse, raising
  ``ValueError`` if a covered value bit has been corrupted.

The checksum is an 8-bit XOR over the bytes of the covered region. When the
region does not end on a byte boundary its last partial byte is zero-padded on
the right before the XOR, which keeps the calculation independent of the
alignment padding that follows the covered region.
"""

from __future__ import annotations

from typing import Any, Callable, Self, Sequence

from bitformat._bits import Tibs
from bitformat._common import Indenter, Colour, override, validate_name
from bitformat._fieldtype import FieldType
from bitformat._format import Format

__all__ = ["Checksum", "ChecksumFormat", "xor8"]


def xor8(bits: Tibs) -> int:
    """XOR the bytes of ``bits``, zero-padding a partial final byte on the right."""
    checksum = 0
    chunks = list(bits.chunks(8))
    for index, chunk in enumerate(chunks):
        value = chunk.to_u()
        if index == len(chunks) - 1 and len(chunk) < 8:
            value <<= 8 - len(chunk)
        checksum ^= value
    return checksum


class Checksum(FieldType):
    """A checksum trailer covering a bit range of the preceding frame data.

    :param bit_length: Width of the checksum trailer in bits.
    :param start: Bit offset where the covered region begins (may be non-byte-aligned).
    :param end: Bit offset where the covered region ends. Alignment padding, if any,
        starts here. Must not extend past the trailer position.
    :param algorithm: ``f(Tibs) -> int`` checksum over the covered bits.
    :param name: Optional field name, exposed like any other named field.

    A ``Checksum`` must be a direct field of a :class:`ChecksumFormat`.
    """

    _checksum_length: int
    _start: int
    _end: int | None
    _algorithm: Callable[[Tibs], int]
    _bits: Tibs | None
    _name: str
    # Transient pack-time context, set by the owning ChecksumFormat just before packing.
    _frame: "ChecksumFormat | None"
    _index: int

    def __new__(cls, *args, **kwargs) -> Self:
        return cls.from_params(*args, **kwargs)

    @classmethod
    @override
    def from_params(cls, bit_length: int = 8, start: int = 0, end: int | None = None,
                    algorithm: Callable[[Tibs], int] = xor8, name: str = "") -> Self:
        x = super().__new__(cls)
        if not isinstance(bit_length, int) or bit_length <= 0:
            raise ValueError("The checksum bit_length must be a positive integer.")
        if not isinstance(start, int) or start < 0:
            raise ValueError("The covered start offset must be a non-negative integer.")
        if end is not None and (not isinstance(end, int) or end < start):
            raise ValueError("The covered end offset must be >= start, or None.")
        x._checksum_length = bit_length
        x._start = start
        x._end = end
        x._algorithm = algorithm
        x._bits = None
        x._frame = None
        x._index = -1
        x.name = name
        return x

    @property
    def checksum_bit_length(self) -> int:
        return self._checksum_length

    @override
    def _get_bit_length(self) -> int:
        return self._checksum_length

    @override
    def clear(self) -> None:
        self._bits = None

    @override
    def has_dynamic_size(self) -> bool:
        return False

    @override
    def is_const(self) -> bool:
        return False

    @override
    def _get_name(self) -> str:
        return self._name

    @override
    def _set_name(self, name: str) -> None:
        self._name = validate_name(name)

    def _covered(self, frame: Tibs) -> Tibs:
        end = self._end if self._end is not None else len(frame)
        return frame[self._start:end]

    def _prefix(self) -> Tibs:
        if self._frame is None:
            raise ValueError("A Checksum field can only be packed by a ChecksumFormat.")
        # Preceding siblings have already been packed by the Format pack loop.
        return Tibs.from_joined(field.to_tibs() for field in self._frame._fields[:self._index])

    @override
    def _pack(self, value: Any, kwargs: dict[str, Any]) -> bool:
        checksum = self._algorithm(self._covered(self._prefix()))
        self._bits = Tibs.from_u(checksum, length=self._checksum_length)
        if self._name:
            kwargs[self._name] = checksum
        # The Format allocates one value slot per non-const field; the supplied value (if any)
        # is intentionally ignored because the checksum is derived, but the slot is still consumed.
        return True

    @override
    def _parse(self, b: Tibs, startbit: int, vars_: dict[str, Any]) -> int:
        end = self._end if self._end is not None else startbit
        if end < self._start:
            raise ValueError("The covered end offset must not be before the start offset.")
        if startbit < end:
            raise ValueError(
                f"Checksum trailer starts at bit {startbit}, but the covered region ends at bit {end}. "
                "The covered region must not overlap the trailer."
            )
        trailer = b[startbit: startbit + self._checksum_length]
        if len(trailer) != self._checksum_length:
            raise ValueError(
                f"Needed {self._checksum_length} bits for the checksum trailer, but only {len(trailer)} were available."
            )
        expected = self._algorithm(self._covered(b))
        actual = trailer.to_u()
        if actual != expected:
            raise ValueError(
                f"Checksum mismatch: expected {expected:#04x} over bits [{self._start}:{end}], "
                f"but the trailer holds {actual:#04x}."
            )
        self._bits = trailer
        if self._name:
            vars_[self._name] = actual
        return self._checksum_length

    @override
    def to_tibs(self) -> Tibs:
        if self._bits is None:
            raise ValueError(f"Checksum field '{self}' has no value, so can't be converted to bits.")
        return self._bits

    @override
    def _get_value(self) -> int | None:
        if self._bits is None:
            return None
        return self._bits.to_u()

    @override
    def _set_value_with_kwargs(self, val: Any, kwargs: dict[str, Any]) -> None:
        if val is not None:
            raise ValueError(
                "The checksum value is calculated, not set. Pack the frame without supplying a value for it."
            )
        self._pack(None, kwargs)

    @override
    def _copy(self) -> "Checksum":
        return Checksum.from_params(self._checksum_length, self._start, self._end,
                                    self._algorithm, self._name)

    @override
    def _info(self, use_colour: bool) -> str:
        end = "end" if self._end is None else self._end
        return f"{self._checksum_length}-bit checksum over bits [{self._start}:{end}]"

    @override
    def _str(self, indent: Indenter, use_colour: bool) -> str:
        colour = Colour(use_colour)
        name = "" if self._name == "" else f"{colour.name}{self._name}{colour.off}: "
        end = "end" if self._end is None else self._end
        value = "" if self._bits is None else f" = {colour.value}{self._bits.to_u():#04x}{colour.off}"
        return indent(f"{name}checksum{self._checksum_length}[{self._start}:{end}]{value}")

    @override
    def _repr(self) -> str:
        return (f"Checksum.from_params(bit_length={self._checksum_length}, start={self._start}, "
                f"end={self._end!r}, name={self._name!r})")

    @override
    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Checksum):
            return False
        return (self._checksum_length == other._checksum_length
                and self._start == other._start
                and self._end == other._end
                and self._name == other._name)


class ChecksumFormat(Format):
    """A :class:`Format` whose direct :class:`Checksum` fields can see their preceding fields.

    Use it exactly like a normal ``Format``. Before packing, each direct
    checksum child is given a reference to the frame and its field index, so it
    can calculate over the bits produced by the fields encoded before it
    (including any alignment padding) and slice its covered region from them.
    """

    @override
    def _pack(self, values: Sequence[Any] | None, kwargs: dict[str, Any]) -> bool:
        for index, field in enumerate(self._fields):
            if isinstance(field, Checksum):
                field._frame = self
                field._index = index
        try:
            return super()._pack(values, kwargs)
        finally:
            for field in self._fields:
                if isinstance(field, Checksum):
                    field._frame = None
                    field._index = -1

    @override
    def _copy(self) -> "ChecksumFormat":
        x = ChecksumFormat()
        x.name = self.name
        x.vars = {}
        x._fields = [f._copy() for f in self._fields]
        x._field_names = {}
        for field in x._fields:
            if field.name != "":
                x._field_names[field.name] = field
        return x

from __future__ import annotations

from typing import Any, Callable

from ._bits import Tibs
from ._common import (
    ChecksumResult,
    Expression,
    ExpressionError,
    FrameError,
    Indenter,
    TruncationError,
    override,
    validate_name,
)
from ._fieldtype import FieldType

__all__ = ["Checksum", "ChecksumError"]


def _default_compute(bits: Tibs) -> int:
    """Default checksum: the sum of all whole bytes, zero padded at the end."""
    if len(bits) == 0:
        return 0
    padding = -len(bits) % 8
    data = bits + Tibs.from_zeros(padding) if padding else bits
    return sum(data.to_bytes())


def _parse_layer(value: Any) -> str:
    if value is None:
        return "checksum"
    if not isinstance(value, str):
        raise TypeError(f"A checksum layer must be a string, not a '{type(value).__name__}'.")
    layer = value.strip()
    if not layer:
        raise ValueError("A checksum layer cannot be empty.")
    return layer


def _parse_rule(rule: Any) -> Callable[[Tibs], int]:
    if rule is None:
        return _default_compute
    if not callable(rule):
        raise TypeError(f"A checksum rule must be callable, not a '{type(rule).__name__}'.")
    return rule


def _validate_position(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, Expression):
        return value
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"A checksum {name} cannot be negative.")
        return value
    if isinstance(value, str):
        if value.startswith("{"):
            try:
                return Expression.from_string(value)
            except ExpressionError as e:
                raise ValueError(f"Invalid checksum {name} expression '{value}': {e}") from None
        if not value:
            raise ValueError(f"A checksum {name} field name cannot be empty.")
        return value
    raise TypeError(f"A checksum {name} must be an int, a field name or an Expression, "
                    f"not a '{type(value).__name__}'.")


def coverage_segments(frame_length: int, start: int, end: int,
                      own_start: int, own_end: int) -> list[tuple[int, int]] | None:
    """Return the frame segments covered by a checksum.

    The half-open ``[start, end)`` range is clipped to the frame, and the
    checksum's own field is removed.  Removing the own field can split the
    range in two, which is why a list is returned.
    """
    if start < 0 or end < 0 or start > end or end > frame_length:
        return None
    segments: list[tuple[int, int]] = []
    if end <= own_start or start >= own_end:
        if start < end:
            segments.append((start, end))
        return segments
    if start < own_start:
        segments.append((start, own_start))
    if end > own_end:
        segments.append((own_end, end))
    return segments


def coverage_bits(frame: Tibs, segments: list[tuple[int, int]]) -> Tibs:
    """Join the selected segments of *frame* into a single :class:`Tibs`."""
    if not segments:
        return Tibs()
    if len(segments) == 1:
        start, end = segments[0]
        return frame[start:end]
    return Tibs.from_joined(frame[start:end] for start, end in segments)


class ChecksumError(FrameError):
    """Raised when a checksum declaration cannot be used."""


class Checksum(FieldType):
    """
    A checksum field covering a range of the frame that contains it.

    The field itself occupies ``width`` bits like any other field.  Its value is
    calculated from the frame being parsed or built, rather than supplied as a
    measurement.  Coverage is resolved from the actual field positions in the
    current frame, so a repetition whose length comes from a count is handled
    without hard-coding an offset.

    ``layer`` is an independent label, normally ``'inner'`` or ``'outer'``.
    Every checksum keeps its own result, so an outer failure does not hide an
    inner success (or the other way around).  ``rule`` is a callable accepting
    the protected :class:`Tibs` and returning an integer that fits in the
    field.  When no rule is supplied a byte-sum checksum is used.

    ``start`` and ``end`` select the protected half-open bit range.  Each may be
    an integer bit offset, an :class:`Expression` that evaluates to one, or the
    name of a field in the containing format.  For a field name, ``start`` uses
    the first bit of that field and ``end`` uses the last bit.  The defaults are
    the start of the containing format and the start of this checksum field,
    which makes an ordinary trailing checksum cover everything before it.

    A checksum's own bits are never part of its coverage, even when an explicit
    ``end`` extends past the field.  If the range covers the field and bits
    after it, the protected data is the two pieces joined without the checksum.

    Checksums are normally created programmatically because the rule is a
    callable::

        inner = Checksum.from_params(8, layer="inner", rule=my_rule,
                                     name="inner_crc", start="samples")
        outer = Checksum.from_params(8, layer="outer", rule=my_rule,
                                     name="outer_crc")
        fmt = Format.from_params([flag, count, samples, inner, pad, outer])

    :meth:`Format.parse` verifies every checksum after the fields have been
    parsed and raises :class:`ChecksumFailure` carrying all independent
    results.  The parsed values remain available on the format, so a caller can
    correct a measurement and call :meth:`Format.to_tibs` to rebuild the frame.
    That recalculation happens in declaration order, so an outer checksum sees
    the newly written inner checksum and any preserved padding.
    """

    _width: int
    _layer: str
    _rule: Callable[[Tibs], int]
    _name: str
    _value: int | None
    _write_back: bool
    _start_spec: Any
    _end_spec: Any
    _expected: int | None
    _ok: bool | None
    _covered: tuple[int, int] | None
    _segments: list[tuple[int, int]] | None
    _start: int | None
    _end: int | None

    def __new__(cls, s: str) -> "Checksum":
        return cls.from_string(s)

    @classmethod
    @override
    def from_params(cls, width: int, layer: str = "inner", rule: Callable[[Tibs], int] | None = None,
                    name: str = "", value: int | None = None, start: Any = None, end: Any = None,
                    write_back: bool = True) -> "Checksum":
        """
        Create a checksum field.

        :param width: The number of bits occupied by the checksum.
        :param layer: An independent layer label, for example ``'inner'`` or ``'outer'``.
        :param rule: A callable taking the protected :class:`Tibs` and returning an int.
        :param name: An optional field name, usable like any other field name.
        :param value: An initial stored value, optional.
        :param start: Coverage start: a bit offset, a field name, or an Expression.
        :param end: Coverage end (exclusive): a bit offset, a field name, or an Expression.
        :param write_back: Whether :meth:`Format.to_tibs` may replace the stored value.
        :return: The checksum field.
        """
        x = super().__new__(cls)
        if not isinstance(width, int):
            raise TypeError(f"A checksum width must be an int, not a '{type(width).__name__}'.")
        if width <= 0:
            raise ValueError(f"A checksum width must be positive, not {width}.")
        x._width = width
        x._layer = _parse_layer(layer)
        x._rule = _parse_rule(rule)
        x._name = ""
        x.name = name
        x._value = None
        x._write_back = bool(write_back)
        x._start_spec = _validate_position(start, "start")
        x._end_spec = _validate_position(end, "end")
        x._expected = None
        x._ok = None
        x._covered = None
        x._segments = None
        x._start = None
        x._end = None
        if value is not None:
            x._set_value_no_const_check(value)
        return x

    @classmethod
    @override
    def from_string(cls, s: str, /) -> "Checksum":
        """
        Create a checksum from ``'checksum<width>'`` or ``'name: checksum<width>'``.

        The string form is mainly useful for round-tripping.  As a callable rule
        cannot be represented as text, use :meth:`from_params` when a rule,
        layer, or bit range is required.  The string form can still be completed
        by setting :attr:`layer`, :attr:`rule`, :attr:`start` and :attr:`end`.
        """
        text = s.strip()
        name = ""
        if ":" in text:
            possible_name, rest = text.split(":", 1)
            name = possible_name.strip()
            text = rest.strip()
        if not text.startswith("checksum"):
            raise ValueError(f"Can't parse a Checksum from '{s}'.")
        rest = text[len("checksum"):].strip()
        value = None
        if "=" in rest:
            rest, value_str = rest.split("=", 1)
            rest = rest.strip()
            try:
                value = int(value_str.strip(), 0)
            except ValueError:
                raise ValueError(f"Invalid checksum value in '{s}'.") from None
        if not rest:
            raise ValueError(f"A checksum width is required in '{s}'.")
        try:
            width = int(rest, 0)
        except ValueError:
            raise ValueError(f"Invalid checksum width '{rest}' in '{s}'.") from None
        return cls.from_params(width, name=name, value=value)

    @override
    def _get_bit_length(self) -> int:
        return self._width

    @override
    def _pack(self, value: Any, kwargs: dict[str, Any]) -> bool:
        if value is not None:
            if not self._write_back:
                raise ValueError(f"Checksum field '{self}' is not writable.")
            self._set_value_no_const_check(value)
        return True

    def _set_value_no_const_check(self, value: Any) -> None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"A checksum value must be an integer, not '{value}'.") from None
        if value < 0 or value >= (1 << self._width):
            raise ValueError(f"Checksum value {value} does not fit in {self._width} bits.")
        self._value = value
        self._ok = None

    @override
    def _parse(self, b: Tibs, startbit: int, vars_: dict[str, Any]) -> int:
        if len(b) - startbit < self._width:
            raise TruncationError(
                f"Checksum field '{self}' needs {self._width} bits to parse, "
                f"but {len(b) - startbit} were available.",
                field=self.name, needed=self._width,
                available=len(b) - startbit, position=startbit,
            )
        bits = b[startbit:startbit + self._width]
        self._start = startbit
        self._end = startbit + self._width
        self._value = bits.unpack(f"u{self._width}")
        self._expected = None
        self._ok = None
        self._covered = None
        self._segments = None
        if self.name:
            vars_[self.name] = self._value
        return self._width

    def _reset_result(self) -> None:
        self._expected = None
        self._ok = None
        self._covered = None
        self._segments = None

    def _set_verified(self, expected: int, covered: tuple[int, int],
                      segments: list[tuple[int, int]], ok: bool) -> ChecksumResult:
        self._expected = expected
        self._covered = covered
        self._segments = segments
        self._segments = segments
        self._ok = ok
        return ChecksumResult(self._layer, self.name, ok, expected, self._value, covered,
                              segments=segments)

    def _set_failed(self, error: str, covered: tuple[int, int] | None = None) -> ChecksumResult:
        self._expected = None
        self._ok = False
        self._covered = covered
        self._segments = None
        return ChecksumResult(self._layer, self.name, False, None, self._value, covered, error)

    @override
    def to_tibs(self) -> Tibs:
        if self._value is None:
            raise ChecksumError(f"Checksum field '{self}' has no value.")
        return Tibs.from_dtype(f"u{self._width}", self._value)

    @override
    def clear(self) -> None:
        self._value = None
        self._reset_result()
        self._start = None
        self._end = None

    @override
    def has_dynamic_size(self) -> bool:
        return False

    @override
    def is_const(self) -> bool:
        # A checksum is calculated, not supplied as a measurement, so it never
        # consumes a value when a Format is packed.
        return True

    @override
    def _copy(self) -> "Checksum":
        x = self.__class__.from_params(self._width, self._layer, self._rule, self._name,
                                       self._value, self._start_spec, self._end_spec,
                                       self._write_back)
        return x

    @override
    def _get_value(self) -> int | None:
        return self._value

    @override
    def _set_value_with_kwargs(self, value: Any, kwargs: dict[str, Any]) -> None:
        if not self._write_back:
            raise ValueError(f"Checksum field '{self}' is not writable.")
        if value is not None:
            self._set_value_no_const_check(value)

    @override
    def _get_name(self) -> str:
        return self._name

    @override
    def _set_name(self, name: str) -> None:
        self._name = validate_name(name)

    @override
    def _info(self, use_colour: bool) -> str:
        return f"{self._layer} checksum of {self._width} bits."

    @override
    def _str(self, indent: Indenter, use_colour: bool) -> str:
        name = "" if not self._name else f"{self._name}: "
        value = "" if self._value is None else f" = {self._value}"
        return indent(f"{name}checksum{self._width}{value}")

    @override
    def _repr(self) -> str:
        return (f"Checksum.from_params({self._width}, layer={self._layer!r}, "
                f"name={self._name!r})")

    @override
    def __eq__(self, other: Any) -> bool:
        return (isinstance(other, Checksum) and self._width == other._width
                and self._layer == other._layer and self._name == other._name
                and self._start_spec == other._start_spec and self._end_spec == other._end_spec)

    @property
    def layer(self) -> str:
        """The independent layer name for this checksum."""
        return self._layer

    @layer.setter
    def layer(self, value: str) -> None:
        self._layer = _parse_layer(value)

    @property
    def width(self) -> int:
        """The checksum width in bits."""
        return self._width

    @property
    def rule(self) -> Callable[[Tibs], int]:
        """The callable used to compute the checksum."""
        return self._rule

    @rule.setter
    def rule(self, value: Callable[[Tibs], int] | None) -> None:
        self._rule = _parse_rule(value)

    @property
    def start(self) -> Any:
        """The coverage start specification."""
        return self._start_spec

    @start.setter
    def start(self, value: Any) -> None:
        self._start_spec = _validate_position(value, "start")

    @property
    def end(self) -> Any:
        """The coverage end specification."""
        return self._end_spec

    @end.setter
    def end(self, value: Any) -> None:
        self._end_spec = _validate_position(value, "end")

    @property
    def writable(self) -> bool:
        """Whether :meth:`Format.to_tibs` may replace the stored value."""
        return self._write_back

    @property
    def ok(self) -> bool | None:
        """The result of the last verification, or None if not yet verified."""
        return self._ok

    @property
    def valid(self) -> bool | None:
        """An alias for :attr:`ok`."""
        return self._ok

    @property
    def expected(self) -> int | None:
        """The last computed checksum value, or None if not yet computed."""
        return self._expected

    @property
    def covered(self) -> tuple[int, int] | None:
        """The checked half-open bit range, or None if not yet verified."""
        return self._covered

    @property
    def covered_segments(self) -> list[tuple[int, int]] | None:
        """The covered frame segments after removing this checksum's own bits."""
        return self._segments

    def result(self) -> ChecksumResult:
        """Return the last verification result as a :class:`ChecksumResult`."""
        if self._ok is None:
            raise ChecksumError(f"Checksum '{self}' has not been verified.")
        return ChecksumResult(self._layer, self._name, self._ok, self._expected,
                              self._value, self._covered, segments=self._segments)

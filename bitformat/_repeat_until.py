from __future__ import annotations

import ast
from typing import Any, Sequence

from ._bits import Tibs
from ._common import override, Indenter, Colour
from ._fieldtype import FieldType
from ._tibs import tibs_from_any

__all__ = ["RepeatUntil", "Until"]


class RepeatUntil(FieldType):
    """
    A :class:`FieldType` that repeats another :class:`FieldType` until a marker is found.

    The marker is only tested at the start of a repetition, so a matching bit pattern
    inside one of the repeated items won't end the loop. The marker is compared bit by
    bit, so it doesn't need to be byte aligned.

    By default the marker is consumed when it is found. Set ``consume`` to ``False`` to
    leave it in place for the next field in the enclosing :class:`Format` to parse.

    .. code-block:: python

        r = RepeatUntil.from_params('(sensor: u16, flags: u8)', '0xffff')
        r.parse('0x00010002ffff')

    """

    _field: FieldType
    _marker: Tibs
    _consume: bool
    _bits_list: list[Tibs]
    _marker_seen: bool

    def __new__(cls, s: str) -> RepeatUntil:
        return cls.from_string(s)

    @classmethod
    @override
    def from_params(cls, first: Any = None, second: Any = None, /, *,
                    field: FieldType | str | None = None,
                    fieldtype: FieldType | str | None = None,
                    marker: Any = None,
                    until: Any = None,
                    terminator: Any = None,
                    consume: bool = True,
                    discard: bool | None = None,
                    keep: bool | None = None,
                    value: Sequence[Any] | None = None,
                    **_kwargs) -> RepeatUntil:
        """
        Create a :class:`RepeatUntil` instance.

        The field and marker can be supplied in either order as the first two positional
        arguments. They can also be given using the keyword arguments ``field`` (or
        ``fieldtype``) and ``marker`` (or ``until`` / ``terminator``).

        :param first: The field to repeat, or the marker to stop at.
        :param second: The marker to stop at, or the field to repeat.
        :param field: The :class:`FieldType` to repeat.
        :param fieldtype: An alias for ``field``.
        :param marker: The bit pattern that terminates the loop.
        :param until: An alias for ``marker``.
        :param terminator: An alias for ``marker``.
        :param consume: Whether to consume the marker when it is found. Defaults to ``True``.
        :param discard: If given, the inverse of ``consume``.
        :param keep: If given, the inverse of ``consume``.
        :param value: An optional sequence of values for the repeated field.
        :return: The :class:`RepeatUntil` instance.

        """
        marker_spec = _first_not_none(marker, until, terminator)
        field_spec = _first_not_none(field, fieldtype)
        if discard is not None:
            consume = not discard
        if keep is not None:
            consume = not keep

        remaining = [x for x in (first, second) if x is not None]
        if field_spec is None and marker_spec is None:
            if len(remaining) < 2:
                raise TypeError("RepeatUntil.from_params() needs both a field and a marker.")
            field_spec, marker_spec = _split_field_and_marker(remaining[0], remaining[1])
        elif field_spec is None:
            if not remaining:
                raise TypeError("RepeatUntil.from_params() needs a field to repeat.")
            field_spec = remaining[0]
        elif marker_spec is None:
            if not remaining:
                raise TypeError("RepeatUntil.from_params() needs a marker.")
            marker_spec = remaining[0]

        x = super().__new__(cls)
        if isinstance(field_spec, str):
            field_spec = FieldType.from_string(field_spec)
        elif not isinstance(field_spec, FieldType):
            raise ValueError(f"Invalid field of type {type(field_spec)}.")
        x._field = field_spec
        x._marker = _to_marker_tibs(marker_spec)
        if len(x._marker) == 0:
            raise ValueError("The marker for a RepeatUntil field cannot be empty.")
        x._consume = bool(consume)
        x._bits_list = []
        x._marker_seen = False
        if value is not None:
            x.value = value
        return x

    @classmethod
    @override
    def from_string(cls, s: str, /) -> RepeatUntil:
        """
        Create a :class:`RepeatUntil` instance from a string.

        The string should be of the form ``'repeat until {marker}: fieldtype'``.
        The optional ``keep`` keyword leaves the marker for the next field to parse.

        :param s: The string to parse.
        :return: The RepeatUntil instance.

        .. code-block:: python

            r = RepeatUntil.from_string('repeat until 0xffff: u16')

        """
        x = super().from_string(s)
        if not isinstance(x, RepeatUntil):
            raise ValueError(f"Can't parse RepeatUntil field from '{s}'. Instead got '{x}'.")
        return x

    @override
    def _get_bit_length(self) -> int:
        if not self._marker_seen:
            raise ValueError("Bit length cannot be calculated for a RepeatUntil field before it is parsed or packed.")
        length = sum(len(bits) for bits in self._bits_list)
        if self._consume:
            length += len(self._marker)
        return length

    @override
    def _info(self, use_colour: bool) -> str:
        consume_str = "consumed" if self._consume else "left for the next field"
        return f"repeat-until field with marker {self._marker} ({consume_str})."

    @override
    def _str(self, indent: Indenter, use_colour: bool) -> str:
        colour = Colour(use_colour)
        keep_str = "" if self._consume else " keep"
        s = indent(f"repeat until {colour.value}{self._marker}{colour.off}{keep_str}:")
        with indent:
            s += "\n" + self._field._str(indent, use_colour)
        return s

    @override
    def _repr(self) -> str:
        return (f"{self.__class__.__name__}.from_params({self._field._repr()}, "
                f"{str(self._marker)!r}, consume={self._consume!r})")

    @override
    def _parse(self, b: Tibs, startbit: int, kwargs: dict[str, Any]) -> int:
        self._bits_list = []
        self._marker_seen = False
        pos = startbit
        marker_length = len(self._marker)
        end = len(b)
        while True:
            remaining = end - pos
            if remaining >= marker_length and b[pos:pos + marker_length] == self._marker:
                self._marker_seen = True
                if self._consume:
                    pos += marker_length
                break
            if 0 < remaining < marker_length and b[pos:end] == self._marker[:remaining]:
                raise ValueError(
                    f"Incomplete marker at the end of the data: only {remaining} of "
                    f"{marker_length} bits of the marker {self._marker} are present."
                )
            if remaining == 0:
                raise ValueError(
                    f"Data ended before the marker {self._marker} was found."
                )
            try:
                bits_used = self._field._parse(b, pos, kwargs)
            except ValueError:
                tail = b[pos:end]
                if 0 < len(tail) < marker_length and tail == self._marker[:len(tail)]:
                    raise ValueError(
                        f"Incomplete marker at the end of the data: only {len(tail)} of "
                        f"{marker_length} bits of the marker {self._marker} are present."
                    ) from None
                raise
            if bits_used <= 0:
                raise ValueError(
                    f"The field being repeated by {self} consumed no bits, so the loop "
                    f"can never reach the marker {self._marker}."
                )
            self._bits_list.append(self._field.to_tibs())
            pos += bits_used
        return pos - startbit

    @override
    def _pack(self, value: Sequence[Any], kwargs: dict[str, Any]) -> bool:
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
            raise TypeError(f"A RepeatUntil field needs a Sequence of values to pack, but received "
                            f"'{value}' of type {type(value)}.")
        self._bits_list = []
        for item in value:
            self._field._pack(item, kwargs)
            self._bits_list.append(self._field.to_tibs())
            self._field.clear()
        self._marker_seen = True
        return True

    @override
    def _copy(self) -> RepeatUntil:
        return self.__class__.from_params(self._field._copy(), self._marker, consume=self._consume)

    @override
    def to_tibs(self) -> Tibs:
        parts = list(self._bits_list)
        if self._consume and self._marker_seen:
            parts.append(self._marker)
        return Tibs.from_joined(parts)

    @override
    def clear(self) -> None:
        self._bits_list = []
        self._marker_seen = False

    @override
    def has_dynamic_size(self) -> bool:
        return False

    @override
    def is_const(self) -> bool:
        return False

    @override
    def _get_value(self) -> list[Any] | None:
        if not self._bits_list and not self._marker_seen:
            return None
        values = [self._field.unpack(bits) for bits in self._bits_list]
        self._field.clear()
        return values

    @override
    def _set_value_with_kwargs(self, value: list[Any], kwargs: dict[str, Any]) -> None:
        self._pack(value, kwargs)

    @override
    def __eq__(self, other) -> bool:
        if not isinstance(other, RepeatUntil):
            return False
        if self._marker != other._marker:
            return False
        if self._consume != other._consume:
            return False
        return self._field == other._field

    @override
    def _get_name(self) -> None:
        return None

    @override
    def _set_name(self, name: str) -> None:
        raise AttributeError("The RepeatUntil field has no 'name' property.")

    @property
    def field(self) -> FieldType:
        """The :class:`FieldType` being repeated."""
        return self._field

    @property
    def marker(self) -> Tibs:
        """The bit pattern that terminates the loop."""
        return self._marker

    @property
    def until(self) -> Tibs:
        """An alias for :attr:`marker`."""
        return self._marker

    @property
    def consume(self) -> bool:
        """Whether the marker is consumed when it is found."""
        return self._consume

    @consume.setter
    def consume(self, value: bool) -> None:
        self._consume = bool(value)
        self.clear()


# The short name used in format strings.
Until = RepeatUntil


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _to_marker_tibs(marker: Any) -> Tibs:
    if isinstance(marker, FieldType):
        if marker.value is None:
            raise ValueError(f"The marker field '{marker}' has no value to use as a marker.")
        return marker.to_tibs()
    if isinstance(marker, Tibs):
        return marker
    if isinstance(marker, (bytes, bytearray, memoryview)):
        return Tibs.from_bytes(marker)
    if isinstance(marker, tuple) and len(marker) == 2:
        dtype, value = marker
        from ._field import Field
        return Field.from_params(dtype, value=value).to_tibs()
    if isinstance(marker, str):
        marker_str = marker.strip()
        if len(marker_str) >= 2 and marker_str[0] in "'\"" and marker_str[-1] == marker_str[0]:
            marker_str = ast.literal_eval(marker_str)
        if "=" in marker_str:
            dtype_str, value_str = marker_str.split("=", 1)
            from ._field import Field
            return Field.from_params(dtype_str.strip(), value=value_str.strip()).to_tibs()
        return tibs_from_any(marker_str)
    raise TypeError(f"Can't use '{marker}' of type {type(marker)} as a RepeatUntil marker.")


def _looks_like_marker(value: Any) -> bool:
    if isinstance(value, (Tibs, bytes, bytearray, memoryview)):
        return True
    if isinstance(value, FieldType):
        return value.is_const() and value.value is not None
    if isinstance(value, str):
        s = value.strip()
        if s[:2].lower() in {"0x", "0b", "0o"}:
            return True
        try:
            tibs_from_any(s)
            return True
        except (ValueError, TypeError):
            return False
    if isinstance(value, tuple) and len(value) == 2:
        return True
    return False


def _looks_like_field(value: Any) -> bool:
    if isinstance(value, FieldType):
        return True
    if isinstance(value, str):
        try:
            FieldType.from_string(value)
            return True
        except (ValueError, TypeError):
            return False
    return False


def _split_field_and_marker(first: Any, second: Any) -> tuple[Any, Any]:
    first_is_marker = _looks_like_marker(first)
    second_is_marker = _looks_like_marker(second)
    first_is_field = _looks_like_field(first)
    second_is_field = _looks_like_field(second)
    if first_is_field and not second_is_field:
        return first, second
    if second_is_field and not first_is_field:
        return second, first
    if first_is_marker and not second_is_marker:
        return second, first
    if second_is_marker and not first_is_marker:
        return first, second
    # Both look like fields (for example a const Field used as the marker).
    if first_is_field and second_is_field:
        if first.is_const() and not second.is_const():
            return second, first
        return first, second
    raise ValueError(f"Can't tell which of '{first}' and '{second}' is the field and which is the marker.")

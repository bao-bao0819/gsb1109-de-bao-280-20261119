from __future__ import annotations

from typing import Any

from ._bits import Tibs, Mutibs
from ._fieldtype import FieldType
from ._dtypes import Dtype, DtypeSingle
from ._common import DtypeKind


class _FieldState:
    """A snapshot of a fieldtype's mutable parsing state."""

    def __init__(self, fieldtype: FieldType, memo: dict[int, tuple]) -> None:
        self._fieldtype = fieldtype
        memo[id(fieldtype)] = ("field", self, fieldtype)
        self._attributes = {
            name: _snapshot_value(value, memo) for name, value in fieldtype.__dict__.items()
        }

    def restore(self) -> None:
        attributes = self._fieldtype.__dict__
        for name in list(attributes):
            if name not in self._attributes:
                del attributes[name]
        for name, snapshot in self._attributes.items():
            attributes[name] = _restore_value(snapshot)


def _snapshot_value(value: Any, memo: dict[int, tuple]) -> tuple:
    value_id = id(value)
    if value_id in memo:
        return memo[value_id]
    if isinstance(value, FieldType):
        _FieldState(value, memo)
        return memo[value_id]
    if isinstance(value, dict):
        snapshot = ("dict", {}, value)
        memo[value_id] = snapshot
        snapshot[1].update({key: _snapshot_value(item, memo) for key, item in value.items()})
        return snapshot
    if isinstance(value, list):
        snapshot = ("list", [], value)
        memo[value_id] = snapshot
        snapshot[1].extend(_snapshot_value(item, memo) for item in value)
        return snapshot
    if isinstance(value, tuple):
        snapshot = ("tuple", tuple(_snapshot_value(item, memo) for item in value), value)
        memo[value_id] = snapshot
        return snapshot
    if isinstance(value, set):
        snapshot = ("set", set(), value)
        memo[value_id] = snapshot
        snapshot[1].update(_restore_value(_snapshot_value(item, memo)) for item in value)
        return snapshot
    return ("value", value, value)


def _restore_value(snapshot: tuple) -> Any:
    kind, value, original = snapshot
    if kind == "field":
        value.restore()
        return original
    if kind == "dict":
        restored = {key: _restore_value(item) for key, item in value.items()}
        original.clear()
        original.update(restored)
        return original
    if kind == "list":
        restored = [_restore_value(item) for item in value]
        original[:] = restored
        return original
    if kind == "tuple":
        return tuple(_restore_value(item) for item in value)
    if kind == "set":
        original.clear()
        original.update(value)
        return original
    return value


class Reader:
    """
    Wraps a :class:`Tibs` or :class:`Mutibs` object and a bit position to allow reading and parsing as a stream of bits.

    **Methods:**

    - ``read()``: Read from the current bit position, and interpret according to the given format.
    - ``peek()``: Peek from the current bit position, and interpret according to the given format without changing the pos.
    - ``parse()``: Parse a fieldtype from the current bit position, returning the number of bits parsed.
    - ``try_parse()``: Try to parse a fieldtype, rolling back if it doesn't match.

    **Properties:**

    - ``bits``: The :class:`Tibs` or :class:`Mutibs` object.
    - ``pos``: The current bit position to read and parse from.

    """

    def __init__(self, bits: Tibs | Mutibs, pos: int = 0) -> None:
        Reader._validate_bits(bits)
        self._bits = bits
        self._pos = pos

    @staticmethod
    def _validate_bits(value):
        if not isinstance(value, (Tibs, Mutibs)):
            help_ = ""
            if isinstance(value, str):
                help_ = " Perhaps use 'Tibs.from_string()'?"
            elif isinstance(value, (bytes, bytearray, memoryview)):
                help_ = " Perhaps use 'Tibs.from_bytes()'?"
            raise TypeError(f"A Reader should be initialised with a 'Tibs' or 'Mutibs' object, but received a {type(value)}.{help_}")

    @property
    def bits(self) -> Tibs | Mutibs:
        """
        Get or set the :class:`Tibs` or :class:`Mutibs` object associated with the :class:`Reader`.

        Changing this object may invalidate the bit position, but it's left
        to the user to manage this.

        :return: The current bits object.

        :raises ValueError: If the provided value is not a valid type.

        """
        return self._bits

    @bits.setter
    def bits(self, value: Tibs | Mutibs) -> None:
        Reader._validate_bits(value)
        self._bits = value

    @property
    def pos(self) -> int:
        """Get or set the current bit position.

        This should be a positive int, but no attempt is made to check if the position is valid before it is used.

        :return: The current bit position.

        :raises ValueError: If set to a value that is not an integer.

        """
        return self._pos

    @pos.setter
    def pos(self, value: int) -> None:
        self._pos = int(value)

    def read(self, dtype: Dtype | str | int, /) -> Any | tuple[Any] | list[Any | tuple[Any]]:
        """Read from the current bit position, and interpret according to the given :class:`Dtype`."""
        if isinstance(dtype, int):
            dtype = DtypeSingle.from_params(DtypeKind.BITS, dtype)
        elif isinstance(dtype, str):
            dtype = Dtype.from_string(dtype)
        bits_read, x = dtype._unpack(self.bits[self._pos :])
        self._pos += bits_read
        return x

    def peek(self, dtype: Dtype | str | int, /) -> Any | tuple[Any] | list[Any | tuple[Any]]:
        """Peek from the current bit position, and interpret according to the given :class:`Dtype`.

        :param: dtype: The :class:`Dtype` to interpret the bits as, or a string or int that can be converted to a :class:`Dtype`.

        :return: The value interpreted as the given dtype without changing the current position.

        """
        current_pos = self._pos
        value = self.read(dtype)
        self._pos = current_pos
        return value

    def parse(self, f: FieldType, /) -> int:
        """Parse a fieldtype from the current bit position, returning the number of bits parsed.

        If the parse doesn't match the data then the bit position and the fieldtype's parsed values
        are restored before the :class:`ValueError` is raised, so another fieldtype can be tried
        from the same position.

        :param f: The fieldtype to parse.
        :return: The number of bits parsed.

        :raises TypeError: If `f` is not a subclass of :class:`FieldType`.
        """
        return self._parse(f, "parse", suppress=False)

    def try_parse(self, f: FieldType, /) -> int | None:
        """Try to parse a fieldtype from the current bit position, rolling back if it doesn't match.

        If the parse raises a :class:`ValueError` then the bit position and the fieldtype's parsed
        values are restored, and ``None`` is returned so that another fieldtype can be tried from
        the same position. On success this behaves exactly like :meth:`parse`.

        :param f: The fieldtype to parse.
        :return: The number of bits parsed, or ``None`` if the parse didn't match.
        """
        return self._parse(f, "try_parse", suppress=True)

    def _parse(self, f: FieldType, method_name: str, *, suppress: bool) -> int | None:
        if not isinstance(f, FieldType):
            raise TypeError(f"{method_name}() requires a FieldType. Got {f!r} of type {type(f)}.")
        pos = self._pos
        state = _FieldState(f, {})
        try:
            bits_parsed = f.parse(self._bits[pos:])
        except ValueError:
            state.restore()
            self._pos = pos
            if suppress:
                return None
            raise
        self._pos += bits_parsed
        return bits_parsed

    def __str__(self) -> str:
        return f"Reader(<{self._bits.__class__.__name__} of length {len(self._bits)} bits>, pos={self._pos})"

    def __repr__(self) -> str:
        return str(self)

    def __len__(self) -> int:
        return len(self.bits)

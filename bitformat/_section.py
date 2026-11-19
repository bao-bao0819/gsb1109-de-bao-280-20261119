from __future__ import annotations

from ._fieldtype import FieldType
from ._common import override, Indenter, Colour, Expression, ExpressionError, validate_name
from typing import Any
from ._bits import Tibs

__all__ = ["Section"]


class Section(FieldType):
    """
    A FieldType that confines another FieldType to a known bit range.

    The length of the section is known (or can be evaluated from previously parsed fields),
    so when parsing, the contents can only be read from within the section's bit range -
    data belonging to later sections will never be consumed to satisfy this one.
    The section always occupies its full declared bit range, so any sections that follow
    it start at a known bit position and can be parsed individually.

    """

    field: FieldType
    _length: Expression
    _concrete_length: int | None
    _bits: Tibs | None
    _name: str

    def __new__(cls, s: str) -> Section:
        return cls.from_string(s)

    @classmethod
    @override
    def from_params(cls, length: int | str | Expression, fieldtype: FieldType | str, name: str = "") -> Section:
        """
        Create a Section instance.

        :param length: An Expression or int giving the length of the section in bits.
        :param fieldtype: The FieldType contained in the section.
        :param name: An optional name for the section, used in error messages and field lookups.
        :return: The Section instance.
        """
        x = super().__new__(cls)
        if isinstance(length, str):
            length = Expression.from_string(length)
        if isinstance(length, int):
            length = Expression.from_int(length)
        if not isinstance(length, Expression):
            raise ValueError(f"Invalid length of type {type(length)} for Section field.")
        x._length = length
        x._concrete_length = None
        if x._length.has_const_value:
            x._concrete_length = x._validate_length(x._length.const_value)
        x._bits = None
        if isinstance(fieldtype, str):
            fieldtype = FieldType.from_string(fieldtype)
        elif not isinstance(fieldtype, FieldType):
            raise ValueError(f"Invalid field of type {type(fieldtype)}.")
        x.field = fieldtype
        x.name = name
        return x

    @staticmethod
    def _validate_length(length: Any) -> int:
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise ValueError(f"Section length must be a non-negative integer, not {length!r}.")
        return length

    def _evaluate_length(self, vars_: dict[str, Any]) -> int:
        try:
            length = self._length.evaluate(**vars_)
        except ExpressionError as e:
            raise ValueError(f"Cannot evaluate length for Section field: {e}")
        return self._validate_length(length)

    def _describe(self, startbit: int) -> str:
        name = self._name or getattr(self.field, "name", "") or ""
        name_str = f" '{name}'" if name else ""
        return f"section{name_str} starting at bit {startbit}"

    @override
    def _get_bit_length(self) -> int:
        if self._concrete_length is None:
            raise ValueError("Section length is not concrete, cannot calculate bit length.")
        return self._concrete_length

    @classmethod
    @override
    def from_string(cls, s: str) -> Section:
        """
        Create a :class:`Section` instance from a string.

        The string should be of the form ``'section {expression}: fieldtype'``,
        optionally preceded by a name, e.g. ``'name: section {expression}: fieldtype'``.
        The fieldtype can be a :class:`Format` to group multiple fields together.

        :param s: The string to parse.
        :return: The Section instance.

        .. code-block:: python

            s1 = Section.from_string('section {24}: (u8, u16)')
            s2 = Section.from_string('header: section {x * 8}: (u8, bool)')

        """
        x = super().from_string(s)
        if not isinstance(x, Section):
            raise ValueError(f"Can't parse Section field from '{s}'. Instead got '{x}'.")
        return x

    @override
    def _info(self, use_colour: bool) -> str:
        s = f"section field with bit length of {self._length}."
        return s

    @override
    def _str(self, indent: Indenter, use_colour: bool) -> str:
        colour = Colour(use_colour)
        name_str = "" if self._name == "" else f"{colour.name}{self._name}{colour.off}: "
        # We don't use str(self._length) here as that strips the {} from const expressions.
        length_str = "{" + self._length.code_str + "}"
        s = indent(f"{name_str}section {length_str}:")
        with indent:
            s += "\n" + self.field._str(indent, use_colour)
        return s

    @override
    def _repr(self) -> str:
        s = f"Section.from_params({self._length!r}, "
        s += self.field._repr()
        if self._name != "":
            s += f", name={self._name!r}"
        s += ")"
        return s

    @override
    def _parse(self, b: Tibs, startbit: int, vars_: dict[str, Any]) -> int:
        if self._concrete_length is None:
            self._concrete_length = self._evaluate_length(vars_)
        available = len(b) - startbit
        if available < self._concrete_length:
            raise ValueError(f"The {self._describe(startbit)} needs {self._concrete_length} bits, "
                             f"but only {available} bits are available.")
        # Only the bits inside the section's range are visible to its contents, so a short
        # read here can never consume bits that belong to the sections that follow.
        section_bits = b[startbit: startbit + self._concrete_length]
        try:
            self.field._parse(section_bits, 0, vars_)
        except ValueError as e:
            raise ValueError(f"Error parsing {self._describe(startbit)}: {e}") from None
        self._bits = section_bits
        return self._concrete_length

    @override
    def _pack(self, value: Any, kwargs: dict[str, Any]) -> bool:
        if self._concrete_length is None:
            self._concrete_length = self._evaluate_length(kwargs)
        self._set_value_with_kwargs(value, kwargs)
        return True

    @override
    def _copy(self) -> Section:
        x = self.__class__.from_params(self._length, self.field._copy(), self._name)
        return x

    @override
    def to_tibs(self) -> Tibs:
        if self._bits is None:
            raise ValueError(f"Section '{self}' has no value, so can't be converted to bits.")
        return self._bits

    @override
    def clear(self) -> None:
        self._concrete_length = self._length.const_value if self._length.has_const_value else None
        self._bits = None
        self.field.clear()

    @override
    def has_dynamic_size(self) -> bool:
        return False

    @override
    def is_const(self) -> bool:
        return self._length.has_const_value and self.field.is_const()

    @override
    def _get_value(self) -> Any:
        return self.field._get_value()

    @override
    def _set_value_with_kwargs(self, value: Any, kwargs: dict[str, Any]) -> None:
        if self._concrete_length is None:
            self._concrete_length = self._evaluate_length(kwargs)
        self.field._pack(value, kwargs)
        inner_bits = self.field.to_tibs()
        if len(inner_bits) > self._concrete_length:
            name_str = f" '{self._name}'" if self._name else ""
            raise ValueError(f"The packed value is {len(inner_bits)} bits long, which doesn't fit in the "
                             f"{self._concrete_length} bits of the section{name_str}.")
        if len(inner_bits) < self._concrete_length:
            inner_bits = inner_bits + Tibs.from_zeros(self._concrete_length - len(inner_bits))
        self._bits = inner_bits

    @override
    def __eq__(self, other) -> bool:
        if not isinstance(other, Section):
            return False
        if self._length != other._length:
            return False
        if self._name != other._name:
            return False
        if self.field != other.field:
            return False
        return True

    @override
    def _get_name(self) -> str:
        return self._name

    @override
    def _set_name(self, name: str) -> None:
        self._name = validate_name(name)

    @property
    def length(self) -> Expression:
        """
        The length of the Section field in bits.

        :return: The length of the Section field.
        """
        return self._length

    @length.setter
    def length(self, value: int | str | Expression) -> None:
        """
        Set the length of the Section field.

        :param value: The new length value.
        """
        if isinstance(value, str):
            value = Expression.from_string(value)
        elif isinstance(value, int):
            value = Expression.from_int(value)
        self._length = value
        self.clear()

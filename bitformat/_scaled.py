"""Fixed-scale integer data types for bitformat.

A scaled dtype stores a signed integer *raw code* and exposes a calibrated
value::

    value = raw * scale + offset

One raw code can be reserved as a "missing" / "not read" marker.  Reading that
code returns ``None`` and packing ``None`` writes the marker back.  All
boundaries are checked explicitly, so writing a value that doesn't fit the
underlying raw integer raises :class:`ValueError` instead of silently wrapping.

Scales and offsets are stored as exact :class:`fractions.Fraction` values, so
scales such as ``1/16`` round-trip without floating point drift.

The dtype can be created in several equivalent ways::

    Scaled("i12", scale=0.0625, missing=-2048)
    Scaled("i12", 0.0625, 0, -2048)
    Scaled(12, scale=1/16)
    Dtype("i12*0.0625,missing=-2048")
    Dtype("scaled(i12, 0.0625, 0, -2048)")

The aliases ``Scaled``, ``ScaledDtype``, ``ScaledInt`` and ``FixedPoint``
all refer to :class:`DtypeScaled`.
"""

from __future__ import annotations

import copy
import re
from decimal import Decimal
from fractions import Fraction
from typing import Any, Self

from ._common import DtypeKind, Endianness, Expression, override
from ._dtypes import Dtype, DtypeArray, DtypeSingle
from ._tibs import Tibs, TibsLike, tibs_from_any


__all__ = [
    "DtypeScaled",
    "Scaled",
    "ScaledDtype",
    "ScaledInt",
    "ScaledInteger",
    "FixedPoint",
    "Fixed",
    "FixedScale",
    "ScaledArray",
    "parse_scaled_dtype",
    "install_scaled_dtype",
]


# ---------------------------------------------------------------------------
# Exact number helpers


def _as_fraction(value: Any) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, (tuple, list)):
        if len(value) == 2:
            return _as_fraction(value[0]) / _as_fraction(value[1])
        if len(value) == 1:
            return _as_fraction(value[0])
        raise ValueError(f"Can't convert {value!r} to an exact number.")
    if isinstance(value, bool):
        return Fraction(int(value))
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"A finite scale is required, but received {value!r}.")
        # str() keeps the decimal the caller wrote for normal values rather
        # than exposing the binary float representation in the stored fraction.
        return Fraction(str(value))
    if isinstance(value, Decimal):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError):
            pass
    raise ValueError(f"Can't convert {value!r} to an exact number.")


def _fraction_or_int(value: Fraction) -> int | float:
    if value.denominator == 1:
        return value.numerator
    return float(value)


def _format_fraction(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    decimal = Decimal(value.numerator) / Decimal(value.denominator)
    text = format(decimal.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text and Fraction(text) == value:
        return text
    return f"{value.numerator}/{value.denominator}"


def _round_half_even(value: Fraction) -> int:
    """Round an exact fraction to the nearest integer, with ties to even."""
    numerator = value.numerator
    denominator = value.denominator
    sign = -1 if numerator < 0 else 1
    quotient, remainder = divmod(abs(numerator), denominator)
    twice = 2 * remainder
    if twice > denominator or (twice == denominator and quotient % 2):
        quotient += 1
    return sign * quotient


def _raw_limits(bit_length: int) -> tuple[int, int]:
    return -(1 << (bit_length - 1)), (1 << (bit_length - 1)) - 1


_MISSING_NAME_MIN = {"min", "minimum", "lowest", "negative", "neg", "raw_min", "rawmin"}
_MISSING_NAME_MAX = {"max", "maximum", "highest", "positive", "pos", "raw_max", "rawmax"}
_MISSING_NAME_NONE = {"none", "", "null", "nil"}

_SCALE_KEYS = ("scale", "factor", "resolution", "multiplier", "step", "lsb", "unit_scale", "units_per_count")
_OFFSET_KEYS = ("offset", "bias", "zero", "zero_point", "zeropoint")
_MISSING_KEYS = ("missing", "raw_missing", "missing_code", "sentinel", "sentinel_code", "no_value", "no_value_code", "na", "na_code", "not_read", "invalid")
_UNIT_KEYS = ("unit", "units", "symbol", "label")


def _first(mapping: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _has_any(mapping: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in mapping for key in keys)


def _normalise_missing(missing: Any, bit_length: int | None) -> Any:
    if missing is None:
        return None
    if isinstance(missing, str):
        text = missing.strip().lower()
        if text in _MISSING_NAME_NONE:
            return None
        if bit_length is None:
            if text in _MISSING_NAME_MIN:
                return "min"
            if text in _MISSING_NAME_MAX:
                return "max"
            try:
                return int(text, 0)
            except ValueError:
                raise ValueError(f"Can't interpret missing code {missing!r}.") from None
        low, high = _raw_limits(bit_length)
        if text in _MISSING_NAME_MIN:
            return low
        if text in _MISSING_NAME_MAX:
            return high
        try:
            missing = int(text, 0)
        except ValueError:
            raise ValueError(f"Can't interpret missing code {missing!r}.") from None
    missing = int(missing)
    if bit_length is None:
        return missing
    low, high = _raw_limits(bit_length)
    if not low <= missing <= high:
        raise ValueError(
            f"The missing code {missing} is outside the signed {bit_length}-bit range "
            f"[{low}, {high}].")
    return missing


# ---------------------------------------------------------------------------
# The Scaled dtype


class DtypeScaled(DtypeSingle):
    """A signed fixed-scale integer dtype.

    ``value = raw * scale + offset``.  If a missing code is configured, that
    raw code unpacks as ``None`` and ``None`` packs back to it.

    .. code-block:: pycon

        >>> d = DtypeScaled("i12", scale=0.0625, missing=-2048, unit="degC")
        >>> d.unpack(d.pack(21.5))
        21.5
        >>> d.unpack(d.pack(None)) is None
        True
        >>> d.pack(1000)
        Traceback (most recent call last):
        ...
        ValueError: ...

    """

    _raw_dtype: DtypeSingle
    _scale: Fraction
    _offset: Fraction
    _missing: int | None
    _unit: str | None
    _scaled_definition: Any

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        if not args and not kwargs:
            # Used by copy/pickle style construction and by Dtype.__new__.
            return super().__new__(cls)
        return cls.from_params(*args, **kwargs)

    # ------------------------------------------------------------------
    # Construction

    @classmethod
    def from_params(cls, dtype: Any = "i12", *args: Any, **kwargs: Any) -> Self:
        """Create a scaled dtype from an integer dtype and calibration data.

        Parameters may be positional or keyword.  Recognised keywords include
        ``scale`` / ``factor`` / ``resolution``, ``offset`` / ``bias``,
        ``missing`` / ``sentinel`` / ``no_value`` and ``unit``.
        """
        params = dict(kwargs)

        # Allow raw_dtype=..., base_dtype=..., base=..., dtype=..., kind=..., size=...
        if "raw_dtype" in params:
            dtype = params.pop("raw_dtype")
        elif "base_dtype" in params:
            dtype = params.pop("base_dtype")
        elif "base" in params:
            dtype = params.pop("base")
        elif "dtype" in params:
            dtype = params.pop("dtype")
        elif "kind" in params or "size" in params:
            kind = params.pop("kind", DtypeKind.INT)
            size = params.pop("size", None)
            dtype = (kind, size)

        scale_supplied = _has_any(params, _SCALE_KEYS)
        offset_supplied = _has_any(params, _OFFSET_KEYS)
        missing_supplied = _has_any(params, _MISSING_KEYS)
        scale = _first(params, _SCALE_KEYS, 1)
        offset = _first(params, _OFFSET_KEYS, 0)
        missing = _first(params, _MISSING_KEYS, None)
        unit = _first(params, _UNIT_KEYS, None)
        for key in (*_SCALE_KEYS, *_OFFSET_KEYS, *_MISSING_KEYS, *_UNIT_KEYS):
            params.pop(key, None)

        calibration = params.pop("calibration", params.pop("cal", None))
        if calibration is not None:
            if isinstance(calibration, dict):
                scale = _first(calibration, _SCALE_KEYS, scale)
                offset = _first(calibration, _OFFSET_KEYS, offset)
                missing = _first(calibration, _MISSING_KEYS, missing)
                unit = _first(calibration, _UNIT_KEYS, unit)
            elif isinstance(calibration, (tuple, list)):
                if len(calibration) >= 1:
                    scale = calibration[0]
                if len(calibration) >= 2:
                    offset = calibration[1]
                if len(calibration) >= 3:
                    missing = calibration[2]
                if len(calibration) >= 4:
                    unit = calibration[3]
            else:
                raise ValueError("calibration must be a mapping or a (scale, offset, missing) sequence.")

        raw_dtype: DtypeSingle

        def apply_positional(args: tuple[Any, ...], use_scale: bool = True) -> None:
            nonlocal scale, offset, missing, unit
            if len(args) > 4:
                raise TypeError("Too many positional arguments for Scaled.from_params().")
            if use_scale and len(args) >= 1:
                scale = args[0]
            if len(args) >= 2:
                offset = args[1]
            if len(args) >= 3:
                missing = args[2]
            if len(args) >= 4:
                unit = args[3]

        if isinstance(dtype, DtypeScaled):
            raw_dtype = dtype._raw_dtype
            if args:
                apply_positional(args)
            else:
                if not scale_supplied:
                    scale = dtype._scale
                if not offset_supplied:
                    offset = dtype._offset
                if not missing_supplied:
                    missing = dtype._missing
                if unit is None:
                    unit = dtype._unit
        elif isinstance(dtype, DtypeSingle):
            raw_dtype = dtype
            apply_positional(args)
        elif isinstance(dtype, tuple):
            kind, size = dtype
            if not isinstance(kind, DtypeKind):
                kind = DtypeKind(kind)
            if kind is DtypeKind.SCALED:
                kind = DtypeKind.INT
            raw_dtype = DtypeSingle.from_params(kind, size)
            apply_positional(args)
        elif isinstance(dtype, DtypeKind):
            kind = DtypeKind.INT if dtype is DtypeKind.SCALED else dtype
            endianness = params.pop("endianness", Endianness.UNSPECIFIED)
            if args and not isinstance(args[0], (int, str)):
                raise TypeError("A size is required immediately after the DtypeKind.")
            size = args[0] if args else params.pop("size", None)
            raw_dtype = DtypeSingle.from_params(kind, size, endianness)
            apply_positional(args[1:])
        elif isinstance(dtype, int) or (isinstance(dtype, str) and dtype.strip().isdigit()):
            raw_dtype = DtypeSingle.from_params(DtypeKind.INT, int(dtype))
            apply_positional(args)
        else:
            text = str(dtype).strip()
            parsed = parse_scaled_dtype(text)
            if parsed is not None:
                raw_dtype = parsed._raw_dtype
                if args:
                    apply_positional(args)
                else:
                    if not scale_supplied:
                        scale = parsed._scale
                    if not offset_supplied:
                        offset = parsed._offset
                    if not missing_supplied:
                        missing = parsed._missing
                    if unit is None:
                        unit = parsed._unit
            else:
                base = Dtype.from_string(text)
                if not isinstance(base, DtypeSingle):
                    raise ValueError(f"A scaled dtype must be based on a DtypeSingle, not '{text}'.")
                raw_dtype = base
                apply_positional(args)

        if params:
            unexpected = ", ".join(sorted(params))
            raise TypeError(f"Unexpected keyword argument(s) for Scaled: {unexpected}.")

        if not isinstance(raw_dtype, DtypeSingle) or isinstance(raw_dtype, DtypeScaled):
            raise ValueError("Scaled dtypes require a plain single integer dtype.")
        if raw_dtype.kind is not DtypeKind.INT:
            raise ValueError(f"Scaled dtypes must be based on a signed integer dtype, not '{raw_dtype}'.")

        scale_fraction = _as_fraction(scale)
        if scale_fraction == 0:
            raise ValueError("A non-zero scale is required.")
        offset_fraction = _as_fraction(offset)
        if unit is not None and not isinstance(unit, str):
            raise TypeError("The unit must be a string if supplied.")
        missing_code = _normalise_missing(missing, raw_dtype.bit_length)

        return cls._create_scaled(raw_dtype, scale_fraction, offset_fraction, missing_code, unit)

    @classmethod
    def from_string(cls, s: str, /) -> Self:
        parsed = parse_scaled_dtype(s)
        if parsed is None:
            raise ValueError(f"Can't parse a scaled dtype from '{s}'.")
        return parsed

    @classmethod
    def _create_scaled(cls, raw_dtype: DtypeSingle, scale: Fraction,
                       offset: Fraction, missing: int | None,
                       unit: str | None) -> Self:
        x = DtypeSingle.__new__(cls)
        x._raw_dtype = raw_dtype
        x._size = raw_dtype.size
        x._bit_length = raw_dtype.bit_length
        x._endianness = raw_dtype.endianness
        x._scale = scale
        x._offset = offset
        x._missing = missing
        x._unit = unit

        definition = copy.copy(raw_dtype._definition)
        definition.kind = DtypeKind.SCALED
        definition.return_type = float
        definition.is_signed = True
        definition.description = "a fixed-scale signed integer"
        definition.short_description = "scaled signed int"
        x._definition = definition
        x._scaled_definition = definition

        def get_fn(bits, start, length):
            raw = raw_dtype.unpack(tibs_from_any(bits)[start:start + length])
            return x._decode_raw(raw)

        def create_fn(value):
            return x.pack(value)

        x._get_fn = get_fn
        x._create_fn = create_fn
        return x

    @classmethod
    def from_parts(cls, raw_dtype: DtypeSingle, scale: Any = 1, offset: Any = 0,
                   missing: Any = None, unit: str | None = None) -> Self:
        """Create from an explicit raw dtype and calibration values."""
        return cls.from_params(raw_dtype, scale, offset, missing, unit)

    # ------------------------------------------------------------------
    # Properties

    @property
    def raw_dtype(self) -> DtypeSingle:
        """The underlying integer dtype that stores the raw codes."""
        return self._raw_dtype

    @property
    def base_dtype(self) -> DtypeSingle:
        return self._raw_dtype

    @property
    def dtype(self) -> DtypeSingle:
        return self._raw_dtype

    @property
    def raw_kind(self) -> DtypeKind:
        return self._raw_dtype.kind

    @property
    def scale(self) -> Fraction:
        """The exact multiplier that converts a raw code to a physical value."""
        return self._scale

    @property
    def factor(self) -> Fraction:
        return self._scale

    @property
    def resolution(self) -> Fraction:
        return self._scale

    @property
    def multiplier(self) -> Fraction:
        return self._scale

    @property
    def step(self) -> Fraction:
        return self._scale

    @property
    def offset(self) -> Fraction:
        """The additive offset applied after scaling."""
        return self._offset

    @property
    def bias(self) -> Fraction:
        return self._offset

    @property
    def zero(self) -> Fraction:
        return self._offset

    @property
    def unit(self) -> str | None:
        """An optional unit label such as ``'degC'``."""
        return self._unit

    @property
    def units(self) -> str | None:
        return self._unit

    @property
    def missing(self) -> int | None:
        """The raw integer code reserved for a missing/not-read value."""
        return self._missing

    @property
    def missing_code(self) -> int | None:
        return self._missing

    @property
    def missing_raw(self) -> int | None:
        return self._missing

    @property
    def raw_missing(self) -> int | None:
        return self._missing

    @property
    def sentinel(self) -> int | None:
        return self._missing

    @property
    def no_value(self) -> int | None:
        return self._missing

    @property
    def no_value_code(self) -> int | None:
        return self._missing

    @property
    def not_read(self) -> int | None:
        return self._missing

    @property
    def missing_value(self) -> None:
        """The calibrated representation of the missing code (always ``None``)."""
        return None

    @property
    def raw_min(self) -> int | None:
        """The most negative raw code that the underlying integer can store."""
        if self._bit_length is None:
            return None
        return _raw_limits(self._bit_length)[0]

    @property
    def raw_max(self) -> int | None:
        """The most positive raw code that the underlying integer can store."""
        if self._bit_length is None:
            return None
        return _raw_limits(self._bit_length)[1]

    @property
    def min_raw(self) -> int | None:
        return self.valid_raw_min

    @property
    def max_raw(self) -> int | None:
        return self.valid_raw_max

    @property
    def valid_raw_min(self) -> int | None:
        """The lowest raw code available for a real value (excluding the missing code)."""
        if self._bit_length is None:
            return None
        low, _ = _raw_limits(self._bit_length)
        return low + 1 if self._missing == low else low

    @property
    def valid_raw_max(self) -> int | None:
        """The highest raw code available for a real value (excluding the missing code)."""
        if self._bit_length is None:
            return None
        _, high = _raw_limits(self._bit_length)
        return high - 1 if self._missing == high else high

    @property
    def min_value(self) -> int | float | None:
        """The calibrated value at the lowest valid raw code, or ``None`` if the range is empty."""
        if self._bit_length is None or self.valid_raw_min > self.valid_raw_max:
            return None
        return _fraction_or_int(self._decode_fraction(self.valid_raw_min))

    @property
    def max_value(self) -> int | float | None:
        """The calibrated value at the highest valid raw code, or ``None`` if the range is empty."""
        if self._bit_length is None or self.valid_raw_min > self.valid_raw_max:
            return None
        return _fraction_or_int(self._decode_fraction(self.valid_raw_max))

    @property
    def minimum(self) -> int | float | None:
        return self.min_value

    @property
    def maximum(self) -> int | float | None:
        return self.max_value

    @property
    def lowest(self) -> int | float | None:
        return self.min_value

    @property
    def highest(self) -> int | float | None:
        return self.max_value

    @property
    def valid_min(self) -> int | float | None:
        return self.min_value

    @property
    def valid_max(self) -> int | float | None:
        return self.max_value

    @property
    def min_raw_code(self) -> int | None:
        return self.valid_raw_min

    @property
    def max_raw_code(self) -> int | None:
        return self.valid_raw_max

    @property
    def calibration(self) -> dict[str, Any]:
        """The calibration data as a dictionary."""
        return {
            "scale": self._scale,
            "offset": self._offset,
            "missing": self._missing,
            "unit": self._unit,
        }

    @classmethod
    def from_calibration(cls, dtype: Any, scale: Any = 1, offset: Any = 0,
                         missing: Any = None, unit: str | None = None) -> Self:
        """Create from a raw dtype and explicit calibration data."""
        return cls.from_params(dtype, scale, offset, missing, unit)

    def is_valid_raw(self, raw: int) -> bool:
        """Return whether ``raw`` is in range and is not the missing code."""
        if self.valid_raw_min is None:
            return False
        return self.valid_raw_min <= int(raw) <= self.valid_raw_max

    def check_raw(self, raw: int) -> int:
        """Return ``raw`` unchanged, raising :class:`ValueError` if it isn't a valid raw code."""
        raw = int(raw)
        if self._bit_length is not None:
            low, high = _raw_limits(self._bit_length)
            if not low <= raw <= high:
                raise ValueError(
                    f"Raw code {raw} is outside the {self._bit_length}-bit signed range "
                    f"[{low}, {high}].")
        if self._missing is not None and raw == self._missing:
            raise ValueError(f"Raw code {raw} is the missing code of dtype '{self}'.")
        return raw

    # ------------------------------------------------------------------
    # Conversion

    def _decode_fraction(self, raw: int) -> Fraction:
        return raw * self._scale + self._offset

    def _decode_raw(self, raw: int) -> Any:
        if self._missing is not None and raw == self._missing:
            return None
        return _fraction_or_int(self._decode_fraction(raw))

    def is_missing(self, value: Any) -> bool:
        """Return whether ``value`` is the missing marker (``None``)."""
        return value is None

    def encode(self, value: Any) -> int:
        """Convert a physical value to its raw integer code.

        Raises :class:`ValueError` if the value doesn't fit the raw integer
        range or would collide with the missing code.
        """
        if value is None:
            if self._missing is None:
                raise ValueError(f"Dtype '{self}' has no missing code, so None can't be packed.")
            return self._missing
        if isinstance(value, bool):
            value = int(value)
        try:
            physical = _as_fraction(value)
        except ValueError:
            raise ValueError(f"Can't pack {value!r} with scaled dtype '{self}'.") from None
        raw = _round_half_even((physical - self._offset) / self._scale)
        low, high = _raw_limits(self._bit_length)
        if not low <= raw <= high:
            raise ValueError(
                f"Value {value!r} is outside the range of dtype '{self}'. "
                f"Raw code {raw} is not in [{low}, {high}].")
        if self._missing is not None and raw == self._missing:
            raise ValueError(
                f"Value {value!r} maps to the missing code {self._missing} of dtype "
                f"'{self}' and can't be packed.")
        return raw

    def decode(self, raw: int) -> Any:
        """Convert a raw integer code to a physical value (or ``None`` for the missing code)."""
        raw = int(raw)
        low, high = _raw_limits(self._bit_length)
        if not low <= raw <= high:
            raise ValueError(
                f"Raw code {raw} is outside the {self._bit_length}-bit signed range "
                f"[{low}, {high}].")
        return self._decode_raw(raw)

    def value_to_raw(self, value: Any) -> int:
        """Alias for :meth:`encode`."""
        return self.encode(value)

    def raw_to_value(self, raw: int) -> Any:
        """Alias for :meth:`decode`."""
        return self.decode(raw)

    def to_raw(self, value: Any) -> int:
        return self.encode(value)

    def from_raw(self, raw: int) -> Any:
        return self.decode(raw)

    def value_to_bits(self, value: Any) -> Tibs:
        """Return the raw bit pattern for a calibrated value (``None`` gives the missing code)."""
        return self.pack(value)

    def bits_to_value(self, bits: TibsLike) -> Any:
        """Read a calibrated value from its raw bit pattern."""
        return self.unpack(bits)

    def to_raw_bits(self, value: Any) -> Tibs:
        """Return the raw bit pattern for a calibrated value."""
        return self.pack(value)

    def from_raw_bits(self, bits: TibsLike) -> Any:
        """Read a calibrated value from its raw bit pattern."""
        return self.unpack(bits)

    # ------------------------------------------------------------------
    # Packing/unpacking

    @override
    def pack(self, value: Any, /) -> Tibs:
        """Pack a physical value.  ``None`` packs to the configured missing code."""
        return self._raw_dtype.pack(self.encode(value))

    def pack_raw(self, raw: int, /) -> Tibs:
        """Pack a raw integer code directly, preserving its bit representation."""
        raw = int(raw)
        low, high = _raw_limits(self._bit_length)
        if not low <= raw <= high:
            raise ValueError(
                f"Raw code {raw} is outside the {self._bit_length}-bit signed range "
                f"[{low}, {high}].")
        return self._raw_dtype.pack(raw)

    def unpack_raw(self, b: TibsLike, /) -> int:
        """Unpack bits to the raw integer code, including the missing code."""
        b = tibs_from_any(b)
        if len(b) < self._bit_length:
            raise ValueError(
                f"{self!r} is {self._bit_length} bits long, but only got {len(b)} bits to unpack.")
        return int(self._raw_dtype.unpack(b[:self._bit_length]))

    def unpack_from(self, b: TibsLike, start: int = 0, /) -> Any:
        """Unpack a value starting at a bit offset."""
        return self.unpack(tibs_from_any(b)[start:start + self._bit_length])

    def raw_from(self, b: TibsLike, start: int = 0, /) -> int:
        """Unpack a raw code starting at a bit offset."""
        return self.unpack_raw(tibs_from_any(b)[start:start + self._bit_length])

    @override
    def _unpack(self, b: TibsLike, /) -> tuple[int, Any]:
        b = tibs_from_any(b)
        if len(b) < self._bit_length:
            raise ValueError(
                f"{self!r} is {self._bit_length} bits long, but only got {len(b)} bits to unpack.")
        raw = int(self._raw_dtype.unpack(b[:self._bit_length]))
        return self._bit_length, self._decode_raw(raw)

    @override
    def _unpack_no_checks(self, b: Tibs | Any) -> Any:
        raw = int(self._raw_dtype._unpack_no_checks(b))
        return self._decode_raw(raw)

    # ------------------------------------------------------------------
    # Dtype API

    @override
    def info(self) -> str:
        unit = f" in {self._unit}" if self._unit else ""
        missing = f", missing code {self._missing}" if self._missing is not None else ""
        return (
            f"{self._bit_length} bit scaled signed int ({self._raw_dtype}), "
            f"value = raw * {_format_fraction(self._scale)} + "
            f"{_format_fraction(self._offset)}{unit}{missing}")

    @override
    def __str__(self) -> str:
        base = str(self._raw_dtype)
        params = [f"scale={_format_fraction(self._scale)}"]
        if self._offset:
            params.append(f"offset={_format_fraction(self._offset)}")
        if self._missing is not None:
            params.append(f"missing={self._missing}")
        if self._unit is not None:
            unit = self._unit
            if unit.isidentifier():
                params.append(f"unit={unit}")
            else:
                escaped = unit.replace("\\", "\\\\").replace('"', '\\"')
                params.append(f'unit="{escaped}"')
        return f"{base}[{','.join(params)}]"

    @override
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}('{self}')"

    @override
    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            other = parse_scaled_dtype(other)
            if other is None:
                return False
        if isinstance(other, DtypeScaled):
            return (
                self._raw_dtype == other._raw_dtype
                and self._scale == other._scale
                and self._offset == other._offset
                and self._missing == other._missing
                and self._unit == other._unit)
        return False

    @override
    def __hash__(self) -> int:
        return hash((self._raw_dtype, self._scale, self._offset, self._missing, self._unit))

    @override
    def evaluate(self, **kwargs) -> Self:
        if self._raw_dtype.has_known_size():
            return self
        raw = self._raw_dtype.evaluate(**kwargs)
        return DtypeScaled.from_params(raw, self._scale, self._offset, self._missing, self._unit)

    @override
    def has_known_size(self) -> bool:
        return self._raw_dtype.has_known_size()

    @override
    def has_dynamic_size(self) -> bool:
        return self._raw_dtype.has_dynamic_size()

    @override
    def _get_bit_length(self) -> int | None:
        return self._bit_length

    def with_scale(self, scale: Any) -> Self:
        return DtypeScaled.from_params(self._raw_dtype, scale, self._offset, self._missing, self._unit)

    def with_offset(self, offset: Any) -> Self:
        return DtypeScaled.from_params(self._raw_dtype, self._scale, offset, self._missing, self._unit)

    def with_missing(self, missing: Any) -> Self:
        return DtypeScaled.from_params(self._raw_dtype, self._scale, self._offset, missing, self._unit)

    def with_unit(self, unit: str | None) -> Self:
        return DtypeScaled.from_params(self._raw_dtype, self._scale, self._offset, self._missing, unit)


class ScaledArray(DtypeArray):
    """A :class:`DtypeArray` whose items are :class:`Scaled` values."""

    @classmethod
    def _create_scaled(cls, scaled: DtypeScaled, items: Expression | int | None = None) -> Self:
        x = DtypeArray.__new__(cls)
        x._dtype_single = scaled
        if items is None:
            x._items = Expression.from_none()
        elif isinstance(items, int):
            x._items = Expression.from_int(items)
        else:
            x._items = items
        return x

    @override
    def evaluate(self, **kwargs) -> Self:
        if self._dtype_single._raw_dtype.has_known_size() and self._items.has_const_value:
            return self
        raw = self._dtype_single.evaluate(**kwargs)
        items = self._items.evaluate(**kwargs)
        return ScaledArray._create_scaled(raw, items)


# Friendly aliases.  ``DtypeScaled`` is the primary name, following the
# ``DtypeSingle`` / ``DtypeArray`` / ``DtypeTuple`` naming convention.
Scaled = DtypeScaled
ScaledDtype = DtypeScaled
ScaledInt = DtypeScaled
ScaledInteger = DtypeScaled
DtypeScaledInt = DtypeScaled
DtypeFixed = DtypeScaled
DtypeFixedPoint = DtypeScaled
DtypeUnit = DtypeScaled
DtypeQuantity = DtypeScaled
DtypeCalibrated = DtypeScaled
FixedPoint = DtypeScaled
Fixed = DtypeScaled
FixedScale = DtypeScaled
UnitScaled = DtypeScaled
ScaledUnit = DtypeScaled
Quantity = DtypeScaled
Calibrated = DtypeScaled


# ---------------------------------------------------------------------------
# String parsing


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:/\d+)?(?:[eE][+-]?\d+)?"
_NUMBER_FULL = rf"({_NUMBER})"


def _split_params(text: str) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    keyword: dict[str, str] = {}
    parts = [part.strip() for part in re.split(r"[,;]", text) if part.strip()]
    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip().lower().replace("-", "_")
            keyword[key] = value.strip()
        else:
            positional.append(part)
    return positional, keyword


def _apply_keywords(keyword: dict[str, str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in keyword.items():
        if key in _SCALE_KEYS:
            params["scale"] = value
        elif key in _OFFSET_KEYS:
            params["offset"] = value
        elif key in _MISSING_KEYS:
            params["missing"] = value
        elif key in _UNIT_KEYS:
            params["unit"] = value
        else:
            raise ValueError(f"Unknown scaled dtype parameter '{key}'.")
    return params


def _build(base: str, scale: Any, offset: Any, missing: Any, unit: Any) -> DtypeScaled | None:
    try:
        return Scaled.from_params(base, scale, offset, missing, unit)
    except (ValueError, TypeError):
        return None


def _parse_tail(base: str, scale: Any, offset: Any, tail: str) -> DtypeScaled | None:
    tail = tail.strip()
    if tail.startswith("(") and tail.endswith(")"):
        tail = tail[1:-1]
    if tail.startswith("[") and tail.endswith("]"):
        tail = tail[1:-1]
    tail = tail.strip()
    if tail.startswith(",") or tail.startswith(";"):
        tail = tail[1:].strip()

    # A bare leading +2.5 / -1 is an offset.
    if tail and tail[0] in "+-" and offset is None:
        match = re.fullmatch(rf"{_NUMBER_FULL}(.*)", tail)
        if match:
            offset = match.group(1)
            tail = match.group(2).strip()

    if tail.startswith("[") and tail.endswith("]"):
        tail = tail[1:-1]

    positional, keyword = _split_params(tail) if tail else ([], {})
    if positional:
        if offset is None:
            offset = positional.pop(0)
        if positional:
            keyword.setdefault("missing", positional.pop(0))
        if positional:
            keyword.setdefault("unit", positional.pop(0))
    if offset is None:
        offset = keyword.pop("offset", keyword.pop("bias", 0))
    missing = keyword.pop("missing", keyword.pop("sentinel", keyword.pop("no_value", None)))
    unit = keyword.pop("unit", keyword.pop("units", None))
    params = _apply_keywords(keyword) if keyword else {}
    if "scale" in params:
        scale = params["scale"]
    if "offset" in params:
        offset = params["offset"]
    if "missing" in params:
        missing = params["missing"]
    if "unit" in params:
        unit = params["unit"]
    return _build(base, scale, offset, missing, unit)


def parse_scaled_dtype(s: str) -> DtypeScaled | None:
    """Parse a scaled dtype string, returning ``None`` if it isn't one.

    Examples of accepted forms::

        i12*0.0625
        i12*1/16
        i12/16
        i12@0.0625
        i12*0.0625+2.5
        i12*0.0625[missing=-2048]
        i12*0.0625+2.5[missing=-2048,unit=degC]
        s12*0.0625
        scaled(i12, 0.0625, 0, -2048)
        fixedpoint(i12, 1/16, missing=min, unit=degC)
    """
    if not isinstance(s, str):
        return None
    text = s.strip()
    if not text:
        return None

    # scaled(...) / fixedpoint(...)
    match = re.fullmatch(
        r"(?:scaled|scaled_dtype|scaledint|fixedpoint|fixed_point)\s*\((.*)\)",
        text, re.IGNORECASE | re.DOTALL)
    if match:
        positional, keyword = _split_params(match.group(1))
        if not positional:
            return None
        base = positional.pop(0)
        scale = positional.pop(0) if positional else keyword.pop("scale", keyword.pop("factor", 1))
        offset = positional.pop(0) if positional else keyword.pop("offset", keyword.pop("bias", 0))
        missing = positional.pop(0) if positional else keyword.pop(
            "missing", keyword.pop("sentinel", keyword.pop("no_value", None)))
        unit = positional.pop(0) if positional else keyword.pop("unit", keyword.pop("units", None))
        params = _apply_keywords(keyword) if keyword else {}
        scale = params.get("scale", scale)
        offset = params.get("offset", offset)
        missing = params.get("missing", missing)
        unit = params.get("unit", unit)
        return _build(base, scale, offset, missing, unit)

    # 's12*0.0625' is shorthand for a signed base.
    match = re.fullmatch(rf"[sS](?P<size>\d+)\s*(?P<op>[*@/])\s*(?P<rest>.+)", text)
    if match:
        base = f"i{match.group('size')}"
        op = match.group("op")
        rest = match.group("rest").strip()
        number = re.fullmatch(rf"{_NUMBER_FULL}(?P<tail>.*)", rest, re.DOTALL)
        if number is None:
            return None
        scale: Any = number.group(1)
        if op == "/":
            try:
                scale = Fraction(1) / _as_fraction(scale)
            except (ValueError, ZeroDivisionError):
                return None
        return _parse_tail(base, scale, None, number.group("tail"))

    # ordinary spelling: i12*0.0625, i12/16, i12@0.0625
    match = re.fullmatch(
        rf"(?P<base>[iI]\d+(?:_(?:le|be))?)\s*(?P<op>[*@/])\s*(?P<rest>.+)",
        text, re.DOTALL)
    if match:
        base = match.group("base")
        op = match.group("op")
        rest = match.group("rest").strip()
        number = re.fullmatch(rf"{_NUMBER_FULL}(?P<tail>.*)", rest, re.DOTALL)
        if number is None:
            return None
        scale: Any = number.group(1)
        if op == "/":
            try:
                scale = Fraction(1) / _as_fraction(scale)
            except (ValueError, ZeroDivisionError):
                return None
        return _parse_tail(base, scale, None, number.group("tail"))

    return None


# ---------------------------------------------------------------------------
# Integration with the rest of bitformat


_ORIGINAL_DTYPE_FROM_STRING = Dtype.from_string.__func__
_ORIGINAL_DTYPE_SINGLE_FROM_PARAMS = DtypeSingle.from_params.__func__


def _dtype_from_string(cls, s: str, /) -> Any:
    scaled = parse_scaled_dtype(s)
    if scaled is not None:
        return scaled
    return _ORIGINAL_DTYPE_FROM_STRING(cls, s)


def _dtype_single_from_params(cls, kind: DtypeKind, size: Any = None,
                              endianness: Endianness = Endianness.UNSPECIFIED,
                              **kwargs: Any) -> Any:
    if kind is DtypeKind.SCALED:
        return DtypeScaled.from_params(DtypeKind.INT, size, endianness=endianness, **kwargs)
    return _ORIGINAL_DTYPE_SINGLE_FROM_PARAMS(cls, kind, size, endianness)


def install_scaled_dtype() -> None:
    """Patch the dtype constructors so scaled strings work everywhere."""
    Dtype.from_string = classmethod(_dtype_from_string)
    DtypeSingle.from_params = classmethod(_dtype_single_from_params)
    _patch_register()
    _register_scaled_definition()


def _patch_register() -> None:
    from ._dtypes import Register
    if getattr(Register, "_scaled_patched", False):
        return
    original_single = Register.get_single_dtype.__func__
    original_array = Register.get_array_dtype.__func__

    def get_single_dtype(cls, kind, size, endianness=Endianness.UNSPECIFIED):
        if kind is DtypeKind.SCALED:
            return DtypeScaled.from_params(DtypeKind.INT, size, endianness=endianness)
        return original_single(cls, kind, size, endianness)

    def get_array_dtype(cls, kind, size, items, endianness=Endianness.UNSPECIFIED):
        if kind is DtypeKind.SCALED:
            scaled = Scaled.from_params(DtypeKind.INT, size, endianness=endianness)
            return ScaledArray._create_scaled(scaled, items)
        return original_array(cls, kind, size, items, endianness)

    Register.get_single_dtype = classmethod(get_single_dtype)
    Register.get_array_dtype = classmethod(get_array_dtype)
    Register._scaled_patched = True


def _register_scaled_definition() -> None:
    """Make the scaled dtype discoverable through the ``Register``."""
    from ._dtypes import AllowedSizes, DtypeDefinition, Register
    if DtypeKind.SCALED in Register.kind_to_def:
        return
    definition = DtypeDefinition(
        DtypeKind.SCALED,
        "a fixed-scale signed integer",
        "scaled signed int",
        AllowedSizes(1, None),
        None,
        None,
        float,
        True,
        _scaled_bits2chars,
    )
    Register.kind_to_def[DtypeKind.SCALED] = definition


def _scaled_bits2chars(bit_length: int) -> int:
    # Enough room for a signed decimal value with a fractional part.
    return len(str(-(1 << (bit_length - 1)))) + 8

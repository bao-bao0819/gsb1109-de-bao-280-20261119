from __future__ import annotations

import itertools

from ._bits import Tibs
from typing import Sequence, Any, Iterable, Self
import copy
from ._common import (override, Indenter, Colour, validate_name, ChecksumResult,
                      ChecksumFailure, Expression, ExpressionError, TruncationError,
                      FrameError)
from ._fieldtype import FieldType
from ._checksum import Checksum, ChecksumError, coverage_bits, coverage_segments
from ._pass import Pass
from ._repeat import Repeat
from ._field import Field

__all__ = ["Format"]


class _ParseContext:
    """Spans and frame boundaries recorded while a Format tree is parsed."""

    __slots__ = ("spans", "frame_start", "frame_end")

    def __init__(self) -> None:
        self.spans: list[tuple[FieldType, int, int, int]] = []
        self.frame_start = 0
        self.frame_end = 0


_PARSE_CONTEXT_KEY = "__bitformat_parse_context__"


class Format(FieldType):
    """
    A sequence of :class:`FieldType` objects, used to group fields together.

    """
    _name: str
    _fields: list[FieldType]
    _field_names: dict[str, FieldType]
    vars: dict[str, Any]

    def __new__(cls, s: str | None = None) -> Self:
        if s is None:
            x = super().__new__(cls)
            x._fields = []
            x.name = ""
            x.vars = {}
            x._field_names = {}
            return x
        return cls.from_string(s)

    @classmethod
    def from_params(cls, fields: Sequence[FieldType | str] | None = None, name: str = "") -> Self:
        """
        Create a Format instance.

        :param fields: The field types to include in the format, optional.
        :param name: The name of the format, optional.
        :return: The Format instance.
        """
        x = super().__new__(cls)
        x._fields = []
        if fields is None:
            fields = []
        x.name = name
        x.vars = {}
        x._field_names = {}
        stretchy_field = ""
        for fieldtype in fields:
            if stretchy_field:
                raise ValueError(f"A Field with unknown length may only occur at the end of a Format. "
                                 f"Field '{stretchy_field}' is before the end.")
            if isinstance(fieldtype, FieldType):
                fieldtype = fieldtype._copy()
            elif isinstance(fieldtype, str):
                fieldtype = FieldType.from_string(fieldtype)
            else:
                raise ValueError(f"Invalid Field of type {type(fieldtype)}.")
            if fieldtype is Pass():
                # Don't bother appending if it's the Pass singleton.
                continue
            if fieldtype.has_dynamic_size():
                stretchy_field = fieldtype
            x._fields.append(fieldtype)
            if fieldtype.name:
                x._field_names[fieldtype.name] = fieldtype
        return x


    @classmethod
    @override
    def from_string(cls, s: str, /) -> Self:
        """
        Create a :class:`Format` instance from a string.

        The string should be of the form ``'(field1, field2, ...)'`` or ``'name: (field1, field2, ...)'``,
        with commas separating strings that will be used to create other :class:`FieldType` instances.

        :param s: The string to parse.
        :return: The Format instance.

        .. code-block:: python

            f1 = Format.from_string('(u8, bool=True, val: i7)')
            f2 = Format.from_string('my_format: (float16,)')
            f3 = Format.from_string('(u16, another_format: ([u8; 64], [bool; 8]))')

        """
        x = super().from_string(s)
        if not isinstance(x, Format):
            raise ValueError(f'Can\'t parse a Format field from "{s}". This looks like a {type(x)}.')
        return x

    @override
    def _get_bit_length(self) -> int:
        """Return the total length of the Format in bits."""
        return sum(f.bit_length for f in self._fields)

    def pack_measured(self, values: Sequence[Any] | None, /, **kwargs) -> Tibs:
        """
        Pack only the measured fields, leaving checksums to be recalculated.

        This is a convenience wrapper around :meth:`pack` for the common
        correction workflow: supply the non-checksum values and let
        :meth:`to_tibs` derive both checksum layers.  Padding bits that were
        received with the same length are kept; when the new measurement
        changes the padding length, the new padding is written as zero bits.
        """
        if values is None:
            values = []
        values = list(values)
        padding = [(field, len(field.to_tibs()), field.to_tibs())
                   for field in self._padding_fields()]
        for checksum in self._all_checksums():
            checksum.clear()
        self._pack(values, kwargs)
        for field, old_length, old_bits in padding:
            if field._get_value() is None and len(field.to_tibs()) == old_length:
                field._bits = old_bits
        return self.to_tibs()

    def _padding_fields(self) -> list[Field]:
        fields: list[Field] = []
        for fieldtype in self._fields:
            if isinstance(fieldtype, Field) and fieldtype.dtype._is_padding():
                fields.append(fieldtype)
            elif isinstance(fieldtype, Format):
                fields.extend(fieldtype._padding_fields())
        return fields

    @override
    def _pack(self, values: Sequence[Any] | None, kwargs: dict[str, Any]) -> bool:
        self.vars = kwargs
        self._checksum_results = []
        self._valid_bit_length = None
        if values is not None and not isinstance(values, Sequence):
            raise TypeError(f"Format.pack needs a sequence to pack, but received {type(values)}.")
        if values is None:
            value_iter = itertools.repeat(None)
        else:
            value_iter = iter(values)
        consumed_all_values = False
        need_next_value = True
        for fieldtype in self._fields:
            # For const fields (and Repeat with const fields), and padding we don't need to use up a value
            if fieldtype.is_const() or (isinstance(fieldtype, Field) and fieldtype.dtype._is_padding()):
                _ = fieldtype._pack(None, kwargs)
                continue
            fieldtype.clear()
            if isinstance(fieldtype, Repeat) and fieldtype.field.is_const():
                _ = fieldtype._pack([], kwargs)
                continue
            try:
                if need_next_value:
                    next_value = next(value_iter)
            except StopIteration:
                # No more values left to pack, but there may still be some constant fields
                consumed_all_values = True
                _ = fieldtype._pack(None, kwargs)
                continue
            else:
                need_next_value = fieldtype._pack(next_value, kwargs)
        if consumed_all_values:
            return True
        try:
            _ = next(value_iter)
        except StopIteration:
            return True
        return False  # Didn't use up all of values

    @override
    def _parse(self, b: Tibs, startbit: int, vars_: dict[str, Any]) -> int:
        self.vars = vars_
        root_context = vars_.get(_PARSE_CONTEXT_KEY)
        is_root = root_context is None
        declared_length = None
        verify = True
        if is_root:
            declared_length = vars_.pop("valid_bit_length", None)
            if declared_length is None:
                declared_length = vars_.pop("bit_length", None)
            verify = bool(vars_.pop("verify", True))
            if declared_length is not None:
                if not isinstance(declared_length, int):
                    raise TypeError("valid_bit_length must be an int.")
                if declared_length < 0:
                    raise ValueError("valid_bit_length cannot be negative.")
                if startbit + declared_length > len(b):
                    raise TruncationError(
                        f"The frame declares {declared_length} valid bits from position {startbit}, "
                        f"but only {len(b) - startbit} were supplied.",
                        needed=declared_length, available=len(b) - startbit,
                        position=startbit)
                b = b[:startbit + declared_length]
            root_context = _ParseContext()
            root_context.frame_start = startbit
            vars_[_PARSE_CONTEXT_KEY] = root_context
        pos = startbit
        for fieldtype in self._fields:
            field_start = pos
            try:
                pos += fieldtype._parse(b, pos, self.vars)
            except TruncationError as e:
                e.add_context(fieldtype.name or "")
                raise
            root_context.spans.append((fieldtype, field_start, pos, startbit))
        if is_root:
            root_context.frame_end = pos
            try:
                if declared_length is not None:
                    if pos - startbit > declared_length:
                        raise FrameError(
                            f"The format used {pos - startbit} bits but the frame declares only "
                            f"{declared_length} valid bits.")
                    if pos - startbit < declared_length:
                        raise FrameError(
                            f"The format only accounted for {pos - startbit} of the "
                            f"{declared_length} valid bits in this frame. The remaining "
                            f"{declared_length - (pos - startbit)} bits were not consumed.")
                    self._valid_bit_length = declared_length
                if verify:
                    self._verify_checksums(b, startbit, pos, root_context)
            finally:
                vars_.pop(_PARSE_CONTEXT_KEY, None)
        return pos - startbit

    def _verify_checksums(self, b: Tibs, startbit: int, endbit: int,
                          context: "_ParseContext") -> None:
        checksums = self._all_checksums()
        if not checksums:
            return
        frame = b[startbit:endbit]
        results: list[ChecksumResult] = []
        for checksum in checksums:
            if checksum._start is None or checksum._end is None:
                results.append(checksum._set_failed("the checksum field itself was not parsed"))
                continue
            resolved = self._checksum_range(checksum, len(frame), context, startbit)
            if resolved is None:
                results.append(checksum._set_failed(
                    "the checksum coverage could not be resolved",
                    (checksum._start - startbit, checksum._end - startbit)))
                continue
            (covered_start, covered_end), segments = resolved
            protected = coverage_bits(frame, segments)
            try:
                expected = int(checksum.rule(protected))
            except Exception as e:
                results.append(checksum._set_failed(f"{type(e).__name__}: {e}"))
                continue
            if expected < 0:
                expected &= (1 << checksum.width) - 1
            expected &= (1 << checksum.width) - 1
            ok = expected == checksum._value
            results.append(checksum._set_verified(
                expected, (checksum._start - startbit, checksum._end - startbit),
                segments, ok))
        self._checksum_results = results
        for result in results:
            self._record_checksum_observation(result)
        if not all(result.ok for result in results):
            raise ChecksumFailure(results)

    def _record_checksum_observation(self, result: ChecksumResult) -> None:
        """Keep a checksum's own value while exposing its coverage as a variable.

        A checksum keeps its stored bits as its ``value`` so that a rebuild
        preserves the position of the field.  When it is given a name, two
        variables are made available for later expressions: the bare name is
        the checksum coverage, and ``<name>_value`` is the stored checksum.
        """
        checksum = next((item for item in self._all_checksums()
                         if item.name == result.field), None)
        if checksum is None or not checksum.name:
            return
        if result.expected is not None:
            self.vars[checksum.name] = result.expected
            self.vars[f"{checksum.name}_value"] = checksum.value
        else:
            self.vars[checksum.name] = None

    def _checksum_positions(self, checksum: Checksum, frame_length: int,
                            context: "_ParseContext", frame_start: int) -> tuple[int, int] | None:
        """Resolve a checksum's coverage to frame-relative half-open bit positions."""
        own_scope = next((span[3] for span in context.spans if span[0] is checksum), frame_start)
        start = self._resolve_position(checksum.start, context, is_start=True,
                                       default=own_scope)
        end = self._resolve_position(checksum.end, context, is_start=False,
                                     default=checksum._start)
        if start is None or end is None:
            return None
        start -= frame_start
        end -= frame_start
        if start < 0 or end < 0 or start > end or end > frame_length:
            return None
        return start, end

    def _checksum_range(self, checksum: Checksum, frame_length: int,
                        context: "_ParseContext", frame_start: int
                        ) -> tuple[tuple[int, int], list[tuple[int, int]]] | None:
        positions = self._checksum_positions(checksum, frame_length, context, frame_start)
        if positions is None:
            return None
        start, end = positions
        own_start = checksum._start - frame_start
        own_end = checksum._end - frame_start
        segments = coverage_segments(frame_length, start, end, own_start, own_end)
        if segments is None:
            return None
        return (start, end), segments

    def _resolve_position(self, spec: Any, context: "_ParseContext",
                          is_start: bool, default: int | None) -> int | None:
        if spec is None:
            return default
        if isinstance(spec, Expression):
            try:
                value = spec.evaluate(**self.vars)
            except ExpressionError:
                return None
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        if isinstance(spec, int):
            return spec
        # A field name: start means its first bit, end means its last bit.
        first = None
        last = None
        for field, field_start, field_end, _ in context.spans:
            if field.name == spec:
                if first is None:
                    first = field_start
                last = field_end
        if is_start:
            return first
        return last

    def parse_frame(self, b: Any, bit_length: int | None = None, **kwargs) -> int:
        """
        Parse one archived frame with a known valid bit length.

        ``bit_length`` is the number of bits actually received for this frame,
        as recorded by the archiver.  Only those bits are offered to the
        parser; any later frame that happens to follow in ``b`` is never used
        to fill a short field or checksum.  If the frame needs more bits than
        were received a :class:`TruncationError` is raised with ``field``,
        ``needed``, ``available`` and ``missing`` describing the first gap.

        When the frame is longer than its declared valid length, no attempt is
        made to silently discard the excess: :class:`FrameError` is raised.
        This keeps a corrupt length from being hidden.

        :param b: The received bits, possibly containing further frames.
        :param bit_length: The valid bit length of this frame. Defaults to all of ``b``.
        :return: The number of bits used by the format.
        """
        from ._tibs import tibs_from_any
        bits = tibs_from_any(b)
        if bit_length is None:
            bit_length = len(bits)
        if not isinstance(bit_length, int):
            raise TypeError(f"bit_length must be an int, not a '{type(bit_length).__name__}'.")
        if bit_length < 0:
            raise ValueError(f"bit_length cannot be negative, not {bit_length}.")
        available = min(bit_length, len(bits))
        if bit_length > len(bits):
            raise TruncationError(
                f"The frame declares {bit_length} valid bits, but only {len(bits)} were supplied.",
                needed=bit_length, available=len(bits), position=len(bits))
        frame = bits[:bit_length]
        used = self.parse(frame, **kwargs)
        if used < bit_length:
            raise FrameError(
                f"The format only accounted for {used} of the {bit_length} valid bits in this frame. "
                f"The remaining {bit_length - used} bits were not consumed.")
        self.valid_bit_length = bit_length
        return used

    @property
    def checksums(self) -> list[Checksum]:
        """The checksum fields in this format, in declaration order."""
        return self._all_checksums()

    @property
    def checksum_results(self) -> list[ChecksumResult]:
        """The independent results from the last verification, in declaration order."""
        return list(getattr(self, "_checksum_results", []))

    @property
    def valid_bit_length(self) -> int | None:
        """The valid bit length of the last frame parsed with :meth:`parse_frame`."""
        return getattr(self, "_valid_bit_length", None)

    @valid_bit_length.setter
    def valid_bit_length(self, value: int | None) -> None:
        self._valid_bit_length = value

    def _refresh_vars(self) -> None:
        """Refresh expression variables from the current field values."""
        for fieldtype in self._fields:
            if isinstance(fieldtype, Field) and fieldtype.name:
                value = fieldtype.value
                if value is not None:
                    self.vars[fieldtype.name] = value
            elif isinstance(fieldtype, Repeat):
                value = fieldtype._get_value()
                if value is not None and fieldtype._count.code_str.isidentifier():
                    self.vars[fieldtype._count.code_str] = len(value)
            elif isinstance(fieldtype, Format):
                fieldtype._refresh_vars()
                self.vars.update(fieldtype.vars)

    def verify_checksums(self) -> list[ChecksumResult]:
        """
        Recalculate every checksum from the currently held field values.

        This is useful after correcting a measurement: the checksums are
        recalculated without requiring a new parse.  Writable checksum fields
        are updated to the calculated value, so their results pass by
        construction; a fixed checksum (``write_back=False``) is compared with
        the newly calculated value and can fail.  The results are returned and
        stored.
        """
        self._refresh_vars()
        frame = self.to_tibs()
        startbit = 0
        endbit = len(frame)
        context = _ParseContext()
        context.frame_start = startbit
        context.frame_end = endbit
        pos = startbit
        for fieldtype in self._fields:
            field_start = pos
            try:
                length = fieldtype.bit_length
            except ValueError:
                length = len(fieldtype.to_tibs())
            pos += length
            context.spans.append((fieldtype, field_start, pos, startbit))
        self._verify_checksums(frame, startbit, endbit, context)
        return self.checksum_results

    def checksum_failure(self) -> ChecksumFailure | None:
        """Return a :class:`ChecksumFailure` for the last results, or None if all passed."""
        results = self.checksum_results
        if results and not all(result.ok for result in results):
            return ChecksumFailure(results)
        return None

    def _all_checksums(self) -> list[Checksum]:
        checksums: list[Checksum] = []
        for fieldtype in self._fields:
            if isinstance(fieldtype, Checksum):
                checksums.append(fieldtype)
            elif isinstance(fieldtype, Format):
                checksums.extend(fieldtype._all_checksums())
            elif isinstance(fieldtype, Repeat):
                if isinstance(fieldtype.field, Checksum):
                    checksums.append(fieldtype.field)
                elif isinstance(fieldtype.field, Format):
                    checksums.extend(fieldtype.field._all_checksums())
        return checksums

    @override
    def _copy(self) -> Format:
        x = Format()
        x.name = self.name
        x._fields = [f._copy() for f in self._fields]
        x._field_names = {}
        for field in x._fields:
            if field.name != "":
                x._field_names[field.name] = field
        return x

    @override
    def clear(self) -> None:
        self.vars = {}
        self._checksum_results = []
        self._valid_bit_length = None
        for fieldtype in self._fields:
            fieldtype.clear()

    @override
    def has_dynamic_size(self) -> bool:
        return False

    @override
    def is_const(self) -> bool:
        return all(fieldtype.is_const() for fieldtype in self._fields)

    @override
    def _get_value(self) -> list[Any]:
        values = []
        for fieldtype in self._fields:
            try:
                value = fieldtype._get_value()
                if isinstance(fieldtype, Field) and fieldtype._dtype._is_padding():
                    pass
                else:
                    values.append(value)
            except AttributeError:
                pass  # This field type doesn't have values, but that's fine in this context.
        return values

    def measured_value(self) -> list[Any]:
        """
        Return the values without the checksum field entries.

        This is the same shape as :meth:`pack` expects for the measurements,
        which makes it convenient to correct a reading and rebuild::

            values = fmt.measured_value()
            values[2] = corrected_readings
            fmt.pack([*values, *extra_values])
        """
        checksum_names = {checksum.name for checksum in self._all_checksums()}
        values = []
        for fieldtype in self._fields:
            if isinstance(fieldtype, Checksum) and fieldtype.name in checksum_names:
                continue
            try:
                value = fieldtype._get_value()
                if isinstance(fieldtype, Field) and fieldtype._dtype._is_padding():
                    pass
                else:
                    values.append(value)
            except AttributeError:
                pass
        return values

    @override
    def _set_value_with_kwargs(self, val: Sequence[Any], kwargs: dict[str, Any]) -> None:
        if len(val) != len(self._fields):
            raise ValueError(f"Can't set {len(self._fields)} fields from {len(val)} values.")
        for fieldtype, v in zip(self._fields, val):
            fieldtype._set_value_with_kwargs(v, kwargs)

    @override
    def to_tibs(self) -> Tibs:
        # A nested format may contain its own checksums. Build those first so
        # that the enclosing frame, if any, protects the final nested values.
        direct: list[tuple[int, FieldType]] = []
        parts: list[Tibs] = []
        for index, fieldtype in enumerate(self._fields):
            if isinstance(fieldtype, Checksum):
                direct.append((index, fieldtype))
                if fieldtype._value is None:
                    parts.append(Tibs.from_zeros(fieldtype.width))
                else:
                    parts.append(fieldtype.to_tibs())
            elif isinstance(fieldtype, Format) and fieldtype._all_checksums():
                parts.append(fieldtype.to_tibs())
            else:
                parts.append(fieldtype.to_tibs())
        if not direct:
            return Tibs.from_joined(parts)
        frame = Tibs.from_joined(parts)
        # Inner checksums are written first so that an outer checksum sees the
        # final inner value rather than the one that happened to be present.
        ordered = sorted(direct, key=lambda item: (0 if item[1].layer == "inner" else 1, item[0]))
        for index, checksum in ordered:
            frame = self._rebuild_checksum(frame, parts, index, checksum)
        # The frame and every checksum field now hold consistent values, so the
        # stored results are refreshed to match this rebuild.
        self._checksum_results = [checksum.result() for _, checksum in direct]
        return frame

    def _rebuild_checksum(self, frame: Tibs, parts: list[Tibs], index: int,
                          checksum: Checksum) -> Tibs:
        own_start = sum(len(part) for part in parts[:index])
        own_end = own_start + len(parts[index])
        spans = self._part_spans(parts)
        start = self._resolve_build_position(checksum.start, spans, is_start=True, default=0)
        end = self._resolve_build_position(checksum.end, spans, is_start=False,
                                           default=own_start)
        if start is None or end is None:
            raise ChecksumError(f"Can't resolve the coverage of checksum '{checksum}'.")
        segments = coverage_segments(len(frame), start, end, own_start, own_end)
        if segments is None:
            raise ChecksumError(f"Invalid coverage for checksum '{checksum}': "
                                f"bits {start}..{end} of a {len(frame)} bit frame.")
        protected = coverage_bits(frame, segments)
        try:
            expected = int(checksum.rule(protected))
        except Exception as e:
            raise ChecksumError(f"Checksum '{checksum}' rule failed: {type(e).__name__}: {e}") from e
        expected &= (1 << checksum.width) - 1
        if checksum.writable:
            checksum._set_value_no_const_check(expected)
            checksum._expected = expected
            checksum._ok = True
            checksum._covered = (own_start, own_end)
            checksum._segments = segments
        elif checksum._value is None:
            checksum._set_value_no_const_check(expected)
        new_bits = checksum.to_tibs()
        if len(new_bits) != len(parts[index]):
            raise ChecksumError(f"Checksum '{checksum}' changed width while rebuilding.")
        parts[index] = new_bits
        return Tibs.from_joined([frame[:own_start], new_bits, frame[own_end:]])

    def _part_spans(self, parts: list[Tibs]) -> dict[str, tuple[int, int]]:
        spans: dict[str, tuple[int, int]] = {}
        pos = 0
        for fieldtype, part in zip(self._fields, parts):
            if fieldtype.name:
                spans[fieldtype.name] = (pos, pos + len(part))
            pos += len(part)
        return spans

    def _resolve_build_position(self, spec: Any, spans: dict[str, tuple[int, int]],
                                is_start: bool, default: int | None) -> int | None:
        if spec is None:
            return default
        if isinstance(spec, Expression):
            try:
                value = spec.evaluate(**self.vars)
            except ExpressionError:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        if isinstance(spec, int):
            return spec
        span = spans.get(spec)
        if span is None:
            return None
        return span[0] if is_start else span[1]

    def __len__(self) -> int:
        return len(self._fields)

    def __getitem__(self, key: slice | str | int) -> Any:
        if isinstance(key, int):
            return self._fields[key]
        if isinstance(key, slice):
            return self.__class__.from_params(self._fields[key])
        try:
            return self._field_names[key]
        except KeyError:
            raise KeyError(f"Field with name '{key}' not found.")

    def __setitem__(self, key: slice | str | int, value: str | FieldType) -> None:
        if isinstance(value, str):
            try:
                _ = FieldType.from_string(value)
            except ValueError as e:
                raise ValueError(f"Can't set field from string: {e}") from None
        elif isinstance(value, FieldType):
            field = value
        else:
            raise ValueError(f"Can't create and set field from type '{type(value)}'.")
        if isinstance(key, int):
            self._fields[key] = field
        elif isinstance(key, slice):
            self._fields[key] = field
        elif isinstance(key, str):
            for i in range(len(self._fields)):
                if self._fields[i].name == key:
                    self._fields[i] = field
                    return
            raise KeyError(f"Field with name '{key}' not found.")
        else:
            raise TypeError(f"Invalid key type {type(key)}.")

    def __delitem__(self, key: slice | int | str):
        if isinstance(key, int):
            del self._fields[key]
        elif isinstance(key, slice):
            del self._fields[key]
        elif isinstance(key, str):
            for i, field in enumerate(self._fields):
                if field.name == key:
                    del self._fields[i]
                    return
            raise KeyError(f"Field with name '{key}' not found.")
        else:
            raise TypeError(f"Invalid key type {type(key)}.")

    @override
    def _info(self, use_colour: bool) -> str:
        field_count = len(self._fields)
        name_str = "" if self.name == "" else f" '{self.name}'"
        if field_count == 0:
            return f"an empty Format{name_str}."
        s = f"format{name_str} with {len(self._fields)} field{'' if field_count == 1 else 's'}."
        return s

    @override
    def _str(self, indent: Indenter, use_colour: bool) -> str:
        colour = Colour(use_colour)
        name_str = "" if self.name == "" else f"{colour.name}{self.name}{colour.off}: "
        s = ""
        s += indent(f"{name_str}(\n")
        if self._fields:
            with indent:
                for fieldtype in self._fields[:-1]:
                    s += fieldtype._str(indent, use_colour) + ",\n"
                s += self._fields[-1]._str(indent, use_colour) + '\n'
        s += indent(")")
        return s

    @override
    def _repr(self) -> str:
        name_str = "" if self.name == "" else f", name={self.name!r}"
        s = f"{self.__class__.__name__}.from_params(["
        for i, fieldtype in enumerate(self._fields):
            s += fieldtype._repr()
            if i != len(self._fields) - 1:
                s += ", "
        s += f"]{name_str})"
        return s

    def __iadd__(self, other: FieldType | str) -> Format:
        if isinstance(other, str):
            other = FieldType.from_string(other)
        field_copy = copy.copy(other)
        if other.name != "":
            self._field_names[other.name] = field_copy
        self._fields.append(field_copy)
        return self

    def __copy__(self) -> Format:
        x = Format()
        x.name = self.name
        x.vars = copy.deepcopy(self.vars)
        x._fields = [copy.copy(f) for f in self._fields]
        return x

    def __add__(self, other: FieldType | str) -> Format:
        """
        Add a field to a copy of the format.

        :param other: The field to add.
        :return: The updated format.
        """
        x = copy.copy(self)
        x.__iadd__(other)
        return x

    def append(self, value: Any) -> None:
        """
        Append a field to the format.

        :param value: The field to append.
        """
        self.__iadd__(value)

    def extend(self, values: Iterable) -> None:
        """
        Extend the format with multiple fields.

        :param values: The fields to add.
        """
        for value in values:
            self.__iadd__(value)

    @override
    def __eq__(self, other) -> bool:
        if not isinstance(other, Format):
            return False
        if self.name != other.name:
            return False
        if self.vars != other.vars:
            return False
        if self._fields != other._fields:
            return False
        return True

    @override
    def _get_name(self) -> str:
        return self._name

    @override
    def _set_name(self, name: str) -> None:
        self._name = validate_name(name)
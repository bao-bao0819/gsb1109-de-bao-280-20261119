from __future__ import annotations

from fractions import Fraction

import pytest

import bitformat
from bitformat import (Array, Dtype, DtypeScaled, Field, Format,
                       ScaledIntError, ScaledIntMissingError, ScaledIntPrecisionError,
                       ScaledIntRangeError)


class TestScaledDtypeBasics:
    def test_creation_from_string(self):
        d = Dtype('si8(1/2, 0)')
        assert isinstance(d, DtypeScaled)
        assert d.bit_length == 8
        assert d.scale == Fraction(1, 2)
        assert d.offset == Fraction(0)
        assert d.missing is None
        assert d.kind is bitformat.DtypeKind.SCALED_INT

    def test_creation_with_missing_code(self):
        d = Dtype('si8(1/4, 16, -128)')
        assert d.scale == Fraction(1, 4)
        assert d.offset == Fraction(16)
        assert d.missing == -128

    def test_creation_from_params(self):
        d = DtypeScaled.from_params(8, Fraction(1, 2), 0, -128)
        assert d == Dtype('si8(1/2, 0, -128)')
        assert DtypeScaled.from_params(8, 0.5, 16) == Dtype('si8(1/2, 16)')

    def test_str_roundtrip(self):
        for s in ['si8(1/2, 0)', 'si8(1/4, 16, -128)', 'si12(3, -1, 0)', 'si5(7/3, 2/5)']:
            d = Dtype(s)
            assert str(d) == s
            assert Dtype(str(d)) == d

    def test_unpack_uses_own_scale_and_offset(self):
        # Same bit width and same raw bits, but each block converts with its own scale/offset.
        d1 = Dtype('si8(1/2, 0)')
        d2 = Dtype('si8(1/4, 16)')
        bits = '0b00100000'  # raw code 32
        assert d1.unpack(bits) == 16
        assert d2.unpack(bits) == 24

    def test_unpack_returns_exact_fraction(self):
        d = Dtype('si8(1/3, 1/7)')
        v = d.unpack('0b00000001')
        assert v == Fraction(1, 3) + Fraction(1, 7)
        assert isinstance(v, Fraction)

    def test_negative_values_roundtrip(self):
        d = Dtype('si8(1/4, 16, -128)')
        # A genuine negative measurement, not the missing code.
        bits = d.pack(Fraction(-9))
        assert bits == '0b10011100'  # raw code -100
        assert d.unpack(bits) == -9

    def test_zero_scale_not_allowed(self):
        with pytest.raises(ValueError):
            DtypeScaled.from_params(8, 0, 0)

    def test_missing_code_must_fit(self):
        with pytest.raises(ValueError):
            DtypeScaled.from_params(8, 1, 0, 128)
        with pytest.raises(ValueError):
            DtypeScaled.from_params(8, 1, 0, -129)

    def test_size_must_be_positive(self):
        with pytest.raises(ValueError):
            DtypeScaled.from_params(0, 1, 0)

    def test_invalid_dtype_strings(self):
        for bad in ['si8(1/2)', 'si8(1/0, 0)', 'si8(a, 0)', 'si(1/2, 0)', 'si8(1/2, 0, 999)',
                    'si8(1/2, 0, 1, 2)', 'si8()']:
            with pytest.raises(ValueError):
                Dtype(bad)

    def test_field_named_si(self):
        # A field can still be called 'si' without clashing with the dtype kind.
        f = Format('(si: u8, t: si8(1/2, 0))')
        f.parse('0b0000001100000101')
        assert f.value == [3, Fraction(5, 2)]

    def test_decimal_and_negative_params(self):
        assert Dtype('si8(0.5, 0)') == Dtype('si8(1/2, 0)')
        d = Dtype('si8(2, -4)')
        assert d.unpack('0b00000101') == 6  # raw 5 -> 5 * 2 - 4


class TestScaledPackingRules:
    def test_exact_writeback(self):
        d = Dtype('si8(1/2, 0)')
        assert d.pack(Fraction(5, 2)) == '0b00000101'
        assert d.pack(-16) == '0b11100000'  # raw code -32 in units of 1/2
        assert d.pack(2.5) == '0b00000101'
        assert d.pack('1/2') == '0b00000001'

    def test_not_exactly_representable_raises(self):
        d = Dtype('si8(1/2, 0)')
        with pytest.raises(ScaledIntPrecisionError):
            d.pack(Fraction(1, 3))
        with pytest.raises(ScaledIntPrecisionError):
            d.pack(0.1)
        # A value representable with scale 1/4 but not 1/2.
        assert Dtype('si8(1/4, 0)').pack(Fraction(1, 4)) == '0b00000001'
        with pytest.raises(ScaledIntPrecisionError):
            d.pack(Fraction(1, 4))

    def test_out_of_range_raises(self):
        d = Dtype('si8(1/2, 0)')
        with pytest.raises(ScaledIntRangeError):
            d.pack(64)  # raw code 128 doesn't fit in 8 signed bits
        with pytest.raises(ScaledIntRangeError):
            d.pack(-65)
        # The extremes of the range are fine.
        assert d.unpack(d.pack(Fraction(-64))) == -64
        assert d.unpack(d.pack(Fraction(63, 2))) == Fraction(63, 2)

    def test_missing_code_collision_raises(self):
        d = Dtype('si8(1/4, 16, -128)')
        # -16 would need the raw code -128, which is reserved for missing.
        with pytest.raises(ScaledIntMissingError):
            d.pack(Fraction(-16))
        with pytest.raises(ScaledIntMissingError):
            d.pack(-16)

    def test_missing_written_only_as_none(self):
        d = Dtype('si8(1/4, 16, -128)')
        assert d.pack(None) == '0b10000000'
        assert d.unpack('0b10000000') is None

    def test_none_without_missing_code_raises(self):
        d = Dtype('si8(1/2, 0)')
        with pytest.raises(ScaledIntMissingError):
            d.pack(None)

    def test_errors_are_value_errors(self):
        d = Dtype('si8(1/4, 16, -128)')
        for exc, value in [(ScaledIntPrecisionError, Fraction(1, 3)),
                           (ScaledIntRangeError, 1000),
                           (ScaledIntMissingError, -16)]:
            with pytest.raises(ValueError):
                d.pack(value)
            with pytest.raises(ScaledIntError):
                d.pack(value)
            with pytest.raises(exc):
                d.pack(value)

    def test_no_silent_rounding(self):
        # Values are never rounded to a representable one, even when very close.
        d = Dtype('si8(1/2, 0)')
        with pytest.raises(ScaledIntPrecisionError):
            d.pack(Fraction(5000000000000001, 1000000000000000))


class TestScaledExpressions:
    def test_expression_scale_evaluated_on_parse(self):
        f = Format('(t: si8({s}, 0))')
        f.parse('0b00001010', s=Fraction(1, 2))
        assert f['t'].value == 5
        # Reusing the same format object with a different scale re-evaluates the conversion.
        f.parse('0b00001010', s=Fraction(1, 4))
        assert f['t'].value == Fraction(5, 2)

    def test_same_format_object_next_package_scale_change(self):
        # The same format object processes consecutive packages whose scale changed.
        fmt = Format('(block1: si8({s1}, 0, 127), block2: si8({s2}, 16, -128))')
        raw = '0b1110000010011100'  # block1 raw -32, block2 raw -100
        fmt.parse(raw, s1=Fraction(1, 2), s2=Fraction(1, 4))
        assert fmt.value == [Fraction(-16), Fraction(-9)]
        # The next package reuses the same raw bits but declares a different scale.
        # The previous scale must not be borrowed.
        fmt.parse(raw, s1=Fraction(1, 2), s2=Fraction(1, 8))
        assert fmt.value == [Fraction(-16), Fraction(7, 2)]

    def test_expression_missing_code(self):
        f = Format('(t: si8(1/2, 0, {m}))')
        f.parse('0b11111111', m=-1)
        assert f['t'].value is None

    def test_unevaluated_expression_raises(self):
        d = Dtype('si8({s}, 0)')
        with pytest.raises(ValueError):
            d.unpack('0b00001010')
        with pytest.raises(ValueError):
            d.pack(5)

    def test_evaluate_returns_concrete_dtype(self):
        d = Dtype('si8({s}, {o}, {m})')
        c = d.evaluate(s=Fraction(1, 2), o=16, m=-128)
        assert c == Dtype('si8(1/2, 16, -128)')
        assert c.has_known_size()
        assert not d.has_known_size()


class TestScaledInFormats:
    def test_two_blocks_same_width_different_scale(self):
        # One package, two blocks with the same bit width but different scale/offset,
        # each declaring its own raw missing code.
        fmt = Format('(block1: si8(1/2, 0, 127), block2: si8(1/4, 16, -128))')
        fmt.parse('0b1110000010011100')
        assert fmt['block1'].value == -16  # raw -32 in units of 1/2
        assert fmt['block2'].value == -9   # raw -100 in units of 1/4 plus 16

    def test_instrument_scenario(self):
        # The full archiving round-trip: unpack a package, write corrections back
        # exactly, distinguish the failure modes, then re-read the successful writes.
        fmt = Format('(block1: si8(1/2, 0, 127), block2: si8(1/4, 16, -128))')
        fmt.parse('0b0000101010011100')
        assert fmt['block1'].value == 5
        assert fmt['block2'].value == -9

        # The temperature that the second block's minimum raw code would compute to:
        # -128 * 1/4 + 16 = -16. Written into block1 it is exactly representable.
        min_raw_block2_temp = Fraction(-128) * Fraction(1, 4) + 16
        assert min_raw_block2_temp == -16
        fmt['block1'].value = min_raw_block2_temp

        # A temperature that block2 cannot exactly represent is rejected, not rounded.
        with pytest.raises(ScaledIntPrecisionError):
            fmt['block2'].value = Fraction(1, 3)
        # The same temperature written to block2 itself can only land on its missing code.
        with pytest.raises(ScaledIntMissingError):
            fmt['block2'].value = min_raw_block2_temp
        # And an out-of-range temperature is a third, distinct failure.
        with pytest.raises(ScaledIntRangeError):
            fmt['block2'].value = 1000

        # The failed writes didn't alter the block; re-read all successfully written blocks.
        assert fmt['block1'].value == -16
        assert fmt['block2'].value == -9
        reread = fmt.to_tibs()
        assert reread == '0b1110000010011100'
        fmt2 = Format('(block1: si8(1/2, 0, 127), block2: si8(1/4, 16, -128))')
        fmt2.parse(reread)
        assert fmt2.value == [Fraction(-16), Fraction(-9)]

    def test_missing_value_roundtrip_in_format(self):
        fmt = Format('(block1: si8(1/2, 0, 127), block2: si8(1/4, 16, -128))')
        bits = fmt.pack([Fraction(3, 2), None])
        assert bits == '0b0000001110000000'
        fmt.parse(bits)
        assert fmt['block1'].value == Fraction(3, 2)
        assert fmt['block2'].value is None
        # Missing can only be written via the reserved code.
        fmt['block2'].value = None
        assert fmt.to_tibs() == bits

    def test_negative_readings_do_not_disappear(self):
        # Genuine negative measurements next to a missing code all survive the round-trip.
        fmt = Format('(a: si8(1/4, 16, -128), b: si8(1/4, 16, -128), c: si8(1/4, 16, -128))')
        bits = fmt.pack([Fraction(-1), None, Fraction(-63, 4)])
        fmt.parse(bits)
        assert fmt.value == [Fraction(-1), None, Fraction(-63, 4)]

    def test_field_none_ignored_for_plain_ints(self):
        # Plain integer fields keep their existing behaviour: None doesn't write anything.
        f = Field('t: i8')
        f.value = None
        assert f.value is None
        with pytest.raises(ValueError):
            f.to_tibs()

    def test_scaled_field_accepts_none_only_with_missing_code(self):
        f = Field('t: si8(1/2, 0)')
        with pytest.raises(ValueError):
            f.value = None
        g = Field('t: si8(1/2, 0, -128)')
        g.value = None
        assert g.to_tibs() == '0b10000000'
        assert g.value is None

    def test_scaled_in_tuple_dtype(self):
        d = Dtype('(u8, si8(1/2, 0))')
        bits = d.pack([3, Fraction(5, 2)])
        assert d.unpack(bits) == (3, Fraction(5, 2))

    def test_scaled_field_info(self):
        d = Dtype('si8(1/4, 16, -128)')
        assert '1/4' in d.info()
        assert '16' in d.info()

    def test_pp_with_scaled_dtype(self):
        import io
        t = bitformat.Tibs('0b1110000010011100')
        s = io.StringIO()
        t.pp('si8(1/2, 0)', stream=s)
        out = s.getvalue()
        assert '-16' in out and '-50' in out
        with pytest.raises(ValueError):
            t.pp('si8({s}, 0)')


class TestScaledArrays:
    def test_plain_int_arrays_unchanged(self):
        a = Array('i8', [1, -2, 3])
        assert a.to_list() == [1, -2, 3]
        a.append(-128)
        assert a[-1] == -128
        assert a.dtype == 'i8'

    def test_scaled_array(self):
        a = Array('si8(1/2, 0)', [Fraction(1, 2), -3, Fraction(5, 2)])
        assert a.to_list() == [Fraction(1, 2), Fraction(-3), Fraction(5, 2)]
        a[0] = Fraction(7, 2)
        assert a[0] == Fraction(7, 2)
        with pytest.raises(ScaledIntPrecisionError):
            a.append(Fraction(1, 3))

    def test_scaled_array_with_missing(self):
        a = Array('si8(1/4, 16, -128)', [Fraction(16), None, Fraction(-1)])
        assert a.to_list() == [Fraction(16), None, Fraction(-1)]
        assert a[1] is None


class TestScaledExactRationals:
    @pytest.mark.parametrize("scale,offset", [(Fraction(1, 2), 0), (Fraction(1, 4), 16),
                                              (Fraction(3, 7), Fraction(-2, 5)), (Fraction(10), Fraction(-1, 3))])
    def test_roundtrip_all_raw_codes(self, scale, offset):
        d = DtypeScaled.from_params(8, scale, offset)
        for raw in range(-128, 128):
            bits = bitformat.Tibs.from_i(raw, 8)
            value = d.unpack(bits)
            assert value == raw * scale + offset
            assert d.pack(value) == bits

    def test_thirds_are_exact(self):
        d = Dtype('si8(1/3, 0)')
        v = d.unpack('0b00000001')
        assert v == Fraction(1, 3)
        assert d.pack(v) == '0b00000001'
        # A float approximation of a third is not silently accepted as exact.
        with pytest.raises(ScaledIntPrecisionError):
            d.pack(1 / 3)

from fractions import Fraction

import pytest

from bitformat import Array, Dtype, DtypeKind, DtypeScaled, Field, Format, Scaled, Tibs


def degrees() -> DtypeScaled:
    return Dtype("i12[scale=1/16,missing=-2048,unit=degC]")


def test_calibration_is_exact_and_accessible():
    d = degrees()
    assert isinstance(d, DtypeScaled)
    assert d.scale == Fraction(1, 16)
    assert d.offset == 0
    assert d.missing == -2048
    assert d.unit == "degC"
    assert d.kind is DtypeKind.SCALED
    assert d.bit_length == 12
    assert d.raw_dtype == Dtype("i12")


def test_alternate_spellings_and_aliases():
    expected = degrees()
    for text in [
        "i12*0.0625[missing=-2048,unit=degC]",
        "i12*1/16[missing=min,unit=degC]",
        "s12[scale=0.0625,missing=min,unit=degC]",
        "scaled(i12, 0.0625, 0, -2048, degC)",
    ]:
        assert Dtype(text) == expected
    assert Scaled is DtypeScaled
    assert Dtype("s12").bit_length == 12


def test_negative_boundary_reads_back():
    d = degrees()
    b = d.pack(-127.9375)
    assert b.bin == "100000000001"
    assert d.unpack(b) == -127.9375
    assert d.unpack_raw(b) == -2047


def test_highest_valid_value_reads_back():
    d = degrees()
    b = d.pack(127.9375)
    assert b.bin == "011111111111"
    assert d.unpack(b) == 127.9375
    assert d.unpack_raw(b) == 2047


def test_missing_code_reads_back_as_none_and_packs_back():
    d = degrees()
    b = d.pack(None)
    assert b.bin == "100000000000"
    assert d.unpack(b) is None
    assert d.unpack_raw(b) == -2048
    assert d.pack_raw(-2048) == b


def test_out_of_range_writes_raise():
    d = degrees()
    with pytest.raises(ValueError):
        d.pack(128.0)
    with pytest.raises(ValueError):
        d.pack(-128.0)  # The missing code is -2048, so -128.0 maps to it.
    with pytest.raises(ValueError):
        d.pack(10_000)
    with pytest.raises(ValueError):
        d.pack_raw(2048)
    with pytest.raises(ValueError):
        d.pack_raw(-2049)


def test_offset_and_missing_range():
    d = Dtype("i16[scale=1/16,offset=-40,missing=min]")
    assert d.scale == Fraction(1, 16)
    assert d.offset == -40
    assert d.missing == -32768
    assert d.min_value == -2087.9375
    assert d.max_value == 2007.9375
    for value in (d.min_value, d.max_value, -40, 0, 123.4375):
        assert d.unpack(d.pack(value)) == value
    with pytest.raises(ValueError):
        d.pack(d.max_value + 1 / 16)


def test_array_uses_scaled_dtype():
    d = degrees()
    a = Array(d, [-127.9375, 127.9375, None])
    assert a.to_list() == [-127.9375, 127.9375, None]
    assert a.count(None) == 1
    assert a[2] is None
    a[2] = -1.5
    assert a.to_list() == [-127.9375, 127.9375, -1.5]
    with pytest.raises(ValueError):
        a[0] = 1000

    raw = Array("i12", [-2048, -2047, 2047])
    raw.dtype = d
    assert raw.to_list() == [None, -127.9375, 127.9375]


def test_scaled_values_in_fields_and_formats():
    fmt = Format("(n: u4, temps: [i12[scale=1/16,missing=-2048]; {n}])")
    fmt.pack([3, [None, 127.9375, -127.9375]])
    assert fmt.to_tibs().bin == "0011100000000000011111111111100000000001"
    assert fmt["temps"].value == (None, 127.9375, -127.9375)
    fmt2 = Format("(n: u4, temps: [i12[scale=1/16,missing=-2048]; {n}])")
    fmt2.parse(fmt.to_tibs())
    assert fmt2["temps"].value == (None, 127.9375, -127.9375)

    f = Field("t: i12[scale=1/16,missing=min]")
    f.pack(None)
    assert f.value is None
    assert f.to_tibs().bin == "100000000000"


def test_tibs_helpers():
    d = degrees()
    b = Tibs.from_dtype(d, 21.5)
    assert b.unpack(d) == 21.5
    assert b.unpack(d.raw_dtype) == 344

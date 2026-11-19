import pytest

from bitformat import Array, Format, Mutibs, Tibs


def _sample(sample: int, flag: int) -> int:
    return (sample << 3) | flag


SAMPLES = [_sample(1, 1), _sample(2, 2), _sample(3, 3), _sample(4, 4), _sample(5, 5)]
FLAG = 0b101


def _packed_samples(samples=SAMPLES, flag=FLAG) -> Tibs:
    return Tibs.from_joined([Tibs.from_dtype("u10", x) for x in samples]) + Tibs.from_dtype("u3", flag)


def _read_samples(bits: Tibs) -> tuple[list[int], int]:
    samples = [bits.unpack("u10", i, i + 10) for i in range(0, len(bits) - 3, 10)]
    return samples, bits[-3:].unpack("u3")


def test_tibs_to_bytes_is_strict_for_partial_bytes():
    b = Tibs.from_bytes(b"ABCD") + "0b1"
    with pytest.raises(ValueError):
        b.to_bytes()


def test_tibs_native_endian_views():
    little = Tibs.from_dtype("u16_le", 0x1234)
    assert little == "0x3412"
    assert little.le.u == 0x1234
    assert little.be.u == 0x3412
    assert little.unpack("u16_le") == 0x1234


def test_tibs_byte_aligned_search_is_explicit_argument():
    b = Tibs("0x00 ff 0f f")
    assert b.find_all("0xff") == [8, 20]
    assert b.find_all("0xff", byte_aligned=True) == [8]


def test_mutibs_slice_assignment_uses_direct_tibs_semantics():
    b = Mutibs("0b101010")
    b[1:4] = "0b111"
    assert b == "0b111110"
    b[0:2] = Tibs("0b00")
    assert b == "0b001110"


def test_equal_length_slice_replacement_keeps_trailing_flag():
    b = _packed_samples().to_mutibs()
    replacement = Tibs.from_joined([Tibs.from_dtype("u10", _sample(9, 6)),
                                    Tibs.from_dtype("u10", _sample(8, 7))])
    b[10:30] = replacement

    samples, flag = _read_samples(b.to_tibs())
    assert samples == [_sample(1, 1), _sample(9, 6), _sample(8, 7), _sample(4, 4), _sample(5, 5)]
    assert flag == FLAG
    assert len(b) == len(SAMPLES) * 10 + 3


def test_inserting_one_element_keeps_trailing_flag():
    b = _packed_samples().to_mutibs()
    b[20:20] = Tibs.from_dtype("u10", _sample(7, 0))

    samples, flag = _read_samples(b.to_tibs())
    assert samples == [_sample(1, 1), _sample(2, 2), _sample(7, 0),
                       _sample(3, 3), _sample(4, 4), _sample(5, 5)]
    assert flag == FLAG
    assert len(b) == (len(SAMPLES) + 1) * 10 + 3


def test_array_slice_operations_keep_trailing_flag():
    a = Array.from_tibs("u10", _packed_samples())
    a[1:3] = [_sample(9, 6), _sample(8, 7)]
    assert a.to_list() == [_sample(1, 1), _sample(9, 6), _sample(8, 7), _sample(4, 4), _sample(5, 5)]
    assert a.trailing_bits == Tibs.from_dtype("u3", FLAG)

    a.insert(2, _sample(7, 0))
    assert a.to_list() == [_sample(1, 1), _sample(9, 6), _sample(7, 0), _sample(8, 7),
                           _sample(4, 4), _sample(5, 5)]
    assert a.trailing_bits == Tibs.from_dtype("u3", FLAG)
    assert len(a.to_bytes()) == (len(a.bits) + 7) // 8


def test_format_slice_replacement_and_insertion_then_read_back():
    def build() -> Format:
        f = Format("(s0: u10, s1: u10, s2: u10, s3: u10, s4: u10, flag: u3, pad: pad3)")
        f.pack(SAMPLES + [FLAG])
        return f

    f = build()
    f[1:3] = Format.from_params(["u10", "u10"])
    f[1].value = _sample(9, 6)
    f[2].value = _sample(8, 7)
    data = f.to_bytes()

    g = Format("(s0: u10, s1: u10, s2: u10, s3: u10, s4: u10, flag: u3, pad: pad3)")
    assert g.parse(data) == 56
    assert g.value == [_sample(1, 1), _sample(9, 6), _sample(8, 7), _sample(4, 4), _sample(5, 5), FLAG]
    assert g["flag"].value == FLAG

    f = build()
    f[2:2] = "u10"
    f[2].value = _sample(7, 0)
    data = f.to_bytes()
    h = Format("(s0: u10, s1: u10, s2: u10, s3: u10, s4: u10, s5: u10, flag: u3, pad: pad3)")
    assert h.parse(data) == 66
    assert h.value == [_sample(1, 1), _sample(2, 2), _sample(7, 0), _sample(3, 3),
                       _sample(4, 4), _sample(5, 5), FLAG]
    assert h["flag"].value == FLAG


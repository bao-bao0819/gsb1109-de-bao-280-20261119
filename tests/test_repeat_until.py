from bitformat import RepeatUntil, Repeat, Format, FieldType, Tibs, Field
import pytest


def test_creation():
    r = RepeatUntil.from_params("u16", "0xffff")
    assert r.field == Field("u16")
    assert r.marker == Tibs("0xffff")
    assert r.consume is True


def test_creation_with_keywords_both_ways():
    a = RepeatUntil.from_params(field="u16", marker="0xffff")
    b = RepeatUntil.from_params(marker="0xffff", field="u16")
    c = RepeatUntil.from_params(marker="0xffff", fieldtype="u16")
    d = RepeatUntil.from_params(until="0xffff", field="u16")
    assert a == b == c == d


def test_from_string():
    r = RepeatUntil.from_string("repeat until 0xffff: u16")
    assert r.marker == Tibs("0xffff")
    assert r.field == Field("u16")
    assert r.consume is True
    assert str(r) == "repeat until 0xffff:\n    u16"


def test_from_string_keep():
    r = RepeatUntil("repeat until 0xffff keep: u16")
    assert r.consume is False
    assert str(r) == "repeat until 0xffff keep:\n    u16"


def test_round_trip():
    r = RepeatUntil("repeat until 0xffff: (sensor: u16, flags: u8)")
    assert FieldType.from_string(str(r)) == r
    assert eval(repr(r)) == r


def test_simple_parse():
    r = RepeatUntil("repeat until 0xffff: u16")
    assert r.parse("0x00010002ffff") == 48
    assert r.value == [1, 2]
    assert r.bit_length == 48
    assert r.to_tibs() == "0x00010002ffff"


def test_marker_inside_record_is_not_a_terminator():
    r = RepeatUntil("repeat until 0xffff: (sensor: u16, flags: u8)")
    # The first record's bits contain 'ffff' starting at bit 8, but that's not a
    # record boundary, so the loop continues until the real marker at bit 48.
    assert r.unpack("0x00ffff" + "0001ff" + "ffff") == [[0x00ff, 0xff], [1, 0xff]]


def test_marker_can_start_at_a_record_boundary_only():
    r = RepeatUntil("repeat until 0xffff: u16")
    # 0x00ff followed by 0xff00 contains the marker bits but not at a record start.
    assert r.unpack("0x00ff0001" + "ffff") == [0x00ff, 1]


def test_unaligned_marker():
    r = RepeatUntil("repeat until 0xfff: u12")
    data = Tibs.from_joined(["0x001", "0x002", "0xfff"])
    assert r.unpack(data) == [1, 2]
    assert r.to_tibs() == data


def test_marker_that_spans_a_byte_boundary():
    # A 12-bit marker can straddle a byte. Records are 4 bits so this is all unaligned.
    r = RepeatUntil("repeat until 0b111111111111: u4")
    data = Tibs("0b1010" "0101" "1111" "1111" "1111")
    assert r.unpack(data) == [0b1010, 0b0101]


def test_loop_starting_at_a_non_byte_boundary():
    f = Format("(flag: bool, repeat until 0xfff keep: u12, stop: u12)")
    data = Tibs("0b1") + Tibs.from_joined(["0x001", "0xfff"])
    assert f.unpack(data) == [True, [1], 0xfff]
    assert f.to_tibs() == data


def test_keep_leaves_marker_for_next_field():
    f = Format("(repeat until 0xffff keep: u16, stop: u16, tail: u8)")
    assert f.unpack("0x0001ffffab") == [[1], 0xffff, 0xab]
    assert f.to_tibs() == "0x0001ffffab"


def test_consumed_marker_is_not_available_to_next_field():
    f = Format("(repeat until 0xffff: u16, tail: u8)")
    assert f.unpack("0x0001ffffab") == [[1], 0xab]
    assert f.to_tibs() == "0x0001ffffab"


def test_empty_loop_consumed_marker():
    r = RepeatUntil("repeat until 0xffff: u16")
    assert r.unpack("0xffff") == []
    assert r.bit_length == 16


def test_empty_loop_kept_marker():
    r = RepeatUntil("repeat until 0xffff keep: u16")
    assert r.unpack("0xffff") == []
    assert r.bit_length == 0
    assert r.to_tibs() == Tibs()


def test_half_marker_is_incomplete_packet():
    r = RepeatUntil("repeat until 0xffff: u16")
    # 0xff is the first half of the 16-bit marker.
    with pytest.raises(ValueError, match="Incomplete marker"):
        r.parse(Tibs("0x0001") + Tibs("0xff"))
    # So is a shorter prefix such as 0b1111.
    with pytest.raises(ValueError, match="Incomplete marker"):
        r.parse(Tibs("0x0001") + Tibs("0b1111"))
    # A 12-bit marker split across a byte boundary: 0b111111 is half of it.
    r12 = RepeatUntil("repeat until 0xfff: u8")
    with pytest.raises(ValueError, match="Incomplete marker"):
        r12.parse(Tibs("0x01") + Tibs("0b111111"))


def test_missing_marker_is_incomplete_packet():
    r = RepeatUntil("repeat until 0xffff: u16")
    with pytest.raises(ValueError):
        r.parse("0x00010002")


def test_non_matching_tail_is_not_an_incomplete_marker():
    r = RepeatUntil("repeat until 0xffff: u16")
    # 7 bits left that can't start the marker: it's just data that ran out.
    with pytest.raises(ValueError):
        r.parse(Tibs("0x0001") + Tibs("0b1010101"))


def test_pack():
    r = RepeatUntil("repeat until 0xffff: u16")
    r.pack([1, 2])
    assert r.value == [1, 2]
    assert r.to_tibs() == "0x00010002ffff"


def test_pack_keep_does_not_append_marker():
    r = RepeatUntil("repeat until 0xffff keep: u16")
    r.pack([1, 2])
    assert r.to_tibs() == "0x00010002"


def test_pack_errors():
    r = RepeatUntil("repeat until 0xffff: u16")
    with pytest.raises(TypeError):
        r.pack("not a sequence")


def test_repeat_until_via_repeat_alias():
    r = Repeat.from_params("u16", until="0xffff")
    assert isinstance(r, RepeatUntil)
    assert r.unpack("0x0001ffff") == [1]


def test_zero_length_field_is_rejected():
    r = RepeatUntil.from_params("pass", "0xffff")
    with pytest.raises(ValueError, match="consumed no bits"):
        r.parse(Tibs("0x0000"))


def test_empty_marker_is_rejected():
    with pytest.raises(ValueError, match="cannot be empty"):
        RepeatUntil.from_params("u8", "")


def test_equality():
    assert RepeatUntil("repeat until 0xffff: u16") == RepeatUntil.from_params("u16", "0xffff")
    assert RepeatUntil("repeat until 0xffff: u16") != RepeatUntil("repeat until 0xffff keep: u16")
    assert RepeatUntil("repeat until 0xffff: u16") != RepeatUntil("repeat until 0xfffe: u16")
    assert RepeatUntil("repeat until 0xffff: u16") != RepeatUntil("repeat until 0xffff: u8")


def test_clear():
    r = RepeatUntil("repeat until 0xffff: u16")
    r.parse("0x0001ffff")
    assert r.value == [1]
    r.clear()
    assert r.value is None
    with pytest.raises(ValueError):
        _ = r.bit_length


def test_info():
    assert "0xffff" in RepeatUntil("repeat until 0xffff: u16").info()

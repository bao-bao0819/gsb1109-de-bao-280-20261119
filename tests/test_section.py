import pytest
from bitformat import Format, Section, Field, FieldType, Tibs, Expression


def telemetry_frame() -> Format:
    # A frame that declares the extension region length up front, with the
    # extension region containing its own entry count.
    return Format.from_string("""(
        ext_len: u8,
        ext: section {ext_len * 8}: (
            count: u8,
            repeat {count}: u16
        ),
        next_header: u8
    )""")


def test_creation():
    s = Section.from_params(16, "u8")
    assert s.length == 16
    assert s.field == Field("u8")
    assert s.name == ""
    assert s.bit_length == 16
    t = Section.from_params("{x}", "u8", name="hdr")
    assert t.name == "hdr"
    with pytest.raises(ValueError):
        _ = t.bit_length


def test_from_string():
    s = Section.from_string("section {16}: (u8, u8)")
    assert isinstance(s, Section)
    assert s.name == ""
    assert s.bit_length == 16
    t = Section.from_string("ext: section {x * 8}: (u8, bool)")
    assert t.name == "ext"
    assert t.length == Expression("{x * 8}")
    u = Section("header: section {8}: u8")
    assert u.name == "header"


def test_str_and_repr():
    s = Section.from_string("ext: section {x * 8}: (u8, bool)")
    assert str(s) == "ext: section {x * 8}:\n    (\n        u8,\n        bool\n    )"
    t = FieldType.from_string(str(s))
    assert t == s
    r = Section.from_params(24, "(u8, u16)", name="p")
    assert repr(r) == "Section.from_params(Expression('24'), Format.from_params(['u8', 'u16']), name='p')"


def test_simple_parse():
    s = Section.from_params(16, "(u8, u8)")
    bits_used = s.parse("0xabcd")
    assert bits_used == 16
    assert s.value == [0xab, 0xcd]
    assert s.to_tibs() == Tibs("0xabcd")


def test_parse_only_consumes_declared_range():
    # The inner fields don't fill the declared range, but the section still
    # occupies all of it, so following fields start at a known position.
    f = Format.from_params(["a: u8", Section.from_params(16, "b: u8", name="mid"), "c: u8"])
    bits_used = f.parse("0x01020004")
    assert bits_used == 32
    assert f["a"].value == 1
    assert f["mid"].value == 2
    assert f["c"].value == 4


def test_telemetry_frame_round_trip():
    frame = telemetry_frame()
    data = Tibs("0x05") + Tibs("0x02") + Tibs("0x00010002") + Tibs("0xff")
    bits_used = frame.parse(data)
    assert bits_used == 56
    assert frame["ext_len"].value == 5
    assert frame["ext"].value == [2, [1, 2]]
    assert frame["next_header"].value == 0xFF


def test_missing_entry_does_not_consume_next_section():
    frame = telemetry_frame()
    # ext_len says the extension region is 2 bytes, but the entry count inside
    # it says there are 2 entries, which can't fit. The next section's header
    # must not be used to make up the missing entry.
    bad = Tibs("0x02") + Tibs("0x02") + Tibs("0xaa") + Tibs("0xff")
    with pytest.raises(ValueError) as excinfo:
        frame.parse(bad)
    msg = str(excinfo.value)
    assert "ext" in msg  # the name of the section the short read belongs to
    assert "bit 8" in msg  # the bit position of the section in the frame


def test_section_name_and_position_in_error():
    f = Format.from_params(["hdr: u8", Section.from_params(32, "(u16, u32)", name="payload")])
    with pytest.raises(ValueError) as excinfo:
        f.parse(Tibs("0x00") * 5)  # 40 bits: section is bits 8..40, inner needs 48
    msg = str(excinfo.value)
    assert "payload" in msg
    assert "bit 8" in msg


def test_truncated_section_reports_name_and_position():
    s = Section.from_params(32, "u32", name="payload")
    with pytest.raises(ValueError) as excinfo:
        s.parse(Tibs("0x00"))
    msg = str(excinfo.value)
    assert "payload" in msg
    assert "bit 0" in msg
    assert "32" in msg


def test_later_sections_can_be_parsed_individually():
    frame = telemetry_frame()
    bad = Tibs("0x02") + Tibs("0x02") + Tibs("0xaa") + Tibs("0xff")
    with pytest.raises(ValueError):
        frame.parse(bad)
    # The extension region is corrupt, but as its bit range is known the
    # following section has a deterministic offset and can be parsed on its own.
    next_header = frame["next_header"]
    offset = 8 + 2 * 8  # ext_len field + declared extension region length
    assert next_header.unpack(bad[offset:]) == 0xFF


def test_pack_pads_to_declared_length():
    s = Section.from_params(24, "(a: u8, b: u8)")
    s.pack([1, 2])
    assert s.to_tibs() == Tibs("0x010200")
    assert s.bit_length == 24


def test_pack_too_long_raises():
    s = Section.from_params(8, "(a: u8, b: u8)", name="small")
    with pytest.raises(ValueError) as excinfo:
        s.pack([1, 2])
    assert "small" in str(excinfo.value)


def test_pack_round_trip():
    s = Section.from_string("p: section {24}: (a: u8, b: u8)")
    s.pack([5, 6])
    t = Section.from_string("p: section {24}: (a: u8, b: u8)")
    t.parse(s.to_tibs())
    assert t.value == [5, 6]


def test_stretchy_field_bounded_by_section():
    # A stretchy dtype inside a section is bounded by the section's bit range.
    f = Format.from_params([Section.from_params(16, "data: bytes", name="blob"), "tail: u8"])
    bits_used = f.parse(Tibs("0xcafeff"))
    assert bits_used == 24
    assert f["blob"].value == b"\xca\xfe"
    assert f["tail"].value == 0xFF


def test_length_from_earlier_field():
    f = Format.from_params(["n: u8", Section.from_params("{n * 8}", "x: u8", name="data")])
    f.parse(Tibs("0x02abcd"))
    assert f["data"].value == 0xAB
    assert f["data"].bit_length == 16


def test_const_section():
    s = Section.from_params(8, "const u8 = 7", name="c")
    assert s.is_const()
    f = Format.from_params(["u8", s, "u8"])
    b = f.pack([1, 2])
    assert b == Tibs("0x010702")


def test_copy_and_eq():
    s = Section.from_string("ext: section {x * 8}: (u8, bool)")
    t = s._copy()
    assert t == s
    assert t.field is not s.field
    t.name = "other"
    assert t != s


def test_clear():
    s = Section.from_params(8, "u8")
    s.parse("0xab")
    assert s.value == 0xAB
    s.clear()
    assert s.field.value is None
    assert s.bit_length == 8  # const length is still known after a clear


def test_length_setter():
    s = Section.from_params(8, "u8")
    s.length = "{x}"
    s.parse("0xab", x=8)
    assert s.value == 0xAB
    s.length = 16
    assert s.bit_length == 16


def test_invalid_lengths():
    with pytest.raises(ValueError):
        Section.from_params(-1, "u8")
    with pytest.raises(ValueError):
        Section.from_params(True, "u8")
    with pytest.raises(ValueError):
        Section.from_params(2.5, "u8")
    s = Section.from_params("{x}", "u8")
    with pytest.raises(ValueError):
        s.parse("0xab", x=-4)
    with pytest.raises(ValueError):
        s.parse("0xab")  # x not provided


def test_field_named_section_still_works():
    f = Format("(section: u8, x: u4)")
    f.parse("0xabc")
    assert f["section"].value == 0xAB


def test_section_inside_format_from_string():
    f = Format("(a: u4, b: section {8}: u8, c: u4)")
    f.parse("0x1abc")
    assert f["a"].value == 1
    assert f["b"].value == [0xAB] if isinstance(f["b"].value, list) else 0xAB
    assert f["c"].value == 0xC


def test_nested_sections():
    s = Section.from_params(24, Section.from_params(16, "u16", name="inner"), name="outer")
    bits_used = s.parse(Tibs("0xbeef") + Tibs("0xff"))
    assert bits_used == 24
    assert s.value == 0xBEEF
    assert s.to_tibs() == Tibs("0xbeefff")

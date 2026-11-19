import pytest

from bitformat import Checksum, ChecksumFormat, Field, Tibs, xor8


def make_frame() -> ChecksumFormat:
    # Layout (24 bits = 3 bytes):
    #   tag:u3            bits  0..3
    #   value:u12         bits  3..15  <- the only covered, non-byte-aligned field
    #   pad1              bit  15       <- alignment padding, not covered
    #   checksum:u8       bits 16..24   XOR over bits [3:15]
    return ChecksumFormat.from_params([
        Field.from_params("u3", name="tag"),
        Field.from_params("u12", name="value"),
        Field.from_params("pad1"),
        Checksum.from_params(bit_length=8, start=3, end=15, name="crc"),
    ])


def flip_bit(data: Tibs, pos: int) -> Tibs:
    mutable = data.to_mutibs()
    if mutable[pos]:
        mutable.unset(pos)
    else:
        mutable.set(pos)
    return mutable.to_tibs()


def test_xor8_hand_vector():
    # 12 covered bits 110011011110 -> bytes CD and E (right-padded to E0).
    covered = Tibs.from_bin("110011011110")
    assert xor8(covered) == 0xCD ^ 0xE0
    # Whole-byte region is a plain XOR.
    assert xor8(Tibs.from_bytes(b"\x12\x34")) == 0x12 ^ 0x34


def test_pack_matches_hand_vector():
    frame = make_frame()
    packed = frame.pack([0b101, 0xCDE])
    # tag 101 | value 110011011110 | pad 0 | crc 00101101
    assert packed.to_bin() == "101" + "110011011110" + "0" + "00101101"
    assert packed.to_bytes() == b"\xb9\xbc\x2d"


def test_round_trip_preserves_checksum():
    frame = make_frame()
    original = frame.pack([0b101, 0xCDE])

    parsed = make_frame()
    parsed.parse(original)
    assert parsed["tag"].value == 0b101
    assert parsed["value"].value == 0xCDE
    assert parsed["crc"].value == 0x2D

    # Repacking the read values reconstructs the identical packet.
    repacked = parsed.pack(parsed.value)
    assert repacked == original
    assert repacked.to_bytes() == original.to_bytes()


def test_changing_padding_is_ignored():
    frame = make_frame()
    original = frame.pack([0b101, 0xCDE])

    # Bit 15 is the alignment padding bit. Flip 0 -> 1.
    corrupted_padding = flip_bit(original, 15)
    assert corrupted_padding != original

    parsed = make_frame()
    parsed.parse(corrupted_padding)  # must not raise
    assert parsed["value"].value == 0xCDE
    assert parsed["crc"].value == 0x2D
    # The values repack back to the original packet.
    assert parsed.pack(parsed.value) == original


def test_flipping_a_covered_value_bit_fails_checksum():
    frame = make_frame()
    original = frame.pack([0b101, 0xCDE])

    for bit in (3, 7, 14):  # bits inside the covered value field [3:15)
        corrupted = flip_bit(original, bit)
        parsed = make_frame()
        with pytest.raises(ValueError, match="Checksum mismatch"):
            parsed.parse(corrupted)


def test_flipping_a_trailer_bit_fails_checksum():
    frame = make_frame()
    original = frame.pack([0b101, 0xCDE])
    corrupted = flip_bit(original, 20)  # inside the checksum trailer [16:24)
    parsed = make_frame()
    with pytest.raises(ValueError, match="Checksum mismatch"):
        parsed.parse(corrupted)


def test_checksum_needs_checksum_format_to_pack():
    from bitformat import Format

    plain = Format.from_params([
        Field.from_params("u3", name="tag"),
        Field.from_params("u12", name="value"),
        Field.from_params("pad1"),
        Checksum.from_params(bit_length=8, start=3, end=15),
    ])
    with pytest.raises(ValueError, match="ChecksumFormat"):
        plain.pack([0b101, 0xCDE])


def test_invalid_construction_arguments():
    with pytest.raises(ValueError):
        Checksum.from_params(bit_length=0)
    with pytest.raises(ValueError):
        Checksum.from_params(start=-1)
    with pytest.raises(ValueError):
        Checksum.from_params(start=10, end=2)


def test_truncated_trailer_raises():
    frame = make_frame()
    original = frame.pack([0b101, 0xCDE])
    parsed = make_frame()
    with pytest.raises(ValueError, match="checksum trailer"):
        parsed.parse(original[:20])


def test_copy_preserves_checksum_field():
    frame = make_frame()
    copy = frame._copy()
    assert isinstance(copy, ChecksumFormat)
    assert isinstance(copy[-1], Checksum)
    assert copy[-1].checksum_bit_length == 8
    assert copy.pack([0b101, 0xCDE]) == frame.pack([0b101, 0xCDE])


def test_repr_and_pretty_print():
    checksum = Checksum.from_params(bit_length=8, start=3, end=15, name="crc")
    assert "Checksum.from_params" in repr(checksum)
    rendered = str(make_frame())
    assert "checksum8[3:15]" in rendered


def test_fields_after_checksum_keep_value_slots_aligned():
    # The checksum occupies one value slot even though its value is derived.
    frame = ChecksumFormat.from_params([
        Field.from_params("u4", name="hdr"),
        Field.from_params("u8", name="data"),
        Field.from_params("pad4"),
        Checksum.from_params(8, start=4, end=12, name="crc"),
        Field.from_params("u8", name="tail"),
    ])
    packed = frame.pack([0xA, 0x55, 0x00, 0x7E])  # the 0x00 slot is the ignored checksum
    parsed = ChecksumFormat.from_params([
        Field.from_params("u4", name="hdr"),
        Field.from_params("u8", name="data"),
        Field.from_params("pad4"),
        Checksum.from_params(8, start=4, end=12, name="crc"),
        Field.from_params("u8", name="tail"),
    ])
    parsed.parse(packed)
    assert parsed["tail"].value == 0x7E
    assert parsed["crc"].value != 0x00
    assert parsed.pack(parsed.value) == packed


def test_supplied_checksum_value_is_overwritten():
    frame = make_frame()
    correct = frame.pack([0b101, 0xCDE, 0x2D])
    # Passing a bogus value in the checksum slot must not corrupt the trailer.
    tampered = frame.pack([0b101, 0xCDE, 0xFF])
    assert correct == tampered
    parsed = make_frame()
    parsed.parse(tampered)
    assert parsed["crc"].value == 0x2D

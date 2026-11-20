"""Tests for the collector archive frames with bit-interleaved channels."""

import pytest

from bitformat import Format, Tibs

import archive_frames as af


A_VALUE = 0xABC
B_VALUE = 0x123
NEW_A = 0x777


def dual_archive(flag=1, tag=2):
    return af.build_archive_frame(af.MODE_DUAL, tag, A_VALUE, B_VALUE, flag)


class TestDualChannelFrame:
    def test_reads_a_and_b_as_distinct_original_values(self):
        reading = af.read_frame(dual_archive())
        assert reading.mode == af.MODE_DUAL
        assert reading.tag == 2
        assert reading.a == A_VALUE
        assert reading.b == B_VALUE
        assert reading.flag == 1
        assert reading.a != reading.b

    def test_interleave_order_is_a_msb_then_b_lsb(self):
        payload = af.interleave_channels(A_VALUE, B_VALUE)
        assert len(payload) == 24
        # Slot pair 0 starts with A's MSB and B's LSB, each advancing
        # towards the opposite end.
        assert payload[0] == str((A_VALUE >> 11) & 1)
        assert payload[1] == str(B_VALUE & 1)
        assert payload[22] == str(A_VALUE & 1)
        assert payload[23] == str((B_VALUE >> 11) & 1)
        assert af.deinterleave_channels(payload) == (A_VALUE, B_VALUE)

    def test_correcting_a_only_changes_a_slots(self):
        before = dual_archive().to_bin()
        after = af.correct_channel_a(dual_archive(), NEW_A).to_bin()
        assert len(before) == len(after)
        changed = {i for i, (x, y) in enumerate(zip(before, after)) if x != y}
        expected = {slot for slot, bit in zip(af.channel_a_slots(af.MODE_DUAL),
                                              f"{NEW_A:012b}")
                    if before[slot] != bit}
        assert changed == expected
        assert changed <= set(af.channel_a_slots(af.MODE_DUAL))
        # B's slots, the header and the trailing flag keep their bits.
        kept = (af.channel_b_slots(af.MODE_DUAL) + [0, 1, 2]
                + [af.flag_position(af.MODE_DUAL)])
        for slot in kept:
            assert before[slot] == after[slot]

    def test_corrected_frame_rereads_with_b_and_flag_intact(self):
        corrected = af.correct_channel_a(dual_archive(), NEW_A)
        reading = af.read_frame(corrected)
        assert reading.a == NEW_A
        assert reading.b == B_VALUE
        assert reading.flag == 1
        assert reading.tag == 2

    def test_read_and_written_points_map_to_same_slots(self):
        corrected = af.correct_channel_a(dual_archive(), NEW_A)
        payload = corrected.to_bin()[3:27]
        a, b = af.deinterleave_channels(payload)
        assert a == NEW_A
        assert b == B_VALUE
        # The stored string keeps recorder order: it is not a reversal of
        # the original segment.
        original_payload = dual_archive().to_bin()[3:27]
        assert payload != original_payload[::-1]


class TestSingleChannelFrame:
    def test_reads_a_without_inventing_a_fake_b(self):
        archive = af.build_archive_frame(af.MODE_SINGLE, 1, A_VALUE, flag=0)
        reading = af.read_frame(archive)
        assert reading.mode == af.MODE_SINGLE
        assert reading.a == A_VALUE
        assert reading.b is None
        assert reading.flag == 0

    def test_single_frame_has_no_b_slots(self):
        assert af.channel_b_slots(af.MODE_SINGLE) == []
        archive = af.build_archive_frame(af.MODE_SINGLE, 0, A_VALUE)
        assert len(archive) == 3 + 12 + 1

    def test_flag_position_follows_mode(self):
        assert af.flag_position(af.MODE_DUAL) == 27
        assert af.flag_position(af.MODE_SINGLE) == 15
        single = af.build_archive_frame(af.MODE_SINGLE, 0, A_VALUE, flag=1)
        assert single.to_bin()[15] == "1"
        dual = dual_archive(flag=1)
        assert dual.to_bin()[27] == "1"

    def test_correct_a_in_single_mode(self):
        archive = af.build_archive_frame(af.MODE_SINGLE, 3, A_VALUE, flag=1)
        corrected = af.correct_channel_a(archive, NEW_A)
        reading = af.read_frame(corrected)
        assert reading.a == NEW_A
        assert reading.b is None
        assert reading.flag == 1
        assert reading.tag == 3

    def test_same_format_handles_both_modes(self):
        fmt = af.make_frame_format()
        dual = fmt._copy()
        dual.parse(dual_archive())
        single = fmt._copy()
        single.parse(af.build_archive_frame(af.MODE_SINGLE, 0, A_VALUE))
        assert dual.vars["mode"] == af.MODE_DUAL
        assert single.vars["mode"] == af.MODE_SINGLE


class TestAdjacentFieldFormats:
    def test_plain_adjacent_format_still_works(self):
        fmt = Format("(x: u8, y: u8)")
        packed = fmt.pack([5, 250])
        assert packed.to_bin() == "0000010111111010"
        assert fmt.unpack(packed) == [5, 250]

    def test_header_and_flag_use_plain_order(self):
        # mode=1, tag=2 (binary 10) reads back as plain integers.
        archive = af.build_archive_frame(af.MODE_SINGLE, 2, A_VALUE, flag=1)
        assert archive.to_bin()[:3] == "110"
        reading = af.read_frame(archive)
        assert (reading.mode, reading.tag, reading.flag) == (1, 2, 1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

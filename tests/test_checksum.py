import pytest

from bitformat import (Checksum, ChecksumFailure, Field, Format, FrameError, Repeat,
                       Tibs, TruncationError)


def rolling_rule(bits: Tibs) -> int:
    """A caller-supplied checksum rule: rolling 16 bit multiply-add."""
    value = 0
    for i in range(0, len(bits), 8):
        chunk = bits[i:i + 8]
        value = (value * 257 + chunk.unpack(f"u{len(chunk)}")) & 0xFFFF
    return value


def inner_checksum(width: int = 5) -> Checksum:
    return Checksum.from_params(width, layer="inner", rule=rolling_rule, name="inner",
                                start="{6}", end="{6 + 12 * count}")


def outer_checksum(width: int = 7) -> Checksum:
    return Checksum.from_params(width, layer="outer", rule=rolling_rule, name="outer")


def make_frame_format() -> Format:
    padding_length = "{(- (6 + 12 * count + 5)) % 8}"
    return Format.from_params([
        Field.from_params("u3", name="flag"),
        Field.from_params("u3", name="count"),
        Repeat.from_params("{count}", Field.from_params("u12")),
        inner_checksum(),
        Field.from_params(f"pad{padding_length}", name="padding"),
        outer_checksum(),
    ])


def flip(frame: Tibs, position: int) -> Tibs:
    mutable = frame.to_mutibs()
    mutable[position] = 1 - int(mutable[position])
    return mutable.to_tibs()


def parse_frame(frame: Tibs) -> Format:
    fmt = make_frame_format()
    fmt.parse(frame)
    return fmt


@pytest.mark.parametrize("count", [1, 2])
def test_two_lengths_roundtrip_and_independent_layers(count):
    fmt = make_frame_format()
    values = list(range(100, 100 + count))
    frame = fmt.pack([2, count, values])
    # The repeated region begins at bit 6, which is not a byte boundary.
    assert len(fmt[2].to_tibs()) == 12 * count
    assert (6 % 8) != 0
    assert len(frame) == 6 + 12 * count + 5 + (-(6 + 12 * count + 5) % 8) + 7

    parsed = parse_frame(frame)
    assert parsed.value[:3] == [2, count, values]
    inner, outer = parsed.checksums
    assert inner.layer == "inner" and outer.layer == "outer"
    assert inner.ok is True and outer.ok is True
    assert inner.expected == parsed[3].value
    assert outer.expected == parsed[5].value
    # Inner covers the repeated readings only; outer covers the whole frame.
    assert inner.covered == (6 + 12 * count, 6 + 12 * count + 5)
    assert outer.covered == (len(frame) - 7, len(frame))
    assert parsed.to_tibs() == frame


def test_padding_change_only_breaks_outer_layer():
    frame = make_frame_format().pack([2, 2, [100, 2000]])
    padding_start = 6 + 12 * 2 + 5
    changed = flip(frame, padding_start)
    fmt = make_frame_format()
    with pytest.raises(ChecksumFailure) as excinfo:
        fmt.parse(changed)
    results = excinfo.value.results
    assert [result.layer for result in results] == ["inner", "outer"]
    assert results[0].ok is True
    assert results[1].ok is False
    assert results[0].actual == results[0].expected


def test_reading_change_breaks_both_layers():
    frame = make_frame_format().pack([2, 2, [100, 2000]])
    changed = flip(frame, 10)  # first bit of the first 12 bit reading
    fmt = make_frame_format()
    with pytest.raises(ChecksumFailure) as excinfo:
        fmt.parse(changed)
    results = excinfo.value.results
    assert [result.layer for result in results] == ["inner", "outer"]
    assert results[0].ok is False
    assert results[1].ok is False
    assert results[0].expected != results[0].actual


def test_corrected_measurement_rebuilds_both_layers():
    frame = make_frame_format().pack([2, 2, [100, 2000]])
    fmt = parse_frame(frame)
    fmt[2].value = [101, 2000]
    rebuilt = fmt.to_tibs()
    assert len(rebuilt) == len(frame)
    assert rebuilt[6:6 + 12] == Tibs.from_dtype("u12", 101)

    check = parse_frame(rebuilt)
    assert check.value[2] == [101, 2000]
    assert [checksum.ok for checksum in check.checksums] == [True, True]
    assert [result.layer for result in fmt.verify_checksums()] == ["inner", "outer"]
    assert all(result.ok for result in fmt.checksum_results)


def test_untouched_frame_keeps_padding_bits():
    frame = make_frame_format().pack([2, 1, [500]])
    parsed = parse_frame(frame)
    assert parsed.to_tibs() == frame
    padding_start = 6 + 12 + 5
    changed = flip(frame, padding_start)
    fmt = make_frame_format()
    with pytest.raises(ChecksumFailure):
        fmt.parse(changed)
    # The parsed padding field keeps the received bit, even though the frame
    # as a whole failed its outer checksum.
    assert fmt[4].to_tibs() == changed[padding_start:padding_start + 1]
    assert len(fmt[4].to_tibs()) == 1


@pytest.mark.parametrize("cut", [3, 8])
def test_truncation_does_not_consume_next_frame(cut):
    first = make_frame_format().pack([2, 2, [100, 2000]])
    second = make_frame_format().pack([2, 1, [500]])
    combined = Tibs.from_joined([first, second])
    truncated_length = len(first) - cut
    # The following frame is physically present in the buffer. A correct
    # parser must still report the first frame as short.
    buffer = Tibs.from_joined([first[:truncated_length], second])
    fmt = make_frame_format()
    with pytest.raises(TruncationError) as excinfo:
        fmt.parse_frame(buffer, truncated_length)
    error = excinfo.value
    assert error.missing > 0
    assert error.needed > error.available
    assert error.missing_fields
    assert error.position >= truncated_length - error.available


def test_declared_length_keeps_frames_separate():
    first = make_frame_format().pack([2, 2, [100, 2000]])
    second = make_frame_format().pack([2, 1, [500]])
    buffer = Tibs.from_joined([first, second])
    fmt = make_frame_format()
    used = fmt.parse_frame(buffer, len(first))
    assert used == len(first)
    assert fmt.valid_bit_length == len(first)
    assert fmt.value[:3] == [2, 2, [100, 2000]]
    assert [result.ok for result in fmt.checksum_results] == [True, True]
    # Parsing the second one from its own offset gives its own readings.
    other = make_frame_format()
    other.parse_frame(buffer[len(first):], len(second))
    assert other.value[:3] == [2, 1, [500]]


def test_frame_without_checksums_still_reads_and_writes():
    plain = Format("(x: u3, y: u5)")
    plain.parse("0b10101111")
    assert plain.value == [5, 15]
    plain[1].value = 3
    assert plain.to_tibs() == "0b10100011"
    assert plain.checksums == []
    assert plain.checksum_results == []


def test_checksum_results_are_independent_and_descriptive():
    frame = make_frame_format().pack([2, 2, [100, 2000]])
    changed = flip(frame, 6 + 12 * 2 + 5)  # padding only
    fmt = make_frame_format()
    with pytest.raises(ChecksumFailure) as excinfo:
        fmt.parse(changed)
    failure = excinfo.value
    assert failure.ok is False
    assert [result.layer for result in failure.results] == ["inner", "outer"]
    assert failure.by_layer()["inner"].ok is True
    assert failure.by_layer()["outer"].ok is False
    assert "inner" in str(failure) and "outer" in str(failure)


def test_checksum_widths_and_rules_are_caller_supplied():
    inner = Checksum.from_params(4, layer="inner", rule=lambda bits: 9, name="i")
    outer = Checksum.from_params(11, layer="outer", rule=lambda bits: 1300, name="o")
    fmt = Format.from_params([Field.from_params("u4", name="a"), inner,
                              Field.from_params("u4", name="b"), outer])
    frame = fmt.pack([5, 6])
    assert fmt[1].to_tibs() == Tibs.from_dtype("u4", 9)
    assert fmt[3].to_tibs() == Tibs.from_dtype("u11", 1300)
    parsed = Format.from_params([Field.from_params("u4", name="a"),
                                 Checksum.from_params(4, layer="inner",
                                                      rule=lambda bits: 9, name="i"),
                                 Field.from_params("u4", name="b"),
                                 Checksum.from_params(11, layer="outer",
                                                      rule=lambda bits: 1300, name="o")])
    parsed.parse(frame)
    assert [result.ok for result in parsed.checksum_results] == [True, True]


def test_checksum_range_stretches_with_actual_count():
    frames = []
    for count in (1, 3):
        fmt = make_frame_format()
        frame = fmt.pack([0, count, list(range(count))])
        frames.append(frame)
        parsed = parse_frame(frame)
        assert parsed.checksums[0].covered[0] == 6 + 12 * count
    assert len(frames[0]) != len(frames[1])


def test_frame_error_reports_unconsumed_valid_bits():
    fmt = make_frame_format()
    frame = fmt.pack([2, 1, [7]])
    with pytest.raises(FrameError):
        make_frame_format().parse_frame(Tibs.from_joined([frame, Tibs.from_zeros(8)]),
                                        len(frame) + 8)


def test_one_format_reused_for_two_frame_lengths():
    fmt = make_frame_format()
    short = fmt.pack([2, 1, [111]])
    long_ = fmt.pack([2, 2, [222, 333]])
    assert len(long_) > len(short)
    assert fmt.parse(short) == len(short)
    assert [result.ok for result in fmt.checksum_results] == [True, True]
    assert fmt.parse(long_) == len(long_)
    assert fmt.value[:3] == [2, 2, [222, 333]]
    assert [result.ok for result in fmt.checksum_results] == [True, True]
    assert fmt.to_tibs() == long_


def test_failed_parse_does_not_leak_into_next_frame():
    fmt = make_frame_format()
    good = fmt.pack([2, 2, [222, 333]])
    small = fmt.pack([2, 1, [111]])
    with pytest.raises(ChecksumFailure):
        fmt.parse(flip(good, 6))
    assert fmt.parse(small) == len(small)
    assert fmt.value[:3] == [2, 1, [111]]
    assert [result.ok for result in fmt.checksum_results] == [True, True]


def test_declared_length_ignores_following_bits():
    fmt = make_frame_format()
    frame = fmt.pack([2, 1, [7]])
    marker = Tibs.from_ones(20)
    buffer = Tibs.from_joined([frame, marker])
    parsed = make_frame_format()
    parsed.parse(buffer, valid_bit_length=len(frame))
    assert parsed.valid_bit_length == len(frame)
    assert parsed.value[:3] == [2, 1, [7]]
    assert [result.ok for result in parsed.checksum_results] == [True, True]


def test_long_declared_length_is_truncation():
    fmt = make_frame_format()
    frame = fmt.pack([2, 1, [7]])
    with pytest.raises(TruncationError) as excinfo:
        make_frame_format().parse(frame, valid_bit_length=len(frame) + 5)
    assert excinfo.value.needed == len(frame) + 5
    assert excinfo.value.available == len(frame)

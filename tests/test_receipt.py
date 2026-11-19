import pytest
from bitformat import Format, Receipt, Tibs


def device_frame() -> Format:
    return Format.from_string("""(
    sync: const u8 = 0xA5,
    flags: u4,
    if {flags & 1}: (
        temp: i12
    ),
    count: u4,
    repeat {count}: (
        channel: u4,
        if {channel == 0}: (
            mode: u4
        )
        reading: u8
    )
)""")


# sync = 0xA5, flags = 0b0101 (temp present), temp = -25, count = 2
# item 0: channel 0 (mode present), mode 3, reading 100
# item 1: channel 7 (no mode), reading 200
FRAME_BITS = Tibs('0xA55FE7203647C8')


def test_receipt_spans_match_parse():
    frame = device_frame()
    receipt = frame.parse_with_receipt(FRAME_BITS)
    assert isinstance(receipt, Receipt)
    assert receipt.start == 0
    assert receipt.end == len(FRAME_BITS) == 56
    # The receipt values match the values from a normal parse of the same data.
    frame2 = device_frame()
    frame2.parse(FRAME_BITS)
    assert frame2.value == receipt.value
    named_values = [node.value for node in receipt.walk() if node.name]
    assert named_values == [0xA5, 0b0101, -25, 2, 0, 3, 100, 7, 200]


def test_receipt_hierarchy_and_positions():
    frame = device_frame()
    receipt = frame.parse_with_receipt(FRAME_BITS)
    by_name = {}
    for node in receipt.walk():
        if node.name:
            by_name.setdefault(node.name, []).append(node)
    assert (by_name['sync'][0].start, by_name['sync'][0].end) == (0, 8)
    assert (by_name['flags'][0].start, by_name['flags'][0].end) == (8, 12)
    assert (by_name['temp'][0].start, by_name['temp'][0].end) == (12, 24)
    assert (by_name['count'][0].start, by_name['count'][0].end) == (24, 28)
    # Nested repeat: one entry per iteration, each with its own real span.
    assert [(c.start, c.end) for c in by_name['channel']] == [(28, 32), (44, 48)]
    assert [(m.start, m.end) for m in by_name['mode']] == [(32, 36)]
    assert [(r.start, r.end) for r in by_name['reading']] == [(36, 44), (48, 56)]


def test_receipt_extract_round_trip():
    frame = device_frame()
    receipt = frame.parse_with_receipt(FRAME_BITS)
    # Slicing the original input with the receipt positions gives back the recorded values.
    temps = [n for n in receipt.walk() if n.name == 'temp']
    assert len(temps) == 1
    assert temps[0].value == -25
    assert temps[0].fieldtype.unpack(temps[0].extract(FRAME_BITS)) == -25
    flags = [n for n in receipt.walk() if n.name == 'flags']
    assert flags[0].value == 0b0101
    assert flags[0].fieldtype.unpack(flags[0].extract(FRAME_BITS)) == 0b0101
    readings = [n for n in receipt.walk() if n.name == 'reading']
    assert [r.fieldtype.unpack(r.extract(FRAME_BITS)) for r in readings] == [100, 200]


def test_untaken_branch_has_no_span():
    frame = device_frame()
    # flags with bit 0 clear -> no temp field is parsed. count = 0 -> no repeat items.
    b = Tibs('0xA540')
    receipt = frame.parse_with_receipt(b)
    names = [n.name for n in receipt.walk()]
    assert 'temp' not in names
    assert [name for name in names if name] == ['sync', 'flags', 'count']
    assert receipt.end == len(b) == 16


def test_repeat_iteration_nodes_are_distinct():
    frame = device_frame()
    receipt = frame.parse_with_receipt(FRAME_BITS)
    repeat_node = next(n for n in receipt.walk() if type(n.fieldtype).__name__ == 'Repeat')
    assert len(repeat_node.children) == 2
    first, second = repeat_node.children
    assert (first.start, first.end) == (28, 44)
    assert (second.start, second.end) == (44, 56)
    # Each iteration's reading has its own captured value.
    first_reading = next(n for n in first.walk() if n.name == 'reading')
    second_reading = next(n for n in second.walk() if n.name == 'reading')
    assert first_reading.value == 100
    assert second_reading.value == 200


def test_receipt_parse_failure_raises():
    frame = device_frame()
    with pytest.raises(ValueError):
        frame.parse_with_receipt('0b11')


def test_receipt_str_shows_hierarchy():
    frame = device_frame()
    receipt = frame.parse_with_receipt(FRAME_BITS)
    s = str(receipt)
    assert "Field 'temp' [12:24] = -25" in s
    assert "Field 'flags' [8:12] = 5" in s

import json
from dataclasses import dataclass

import pytest

from mashumaro.codecs.json import JSONDecoder
from mashumaro.codecs.jsonl import (
    JSONLRecord,
    JSONLRejectedLine,
    JSONLStreamReader,
    stream_jsonl,
)
from mashumaro.exceptions import InvalidFieldValue


@dataclass
class Event:
    id: int
    name: str


def build_payload() -> bytes:
    # Physical lines:
    # 1: valid record containing multi-byte UTF-8
    # 2: empty line
    # 3: incomplete JSON (parse error)
    # 4: valid JSON with a field the model cannot parse (model error)
    # 5: whitespace-only line
    # 6: valid record
    # 7: valid record without a terminating line ending
    lines = [
        '{"id": 1, "name": "中文"}',
        "",
        '{"id": 2, "name": "断"',
        '{"id": "oops", "name": "x"}',
        "   ",
        '{"id": 3, "name": "行"}',
        '{"id": 4, "name": "尾"}',
    ]
    return ("\r\n".join(lines[:-1]) + "\r\n" + lines[-1]).encode("utf-8")


def chunks(data, size):
    for index in range(0, len(data), size):
        yield data[index : index + size]


def first_line(data):
    return data[: data.find(b"\n") - 1]


def reference_events(data, decoder):
    """Line-by-line ground truth computed independently from the streamer."""
    events = []
    position = 0
    line_number = 0
    while position < len(data):
        newline = data.find(b"\n", position)
        if newline == -1:
            line_end = len(data)
            raw_line = data[position:line_end]
            content = raw_line
        else:
            line_end = newline + 1
            raw_line = data[position:line_end]
            if raw_line.endswith(b"\r\n"):
                content = raw_line[:-2]
            else:
                content = raw_line[:-1]
        line_number += 1
        start, end = position, position + len(content)
        if content.strip():
            try:
                value = decoder.decode(content)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                events.append(
                    ("parse", line_number, start, end, content, None, error)
                )
            except Exception as error:
                events.append(
                    ("model", line_number, start, end, content, None, error)
                )
            else:
                events.append(
                    ("record", line_number, start, end, content, value, None)
                )
        position = line_end
    return events


def expected_keys(data, decoder):
    return [reference_key(item) for item in reference_events(data, decoder)]


def event_key(event):
    if isinstance(event, JSONLRecord):
        return (
            "record",
            event.line_number,
            event.start,
            event.end,
            event.value,
            None,
        )
    return (
        event.error_kind,
        event.line_number,
        event.start,
        event.end,
        None,
        (type(event.error), str(event.error)),
    )


def reference_key(item):
    kind, line_number, start, end, raw, value, error = item
    return (
        kind,
        line_number,
        start,
        end,
        value,
        None if error is None else (type(error), str(error)),
    )


@pytest.mark.parametrize("chunk_size", list(range(1, 24)) + [31, 50, 128])
def test_chunking_does_not_change_events(chunk_size):
    data = build_payload()
    decoder = JSONDecoder(Event)
    expected = expected_keys(data, decoder)

    streamed = list(stream_jsonl(chunks(data, chunk_size), decoder))
    assert [event_key(event) for event in streamed] == expected


def test_event_shapes_and_error_kinds():
    data = build_payload()
    decoder = JSONDecoder(Event)
    events = list(stream_jsonl(chunks(data, 7), decoder))

    assert [type(event).__name__ for event in events] == [
        "JSONLRecord",
        "JSONLRejectedLine",
        "JSONLRejectedLine",
        "JSONLRecord",
        "JSONLRecord",
    ]

    first = events[0]
    assert isinstance(first, JSONLRecord)
    assert first.line_number == 1
    assert first.start == 0
    assert first.value == Event(id=1, name="中文")
    # The span excludes the CRLF ending and stops before the next line.
    assert data[first.start : first.end] == first_line(data)
    assert data[first.end : first.end + 2] == b"\r\n"

    bad_json = events[1]
    assert isinstance(bad_json, JSONLRejectedLine)
    assert bad_json.line_number == 3
    assert bad_json.error_kind == "parse"
    assert isinstance(bad_json.error, json.JSONDecodeError)
    assert data[bad_json.start : bad_json.end] == bad_json.raw
    assert b"\n" not in bad_json.raw

    bad_model = events[2]
    assert isinstance(bad_model, JSONLRejectedLine)
    assert bad_model.line_number == 4
    assert bad_model.error_kind == "model"
    assert isinstance(bad_model.error, InvalidFieldValue)
    # The rejected line owns only its own bytes; the next line still parses.
    assert data[bad_model.end : bad_model.end + 2] == b"\r\n"

    last = events[-1]
    assert isinstance(last, JSONLRecord)
    assert last.line_number == 7
    assert last.value == Event(id=4, name="尾")
    # A final record without a terminating newline is still delivered.
    assert last.end == len(data)


def test_blank_lines_keep_physical_line_numbers():
    data = build_payload()
    decoder = JSONDecoder(Event)
    events = list(stream_jsonl(chunks(data, 5), decoder))
    line_numbers = [event.line_number for event in events]
    # Empty line 2 and whitespace-only line 5 occupy numbers but no events.
    assert line_numbers == [1, 3, 4, 6, 7]


def test_offsets_reconstruct_original_bytes_for_every_chunk_size():
    data = build_payload()
    decoder = JSONDecoder(Event)
    for size in range(1, len(data) + 1):
        for event in stream_jsonl(chunks(data, size), decoder):
            sliced = data[event.start : event.end]
            assert event.end <= len(data)
            if isinstance(event, JSONLRejectedLine):
                assert sliced == event.raw
            else:
                assert json.loads(sliced) == {
                    "id": event.value.id,
                    "name": event.value.name,
                }


def test_cancel_after_two_events_then_replay_from_confirmed_offset():
    data = build_payload()
    decoder = JSONDecoder(Event)
    pulled_bytes = 0

    def source():
        nonlocal pulled_bytes
        for chunk in chunks(data, 40):
            pulled_bytes += len(chunk)
            yield chunk

    iterator = source()
    reader = JSONLStreamReader(decoder)
    confirmed = []
    for chunk in iterator:
        reader.feed(chunk)
        for event in reader.events():
            confirmed.append(event)
            if len(confirmed) == 2:
                break
        if len(confirmed) == 2:
            break

    # Cancellation must not drain the input to its end, and fetched chunks
    # already hold lines that have not been delivered.
    assert pulled_bytes < len(data)
    assert pulled_bytes > reader.position
    assert reader.position < len(data)

    resume_at = reader.position
    resume_line = reader.line_number
    # Re-chunk the remainder differently; only the confirmed position and
    # line number are carried over, never the pulled chunk count.
    replayed = list(
        stream_jsonl(
            chunks(data[resume_at:], 11),
            decoder,
            start_offset=resume_at,
            start_line=resume_line,
        )
    )

    full_run = list(stream_jsonl(chunks(data, 17), decoder))
    assert [event_key(event) for event in confirmed + replayed] == [
        event_key(event) for event in full_run
    ]

    expected = expected_keys(data, decoder)
    assert [event_key(event) for event in confirmed + replayed] == expected
    # The replay continues exactly after the confirmed line, not at a chunk
    # boundary, so no line is replayed or skipped.
    assert replayed[0].line_number == confirmed[-1].line_number + 1


def test_cancel_keeps_undelivered_buffered_lines_replayable():
    data = build_payload()
    decoder = JSONDecoder(Event)
    reader = JSONLStreamReader(decoder)
    reader.feed(data[:60])
    delivered = list(reader.events())
    # Fetched bytes include an undelivered partial line beyond the position.
    assert 0 < reader.position < 60
    remainder = list(
        stream_jsonl(
            chunks(data[reader.position :], 3),
            decoder,
            start_offset=reader.position,
            start_line=reader.line_number,
        )
    )
    full = list(stream_jsonl(chunks(data, 9), decoder))
    assert [event_key(event) for event in delivered + remainder] == [
        event_key(event) for event in full
    ]


def test_split_inside_crlf_and_multibyte_character():
    data = build_payload()
    crlf = data.find(b"\r\n")
    char_pos = data.find("中".encode("utf-8"))
    boundaries = sorted(
        {
            crlf + 1,  # between CR and LF
            char_pos + 1,  # inside the three-byte UTF-8 sequence
            char_pos + 2,
        }
    )
    pieces = []
    previous = 0
    for boundary in boundaries:
        pieces.append(data[previous:boundary])
        previous = boundary
    pieces.append(data[previous:])

    decoder = JSONDecoder(Event)
    reader = JSONLStreamReader(decoder)
    reader.feed(pieces[0])
    # The first fragment ends inside a multi-byte char, so no line completes.
    assert list(reader.events()) == []
    for piece in pieces[1:]:
        reader.feed(piece)
    reader.close()
    events = list(reader.events())
    expected = expected_keys(data, decoder)
    assert [event_key(event) for event in events] == expected


def test_partial_feed_emits_nothing_until_line_completes():
    decoder = JSONDecoder(Event)
    reader = JSONLStreamReader(decoder)
    line = '{"id": 9, "name": "甲"}'.encode("utf-8")
    reader.feed(line[:5])
    assert list(reader.events()) == []
    assert reader.position == 0
    reader.feed(line[5:] + b"\r\n")
    [event] = list(reader.events())
    assert isinstance(event, JSONLRecord)
    assert event.value == Event(id=9, name="甲")
    assert reader.position == len(line) + 2


def test_trailing_newline_emits_no_phantom_event():
    decoder = JSONDecoder(Event)
    with_newline = b'{"id": 1, "name": "x"}\n'
    without_newline = b'{"id": 1, "name": "x"}'
    a = list(stream_jsonl(iter([with_newline]), decoder))
    b = list(stream_jsonl(iter([without_newline]), decoder))
    assert len(a) == len(b) == 1
    assert a[0].end == b[0].end == len(without_newline)


def test_invalid_utf8_is_a_parse_error():
    decoder = JSONDecoder(Event)
    events = list(stream_jsonl(iter([b'{"id": 1}\xff\n']), decoder))
    [event] = events
    assert isinstance(event, JSONLRejectedLine)
    assert event.error_kind == "parse"
    assert isinstance(event.error, UnicodeDecodeError)


def test_stream_jsonl_accepts_type_decoder_and_callable():
    data = b'{"id": 1, "name": "x"}\n{"id": 2, "name": "y"}\n'
    from_type = list(stream_jsonl(chunks(data, 10), Event))
    decoder = JSONDecoder(Event)
    from_decoder = list(stream_jsonl(chunks(data, 10), decoder))
    from_callable = list(stream_jsonl(chunks(data, 10), decoder.decode))
    assert all(isinstance(event, JSONLRecord) for event in from_type)
    assert [event.value for event in from_type] == [
        Event(1, "x"),
        Event(2, "y"),
    ]
    assert [event.value for event in from_decoder] == [
        event.value for event in from_callable
    ]


def test_accepts_bytearray_and_memoryview_chunks():
    data = build_payload()
    decoder = JSONDecoder(Event)
    views = iter([bytearray(data[:20]), memoryview(data[20:])])
    events = list(stream_jsonl(views, decoder))
    expected = expected_keys(data, decoder)
    assert [event_key(event) for event in events] == expected


def test_feed_after_close_is_rejected():
    reader = JSONLStreamReader(JSONDecoder(Event))
    reader.close()
    with pytest.raises(RuntimeError):
        reader.feed(b"{}")


def test_single_line_json_decoder_still_works():
    decoder = JSONDecoder(Event)
    assert decoder.decode(b'{"id": 1, "name": "x"}') == Event(1, "x")
    assert decoder.decode('{"id": 2, "name": "y"}') == Event(2, "y")


def test_empty_input_yields_no_events():
    decoder = JSONDecoder(Event)
    assert list(stream_jsonl(iter([]), decoder)) == []
    reader = JSONLStreamReader(decoder)
    reader.close()
    assert list(reader.events()) == []
    assert reader.position == 0
    assert reader.line_number == 0


def test_blank_only_input_skips_all_lines_but_advances_position():
    decoder = JSONDecoder(Event)
    reader = JSONLStreamReader(decoder)
    reader.feed(b"\n\r\n   \n")
    assert list(reader.events()) == []
    assert reader.line_number == 3
    assert reader.position == len(b"\n\r\n   \n")


def test_skip_rules_are_stable_across_chunk_sizes():
    data = b'\n{"id": 1, "name": "x"}\n  \n'
    decoder = JSONDecoder(Event)
    reference = [
        event_key(event)
        for event in stream_jsonl(chunks(data, len(data)), decoder)
    ]
    for size in range(1, len(data) + 1):
        assert [
            event_key(event)
            for event in stream_jsonl(chunks(data, size), decoder)
        ] == reference


def test_start_offset_shifts_absolute_positions():
    data = build_payload()
    decoder = JSONDecoder(Event)
    shift = 100
    events = list(stream_jsonl(chunks(data, 13), decoder, start_offset=shift))
    baseline = list(stream_jsonl(chunks(data, 13), decoder))
    assert [
        (event.line_number, event.start + shift, event.end + shift)
        for event in baseline
    ] == [(event.line_number, event.start, event.end) for event in events]
    for event in events:
        sliced = data[event.start - shift : event.end - shift]
        if isinstance(event, JSONLRejectedLine):
            assert sliced == event.raw

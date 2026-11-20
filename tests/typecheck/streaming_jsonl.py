from typing import Iterator

from typing_extensions import assert_type

from mashumaro.codecs.json import JSONDecoder
from mashumaro.codecs.jsonl import (
    JSONLEvent,
    JSONLRecord,
    JSONLRejectedLine,
    JSONLStreamReader,
    stream_jsonl,
)


class Event:
    id: int
    name: str


def check_stream_reader_types() -> None:
    decoder = JSONDecoder(Event)
    reader = JSONLStreamReader(decoder)
    assert_type(reader, JSONLStreamReader[Event])
    assert_type(reader.position, int)
    assert_type(reader.line_number, int)
    reader.feed(b"{}")
    reader.feed(bytearray(b"{}"))
    reader.feed(memoryview(b"{}"))
    reader.close()

    events = list(reader.events())
    assert_type(events, list[JSONLEvent[Event]])
    for event in events:
        if isinstance(event, JSONLRecord):
            assert_type(event, JSONLRecord[Event])
            assert_type(event.value, Event)
        else:
            assert_type(event, JSONLRejectedLine)
            assert_type(event.error_kind, str)
            assert_type(event.error, Exception)
            assert_type(event.raw, bytes)


def check_stream_jsonl_types() -> None:
    chunks: Iterator[bytes] = iter([])

    from_type = list(stream_jsonl(chunks, Event))
    assert_type(from_type, list[JSONLEvent[Event]])

    decoder = JSONDecoder(Event)
    from_decoder = list(
        stream_jsonl(chunks, decoder, start_offset=10, start_line=2)
    )
    assert_type(from_decoder, list[JSONLEvent[Event]])

    from_callable = list(stream_jsonl(chunks, decoder.decode))
    assert_type(from_callable, list[JSONLEvent[Event]])

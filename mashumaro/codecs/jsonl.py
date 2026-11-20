"""Streaming JSON Lines reader built on top of a reusable model codec.

The reader consumes JSON Lines data in arbitrary byte chunks, which makes it
suitable for very large daily payloads that cannot be loaded into memory at
once. Chunks are allowed to split multi-byte UTF-8 characters and line
endings: incomplete code points stay buffered until the rest arrives, so a
chunk boundary never corrupts a record.

Each fully received line is decoded with the wrapped model codec (for
example :class:`mashumaro.codecs.json.JSONDecoder`). Valid records and
rejected lines are both reported as events carrying the 1-based physical
line number and the exact byte span of the original line (excluding its line
ending), so a caller that keeps the raw file can seek back to any rejected
line and replay it later.

Resume after cancellation relies solely on the byte position exposed by the
last confirmed event (see :attr:`JSONLStreamReader.position`), never on the
number of chunks already fetched. See :func:`stream_jsonl` and
:class:`JSONLStreamReader`.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from typing_extensions import TypeForm

from mashumaro.codecs.json import JSONDecoder
from mashumaro.dialect import Dialect

T = TypeVar("T")

# Anything that turns the bytes of a single line into a model instance.
LineDecoder = Callable[[bytes], T]

_LF = b"\n"
_CR = b"\r"

# Failures raised by the JSON parser itself, as opposed to failures raised
# while unpacking parsed data into the target model.
_PARSE_ERRORS = (json.JSONDecodeError, UnicodeDecodeError)


@dataclass(frozen=True, slots=True)
class JSONLRecord(Generic[T]):
    """A line that was parsed and decoded into a model instance."""

    line_number: int
    start: int
    end: int
    value: T


@dataclass(frozen=True, slots=True)
class JSONLRejectedLine:
    """A line whose content could not be turned into a model instance.

    ``error_kind`` is ``"parse"`` when the line is not valid JSON (including
    an incomplete trailing line or a truncated UTF-8 sequence) and
    ``"model"`` when the JSON is valid but cannot be decoded into the target
    type. The original exception is kept in ``error`` and the byte span
    covers the whole original line without borrowing bytes from the next
    line.
    """

    line_number: int
    start: int
    end: int
    error_kind: str
    error: Exception
    raw: bytes


JSONLEvent = JSONLRecord[T] | JSONLRejectedLine


class JSONLStreamReader(Generic[T]):
    """Incrementally decode JSON Lines fed as byte chunks.

    Feed chunks with :meth:`feed` and iterate the resulting events with
    :meth:`events`. Every event exposes absolute byte offsets relative to
    the logical stream start, which can be shifted with ``start_offset`` when
    a reader resumes a file mid-stream.

    A single-line :class:`~mashumaro.codecs.json.JSONDecoder` keeps working
    unchanged: this class only adds line framing, offset bookkeeping and the
    distinction between parse and model errors.
    """

    def __init__(
        self,
        decoder: JSONDecoder[T] | LineDecoder[T],
        *,
        start_offset: int = 0,
        start_line: int = 0,
    ):
        decode = getattr(decoder, "decode", None)
        self._decode: LineDecoder[T] = (
            decode if callable(decode) else cast(LineDecoder[T], decoder)
        )
        self._buffer = b""
        self._line_number = start_line
        self._line_start = start_offset
        self._closed = False

    @property
    def position(self) -> int:
        """Byte offset immediately after the last confirmed line ending.

        This is the only value needed to resume reading after a
        cancellation: pass it as ``start_offset`` of a new reader. It never
        advances for bytes that merely sit in fetched but undelivered
        chunks.
        """
        return self._line_start

    @property
    def line_number(self) -> int:
        """Physical line number of the last confirmed line.

        Pair this with :attr:`position` when resuming so the replacement
        reader continues numbering instead of restarting at one. Both values
        come from confirmed events, never from the amount of buffered data.
        """
        return self._line_number

    def feed(self, chunk: bytes | bytearray | memoryview) -> None:
        """Add a chunk of raw JSON Lines bytes.

        The chunk may end in the middle of a UTF-8 character or a CRLF line
        ending. Such fragments are simply buffered: line splitting looks for
        LF bytes, and 0x0A can never appear inside a multi-byte UTF-8
        sequence, so framing stays correct while the fragment is completed
        by a later chunk.
        """
        if self._closed:
            raise RuntimeError("Cannot feed a closed JSON Lines reader")
        self._buffer += bytes(chunk)

    def close(self) -> None:
        """Mark the stream finished so the final line can be emitted."""
        self._closed = True

    def events(self) -> Iterator[JSONLEvent[T]]:
        """Yield events for every line that can currently be completed.

        After :meth:`close` the final line is yielded even when it is not
        terminated by a line ending. Empty and whitespace-only lines do not
        produce events, but they still occupy a physical line number, which
        keeps line numbers identical to line-by-line reading of the raw
        file regardless of chunk boundaries.
        """
        while True:
            line = self._next_line()
            if line is None:
                return
            content, span_length = line
            line_number = self._line_number + 1
            start = self._line_start
            content_end = start + len(content)
            self._line_number = line_number
            self._line_start = start + span_length
            if not content.strip():
                continue
            event: JSONLEvent[T]
            try:
                value = self._decode(content)
            except _PARSE_ERRORS as error:
                event = JSONLRejectedLine(
                    line_number=line_number,
                    start=start,
                    end=content_end,
                    error_kind="parse",
                    error=error,
                    raw=content,
                )
            except Exception as error:
                event = JSONLRejectedLine(
                    line_number=line_number,
                    start=start,
                    end=content_end,
                    error_kind="model",
                    error=error,
                    raw=content,
                )
            else:
                event = JSONLRecord(
                    line_number=line_number,
                    start=start,
                    end=content_end,
                    value=value,
                )
            yield event

    def _next_line(self) -> tuple[bytes, int] | None:
        """Return ``(content, span_length)`` for the next completed line.

        ``span_length`` counts the content bytes plus its line ending (one
        byte for LF, two for CRLF), so advancing by it moves the absolute
        stream position across exactly one original line.
        """
        newline_index = self._buffer.find(_LF)
        if newline_index != -1:
            line = self._buffer[:newline_index]
            if line.endswith(_CR):
                content = line[:-1]
            else:
                content = line
            span_length = newline_index + 1
            self._buffer = self._buffer[span_length:]
            return content, span_length

        if self._closed:
            if not self._buffer:
                return None
            content = self._buffer
            self._buffer = b""
            return content, len(content)

        return None


def stream_jsonl(
    chunks: Iterator[bytes | bytearray | memoryview],
    shape_type: TypeForm[T] | JSONDecoder[T] | LineDecoder[T],
    *,
    default_dialect: type[Dialect] | None = None,
    start_offset: int = 0,
    start_line: int = 0,
) -> Iterator[JSONLEvent[T]]:
    """Stream JSON Lines events from an iterator of byte chunks.

    ``shape_type`` is usually a model type decoded with a standard
    :class:`JSONDecoder`; an existing decoder (or any callable turning a
    line into a model instance) can be passed instead to reuse custom codec
    settings.

    ``start_offset`` is the absolute byte offset the chunks start at. It
    normally comes from :attr:`JSONLStreamReader.position` right after the
    last confirmed event of a previous run, which makes replay independent
    of how the input was chunked before the cancellation. ``start_line``
    likewise continues physical line numbering from the last confirmed
    event.
    """
    decoder = _coerce_decoder(shape_type, default_dialect)
    reader = JSONLStreamReader(
        decoder, start_offset=start_offset, start_line=start_line
    )
    for chunk in chunks:
        reader.feed(chunk)
        yield from reader.events()
    reader.close()
    yield from reader.events()


def _coerce_decoder(
    shape_type: TypeForm[T] | JSONDecoder[T] | LineDecoder[T],
    default_dialect: type[Dialect] | None,
) -> JSONDecoder[T] | LineDecoder[T]:
    if isinstance(shape_type, JSONDecoder):
        return shape_type
    if callable(shape_type) and not isinstance(shape_type, type):
        return cast(LineDecoder[T], shape_type)
    return JSONDecoder(
        cast(TypeForm[T], shape_type),
        default_dialect=default_dialect,
    )


__all__ = [
    "JSONLRecord",
    "JSONLRejectedLine",
    "JSONLStreamReader",
    "JSONLEvent",
    "stream_jsonl",
]

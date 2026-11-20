"""What a client can still learn from the bytes it received.

The scan below never trusts a final unterminated line. Python's csv.reader
will happily return ``3,partial`` as a record, so completeness is decided
first, by walking the bytes with quote state and only accepting data up to
the last record terminator outside quotes.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass

from .httpwire import WireResponse


def split_complete_records(text: str) -> tuple[list[str], str, str | None]:
    """Split CSV text into complete records plus an unterminated residual.

    Returns (records, residual, error). Each returned record excludes its
    terminator. A quote opened but not closed means the remainder is residual,
    not a record. Accepts CRLF (Datasette's excel dialect) and bare LF.
    """
    records: list[str] = []
    start = 0
    index = 0
    in_quotes = False
    length = len(text)
    while index < length:
        char = text[index]
        if in_quotes:
            if char == '"':
                if index + 1 < length and text[index + 1] == '"':
                    index += 2
                    continue
                in_quotes = False
            index += 1
            continue
        if char == '"':
            in_quotes = True
            index += 1
            continue
        if char == "\r":
            if index + 1 < length and text[index + 1] == "\n":
                records.append(text[start:index])
                index += 2
                start = index
                continue
            # A CR not followed by LF: treat as a terminator only if more
            # data follows; otherwise it is a truncated CRLF.
            if index + 1 < length:
                records.append(text[start:index])
                index += 1
                start = index
                continue
            break
        if char == "\n":
            records.append(text[start:index])
            index += 1
            start = index
            continue
        index += 1
    residual = text[start:]
    error = None
    if in_quotes:
        error = "unterminated quoted field at end of received bytes"
    return records, residual, error


def _parse_record(record: str) -> list[str]:
    return next(csv.reader(io.StringIO(record, newline="")))


@dataclass
class CsvParse:
    records: list[list[str]]
    residual: str
    error: str | None

    @property
    def header(self) -> list[str] | None:
        return self.records[0] if self.records else None

    @property
    def data_rows(self) -> list[list[str]]:
        return self.records[1:] if self.records else []


def parse_csv(body: bytes) -> CsvParse:
    text = body.decode("utf-8", "surrogateescape")
    raw_records, residual, error = split_complete_records(text)
    records: list[list[str]] = []
    for raw in raw_records:
        try:
            records.append(_parse_record(raw))
        except csv.Error as ex:
            return CsvParse(records, residual, f"{type(ex).__name__}: {ex}")
    return CsvParse(records, residual, error)


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass
class Report:
    scenario: str
    title: str
    path: str
    wire: WireResponse
    parse: CsvParse
    record_bytes: int
    tail_is_partial: bool

    @property
    def record_count(self) -> int:
        return len(self.parse.data_rows)

    @property
    def ends_on_record_boundary(self) -> bool:
        return not self.tail_is_partial

    def as_dict(self) -> dict:
        w = self.wire
        return {
            "scenario": self.scenario,
            "title": self.title,
            "path": self.path,
            "http": {
                "status": w.status,
                "content_type": w.header_dict.get("content-type"),
                "content_length": w.header_dict.get("content-length"),
                "transfer_encoding": w.header_dict.get("transfer-encoding"),
                "link": w.header_dict.get("link"),
                "content_disposition": w.header_dict.get("content-disposition"),
                "response_started": w.response_started,
            },
            "transport": {
                "client_saw_complete_http_message": w.complete,
                "client_error": w.client_error,
                "server_closed_without_final_chunk": (
                    w.transport_closed_without_terminal_chunk
                ),
                "wire_events": w.client_events,
                "wire_chunks": len(w.body_chunks),
                "chunk_sizes": [len(c) for c in w.body_chunks],
            },
            "csv": {
                "complete_records_including_header": len(self.parse.records),
                "data_rows": self.record_count,
                "ends_on_record_boundary": self.ends_on_record_boundary,
                "residual_bytes": len(
                    self.parse.residual.encode("utf-8", "surrogateescape")
                ),
                "residual_preview": self.parse.residual[:60],
                "parse_error": self.parse.error,
                "sha256_of_all_body_bytes": body_sha256(w.body),
                "sha256_of_complete_record_bytes": body_sha256(
                    w.body[: self.record_bytes]
                ),
            },
        }


def diagnose(scenario, wire: WireResponse) -> Report:
    parsed = parse_csv(wire.body)
    rebuilt = io.StringIO(newline="")
    writer = csv.writer(rebuilt)
    writer.writerows(parsed.records)
    record_bytes = len(rebuilt.getvalue().encode("utf-8", "surrogateescape"))
    return Report(
        scenario=scenario.key,
        title=scenario.title,
        path=scenario.path,
        wire=wire,
        parse=parsed,
        record_bytes=record_bytes,
        tail_is_partial=bool(parsed.residual),
    )

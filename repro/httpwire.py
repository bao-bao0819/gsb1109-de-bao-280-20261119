"""Capture what uvicorn's real HTTP/1.1 implementation writes.

The sandbox used to prepare this repro does not allow binding a local socket,
so instead of faking HTTP ourselves we instantiate the exact protocol class
uvicorn uses (``H11Protocol``), hand it a recording transport, and feed it the
request bytes a client would send. Everything asserted below - status line,
header order, chunk framing, terminal zero chunk - is produced by uvicorn and
h11, not by this module.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import h11
from uvicorn.config import Config
from uvicorn.server import ServerState


class RecordingTransport(asyncio.Transport):
    """The smallest transport uvicorn's protocol accepts, recording writes."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[float, bytes]] = []
        self.closed = False
        self._paused = False
        self._closing = False

    def write(self, data: bytes) -> None:  # type: ignore[override]
        self.writes.append((time.monotonic(), bytes(data)))

    def close(self) -> None:  # type: ignore[override]
        self.closed = True
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing or self.closed

    def pause_reading(self) -> None:
        self._paused = True

    def resume_reading(self) -> None:
        self._paused = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "sockname":
            return ("127.0.0.1", 8000)
        if name == "peername":
            return ("127.0.0.1", 54321)
        return default


@dataclass
class WireResponse:
    path: str
    status: int | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    body_chunks: list[bytes] = field(default_factory=list)
    raw: bytes = b""
    # Client-side h11 view of the same bytes
    complete: bool = False
    client_error: str | None = None
    client_events: list[str] = field(default_factory=list)
    # Server-side transport facts
    response_started: bool = False
    transport_closed_without_terminal_chunk: bool = False
    writes: list[tuple[float, int]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)

    @property
    def header_dict(self) -> dict[str, str]:
        # Last value wins; HTTP header names are case-insensitive
        result: dict[str, str] = {}
        for key, value in self.headers:
            result[key.lower()] = value
        return result

    def as_dict(self) -> dict[str, Any]:
        headers = self.header_dict
        return {
            "path": self.path,
            "status": self.status,
            "headers": self.headers,
            "content_length_header": headers.get("content-length"),
            "transfer_encoding": headers.get("transfer-encoding"),
            "link_header": headers.get("link"),
            "content_disposition": headers.get("content-disposition"),
            "body_bytes": len(self.body),
            "body_chunks": [len(chunk) for chunk in self.body_chunks],
            "body_sha256": __import__("hashlib").sha256(self.body).hexdigest(),
            "client_saw_complete_message": self.complete,
            "client_error": self.client_error,
            "client_events": self.client_events,
            "response_started": self.response_started,
            "transport_closed_without_terminal_chunk": (
                self.transport_closed_without_terminal_chunk
            ),
            "wire_writes": len(self.writes),
            "raw_bytes": len(self.raw),
        }


def _parse_as_client(raw: bytes) -> tuple[
    int | None, list[tuple[str, str]], bytes, list[bytes], bool, str | None, list[str]
]:
    conn = h11.Connection(h11.CLIENT)
    events: list[str] = []
    status = None
    headers: list[tuple[str, str]] = []
    body_chunks: list[bytes] = []
    complete = False
    error = None
    try:
        conn.receive_data(raw)
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                break
            events.append(type(event).__name__)
            if isinstance(event, h11.Response):
                status = event.status_code
                headers = [
                    (key.decode("latin-1"), value.decode("latin-1"))
                    for key, value in event.headers
                ]
            elif isinstance(event, h11.Data):
                body_chunks.append(event.data)
            elif isinstance(event, h11.EndOfMessage):
                complete = True
                break
        # Tell the parser the peer is gone; an incomplete chunked body is
        # reported here, exactly as a real HTTP client discovers it.
        conn.receive_data(b"")
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                break
            events.append(type(event).__name__)
            if isinstance(event, h11.Data):
                body_chunks.append(event.data)
            elif isinstance(event, h11.EndOfMessage):
                complete = True
                break
            elif isinstance(event, h11.ConnectionClosed):
                break
    except h11.ProtocolError as ex:
        error = f"{type(ex).__name__}: {ex}"
        events.append(f"ERROR {type(ex).__name__}")
    return status, headers, b"".join(body_chunks), body_chunks, complete, error, events


async def capture(
    app,
    path: str,
    *,
    timeline: list[dict[str, Any]] | None = None,
) -> WireResponse:
    """Drive uvicorn's h11 protocol with one request and capture the bytes."""
    config = Config(
        app,
        host="127.0.0.1",
        port=8000,
        http="h11",
        lifespan="off",
        access_log=False,
        log_level="critical",
        headers=[],
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )
    config.load()
    server_state = ServerState()
    server_state.default_headers = list(config.encoded_headers)
    transport = RecordingTransport()
    protocol = config.http_protocol_class(
        config=config, server_state=server_state, app_state={}
    )
    protocol.connection_made(transport)
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    protocol.data_received(request)

    # Let the ASGI task and its thread-pool SQL calls finish.
    for _ in range(2000):
        if not protocol.tasks:
            break
        await asyncio.gather(*list(protocol.tasks), return_exceptions=True)
        await asyncio.sleep(0)
    if protocol.timeout_keep_alive_task is not None:
        protocol.timeout_keep_alive_task.cancel()

    raw = b"".join(data for _, data in transport.writes)
    status, headers, body, chunks, complete, error, events = _parse_as_client(raw)
    response = WireResponse(
        path=path,
        status=status,
        headers=headers,
        body=body,
        body_chunks=chunks,
        raw=raw,
        complete=complete,
        client_error=error,
        client_events=events,
        response_started=status is not None,
        transport_closed_without_terminal_chunk=(
            status is not None and not complete
        ),
        writes=[
            (round(ts - transport.writes[0][0], 4), len(data))
            for ts, data in transport.writes
        ]
        if transport.writes
        else [],
    )
    if timeline is not None:
        response.timeline = timeline
    return response


def dump(response: WireResponse) -> str:
    return json.dumps(response.as_dict(), indent=2, ensure_ascii=False)

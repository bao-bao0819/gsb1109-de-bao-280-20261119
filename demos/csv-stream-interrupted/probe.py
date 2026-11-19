"""Compare the two ways a large CSV export can fail.

Run from the repository root:

    .venv/bin/python demos/csv-stream-interrupted/probe.py

It needs no network port and no plugin: the queries run against a local
demo.db built by make_db.py, through Datasette's in-process ASGI app.

Case A  the first page's SQL hits the time limit, so the CSV renderer never
        runs and the client receives only an error.
Case B  pages 1-5 are written, then page 6 hits the time limit, so the
        client receives a CSV that stops after 500 complete rows.
"""

import asyncio
import csv
import http.client
import io
import pathlib

import h11

from datasette.app import Datasette

HERE = pathlib.Path(__file__).parent
DB = HERE / "demo.db"
TIME_LIMIT_MS = 100
PAGE_SIZE = 100

BASE_SCOPE = {
    "type": "http",
    "asgi": {"version": "3.0"},
    "http_version": "1.1",
    "method": "GET",
    "scheme": "http",
    "root_path": "",
    "headers": [(b"host", b"localhost")],
    "client": ("127.0.0.1", 12345),
    "server": ("localhost", 80),
}


def ensure_db():
    if not DB.exists():
        import make_db

        make_db.main()


async def capture_asgi(ds, path):
    """Run one request and return the ASGI messages plus the raised exception."""
    scope = dict(BASE_SCOPE)
    scope["path"], _, query = path.partition("?")
    scope["raw_path"] = scope["path"].encode()
    scope["query_string"] = query.encode()

    messages = []
    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # Hold the request open; the app never asks for more than one body.
        await asyncio.sleep(3600)

    async def send(message):
        messages.append(message)

    try:
        await ds.app()(scope, receive, send)
        error = None
    except BaseException as exc:  # noqa: BLE001 - this is the thing we measure
        error = exc
    return messages, error


def to_http1(messages):
    """Encode the ASGI response as HTTP/1.1 bytes, adding chunked framing."""
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    conn.next_event()
    wire = bytearray()
    for message in messages:
        if message["type"] == "http.response.start":
            wire += conn.send(h11.Response(status_code=message["status"], headers=message["headers"]))
        else:
            wire += conn.send(h11.Data(data=message.get("body", b"")))
            if not message.get("more_body"):
                wire += conn.send(h11.EndOfMessage())
                break
    return bytes(wire)


class _Socket:
    def __init__(self, data):
        self._file = io.BytesIO(data)

    def makefile(self, *args, **kwargs):
        return self._file


def parse_as_client(wire):
    """Read the response the way an HTTP client library would."""
    response = http.client.HTTPResponse(_Socket(wire))
    response.begin()
    status = response.status
    headers = {key.lower(): value for key, value in response.getheaders()}
    body = b""
    try:
        body = response.read()
        outcome = "complete"
    except http.client.IncompleteRead as exc:
        body = exc.partial
        outcome = f"IncompleteRead after {len(body)} bytes"
    return status, headers, body, outcome


def csv_facts(body):
    """What an importer sees: header row and complete data records."""
    text = body.decode("utf-8", "replace")
    records = list(csv.reader(io.StringIO(text)))
    if not records:
        return None, []
    return records[0], records[1:]


def show_case(label, path, messages, error):
    status, headers, body, outcome = parse_as_client(to_http1(messages))
    header_row, data_rows = csv_facts(body)
    print("=" * 78)
    print(label)
    print("GET", path)
    print(f"  HTTP status        : {status}")
    print(f"  Content-Type       : {headers.get('content-type', '(none)')}")
    print(f"  Content-Disposition: {headers.get('content-disposition', '(none)')}")
    print(f"  Content-Length     : {headers.get('content-length', '(none: chunked)')}")
    print(f"  Link               : {headers.get('link', '(none)')}")
    print(f"  Client read        : {outcome}")
    print(f"  ASGI exception     : {type(error).__name__ if error else None}")
    print(f"  Bytes received     : {len(body)}")
    if header_row is None or header_row[:1] == ["QueryInterrupted: interrupted"]:
        print("  CSV header         : none - body is the error text")
        print(f"  Body               : {body!r}")
    else:
        print(f"  CSV header         : {header_row}")
        print(f"  Complete data rows : {len(data_rows)}")
        print(f"  First data row     : {data_rows[0] if data_rows else None}")
        print(f"  Last data row      : {data_rows[-1] if data_rows else None}")
        print(f"  Tail               : {body[-50:]!r}")


async def main():
    ensure_db()
    ds = Datasette(
        [str(DB)],
        settings={
            "sql_time_limit_ms": 1000,
            "max_returned_rows": 1000,
            "default_page_size": 100,
        },
    )
    try:
        case_a = f"/demo/case_a.csv?_stream=1&_size={PAGE_SIZE}&_dl=1&_timelimit={TIME_LIMIT_MS}"
        case_b = f"/demo/case_b.csv?_stream=1&_size={PAGE_SIZE}&_dl=1&_timelimit={TIME_LIMIT_MS}"

        messages, error = await capture_asgi(ds, case_a)
        show_case("CASE A  first-page SQL timeout: no CSV was ever written", case_a, messages, error)

        messages, error = await capture_asgi(ds, case_b)
        show_case("CASE B  page 6 timeout: CSV started, stops after 500 rows", case_b, messages, error)

        print()
        print("=" * 78)
        print("RESUME CONDITIONS")
        print("=" * 78)

        # 1. A streaming request has no next-page link and rejects ?_next.
        #    Use the plain table: an expensive page would time out before the
        #    ?_next validation is reached.
        messages, _ = await capture_asgi(ds, f"/demo/big.csv?_stream=1&_size={PAGE_SIZE}&_next=500")
        status, _, body, _ = parse_as_client(to_http1(messages))
        print("/demo/big.csv?_stream=1&_next=500 ->")
        print(f"  status {status}, body {body!r}")
        print("  a streamed export cannot be resumed with the server's cursor")

        # 2. Non-streaming pages are atomic and carry rel="next".
        messages, _ = await capture_asgi(ds, "/demo/case_b.csv?_size=100&_dl=1")
        status, headers, body, _ = parse_as_client(to_http1(messages))
        print()
        print("/demo/case_b.csv?_size=100&_dl=1 (no _stream) ->")
        print(f"  status {status} ({len(body)} bytes)")
        print(f"  Link   {headers.get('link')}")

        # 3. Following that Link retries exactly the page that failed. The
        #    slow band is rows 501-600, so the same 100-row page times out
        #    again when retried.
        messages, error = await capture_asgi(ds, "/demo/case_b.csv?_size=100&_dl=1&_next=500")
        status, headers, body, _ = parse_as_client(to_http1(messages))
        print()
        print("/demo/case_b.csv?_size=100&_dl=1&_next=500 ->")
        print(f"  status {status}, body {body[:60]!r}")
        print("  the cursor retries the failing page; it is not a byte offset")

        # 4. ?_timelimit= can only shorten the server limit, never lengthen it,
        #    so a page that is too slow needs smaller pages, a filter that
        #    skips it, or a raised server setting with a restart.
        messages, error = await capture_asgi(ds, "/demo/case_b.csv?_size=100&_dl=1&_next=500&_timelimit=5000")
        status, headers, body, _ = parse_as_client(to_http1(messages))
        print()
        print("/demo/case_b.csv?_size=100&_dl=1&_next=500&_timelimit=5000 ->")
        print(f"  status {status}, body {body[:60]!r}")
        print("  _timelimit only lowers sql_time_limit_ms; it cannot raise it")

        # 5. The same cursor with a smaller _size succeeds: 25 rows is cheap
        #    enough to finish in the remaining budget.
        messages, _ = await capture_asgi(ds, "/demo/case_b.csv?_size=25&_dl=1&_next=500")
        status, headers, body, _ = parse_as_client(to_http1(messages))
        _, rows = csv_facts(body)
        print()
        print("/demo/case_b.csv?_size=25&_dl=1&_next=500 ->")
        print(f"  status {status}, rows {rows[0][0]}..{rows[-1][0]} ({len(rows)} rows)")
        print(f"  Link   {headers.get('link')}")
        print("  smaller _size keeps the same keyset cursor and can make progress")

        # 6. A filter can also express "continue after the last key". It is
        #    only equivalent when the export has a unique, indexed sort key.
        messages, _ = await capture_asgi(ds, "/demo/case_b.csv?_size=100&_dl=1&id__gt=600")
        status, headers, body, _ = parse_as_client(to_http1(messages))
        _, rows = csv_facts(body)
        print()
        print("/demo/case_b.csv?_size=100&_dl=1&id__gt=600 ->")
        print(f"  status {status}, rows {rows[0][0]}..{rows[-1][0]} ({len(rows)} rows)")
        print("  id__gt= last_key is exact only because id is unique here")

        print()
        print("Note: ?_next= is an exclusive cursor - _next=500 starts at row 501.")
        print("      Resume from the last COMPLETE row's key, not from a byte offset,")
        print("      and check that the first resumed key is last_key + 1.")
    finally:
        ds.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Reproduction: Datasette's streamed CSV can end after a partial batch.

These tests drive uvicorn's real h11 protocol (see ``repro/httpwire.py``) so
the evidence is the HTTP/1.1 bytes a client actually sees, not the ASGI
message stream. See ``repro/README.md`` for the full narrative.
"""

import pytest

from repro.diagnostics import diagnose, parse_csv
from repro.fixtures import PAGE_SIZE, ROW_COUNT, make_datasette, prepare
from repro.httpwire import capture
from repro.receiver import receive
from repro.scenarios import scenario


async def _capture(item):
    ds = make_datasette(max_stream_mb=item.max_stream_mb)
    await prepare(ds)
    try:
        wire = await capture(ds.app(), item.path)
    finally:
        ds.close()
    return wire, diagnose(item, wire)


async def test_complete_downloads_end_with_final_chunk():
    for key, rows, pages in (
        ("complete_short", PAGE_SIZE, 1),
        ("complete_long", ROW_COUNT, 2),
    ):
        wire, report = await _capture(scenario(key))
        assert wire.status == 200
        # A clean end means the client parser saw the terminating zero chunk
        assert wire.complete is True
        assert wire.client_error is None
        assert report.record_count == rows
        assert report.ends_on_record_boundary is True
        assert len(wire.body_chunks) == pages
        # Streamed downloads never advertise a length or a next link
        assert wire.header_dict.get("content-length") is None
        assert wire.header_dict.get("transfer-encoding") == "chunked"
        assert wire.header_dict.get("link") is None


async def test_first_page_failure_happens_before_any_csv():
    wire, report = await _capture(scenario("fail_first_page"))
    assert wire.status == 400
    assert wire.complete is True
    assert report.parse.records == []
    # No 200 headers, so this failure is safely retryable as a whole
    assert wire.header_dict.get("content-type") == "text/plain; charset=utf-8"


@pytest.mark.parametrize("key", ["timeout_next_page", "size_limit"])
async def test_aborted_stream_is_indistinguishable_from_a_short_download(key):
    short_wire, short_report = await _capture(scenario("complete_short"))
    wire, report = await _capture(scenario(key))

    # The bytes and headers are identical to a genuinely complete download
    assert wire.status == 200
    assert wire.body == short_wire.body
    assert wire.headers == short_wire.headers
    assert wire.header_dict.get("link") is None

    # Only the transport tells them apart: no final zero chunk
    assert wire.complete is False
    assert wire.client_error is not None
    assert report.record_count == PAGE_SIZE
    assert report.ends_on_record_boundary is True
    assert report.parse.records == short_report.parse.records

    # A trusting receiver commits the truncated batch in both cases
    trusting = []
    assert receive(short_wire, policy="trusting", into=trusting).committed
    assert receive(wire, policy="trusting", into=trusting).committed

    # A strict receiver keeps the complete one and rejects the truncated one
    strict = []
    assert receive(short_wire, policy="strict", into=strict).committed
    rejected = receive(wire, policy="strict", into=strict)
    assert not rejected.committed
    assert "did not terminate" in rejected.reason
    assert len(strict) == PAGE_SIZE


def test_csv_parser_does_not_trust_an_unterminated_final_line():
    body = (
        b'id,note\r\n'
        b'1,"comma, inside"\r\n'
        b'2,"quote "" and\nnewline"\r\n'
        b'3,partial'
    )
    parsed = parse_csv(body)
    assert [row[0] for row in parsed.records] == ["id", "1", "2"]
    assert parsed.residual == "3,partial"


def test_csv_parser_handles_crlf_inside_quoted_field():
    parsed = parse_csv(b'id,note\r\n1,"a\r\nb"\r\n2,plain\r\n')
    assert parsed.records == [["id", "note"], ["1", "a\r\nb"], ["2", "plain"]]
    assert parsed.residual == ""


async def test_headers_sent_then_nothing_written_is_its_own_failure_shape():
    # A first chunk bigger than max_stream_mb is refused before it is yielded:
    # the client sees a 200 with chunked encoding and then zero bytes.
    wire, report = await _capture(scenario("size_limit_before_any_body"))
    assert wire.status == 200
    assert wire.complete is False
    assert wire.body == b""
    assert report.record_count == 0
    assert wire.client_error is not None


async def test_resume_requires_a_cursor_the_stream_does_not_expose():
    from repro.resume import next_link, walk_pages

    ds = make_datasette()
    await prepare(ds)
    try:
        pages, ids, duplicates = await walk_pages(ds.app(), "/data/events.csv?_size=10")
        assert [page["rows"] for page in pages] == [10, 10]
        assert ids == [str(i) for i in range(1, 21)]
        assert duplicates == []
        assert next_link(await capture(ds.app(), "/data/events.csv?_stream=1&_size=10")) is None
    finally:
        ds.close()


async def test_keyset_cursor_skips_rows_inserted_before_it():
    # Resuming is only row-for-row safe against a fixed snapshot. With a
    # keyset cursor, rows inserted before the cursor are skipped and rows
    # inserted after it appear on a later page.
    from repro.diagnostics import parse_csv
    from repro.resume import next_link, to_request_path

    ds = make_datasette()
    await prepare(ds)
    try:
        page1 = await capture(ds.app(), "/data/events.csv?_size=10")
        link = next_link(page1)
        assert link is not None
        db = ds.get_database("data")
        await db.execute_write(
            "insert into events (id, payload, note) values (0, 'x', 'before')"
        )
        await db.execute_write(
            "insert into events (id, payload, note) values (21, 'x', 'after')"
        )
        page2 = await capture(ds.app(), to_request_path(link))
        page3 = await capture(ds.app(), to_request_path(next_link(page2)))
        ids2 = [row[0] for row in parse_csv(page2.body).data_rows]
        ids3 = [row[0] for row in parse_csv(page3.body).data_rows]
        # id 0 was inserted before the cursor, so it is never returned
        assert ids2 == [str(i) for i in range(11, 21)]
        assert ids3 == ["21"]
    finally:
        ds.close()


async def test_server_knows_the_next_cursor_before_the_first_chunk():
    # The CSV renderer receives page["next"] but does not emit it. A probe
    # renderer that does print it shows the server knew, before any bytes,
    # that the complete_short download was done and the long ones were not.
    from repro.server_side import page_next, probe_renderer

    with probe_renderer():
        ds = make_datasette(max_stream_mb=1)
        await prepare(ds)
        try:
            assert await page_next(ds, "/data/events.probe?_stream=1&_size=10") == "'10'"
            assert (
                await page_next(
                    ds,
                    "/data/events.probe?_stream=1&_size=10&_where=id%20%3C%3D%2010",
                )
                == "None"
            )
        finally:
            ds.close()

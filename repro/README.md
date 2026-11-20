# Streaming CSV truncation: local reproduction and diagnosis

This directory reproduces the ingestion bug against the current snapshot
without changing the download protocol. It answers three questions:

1. What evidence proves a download finished normally?
2. What evidence proves it did not, and how early can each failure be seen?
3. When can a partial transfer be resumed without missing or duplicating rows?

## Run it

From the repository root:

```bash
.venv/bin/python -m repro.run            # human readable report
.venv/bin/python -m repro.run --json     # machine readable
.venv/bin/python -m repro.run --raw DIR  # also write real wire bytes
.venv/bin/python -m pytest tests/test_ingest_stream_repro.py -q
```

The sandbox this repro was prepared in does not allow binding a local socket,
so `repro/httpwire.py` does not fake HTTP. It instantiates the exact protocol
class uvicorn uses (`uvicorn.protocols.http.h11_impl.H11Protocol`), feeds it
the request bytes a client would send, and records every byte it writes. The
same bytes are then parsed with an `h11` client. Status line, header order,
chunk framing and the terminal zero chunk are therefore produced by uvicorn
and h11, not by this repro.

On a machine that allows local TCP you can get the same bodies over a real
port with `python -m datasette --memory -p 8001` plus the fixture SQL below;
the transport conclusions do not depend on which path delivered the bytes.

## Fixtures and requests

`repro/fixtures.py` creates `events` (20 rows) and `short` (the first 10).
Every payload is 60,000 bytes, so consecutive pages are almost exactly the
same size. The `note` column deliberately includes `comma, inside`,
`quote " and` + newline, and a doubled quote, so quoting and embedded newlines
are exercised in the paginated output.

| scenario | request | max_stream_mb | expectation |
| --- | --- | --- | --- |
| `complete_short` | `/data/events.csv?_stream=1&_size=10&_dl=1&_where=id <= 10` | 1 | 200, final chunk, 10 rows |
| `complete_long` | `/data/events.csv?_stream=1&_size=10&_dl=1` | 100 | 200, two flushes, 20 rows |
| `fail_first_page` | `...&_where=nosuchfunction(id)` | 100 | 400 before any CSV |
| `timeout_next_page` | `...&_where=case when id <= 11 then 1 else <heavy CTE> end > 0` | 100 | 200, page one only, connection dropped |
| `size_limit` | `/data/events.csv?_stream=1&_size=10&_dl=1` | 1 | 200, page one only, connection dropped |
| `size_limit_before_any_body` | `/data/events.csv?_stream=1&_size=20&_dl=1` | 1 | 200 + chunked, zero body bytes |

`complete_short` and the two aborted scenarios share a path and differ only in
the filter, so the request is otherwise identical. Its body and headers are
byte-for-byte identical to what the aborted downloads received first.

## Observed results

From `repro/artifacts/report.json` (regenerate with `--json`):

```
complete_short               200  complete=True   chunks=1  rows=10  link=None
complete_long                200  complete=True   chunks=2  rows=20  link=None
fail_first_page              400  complete=True   chunks=1  rows=0
timeout_next_page            200  complete=False  chunks=1  rows=10  link=None
size_limit                   200  complete=False  chunks=1  rows=10  link=None
size_limit_before_any_body   200  complete=False  chunks=0  rows=0   link=None
```

The raw `.http` files show the difference directly. A complete response ends
with the terminal chunk:

```
... "comma, inside"\r\n\r\n0\r\n\r\n        complete_short
... newline"\r\n\r\n0\r\n\r\n               complete_long
... "comma, inside"\r\n\r\n                 timeout_next_page  (no 0\r\n\r\n)
... "comma, inside"\r\n\r\n                 size_limit         (no 0\r\n\r\n)
Content-Type: text/csv...\r\n\r\n           size_limit_before_any_body (no chunk at all)
```

A client with a real HTTP stack reports
`RemoteProtocolError: peer closed connection without sending complete message
body (incomplete chunked read)` for the three aborted cases. That is the only
reliable wire-level difference between them and a normal short download.

## Earliest observable difference, by failure

| failure | before any response | after 200 headers | after first bytes |
| --- | --- | --- | --- |
| SQL fails on page 1 | 400 with an error body; no CSV; safe to retry whole request | n/a | n/a |
| page 2+ query times out | not visible | not visible | connection dropped without terminal chunk |
| `max_stream_mb` after page 1 | not visible | not visible | connection dropped without terminal chunk |
| `max_stream_mb` before page 1 | not visible | 200 + `Transfer-Encoding: chunked` with zero body bytes | n/a |

The important split is whether a `Response` was returned before the failure:

* `fail_first_page`: `table_view_data()` raises before the view returns a
  `Response`, so the outer handler still owns the request and sends 400.
* the other three: `Response.stream()` has already sent
  `http.response.start`, so the top-level handler re-raises and uvicorn closes
  the connection. In `size_limit_before_any_body` the first chunk is refused
  before it is yielded, so the body is empty - still not a 200-with-CSV.

## Normal completion: what is reliable

Only the HTTP message terminating cleanly is reliable:

* chunked body ends with the zero-length chunk (`0\r\n\r\n`), and
* the CSV parser reaches end-of-input with an empty residual (no partial
  record, no unclosed quote).

These are *not* reliable:

* `200 OK` - all three aborted shapes start with 200.
* `Content-Length` - streamed exports never send one.
* the last parsed row being well formed - both aborted downloads end exactly
  on a record boundary, because Datasette flushes a whole page at a time.
* `Link: rel=next` - absent on streamed responses even when more pages exist
  (the code sets `next_url = None` after streaming, and `?_next=` is rejected
  with `_stream=1`).
* row count - a partial response can contain a plausible, complete-looking
  prefix with no marker of how many rows were expected.

## What the same receiver sees

`repro/receiver.py` is a deliberately naive receiver. It is run twice over
byte-identical responses: `complete_short`, and the first page of
`timeout_next_page` / `size_limit`.

| policy | complete_short | aborted stream |
| --- | --- | --- |
| `trusting`: 200 + last row parses | commits 10 rows | **commits 10 rows** |
| `strict`: require clean HTTP end | commits 10 rows | rejects, 0 rows |

So with the bytes and headers identical, the trusting policy cannot tell the
two apart at all. The strict policy can, but only by refusing the whole
response - it still cannot know whether a cleanly terminated 200 was supposed
to contain more pages, because the protocol carries no total or cursor.

## Resuming without missing or duplicating rows

A streamed response cannot be resumed. It exposes no cursor and `_next` is
rejected when `_stream=1` is present. The supported alternative is the
non-streamed paginated export:

```
GET /data/events.csv?_size=10          -> 200, 10 rows, Link: <...&_next=10>; rel="next"
GET /data/events.csv?_size=10&_next=10 -> 200, 10 rows, no Link
```

`repro/resume.py` walks that chain: 2 pages, 20 unique ids, no duplicates.
Resuming a page sequence is safe **only if all of these hold**:

1. Each page response terminated cleanly (terminal chunk), and its CSV has no
   residual. A page that was cut short must be re-fetched from its own start;
   there is no byte offset to resume inside a page.
2. The cursor (`_next`) is taken from the last **fully received** page. It is
   an exclusive keyset cursor over the table's primary key, optionally prefixed
   by `_sort`/`_sort_desc` value (for example `_next=plain%2C9&_sort=note`).
   The HTTP response you have does not include a cursor for pages already sent,
   so it must be recorded before the transfer starts, or re-derived by walking
   the non-streamed pages.
3. The rows visible to the query do not change between page requests. A
   keyset cursor skips rows inserted *before* the cursor and includes rows
   inserted *after* it, and a deleted row is simply absent. Verified with this
   fixture: inserting id=0 between pages 1 and 2 leaves page 2 at ids 11..20
   (id 0 is skipped); inserting id=21 appends it to the next page. For a
   stable copy, the source must be read at a fixed snapshot, or the export
   must be bracketed by a change token/version that the ingestion process can
   verify afterwards.

### Evidence gaps in the current interface

The streamed response does not expose, in any response header or body:

* the pagination cursor for the page that was last flushed,
* the total row count or total byte count,
* the number of pages completed,
* any per-page checksum or the page size used.

The `page` dictionary passed to output renderers *does* contain `next` and
`next_url` for the first page, and `row_stream` internally knows each
subsequent page's `next` token (`fetch_next_page()` in
`datasette/views/table.py`). `repro/server_side.py` registers a probe renderer
that prints `page["next"]`; running it shows:

```
complete_short               page['next'] = None
complete_long                page['next'] = '10'
timeout_next_page            page['next'] = '10'
size_limit                   page['next'] = '10'
size_limit_before_any_body   page['next'] = None
```

So before writing the first CSV byte the server already knew the streamed
table had another page - and the CSV renderer discarded that fact. None of it
reaches the CSV bytes or headers. Until the interface exposes a cursor (or a
manifest), a client can prove *transport completeness* but not *dataset
completeness* from a streamed response alone. The only way to close that gap
today is to use the non-streamed paginated form and follow `Link: rel=next`.

## Deliberate non-changes

This repro does not modify the download protocol, the CSV renderer, the size
limit, or the error handling. The existing behaviour - dropping the connection
instead of appending an error line - is what makes the strict transport check
possible; the fix belongs in the ingestion receiver (require a clean HTTP
terminator before committing) and, if dataset completeness must be provable,
in a protocol addition such as a cursor/manifest header.

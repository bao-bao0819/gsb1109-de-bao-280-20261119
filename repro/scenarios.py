"""The five reproducible scenarios and their exact request URLs.

The two 200/aborted cases (``timeout_next_page`` and ``size_limit``) are built
so their first page is byte-for-byte the response of a genuinely complete
download. That is what makes "status 200 + last CSV row is complete" an
insufficient ingestion signal.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

# 20 million rows of recursion: on the second page only, this reliably trips
# Datasette's sql_time_limit_ms. It is gated by ``id <= 11`` because page one
# selects ``limit page_size + 1`` (11) rows, while page two starts at id 11.
HEAVY = (
    "(with recursive c(x) as (select 1 union all select x + 1 from c "
    "where x < 20000000) select count(*) from c)"
)


def timeout_sql() -> str:
    return f"case when id <= 11 then 1 else {HEAVY} end > 0"


@dataclass
class Scenario:
    key: str
    title: str
    path: str
    max_stream_mb: int
    expectation: str
    notes: list[str] = field(default_factory=list)
    # True when this scenario's response is a complete, trustworthy download
    complete: bool = False


SCENARIOS = [
    Scenario(
        key="complete_short",
        title="Normal complete short download (first page is the whole table)",
        path="/data/events.csv?_stream=1&_size=10&_dl=1&_where="
        + urllib.parse.quote("id <= 10"),
        max_stream_mb=1,
        expectation="complete",
        complete=True,
        notes=[
            "Same URL as the aborted cases, only the filter makes the table short.",
            "Its body is byte-identical to the truncated first page below.",
        ],
    ),
    Scenario(
        key="complete_long",
        title="Normal complete download spanning two pages",
        path="/data/events.csv?_stream=1&_size=10&_dl=1",
        max_stream_mb=100,
        expectation="complete",
        complete=True,
        notes=["Second page ends with a normal end-of-stream, not a close."],
    ),
    Scenario(
        key="fail_first_page",
        title="First page query fails (SQL produced no rows yet)",
        path="/data/events.csv?_stream=1&_size=10&_dl=1&_where="
        + urllib.parse.quote("nosuchfunction(id)"),
        max_stream_mb=100,
        expectation="error_before_headers",
        notes=[
            "SQLite rejects the first SELECT (no such function) before the "
            "view returns a Response, so no 200 headers were ever sent."
        ],
    ),
    Scenario(
        key="timeout_next_page",
        title="First page succeeds, next page query times out",
        path="/data/events.csv?_stream=1&_size=10&_dl=1&_where="
        + urllib.parse.quote(timeout_sql()),
        max_stream_mb=100,
        expectation="aborted_after_headers",
        notes=[
            "Page 1 ids 1..11 short-circuit; page 2 hits the recursive CTE and "
            "Datasette raises QueryInterrupted after the 200 has started."
        ],
    ),
    Scenario(
        key="size_limit",
        title="Response stops at the max_stream_mb limit after one page",
        path="/data/events.csv?_stream=1&_size=10&_dl=1",
        max_stream_mb=1,
        expectation="aborted_after_headers",
        notes=[
            "First page is 600,209 bytes and was already flushed; writing page "
            "two would pass 1 MiB, so _limit_stream_size raises before it."
        ],
    ),
    Scenario(
        key="size_limit_before_any_body",
        title="max_stream_mb refuses the first chunk (headers sent, no CSV)",
        path="/data/events.csv?_stream=1&_size=20&_dl=1",
        max_stream_mb=1,
        expectation="aborted_after_headers_empty_body",
        notes=[
            "A single page of 1,200,417 bytes is over the limit, so the size "
            "wrapper raises before yielding it: 200 + chunked, then zero bytes."
        ],
    ),
]

# The two aborted scenarios whose *first page* equals complete_short's body
PARTIAL_KEYS = ("timeout_next_page", "size_limit")


def scenario(key: str) -> Scenario:
    for item in SCENARIOS:
        if item.key == key:
            return item
    raise KeyError(key)

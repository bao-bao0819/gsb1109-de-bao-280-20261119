"""Fixture database and fault-injection SQL functions for the repro.

The tables are deliberately built so that the first ten rows of ``events`` and
every row of ``short`` are byte-for-byte identical, which lets the same
receiver compare a complete short download with a stream that stopped after
its first page.
"""

from __future__ import annotations

from datasette.app import Datasette

PAYLOAD_BYTES = 60_000
ROW_COUNT = 20
PAGE_SIZE = 10


def sql_literal_list(count: int) -> str:
    return "[" + ",".join(str(i) for i in range(1, count + 1)) + "]"


SCHEMA_SQL = f"""
drop table if exists events;
drop table if exists short;
create table events (
    id integer primary key,
    payload text not null,
    note text
);
insert into events (id, payload, note)
select
    value,
    printf('%.*c', {PAYLOAD_BYTES}, 'x'),
    case value % 3
        when 0 then 'plain'
        when 1 then 'comma, inside'
        else 'quote " and' || char(10) || 'newline'
    end
from json_each('{sql_literal_list(ROW_COUNT)}');

create table short (
    id integer primary key,
    payload text not null,
    note text
);
insert into short select * from events where id <= {PAGE_SIZE};
"""


def make_datasette(*, max_stream_mb: int = 100, sql_time_limit_ms: int = 250):
    """Build an in-memory Datasette with the repro fixtures installed."""
    ds = Datasette(
        memory=True,
        settings={
            "default_page_size": PAGE_SIZE,
            "max_returned_rows": 1000,
            "max_stream_mb": max_stream_mb,
            "sql_time_limit_ms": sql_time_limit_ms,
        },
    )
    return ds


async def prepare(ds: Datasette):
    db = ds.add_memory_database("ingest_repro", name="data")
    await db.execute_write_script(SCHEMA_SQL)
    await ds.invoke_startup()
    return db

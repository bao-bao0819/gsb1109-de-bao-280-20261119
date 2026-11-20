import gc
from pathlib import Path
from typing import Iterator, List, cast

import duckdb
import pyarrow as pa
import pytest
from pyarrow import RecordBatch, RecordBatchReader
from pyarrow import Table as ArrowTable
from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.engine import Connection, Engine

from duckdb_engine import (
    ArrowBatchReaderRegistry,
    StaleRecordBatchReaderError,
    _wrap_record_batch_reader,
)

A_ROWS = 10
B_ROWS = 20
BATCH = 3


def _make_engine(tmp_path: Path) -> Engine:
    # a file-backed engine uses QueuePool, and a single pooled connection
    # guarantees that B borrows the exact physical connection used by A
    engine = create_engine(
        f"duckdb:///{tmp_path / 'batches.db'}",
        pool_size=1,
        max_overflow=0,
    )
    with engine.begin() as con:
        con.execute(text("CREATE TABLE a AS SELECT range i FROM range(10)"))
        con.execute(
            text(
                "CREATE TABLE b (label VARCHAR, n INTEGER);"
                "INSERT INTO b SELECT 'x' || range, range FROM range(20)"
            )
        )
    return engine


def _borrow(engine: Engine) -> Connection:
    return engine.connect()


def test_fetch_arrow() -> None:
    engine = create_engine("duckdb:///:memory:")
    with engine.begin() as con:
        con.execute(text("CREATE TABLE tbl (label VARCHAR, value DOUBLE)"))
        con.execute(
            text(
                "INSERT INTO tbl VALUES ('xx',-1.0), ('ww',-4.5), ('zz',6.0), ('yy',2.5)"
            )
        )

    md = MetaData()
    t = Table("tbl", md, autoload_with=engine)
    stmt = t.select().where(t.c.value > -4.0).order_by(t.c.label)

    # rows
    with engine.begin() as con:
        res = con.execute(stmt).cursor.fetchall()
        assert res == [("xx", -1.0), ("yy", 2.5), ("zz", 6.0)]

    # arrow table
    with engine.begin() as con:
        res = con.execute(stmt).cursor.fetch_arrow_table()
        assert isinstance(res, ArrowTable)
        assert res == ArrowTable.from_pydict(
            {"label": ["xx", "yy", "zz"], "value": [-1.0, 2.5, 6.0]}
        )

    # arrow batches
    with engine.begin() as con:
        res = con.execute(stmt).cursor.fetch_record_batch()
        assert isinstance(res, RecordBatchReader)
        assert res.read_all() == ArrowTable.from_pydict(
            {"label": ["xx", "yy", "zz"], "value": [-1.0, 2.5, 6.0]}
        )
        res = con.execute(stmt).cursor.fetch_record_batch(rows_per_batch=2)
        assert res.read_next_batch() == RecordBatch.from_pydict(
            {"label": ["xx", "yy"], "value": [-1.0, 2.5]}
        )
        assert res.read_next_batch() == RecordBatch.from_pydict(
            {"label": ["zz"], "value": [6.0]}
        )


@pytest.mark.parametrize("close_reader", [True, False])
def test_stale_reader_after_pool_reuse(tmp_path, close_reader: bool) -> None:
    engine = _make_engine(tmp_path)

    # A: borrow the connection, start a multi-batch result, take only the
    # first batch and then leave the borrow scope
    with _borrow(engine) as con:
        wrapper_a = con.connection.dbapi_connection
        result_a = con.execute(text("SELECT * FROM a ORDER BY i"))
        reader_a = result_a.cursor.fetch_record_batch(rows_per_batch=BATCH)
        assert isinstance(reader_a, RecordBatchReader)
        batch_a = reader_a.read_next_batch()
        assert batch_a == RecordBatch.from_pydict({"i": [0, 1, 2]})
        if close_reader:
            reader_a.close()

    # B: must receive the same physical connection from the pool
    with _borrow(engine) as con:
        wrapper_b = con.connection.dbapi_connection
        assert wrapper_b is wrapper_a

        result_b = con.execute(text("SELECT * FROM b ORDER BY n"))
        reader_b = result_b.cursor.fetch_record_batch(rows_per_batch=4)
        first_b = reader_b.read_next_batch()
        assert first_b.schema.names == ["label", "n"]
        assert first_b.to_pydict()["n"] == [0, 1, 2, 3]

        # B has already started; touching the stale A reader must fail
        # explicitly and must never surface B's columns or rows
        with pytest.raises(StaleRecordBatchReaderError):
            reader_a.read_next_batch()
        assert reader_a.schema.names == ["i"]
        # closing the stale reader is harmless and must not disturb B
        reader_a.close()

        # once reported stale, further reads signal a clean end rather than
        # leaking the new result
        with pytest.raises((StopIteration, StaleRecordBatchReaderError)):
            reader_a.read_next_batch()

        # B keeps streaming its own, differently-named data to completion
        seen: List[int] = list(first_b.to_pydict()["n"])
        while True:
            try:
                seen.extend(reader_b.read_next_batch().to_pydict()["n"])
            except StopIteration:
                break
        assert seen == list(range(B_ROWS))
        assert reader_b.schema.names == ["label", "n"]

    # the already-delivered A batch is an independent object and stays valid
    assert batch_a.to_pydict() == {"i": [0, 1, 2]}

    # the connection must have been returned to the pool cleanly
    with _borrow(engine) as con:
        assert con.execute(text("SELECT count(*) FROM b")).scalar() == B_ROWS

    engine.dispose()


def test_next_query_in_same_borrow_invalidates_previous_reader() -> None:
    engine = create_engine("duckdb:///:memory:")
    with engine.connect() as con:
        con.execute(text("CREATE TABLE a AS SELECT range i FROM range(10)"))
        con.execute(text("CREATE TABLE b (label VARCHAR, n INTEGER)"))
        con.execute(text("INSERT INTO b SELECT 'x' || range, range FROM range(20)"))

        reader_a = con.execute(
            text("SELECT * FROM a ORDER BY i")
        ).cursor.fetch_record_batch(rows_per_batch=BATCH)
        batch_a = reader_a.read_next_batch()
        assert batch_a.num_rows == BATCH

        # running the next statement on the same connection retires A;
        # the first read after invalidation reports it explicitly
        result_b = con.execute(text("SELECT * FROM b ORDER BY n"))
        with pytest.raises(StaleRecordBatchReaderError):
            reader_a.read_next_batch()

        # afterwards the reader presents a clean end, never B's data
        with pytest.raises((StopIteration, StaleRecordBatchReaderError)):
            reader_a.read_next_batch()
        assert reader_a.schema.names == ["i"]
        assert reader_a.read_all().num_rows == 0

        reader_b = result_b.cursor.fetch_record_batch(rows_per_batch=5)
        rows: List[int] = []
        while True:
            try:
                rows.extend(reader_b.read_next_batch().to_pydict()["n"])
            except StopIteration:
                break
        assert rows == list(range(B_ROWS))
        assert batch_a.to_pydict() == {"i": [0, 1, 2]}

    engine.dispose()


def test_exhausted_reader_ends_cleanly_before_reuse() -> None:
    engine = create_engine("duckdb:///:memory:")
    with engine.connect() as con:
        con.execute(text("CREATE TABLE a AS SELECT range i FROM range(10)"))
        reader = con.execute(
            text("SELECT * FROM a ORDER BY i")
        ).cursor.fetch_record_batch(rows_per_batch=BATCH)
        table = reader.read_all()
        assert table.num_rows == A_ROWS

        # a fully consumed reader signals a normal end, not a stale error
        with pytest.raises(StopIteration):
            reader.read_next_batch()

        con.execute(text("SELECT 1"))
    engine.dispose()


def test_row_fetching_still_works_alongside_arrow() -> None:
    engine = create_engine("duckdb:///:memory:")
    with engine.connect() as con:
        con.execute(text("CREATE TABLE a AS SELECT range i FROM range(10)"))

        reader = con.execute(
            text("SELECT * FROM a ORDER BY i")
        ).cursor.fetch_record_batch(rows_per_batch=BATCH)
        assert reader.read_next_batch().to_pydict() == {"i": [0, 1, 2]}

        # the next statement invalidates the reader, then ordinary
        # fetchall/fetchmany row interfaces keep working as before
        result = con.execute(text("SELECT * FROM a ORDER BY i"))
        assert result.fetchall() == [(i,) for i in range(A_ROWS)]
        result = con.execute(text("SELECT * FROM a ORDER BY i"))
        assert result.fetchmany(2) == [(0,), (1,)]
    engine.dispose()


class _CountingNativeReader:
    """Stand-in for a DuckDB native reader that counts every batch pull."""

    def __init__(self) -> None:
        self.pulls = 0
        self.closed = False
        self.schema = pa.schema([("i", pa.int64())])

        def batches() -> Iterator[RecordBatch]:
            for group in range(10):
                yield RecordBatch.from_pydict(
                    {"i": [group * 10 + offset for offset in range(10)]}
                )

        self._inner = RecordBatchReader.from_batches(self.schema, batches())

    def read_next_batch(self) -> RecordBatch:
        batch = self._inner.read_next_batch()
        self.pulls += 1
        return batch

    def close(self) -> None:
        self.closed = True
        self._inner.close()


def test_batches_are_pulled_on_demand_not_materialized_upfront() -> None:
    native = _CountingNativeReader()
    registry = ArrowBatchReaderRegistry()
    reader = _wrap_record_batch_reader(cast(RecordBatchReader, native), registry)
    assert isinstance(reader, RecordBatchReader)

    # constructing the reader and taking a single batch must convert/pull
    # exactly one batch from the underlying result, never all ten
    assert native.pulls == 0
    first = reader.read_next_batch()
    assert native.pulls == 1
    assert first.to_pydict() == {"i": list(range(10))}

    second = reader.read_next_batch()
    assert native.pulls == 2
    assert second.to_pydict() == {"i": list(range(10, 20))}

    # only the remaining batches are pulled when the caller drains the reader
    rest = reader.read_all()
    assert native.pulls == 10
    assert rest.num_rows == 80

    # dropping the wrapper closes the native result it wraps
    del reader
    gc.collect()
    assert native.closed is True


def test_multi_batch_query_streams_expected_content() -> None:
    engine = create_engine("duckdb:///:memory:")
    with engine.connect() as con:
        con.execute(text("CREATE TABLE a AS SELECT range i FROM range(10)"))
        reader = con.execute(
            text("SELECT * FROM a ORDER BY i")
        ).cursor.fetch_record_batch(rows_per_batch=BATCH)

        batches = []
        while True:
            try:
                batches.append(reader.read_next_batch())
            except StopIteration:
                break

        # 10 rows in batches of 3 -> 3, 3, 3, 1, pulled one at a time
        assert [batch.num_rows for batch in batches] == [3, 3, 3, 1]
        flat = [value for batch in batches for value in batch.to_pydict()["i"]]
        assert flat == list(range(A_ROWS))
    engine.dispose()


class _CountingProxy:
    """Duck-typed native reader proxy counting batch pulls against DuckDB."""

    def __init__(self, inner: RecordBatchReader, counter: List[int]) -> None:
        self._inner = inner
        self._counter = counter
        self.schema = inner.schema

    def read_next_batch(self) -> RecordBatch:
        batch = self._inner.read_next_batch()
        self._counter[0] += 1
        return batch

    def close(self) -> None:
        self._inner.close()


def test_engine_pulls_batches_on_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    # instrument the native DuckDB call so every batch actually pulled from
    # the database is counted through the full SQLAlchemy execution path
    pulls = [0]
    original = duckdb.DuckDBPyConnection.fetch_record_batch

    def counting_fetch(self, rows_per_batch: int = 1_000_000) -> _CountingProxy:
        native = original(self, rows_per_batch)
        return _CountingProxy(native, pulls)

    monkeypatch.setattr(duckdb.DuckDBPyConnection, "fetch_record_batch", counting_fetch)

    engine = create_engine("duckdb:///:memory:")
    with engine.connect() as con:
        con.execute(text("CREATE TABLE a AS SELECT range i FROM range(100)"))
        reader = con.execute(
            text("SELECT * FROM a ORDER BY i")
        ).cursor.fetch_record_batch(rows_per_batch=10)

        # a caller that only needs the first batch must not pull the other
        # nine batches out of DuckDB
        first = reader.read_next_batch()
        assert first.num_rows == 10
        assert pulls == [1]

        reader.close()
        # a fresh reader on the same connection starts from zero again
        reader = con.execute(
            text("SELECT * FROM a ORDER BY i")
        ).cursor.fetch_record_batch(rows_per_batch=10)
        assert reader.read_all().num_rows == 100
        assert pulls == [11]
    engine.dispose()

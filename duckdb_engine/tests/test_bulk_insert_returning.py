"""Regression coverage for executemany inserts and their returned rows.

The tests deliberately keep three facts separate:

* the rows handed back by the statement's result object,
* the rows (and ids) visible inside the transaction that performed the insert,
* the rows another connection sees after the writer's transaction ends.

Returning the last row of a batch, for example, does not prove that the whole
batch was inserted, and no receipt at all does not prove that nothing was.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Generator, List, Sequence, Tuple

from pytest import fixture, raises
from sqlalchemy import (
    CheckConstraint,
    Column,
    Integer,
    MetaData,
    Table,
    create_engine,
    select,
)
from sqlalchemy.dialects import registry  # type: ignore
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from .. import insert

BUSINESS_IDS = (930, 110, 540)
SUCCESS_ROWS = tuple(
    {"business_id": business_id, "amount": business_id * 10}
    for business_id in BUSINESS_IDS
)
FAILING_ROW = {"business_id": BUSINESS_IDS[-1], "amount": -BUSINESS_IDS[-1] * 10}
FAILED_BATCH_ROWS = SUCCESS_ROWS[:2] + (FAILING_ROW,)
SUCCESS_IDS = sorted(row["business_id"] for row in SUCCESS_ROWS)
PREVIOUSLY_SUCCEEDED_IDS = sorted(row["business_id"] for row in SUCCESS_ROWS[:2])


@dataclass(frozen=True)
class Snapshot:
    ids: List[int]
    rows: List[Tuple[int, int]]


@fixture
def file_engine(tmp_path: Path) -> Generator[Engine, None, None]:
    registry.register("duckdb", "duckdb_engine", "Dialect")
    db_path = tmp_path / "bulk_insert.duckdb"
    engine = create_engine(f"duckdb:///{db_path}")
    try:
        yield engine
    finally:
        engine.dispose()
        for suffix in ("", ".wal"):
            try:
                os.unlink(f"{db_path}{suffix}")
            except FileNotFoundError:
                pass


@fixture
def detail_table(file_engine: Engine) -> Table:
    metadata = MetaData()
    table = Table(
        "bulk_detail",
        metadata,
        Column("business_id", Integer, primary_key=True, autoincrement=False),
        Column("amount", Integer, nullable=False),
        CheckConstraint("amount > 0", name="ck_bulk_detail_amount_positive"),
    )
    metadata.create_all(file_engine)
    return table


def _independent_snapshot(file_engine: Engine, table: Table) -> Snapshot:
    """Read the table through a separate transaction on the same file.

    This is intentionally a different connection: reading through the writer's
    connection before consuming a RETURNING result would replace that result.
    """
    with file_engine.connect() as observer:
        transaction = observer.begin()
        try:
            rows = observer.execute(
                select(table.c.business_id, table.c.amount).order_by(
                    table.c.business_id
                )
            ).all()
        finally:
            transaction.rollback()
    return Snapshot([row[0] for row in rows], [tuple(row) for row in rows])


def _transaction_snapshot(connection: Connection, table: Table) -> Snapshot:
    rows = connection.execute(
        select(table.c.business_id, table.c.amount).order_by(table.c.business_id)
    ).all()
    return Snapshot([row[0] for row in rows], [tuple(row) for row in rows])


def _write_batch(
    connection: Connection,
    table: Table,
    payload: Sequence[Dict[str, int]],
    *,
    with_returning: bool,
) -> Any:
    statement = insert(table)
    if with_returning:
        statement = statement.returning(table.c.business_id)
    return connection.execute(statement, list(payload))


def _consume_receipt(result: Any) -> List[Any]:
    """Consume a DML result, treating a missing result set as no receipt."""
    if not result.returns_rows:
        return []
    return result.fetchall()


def _classify_receipt(receipt_ids: List[int], batch_ids: List[int]) -> str:
    """Classify what a RETURNING result covered without assuming completeness."""
    returned = set(receipt_ids)
    expected = set(batch_ids)
    if not returned:
        return "none"
    if returned == expected:
        return "whole-batch"
    if returned.issubset(expected):
        return "partial"
    return "foreign"


def test_plain_bulk_insert_has_no_receipt_and_commits_all_rows(
    file_engine: Engine, detail_table: Table
) -> None:
    with file_engine.connect() as writer:
        transaction = writer.begin()
        result = _write_batch(writer, detail_table, SUCCESS_ROWS, with_returning=False)

        before_consume = _independent_snapshot(file_engine, detail_table)
        receipt_rows = _consume_receipt(result)
        assert (
            _classify_receipt([row[0] for row in receipt_rows], SUCCESS_IDS) == "none"
        )

        in_transaction = _transaction_snapshot(writer, detail_table)
        assert in_transaction.ids == SUCCESS_IDS
        assert in_transaction.rows == sorted(
            (row["business_id"], row["amount"]) for row in SUCCESS_ROWS
        )
        assert before_consume.rows == []
        assert _independent_snapshot(file_engine, detail_table).rows == []

        transaction.commit()

    # A committed transaction closes; DuckDB accepting the commit is not enough
    # on its own, which is why the physical rows are read independently below.
    assert not transaction.is_active
    committed = _independent_snapshot(file_engine, detail_table)
    assert committed.ids == SUCCESS_IDS
    assert committed.rows == in_transaction.rows


def test_plain_bulk_insert_without_commit_leaves_no_rows_after_exit(
    file_engine: Engine, detail_table: Table
) -> None:
    with file_engine.connect() as writer:
        transaction = writer.begin()
        result = _write_batch(writer, detail_table, SUCCESS_ROWS, with_returning=False)

        before_consume = _independent_snapshot(file_engine, detail_table)
        assert _consume_receipt(result) == []
        assert _transaction_snapshot(writer, detail_table).ids == SUCCESS_IDS
        assert before_consume.rows == []
        assert _independent_snapshot(file_engine, detail_table).rows == []

        transaction.rollback()

    assert _independent_snapshot(file_engine, detail_table).rows == []

    # Closing a connection with an uncommitted transaction is the other exit
    # path; it must not persist anything either.
    writer = file_engine.connect()
    writer.begin()
    result = _write_batch(writer, detail_table, SUCCESS_ROWS, with_returning=False)
    before_consume = _independent_snapshot(file_engine, detail_table)
    assert _consume_receipt(result) == []
    assert _transaction_snapshot(writer, detail_table).ids == SUCCESS_IDS
    assert before_consume.rows == []
    writer.close()

    assert _independent_snapshot(file_engine, detail_table).rows == []


def test_parameter_list_returning_reports_last_row_only(
    file_engine: Engine, detail_table: Table
) -> None:
    with file_engine.connect() as writer:
        transaction = writer.begin()
        result = _write_batch(writer, detail_table, SUCCESS_ROWS, with_returning=True)

        before_consume = _independent_snapshot(file_engine, detail_table)
        assert result.returns_rows
        receipt_rows = result.fetchall()
        receipt_ids = [row[0] for row in receipt_rows]

        assert receipt_ids == [BUSINESS_IDS[-1]]
        assert _classify_receipt(receipt_ids, SUCCESS_IDS) == "partial"
        # The explicit last-row boundary: do not infer whole-batch persistence
        # from a receipt that only names one row.
        assert set(receipt_ids) != set(SUCCESS_IDS)

        in_transaction = _transaction_snapshot(writer, detail_table)
        assert in_transaction.ids == SUCCESS_IDS
        assert set(receipt_ids).issubset(in_transaction.ids)
        assert before_consume.rows == []
        assert _independent_snapshot(file_engine, detail_table).rows == []

        transaction.commit()

    # A committed transaction closes; DuckDB accepting the commit is not enough
    # on its own, which is why the physical rows are read independently below.
    assert not transaction.is_active
    committed = _independent_snapshot(file_engine, detail_table)
    assert committed.ids == SUCCESS_IDS
    assert committed.rows == in_transaction.rows


def test_parameter_list_returning_last_row_does_not_persist_after_rollback(
    file_engine: Engine, detail_table: Table
) -> None:
    with file_engine.connect() as writer:
        transaction = writer.begin()
        result = _write_batch(writer, detail_table, SUCCESS_ROWS, with_returning=True)

        before_consume = _independent_snapshot(file_engine, detail_table)
        receipt_ids = [row[0] for row in result.fetchall()]
        assert receipt_ids == [BUSINESS_IDS[-1]]
        assert _classify_receipt(receipt_ids, SUCCESS_IDS) == "partial"
        assert _transaction_snapshot(writer, detail_table).ids == SUCCESS_IDS
        assert before_consume.rows == []
        assert _independent_snapshot(file_engine, detail_table).rows == []

        transaction.rollback()

    assert _independent_snapshot(file_engine, detail_table).rows == []


def test_parameter_list_returning_last_row_does_not_persist_after_close(
    file_engine: Engine, detail_table: Table
) -> None:
    writer = file_engine.connect()
    writer.begin()
    result = _write_batch(writer, detail_table, SUCCESS_ROWS, with_returning=True)

    before_consume = _independent_snapshot(file_engine, detail_table)
    receipt_ids = [row[0] for row in result.fetchall()]
    assert receipt_ids == [BUSINESS_IDS[-1]]
    assert _classify_receipt(receipt_ids, SUCCESS_IDS) == "partial"
    assert _transaction_snapshot(writer, detail_table).ids == SUCCESS_IDS
    assert before_consume.rows == []
    writer.close()

    assert _independent_snapshot(file_engine, detail_table).rows == []


def test_single_row_inserts_put_previously_successful_rows_in_place(
    file_engine: Engine, detail_table: Table
) -> None:
    """The first two payloads are individually valid against the same table."""
    with file_engine.connect() as writer:
        transaction = writer.begin()
        for row in SUCCESS_ROWS[:2]:
            writer.execute(insert(detail_table), row)

        previous = _transaction_snapshot(writer, detail_table)
        assert previous.ids == PREVIOUSLY_SUCCEEDED_IDS
        assert _independent_snapshot(file_engine, detail_table).rows == []

        transaction.commit()

    assert (
        _independent_snapshot(file_engine, detail_table).ids == PREVIOUSLY_SUCCEEDED_IDS
    )


def test_failing_last_row_alone_is_rejected_without_residue(
    file_engine: Engine, detail_table: Table
) -> None:
    with file_engine.connect() as writer:
        transaction = writer.begin()
        with raises(DBAPIError, match="CHECK constraint failed"):
            writer.execute(insert(detail_table), FAILING_ROW)
        assert transaction.is_active
        transaction.rollback()

    assert _independent_snapshot(file_engine, detail_table).rows == []


def _assert_failed_batch_left_no_rows(
    file_engine: Engine,
    detail_table: Table,
    writer: Connection,
    transaction: Any,
) -> None:
    assert transaction.is_active
    # The failed statement rolls the whole DuckDB transaction back to an
    # aborted state, so the two individually valid rows are not readable here.
    with raises(DBAPIError, match="Current transaction is aborted"):
        writer.execute(select(detail_table.c.business_id))
    assert _independent_snapshot(file_engine, detail_table).rows == []


def _run_failing_batch(
    file_engine: Engine,
    detail_table: Table,
    *,
    with_returning: bool,
    attempt_commit: bool,
) -> None:
    assert FAILED_BATCH_ROWS[:2] == SUCCESS_ROWS[:2]
    assert FAILED_BATCH_ROWS[-1] is FAILING_ROW
    assert FAILING_ROW["business_id"] == BUSINESS_IDS[-1]
    assert FAILING_ROW["amount"] < 0

    writer = file_engine.connect()
    transaction = writer.begin()
    try:
        result: Any = None
        with raises(DBAPIError, match="CHECK constraint failed"):
            result = _write_batch(
                writer,
                detail_table,
                FAILED_BATCH_ROWS,
                with_returning=with_returning,
            )
        # A failing batch hands back no result object at all; the two valid rows
        # therefore cannot be mistaken for a partial receipt.
        assert result is None

        _assert_failed_batch_left_no_rows(
            file_engine, detail_table, writer, transaction
        )
        assert FAILING_ROW["business_id"] not in PREVIOUSLY_SUCCEEDED_IDS

        if attempt_commit:
            # DuckDB treats commit of an aborted transaction as a no-op, so this
            # call succeeds but must not persist either successful row.
            transaction.commit()
            assert not transaction.is_active
        else:
            # The transaction is still open here; closing the connection must
            # discard it without an explicit rollback.
            assert transaction.is_active
    finally:
        writer.close()

    assert _independent_snapshot(file_engine, detail_table).rows == []


def test_failing_last_row_plain_bulk_commit_path(
    file_engine: Engine, detail_table: Table
) -> None:
    _run_failing_batch(
        file_engine,
        detail_table,
        with_returning=False,
        attempt_commit=True,
    )


def test_failing_last_row_plain_bulk_close_without_commit(
    file_engine: Engine, detail_table: Table
) -> None:
    _run_failing_batch(
        file_engine,
        detail_table,
        with_returning=False,
        attempt_commit=False,
    )


def test_failing_last_row_returning_commit_path(
    file_engine: Engine, detail_table: Table
) -> None:
    _run_failing_batch(
        file_engine,
        detail_table,
        with_returning=True,
        attempt_commit=True,
    )


def test_failing_last_row_returning_close_without_commit(
    file_engine: Engine, detail_table: Table
) -> None:
    _run_failing_batch(
        file_engine,
        detail_table,
        with_returning=True,
        attempt_commit=False,
    )

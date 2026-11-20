"""
Round-trip DuckDB CHECK constraints through SQLAlchemy reflection.

Two attached databases expose the same schema name and the same
quality-check table name but different rules. Reflecting one of them
explicitly must export every CHECK constraint — including two bounds on
the same column (which the catalog may report under one shared name) and
a cross-column rule referencing a column whose name contains a space and
string literals containing parentheses — rebuild the table in an empty
database from the exported metadata, and accept/reject sample rows
identically in both places.
"""

from pathlib import Path
from typing import Any, Dict, List, Tuple

import duckdb
from sqlalchemy import CheckConstraint, MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine import Connection

SCHEMA = "qc"
TABLE = "quality_check"

SOURCE_DDL = f"""
CREATE TABLE {SCHEMA}.{TABLE} (
    id INTEGER,
    quantity INTEGER,
    "unit price" DOUBLE,
    status VARCHAR,
    CONSTRAINT qty_floor CHECK (quantity >= 1),
    CONSTRAINT qty_cap CHECK (quantity <= 100),
    CHECK (status IN ('ok (new)', 'ok (used)') OR "unit price" > 0)
)
"""

DECOY_DDL = f"""
CREATE TABLE {SCHEMA}.{TABLE} (
    id INTEGER,
    quantity INTEGER,
    "unit price" DOUBLE,
    status VARCHAR,
    CHECK (quantity >= 500)
)
"""

# (label, row, expected to be accepted)
SAMPLES: List[Tuple[str, Dict[str, Any], bool]] = [
    (
        "violates only the lower bound",
        {"id": 1, "quantity": 0, "price": 10.0, "status": "ok (new)"},
        False,
    ),
    (
        "violates only the upper bound",
        {"id": 2, "quantity": 101, "price": 10.0, "status": "ok (new)"},
        False,
    ),
    (
        "violates only the cross-column rule",
        {"id": 3, "quantity": 50, "price": -1.0, "status": "rejected"},
        False,
    ),
    (
        "involves NULLs",
        {"id": 4, "quantity": None, "price": None, "status": None},
        True,
    ),
    (
        "satisfies every rule",
        # 9.5 is exactly representable in both FLOAT and DOUBLE
        {"id": 5, "quantity": 50, "price": 9.5, "status": "ok (used)"},
        True,
    ),
]

def _build_db(path: Path, ddl: str, extra: str = "") -> None:
    con = duckdb.connect(str(path))
    con.execute(f"CREATE SCHEMA {SCHEMA}")
    con.execute(ddl)
    if extra:
        con.execute(extra)
    con.close()


def _catalog_checks(
    conn: Connection, database: str, table: str
) -> List[Tuple[str, str]]:
    """Ground truth straight from duckdb_constraints()."""
    return [
        (name, check_text)
        for name, check_text in conn.execute(
            text(
                "SELECT constraint_name, constraint_text FROM duckdb_constraints() "
                "WHERE database_name = :db AND schema_name = :schema "
                "AND table_name = :table AND constraint_type = 'CHECK' "
                "ORDER BY constraint_index"
            ),
            {"db": database, "schema": SCHEMA, "table": table},
        )
    ]


def _try_insert(conn: Connection, fq_table: str, row: Dict[str, Any]) -> bool:
    # every sample gets its own transaction so that one rejected row
    # cannot abort the transaction and block the samples after it
    conn.rollback()
    try:
        conn.execute(
            text(
                f'INSERT INTO {fq_table} (id, quantity, "unit price", status) '
                "VALUES (:id, :quantity, :price, :status)"
            ),
            row,
        )
    except Exception:
        conn.rollback()
        return False
    conn.commit()
    return True


def test_check_constraint_roundtrip(tmp_path: Path) -> None:
    src_path = tmp_path / "src.duckdb"
    decoy_path = tmp_path / "decoy.duckdb"
    replica_path = tmp_path / "replica.duckdb"
    _build_db(
        src_path,
        SOURCE_DDL,
        extra=f"CREATE TABLE {SCHEMA}.no_rules (id INTEGER)",
    )
    _build_db(decoy_path, DECOY_DDL)

    engine = create_engine("duckdb:///:memory:", future=True)
    with engine.connect() as conn:
        for alias, path in (
            ("src", src_path),
            ("decoy", decoy_path),
            ("replica", replica_path),
        ):
            conn.execute(text(f"ATTACH '{path}' AS {alias}"))
        conn.execute(text(f"CREATE SCHEMA replica.{SCHEMA}"))
        conn.commit()

        inspector = inspect(conn)

        catalog = _catalog_checks(conn, "src", TABLE)
        assert len(catalog) == 3

        # reflect one of the two same-named databases explicitly
        reflected = inspector.get_check_constraints(TABLE, schema=f"src.{SCHEMA}")
        # every catalog constraint is exported: a short or empty list here
        # would mean silent omission, not "the table has no constraints"
        assert len(reflected) == len(catalog)

        # a table that genuinely has no checks: the empty list is the truth,
        # confirmed against the catalog rather than assumed
        assert inspector.get_check_constraints("no_rules", schema=f"src.{SCHEMA}") == []
        assert _catalog_checks(conn, "src", "no_rules") == []

        meta = MetaData()
        src_table = Table(TABLE, meta, schema=f"src.{SCHEMA}", autoload_with=conn)
        checks = [c for c in src_table.constraints if isinstance(c, CheckConstraint)]
        assert len(checks) == 3

        # the two quantity bounds may share one catalog name; they are
        # still two distinct rules and both must survive
        quantity_checks = [c for c in checks if "quantity" in str(c.sqltext)]
        assert len(quantity_checks) == 2
        assert len({str(c.sqltext) for c in quantity_checks}) == 2

        # the cross-column rule survives with its string literals and the
        # space-containing column name intact
        (cross,) = [c for c in checks if "unit price" in str(c.sqltext)]
        cross_text = str(cross.sqltext)
        assert "'ok (new)'" in cross_text
        assert "'ok (used)'" in cross_text
        assert '"unit price"' in cross_text

        # the hand-written names were never stored by DuckDB, so the
        # catalog-provided names are used as-is instead
        assert {c.name for c in checks}.isdisjoint({"qty_floor", "qty_cap"})

        # nothing leaks in from the decoy database's same-named table
        assert all("500" not in str(c.sqltext) for c in checks)

        # rebuild the table in the empty database from the exported metadata
        replica_meta = MetaData()
        replica_table = src_table.to_metadata(replica_meta, schema=f"replica.{SCHEMA}")
        replica_meta.create_all(conn)
        conn.commit()
        assert replica_table.name == TABLE

        replica_reflected = inspect(conn).get_check_constraints(
            TABLE, schema=f"replica.{SCHEMA}"
        )
        assert len(replica_reflected) == 3
        assert sorted(str(c["sqltext"]) for c in replica_reflected) == sorted(
            str(c["sqltext"]) for c in reflected
        )
        assert all("500" not in str(c["sqltext"]) for c in replica_reflected)

        # write every sample to both places; each insert is isolated so a
        # rejection does not prevent the remaining samples from being checked
        for label, row, expected in SAMPLES:
            outcomes = {
                place: _try_insert(conn, f"{place}.{SCHEMA}.{TABLE}", row)
                for place in ("src", "replica")
            }
            assert outcomes["src"] == outcomes["replica"], f"{label}: {outcomes}"
            assert outcomes["src"] is expected, f"{label}: {outcomes}"

        # both places end up holding exactly the same accepted rows
        def rows(place: str) -> List[Tuple]:
            return conn.execute(
                text(
                    f'SELECT id, quantity, "unit price", status '
                    f"FROM {place}.{SCHEMA}.{TABLE} ORDER BY id"
                )
            ).fetchall()

        assert rows("src") == rows("replica")
        assert len(rows("src")) == sum(1 for _, _, accepted in SAMPLES if accepted)

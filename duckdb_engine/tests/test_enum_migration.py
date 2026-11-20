"""
Migrate order tables from two DuckDB databases into a third one while
preserving each database's own ``status_t`` enum definition.

Both source databases use the same schema name (``archive``) and the same
type name (``status_t``) but declare different candidates in a different
order. Enum candidates may also contain commas or quotes, which must survive
reflection and migration verbatim.
"""

from typing import Dict, Iterable, List, Tuple

import pytest
from sqlalchemy import (
    Column,
    Enum,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError

# the first database shares its type between two tables and includes
# candidates containing a comma and double quotes
DB1_LABELS = ["queued", "ready", "done", "needs,review", 'say "hi"']
# the second database declares a different order and its own exclusive value
DB2_LABELS = ["ready", "queued", "blocked"]

SCHEMA_MAP = {"db1.archive": "archive_one", "db2.archive": "archive_two"}


def _quote_label(label: str) -> str:
    return "'{}'".format(label.replace("'", "''"))


def _create_source_database(
    path: str, labels: List[str], tables: Iterable[str]
) -> None:
    engine = create_engine(f"duckdb:///{path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA archive"))
        conn.execute(
            text(
                "CREATE TYPE archive.status_t AS ENUM ({})".format(
                    ", ".join(_quote_label(label) for label in labels)
                )
            )
        )
        for table in tables:
            conn.execute(
                text(
                    f"CREATE TABLE archive.{table} "
                    "(id INTEGER, status archive.status_t, note VARCHAR)"
                )
            )
    engine.dispose()


@pytest.fixture
def source_engine(tmp_path) -> Engine:
    _create_source_database(
        str(tmp_path / "db1.duckdb"), DB1_LABELS, ["orders_alpha", "orders_beta"]
    )
    _create_source_database(str(tmp_path / "db2.duckdb"), DB2_LABELS, ["orders_gamma"])

    engine = create_engine("duckdb:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(f"ATTACH '{tmp_path / 'db1.duckdb'}' AS db1"))
        conn.execute(text(f"ATTACH '{tmp_path / 'db2.duckdb'}' AS db2"))
    yield engine
    engine.dispose()


@pytest.fixture
def target_engine(tmp_path) -> Engine:
    engine = create_engine(f"duckdb:///{tmp_path / 'target.duckdb'}")
    with engine.begin() as conn:
        for schema in set(SCHEMA_MAP.values()):
            conn.execute(text(f"CREATE SCHEMA {schema}"))
    yield engine
    engine.dispose()


def _existing_enum_labels(engine: Engine) -> Dict[Tuple[str, str], List[str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT schema_name, type_name, labels FROM duckdb_types() "
                "WHERE logical_type = 'ENUM' AND labels IS NOT NULL"
            )
        ).fetchall()
    return {(schema, name): list(labels) for schema, name, labels in rows}


def reflect_sources(engine: Engine, sources: List[Tuple[str, str]]) -> List[Table]:
    """Reflect the given (schema, table) sources through a single inspector."""
    inspector = inspect(engine)
    metadata = MetaData()
    return [
        Table(table, metadata, schema=schema, autoload_with=inspector)
        for schema, table in sources
    ]


def migrate_orders(
    source_engine: Engine,
    target_engine: Engine,
    sources: List[Tuple[str, str]],
    schema_map: Dict[str, str],
) -> MetaData:
    """
    Reflect order tables from the source and recreate them in the target
    under explicitly mapped schemas, preserving each enum's declared labels
    and their order.

    Raises a ValueError before creating anything if a same-named enum type
    already exists in the target with different candidates, rather than
    letting table creation succeed with silently different sort semantics.
    """
    reflected = reflect_sources(source_engine, sources)

    target_metadata = MetaData()
    planned: Dict[Tuple[str, str], List[str]] = {}
    for table in reflected:
        target_schema = schema_map[table.schema]
        columns = []
        for column in table.columns:
            column_type = column.type
            if isinstance(column_type, Enum) and column_type.name:
                labels = list(column_type.enums)
                key = (target_schema, column_type.name)
                if key in planned and planned[key] != labels:
                    raise ValueError(
                        f"conflicting definitions for enum type "
                        f"{key[0]}.{key[1]}: {planned[key]} vs {labels}"
                    )
                planned[key] = labels
                column_type = Enum(*labels, name=column_type.name, schema=target_schema)
            columns.append(Column(column.name, column_type))
        Table(table.name, target_metadata, *columns, schema=target_schema)

    existing = _existing_enum_labels(target_engine)
    for (schema, name), labels in planned.items():
        if (schema, name) in existing and existing[(schema, name)] != labels:
            raise ValueError(
                f"enum type {schema}.{name} already exists in the target with "
                f"different candidates {existing[(schema, name)]} != {labels}"
            )

    target_metadata.create_all(target_engine)
    return target_metadata


SOURCES = [
    ("db2.archive", "orders_gamma"),
    ("db1.archive", "orders_alpha"),
    ("db1.archive", "orders_beta"),
]


def test_reflect_sources_preserve_distinct_enums(source_engine: Engine) -> None:
    # reflect the second database first: reflection order must not let one
    # database's status_t definition rewrite the other's
    reflected = reflect_sources(source_engine, SOURCES)
    by_name = {table.name: table for table in reflected}

    gamma = by_name["orders_gamma"].c.status.type
    assert isinstance(gamma, Enum)
    assert gamma.name == "status_t"
    assert gamma.enums == DB2_LABELS

    for name in ("orders_alpha", "orders_beta"):
        status = by_name[name].c.status.type
        assert isinstance(status, Enum)
        assert status.name == "status_t"
        assert status.enums == DB1_LABELS

    # the shared type of the first database rebuilds identically for both tables
    alpha = by_name["orders_alpha"].c.status.type
    beta = by_name["orders_beta"].c.status.type
    assert (alpha.name, alpha.schema, alpha.enums) == (
        beta.name,
        beta.schema,
        beta.enums,
    )

    # candidates containing commas or quotes survive reflection verbatim
    assert "needs,review" in alpha.enums
    assert 'say "hi"' in alpha.enums

    # plain string columns keep their usual semantics
    assert isinstance(by_name["orders_alpha"].c.note.type, String)


def test_migration_preserves_ordering_and_validation(
    source_engine: Engine, target_engine: Engine
) -> None:
    migrate_orders(source_engine, target_engine, SOURCES, SCHEMA_MAP)

    # close the source connections before writing any order data
    source_engine.dispose()

    # the shared type was recreated exactly once per target schema
    assert _existing_enum_labels(target_engine) == {
        ("archive_one", "status_t"): DB1_LABELS,
        ("archive_two", "status_t"): DB2_LABELS,
    }

    # re-reflect the target: declarations (and punctuation) survive the roundtrip
    target_tables = reflect_sources(
        target_engine,
        [
            ("archive_one", "orders_alpha"),
            ("archive_one", "orders_beta"),
            ("archive_two", "orders_gamma"),
        ],
    )
    by_name = {table.name: table for table in target_tables}
    assert by_name["orders_alpha"].c.status.type.enums == DB1_LABELS
    assert by_name["orders_beta"].c.status.type.enums == DB1_LABELS
    assert by_name["orders_gamma"].c.status.type.enums == DB2_LABELS
    assert isinstance(by_name["orders_alpha"].c.note.type, String)

    # insert known statuses out of order; notes exercise plain string semantics
    notes = {
        "orders_alpha": "plain, with comma",
        "orders_beta": 'quoted "note"',
        "orders_gamma": "ünïcode ✓",
    }
    with target_engine.begin() as conn:
        for table in target_tables:
            labels = table.c.status.type.enums
            for ident, status in enumerate(reversed(labels), start=1):
                conn.execute(
                    table.insert().values(
                        id=ident, status=status, note=notes[table.name]
                    )
                )

    # ordering by status follows each enum's actual declaration order
    with target_engine.connect() as conn:
        for table in target_tables:
            rows = conn.execute(
                text(
                    f"SELECT status, note FROM {table.schema}.{table.name} "
                    "ORDER BY status"
                )
            ).fetchall()
            assert [status for (status, _) in rows] == table.c.status.type.enums
            assert {note for (_, note) in rows} == {notes[table.name]}

    # each table only accepts its own enum's candidates: the other database's
    # exclusive value is rejected
    with target_engine.connect() as conn:
        with pytest.raises(DataError):
            conn.execute(
                by_name["orders_alpha"].insert().values(id=99, status="blocked")
            )
    with target_engine.connect() as conn:
        with pytest.raises(DataError):
            conn.execute(by_name["orders_gamma"].insert().values(id=99, status="done"))


def test_conflicting_target_type_detected_before_writing(
    source_engine: Engine, target_engine: Engine
) -> None:
    # a same-named type already exists in the target with a different
    # candidate order
    with target_engine.begin() as conn:
        conn.execute(
            text("CREATE TYPE archive_one.status_t AS ENUM ('done', 'ready', 'queued')")
        )

    with pytest.raises(ValueError, match="archive_one.status_t"):
        migrate_orders(source_engine, target_engine, SOURCES, SCHEMA_MAP)

    # the conflict was raised before any order table was created or written
    inspector = inspect(target_engine)
    assert inspector.get_table_names(schema="archive_one") == []
    assert inspector.get_table_names(schema="archive_two") == []
    assert _existing_enum_labels(target_engine)[("archive_one", "status_t")] == [
        "done",
        "ready",
        "queued",
    ]


def test_matching_existing_type_is_not_a_conflict(
    source_engine: Engine, target_engine: Engine
) -> None:
    migrate_orders(source_engine, target_engine, SOURCES, SCHEMA_MAP)
    # re-running the same migration against matching types succeeds
    migrate_orders(source_engine, target_engine, SOURCES, SCHEMA_MAP)
    assert _existing_enum_labels(target_engine) == {
        ("archive_one", "status_t"): DB1_LABELS,
        ("archive_two", "status_t"): DB2_LABELS,
    }

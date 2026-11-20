import decimal
import json
import os
import warnings
from typing import Any, Dict, Type
from uuid import uuid4

from packaging.version import Version
from pytest import fixture, importorskip, mark
from pytest_snapshot.plugin import Snapshot
from sqlalchemy import (
    Column,
    Integer,
    Interval,
    MetaData,
    Sequence,
    String,
    Table,
    inspect,
    schema,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session
from sqlalchemy.sql import sqltypes
from sqlalchemy.types import FLOAT, JSON

from .._supports import duckdb_version, has_uhugeint_support
from ..datatypes import Map, Struct, USmallInteger, types, parse_type


@mark.parametrize("coltype", types)
@mark.skipif(not has_uhugeint_support, reason="duckdb version too old")
def test_unsigned_integer_type(
    engine: Engine, session: Session, coltype: Type[Integer]
) -> None:
    Base = declarative_base()

    tname = "table"
    table = type(
        "Table",
        (Base,),
        {
            "__tablename__": tname,
            "id": Column(Integer, primary_key=True, default=0),
            "a": Column(coltype),
        },
    )
    Base.metadata.create_all(engine)

    has_table = (
        engine.has_table if hasattr(engine, "has_table") else inspect(engine).has_table
    )

    assert has_table(tname)

    session.add(table(a=1))
    session.commit()

    assert session.query(table).one()


@mark.remote_data()
def test_raw_json(engine: Engine) -> None:
    importorskip("duckdb", "0.9.3.dev4040")

    with engine.connect() as conn:
        assert conn.execute(text("load json"))

        assert conn.execute(text("select {'Hello': 'world'}::JSON")).fetchone() == (
            {"Hello": "world"},
        )


@mark.remote_data()
def test_custom_json_serializer() -> None:
    def default(o: Any) -> Any:
        if isinstance(o, decimal.Decimal):
            return {"__tag": "decimal", "value": str(o)}

    def object_hook(pairs: Dict[str, Any]) -> Any:
        if pairs.get("__tag", None) == "decimal":
            return decimal.Decimal(pairs["value"])
        else:
            return pairs

    engine = create_engine(
        "duckdb://",
        json_serializer=json.JSONEncoder(default=default).encode,
        json_deserializer=json.JSONDecoder(object_hook=object_hook).decode,
    )

    Base = declarative_base()

    class Entry(Base):
        __tablename__ = "test_json"
        id = Column(Integer, Sequence("id_seq"), primary_key=True)
        data = Column(JSON, nullable=False)

    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        session = Session(bind=conn)

        data = {"hello": decimal.Decimal("42")}

        session.add(Entry(data=data))  # type: ignore[call-arg]
        session.commit()

        (res,) = session.execute(select(Entry)).one()

        assert res.data == data


def test_json(engine: Engine, session: Session) -> None:
    base = declarative_base()

    class Entry(base):
        __tablename__ = "test_json"

        id = Column(Integer, primary_key=True, default=0)
        meta = Column(JSON, nullable=False)

    base.metadata.create_all(bind=engine)

    session.add(Entry(meta={"hello": "world"}))  # type: ignore[call-arg]
    session.commit()

    result = session.query(Entry).one()

    assert result.meta == {"hello": "world"}


def test_uuid(engine: Engine, session: Session) -> None:
    importorskip("duckdb", "0.7.1")
    base = declarative_base()

    class Entry(base):
        __tablename__ = "test_uuid"

        id = Column(UUID, primary_key=True, default=0)

    base.metadata.create_all(bind=engine)

    ident = uuid4()

    session.add(Entry(id=ident))  # type: ignore[call-arg]
    session.commit()

    result = session.query(Entry).one()

    assert result.id == ident


def test_double_in_sqla_v2(engine: Engine) -> None:
    with engine.begin() as con:
        con.execute(text("CREATE TABLE t (x DOUBLE)"))
        con.execute(text("INSERT INTO t VALUES (1.0), (2.0), (3.0)"))

    md = MetaData()

    t = Table("t", md, autoload_with=engine)

    with engine.begin() as con:
        con.execute(t.select())


def test_all_types_reflection(engine: Engine) -> None:
    importorskip("sqlalchemy", "1.4.0")
    importorskip("duckdb", "0.5.1")

    with warnings.catch_warnings() as capture, engine.connect() as conn:
        conn.execute(text("create table t2 as select * from test_all_types()"))
        table = Table("t2", MetaData(), autoload_with=conn)
        for col in table.columns:
            name = col.name
            if name.endswith("_enum") and duckdb_version < Version("0.7.1"):
                continue
            has_nested_shape = "struct" in name or "map" in name
            unsupported = (
                name == "union"
                or name.startswith("fixed_")
                or name == "struct_of_fixed_array"
                or ("array" in name and not has_nested_shape)
            )
            if unsupported:
                assert col.type == sqltypes.NULLTYPE, name
            else:
                assert col.type != sqltypes.NULLTYPE, name
        assert not capture


def test_nested_types(engine: Engine, session: Session) -> None:
    importorskip("duckdb", "0.5.0")  # nested types require at least duckdb 0.5.0
    base = declarative_base()

    class Entry(base):
        __tablename__ = "test_struct"

        id = Column(Integer, primary_key=True, default=0)
        struct = Column(Struct(fields={"name": String}))
        map = Column(Map(String, Integer))
        # union = Column(Union(fields={"name": String, "age": Integer}))

    base.metadata.create_all(bind=engine)

    struct_data = {"name": "Edgar"}
    map_data = {"one": 1, "two": 2}

    session.add(Entry(struct=struct_data, map=map_data))  # type: ignore[call-arg]
    session.commit()

    result = session.query(Entry).one()

    assert result.struct == struct_data
    assert result.map == map_data


def test_double_nested_types(engine: Engine, session: Session) -> None:
    """Test for https://github.com/Mause/duckdb_engine/issues/1138"""
    importorskip("duckdb", "0.5.0")  # nested types require at least duckdb 0.5.0
    base = declarative_base()

    class Entry(base):
        __tablename__ = "test_struct"

        id = Column(Integer, primary_key=True, default=0)
        outer = Column(Struct({"inner": Struct({"val": Integer})}))

    base.metadata.create_all(bind=engine)

    outer = {"inner": {"val": 42}}

    session.add(Entry(outer=outer))  # type: ignore[call-arg]
    session.commit()

    result = session.query(Entry).one()

    assert result.outer == outer


def test_interval(engine: Engine, snapshot: Snapshot) -> None:
    test_table = Table("test_table", MetaData(), Column("duration", Interval))
    create_sql = str(schema.CreateTable(test_table).compile(engine)).strip()
    snapshot.assert_match(create_sql, "schema.sql")


def test_div_is_floordiv(engine: Engine) -> None:
    test_table = Table(
        "test_table", MetaData(), Column("value", FLOAT), Column("eur2usd_rate", FLOAT)
    )

    stmt = test_table.c.value / test_table.c.eur2usd_rate

    assert str(stmt.compile(engine)) == "test_table.value / test_table.eur2usd_rate"


# A field name that contains a space, a comma and a double quote at once.
def _quote(identifier: str) -> str:
    return '"{}"'.format(identifier.replace('"', '""'))


NESTED_FIELD = 'a b,c"d'
NESTED_TYPE = f"STRUCT({_quote(NESTED_FIELD)} MAP(VARCHAR, USMALLINT[]))[]"


@fixture
def source_and_target(tmp_path: Any) -> Any:
    source = os.path.join(str(tmp_path), "source.db")
    target = os.path.join(str(tmp_path), "target.db")
    return create_engine(f"duckdb:///{source}"), create_engine(
        f"duckdb:///{target}"
    )


def _create_source(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE telemetry (
                    id INTEGER,
                    nested {NESTED_TYPE},
                    counter USMALLINT
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                INSERT INTO telemetry VALUES
                (1, []::{NESTED_TYPE}, 1),
                (2, [{{{_quote(NESTED_FIELD)}: MAP{{}}}}]::{NESTED_TYPE}, 2),
                (3, [{{{_quote(NESTED_FIELD)}: MAP{{'x': []}}}}]::{NESTED_TYPE}, 3),
                (4, [{{{_quote(NESTED_FIELD)}: MAP{{'x': NULL}}}}]::{NESTED_TYPE}, 4),
                (5, [{{{_quote(NESTED_FIELD)}: NULL}}]::{NESTED_TYPE}, 5),
                (6, [{{{_quote(NESTED_FIELD)}: MAP{{'y': [1, 40000, 65535]}}}}]
                    ::{NESTED_TYPE}, 40000)
                """
            )
        )


def test_parse_nested_type_string() -> None:
    parsed = parse_type(NESTED_TYPE)
    assert isinstance(parsed, ARRAY)
    assert parsed.dimensions == 1
    assert isinstance(parsed.item_type, Struct)
    assert list(parsed.item_type.fields) == [NESTED_FIELD]
    map_type = parsed.item_type.fields[NESTED_FIELD]
    assert isinstance(map_type, Map)
    assert isinstance(map_type.key_type, String)
    assert isinstance(map_type.value_type, ARRAY)
    assert isinstance(map_type.value_type.item_type, USmallInteger)


def test_reflected_nested_metadata_roundtrips(
    source_and_target: Any,
) -> None:
    source, target = source_and_target
    _create_source(source)

    metadata = MetaData()
    source_table = Table("telemetry", metadata, autoload_with=source)

    nested_column = source_table.columns["nested"]
    assert isinstance(nested_column.type, ARRAY)
    struct_type = nested_column.type.item_type
    assert isinstance(struct_type, Struct)
    assert list(struct_type.fields) == [NESTED_FIELD]
    map_type = struct_type.fields[NESTED_FIELD]
    assert isinstance(map_type, Map)
    assert isinstance(map_type.value_type, ARRAY)
    assert isinstance(map_type.value_type.item_type, USmallInteger)

    # Build the target table from the reflected metadata alone, with the
    # source connection closed first.
    source.dispose()

    target_metadata = MetaData()
    target_table = source_table.to_metadata(target_metadata)
    target_metadata.create_all(target)

    reflected = MetaData()
    copied_table = Table("telemetry", reflected, autoload_with=target)
    copied_type = copied_table.columns["nested"].type
    assert isinstance(copied_type, ARRAY)
    assert isinstance(copied_type.item_type, Struct)
    assert list(copied_type.item_type.fields) == [NESTED_FIELD]
    # A single field, not one per punctuation character.
    assert len(copied_type.item_type.fields) == 1


def test_nested_reflection_survives_copy(source_and_target: Any) -> None:
    source, target = source_and_target
    _create_source(source)

    metadata = MetaData()
    source_table = Table("telemetry", metadata, autoload_with=source)

    with source.connect() as conn:
        rows = [dict(row) for row in conn.execute(select(source_table)).mappings()]

    source.dispose()

    target_metadata = MetaData()
    target_table = source_table.to_metadata(target_metadata)
    target_metadata.create_all(target)

    with target.begin() as conn:
        conn.execute(target_table.insert(), rows)

    reflected = MetaData()
    copied_table = Table("telemetry", reflected, autoload_with=target)

    def describe(table: Table) -> Any:
        column = table.columns["nested"]
        struct_type = column.type.item_type
        map_type = struct_type.fields[NESTED_FIELD]
        return (
            list(struct_type.fields),
            isinstance(map_type, Map),
            isinstance(map_type.value_type, ARRAY),
            map_type.value_type.item_type.__class__.__name__,
            map_type.value_type.item_type.min,
            map_type.value_type.item_type.max,
        )

    assert describe(source_table) == describe(copied_table)

    with target.connect() as conn:
        rows = {
            row["id"]: row
            for row in conn.execute(select(copied_table)).mappings()
        }
        copied = {ident: row["nested"] for ident, row in rows.items()}

    assert copied[1] == []
    assert copied[2] == [{NESTED_FIELD: {}}]
    assert copied[3] == [{NESTED_FIELD: {"x": []}}]
    assert copied[4] == [{NESTED_FIELD: {"x": None}}]
    assert copied[5] == [{NESTED_FIELD: None}]
    assert copied[6] == [{NESTED_FIELD: {"y": [1, 40000, 65535]}}]

    # The high-valued unsigned integer survives without narrowing.
    assert rows[6]["counter"] == 40000
    assert isinstance(copied_table.columns["counter"].type, USmallInteger)


def test_metadata_reflect_reads_nested_columns(source_and_target: Any) -> None:
    source, target = source_and_target
    _create_source(source)

    metadata = MetaData()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        metadata.reflect(source)

    table = metadata.tables["telemetry"]
    nested_type = table.columns["nested"].type
    assert isinstance(nested_type, ARRAY)
    assert isinstance(nested_type.item_type, Struct)
    assert list(nested_type.item_type.fields) == [NESTED_FIELD]
    assert isinstance(nested_type.item_type.fields[NESTED_FIELD], Map)


def test_scalar_columns_keep_existing_reflection(source_and_target: Any) -> None:
    source, target = source_and_target
    with source.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE scalars (
                    id INTEGER,
                    name VARCHAR,
                    amount USMALLINT,
                    tags VARCHAR[]
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO scalars VALUES (1, 'one', 40000, ['a', 'b'])"
            )
        )
    metadata = MetaData()
    source_table = Table("scalars", metadata, autoload_with=source)
    assert isinstance(source_table.columns["id"].type, Integer)
    assert isinstance(source_table.columns["name"].type, String)
    assert isinstance(source_table.columns["amount"].type, USmallInteger)
    # Plain (non-struct/map) arrays keep their historical reflected type.
    assert isinstance(source_table.columns["tags"].type, sqltypes.NullType)

    # Scalars still flow through the same reflect/create/bind/copy chain.
    scalar_table = Table(
        "scalars_copy",
        MetaData(),
        *[
            column.copy()
            for column in source_table.columns
            if column.name != "tags"
        ],
    )
    with source.connect() as conn:
        rows = [
            {key: value for key, value in row.items() if key != "tags"}
            for row in conn.execute(select(source_table)).mappings()
        ]

    target_metadata = MetaData()
    target_table = scalar_table.to_metadata(target_metadata)
    target_metadata.create_all(target)
    with target.begin() as conn:
        conn.execute(target_table.insert(), rows)

    reflected = MetaData()
    copied_table = Table("scalars_copy", reflected, autoload_with=target)
    with target.connect() as conn:
        copied = conn.execute(select(copied_table)).mappings().one()
    assert copied["id"] == 1
    assert copied["name"] == "one"
    assert copied["amount"] == 40000
    assert isinstance(copied_table.columns["amount"].type, USmallInteger)

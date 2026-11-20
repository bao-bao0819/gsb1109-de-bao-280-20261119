import re
import warnings
from functools import lru_cache
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
)

import duckdb
import sqlalchemy
from sqlalchemy import pool, select, sql, text, util
from sqlalchemy import types as sqltypes
from sqlalchemy.dialects.postgresql import UUID, insert
from sqlalchemy.dialects.postgresql.base import (
    PGDialect,
    PGIdentifierPreparer,
    PGInspector,
    PGTypeCompiler,
)
from sqlalchemy.dialects.postgresql.psycopg2 import PGDialect_psycopg2
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.engine.interfaces import Dialect as RootDialect
from sqlalchemy.engine.reflection import cache
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import bindparam
from sqlalchemy.sql.selectable import Select

from ._supports import has_comment_support
from .config import apply_config, get_core_config
from .datatypes import ISCHEMA_NAMES, register_extension_types

__version__ = "0.17.0"
sqlalchemy_version = sqlalchemy.__version__
duckdb_version: str = duckdb.__version__
supports_attach: bool = duckdb_version >= "0.7.0"
supports_user_agent: bool = duckdb_version >= "0.9.2"

if TYPE_CHECKING:
    from sqlalchemy.base import Connection
    from sqlalchemy.engine.interfaces import _IndexDict
    from sqlalchemy.sql.type_api import _ResultProcessor

register_extension_types()


__all__ = [
    "Dialect",
    "ConnectionWrapper",
    "CursorWrapper",
    "DBAPI",
    "DuckDBEngineWarning",
    "insert",  # reexport of sqlalchemy.dialects.postgresql.insert
]


class DBAPI:
    paramstyle = "numeric_dollar" if sqlalchemy_version >= "2.0.0" else "qmark"
    apilevel = duckdb.apilevel
    threadsafety = duckdb.threadsafety

    # this is being fixed upstream to add a proper exception hierarchy
    Error = getattr(duckdb, "Error", RuntimeError)
    TransactionException = getattr(duckdb, "TransactionException", Error)
    ParserException = getattr(duckdb, "ParserException", Error)

    @staticmethod
    def Binary(x: Any) -> Any:
        return x


class DuckDBInspector(PGInspector):
    def get_check_constraints(
        self, table_name: str, schema: Optional[str] = None, **kw: Any
    ) -> List[Dict[str, Any]]:
        try:
            return super().get_check_constraints(table_name, schema, **kw)
        except Exception as e:
            raise NotImplementedError() from e


class ConnectionWrapper:
    __c: duckdb.DuckDBPyConnection
    notices: List[str]
    autocommit = None  # duckdb doesn't support setting autocommit
    closed = False

    def __init__(self, c: duckdb.DuckDBPyConnection) -> None:
        self.__c = c
        self.notices = list()

    def cursor(self) -> "CursorWrapper":
        return CursorWrapper(self.__c, self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__c, name)

    def close(self) -> None:
        self.__c.close()
        self.closed = True


class CursorWrapper:
    __c: duckdb.DuckDBPyConnection
    __connection_wrapper: "ConnectionWrapper"

    def __init__(
        self, c: duckdb.DuckDBPyConnection, connection_wrapper: "ConnectionWrapper"
    ) -> None:
        self.__c = c
        self.__connection_wrapper = connection_wrapper

    def executemany(
        self,
        statement: str,
        parameters: Optional[List[Dict]] = None,
        context: Optional[Any] = None,
    ) -> None:
        self.__c.executemany(statement, list(parameters) if parameters else [])

    def execute(
        self,
        statement: str,
        parameters: Optional[Tuple] = None,
        context: Optional[Any] = None,
    ) -> None:
        try:
            if statement.lower() == "commit":  # this is largely for ipython-sql
                self.__c.commit()
            elif statement.lower() in (
                "register",
                "register(?, ?)",
                "register($1, $2)",
            ):
                assert parameters and len(parameters) == 2, parameters
                view_name, df = parameters
                self.__c.register(view_name, df)
            elif parameters is None:
                self.__c.execute(statement)
            else:
                self.__c.execute(statement, parameters)
        except RuntimeError as e:
            if e.args[0].startswith("Not implemented Error"):
                raise NotImplementedError(*e.args) from e
            elif (
                e.args[0]
                == "TransactionContext Error: cannot commit - no transaction is active"
            ):
                return
            else:
                raise e

    @property
    def connection(self) -> "Connection":
        return self.__connection_wrapper

    def close(self) -> None:
        pass  # closing cursors is not supported in duckdb

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__c, name)

    def fetchmany(self, size: Optional[int] = None) -> List:
        if size is None:
            return self.__c.fetchmany()
        else:
            return self.__c.fetchmany(size)


class DuckDBEngineWarning(Warning):
    pass


def index_warning() -> None:
    warnings.warn(
        "duckdb-engine doesn't yet support reflection on indices",
        DuckDBEngineWarning,
    )


class DuckDBIdentifierPreparer(PGIdentifierPreparer):
    def __init__(self, dialect: "Dialect", **kwargs: Any) -> None:
        super().__init__(dialect, **kwargs)

        self.reserved_words.update(
            {
                keyword_name
                for (keyword_name,) in duckdb.cursor()
                .execute(
                    "select keyword_name from duckdb_keywords() where keyword_category == 'reserved'"
                )
                .fetchall()
            }
        )

    def _separate(self, name: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
        """
        Get database name and schema name from schema if it contains a database name
            Format:
              <db_name>.<schema_name>
              db_name and schema_name are double quoted if contains spaces or double quotes
        """
        database_name, schema_name = None, name
        if name is not None and "." in name:
            database_name, schema_name = (
                max(s) for s in re.findall(r'"([^.]+)"|([^.]+)', name)
            )
        return database_name, schema_name

    def format_schema(self, name: str) -> str:
        """Prepare a quoted schema name."""
        database_name, schema_name = self._separate(name)
        if database_name is None:
            return self.quote(name)
        return ".".join(self.quote(_n) for _n in [database_name, schema_name])

    def quote_schema(self, schema: str, force: Any = None) -> str:
        """
        Conditionally quote a schema name.

        :param schema: string schema name
        :param force: unused
        """
        return self.format_schema(schema)


class DuckDBNullType(sqltypes.NullType):
    def result_processor(
        self, dialect: RootDialect, coltype: sqltypes.TypeEngine
    ) -> Optional["_ResultProcessor"]:
        if coltype == "JSON":
            return sqltypes.JSON().result_processor(dialect, coltype)
        else:
            return super().result_processor(dialect, coltype)


_RELATION_CATALOG = """
    SELECT t.table_catalog AS database_name,
           t.table_schema AS schema_name,
           t.table_name AS relation_name,
           t.table_type AS relation_kind,
           MIN(c.oid) AS relation_oid
    FROM information_schema.tables t
    JOIN duckdb_schemas() s
      ON s.database_name = t.table_catalog
     AND s.schema_name = t.table_schema
    JOIN pg_catalog.pg_class c
      ON c.relnamespace = s.oid
     AND c.relname = t.table_name
     AND c.relkind IN ('r', 'v')
    WHERE t.table_catalog <> 'system'
      AND t.table_schema NOT LIKE 'pg\\_%' ESCAPE '\\'
    GROUP BY t.table_catalog, t.table_schema, t.table_name, t.table_type
"""

_COLUMNS_BY_OID = """
    SELECT a.attname AS name,
           pg_catalog.format_type(a.atttypid, a.atttypmod) AS format_type,
           (
             SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid)
             FROM pg_catalog.pg_attrdef d
             WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum
             AND a.atthasdef
           ) AS "default",
           a.attnotnull AS not_null,
           a.attrelid AS table_oid,
           pgd.description AS comment
    FROM pg_catalog.pg_attribute a
    LEFT JOIN pg_catalog.pg_description pgd ON (
        pgd.objoid = a.attrelid AND pgd.objsubid = a.attnum)
    WHERE a.attrelid = :table_oid
    AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
"""


def _split_identifier_list(value: Optional[str], separator: str) -> List[str]:
    """Split on ``separator`` while ignoring separators inside double quotes."""
    parts: List[str] = []
    buffer: List[str] = []
    quoted = False
    index = 0
    while value is not None and index < len(value):
        char = value[index]
        if char == '"':
            if quoted and index + 1 < len(value) and value[index + 1] == '"':
                buffer.append('"')
                index += 2
                continue
            quoted = not quoted
            index += 1
            continue
        if char == separator and not quoted:
            parts.append("".join(buffer).strip())
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer).strip())
    return [part for part in parts if part]


def _result_mappings(result: Any) -> List[Dict[str, Any]]:
    """Return mapping rows for both SQLAlchemy 1.3 and later result objects."""
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    keys = list(result.keys())
    return [dict(zip(keys, row)) for row in result.fetchall()]


def _result_scalar(connection: "Connection", statement: str) -> Any:
    result = connection.execute(text(statement))
    return result.scalar() if hasattr(result, "scalar") else result.fetchone()[0]


class Dialect(PGDialect_psycopg2):
    name = "duckdb"
    driver = "duckdb_engine"
    _has_events = False
    supports_statement_cache = False
    supports_comments = has_comment_support()
    supports_sane_rowcount = False
    supports_server_side_cursors = False
    div_is_floordiv = False  # TODO: tweak this to be based on DuckDB version
    inspector = DuckDBInspector
    colspecs = util.update_copy(
        PGDialect.colspecs,
        {
            # the psycopg2 driver registers a _PGNumeric with custom logic for
            # postgres type_codes (such as 701 for float) that duckdb doesn't have
            sqltypes.Numeric: sqltypes.Numeric,
            sqltypes.JSON: sqltypes.JSON,
            UUID: UUID,
        },
    )
    ischema_names = util.update_copy(
        PGDialect.ischema_names,
        ISCHEMA_NAMES,
    )
    preparer = DuckDBIdentifierPreparer
    identifier_preparer: DuckDBIdentifierPreparer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["use_native_hstore"] = False
        super().__init__(*args, **kwargs)

    def type_descriptor(self, typeobj: Type[sqltypes.TypeEngine]) -> Any:  # type: ignore[override]
        res = super().type_descriptor(typeobj)

        if isinstance(res, sqltypes.NullType):
            return DuckDBNullType()

        return res

    def connect(self, *cargs: Any, **cparams: Any) -> "Connection":
        core_keys = get_core_config()
        preload_extensions = cparams.pop("preload_extensions", [])
        config = dict(cparams.get("config", {}))
        cparams["config"] = config
        config.update(cparams.pop("url_config", {}))

        ext = {k: config.pop(k) for k in list(config) if k not in core_keys}
        if supports_user_agent:
            user_agent = f"duckdb_engine/{__version__}(sqlalchemy/{sqlalchemy_version})"
            if "custom_user_agent" in config:
                user_agent = f"{user_agent} {config['custom_user_agent']}"
            config["custom_user_agent"] = user_agent

        filesystems = cparams.pop("register_filesystems", [])

        conn = duckdb.connect(*cargs, **cparams)

        for extension in preload_extensions:
            conn.execute(f"LOAD {extension}")

        for filesystem in filesystems:
            conn.register_filesystem(filesystem)

        apply_config(self, conn, ext)

        return ConnectionWrapper(conn)

    def on_connect(self) -> None:
        pass

    @classmethod
    def get_pool_class(cls, url: URL) -> Type[pool.Pool]:
        if url.database == ":memory:":
            return pool.SingletonThreadPool
        else:
            return pool.QueuePool

    @staticmethod
    def dbapi(**kwargs: Any) -> Type[DBAPI]:
        return DBAPI

    def _get_server_version_info(self, connection: "Connection") -> Tuple[int, int]:
        return (8, 0)

    def get_default_isolation_level(self, connection: "Connection") -> None:
        raise NotImplementedError()

    def do_rollback(self, connection: "Connection") -> None:
        try:
            super().do_rollback(connection)
        except DBAPI.TransactionException as e:
            if (
                e.args[0]
                != "TransactionContext Error: cannot rollback - no transaction is active"
            ):
                raise e

    def do_begin(self, connection: "Connection") -> None:
        connection.begin()

    def get_view_names(
        self,
        connection: Any,
        schema: Optional[Any] = None,
        include: Optional[Any] = None,
        **kw: Any,
    ) -> Any:
        s = """
            SELECT table_name
            FROM information_schema.tables
            WHERE
                table_type='VIEW'
                AND table_schema = :schema_name
            """
        params = {}
        database_name = None

        if schema is not None:
            database_name, schema = self.identifier_preparer._separate(schema)
        else:
            schema = "main"

        params.update({"schema_name": schema})

        if database_name is not None:
            s += "AND table_catalog = :database_name\n"
            params.update({"database_name": database_name})

        rs = connection.execute(text(s), params)
        return [view for (view,) in rs]

    @cache  # type: ignore[call-arg]
    def get_schema_names(self, connection: "Connection", **kw: "Any"):  # type: ignore[no-untyped-def]
        """
        Return unquoted database_name.schema_name unless either contains spaces or double quotes.
        In that case, escape double quotes and then wrap in double quotes.
        SQLAlchemy definition of a schema includes database name for databases like SQL Server (Ex: databasename.dbo)
        (see https://docs.sqlalchemy.org/en/20/dialects/mssql.html#multipart-schema-names)
        """

        if not supports_attach:
            return super().get_schema_names(connection, **kw)

        s = """
            SELECT database_name, schema_name AS nspname
            FROM duckdb_schemas()
            WHERE schema_name NOT LIKE 'pg\\_%' ESCAPE '\\'
            ORDER BY database_name, nspname
            """
        rs = connection.execute(text(s))

        qs = self.identifier_preparer.quote_schema
        return [qs(".".join(nspname)) for nspname in rs]

    def _build_query_where(
        self,
        table_name: Optional[str] = None,
        schema_name: Optional[str] = None,
        database_name: Optional[str] = None,
    ) -> Tuple[str, Dict[str, str]]:
        sql = ""
        params = {}

        # If no database name is provided, try to get it from the schema name
        # specified as "<db name>.<schema name>"
        # If only a schema name is found, database_name will return None
        if database_name is None and schema_name is not None:
            database_name, schema_name = self.identifier_preparer._separate(schema_name)

        if table_name is not None:
            sql += "AND table_name = :table_name\n"
            params.update({"table_name": table_name})

        if schema_name is not None:
            sql += "AND schema_name = :schema_name\n"
            params.update({"schema_name": schema_name})

        if database_name is not None:
            sql += "AND database_name = :database_name\n"
            params.update({"database_name": database_name})

        return sql, params

    @cache  # type: ignore[call-arg]
    def get_table_names(self, connection: "Connection", schema=None, **kw: "Any"):  # type: ignore[no-untyped-def]
        """
        Return unquoted database_name.schema_name unless either contains spaces or double quotes.
        In that case, escape double quotes and then wrap in double quotes.
        SQLAlchemy definition of a schema includes database name for databases like SQL Server (Ex: databasename.dbo)
        (see https://docs.sqlalchemy.org/en/20/dialects/mssql.html#multipart-schema-names)
        """

        if not supports_attach:
            return super().get_table_names(connection, schema, **kw)

        s = """
            SELECT database_name, schema_name, table_name
            FROM duckdb_tables()
            WHERE schema_name NOT LIKE 'pg\\_%' ESCAPE '\\'
            """
        sql, params = self._build_query_where(schema_name=schema)
        s += sql
        rs = connection.execute(text(s), params)

        return [
            table
            for (
                db,
                sc,
                table,
            ) in rs
        ]

    def _split_schema_name(self, schema: str) -> Tuple[Optional[str], str]:
        """Split a schema argument into ``(database, schema)``.

        Handles the ``database.schema`` form as well as quoted identifiers,
        without the ambiguity of a regular expression (identifiers may contain
        dots when quoted).
        """
        parts = _split_identifier_list(schema, ".")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        return None, parts[0]

    def _relation_catalog(self, connection: "Connection") -> List[Dict[str, Any]]:
        return _result_mappings(connection.execute(text(_RELATION_CATALOG)))

    def _search_path_pairs(
        self, connection: "Connection"
    ) -> List[Tuple[str, str]]:
        """Return the connection's default resolution pairs, in order.

        DuckDB resolves an unqualified table name by looking in the temporary
        schema first, then through the connection's live ``search_path``, and
        finally in ``main`` of the connection's current database.  The
        connection's current database is read here rather than captured when
        the inspector was created, so it follows ``USE`` / another connection.
        """
        current_database = _result_scalar(connection, "SELECT current_database()")
        raw = _result_scalar(connection, "SELECT current_setting('search_path')")

        pairs: List[Tuple[str, str]] = [("temp", "main")]
        for entry in _split_identifier_list(raw or "", ","):
            database_name, schema_name = self._split_schema_name(entry)
            pairs.append(
                (
                    database_name if database_name is not None else current_database,
                    schema_name,
                )
            )
        pairs.append((current_database, "main"))

        deduplicated: List[Tuple[str, str]] = []
        for pair in pairs:
            if pair not in deduplicated:
                deduplicated.append(pair)
        return deduplicated

    def _schema_pairs(
        self, connection: "Connection", schema: str
    ) -> List[Tuple[str, str]]:
        """Return resolution pairs for a bare schema name.

        DuckDB resolves ``schema.table`` against the temporary schema, then
        any database on the search path that has that schema (in path order),
        and finally the current database.
        """
        current_database = _result_scalar(connection, "SELECT current_database()")
        raw = _result_scalar(connection, "SELECT current_setting('search_path')")

        pairs: List[Tuple[str, str]] = [("temp", schema)]
        for entry in _split_identifier_list(raw or "", ","):
            database_name, schema_name = self._split_schema_name(entry)
            if schema_name == schema:
                pairs.append(
                    (
                        database_name
                        if database_name is not None
                        else current_database,
                        schema,
                    )
                )
        pairs.append((current_database, schema))

        deduplicated: List[Tuple[str, str]] = []
        for pair in pairs:
            if pair not in deduplicated:
                deduplicated.append(pair)
        return deduplicated

    def _resolve_relation(
        self,
        connection: "Connection",
        table_name: str,
        schema: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Resolve ``table_name`` exactly as an unqualified/qualified query would.

        ``schema=None`` follows the connection's temporary-schema and
        ``search_path`` resolution.  ``database.schema`` restricts the lookup to
        that database; a bare schema follows DuckDB's schema lookup, which
        prefers the temporary schema and the current database.
        """
        catalog = self._relation_catalog(connection)

        if schema is not None:
            database_name, schema_name = self._split_schema_name(schema)
            if database_name is not None:
                pairs = [(database_name, schema_name)]
            else:
                pairs = self._schema_pairs(connection, schema_name)
        else:
            pairs = self._search_path_pairs(connection)

        for pair in pairs:
            for relation in catalog:
                if (
                    relation["database_name"],
                    relation["schema_name"],
                ) == pair and relation["relation_name"] == table_name:
                    return relation

        if schema is None:
            # Preserve reflection of a unique relation that is not on the
            # connection's search path, but never guess between same-named
            # relations in different catalogs.
            matches = [
                relation
                for relation in catalog
                if relation["relation_name"] == table_name
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    def _load_columns_by_oid(
        self, connection: "Connection", table_oid: int
    ) -> List[Dict[str, Any]]:
        return _result_mappings(
            connection.execute(text(_COLUMNS_BY_OID), {"table_oid": table_oid})
        )

    def _column_domains_and_enums(
        self, connection: "Connection"
    ) -> Tuple[Dict[Any, Any], Dict[Any, Any]]:
        enums = dict(
            ((rec["name"],), rec)
            if rec["visible"]
            else ((rec["schema"], rec["name"]), rec)
            for rec in self._load_enums(connection, schema="*")  # type: ignore[attr-defined]
        )
        # DuckDB has no pg_collation/pg_type domain catalog for SQLAlchemy's
        # domain query, so domains are intentionally left empty (as before).
        return {}, enums

    def _columns_for_relation(
        self,
        connection: "Connection",
        relation: Dict[str, Any],
        table_name: str,
        schema: Optional[str],
    ) -> List[Dict[str, Any]]:
        rows = self._load_columns_by_oid(connection, relation["relation_oid"])
        if not rows:
            return []
        domains, enums = self._column_domains_and_enums(connection)

        if sqlalchemy_version >= "2.0":
            reflection_rows = [
                {
                    "name": row["name"],
                    "format_type": row["format_type"],
                    "default": row["default"],
                    "not_null": row["not_null"],
                    "table_name": table_name,
                    "comment": row["comment"],
                    "generated": None,
                    "identity_options": None,
                    "collation": None,
                }
                for row in rows
            ]
            columns = self._get_columns_info(  # type: ignore[attr-defined]
                reflection_rows, domains, enums, schema
            )
            return list(columns[(schema, table_name)])

        return [
            self._get_column_info(
                row["name"],
                row["format_type"],
                row["default"],
                row["not_null"],
                domains,
                enums,
                schema,
                row["comment"],
                None,
                None,
            )
            for row in rows
        ]

    @cache  # type: ignore[call-arg]
    def get_columns(  # type: ignore[no-untyped-def]
        self,
        connection: "Connection",
        table_name: str,
        schema: "Optional[str]" = None,
        **kw: "Any",
    ):
        if not supports_attach:
            return super().get_columns(connection, table_name, schema, **kw)

        relation = self._resolve_relation(connection, table_name, schema)
        if relation is None:
            raise NoSuchTableError(table_name)

        return self._columns_for_relation(
            connection, relation, table_name, schema
        )

    def get_multi_columns(  # type: ignore[no-untyped-def]
        self,
        connection: "Connection",
        schema: "Optional[str]" = None,
        filter_names: "Optional[Collection[str]]" = None,
        scope: "Optional[str]" = None,
        kind: "Optional[Tuple[str, ...]]" = None,
        **kw: Any,
    ) -> List:
        """Reflect columns for several names through the same resolution."""
        if not supports_attach:
            return super().get_multi_columns(  # type: ignore[misc]
                connection,
                schema=schema,
                filter_names=filter_names,
                scope=scope,
                kind=kind,
                **kw,
            )

        catalog = self._relation_catalog(connection)
        if filter_names:
            names = list(filter_names)
        else:
            # No filter: the default scope is every relation visible through
            # the connection's current resolution pairs.
            if schema is not None:
                database_name, schema_name = self._split_schema_name(schema)
                if database_name is not None:
                    pairs = [(database_name, schema_name)]
                else:
                    pairs = self._schema_pairs(connection, schema_name)
            else:
                pairs = self._search_path_pairs(connection)
            names = []
            for pair in pairs:
                for relation in catalog:
                    if (
                        relation["database_name"],
                        relation["schema_name"],
                    ) == pair and relation["relation_name"] not in names:
                        names.append(relation["relation_name"])

        result = []
        for name in names:
            relation = self._resolve_relation(connection, name, schema)
            if relation is None:
                continue
            result.append(
                (
                    (schema, name),
                    self._columns_for_relation(
                        connection, relation, name, schema
                    ),
                )
            )
        return result

    @cache  # type: ignore[call-arg]
    def get_table_oid(  # type: ignore[no-untyped-def]
        self,
        connection: "Connection",
        table_name: str,
        schema: "Optional[str]" = None,
        **kw: "Any",
    ):
        """Fetch the oid for the relation the connection would actually use.

        The schema name can be formatted either as database.schema or just the
        schema name.  Without a schema, resolution follows the connection's
        temporary schema and search path rather than picking an arbitrary
        catalog entry.
        """
        if not supports_attach:
            return super().get_table_oid(connection, table_name, schema, **kw)

        relation = self._resolve_relation(connection, table_name, schema)
        if relation is None:
            raise NoSuchTableError(table_name)
        return relation["relation_oid"]

    def has_table(
        self,
        connection: "Connection",
        table_name: str,
        schema: Optional[str] = None,
        **kw: Any,
    ) -> bool:
        try:
            return self.get_table_oid(connection, table_name, schema) is not None
        except NoSuchTableError:
            return False

    def get_indexes(
        self,
        connection: "Connection",
        table_name: str,
        schema: Optional[str] = None,
        **kw: Any,
    ) -> List["_IndexDict"]:
        index_warning()
        return []

    # the following methods are for SQLA2 compatibility
    def get_multi_indexes(
        self,
        connection: "Connection",
        schema: Optional[str] = None,
        filter_names: Optional[Collection[str]] = None,
        **kw: Any,
    ) -> Iterable[Tuple]:
        index_warning()
        return []

    def initialize(self, connection: "Connection") -> None:
        DefaultDialect.initialize(self, connection)

    def create_connect_args(self, url: URL) -> Tuple[tuple, dict]:
        opts = url.translate_connect_args(database="database")
        opts["url_config"] = dict(url.query)
        user = opts["url_config"].pop("user", None)
        if user is not None:
            opts["database"] += f"?user={user}"
        return (), opts

    @classmethod
    def import_dbapi(cls: Type["Dialect"]) -> Type[DBAPI]:
        return cls.dbapi()

    def do_executemany(
        self, cursor: Any, statement: Any, parameters: Any, context: Optional[Any] = ...
    ) -> None:
        return DefaultDialect.do_executemany(
            self, cursor, statement, parameters, context
        )

    def _pg_class_filter_scope_schema(
        self,
        query: Select,
        schema: str,
        scope: Any,
        pg_class_table: Any = None,
    ) -> Any:
        # Don't scope by schema for now
        if hasattr(super(), "_pg_class_filter_scope_schema"):
            query = getattr(super(), "_pg_class_filter_scope_schema")(
                query, schema=None, scope=scope, pg_class_table=pg_class_table
            )
            if schema is not None:
                # Now let's scope by schema, but make sure we're not adding in the database name prefix
                # This will not work if a schema or table name is not unique!
                _, schema_name = self.identifier_preparer._separate(schema)
                query = query.where(
                    text("pg_namespace.nspname = :schema_name").bindparams(
                        schema_name=schema_name
                    )
                )
            return query

    # fix for https://github.com/Mause/duckdb_engine/issues/1128
    # (Overrides sqlalchemy method)
    @lru_cache()
    def _comment_query(  # type: ignore[no-untyped-def]
        self, schema: str, has_filter_names: bool, scope: Any, kind: Any
    ):
        if sqlalchemy.__version__ >= "2.0.36":
            from sqlalchemy.dialects.postgresql import (  # type: ignore[attr-defined]
                pg_catalog,
            )

            if (
                hasattr(super(), "_kind_to_relkinds")
                and hasattr(super(), "_pg_class_filter_scope_schema")
                and hasattr(super(), "_pg_class_relkind_condition")
            ):
                relkinds = getattr(super(), "_kind_to_relkinds")(kind)
                query = (
                    select(
                        pg_catalog.pg_class.c.relname,
                        pg_catalog.pg_description.c.description,
                    )
                    .select_from(pg_catalog.pg_class)
                    .outerjoin(
                        pg_catalog.pg_description,
                        sql.and_(
                            pg_catalog.pg_class.c.oid
                            == pg_catalog.pg_description.c.objoid,
                            pg_catalog.pg_description.c.objsubid == 0,
                        ),
                    )
                    .where(getattr(super(), "_pg_class_relkind_condition")(relkinds))
                )
                query = self._pg_class_filter_scope_schema(query, schema, scope)
                if has_filter_names:
                    query = query.where(
                        pg_catalog.pg_class.c.relname.in_(bindparam("filter_names"))
                    )
                return query
        else:
            if hasattr(super(), "_comment_query"):
                return getattr(super(), "_comment_query")(
                    schema, has_filter_names, scope, kind
                )


if sqlalchemy.__version__ >= "2.0.14":
    from sqlalchemy import TryCast  # type: ignore[attr-defined]

    @compiles(TryCast, "duckdb")  # type: ignore[misc]
    def visit_try_cast(
        instance: TryCast,
        compiler: PGTypeCompiler,
        **kw: Any,
    ) -> str:
        return "TRY_CAST({} AS {})".format(
            compiler.process(instance.clause, **kw),
            compiler.process(instance.typeclause, **kw),
        )

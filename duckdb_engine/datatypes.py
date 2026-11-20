"""
See https://duckdb.org/docs/sql/data_types/numeric for more information

Also
```sql
select * from duckdb_types where type_category = 'NUMERIC';
```
"""

import re
import typing
from typing import Any, Callable, Dict, List, Optional, Type

import duckdb
from packaging.version import Version
from sqlalchemy import exc
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.dialects.postgresql.base import (
    PGDialect,
    PGIdentifierPreparer,
    PGTypeCompiler,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import sqltypes, type_api
from sqlalchemy.sql.type_api import TypeEngine
from sqlalchemy.types import BigInteger, Integer, SmallInteger, String

# INTEGER	INT4, INT, SIGNED	-2147483648	2147483647
# SMALLINT	INT2, SHORT	-32768	32767
# BIGINT	INT8, LONG	-9223372036854775808	9223372036854775807
(BigInteger, SmallInteger)  # pure reexport

duckdb_version = duckdb.__version__

IS_GT_1 = Version(duckdb_version) > Version("1.0.0")


class UInt64(Integer):
    pass


class UInt32(Integer):
    pass


class UInt16(Integer):
    "AKA USMALLINT"


class UInt8(Integer):
    pass


class UTinyInteger(Integer):
    "AKA UInt1"

    name = "UTinyInt"
    # UTINYINT	-	0	255


class TinyInteger(Integer):
    "AKA Int1"

    name = "TinyInt"
    # TINYINT	INT1	-128	127


class USmallInteger(Integer):
    name = "usmallint"
    "AKA UInt2"
    # USMALLINT	-	0	65535
    min = 0
    max = 2**16 - 1


class UBigInteger(Integer):
    name = "UBigInt"
    min = 0
    max = 18446744073709551615


class HugeInteger(Integer):
    name = "HugeInt"
    # HUGEINT	 	-170141183460469231731687303715884105727*	170141183460469231731687303715884105727


class UHugeInteger(Integer):
    name = "UHugeInt"


class UInteger(Integer):
    # UINTEGER	-	0	4294967295
    pass


if IS_GT_1:

    class VarInt(Integer):
        pass


def compile_uint(element: Integer, compiler: PGTypeCompiler, **kw: Any) -> str:
    return getattr(element, "name", type(element).__name__)


types = [
    subclass
    for subclass in Integer.__subclasses__()
    if subclass.__module__ == UInt64.__module__
]
assert types


TV = typing.Union[Type[TypeEngine], TypeEngine]


def _bind_processor(
    type_: TV, dialect: Dialect
) -> Callable[[Any], Any]:
    impl = type_api.to_instance(type_).dialect_impl(dialect)
    processor = impl.bind_processor(dialect)
    return processor if processor is not None else lambda value: value


def _result_processor(
    type_: TV, dialect: Dialect, coltype: str
) -> Callable[[Any], Any]:
    impl = type_api.to_instance(type_).dialect_impl(dialect)
    processor = impl.result_processor(dialect, coltype)
    return processor if processor is not None else lambda value: value


class Struct(TypeEngine):
    """
    Represents a STRUCT type in DuckDB

    ```python
    from duckdb_engine.datatypes import Struct
    from sqlalchemy import Table, Column, String

    Table(
        'hello',
        Column('name', Struct({'first': String, 'last': String})
    )
    ```

    :param fields: only optional due to limitations with how much type information DuckDB returns to us in the description field
    """

    __visit_name__ = "struct"

    def __init__(self, fields: Optional[Dict[str, TV]] = None):
        self.fields = fields

    def bind_processor(
        self, dialect: Dialect
    ) -> Optional[Callable[[Optional[dict]], Optional[dict]]]:
        if self.fields is None:
            return None

        processors = {
            name: _bind_processor(field_type, dialect)
            for name, field_type in self.fields.items()
        }

        def process(value: Optional[dict]) -> Optional[dict]:
            if value is None:
                return None
            return {
                name: processors[name](field_value)
                if name in processors
                else field_value
                for name, field_value in value.items()
            }

        return process

    def result_processor(
        self, dialect: Dialect, coltype: str
    ) -> Optional[Callable[[Optional[dict]], Optional[dict]]]:
        if not self.fields:
            return None

        processors = {
            name: _result_processor(field_type, dialect, coltype)
            for name, field_type in self.fields.items()
        }

        def process(value: Optional[dict]) -> Optional[dict]:
            if value is None:
                return None
            return {
                name: processors[name](field_value)
                if name in processors
                else field_value
                for name, field_value in value.items()
            }

        return process


class Map(TypeEngine):
    """
    Represents a MAP type in DuckDB

    ```python
    from duckdb_engine.datatypes import Map
    from sqlalchemy import Table, Column, String

    Table(
        'hello',
        Column('name', Map(String, String)
    )
    ```
    """

    __visit_name__ = "map"
    key_type: TV
    value_type: TV

    def __init__(self, key_type: TV, value_type: TV):
        self.key_type = key_type
        self.value_type = value_type

    def bind_processor(
        self, dialect: Dialect
    ) -> Optional[Callable[[Optional[dict]], Optional[dict]]]:
        key_processor = _bind_processor(self.key_type, dialect)
        value_processor = _bind_processor(self.value_type, dialect)

        def process(value: Optional[dict]) -> Optional[dict]:
            if value is None:
                return None
            return {
                "key": [key_processor(key) for key in value],
                "value": [value_processor(item) for item in value.values()],
            }

        return process

    def result_processor(
        self, dialect: Dialect, coltype: str
    ) -> Optional[Callable[[Optional[dict]], Optional[dict]]]:
        key_processor = _result_processor(self.key_type, dialect, coltype)
        value_processor = _result_processor(self.value_type, dialect, coltype)

        if IS_GT_1:
            # duckdb >= 1.0 returns maps as python dictionaries already
            def process(value: Optional[dict]) -> Optional[dict]:
                if value is None:
                    return None
                return {
                    key_processor(key): value_processor(item)
                    for key, item in value.items()
                }

        else:

            def process(value: Optional[dict]) -> Optional[dict]:
                if value is None:
                    return None
                return {
                    key_processor(key): value_processor(item)
                    for key, item in zip(value["key"], value["value"])
                }

        return process


class Union(TypeEngine):
    """
    Represents a UNION type in DuckDB

    ```python
    from duckdb_engine.datatypes import Union
    from sqlalchemy import Table, Column, String

    Table(
        'hello',
        Column('name', Union({"name": String, "age": String})
    )
    ```
    """

    __visit_name__ = "union"
    fields: Dict[str, TV]

    def __init__(self, fields: Dict[str, TV]):
        self.fields = fields


ISCHEMA_NAMES = {
    "hugeint": HugeInteger,
    "uhugeint": UHugeInteger,
    "tinyint": TinyInteger,
    "utinyint": UTinyInteger,
    "int8": BigInteger,
    "int4": Integer,
    "int2": SmallInteger,
    "timetz": sqltypes.TIME,
    "timestamptz": sqltypes.TIMESTAMP,
    "float4": sqltypes.FLOAT,
    "float8": sqltypes.FLOAT,
    "usmallint": USmallInteger,
    "uinteger": UInteger,
    "ubigint": UBigInteger,
    "timestamp_s": sqltypes.TIMESTAMP,
    "timestamp_ms": sqltypes.TIMESTAMP,
    "timestamp_ns": sqltypes.TIMESTAMP,
    "enum": sqltypes.Enum,
    "bool": sqltypes.BOOLEAN,
    "varchar": String,
}
if IS_GT_1:
    ISCHEMA_NAMES["varint"] = VarInt


# Names as DuckDB spells them in `duckdb_columns().data_type`, which differs
# from the PostgreSQL `format_type()` names used for scalar reflection.
DUCKDB_SCALAR_NAMES: Dict[str, TV] = {
    "boolean": sqltypes.BOOLEAN,
    "tinyint": TinyInteger,
    "smallint": SmallInteger,
    "integer": Integer,
    "bigint": BigInteger,
    "hugeint": HugeInteger,
    "uhugeint": UHugeInteger,
    "utinyint": UTinyInteger,
    "usmallint": USmallInteger,
    "uinteger": UInteger,
    "ubigint": UBigInteger,
    "date": sqltypes.DATE,
    "time": sqltypes.TIME,
    "timestamp": sqltypes.TIMESTAMP,
    "timestamp_s": sqltypes.TIMESTAMP,
    "timestamp_ms": sqltypes.TIMESTAMP,
    "timestamp_ns": sqltypes.TIMESTAMP,
    "time with time zone": sqltypes.TIME,
    "timestamp with time zone": sqltypes.TIMESTAMP,
    "float": sqltypes.FLOAT,
    "double": sqltypes.FLOAT,
    "real": sqltypes.FLOAT,
    "uuid": UUID,
    "interval": sqltypes.Interval,
    "varchar": String,
    "blob": sqltypes.LargeBinary,
    "bit": sqltypes.BINARY,
    "json": sqltypes.JSON,
}
if IS_GT_1:
    DUCKDB_SCALAR_NAMES["varint"] = VarInt


def _scalar_type(name: str) -> Optional[TypeEngine]:
    lowered = " ".join(name.lower().split())
    if lowered in DUCKDB_SCALAR_NAMES:
        return type_api.to_instance(DUCKDB_SCALAR_NAMES[lowered])
    if lowered in ISCHEMA_NAMES:
        return type_api.to_instance(ISCHEMA_NAMES[lowered])
    if lowered in PGDialect.ischema_names:
        return type_api.to_instance(PGDialect.ischema_names[lowered])
    return None


class _TypeParser:
    """Parse DuckDB's canonical type strings, e.g. ``STRUCT(...)[][]``."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def parse(self) -> TypeEngine:
        result = self._parse_type()
        self._skip_space()
        if self.pos != len(self.text):
            raise ValueError(f"unexpected trailing input in type {self.text!r}")
        return result

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _skip_space(self) -> None:
        while self._peek().isspace():
            self.pos += 1

    def _consume(self, expected: str) -> None:
        self._skip_space()
        if not self.text.startswith(expected, self.pos):
            raise ValueError(
                f"expected {expected!r} at {self.pos} in type {self.text!r}"
            )
        self.pos += len(expected)

    def _parse_identifier(self) -> str:
        self._skip_space()
        if self._peek() == '"':
            self.pos += 1
            parts: List[str] = []
            while True:
                if self.pos >= len(self.text):
                    raise ValueError(f"unterminated identifier in {self.text!r}")
                char = self.text[self.pos]
                if char == '"':
                    if self.text.startswith('""', self.pos):
                        parts.append('"')
                        self.pos += 2
                        continue
                    self.pos += 1
                    return "".join(parts)
                parts.append(char)
                self.pos += 1

        start = self.pos
        while True:
            char = self._peek()
            if not char or char.isspace() or char in "(,)[:":
                break
            self.pos += 1
        if start == self.pos:
            raise ValueError(f"expected identifier at {self.pos} in {self.text!r}")
        return self.text[start : self.pos]

    def _parse_type(self) -> TypeEngine:
        self._skip_space()
        upper = self.text[self.pos :].upper()
        if upper.startswith("STRUCT("):
            self._consume("STRUCT(")
            fields = self._parse_fields()
            self._consume(")")
            result: TypeEngine = Struct(fields)
        elif upper.startswith("UNION("):
            self._consume("UNION(")
            fields = self._parse_fields()
            self._consume(")")
            result = Union(fields)
        elif upper.startswith("MAP("):
            self._consume("MAP(")
            key_type = self._parse_type()
            self._consume(",")
            value_type = self._parse_type()
            self._consume(")")
            result = Map(key_type, value_type)
        elif upper.startswith("LIST("):
            self._consume("LIST(")
            item_type = self._parse_type()
            self._consume(")")
            result = ARRAY(item_type)
        else:
            result = self._parse_scalar()

        dimensions = 0
        while True:
            self._skip_space()
            if self._peek() != "[":
                break
            self.pos += 1
            self._skip_space()
            if self._peek() != "]":
                raise ValueError(f"fixed-size array is unsupported in {self.text!r}")
            self.pos += 1
            dimensions += 1
        if dimensions:
            if isinstance(result, ARRAY):
                result = ARRAY(result.item_type, dimensions=dimensions + 1)
            else:
                result = ARRAY(result, dimensions=dimensions)
        return result

    def _parse_fields(self) -> Dict[str, TypeEngine]:
        fields: Dict[str, TypeEngine] = {}
        while True:
            self._skip_space()
            if self._peek() == ")":
                break
            name = self._parse_identifier()
            fields[name] = self._parse_type()
            self._skip_space()
            if self._peek() == ",":
                self.pos += 1
                continue
            break
        return fields

    def _parse_scalar(self) -> TypeEngine:
        start = self.pos
        depth = 0
        quoted = False
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if quoted:
                if char == "'":
                    if self.text.startswith("''", self.pos):
                        self.pos += 1
                    else:
                        quoted = False
            elif char == "'":
                quoted = True
            elif char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char == "," and depth == 0:
                break
            elif char == "[" and depth == 0:
                break
            self.pos += 1
        name = self.text[start : self.pos].strip()
        if not name:
            raise ValueError(f"expected type at {start} in {self.text!r}")

        match = re.fullmatch(r"(?i)(decimal|numeric)\s*\((.*)\)", name)
        if match:
            args = [int(part) for part in re.findall(r"\d+", match.group(2))]
            return sqltypes.Numeric(*args)

        match = re.fullmatch(r"(?i)enum\s*\((.*)\)", name, re.DOTALL)
        if match:
            labels = re.findall(r"'((?:[^']|'')*)'", match.group(1))
            return sqltypes.Enum(*[label.replace("''", "'") for label in labels])

        match = re.fullmatch(r"(?i)varchar\s*\((\d+)\)", name)
        if match:
            return String(int(match.group(1)))

        scalar = _scalar_type(name)
        if scalar is None:
            raise ValueError(f"unknown DuckDB type {name!r} in {self.text!r}")
        return scalar


def parse_type(text: str) -> TypeEngine:
    """Parse a DuckDB type string into a SQLAlchemy type.

    Raises :class:`ValueError` for type shapes that cannot be represented,
    such as fixed-size arrays.
    """

    return _TypeParser(text).parse()


def register_extension_types() -> None:
    for subclass in types:
        compiles(subclass, "duckdb")(compile_uint)


@compiles(Struct, "duckdb")  # type: ignore[misc]
def visit_struct(
    instance: Struct,
    compiler: PGTypeCompiler,
    identifier_preparer: PGIdentifierPreparer,
    **kw: Any,
) -> str:
    return "STRUCT" + struct_or_union(instance, compiler, identifier_preparer, **kw)


@compiles(Union, "duckdb")  # type: ignore[misc]
def visit_union(
    instance: Union,
    compiler: PGTypeCompiler,
    identifier_preparer: PGIdentifierPreparer,
    **kw: Any,
) -> str:
    return "UNION" + struct_or_union(instance, compiler, identifier_preparer, **kw)


def struct_or_union(
    instance: typing.Union[Union, Struct],
    compiler: PGTypeCompiler,
    identifier_preparer: PGIdentifierPreparer,
    **kw: Any,
) -> str:
    fields = instance.fields
    if fields is None:
        raise exc.CompileError(f"DuckDB {repr(instance)} type requires fields")
    return "({})".format(
        ", ".join(
            "{} {}".format(
                identifier_preparer.quote_identifier(key),
                process_type(
                    value, compiler, identifier_preparer=identifier_preparer, **kw
                ),
            )
            for key, value in fields.items()
        )
    )


def process_type(
    value: typing.Union[TypeEngine, Type[TypeEngine]],
    compiler: PGTypeCompiler,
    **kw: Any,
) -> str:
    return compiler.process(type_api.to_instance(value), **kw)


@compiles(Map, "duckdb")  # type: ignore[misc]
def visit_map(instance: Map, compiler: PGTypeCompiler, **kw: Any) -> str:
    return "MAP({}, {})".format(
        process_type(instance.key_type, compiler, **kw),
        process_type(instance.value_type, compiler, **kw),
    )

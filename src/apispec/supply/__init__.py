"""Supply interface contracts and version-aware record routing.

This subpackage generates an APISpec field contract for the supply interface
and routes incoming JSON records into the table matching the format declared
by their top-level ``format`` field.
"""

from __future__ import annotations

from .contract import (
    OPENAPI_VERSION,
    RECORD_V1,
    RECORD_V2,
    SPEC_TITLE,
    SPEC_VERSION,
    SupplyRecordV1Schema,
    SupplyRecordV2Schema,
    build_spec,
    register_supply_schemas,
)
from .routing import (
    TABLE_NAMES,
    VERSION_SCHEMAS,
    RoutedRecord,
    SupplyRouter,
    UnknownFormatError,
)
from .schemas import (
    FORMAT_V1,
    FORMAT_V2,
    SupplyDetailsV1Schema,
    SupplyDetailsV2Schema,
    SupplyItemV1Schema,
    SupplyItemV2Schema,
)

__all__ = [
    "FORMAT_V1",
    "FORMAT_V2",
    "OPENAPI_VERSION",
    "RECORD_V1",
    "RECORD_V2",
    "SPEC_TITLE",
    "SPEC_VERSION",
    "TABLE_NAMES",
    "VERSION_SCHEMAS",
    "RoutedRecord",
    "SupplyDetailsV1Schema",
    "SupplyDetailsV2Schema",
    "SupplyItemV1Schema",
    "SupplyItemV2Schema",
    "SupplyRecordV1Schema",
    "SupplyRecordV2Schema",
    "SupplyRouter",
    "UnknownFormatError",
    "build_spec",
    "register_supply_schemas",
]

"""Route supply records into the table matching their declared ``format``.

A record is only accepted by the schema family matching its top-level
``format`` discriminator. The selected Marshmallow declaration validates and
deserializes the payload before anything is written to a table, so suppliers
can run the same contract as a pre-import check.

Stored rows keep the public wire representation (real ``wire_key`` names),
not the Python attribute names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from marshmallow import ValidationError

from .schemas import (
    FORMAT_V1,
    FORMAT_V2,
    SupplyRecordV1Schema,
    SupplyRecordV2Schema,
)

VERSION_SCHEMAS = {
    FORMAT_V1: SupplyRecordV1Schema,
    FORMAT_V2: SupplyRecordV2Schema,
}

TABLE_NAMES = {
    FORMAT_V1: "supply_records_v1",
    FORMAT_V2: "supply_records_v2",
}


class UnknownFormatError(ValueError):
    """Raised when a record has no supported top-level ``format``."""


@dataclass(frozen=True)
class RoutedRecord:
    """A validated record bound to its target table."""

    format: str
    table: str
    data: dict
    wire: dict


class SupplyRouter:
    """Validate records against their version contract and route them."""

    def __init__(self) -> None:
        self.tables: dict[str, list[RoutedRecord]] = {
            FORMAT_V1: [],
            FORMAT_V2: [],
        }

    def resolve_version(self, record: Mapping) -> str:
        """Return the declared format or raise if it is missing/unsupported."""
        format_value = record.get("format")
        if format_value not in VERSION_SCHEMAS:
            supported = ", ".join(sorted(VERSION_SCHEMAS))
            raise UnknownFormatError(
                f"Unsupported record format {format_value!r}; expected one of {supported}."
            )
        return format_value

    def validate(self, record: Mapping) -> dict:
        """Validate a record against its declared version.

        :raises UnknownFormatError: if ``format`` is missing or unsupported.
        :raises marshmallow.ValidationError: with messages located at the exact
            nested array item when a required nested field is invalid.
        """
        format_value = self.resolve_version(record)
        schema = VERSION_SCHEMAS[format_value]()
        return schema.load(record)

    def route(self, record: Mapping) -> RoutedRecord:
        """Validate a record and append it to its version's table."""
        format_value = self.resolve_version(record)
        schema = VERSION_SCHEMAS[format_value]()
        data = schema.load(record)
        wire = schema.dump(data)
        row = RoutedRecord(
            format=format_value,
            table=TABLE_NAMES[format_value],
            data=data,
            wire=wire,
        )
        self.tables[format_value].append(row)
        return row


__all__ = [
    "TABLE_NAMES",
    "VERSION_SCHEMAS",
    "RoutedRecord",
    "SupplyRouter",
    "UnknownFormatError",
    "ValidationError",
]

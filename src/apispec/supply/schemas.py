"""Marshmallow declarations for the two supply record formats.

The top-level ``format`` field discriminates the schema family. V1 items carry
the article code as a string under the wire key ``wire_key``. V2 items carry an
integer batch under ``batch_number`` and additionally require the article code
under ``wire_key``. Both versions expose a single optional ``details``
sub-object: omitting it applies the version's own default while an explicit
JSON ``null`` deserializes to ``None``.
"""

from __future__ import annotations

from marshmallow import Schema, fields, validate

FORMAT_V1 = "v1"
FORMAT_V2 = "v2"


class SupplyDetailsV1Schema(Schema):
    tolerance = fields.Integer(load_default=5)
    note = fields.String(load_default="v1-note")


class SupplyDetailsV2Schema(Schema):
    tolerance = fields.Integer(load_default=10)


class SupplyItemV1Schema(Schema):
    article_code = fields.String(
        data_key="wire_key",
        required=True,
        metadata={"description": "Supplier article code (string)."},
    )
    priority = fields.Integer(load_default=10)


class SupplyItemV2Schema(Schema):
    batch_number = fields.Integer(
        required=True,
        metadata={"description": "Batch number (integer)."},
    )
    article_code = fields.String(
        data_key="wire_key",
        required=True,
        metadata={"description": "Supplier article code (string)."},
    )
    priority = fields.Integer(load_default=20)


def _details_v1_default() -> dict:
    return {"tolerance": 5, "note": "v1-note"}


def _details_v2_default() -> dict:
    return {"tolerance": 10}


class SupplyRecordV1Schema(Schema):
    format = fields.String(
        required=True,
        validate=validate.Equal(FORMAT_V1),
        metadata={"description": "Record format discriminator."},
    )
    items = fields.List(fields.Nested(SupplyItemV1Schema), required=True)
    details = fields.Nested(
        SupplyDetailsV1Schema,
        allow_none=True,
        load_default=_details_v1_default,
    )


class SupplyRecordV2Schema(Schema):
    format = fields.String(
        required=True,
        validate=validate.Equal(FORMAT_V2),
        metadata={"description": "Record format discriminator."},
    )
    items = fields.List(fields.Nested(SupplyItemV2Schema), required=True)
    details = fields.Nested(
        SupplyDetailsV2Schema,
        allow_none=True,
        load_default=_details_v2_default,
    )

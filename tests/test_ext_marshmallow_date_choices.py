"""Regression tests for date choices rendered in each field's wire format.

A settlement batch carries two date fields at the leaves of a nested array.
``OneOf``/``Equal`` validators compare internal ``datetime.date`` objects, but
the JSON contract must list the choices in the exact string format the field
deserializes. One field reads ISO dates (``2024-02-29``), the other reads
slash dates (``29/02/2024``), and only the latter allows ``null``.

The generated OpenAPI 3.0 and 3.1 contracts are cross-checked against payloads
Marshmallow actually loads.
"""

import datetime as dt

import pytest
from marshmallow import Schema, ValidationError, fields, validate
from openapi_schema_validator import OAS30Validator, OAS31Validator
from openapi_spec_validator import validate as validate_openapi_spec

from apispec import APISpec
from apispec.ext.marshmallow import MarshmallowPlugin

from .utils import get_schemas

LEAP_DAY = dt.date(2024, 2, 29)
OTHER_DAY = dt.date(2023, 3, 15)
THIRD_DAY = dt.date(2025, 1, 1)

OAS_VERSIONS = ("3.0.0", "3.1.0")


def build_spec(openapi_version, schema_cls, name="Batch"):
    spec = APISpec(
        title="Settlement batches",
        version="1",
        openapi_version=openapi_version,
        # Inline nested schemas so a payload validates against one schema object
        plugins=(MarshmallowPlugin(schema_name_resolver=lambda schema: None),),
    )
    spec.components.schema(name, schema=schema_cls)
    return spec


def entry_properties(spec):
    return get_schemas(spec)["Batch"]["properties"]["entries"]["items"]["properties"]


def payload_validator(spec, openapi_version):
    schema = get_schemas(spec)["Batch"]
    if openapi_version == "3.1.0":
        return OAS31Validator(schema)
    return OAS30Validator(schema)


def json_error_paths(validator, payload):
    return {tuple(error.absolute_path) for error in validator.iter_errors(payload)}


class SettlementEntrySchema(Schema):
    settlement_day = fields.Date(
        validate=[
            validate.OneOf([LEAP_DAY, OTHER_DAY]),
            validate.OneOf([LEAP_DAY, THIRD_DAY]),
        ],
    )
    archived_day = fields.Date(
        format="%d/%m/%Y",
        allow_none=True,
        validate=[
            validate.OneOf([LEAP_DAY, OTHER_DAY]),
            validate.OneOf([LEAP_DAY, THIRD_DAY]),
        ],
    )
    kind = fields.String(validate=validate.OneOf(["plain", "gold"]))


class BatchSchema(Schema):
    entries = fields.List(fields.Nested(SettlementEntrySchema))


class DisjointEntrySchema(Schema):
    settlement_day = fields.Date(
        validate=[validate.OneOf([LEAP_DAY]), validate.OneOf([THIRD_DAY])],
    )
    archived_day = fields.Date(
        format="%d/%m/%Y",
        allow_none=True,
        validate=[validate.OneOf([LEAP_DAY]), validate.OneOf([THIRD_DAY])],
    )


class DisjointBatchSchema(Schema):
    entries = fields.List(fields.Nested(DisjointEntrySchema))


class OpenEntrySchema(Schema):
    settlement_day = fields.Date()
    archived_day = fields.Date(format="%d/%m/%Y", allow_none=True)


class OpenBatchSchema(Schema):
    entries = fields.List(fields.Nested(OpenEntrySchema))


# ---------------------------------------------------------------------------
# field2choices unit tests
# ---------------------------------------------------------------------------


def test_field2choices_iso_date_choices(openapi):
    field = fields.Date(validate=validate.OneOf([LEAP_DAY, OTHER_DAY]))
    assert openapi.field2choices(field) == {"enum": ["2024-02-29", "2023-03-15"]}


def test_field2choices_slash_date_choices(openapi):
    field = fields.Date(
        format="%d/%m/%Y", validate=validate.OneOf([LEAP_DAY, OTHER_DAY])
    )
    assert openapi.field2choices(field) == {"enum": ["29/02/2024", "15/03/2023"]}


def test_field2choices_slash_date_equal(openapi):
    field = fields.Date(format="%d/%m/%Y", validate=validate.Equal(LEAP_DAY))
    assert openapi.field2choices(field) == {"enum": ["29/02/2024"]}


def test_field2choices_slash_date_allow_none(openapi):
    field = fields.Date(
        format="%d/%m/%Y", allow_none=True, validate=validate.OneOf([LEAP_DAY])
    )
    assert openapi.field2choices(field) == {"enum": ["29/02/2024", None]}


def test_field2choices_custom_datetime_choices(openapi):
    moment = dt.datetime(2024, 2, 29, 8, 30)
    field = fields.DateTime(
        format="%d/%m/%Y %H:%M", validate=validate.OneOf([moment])
    )
    assert openapi.field2choices(field) == {"enum": ["29/02/2024 08:30"]}


def test_field2choices_non_date_field_untouched(openapi):
    field = fields.Str(validate=validate.OneOf(["a", "b"]))
    assert openapi.field2choices(field) == {"enum": ["a", "b"]}


def test_field2choices_without_validators(openapi):
    assert openapi.field2choices(fields.Date(format="%d/%m/%Y")) == {}


# ---------------------------------------------------------------------------
# generated contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("openapi_version", OAS_VERSIONS)
def test_generated_contract_is_valid_openapi(openapi_version):
    validate_openapi_spec(build_spec(openapi_version, BatchSchema).to_dict())


@pytest.mark.parametrize("openapi_version", OAS_VERSIONS)
def test_date_enums_follow_each_field_wire_format(openapi_version):
    props = entry_properties(build_spec(openapi_version, BatchSchema))

    assert props["settlement_day"]["enum"] == ["2024-02-29"]
    assert props["archived_day"]["enum"] == ["29/02/2024", None]
    # Non-date enums keep their raw values and existing strategy
    assert props["kind"] == {"type": "string", "enum": ["plain", "gold"]}


@pytest.mark.parametrize("openapi_version", OAS_VERSIONS)
def test_nullable_only_rendered_for_allow_none_field(openapi_version):
    props = entry_properties(build_spec(openapi_version, BatchSchema))

    if openapi_version == "3.0.0":
        assert props["settlement_day"] == {
            "type": "string",
            "format": "date",
            "enum": ["2024-02-29"],
        }
        assert props["archived_day"] == {
            "type": "string",
            "format": "date",
            "enum": ["29/02/2024", None],
            "nullable": True,
        }
    else:
        assert props["settlement_day"] == {
            "type": "string",
            "format": "date",
            "enum": ["2024-02-29"],
        }
        assert props["archived_day"] == {
            "type": ["string", "null"],
            "format": "date",
            "enum": ["29/02/2024", None],
        }
        assert "nullable" not in props["archived_day"]


# ---------------------------------------------------------------------------
# contract vs. Marshmallow loading on the nested batch payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("openapi_version", OAS_VERSIONS)
def test_valid_batch_payload_accepted_by_contract_and_marshmallow(openapi_version):
    spec = build_spec(openapi_version, BatchSchema)
    payload = {
        "entries": [
            {
                "settlement_day": "2024-02-29",
                "archived_day": "29/02/2024",
                "kind": "plain",
            }
        ],
    }

    payload_validator(spec, openapi_version).validate(payload)
    loaded = BatchSchema().load(payload)
    assert loaded == {
        "entries": [
            {"settlement_day": LEAP_DAY, "archived_day": LEAP_DAY, "kind": "plain"}
        ],
    }


@pytest.mark.parametrize("openapi_version", OAS_VERSIONS)
def test_null_only_accepted_on_allow_none_field(openapi_version):
    spec = build_spec(openapi_version, BatchSchema)
    payload = {
        "entries": [
            {"settlement_day": "2024-02-29", "archived_day": None, "kind": "gold"},
        ],
    }

    payload_validator(spec, openapi_version).validate(payload)
    assert BatchSchema().load(payload) == {
        "entries": [{"settlement_day": LEAP_DAY, "archived_day": None, "kind": "gold"}],
    }

    null_on_iso_field = {
        "entries": [{"settlement_day": None, "archived_day": None, "kind": "gold"}],
    }
    error_paths = json_error_paths(
        payload_validator(spec, openapi_version), null_on_iso_field
    )
    assert ("entries", 0, "settlement_day") in error_paths
    assert ("entries", 0, "archived_day") not in error_paths

    with pytest.raises(ValidationError) as exc_info:
        BatchSchema().load(null_on_iso_field)
    assert set(exc_info.value.messages["entries"][0]) == {"settlement_day"}


@pytest.mark.parametrize("openapi_version", OAS_VERSIONS)
def test_cross_format_payload_rejected_by_contract_and_marshmallow(openapi_version):
    spec = build_spec(openapi_version, BatchSchema)
    # Each date uses the other field's format instead of its own
    swapped = {
        "entries": [
            {
                "settlement_day": "29/02/2024",
                "archived_day": "2024-02-29",
                "kind": "plain",
            }
        ],
    }

    error_paths = json_error_paths(
        payload_validator(spec, openapi_version), swapped
    )
    assert ("entries", 0, "settlement_day") in error_paths
    assert ("entries", 0, "archived_day") in error_paths

    with pytest.raises(ValidationError) as exc_info:
        BatchSchema().load(swapped)
    assert set(exc_info.value.messages["entries"][0]) == {
        "settlement_day",
        "archived_day",
    }


# ---------------------------------------------------------------------------
# empty intersection of the two OneOf validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("openapi_version", OAS_VERSIONS)
def test_empty_intersection_does_not_allow_any_date(openapi_version):
    spec = build_spec(openapi_version, DisjointBatchSchema)
    props = entry_properties(spec)

    assert props["settlement_day"]["enum"] == []
    assert props["archived_day"]["enum"] == [None]

    validator = payload_validator(spec, openapi_version)
    for value in ("2024-02-29", "1999-12-31"):
        payload = {"entries": [{"settlement_day": value, "archived_day": None}]}
        assert ("entries", 0, "settlement_day") in json_error_paths(
            validator, payload
        )

    slash_payload = {
        "entries": [{"settlement_day": "2024-02-29", "archived_day": "29/02/2024"}],
    }
    assert ("entries", 0, "archived_day") in json_error_paths(
        validator, slash_payload
    )
    # null remains the single accepted value on the nullable field
    validator.validate({"entries": [{"archived_day": None}]})

    with pytest.raises(ValidationError):
        DisjointBatchSchema().load(
            {"entries": [{"settlement_day": "2024-02-29", "archived_day": None}]},
        )


# ---------------------------------------------------------------------------
# fields without any date choice configured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("openapi_version", OAS_VERSIONS)
def test_unconfigured_date_fields_are_not_restricted(openapi_version):
    spec = build_spec(openapi_version, OpenBatchSchema)
    props = entry_properties(spec)

    assert "enum" not in props["settlement_day"]
    assert "enum" not in props["archived_day"]

    validator = payload_validator(spec, openapi_version)
    payload = {
        "entries": [
            {"settlement_day": "2030-07-14", "archived_day": "14/07/2030"},
        ],
    }
    validator.validate(payload)
    assert OpenBatchSchema().load(payload) == {
        "entries": [
            {"settlement_day": dt.date(2030, 7, 14), "archived_day": dt.date(2030, 7, 14)},
        ],
    }

    # Nullability still follows allow_none, not the absent enum
    null_payload = {"entries": [{"settlement_day": None, "archived_day": None}]}
    assert ("entries", 0, "settlement_day") in json_error_paths(
        validator, null_payload
    )
    validator.validate(
        {"entries": [{"settlement_day": "2030-07-14", "archived_day": None}]}
    )

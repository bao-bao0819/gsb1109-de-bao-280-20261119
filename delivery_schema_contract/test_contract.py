"""Executable checks for the delivery-note schema contract.

Run from the repository root::

    PYTHONPATH=. python3 -m pytest delivery_schema_contract/test_contract.py

These tests use only the public mashumaro path (``build_json_schema`` plus
the generated ``to_dict``/``to_json``) and a stock Draft 2020-12 validator.
They do not modify the schema builder or turn ``__post_serialize__`` into a
model constraint.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from .contract import (
    CONFIGS,
    build_default_schema,
    build_report,
    build_wrapped_schema,
    case_report,
    dumps,
    errors_for,
    independent_checks,
    sample_note,
)
from .models import CentsDialect, DeliveryNote, WrappedDeliveryNote

EXPECTED_DIR = Path(__file__).with_name("expected")


@pytest.fixture(scope="module")
def schema():
    return build_default_schema()


def test_published_schema_declares_nested_amounts_as_strings(schema):
    assert schema["properties"]["amountGroups"] == {
        "type": "array",
        "items": {"type": "array", "items": {"type": "string"}},
    }
    assert schema["properties"]["invoiceId"] == {"type": "string"}
    assert schema["required"] == ["invoiceId", "amountGroups"]
    assert schema["additionalProperties"] is False


def test_alias_keys_are_the_only_published_property_names(schema):
    assert list(schema["properties"]) == ["invoiceId", "amountGroups"]


def test_bare_decimal_schema_is_string_with_format():
    from mashumaro.jsonschema import build_json_schema

    assert build_json_schema(Decimal).to_dict() == {
        "type": "string",
        "format": "decimal",
    }


@pytest.mark.parametrize(
    ("name", "use_dialect", "use_envelope"),
    CONFIGS,
    ids=[name for name, _, _ in CONFIGS],
)
def test_case_matches_snapshot(name, use_dialect, use_envelope, schema):
    expected = json.loads((EXPECTED_DIR / "report.json").read_text())
    expected_case = next(c for c in expected["cases"] if c["name"] == name)
    actual = case_report(name, use_dialect, use_envelope, schema)
    assert actual == expected_case


def test_full_report_matches_snapshot():
    expected = json.loads((EXPECTED_DIR / "report.json").read_text())
    assert build_report() == expected


def test_default_configuration_passes(schema):
    payload = sample_note(DeliveryNote).to_dict()
    assert errors_for(schema, payload) == []
    assert payload["amountGroups"][0][0] == "12.34"


def test_dialect_changes_the_amount_leaf_and_the_schema_rejects_it(schema):
    payload = sample_note(DeliveryNote).to_dict(dialect=CentsDialect)
    assert payload["amountGroups"][0][0] == 1234
    errors = errors_for(schema, payload)
    first = errors[0]
    assert first["validator"] == "type"
    assert first["instance_path"] == ["amountGroups", 0, 0]
    assert first["schema_path"] == [
        "properties",
        "amountGroups",
        "items",
        "items",
        "type",
    ]


def test_envelope_changes_the_root_keys_and_the_schema_rejects_it(schema):
    payload = sample_note(WrappedDeliveryNote).to_dict()
    assert list(payload) == ["payload"]
    errors = errors_for(schema, payload)
    assert errors[0]["validator"] == "additionalProperties"
    assert errors[0]["instance_path"] == []
    assert {e["validator"] for e in errors} == {
        "additionalProperties",
        "required",
    }


def test_envelope_schema_is_not_part_of_the_published_schema():
    published = build_default_schema()
    wrapped = build_wrapped_schema()
    assert wrapped["properties"] == published["properties"]
    assert wrapped["required"] == published["required"]
    assert wrapped["additionalProperties"] == published["additionalProperties"]
    assert "payload" not in json.dumps(wrapped)


def test_json_string_round_trip_matches_to_dict(schema):
    for name, use_dialect, use_envelope in CONFIGS:
        model = WrappedDeliveryNote if use_envelope else DeliveryNote
        instance = sample_note(model)
        kwargs = {"dialect": CentsDialect} if use_dialect else {}
        assert instance.to_dict(**kwargs) == json.loads(
            instance.to_json(**kwargs)
        ), name


def test_json_string_does_not_change_validity(schema):
    for name, use_dialect, use_envelope in CONFIGS:
        model = WrappedDeliveryNote if use_envelope else DeliveryNote
        instance = sample_note(model)
        kwargs = {"dialect": CentsDialect} if use_dialect else {}
        as_dict = errors_for(schema, instance.to_dict(**kwargs))
        decoded = errors_for(schema, json.loads(instance.to_json(**kwargs)))
        assert as_dict == decoded, name


def test_fixing_numeric_does_not_fix_envelope(schema):
    checks = independent_checks(schema)
    broken = checks["numeric_fixed_but_envelope_still_broken"]
    assert broken["observed_amount_pointer"] == "$.payload.amountGroups.0.0"
    assert broken["first_error"]["validator"] == "additionalProperties"
    assert broken["first_error"]["instance_pointer"] == "$"


def test_fixing_envelope_does_not_fix_numeric(schema):
    checks = independent_checks(schema)
    broken = checks["envelope_fixed_but_numeric_still_broken"]
    assert broken["observed_amount_pointer"] == "$.amountGroups.0.0"
    assert broken["first_error"]["validator"] == "type"
    assert broken["first_error"]["instance_path"] == ["amountGroups", 0, 0]


def test_both_broken_envelope_error_masks_numeric_error(schema):
    checks = independent_checks(schema)
    masked = checks["both_broken_envelope_hides_numeric"]
    assert masked["first_error"]["validator"] == "additionalProperties"
    assert all(e["instance_pointer"] == "$" for e in masked["all_errors"])
    after_unwrap = checks["both_broken_after_manual_unwrap"]
    assert after_unwrap["first_error"]["validator"] == "type"
    assert after_unwrap["first_error"]["instance_path"] == [
        "amountGroups",
        0,
        0,
    ]
    assert {
        error["instance_pointer"] for error in after_unwrap["all_errors"]
    } == {
        "$.amountGroups.0.0",
        "$.amountGroups.0.1",
        "$.amountGroups.1.0",
    }
    assert all(
        error["schema_pointer"] == "/properties/amountGroups/items/items/type"
        for error in after_unwrap["all_errors"]
    )


def test_caller_dialect_is_not_a_json_schema_dialect():
    with pytest.raises(AttributeError):
        from mashumaro.jsonschema import build_json_schema

        build_json_schema(DeliveryNote, dialect=CentsDialect)


def test_dialect_deserialization_round_trips_only_with_the_same_dialect():
    payload = sample_note(DeliveryNote).to_dict(dialect=CentsDialect)
    assert DeliveryNote.from_dict(
        payload, dialect=CentsDialect
    ) == sample_note(DeliveryNote)
    # The default deserializer accepts the integer without an error, but it
    # reads 1234 as Decimal("1234") rather than Decimal("12.34").  Model
    # deserialization is not a schema check.
    silently_wrong = DeliveryNote.from_dict(payload)
    assert silently_wrong.amount_groups[0][0] == Decimal("1234")


def test_report_dump_is_stable():
    assert dumps(build_report()) == (EXPECTED_DIR / "report.json").read_text()

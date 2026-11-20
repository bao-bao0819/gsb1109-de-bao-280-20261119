"""Reproduce the published-schema vs delivered-payload mismatch.

The published schema is built exactly the way a producer would build it:
``build_json_schema(DeliveryNote)`` with no caller-dialect argument.  The
caller dialect therefore never participates in schema generation.

Four runtime configurations are compared:

===========  =======================  =================
name         caller dialect           envelope hook
===========  =======================  =================
default      no                       no
dialect      yes                      no
envelope     no                       yes
both         yes                      yes
===========  =======================  =================

Run from the repository root::

    PYTHONPATH=. python3 -m delivery_schema_contract.contract

Use ``--write-expected`` to refresh snapshots and ``--check`` to compare the
current report against them.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from mashumaro.jsonschema import build_json_schema

from .models import (
    CentsDialect,
    DecimalAsText,
    DeliveryNote,
    WrappedDeliveryNote,
)

EXPECTED_DIR = Path(__file__).with_name("expected")

CONFIGS = (
    ("default", False, False),
    ("dialect", True, False),
    ("envelope", False, True),
    ("both", True, True),
)


def sample_note(cls: type[DeliveryNote]) -> DeliveryNote:
    """Build the same business value for every configuration."""

    return cls(
        invoice_id="INV-2026-1119",
        amount_groups=[
            [Decimal("12.34"), Decimal("0.50")],
            [Decimal("7.00")],
        ],
    )


def build_default_schema() -> dict[str, Any]:
    return build_json_schema(DeliveryNote).to_dict()


def build_wrapped_schema() -> dict[str, Any]:
    return build_json_schema(WrappedDeliveryNote).to_dict()


def json_pointer(parts: list[Any]) -> str:
    if not parts:
        return "$"
    escaped = [
        str(part).replace("~", "~0").replace("/", "~1") for part in parts
    ]
    return "$" + "".join(
        f"[{part!r}]" if isinstance(part, int) else f".{part}"
        for part in escaped
    )


def schema_pointer(parts: list[Any]) -> str:
    if not parts:
        return "/"
    return "/" + "/".join(
        str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


def errors_for(schema: dict[str, Any], payload: Any) -> list[dict[str, Any]]:
    validator = Draft202012Validator(schema)
    result = []
    for error in validator.iter_errors(payload):
        instance_path = list(error.absolute_path)
        schema_path = list(error.absolute_schema_path)
        result.append(
            {
                "message": error.message,
                "validator": error.validator,
                "instance_path": instance_path,
                "instance_pointer": json_pointer(instance_path),
                "schema_path": schema_path,
                "schema_pointer": schema_pointer(schema_path),
            }
        )
    return result


def amount_path(payload: Any) -> list[str]:
    """Locate the first delivered amount in the decoded structure."""

    def walk(value: Any, parts: list[Any]) -> list[Any] | None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                found = walk(item, parts + [index])
                if found is not None:
                    return found
        elif isinstance(value, dict):
            if "amountGroups" in value:
                groups = value["amountGroups"]
                if (
                    isinstance(groups, list)
                    and groups
                    and isinstance(groups[0], list)
                    and groups[0]
                ):
                    return parts + ["amountGroups", 0, 0]
            for key, item in value.items():
                found = walk(item, parts + [key])
                if found is not None:
                    return found
        return None

    found = walk(payload, [])
    return json_pointer(found or [])


def classify(errors: list[dict[str, Any]], payload: Any) -> str:
    if not errors:
        return "passes"
    first = errors[0]
    if first["instance_pointer"] == "$":
        return "envelope: top-level keys moved under payload"
    if "amountGroups" in first["instance_path"]:
        return "numeric representation: amount leaf is not the declared type"
    return "other"


def unwrapped_payload(payload: Any) -> Any | None:
    if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
        return payload["payload"]
    return None


def case_report(
    name: str,
    use_dialect: bool,
    use_envelope: bool,
    schema: dict[str, Any],
) -> dict[str, Any]:
    model = WrappedDeliveryNote if use_envelope else DeliveryNote
    instance = sample_note(model)
    kwargs = {"dialect": CentsDialect} if use_dialect else {}
    as_dict = instance.to_dict(**kwargs)
    json_text = instance.to_json(**kwargs)
    decoded = json.loads(json_text)
    errors = errors_for(schema, decoded)
    inner = unwrapped_payload(decoded)
    inner_errors = errors_for(schema, inner) if inner is not None else None
    first = errors[0] if errors else None
    return {
        "name": name,
        "configuration": {
            "caller_dialect": use_dialect,
            "envelope_hook": use_envelope,
            "model": model.__name__,
        },
        "to_dict": as_dict,
        "json_text": json_text,
        "json_decoded": decoded,
        "json_decoded_equals_to_dict": decoded == as_dict,
        "observed_amount_pointer": amount_path(decoded),
        "observed_amount_value": (
            decoded["payload"]["amountGroups"][0][0]
            if use_envelope
            else decoded["amountGroups"][0][0]
        ),
        "schema_validation": {
            "first_error": first,
            "all_errors": errors,
            "classification": classify(errors, decoded),
        },
        "unwrapped_payload_validation": (
            {
                "first_error": inner_errors[0] if inner_errors else None,
                "all_errors": inner_errors,
                "classification": classify(inner_errors or [], inner),
            }
            if inner_errors is not None
            else None
        ),
    }


def independent_checks(schema: dict[str, Any]) -> dict[str, Any]:
    """Show that fixing one axis leaves the other axis broken."""

    default_instance = sample_note(DeliveryNote)
    dialect_instance = sample_note(DeliveryNote)
    wrapped_instance = sample_note(WrappedDeliveryNote)

    numeric_fixed_envelope_broken = wrapped_instance.to_dict()
    envelope_fixed_numeric_broken = dialect_instance.to_dict(
        dialect=CentsDialect
    )
    both_broken = wrapped_instance.to_dict(dialect=CentsDialect)
    both_payload = unwrapped_payload(both_broken)

    def summarize(payload: Any) -> dict[str, Any]:
        errors = errors_for(schema, payload)
        return {
            "payload": payload,
            "observed_amount_pointer": amount_path(payload),
            "first_error": errors[0] if errors else None,
            "all_errors": errors,
            "classification": classify(errors, payload),
        }

    return {
        "default_control": summarize(default_instance.to_dict()),
        "numeric_fixed_but_envelope_still_broken": summarize(
            numeric_fixed_envelope_broken
        ),
        "envelope_fixed_but_numeric_still_broken": summarize(
            envelope_fixed_numeric_broken
        ),
        "both_broken_envelope_hides_numeric": summarize(both_broken),
        "both_broken_after_manual_unwrap": summarize(both_payload),
    }


def schema_scope() -> dict[str, Any]:
    delivery_schema = build_default_schema()
    wrapped_schema = build_wrapped_schema()
    amount_leaf = delivery_schema["properties"]["amountGroups"]["items"][
        "items"
    ]

    dialect_as_schema_dialect: dict[str, Any]
    try:
        build_json_schema(DeliveryNote, dialect=CentsDialect)  # type: ignore[arg-type]
        dialect_as_schema_dialect = {"accepted": True}
    except Exception as exc:  # noqa: BLE001
        dialect_as_schema_dialect = {
            "accepted": False,
            "exception": type(exc).__name__,
            "message": str(exc),
        }

    return {
        "published_schema": delivery_schema,
        "wrapped_schema": wrapped_schema,
        "wrapped_schema_matches_published": {
            "properties": wrapped_schema.get("properties")
            == delivery_schema.get("properties"),
            "required": wrapped_schema.get("required")
            == delivery_schema.get("required"),
            "additionalProperties": wrapped_schema.get("additionalProperties")
            == delivery_schema.get("additionalProperties"),
            "definitions": wrapped_schema.get("$defs")
            == delivery_schema.get("$defs"),
        },
        "alias_keys": sorted(delivery_schema["properties"]),
        "amount_leaf_schema": amount_leaf,
        "bare_decimal_schema": build_json_schema(Decimal).to_dict(),
        "model_strategy": {
            "class": type(DecimalAsText()).__name__,
            "serialize_return_annotation": str(
                DecimalAsText.serialize.__annotations__["return"]
            ),
            "declared_on": "DeliveryNote.Config.serialization_strategy",
        },
        "caller_dialect_as_json_schema_dialect": dialect_as_schema_dialect,
    }


def build_report() -> dict[str, Any]:
    schema = build_default_schema()
    return {
        "schema_scope": schema_scope(),
        "cases": [
            case_report(name, use_dialect, use_envelope, schema)
            for name, use_dialect, use_envelope in CONFIGS
        ],
        "independent_checks": independent_checks(schema),
    }


def dumps(report: Any) -> str:
    return (
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def print_summary(report: dict[str, Any]) -> None:
    print("Published schema amount leaf:")
    print(dumps(report["schema_scope"]["amount_leaf_schema"]).rstrip())
    print()
    print(f"{'case':10} {'amount@':34} {'first validator':20} origin")
    for case in report["cases"]:
        first = case["schema_validation"]["first_error"]
        first_desc = (
            f"{first['validator']} {first['instance_pointer']}"
            if first
            else "-"
        )
        print(
            f"{case['name']:10} {case['observed_amount_pointer']:34} "
            f"{first_desc:20} {case['schema_validation']['classification']}"
        )
    print()
    print("Independent checks:")
    for name, check in report["independent_checks"].items():
        first = check["first_error"]
        first_desc = (
            f"{first['validator']} {first['instance_pointer']}"
            if first
            else "no error"
        )
        print(f"  {name}: {first_desc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-expected",
        action="store_true",
        help="write report.json and schema snapshots under expected/",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the current report with expected/report.json",
    )
    args = parser.parse_args(argv)

    report = build_report()

    if args.write_expected:
        EXPECTED_DIR.mkdir(parents=True, exist_ok=True)
        (EXPECTED_DIR / "report.json").write_text(dumps(report))
        (EXPECTED_DIR / "schema_delivery_note.json").write_text(
            dumps(report["schema_scope"]["published_schema"])
        )
        (EXPECTED_DIR / "schema_wrapped_delivery_note.json").write_text(
            dumps(report["schema_scope"]["wrapped_schema"])
        )
        print(f"wrote snapshots to {EXPECTED_DIR}")
        return 0

    if args.check:
        expected = json.loads((EXPECTED_DIR / "report.json").read_text())
        if expected != report:
            print("report differs from expected/report.json", file=sys.stderr)
            return 1
        print("report matches expected/report.json")
        return 0

    print_summary(report)
    print()
    print(dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

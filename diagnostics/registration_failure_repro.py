"""Deterministic reproduction of apispec registration-failure state.

Run with: .venv/bin/python diagnostics/registration_failure_repro.py
No network access is used.

The JSON output path can be overridden with the first CLI argument.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from marshmallow import Schema, fields

from apispec import APISpec
from apispec.ext.marshmallow import MarshmallowPlugin


class ExplodingField(fields.String):
    """Custom leaf field whose conversion can be made to fail deterministically."""


class HealthSchema(Schema):
    status = fields.Str()


class ProductSchema(Schema):
    """Circular product; ``part`` precedes the failing leaf."""

    id = fields.Int(required=True)
    name = fields.Str()
    part = fields.Nested(lambda: PartSchema())
    secret = ExplodingField()


class PartSchema(Schema):
    id = fields.Int(required=True)
    label = fields.Str()
    product = fields.Nested(ProductSchema, only=("id", "name"))


class ProductLeafFirstSchema(Schema):
    """Circular product with the failing leaf before the nested part."""

    id = fields.Int(required=True)
    name = fields.Str()
    secret = ExplodingField()
    part = fields.Nested(lambda: PartLeafFirstSchema())


class PartLeafFirstSchema(Schema):
    id = fields.Int(required=True)
    label = fields.Str()
    product = fields.Nested(ProductLeafFirstSchema, only=("id", "name"))


class DeclaredProductSchema(Schema):
    """Over-restricted on purpose; the declaration itself is wrong."""

    id = fields.Int(required=True)
    name = fields.Str()
    part = fields.Nested(lambda: DeclaredPartSchema())


class DeclaredPartSchema(Schema):
    id = fields.Int(required=True)
    product = fields.Nested(DeclaredProductSchema, only=("id",))


def make_spec() -> tuple[MarshmallowPlugin, APISpec, dict[str, bool]]:
    plugin = MarshmallowPlugin()
    spec = APISpec(
        title="registration-failure-repro",
        version="1.0.0",
        openapi_version="3.0.2",
        plugins=[plugin],
    )
    state = {"explode": True}

    def explode_at_leaf(self, field, ret, **kwargs):
        if state["explode"] and isinstance(field, ExplodingField):
            raise RuntimeError(f"deterministic conversion failure on {field.name}")
        return {}

    plugin.converter.add_attribute_function(explode_at_leaf)
    return plugin, spec, state


def operation(schema: type[Schema]) -> dict[str, Any]:
    return {
        "get": {
            "responses": {
                "200": {
                    "description": "ok",
                    "content": {"application/json": {"schema": schema}},
                }
            }
        }
    }


def collect_refs(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            found.append(ref)
        for child in value.values():
            found.extend(collect_refs(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(collect_refs(child))
    return sorted(set(found))


def resolve_json_pointer(document: dict, ref: str) -> Any:
    current: Any = document
    for part in ref.removeprefix("#/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        current = current[part]
    return current


def snapshot(spec: APISpec, plugin: MarshmallowPlugin, label: str) -> dict[str, Any]:
    document = spec.to_dict()
    schemas = spec.components.schemas
    refs = []
    for key, name in plugin.converter.refs.items():
        refs.append(
            {
                "schema": key[0].__name__,
                "modifiers": {
                    "only": sorted(key[1]) if key[1] else [],
                    "exclude": sorted(key[2]) if key[2] else [],
                    "load_only": sorted(key[3]) if key[3] else [],
                    "dump_only": sorted(key[4]) if key[4] else [],
                    "partial": list(key[5])
                    if isinstance(key[5], (list, tuple))
                    else key[5],
                },
                "name": name,
                "name_registered_in_components": name in schemas,
                "registered_properties": (
                    list(schemas[name].get("properties", {}))
                    if name in schemas
                    else None
                ),
            }
        )
    all_refs = collect_refs(document)
    unresolved = []
    for ref in all_refs:
        try:
            resolve_json_pointer(document, ref)
        except (KeyError, TypeError):
            unresolved.append(ref)
    return {
        "label": label,
        "component_schema_names": list(schemas),
        "component_schemas": {
            name: {
                "properties": list(schema.get("properties", {})),
                "required": schema.get("required", []),
            }
            for name, schema in schemas.items()
        },
        "refs": refs,
        "paths": list(spec._paths),
        "all_refs_in_document": all_refs,
        "unresolved_refs_in_document": unresolved,
    }


def run_after_part_failure() -> dict[str, Any]:
    plugin, spec, state = make_spec()
    spec.path("/health", operations=operation(HealthSchema))
    after_health = snapshot(spec, plugin, "after-health")

    try:
        spec.path("/products", operations=operation(ProductSchema))
    except RuntimeError as exc:
        failure = f"{type(exc).__name__}: {exc}"
    after_failure = snapshot(spec, plugin, "after-failure")

    state["explode"] = False
    spec.path("/products", operations=operation(ProductSchema))
    after_same_instance_retry = snapshot(spec, plugin, "same-instance-retry")

    # Direct conversion is independent of the registration cache.
    direct_conversion = plugin.converter.schema2jsonschema(ProductSchema())

    plugin2, spec2, state2 = make_spec()
    state2["explode"] = False
    spec2.path("/health", operations=operation(HealthSchema))
    spec2.path("/products", operations=operation(ProductSchema))
    after_fresh_retry = snapshot(spec2, plugin2, "fresh-instance-retry")
    direct_conversion_fresh = plugin2.converter.schema2jsonschema(ProductSchema())

    return {
        "scenario": "failure-after-part-registration",
        "failure": failure,
        "states": {
            "after_health": after_health,
            "after_failure": after_failure,
            "same_instance_retry": after_same_instance_retry,
            "fresh_instance_retry": after_fresh_retry,
        },
        "direct_product_conversion": {
            "same_instance": list(direct_conversion.get("properties", {})),
            "fresh_instance": list(direct_conversion_fresh.get("properties", {})),
        },
    }


def run_before_part_failure() -> dict[str, Any]:
    plugin, spec, state = make_spec()
    spec.path("/health", operations=operation(HealthSchema))
    after_health = snapshot(spec, plugin, "after-health")
    try:
        spec.path("/products", operations=operation(ProductLeafFirstSchema))
    except RuntimeError as exc:
        failure = f"{type(exc).__name__}: {exc}"
    after_failure = snapshot(spec, plugin, "after-failure")

    state["explode"] = False
    spec.path("/products", operations=operation(ProductLeafFirstSchema))
    after_retry = snapshot(spec, plugin, "same-instance-retry")

    plugin2, spec2, state2 = make_spec()
    state2["explode"] = False
    spec2.path("/health", operations=operation(HealthSchema))
    spec2.path("/products", operations=operation(ProductLeafFirstSchema))
    after_fresh_retry = snapshot(spec2, plugin2, "fresh-instance-retry")

    return {
        "scenario": "failure-before-part-registration",
        "failure": failure,
        "states": {
            "after_health": after_health,
            "after_failure": after_failure,
            "same_instance_retry": after_retry,
            "fresh_instance_retry": after_fresh_retry,
        },
    }


def run_projection_only_control() -> dict[str, Any]:
    """No exception; compare product-first and part-first entry points."""
    results = {}
    for entry, schema in (("product_entry", ProductSchema), ("part_entry", PartSchema)):
        plugin, spec, state = make_spec()
        state["explode"] = False
        spec.path("/entry", operations=operation(schema))
        results[entry] = snapshot(spec, plugin, entry)
        results[entry]["direct_product_conversion"] = list(
            plugin.converter.schema2jsonschema(ProductSchema()).get("properties", {})
        )
    return {"scenario": "no-error-projection-control", "states": results}


def run_declared_projection_control() -> dict[str, Any]:
    """Run the same deliberately wrong projection twice, with fresh instances."""
    states = {}
    for label in ("first_run", "second_run"):
        plugin, spec, state = make_spec()
        state["explode"] = False
        spec.path("/parts", operations=operation(DeclaredPartSchema))
        states[label] = snapshot(spec, plugin, label)
        states[label]["direct_product_conversion"] = list(
            plugin.converter.schema2jsonschema(DeclaredProductSchema()).get(
                "properties", {}
            )
        )
        if label == "first_run":
            # Same instance, unchanged declaration: this is the "retry" case.
            spec.path("/parts-again", operations=operation(DeclaredPartSchema))
            states["same_instance_rerun"] = snapshot(
                spec, plugin, "same-instance-rerun"
            )
    return {"scenario": "declared-projection-control", "states": states}


def main() -> None:
    result = {
        "after_part_failure": run_after_part_failure(),
        "before_part_failure": run_before_part_failure(),
        "projection_only_control": run_projection_only_control(),
        "declared_projection_control": run_declared_projection_control(),
    }
    print(json.dumps(result, indent=2))
    output = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "diagnostics/registration_failure_evidence.json"
    )
    with open(output, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()

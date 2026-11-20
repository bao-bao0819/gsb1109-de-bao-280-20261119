"""Regression tests: two modules declaring a same-named recursive model
(``Node``) must never borrow each other's definitions when their schemas
are emitted into the same schema package (one ``$defs`` namespace).

Module ``module_a`` declares ``Node`` whose nullable ``value`` accepts
only integers when non-null; module ``module_b`` declares ``Node`` whose
nullable ``value`` accepts only strings. Children of each ``Node`` are
the ``Node`` of its own module. Both specializations are carried by the
same generic ``Wrapper[T]`` whose root value may be null.

For each ``all_refs`` configuration (the dialect default and explicitly
enabled) the schemas are built in one builder requesting A's wrapper
then B's, and in a second builder in the reverse order. Verification
resolves every reachable reference against the emitted definitions and
validates concrete instances -- comparing definition counts or names
alone cannot prove reference ownership.
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, Generic, List, Optional, Set, Tuple, TypeVar

import pytest

from mashumaro.jsonschema import DRAFT_2020_12
from mashumaro.jsonschema.builder import JSONSchemaBuilder
from tests.test_jsonschema.same_name_nodes import module_a, module_b

T = TypeVar("T")


@dataclass
class Wrapper(Generic[T]):
    payload: Optional[T]


NODES = {"A": module_a.Node, "B": module_b.Node}
EXPECTED_VALUE_TYPE = {"A": "integer", "B": "string"}

# The same root value (null) passes both outer structures.
NULL_ROOT = {"payload": None}
# Only one deep child item carries the type distinction ...
A_DEEP_CHILD = {
    "payload": {"value": None, "children": [{"value": 1, "children": []}]}
}
# ... and the reverse distinction.
B_DEEP_CHILD = {
    "payload": {"value": None, "children": [{"value": "1", "children": []}]}
}

INSTANCES = [
    ("NULL_ROOT", NULL_ROOT, {"A": True, "B": True}),
    ("A_DEEP_CHILD", A_DEEP_CHILD, {"A": True, "B": False}),
    ("B_DEEP_CHILD", B_DEEP_CHILD, {"A": False, "B": True}),
]

BUILD_ORDERS = (("A", "B"), ("B", "A"))


def config_desc(all_refs: Optional[bool]) -> str:
    if all_refs is None:
        return (
            "JSONSchemaBuilder(dialect=DRAFT_2020_12) "
            "# all_refs not set, dialect default"
        )
    return f"JSONSchemaBuilder(dialect=DRAFT_2020_12, all_refs={all_refs})"


def order_desc(order: Tuple[str, ...]) -> str:
    return " then ".join(
        f"builder.build(Wrapper[{NODES[key].__module__}.Node])"
        for key in order
    )


def reproduce_desc(order: Tuple[str, ...], all_refs: Optional[bool]) -> str:
    return f"Reproduce with: {config_desc(all_refs)}; {order_desc(order)}"


def build_package(
    order: Tuple[str, ...], all_refs: Optional[bool]
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"dialect": DRAFT_2020_12}
    if all_refs is not None:
        kwargs["all_refs"] = all_refs
    builder = JSONSchemaBuilder(**kwargs)
    roots = {}
    for key in order:
        roots[key] = builder.build(Wrapper[NODES[key]]).to_dict()
    return {
        "roots": roots,
        "definitions": builder.get_definitions().to_dict(),
        "ref_prefix": DRAFT_2020_12.definitions_root_pointer,
    }


def ref_key(ref: str, ref_prefix: str) -> Optional[str]:
    prefix = ref_prefix + "/"
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return None


def iter_refs(schema: Any, path: str = "$"):
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if ref is not None:
            yield path, ref
        for key, value in schema.items():
            if key == "$ref":
                continue
            yield from iter_refs(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            yield from iter_refs(item, f"{path}[{index}]")


def accepted_value_types(schema: Any) -> Set[str]:
    if not isinstance(schema, dict):
        return set()
    any_of = schema.get("anyOf")
    if any_of is not None:
        result: Set[str] = set()
        for branch in any_of:
            result |= accepted_value_types(branch)
        return result
    schema_type = schema.get("type")
    return {schema_type} if schema_type else set()


def check_all_refs_resolvable(
    package: Dict[str, Any],
    order: Tuple[str, ...],
    all_refs: Optional[bool],
    problems: List[str],
) -> None:
    definitions = package["definitions"]
    for root_key, root_schema in package["roots"].items():
        queue = [("$", root_schema)]
        visited_keys: Set[str] = set()
        while queue:
            origin, schema = queue.pop(0)
            for path, ref in iter_refs(schema):
                key = ref_key(ref, package["ref_prefix"])
                if key is None or key not in definitions:
                    problems.append(
                        f"[{config_desc(all_refs)}; {order_desc(order)}] "
                        f"root Wrapper[{NODES[root_key].__module__}.Node]: "
                        f"unresolvable reference {ref!r} at {origin}{path}; "
                        f"definitions only contain {sorted(definitions)}. "
                        f"{reproduce_desc(order, all_refs)}"
                    )
                elif key not in visited_keys:
                    visited_keys.add(key)
                    queue.append(
                        (f"{origin}{path} -> {ref} ", definitions[key])
                    )


def check_ref_ownership(
    package: Dict[str, Any],
    root_key: str,
    order: Tuple[str, ...],
    all_refs: Optional[bool],
    problems: List[str],
) -> None:
    """Every Node-shaped schema reachable from ``root_key`` -- including
    the recursive child behind a ``$ref`` -- must belong to the module
    that root was built from, never to the other same-named module."""
    expected = EXPECTED_VALUE_TYPE[root_key]
    owner_module = NODES[root_key].__module__
    definitions = package["definitions"]
    checked_defs: Set[str] = set()

    def visit(schema: Any, trail: List[str]) -> None:
        if not isinstance(schema, dict):
            return
        ref = schema.get("$ref")
        if ref is not None:
            key = ref_key(ref, package["ref_prefix"])
            target = definitions.get(key) if key is not None else None
            if target is None or key in checked_defs:
                return  # unresolvable refs are reported separately
            checked_defs.add(key)
            visit(target, trail + [f"$ref {ref}"])
            return
        properties = schema.get("properties")
        if (
            schema.get("type") == "object"
            and isinstance(properties, dict)
            and "value" in properties
        ):
            value_types = accepted_value_types(properties["value"])
            non_null_types = value_types - {"null"}
            if non_null_types != {expected}:
                problems.append(
                    f"[{config_desc(all_refs)}; {order_desc(order)}] "
                    f"root Wrapper[{owner_module}.Node]: node definition "
                    f"reached via {' -> '.join(trail)} accepts non-null "
                    f"value types {sorted(non_null_types)} but this root "
                    f"must own {expected!r} values from module "
                    f"{owner_module}; the recursive schema borrowed the "
                    f"definition of the other same-named module. "
                    f"{reproduce_desc(order, all_refs)}"
                )
        if isinstance(properties, dict):
            for prop_name, prop_schema in properties.items():
                visit(prop_schema, trail + [f"properties.{prop_name}"])
        for index, branch in enumerate(schema.get("anyOf", [])):
            visit(branch, trail + [f"anyOf[{index}]"])
        if "items" in schema:
            visit(schema["items"], trail + ["items"])

    visit(package["roots"][root_key], ["$"])


def validate_instance(
    instance: Any, schema: Dict[str, Any], definitions: Dict[str, Any]
) -> List[str]:
    errors: List[str] = []

    def validate(value: Any, subschema: Dict[str, Any], path: str) -> None:
        ref = subschema.get("$ref")
        if ref is not None:
            key = ref.rsplit("/", 1)[-1]
            target = definitions.get(key)
            if target is None:
                errors.append(f"{path}: unresolvable reference {ref!r}")
                return
            validate(value, target, path)
            return
        any_of = subschema.get("anyOf")
        if any_of is not None:
            for branch in any_of:
                if not validate_instance(value, branch, definitions):
                    return
            errors.append(
                f"{path}: {value!r} does not match any anyOf branch"
            )
            return
        schema_type = subschema.get("type")
        if schema_type == "null":
            if value is not None:
                errors.append(f"{path}: expected null, got {value!r}")
        elif schema_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"{path}: expected integer, got {value!r}")
        elif schema_type == "string":
            if not isinstance(value, str):
                errors.append(f"{path}: expected string, got {value!r}")
        elif schema_type == "array":
            if not isinstance(value, list):
                errors.append(f"{path}: expected array, got {value!r}")
            else:
                items = subschema.get("items")
                if items is not None:
                    for index, item in enumerate(value):
                        validate(item, items, f"{path}[{index}]")
        elif schema_type == "object":
            if not isinstance(value, dict):
                errors.append(f"{path}: expected object, got {value!r}")
            else:
                for required in subschema.get("required", []):
                    if required not in value:
                        errors.append(
                            f"{path}: missing required property "
                            f"{required!r}"
                        )
                properties = subschema.get("properties", {})
                for prop_name, prop_value in value.items():
                    if prop_name in properties:
                        validate(
                            prop_value,
                            properties[prop_name],
                            f"{path}.{prop_name}",
                        )
                    elif subschema.get("additionalProperties") is False:
                        errors.append(
                            f"{path}: unexpected property {prop_name!r}"
                        )

    validate(instance, schema, "$")
    return errors


def compute_verdicts(
    package: Dict[str, Any],
    order: Tuple[str, ...],
    all_refs: Optional[bool],
    problems: List[str],
) -> Dict[Tuple[str, str], bool]:
    verdicts = {}
    for root_key, root_schema in package["roots"].items():
        owner_module = NODES[root_key].__module__
        for inst_name, instance, expected in INSTANCES:
            errors = validate_instance(
                instance, root_schema, package["definitions"]
            )
            is_valid = not errors
            verdicts[(root_key, inst_name)] = is_valid
            if is_valid != expected[root_key]:
                detail = (
                    f"errors: {errors}" if errors else "no validation errors"
                )
                problems.append(
                    f"[{config_desc(all_refs)}; {order_desc(order)}] "
                    f"instance {inst_name}={json.dumps(instance)}: "
                    f"expected "
                    f"{'valid' if expected[root_key] else 'invalid'} "
                    f"under root Wrapper[{owner_module}.Node] but it was "
                    f"{'valid' if is_valid else 'invalid'} ({detail}). "
                    f"{reproduce_desc(order, all_refs)}"
                )
    return verdicts


@pytest.mark.parametrize(
    "all_refs",
    [None, True],
    ids=["dialect-default-all-refs", "explicit-all-refs-true"],
)
def test_same_name_recursive_models_never_borrow_definitions(
    all_refs: Optional[bool],
) -> None:
    problems: List[str] = []
    verdicts_by_order: Dict[Tuple[str, ...], Dict[Tuple[str, str], bool]] = {}
    for order in BUILD_ORDERS:
        package = build_package(order, all_refs)
        check_all_refs_resolvable(package, order, all_refs, problems)
        for root_key in NODES:
            check_ref_ownership(package, root_key, order, all_refs, problems)
        verdicts_by_order[order] = compute_verdicts(
            package, order, all_refs, problems
        )
    forward = verdicts_by_order[("A", "B")]
    reverse = verdicts_by_order[("B", "A")]
    for key in forward:
        if forward[key] != reverse[key]:
            problems.append(
                f"[{config_desc(all_refs)}] verdict for root {key[0]} "
                f"instance {key[1]} depends on the build order: "
                f"A-then-B gave {'valid' if forward[key] else 'invalid'}, "
                f"B-then-A gave {'valid' if reverse[key] else 'invalid'}; "
                f"the two same-named modules share a definition slot"
            )
    assert not problems, (
        "same-named recursive models borrowed each other's definitions "
        f"({len(problems)} problem(s)):\n"
        + "\n".join(f"  - {problem}" for problem in problems)
    )

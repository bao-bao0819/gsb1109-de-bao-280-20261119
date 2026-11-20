"""Compatibility checks for the event ingestion collector and parser.

The checker models a deliberately small fragment of OpenAPI/JSON Schema:

* object ``required`` lists,
* scalar ``enum`` values,
* ``oneOf`` unions,
* local JSON References (``#/...``),
* structural ``type`` and array ``items`` needed to reach those rules.

Any other keyword that differs between the documents is reported as an
unknown scope. Equal keywords are harmless and are ignored, so a description
or an unrelated bound does not turn a rename into a false break.

Directions are modelled separately:

* request: the old collector must keep sending, so every request accepted by
  the old contract must also be accepted by the new one;
* response: the old parser must accept what the new server sends, so every
  response accepted by a new branch must be accepted by the same old branch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Keywords that carry validation semantics outside the supported fragment.
# Equal values on both sides are harmless; different values are unknown scope.
UNMODELLED_KEYWORDS = (
    "allOf",
    "anyOf",
    "not",
    "if",
    "then",
    "else",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
    "additionalProperties",
    "patternProperties",
    "propertyNames",
    "dependentRequired",
    "nullable",
    "discriminator",
    "readOnly",
    "writeOnly",
)

SUPPORTED_TYPES = ("object", "array", "string", "integer", "number", "boolean")

SCOPE_NOTE = (
    "evaluated only with required, enum, oneOf and local $ref, plus "
    "structural type/items; other keywords are not used to prove behaviour"
)


class Status(str, Enum):
    """Outcome of one compatibility check."""

    EQUIVALENT = "equivalent"
    COMPATIBLE = "compatible"
    BREAKING = "breaking"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    """A single compatibility conclusion with a concrete operation location."""

    status: Status
    location: str
    message: str
    old: Any = None
    new: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "location": self.location,
            "message": self.message,
            "old": self.old,
            "new": self.new,
        }


@dataclass
class Evaluation:
    """Result of checking one instance against the supported fragment."""

    accepted: bool
    failures: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def proven(self) -> bool:
        return not self.unsupported


@dataclass
class Document:
    """An OpenAPI document plus local-reference helpers."""

    raw: dict[str, Any]
    label: str = "document"

    def resolve(self, node: Any) -> Any:
        """Follow a chain of local ``$ref`` values."""
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise ValueError(f"non-local reference {ref!r}")
            if ref in seen:
                raise ValueError(f"reference cycle at {ref!r}")
            seen.add(ref)
            node = self.pointer(ref)
        return node

    def pointer(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise ValueError(f"non-local reference {ref!r}")
        node: Any = self.raw
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list):
                try:
                    node = node[int(token)]
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"bad array index in {ref!r}") from exc
            else:
                node = node[token]
        return node

    def ref_of(self, node: Any) -> str | None:
        """Return the local ref string when ``node`` is a reference."""
        if isinstance(node, dict) and isinstance(node.get("$ref"), str):
            return node["$ref"]
        return None


# ---------------------------------------------------------------------------
# Instance evaluation (required / enum / oneOf / $ref)
# ---------------------------------------------------------------------------


def evaluate(
    document: Document,
    schema: Any,
    instance: Any,
    location: str,
    ref_stack: tuple[str, ...] = (),
) -> Evaluation:
    """Evaluate ``instance`` using only the supported fragment."""
    ref = document.ref_of(schema)
    if ref is not None:
        if ref in ref_stack:
            return Evaluation(True)
        try:
            resolved = document.pointer(ref)
        except (KeyError, ValueError, IndexError) as exc:
            return Evaluation(False, [f"{location}: cannot resolve {ref!r}: {exc}"])
        return evaluate(document, resolved, instance, location, (*ref_stack, ref))

    if not isinstance(schema, dict):
        return Evaluation(False, [f"{location}: schema is not an object"])

    if "oneOf" in schema:
        branches = schema["oneOf"]
        if not isinstance(branches, list):
            return Evaluation(False, [f"{location}: oneOf is not a list"])
        accepted = []
        failures: list[str] = []
        unsupported: list[str] = []
        for index, branch in enumerate(branches):
            result = evaluate(
                document, branch, instance, f"{location}.oneOf[{index}]", ref_stack
            )
            unsupported.extend(result.unsupported)
            if result.accepted and result.proven:
                accepted.append(index)
            elif not result.accepted:
                failures.extend(result.failures)
        if unsupported:
            return Evaluation(False, failures, unsupported)
        if len(accepted) == 1:
            return Evaluation(True)
        if not accepted:
            return Evaluation(False, failures or [f"{location}: no oneOf branch"])
        return Evaluation(
            False,
            [f"{location}: {len(accepted)} oneOf branches accept the instance"],
        )

    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema or "required" in schema:
        if not isinstance(instance, dict):
            return Evaluation(False, [f"{location}: expected an object"])
        required = schema.get("required", [])
        if not isinstance(required, list):
            return Evaluation(False, [f"{location}: required is not a list"])
        failures = [
            f"{location}.{name}: required field is missing"
            for name in required
            if name not in instance
        ]
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return Evaluation(False, [f"{location}: properties is not an object"])
        for name, value in instance.items():
            if name in properties:
                failures.extend(
                    evaluate(
                        document,
                        properties[name],
                        value,
                        f"{location}.{name}",
                        ref_stack,
                    ).failures
                )
        return Evaluation(not failures, failures)

    if schema_type == "array":
        if not isinstance(instance, list):
            return Evaluation(False, [f"{location}: expected an array"])
        items = schema.get("items")
        if not isinstance(items, dict):
            return Evaluation(
                True,
                unsupported=(
                    [f"{location}: array items cannot be checked"]
                    if items is not None
                    else []
                ),
            )
        failures: list[str] = []
        unsupported: list[str] = []
        for index, value in enumerate(instance):
            result = evaluate(
                document, items, value, f"{location}[{index}]", ref_stack
            )
            failures.extend(result.failures)
            unsupported.extend(result.unsupported)
        return Evaluation(not failures, failures, unsupported)

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            return Evaluation(False, [f"{location}: enum is not a list"])
        return Evaluation(
            instance in enum,
            [] if instance in enum else [f"{location}: value is not in enum"],
        )

    if schema_type is not None:
        if schema_type not in SUPPORTED_TYPES:
            return Evaluation(
                True, unsupported=[f"{location}: unsupported type {schema_type!r}"]
            )
        return Evaluation(
            _has_type(instance, schema_type),
            [] if _has_type(instance, schema_type) else [
                f"{location}: expected {schema_type}"
            ],
        )
    return Evaluation(True)


def _has_type(instance: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(instance, str)
    if schema_type == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if schema_type == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if schema_type == "boolean":
        return isinstance(instance, bool)
    if schema_type == "object":
        return isinstance(instance, dict)
    if schema_type == "array":
        return isinstance(instance, list)
    return True


# ---------------------------------------------------------------------------
# Structural schema comparison
# ---------------------------------------------------------------------------


@dataclass
class Comparison:
    """Accumulated findings and the reference mapping proved so far."""

    findings: list[Finding] = field(default_factory=list)
    ref_pairs: dict[str, str] = field(default_factory=dict)
    reverse_ref_pairs: dict[str, str] = field(default_factory=dict)

    def add(self, status: Status, location: str, message: str, old=None, new=None):
        self.findings.append(Finding(status, location, message, old, new))

    def note_ref_pair(self, old_ref: str, new_ref: str, location: str) -> bool:
        """Record a rename pair and reject long-to-many renames."""
        previous_new = self.ref_pairs.get(old_ref)
        previous_old = self.reverse_ref_pairs.get(new_ref)
        if previous_new is not None and previous_new != new_ref:
            self.add(
                Status.BREAKING,
                location,
                "one old component maps to several new components",
                old_ref,
                new_ref,
            )
            return False
        if previous_old is not None and previous_old != old_ref:
            self.add(
                Status.BREAKING,
                location,
                "one new component maps to several old components",
                old_ref,
                new_ref,
            )
            return False
        first_pair = old_ref not in self.ref_pairs
        self.ref_pairs[old_ref] = new_ref
        self.reverse_ref_pairs[new_ref] = old_ref
        if first_pair:
            self.add(
                Status.EQUIVALENT,
                location,
                "local reference pair maps one-to-one across the documents",
                old_ref,
                new_ref,
            )
        return True


def _required(schema: dict[str, Any], location: str) -> set[str]:
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(item, str) for item in required
    ):
        raise ValueError(f"{location}: required is not a list of strings")
    return set(required)


def _properties(schema: dict[str, Any], location: str) -> dict[str, Any]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(f"{location}: properties is not an object")
    return properties


def _enum(schema: dict[str, Any], location: str) -> list[Any] | None:
    enum = schema.get("enum")
    if enum is None:
        return None
    if not isinstance(enum, list):
        raise ValueError(f"{location}: enum is not a list")
    return enum


def compare_schema(
    old_doc: Document,
    old_schema: Any,
    new_doc: Document,
    new_schema: Any,
    location: str,
    direction: str,
    comparison: Comparison,
    ref_stack: tuple[tuple[str, str], ...] = (),
) -> None:
    """Compare two schemas in ``direction`` (``request`` or ``response``)."""
    assert direction in {"request", "response"}

    old_ref = old_doc.ref_of(old_schema)
    new_ref = new_doc.ref_of(new_schema)
    if old_ref is not None or new_ref is not None:
        if (
            old_ref is None
            or new_ref is None
            or not old_ref.startswith("#/")
            or not new_ref.startswith("#/")
        ):
            comparison.add(
                Status.UNKNOWN,
                location,
                "reference is missing on one side or is not local; rename "
                "equivalence cannot be proved",
                old_ref,
                new_ref,
            )
            return
        if (old_ref, new_ref) in ref_stack:
            return
        if not comparison.note_ref_pair(old_ref, new_ref, location):
            return
        ref_stack = (*ref_stack, (old_ref, new_ref))

    try:
        old = old_doc.resolve(old_schema)
        new = new_doc.resolve(new_schema)
    except (KeyError, ValueError, IndexError) as exc:
        comparison.add(Status.UNKNOWN, location, f"cannot resolve reference: {exc}")
        return
    if not isinstance(old, dict) or not isinstance(new, dict):
        comparison.add(Status.UNKNOWN, location, "schema is not an object")
        return

    for keyword in UNMODELLED_KEYWORDS:
        old_value = old.get(keyword)
        new_value = new.get(keyword)
        if old_value != new_value:
            comparison.add(
                Status.UNKNOWN,
                f"{location}.{keyword}",
                f"{keyword} changed; it is outside the supported rule set",
                old_value,
                new_value,
            )

    if "oneOf" in old or "oneOf" in new:
        comparison.add(
            Status.UNKNOWN,
            location,
            "nested oneOf requires a branch-by-branch comparison",
        )
        return

    old_type = old.get("type")
    new_type = new.get("type")
    if old_type != new_type:
        comparison.add(
            Status.BREAKING,
            f"{location}.type",
            "schema type changed",
            old_type,
            new_type,
        )
        return

    try:
        old_enum = _enum(old, location)
        new_enum = _enum(new, location)
        old_required = _required(old, location)
        new_required = _required(new, location)
        old_properties = _properties(old, location)
        new_properties = _properties(new, location)
    except ValueError as exc:
        comparison.add(Status.UNKNOWN, location, str(exc))
        return

    if old_type == "object" or old_required or new_required:
        if direction == "request":
            added_required = new_required - old_required
            if added_required:
                comparison.add(
                    Status.BREAKING,
                    f"{location}.required",
                    "new contract requires fields the old collector may omit",
                    sorted(old_required),
                    sorted(new_required),
                )
        else:
            dropped_required = old_required - new_required
            if dropped_required:
                comparison.add(
                    Status.BREAKING,
                    f"{location}.required",
                    "new response may omit fields the old parser requires",
                    sorted(old_required),
                    sorted(new_required),
                )

        if old_enum is not None or new_enum is not None:
            _compare_enum(comparison, old_enum, new_enum, location, direction)

        if direction == "response":
            new_only = sorted(set(new_properties) - set(old_properties))
            if new_only:
                comparison.add(
                    Status.UNKNOWN,
                    f"{location}.properties",
                    "new response fields have no old counterpart; parser "
                    "behaviour for them is not specified",
                    sorted(old_properties),
                    new_only,
                )
        else:
            old_only = sorted(set(old_properties) - set(new_properties))
            if old_only:
                comparison.add(
                    Status.UNKNOWN,
                    f"{location}.properties",
                    "old request fields are absent from the new schema; whether "
                    "the new server still accepts them is not provable",
                    old_only,
                    sorted(new_properties),
                )

        for name in sorted(set(old_properties) & set(new_properties)):
            old_field = old_properties[name]
            new_field = new_properties[name]
            if not isinstance(old_field, dict) or not isinstance(new_field, dict):
                comparison.add(
                    Status.UNKNOWN,
                    f"{location}.properties.{name}",
                    "property schema is not an object",
                )
                continue
            compare_schema(
                old_doc,
                old_field,
                new_doc,
                new_field,
                f"{location}.properties.{name}",
                direction,
                comparison,
                ref_stack,
            )
        return

    if (
        old_type == "array"
        or new_type == "array"
        or "items" in old
        or "items" in new
    ):
        old_items = old.get("items")
        new_items = new.get("items")
        if isinstance(old_items, dict) and isinstance(new_items, dict):
            compare_schema(
                old_doc,
                old_items,
                new_doc,
                new_items,
                f"{location}.items",
                direction,
                comparison,
                ref_stack,
            )
        elif old_items != new_items:
            comparison.add(
                Status.UNKNOWN,
                f"{location}.items",
                "array items differ and are not both schemas",
                old_items,
                new_items,
            )
        return

    if old_enum is not None or new_enum is not None:
        _compare_enum(comparison, old_enum, new_enum, location, direction)


def _compare_enum(
    comparison: Comparison,
    old_enum: list[Any] | None,
    new_enum: list[Any] | None,
    location: str,
    direction: str,
) -> None:
    if old_enum is None or new_enum is None:
        comparison.add(
            Status.UNKNOWN,
            f"{location}.enum",
            "enum exists on one side only; value range is not provable",
            old_enum,
            new_enum,
        )
        return
    if direction == "request":
        lost = [value for value in old_enum if value not in new_enum]
        lost_message = "new contract no longer accepts values the old collector sends"
        safe_message = "new contract accepts every old enum value, and more"
    else:
        lost = [value for value in new_enum if value not in old_enum]
        lost_message = "new response may emit values the old parser does not accept"
        safe_message = "new response emits a subset of the old enum values"
    if lost:
        comparison.add(
            Status.BREAKING,
            f"{location}.enum",
            lost_message,
            old_enum,
            new_enum,
        )
    elif old_enum == new_enum:
        comparison.add(
            Status.EQUIVALENT,
            f"{location}.enum",
            "enum unchanged",
            old_enum,
            new_enum,
        )
    else:
        comparison.add(
            Status.COMPATIBLE,
            f"{location}.enum",
            safe_message,
            old_enum,
            new_enum,
        )


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _parameters(operation: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result = {}
    for parameter in operation.get("parameters", []):
        key = (parameter.get("in", ""), parameter.get("name", ""))
        result[key] = parameter
    return result


def validate_request(
    document: Document,
    operation: dict[str, Any],
    instance: dict[str, Any],
    location: str,
) -> list[str]:
    """Validate a request against parameters and requestBody.

    Returns human-readable failures; an empty list means the request is
    accepted under the supported fragment.
    """
    failures: list[str] = []
    for (where, name), parameter in _parameters(operation).items():
        value = instance.get(where, {}).get(name)
        if value is None:
            if parameter.get("required"):
                failures.append(f"{location}: missing required {where} {name!r}")
            continue
        schema = parameter.get("schema")
        if schema is not None:
            result = evaluate(
                document, schema, value, f"{location}.parameters[{where}:{name}]"
            )
            failures.extend(result.failures)
            failures.extend(result.unsupported)

    request_body = operation.get("requestBody")
    body = instance.get("body")
    if isinstance(request_body, dict):
        if body is None:
            if request_body.get("required"):
                failures.append(f"{location}: missing required request body")
        else:
            content = request_body.get("content", {}).get("application/json", {})
            schema = content.get("schema")
            if schema is not None:
                result = evaluate(
                    document, schema, body, f"{location}.requestBody"
                )
                failures.extend(result.failures)
                failures.extend(result.unsupported)
    return failures


def compare_request(
    old_doc: Document,
    new_doc: Document,
    old_operation: dict[str, Any],
    new_operation: dict[str, Any],
    location: str,
    instance: dict[str, Any] | None = None,
) -> list[Finding]:
    """Check that the old collector may keep sending the same requests."""
    comparison = Comparison()
    old_parameters = _parameters(old_operation)
    new_parameters = _parameters(new_operation)

    for key in sorted(set(old_parameters) | set(new_parameters)):
        where, name = key
        parameter_location = f"{location}.parameters[{where}:{name}]"
        old_parameter = old_parameters.get(key)
        new_parameter = new_parameters.get(key)
        if old_parameter is None:
            if new_parameter and new_parameter.get("required"):
                comparison.add(
                    Status.BREAKING,
                    f"{parameter_location}.required",
                    "new contract adds a required parameter the old collector "
                    "never sends",
                    None,
                    True,
                )
            continue
        if new_parameter is None:
            comparison.add(
                Status.UNKNOWN,
                parameter_location,
                "old parameter is absent from the new contract; it is not "
                "provable that the new server still accepts it",
                old_parameter,
                None,
            )
            continue

        old_required = bool(old_parameter.get("required", False))
        new_required = bool(new_parameter.get("required", False))
        if not old_required and new_required:
            comparison.add(
                Status.BREAKING,
                f"{parameter_location}.required",
                "parameter became mandatory; old requests without it are no "
                "longer allowed",
                old_required,
                new_required,
            )
        elif old_required == new_required:
            comparison.add(
                Status.EQUIVALENT,
                f"{parameter_location}.required",
                "parameter requiredness unchanged",
                old_required,
                new_required,
            )

        for field_name in ("in", "name"):
            if old_parameter.get(field_name) != new_parameter.get(field_name):
                comparison.add(
                    Status.BREAKING,
                    f"{parameter_location}.{field_name}",
                    "parameter location or name changed",
                    old_parameter.get(field_name),
                    new_parameter.get(field_name),
                )
        old_schema = old_parameter.get("schema")
        new_schema = new_parameter.get("schema")
        if old_schema is not None and new_schema is not None:
            compare_schema(
                old_doc,
                old_schema,
                new_doc,
                new_schema,
                f"{parameter_location}.schema",
                "request",
                comparison,
            )

    old_body = old_operation.get("requestBody")
    new_body = new_operation.get("requestBody")
    if isinstance(old_body, dict) and isinstance(new_body, dict):
        old_required = bool(old_body.get("required", False))
        new_required = bool(new_body.get("required", False))
        if not old_required and new_required:
            comparison.add(
                Status.BREAKING,
                f"{location}.requestBody.required",
                "request body became mandatory",
                old_required,
                new_required,
            )
        old_content = old_body.get("content", {}).get("application/json", {})
        new_content = new_body.get("content", {}).get("application/json", {})
        if "schema" in old_content and "schema" in new_content:
            compare_schema(
                old_doc,
                old_content["schema"],
                new_doc,
                new_content["schema"],
                f"{location}.requestBody.content['application/json'].schema",
                "request",
                comparison,
            )
    elif old_body is not None or new_body is not None:
        comparison.add(
            Status.UNKNOWN,
            f"{location}.requestBody",
            "request body exists on one side only",
        )

    if instance is not None:
        old_failures = validate_request(old_doc, old_operation, instance, location)
        new_failures = validate_request(new_doc, new_operation, instance, location)
        if old_failures:
            comparison.add(
                Status.UNKNOWN,
                f"{location}.counterexample",
                "counterexample is not accepted under the old request contract",
                old_failures,
                new_failures,
            )
        elif new_failures:
            comparison.add(
                Status.BREAKING,
                f"{location}.counterexample",
                "request accepted by the old contract is rejected by the new one",
                old_failures,
                new_failures,
            )
        else:
            comparison.add(
                Status.COMPATIBLE,
                f"{location}.counterexample",
                "counterexample remains accepted by the new request contract",
            )
    return comparison.findings


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _one_of(schema: dict[str, Any], location: str) -> list[Any] | None:
    branches = schema.get("oneOf")
    if branches is None:
        return None
    if not isinstance(branches, list):
        raise ValueError(f"{location}: oneOf is not a list")
    return branches


def _branch_table(document: Document, branch: Any) -> str | None:
    """Return the collector table marker without relying on the branch name."""
    resolved = document.resolve(branch)
    if isinstance(resolved, dict):
        table = resolved.get("x-collector-table")
        return table if isinstance(table, str) else None
    return None


def _branch_key(document: Document, branch: Any) -> tuple[Any, ...]:
    """Structural branch identity: table marker plus required fields."""
    resolved = document.resolve(branch)
    if not isinstance(resolved, dict):
        return ("unresolved",)
    return (
        _branch_table(document, branch),
        tuple(sorted(map(str, resolved.get("required", [])))),
    )


def _branch_identity(
    document: Document,
    branches: list[Any],
    *,
    include_table: bool = True,
) -> list[tuple[Any, ...]]:
    """Identify branches across documents without using component names.

    An explicit collector table marker is the strongest identity. When every
    branch has a distinct marker, use it alone so that a change to ``required``
    is diagnosed on the branch instead of hiding the branch correspondence.
    Otherwise fall back to the structural table/required key.
    """
    if not include_table:
        return [
            (
                "shape",
                tuple(
                    sorted(
                        map(
                            str,
                            (document.resolve(branch) or {}).get("required", []),
                        )
                    )
                ),
            )
            for branch in branches
        ]
    tables = [_branch_table(document, branch) for branch in branches]
    if all(table is not None for table in tables) and len(set(tables)) == len(tables):
        return [("table", table) for table in tables]
    return [("shape", *_branch_key(document, branch)) for branch in branches]


def match_branches(
    document: Document,
    branches: list[Any],
    instance: Any,
    location: str,
) -> tuple[list[int], list[str]]:
    """Return accepting branch indices and any unsupported-scope notes."""
    matches: list[int] = []
    unsupported: list[str] = []
    for index, branch in enumerate(branches):
        result = evaluate(
            document, branch, instance, f"{location}.oneOf[{index}]"
        )
        unsupported.extend(result.unsupported)
        if result.accepted:
            matches.append(index)
    return matches, unsupported


def compare_response(
    old_doc: Document,
    new_doc: Document,
    old_schema: Any,
    new_schema: Any,
    location: str,
    samples: list[Any] | None = None,
) -> list[Finding]:
    """Check that new response branches route to the same old parser branches."""
    comparison = Comparison()
    samples = samples or []
    try:
        old_envelope = old_doc.resolve(old_schema)
        new_envelope = new_doc.resolve(new_schema)
        if not isinstance(old_envelope, dict) or not isinstance(new_envelope, dict):
            raise ValueError("response schema is not an object")
        old_branches = _one_of(old_envelope, f"{location}.old")
        new_branches = _one_of(new_envelope, f"{location}.new")
    except (KeyError, ValueError, IndexError) as exc:
        return [
            Finding(
                Status.UNKNOWN,
                location,
                f"cannot resolve response oneOf: {exc}",
                None,
                None,
            )
        ]
    if old_branches is None or new_branches is None:
        return [
            Finding(
                Status.UNKNOWN,
                location,
                "response is not a oneOf union; branch routing cannot be proven",
            )
        ]

    old_keys = _branch_identity(old_doc, old_branches)
    new_keys = _branch_identity(new_doc, new_branches)
    old_tables = [_branch_table(old_doc, branch) for branch in old_branches]
    new_tables = [_branch_table(new_doc, branch) for branch in new_branches]
    missing_markers = [
        ("old", index) for index, table in enumerate(old_tables) if table is None
    ] + [
        ("new", index) for index, table in enumerate(new_tables) if table is None
    ]
    if missing_markers:
        comparison.add(
            Status.UNKNOWN,
            f"{location}.oneOf",
            "a collector table marker is missing; table routing cannot be "
            "proved even when sample responses validate",
            missing_markers,
        )
        old_keys = _branch_identity(old_doc, old_branches, include_table=False)
        new_keys = _branch_identity(new_doc, new_branches, include_table=False)
    if len(set(old_keys)) != len(old_keys):
        comparison.add(
            Status.UNKNOWN,
            f"{location}.oneOf",
            "old branches do not have unique table/required identities",
            old_keys,
        )
    if len(set(new_keys)) != len(new_keys):
        comparison.add(
            Status.UNKNOWN,
            f"{location}.oneOf",
            "new branches do not have unique table/required identities",
            new_keys,
        )

    mapping: dict[int, int] = {}
    reordered = new_keys != old_keys and sorted(map(str, new_keys)) == sorted(
        map(str, old_keys)
    )
    for new_index, new_key in enumerate(new_keys):
        candidates = [
            old_index
            for old_index, old_key in enumerate(old_keys)
            if old_key == new_key
        ]
        if len(candidates) != 1:
            comparison.add(
                Status.BREAKING,
                f"{location}.oneOf.new[{new_index}]",
                "new branch has no unique old branch with the same table/required "
                "identity",
                old_keys,
                new_key,
            )
            continue
        old_index = candidates[0]
        mapping[new_index] = old_index
        comparison.add(
            Status.EQUIVALENT,
            f"{location}.oneOf.new[{new_index}]",
            "branch maps to one old branch by table or required fields",
            old_keys[old_index],
            new_key,
        )
        compare_schema(
            old_doc,
            old_branches[old_index],
            new_doc,
            new_branches[new_index],
            f"{location}.oneOf.new[{new_index}]",
            "response",
            comparison,
        )

    if reordered:
        comparison.add(
            Status.EQUIVALENT,
            f"{location}.oneOf",
            "oneOf branch order changed but branch identities are the same",
            old_keys,
            new_keys,
        )

    for index, sample in enumerate(samples):
        sample_location = f"{location}.samples[{index}]"
        old_matches, old_unsupported = match_branches(
            old_doc, old_branches, sample, f"{location}.old"
        )
        new_matches, new_unsupported = match_branches(
            new_doc, new_branches, sample, f"{location}.new"
        )
        if old_unsupported or new_unsupported:
            comparison.add(
                Status.UNKNOWN,
                sample_location,
                "sample uses constructs outside the supported rule set",
                old_unsupported,
                new_unsupported,
            )
            continue
        if len(new_matches) != 1:
            comparison.add(
                Status.UNKNOWN,
                sample_location,
                "new contract does not select exactly one branch; the new "
                "parser cannot route this response",
                old_matches,
                new_matches,
            )
            continue
        if not old_matches:
            comparison.add(
                Status.BREAKING,
                sample_location,
                "new branch accepts a response the old parser rejects",
                old_matches,
                new_matches,
            )
        elif len(old_matches) > 1:
            comparison.add(
                Status.BREAKING,
                sample_location,
                "new contract selects one branch but the old contract matches "
                "several; the old parser treats this response as invalid and "
                "cannot route it to a table",
                old_matches,
                new_matches,
            )
        else:
            old_index = old_matches[0]
            new_index = new_matches[0]
            old_table = _branch_table(old_doc, old_branches[old_index])
            new_table = _branch_table(new_doc, new_branches[new_index])
            if old_table is None or new_table is None:
                comparison.add(
                    Status.UNKNOWN,
                    sample_location,
                    "a branch has no collector table marker; routing is not "
                    "provable from the supported rules",
                    old_table,
                    new_table,
                )
            elif old_table != new_table or mapping.get(new_index) != old_index:
                comparison.add(
                    Status.BREAKING,
                    sample_location,
                    "new branch routes to a different table than the old branch",
                    old_table,
                    new_table,
                )
            else:
                comparison.add(
                    Status.COMPATIBLE,
                    sample_location,
                    "response is accepted by exactly one branch on both sides "
                    "and routes to the same table",
                    old_table,
                    new_table,
                )
    return comparison.findings


# ---------------------------------------------------------------------------
# Document helpers
# ---------------------------------------------------------------------------


def operation(document: Document, path: str, method: str) -> tuple[dict, dict]:
    """Return ``(path_item, operation)`` for a path/method."""
    path_item = document.raw["paths"][path]
    return path_item, path_item[method]


def response_schema(document: Document, operation_node: dict) -> Any:
    """Return the 200 response schema, following a local response reference."""
    resolved = document.resolve(operation_node["responses"]["200"])
    return resolved["content"]["application/json"]["schema"]


def summarize(findings: list[Finding]) -> Status:
    """Combine findings, with breaking taking precedence over unknown."""
    statuses = {finding.status for finding in findings}
    if Status.BREAKING in statuses:
        return Status.BREAKING
    if Status.UNKNOWN in statuses:
        return Status.UNKNOWN
    if Status.COMPATIBLE in statuses and Status.EQUIVALENT not in statuses:
        return Status.COMPATIBLE
    return Status.EQUIVALENT


def load(path: str) -> Document:
    """Load a JSON or YAML OpenAPI document."""
    with open(path) as handle:
        raw = json.load(handle) if path.endswith(".json") else _yaml(handle)
    return Document(raw, label=path)


def _yaml(handle: Any) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(handle)

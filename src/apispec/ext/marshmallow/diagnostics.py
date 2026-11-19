"""Generation-side diagnostics for marshmallow field conversion.

When a marshmallow field cannot be translated to an OpenAPI schema (for
example because it is a custom field without a type mapping), apispec records
the problem in a :class:`ConversionReport`.

The report is generation-side information only.  It is never written to the
generated specification, so ``APISpec.to_dict()`` and ``APISpec.to_yaml()``
keep their usual structure.

A report entry contains the field, the nesting path that led to it (schema and
field names) and the operation or component that uses it.  When the same
unconvertible field is reached from several operations, one reference is
recorded per operation.
"""

from __future__ import annotations

import logging
import typing
from contextlib import contextmanager
from dataclasses import dataclass, field

__all__ = [
    "ConversionOccurrence",
    "ConversionReference",
    "ConversionReport",
    "ConversionTracker",
    "FieldConversionIssue",
]

logger = logging.getLogger(__name__)

_COMPONENT_PLURALS = {
    "schema": "schemas",
    "response": "responses",
    "parameter": "parameters",
    "header": "headers",
    "example": "examples",
    "link": "links",
    "security_scheme": "securitySchemes",
}

_HTTP_METHODS = {
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
}


class UnconvertibleFieldWarning(UserWarning):
    """Warning category for fields that have no OpenAPI representation."""


def _component_location(component_type: str | None, component_name: str | None) -> str:
    if not component_type or not component_name:
        return "components"
    return (
        f"components.{_COMPONENT_PLURALS.get(component_type, component_type)}"
        f".{component_name}"
    )


@dataclass(frozen=True)
class ConversionReference:
    """A place in the generated document that used a marshmallow field."""

    kind: str = "conversion"
    path: str | None = None
    method: str | None = None
    operation_id: str | None = None
    component_type: str | None = None
    component_name: str | None = None
    location: str | None = None

    @classmethod
    def operation(
        cls, path: str | None, method: str | None, operation: dict | None = None
    ) -> ConversionReference:
        operation = operation or {}
        method = method.lower() if isinstance(method, str) else method
        return cls(
            kind="operation",
            path=path,
            method=method,
            operation_id=operation.get("operationId"),
            location=(f"paths.{path}.{method}" if path is not None and method else None),
        )

    @classmethod
    def component(cls, component_type: str, component_name: str) -> ConversionReference:
        return cls(
            kind="component",
            component_type=component_type,
            component_name=component_name,
            location=_component_location(component_type, component_name),
        )

    @classmethod
    def conversion(cls, label: str | None = None) -> ConversionReference:
        return cls(kind="conversion", location=label)

    @property
    def operation_data(self) -> dict[str, str] | None:
        """Return the operation as a small dictionary, if this is an operation."""
        if self.kind != "operation":
            return None
        data = {}
        if self.path is not None:
            data["path"] = self.path
        if self.method is not None:
            data["method"] = self.method
        if self.operation_id is not None:
            data["operationId"] = self.operation_id
        return data

    @property
    def operation_ref(self) -> dict[str, str] | None:
        return self.operation_data

    @property
    def interface(self) -> str:
        """Human readable description of the interface that used the field."""
        if self.kind == "operation":
            interface = (
                f"{self.method.upper() if self.method else ''} {self.path or ''}"
            ).strip()
            if self.operation_id:
                interface = f"{interface} (operationId: {self.operation_id})"
            return interface
        if self.kind == "component":
            return self.location or _component_location(
                self.component_type, self.component_name
            )
        return self.location or "field conversion"

    def describe(self) -> str:
        return self.interface

    def key(self) -> tuple[typing.Any, ...]:
        return (
            self.kind,
            self.path,
            self.method,
            self.operation_id,
            self.component_type,
            self.component_name,
            self.location,
        )

    def to_dict(self) -> dict[str, typing.Any]:
        ret: dict[str, typing.Any] = {"kind": self.kind}
        if self.kind == "operation":
            ret.update({"path": self.path, "method": self.method})
            if self.operation_id is not None:
                ret["operationId"] = self.operation_id
        elif self.kind == "component":
            ret.update(
                {
                    "component_type": self.component_type,
                    "component_name": self.component_name,
                }
            )
        if self.location is not None:
            ret["location"] = self.location
        return ret


@dataclass
class FieldConversionIssue:
    """One field that could not be converted, independent of where it is used."""

    field_class: type
    field_name: str | None
    schema_class: type | None
    schema_name: str | None
    field_path: tuple[str, ...]
    schema_path: tuple[str, ...]
    kind: str
    message: str
    component_type: str | None = "schema"
    schema_key: tuple | None = None
    references: list[ConversionReference] = field(default_factory=list)

    @property
    def field_type(self) -> type:
        return self.field_class

    @property
    def field_type_name(self) -> str:
        return f"{self.field_class.__module__}.{self.field_class.__qualname__}"

    @property
    def path(self) -> str:
        if self.schema_path:
            return _join_path(self.schema_path, self.field_path)
        return _schema_qualified_path(
            self.schema_name, self.schema_class, self.field_path
        )

    @property
    def operation(self) -> dict[str, str] | None:
        for reference in self.references:
            if reference.kind == "operation":
                return reference.operation_data
        return None

    @property
    def operations(self) -> list[dict[str, str]]:
        return [
            operation
            for reference in self.references
            if (operation := reference.operation_data) is not None
        ]

    @property
    def operation_references(self) -> list[ConversionReference]:
        return [r for r in self.references if r.kind == "operation"]

    def add_reference(self, reference: ConversionReference) -> None:
        if reference not in self.references:
            self.references.append(reference)

    def key(self) -> tuple[typing.Any, ...]:
        schema_identity = (
            self.schema_key
            if self.schema_key is not None
            else (self.component_type, self.schema_name, self.schema_class)
        )
        return (self.field_class, schema_identity, self.field_path, self.kind)

    def to_dict(self) -> dict[str, typing.Any]:
        ret: dict[str, typing.Any] = {
            "field": self.field_type_name,
            "field_name": self.field_name,
            "schema": self.schema_name,
            "path": self.path,
            "field_path": list(self.field_path),
            "schema_path": list(self.schema_path),
            "kind": self.kind,
            "message": self.message,
            "references": [reference.to_dict() for reference in self.references],
        }
        if self.operations:
            ret["operations"] = self.operations
        return ret

    def __str__(self) -> str:
        return self.message


@dataclass
class ConversionOccurrence:
    """A field conversion issue at one particular usage site."""

    issue: FieldConversionIssue
    reference: ConversionReference
    field_path: tuple[str, ...]
    schema_path: tuple[str, ...]

    @property
    def path(self) -> str:
        if self.schema_path:
            return _join_path(self.schema_path, self.field_path)
        return _schema_qualified_path(
            self.issue.schema_name, self.issue.schema_class, self.field_path
        )

    @property
    def schema_name(self) -> str | None:
        return self.issue.schema_name

    @property
    def field_name(self) -> str | None:
        return self.issue.field_name

    @property
    def field_class(self) -> type:
        return self.issue.field_class

    @property
    def field_type_name(self) -> str:
        return self.issue.field_type_name

    @property
    def kind(self) -> str:
        return self.issue.kind

    @property
    def message(self) -> str:
        return self.issue.message

    @property
    def operation(self) -> dict[str, str] | None:
        return self.reference.operation_data

    @property
    def operations(self) -> list[dict[str, str]]:
        return self.issue.operations

    @property
    def references(self) -> list[ConversionReference]:
        return self.issue.references

    def key(self) -> tuple[typing.Any, ...]:
        return (self.issue.key(), self.reference.key())

    def to_dict(self) -> dict[str, typing.Any]:
        ret = self.issue.to_dict()
        ret["path"] = self.path
        ret["field_path"] = list(self.field_path)
        ret["schema_path"] = list(self.schema_path)
        ret["reference"] = self.reference.to_dict()
        ret["operation"] = self.operation
        return ret

    def __str__(self) -> str:
        nesting = " -> ".join(name for name in self.schema_path if name)
        nesting_text = f"; nesting: {nesting}" if nesting else ""
        return (
            f"Cannot convert field {self.field_type_name} at {self.path}"
            f"{nesting_text}; used by {self.reference.describe()}."
        )


class ConversionReport:
    """Collection of unconvertible field diagnostics.

    Iterating over the report yields one :class:`ConversionOccurrence` per
    usage site.  ``report.issues`` yields unique field issues.  This is what
    makes a field reused by two operations visible as two entries/references
    while still being a single underlying issue.
    """

    def __init__(self) -> None:
        self._issues: list[FieldConversionIssue] = []
        self._occurrences: list[ConversionOccurrence] = []
        self._seen_occurrences: set[tuple[typing.Any, ...]] = set()

    @property
    def issues(self) -> list[FieldConversionIssue]:
        return list(self._issues)

    @property
    def occurrences(self) -> list[ConversionOccurrence]:
        return list(self._occurrences)

    @property
    def entries(self) -> list[ConversionOccurrence]:
        return self.occurrences

    @property
    def references(self) -> list[ConversionReference]:
        return [occurrence.reference for occurrence in self._occurrences]

    def __iter__(self) -> typing.Iterator[ConversionOccurrence]:
        return iter(self._occurrences)

    def __len__(self) -> int:
        return len(self._occurrences)

    def __bool__(self) -> bool:
        return bool(self._occurrences)

    def __getitem__(
        self, index: int | slice
    ) -> ConversionOccurrence | list[ConversionOccurrence]:
        return self._occurrences[index]

    def add_issue(self, issue: FieldConversionIssue) -> FieldConversionIssue:
        for existing in self._issues:
            if existing.key() == issue.key():
                return existing
        self._issues.append(issue)
        return issue

    def add(
        self,
        issue: FieldConversionIssue,
        reference: ConversionReference,
        *,
        field_path: tuple[str, ...],
        schema_path: tuple[str, ...],
    ) -> ConversionOccurrence | None:
        issue = self.add_issue(issue)
        issue.add_reference(reference)
        occurrence_key = (issue.key(), reference.key())
        if occurrence_key in self._seen_occurrences:
            return None
        self._seen_occurrences.add(occurrence_key)
        occurrence = ConversionOccurrence(
            issue=issue,
            reference=reference,
            field_path=field_path,
            schema_path=schema_path,
        )
        self._occurrences.append(occurrence)
        return occurrence

    def for_operation(
        self, path: str | None = None, method: str | None = None
    ) -> list[ConversionOccurrence]:
        """Return occurrences for an operation, optionally filtered."""
        return [
            occurrence
            for occurrence in self._occurrences
            if occurrence.reference.kind == "operation"
            and (path is None or occurrence.reference.path == path)
            and (method is None or occurrence.reference.method == method.lower())
        ]

    def to_dict(self) -> list[dict[str, typing.Any]]:
        return [occurrence.to_dict() for occurrence in self._occurrences]

    def extend(self, other: ConversionReport) -> ConversionReport:
        for occurrence in other:
            self.add(
                occurrence.issue,
                occurrence.reference,
                field_path=occurrence.field_path,
                schema_path=occurrence.schema_path,
            )
        return self

    def clear(self) -> None:
        self._issues.clear()
        self._occurrences.clear()
        self._seen_occurrences.clear()

    def log(self, logger_: logging.Logger | None = None) -> None:
        """Log every occurrence at warning level."""
        log = logger_ or logger
        for occurrence in self._occurrences:
            log.warning("%s", occurrence)

    def __repr__(self) -> str:
        return (
            f"<ConversionReport issues={len(self._issues)} "
            f"occurrences={len(self._occurrences)}>"
        )


def _join_path(
    schema_path: tuple[str, ...], field_path: tuple[str, ...]
) -> str:
    parts = [name for name in schema_path if name]
    if not parts:
        return ".".join(field_path) or "<root>"
    return ".".join([parts[0], *field_path])


def _schema_qualified_path(
    schema_name: str | None,
    schema_class: type | None,
    field_path: tuple[str, ...],
) -> str:
    if schema_name:
        return ".".join([schema_name, *field_path])
    if schema_class is not None:
        name = schema_class.__name__
        if name.endswith("Schema"):
            name = name[: -len("Schema")] or name
        return ".".join([name, *field_path])
    return ".".join(field_path) or "<root>"


@dataclass
class _SchemaFrame:
    key: tuple
    name: str | None
    schema_class: type | None
    component_type: str | None
    field_start: int


class ConversionTracker:
    """Mutable state used while a converter walks schemas and operations.

    Issues are recorded during field conversion.  Usage references are added
    when operations are resolved and when the final document is built, by
    walking the schemas an operation uses.  This keeps attribution independent
    of schema registration order.
    """

    def __init__(self) -> None:
        self.report = ConversionReport()
        self._contexts: list[ConversionReference] = []
        self._field_path: list[str] = []
        self._schema_frames: list[_SchemaFrame] = []
        self._issues: list[FieldConversionIssue] = []
        self._issues_by_key: dict[tuple, FieldConversionIssue] = {}
        self._logged_occurrences: set[tuple[typing.Any, ...]] = set()

    # ------------------------------------------------------------------
    # Conversion contexts
    # ------------------------------------------------------------------
    @contextmanager
    def conversion_context(
        self,
        *,
        path: str | None = None,
        method: str | None = None,
        operation: dict | None = None,
    ) -> typing.Iterator[ConversionReference]:
        reference = ConversionReference.operation(path, method, operation)
        self._contexts.append(reference)
        try:
            yield reference
        finally:
            self._contexts.pop()

    @contextmanager
    def schema_frame(
        self,
        key: tuple,
        name: str | None,
        schema_class: type | None,
        component_type: str | None = "schema",
    ) -> typing.Iterator[_SchemaFrame]:
        frame = _SchemaFrame(
            key, name, schema_class, component_type, len(self._field_path)
        )
        self._schema_frames.append(frame)
        try:
            yield frame
        finally:
            self._schema_frames.pop()

    @contextmanager
    def field(self, name: str) -> typing.Iterator[None]:
        self._field_path.append(name)
        try:
            yield
        finally:
            self._field_path.pop()

    def current_operation_reference(self) -> ConversionReference | None:
        for reference in reversed(self._contexts):
            if reference.kind == "operation":
                return reference
        return None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record_field(
        self,
        field: typing.Any,
        *,
        kind: str,
        message: str,
    ) -> FieldConversionIssue | None:
        frames = self._schema_frames
        if frames:
            # Attribute the issue to the innermost named component.  Inline
            # nested schemas keep their field path relative to it.
            target = next(
                (frame for frame in reversed(frames) if frame.name), frames[-1]
            )
            field_path = tuple(self._field_path[target.field_start :])
            schema_key = target.key
            schema_class = target.schema_class
            schema_name = target.name
            component_type = target.component_type
        else:
            field_name = getattr(field, "name", None) or getattr(
                field, "data_key", None
            )
            field_path = tuple(self._field_path) or (
                (field_name,) if field_name else ()
            )
            schema_key = None
            schema_class = None
            schema_name = None
            component_type = None

        issue = FieldConversionIssue(
            field_class=type(field),
            field_name=field_path[-1] if field_path else None,
            schema_class=schema_class,
            schema_name=schema_name,
            field_path=field_path,
            schema_path=(),
            kind=kind,
            message=message,
            component_type=component_type,
            schema_key=schema_key,
        )
        existing = self._issues_by_key.get(issue.key())
        if existing is not None:
            if schema_name is None:
                # Inline schemas are expanded again for each operation, so the
                # same issue must be attached to each one.
                self._attach_inline_occurrence(existing)
            return existing
        self._issues_by_key[issue.key()] = issue
        self._issues.append(issue)
        self.report.add_issue(issue)

        if schema_name is not None:
            # Attribution to operations is done from the final document so the
            # full nesting path is known.
            return issue

        self._attach_inline_occurrence(issue)
        return issue

    def _attach_inline_occurrence(self, issue: FieldConversionIssue) -> None:
        reference = self.current_operation_reference()
        if reference is None:
            return
        occurrence = self.report.add(
            issue,
            reference,
            field_path=issue.field_path,
            schema_path=(),
        )
        if occurrence is not None:
            self._log(occurrence)

    def has_issues(self) -> bool:
        return bool(self._issues)

    # ------------------------------------------------------------------
    # Attribution
    # ------------------------------------------------------------------
    def attribute_operation(
        self,
        path: str | None,
        method: str,
        operation: dict,
        components: typing.Any,
        *,
        log: bool = True,
    ) -> list[ConversionOccurrence]:
        """Attach recorded issues to an operation that uses their schema."""
        if not self._issues:
            return []
        reference = ConversionReference.operation(path, method, operation)
        usages = collect_schema_usages(operation, components)
        added = []

        for issue in self._issues:
            if issue.schema_name is None:
                continue
            # One occurrence per schema nesting route, using the shortest field
            # prefix that reaches the schema on that route.
            best_by_route: dict[tuple[str, ...], tuple[str, ...]] = {}
            for schema_name, schema_path, field_prefix in usages:
                if schema_name != issue.schema_name:
                    continue
                current = best_by_route.get(schema_path)
                if current is None or len(field_prefix) < len(current):
                    best_by_route[schema_path] = field_prefix
            for schema_path, field_prefix in best_by_route.items():
                occurrence = self.report.add(
                    issue,
                    reference,
                    field_path=field_prefix + issue.field_path,
                    schema_path=schema_path,
                )
                if occurrence is not None:
                    added.append(occurrence)
                    if log:
                        self._log(occurrence)
        return added

    def build_report(
        self,
        spec: typing.Any = None,
        *,
        log: bool = True,
        include_components: bool = True,
    ) -> ConversionReport:
        """Attribute issues to every operation and component of the document.

        The generated specification is only read, never modified.
        """
        if spec is not None and spec._paths:
            for path, path_item in spec._paths.items():
                if not isinstance(path_item, dict):
                    continue
                for method, operation in path_item.items():
                    if method in _HTTP_METHODS and isinstance(operation, dict):
                        self.attribute_operation(
                            path,
                            method,
                            operation,
                            spec.components,
                            log=log,
                        )
        if include_components:
            self._attribute_unreferenced_components(log=log)
        return self.report

    def _attribute_unreferenced_components(self, *, log: bool) -> None:
        for issue in self._issues:
            if issue.references:
                continue
            reference = (
                ConversionReference.component(issue.component_type, issue.schema_name)
                if issue.component_type and issue.schema_name
                else ConversionReference.conversion()
            )
            occurrence = self.report.add(
                issue,
                reference,
                field_path=issue.field_path,
                schema_path=(issue.schema_name,) if issue.schema_name else (),
            )
            if occurrence is not None and log:
                self._log(occurrence)

    def _log(self, occurrence: ConversionOccurrence) -> None:
        key = occurrence.key()
        if key in self._logged_occurrences:
            return
        self._logged_occurrences.add(key)
        logger.warning("%s", occurrence)

    def clear(self) -> None:
        self.report.clear()
        self._issues.clear()
        self._issues_by_key.clear()
        self._logged_occurrences.clear()
        self._field_path.clear()
        self._schema_frames.clear()
        self._contexts.clear()


def collect_schema_usages(
    operation: typing.Any, components: typing.Any
) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """Return ``(schema_name, schema_nesting, field_path)`` for an operation.

    ``schema_nesting`` is the chain of schema names from the first referenced
    schema to ``schema_name``.  ``field_path`` is the field path accumulated
    along the way (property names and ``[]`` for list items).
    """
    usages: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    seen: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()

    def add(
        schema_name: str,
        schema_nesting: tuple[str, ...],
        field_path: tuple[str, ...],
    ) -> None:
        key = (schema_name, schema_nesting, field_path)
        if key not in seen:
            seen.add(key)
            usages.append(key)
        schema = components.schemas.get(schema_name)
        if isinstance(schema, dict):
            walk_schema(schema, schema_name, schema_nesting, field_path, set())

    def ref_name(node: typing.Any) -> str | None:
        if not isinstance(node, dict):
            return None
        ref = node.get("$ref")
        if not isinstance(ref, str):
            return None
        if "schemas" in ref or "definitions" in ref:
            return ref.rsplit("/", 1)[-1]
        return None

    def walk_schema(
        schema: dict,
        schema_name: str,
        schema_nesting: tuple[str, ...],
        field_path: tuple[str, ...],
        stack: set[str],
    ) -> None:
        if schema_name in stack:
            return
        stack = stack | {schema_name}

        def walk_subschema(
            node: typing.Any, path: tuple[str, ...]
        ) -> None:
            name = ref_name(node)
            if name is not None:
                add(name, schema_nesting + (name,), path)
                return
            if isinstance(node, dict):
                properties = node.get("properties")
                if isinstance(properties, dict):
                    for prop_name, prop in properties.items():
                        walk_subschema(prop, path + (prop_name,))
                if "items" in node:
                    walk_subschema(node["items"], path + ("[]",))
                for keyword in ("allOf", "oneOf", "anyOf"):
                    subschemas = node.get(keyword)
                    if isinstance(subschemas, list):
                        for subschema in subschemas:
                            walk_subschema(subschema, path)
                if "not" in node:
                    walk_subschema(node["not"], path)
                additional = node.get("additionalProperties")
                if isinstance(additional, dict):
                    walk_subschema(additional, path + ("*",))

        walk_subschema(schema, field_path)

    def walk_operation(node: typing.Any, stack: set[str]) -> None:
        name = ref_name(node)
        if name is not None:
            add(name, (name,), ())
            return
        if isinstance(node, dict):
            for value in node.values():
                walk_operation(value, stack)
        elif isinstance(node, list):
            for value in node:
                walk_operation(value, stack)
        elif isinstance(node, str) and node in components.schemas:
            add(node, (node,), ())

    walk_operation(operation, set())
    return usages

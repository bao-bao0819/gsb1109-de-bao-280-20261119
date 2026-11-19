"""Utilities for checking whether a new OpenAPI document breaks old clients.

The comparison is operation-centric.  Schemas are resolved before they are
compared, so renaming a component while updating all of its references does not
show up as a breaking change.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import typing
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

_METHOD_ORDER = {
    "get": 0,
    "post": 1,
    "put": 2,
    "patch": 3,
    "delete": 4,
    "head": 5,
    "options": 6,
    "trace": 7,
}
_HTTP_METHODS = frozenset(_METHOD_ORDER)
_MISSING = object()


@dataclass(frozen=True)
class CompatibilityIssue:
    """A single compatibility change affecting one operation and property."""

    operation: str
    method: str
    path: str
    location: str
    property: str
    kind: str
    breaking: bool
    message: str
    old: typing.Any = None
    new: typing.Any = None
    media_type: str | None = None
    status_code: str | None = None

    @property
    def name(self) -> str:
        """Return the leaf name of the affected property."""
        value = self.property
        for separator in (".", "[]"):
            if separator == "[]":
                value = value.replace("[]", "")
            else:
                value = value.rsplit(separator, 1)[-1]
        return value

    @property
    def property_path(self) -> str:
        """Alias for :attr:`property`."""
        return self.property

    @property
    def change_type(self) -> str:
        """Alias for :attr:`kind`."""
        return self.kind

    @property
    def scope(self) -> str:
        """Alias for :attr:`location`."""
        return self.location

    @property
    def severity(self) -> str:
        """Return ``"breaking"`` or ``"non-breaking"``."""
        return "breaking" if self.breaking else "non-breaking"

    def to_dict(self) -> dict[str, typing.Any]:
        """Return the issue as a JSON-serializable dictionary."""
        return {
            "operation": self.operation,
            "method": self.method,
            "path": self.path,
            "location": self.location,
            "scope": self.location,
            "property": self.property,
            "property_path": self.property,
            "name": self.name,
            "kind": self.kind,
            "change_type": self.kind,
            "breaking": self.breaking,
            "severity": self.severity,
            "message": self.message,
            "old": self.old,
            "new": self.new,
            "media_type": self.media_type,
            "status_code": self.status_code,
        }

    def __getitem__(self, key: str) -> typing.Any:
        return self.to_dict()[key]

    def get(self, key: str, default: typing.Any = None) -> typing.Any:
        """Return a field by name, like a mapping."""
        return self.to_dict().get(key, default)

    def __str__(self) -> str:
        return self.message


@dataclass
class CompatibilityReport(Sequence[CompatibilityIssue]):
    """The result of comparing two OpenAPI documents."""

    changes: list[CompatibilityIssue] = field(default_factory=list)

    def __iter__(self) -> Iterator[CompatibilityIssue]:
        return iter(self.changes)

    def __len__(self) -> int:
        return len(self.changes)

    def __getitem__(
        self, index: int | slice
    ) -> CompatibilityIssue | list[CompatibilityIssue]:
        return self.changes[index]

    @property
    def issues(self) -> list[CompatibilityIssue]:
        """Alias for :attr:`changes`."""
        return self.changes

    @property
    def breaking_changes(self) -> list[CompatibilityIssue]:
        """Return only changes that may break old clients."""
        return [change for change in self.changes if change.breaking]

    @property
    def non_breaking_changes(self) -> list[CompatibilityIssue]:
        """Return changes that old clients can tolerate."""
        return [change for change in self.changes if not change.breaking]

    @property
    def compatible(self) -> bool:
        """Whether all detected changes are backward compatible."""
        return not self.breaking_changes

    @property
    def is_compatible(self) -> bool:
        """Alias for :attr:`compatible`."""
        return self.compatible

    @property
    def breaking(self) -> bool:
        """Whether any breaking change was detected."""
        return bool(self.breaking_changes)

    @property
    def affected_operations(self) -> list[str]:
        """Operations with at least one detected change."""
        return sorted({change.operation for change in self.changes})

    @property
    def affected_properties(self) -> list[str]:
        """Properties with at least one detected change."""
        return sorted({change.property for change in self.changes})

    def to_dict(self) -> dict[str, typing.Any]:
        """Return the report as a JSON-serializable dictionary."""
        return {
            "compatible": self.compatible,
            "is_compatible": self.compatible,
            "breaking": self.breaking,
            "breaking_count": len(self.breaking_changes),
            "change_count": len(self.changes),
            "affected_operations": self.affected_operations,
            "affected_properties": self.affected_properties,
            "changes": [change.to_dict() for change in self.changes],
        }

    def to_json(self, **kwargs: typing.Any) -> str:
        """Serialize the report to JSON."""
        kwargs.setdefault("default", repr)
        return json.dumps(self.to_dict(), **kwargs)

    def summary(self) -> str:
        """Return a short human-readable summary."""
        if not self.changes:
            return "No compatibility changes detected."
        breaking = len(self.breaking_changes)
        total = len(self.changes)
        if breaking:
            return f"{breaking} breaking change(s) detected in {total} change(s)."
        return f"No breaking changes detected in {total} change(s)."


@dataclass(frozen=True)
class _Field:
    path: str
    required: bool
    enum: tuple[typing.Any, ...] | None = None
    schema: Mapping[str, typing.Any] = field(default_factory=dict)
    base: bool = False


class _Resolver:
    """Resolve local JSON references in a single document."""

    def __init__(self, spec: Mapping[str, typing.Any]) -> None:
        self.spec = spec

    def lookup(self, ref: str) -> typing.Any:
        if not isinstance(ref, str) or not ref.startswith("#"):
            return _MISSING
        fragment = ref[1:]
        if not fragment:
            return self.spec
        if not fragment.startswith("/"):
            return _MISSING
        node: typing.Any = self.spec
        for raw_part in fragment[1:].split("/"):
            part = unquote(raw_part).replace("~1", "/").replace("~0", "~")
            if isinstance(node, Mapping):
                if part not in node:
                    return _MISSING
                node = node[part]
            elif isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    return _MISSING
            else:
                return _MISSING
        return node

    def deref(self, obj: typing.Any) -> typing.Any:
        seen: set[str] = set()
        while isinstance(obj, Mapping) and "$ref" in obj:
            ref = obj["$ref"]
            if not isinstance(ref, str) or ref in seen:
                break
            seen.add(ref)
            target = self.lookup(ref)
            if target is _MISSING:
                break
            siblings = {key: value for key, value in obj.items() if key != "$ref"}
            if siblings and isinstance(target, Mapping):
                target = {**target, **siblings}
            obj = target
        return obj


def _canonical(value: typing.Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    except (TypeError, ValueError):
        return repr(value)


def _enum_map(values: typing.Iterable[typing.Any]) -> dict[str, typing.Any]:
    result: dict[str, typing.Any] = {}
    for value in values:
        result.setdefault(_canonical(value), value)
    return result


def _enum_values(mapping: Mapping[str, typing.Any]) -> tuple[typing.Any, ...]:
    return tuple(mapping.values())


def _merge_enums(
    left: tuple[typing.Any, ...] | None,
    right: tuple[typing.Any, ...] | None,
    mode: str,
) -> tuple[typing.Any, ...] | None:
    if left is None:
        return right
    if right is None:
        return left if mode == "allOf" else None
    left_map = _enum_map(left)
    right_map = _enum_map(right)
    if mode == "allOf":
        keys = [key for key in left_map if key in right_map]
        return _enum_values({key: left_map[key] for key in keys})
    merged = dict(left_map)
    for key, value in right_map.items():
        merged.setdefault(key, value)
    return _enum_values(merged)


def _effective_enum(
    schema: typing.Any,
    resolver: _Resolver,
    ancestors: frozenset[str] = frozenset(),
) -> tuple[typing.Any, ...] | None:
    if not isinstance(schema, Mapping):
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in ancestors:
            return None
        target = resolver.lookup(ref)
        if target is _MISSING:
            return None
        return _effective_enum(target, resolver, ancestors | {ref})

    enum = schema.get("enum")
    result = tuple(enum) if isinstance(enum, list) else None

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            branch_enum = _effective_enum(branch, resolver, ancestors)
            if branch_enum is not None:
                result = _merge_enums(result, branch_enum, "allOf")

    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if not isinstance(branches, list) or not branches:
            continue
        branch_enums = [
            _effective_enum(branch, resolver, ancestors) for branch in branches
        ]
        if all(branch_enum is not None for branch_enum in branch_enums):
            union: tuple[typing.Any, ...] | None = None
            for branch_enum in branch_enums:
                union = _merge_enums(union, branch_enum, "anyOf")
            if union is not None:
                result = _merge_enums(result, union, "allOf")
    return result


def _merge_field(left: _Field, right: _Field, mode: str) -> _Field:
    if mode == "allOf":
        required = left.required or right.required
    else:
        required = left.required and right.required
    return _Field(
        path=left.path,
        required=required,
        enum=_merge_enums(left.enum, right.enum, mode),
        schema=left.schema or right.schema,
        base=left.base or right.base,
    )


def _merge_fields(
    left: dict[str, _Field], right: dict[str, _Field], mode: str
) -> dict[str, _Field]:
    merged = dict(left)
    for path, field_value in right.items():
        if path in merged:
            merged[path] = _merge_field(merged[path], field_value, mode)
        else:
            merged[path] = field_value
    return merged


def _collect_fields(
    schema: typing.Any,
    path: str,
    required: bool,
    resolver: _Resolver,
    ancestors: frozenset[str] = frozenset(),
) -> dict[str, _Field]:
    if not isinstance(schema, Mapping):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in ancestors:
            return {}
        target = resolver.lookup(ref)
        if target is _MISSING:
            return {}
        return _collect_fields(target, path, required, resolver, ancestors | {ref})

    fields: dict[str, _Field] = {}
    required_names = {
        name for name in schema.get("required", []) if isinstance(name, str)
    }
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, property_schema in properties.items():
            if not isinstance(name, str):
                continue
            child_path = f"{path}.{name}" if path else name
            child_required = name in required_names
            resolved_property = resolver.deref(property_schema)
            child = _Field(
                path=child_path,
                required=child_required,
                enum=_effective_enum(property_schema, resolver, ancestors),
                schema=resolved_property
                if isinstance(resolved_property, Mapping)
                else {},
            )
            fields[child_path] = child
            fields = _merge_fields(
                fields,
                _collect_fields(
                    property_schema,
                    child_path,
                    child_required,
                    resolver,
                    ancestors,
                ),
                "allOf",
            )

    items = schema.get("items")
    if isinstance(items, Mapping):
        item_path = f"{path}[]" if path else "[]"
        resolved_item = resolver.deref(items)
        item_field = _Field(
            path=item_path,
            required=False,
            enum=_effective_enum(items, resolver, ancestors),
            schema=resolved_item if isinstance(resolved_item, Mapping) else {},
            base=True,
        )
        fields[item_path] = item_field
        fields = _merge_fields(
            fields,
            _collect_fields(items, item_path, False, resolver, ancestors),
            "allOf",
        )

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            fields = _merge_fields(
                fields,
                _collect_fields(branch, path, required, resolver, ancestors),
                "allOf",
            )

    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if not isinstance(branches, list) or not branches:
            continue
        branch_fields: dict[str, _Field] = {}
        for branch in branches:
            this_branch = _collect_fields(branch, path, required, resolver, ancestors)
            branch_fields = (
                this_branch
                if not branch_fields
                else _merge_fields(branch_fields, this_branch, "anyOf")
            )
        fields = _merge_fields(fields, branch_fields, "allOf")

    return fields


def _major_version(spec: Mapping[str, typing.Any]) -> int:
    openapi = spec.get("openapi")
    if isinstance(openapi, str):
        try:
            return int(openapi.split(".", 1)[0])
        except ValueError:
            pass
    if spec.get("swagger") is not None:
        return 2
    if "components" in spec:
        return 3
    if "definitions" in spec:
        return 2
    return 3


def _iter_operations(
    spec: Mapping[str, typing.Any], resolver: _Resolver
) -> Iterator[tuple[str, str, Mapping[str, typing.Any], list[typing.Any]]]:
    paths = spec.get("paths")
    if not isinstance(paths, Mapping):
        return
    for path, path_item in paths.items():
        if not isinstance(path, str):
            continue
        resolved_item = resolver.deref(path_item)
        if not isinstance(resolved_item, Mapping):
            continue
        common_parameters = resolved_item.get("parameters")
        if not isinstance(common_parameters, list):
            common_parameters = []
        for method, operation in resolved_item.items():
            if not isinstance(method, str) or method.lower() not in _HTTP_METHODS:
                continue
            resolved_operation = resolver.deref(operation)
            if isinstance(resolved_operation, Mapping):
                yield method.upper(), path, resolved_operation, common_parameters


def _operation_key(
    operation: tuple[str, str, Mapping[str, typing.Any], list[typing.Any]],
) -> tuple[str, str]:
    return operation[0].lower(), operation[1]


def _merged_parameters(
    common_parameters: list[typing.Any],
    operation: Mapping[str, typing.Any],
    resolver: _Resolver,
) -> list[Mapping[str, typing.Any]]:
    parameter_map: dict[tuple[str, str], Mapping[str, typing.Any]] = {}
    operation_parameters = operation.get("parameters")
    if not isinstance(operation_parameters, list):
        operation_parameters = []
    for raw_parameter in [*common_parameters, *operation_parameters]:
        parameter = resolver.deref(raw_parameter)
        if not isinstance(parameter, Mapping):
            continue
        name = parameter.get("name")
        location = parameter.get("in")
        if isinstance(name, str) and isinstance(location, str):
            parameter_map[(name, location)] = parameter
    return list(parameter_map.values())


def _add_field(
    fields: dict[str, _Field],
    path: str,
    required: bool,
    schema: typing.Any,
    resolver: _Resolver,
    *,
    base: bool = False,
    ancestors: frozenset[str] = frozenset(),
) -> None:
    resolved = resolver.deref(schema)
    new_field = _Field(
        path=path,
        required=required,
        enum=_effective_enum(schema, resolver, ancestors),
        schema=resolved if isinstance(resolved, Mapping) else {},
        base=base,
    )
    if path in fields:
        fields[path] = _merge_field(fields[path], new_field, "allOf")
    else:
        fields[path] = new_field


def _schema_for_parameter(
    parameter: Mapping[str, typing.Any], version: int
) -> list[tuple[typing.Any, str | None]]:
    if "content" in parameter:
        content = parameter.get("content")
        if not isinstance(content, Mapping):
            return []
        return [
            (media_object.get("schema"), media_type)
            for media_type, media_object in content.items()
            if isinstance(media_object, Mapping)
        ]
    if version < 3:
        return [(parameter.get("schema", parameter), None)]
    return [(parameter.get("schema"), None)]


def _request_fields(
    operation: Mapping[str, typing.Any],
    common_parameters: list[typing.Any],
    resolver: _Resolver,
    version: int,
) -> dict[str, _Field]:
    fields: dict[str, _Field] = {}
    for parameter in _merged_parameters(common_parameters, operation, resolver):
        location = parameter.get("in")
        name = parameter.get("name")
        required = bool(parameter.get("required"))
        if not isinstance(location, str):
            continue
        if location == "body":
            _add_field(
                fields, "body", required, parameter.get("schema"), resolver, base=True
            )
            fields = _merge_fields(
                fields,
                _collect_fields(parameter.get("schema"), "body", required, resolver),
                "allOf",
            )
            continue
        if not isinstance(name, str):
            continue
        if location == "formData":
            path = f"formData.{name}"
        else:
            path = f"{location}.{name}"
        for schema, _media_type in _schema_for_parameter(parameter, version):
            _add_field(fields, path, required, schema, resolver)
            fields = _merge_fields(
                fields,
                _collect_fields(schema, path, required, resolver),
                "allOf",
            )

    request_body = resolver.deref(operation.get("requestBody"))
    if isinstance(request_body, Mapping):
        required = bool(request_body.get("required"))
        _add_field(fields, "body", required, None, resolver, base=True)
        content = request_body.get("content")
        if isinstance(content, Mapping):
            for media_object in content.values():
                if not isinstance(media_object, Mapping):
                    continue
                schema = media_object.get("schema")
                _add_field(fields, "body", required, schema, resolver, base=True)
                fields = _merge_fields(
                    fields,
                    _collect_fields(schema, "body", required, resolver),
                    "allOf",
                )
    return fields


def _response_fields(
    operation: Mapping[str, typing.Any],
    resolver: _Resolver,
    version: int,
) -> dict[str, _Field]:
    responses = resolver.deref(operation.get("responses"))
    fields: dict[str, _Field] = {}
    if not isinstance(responses, Mapping):
        return fields
    for status_code, raw_response in responses.items():
        if not isinstance(status_code, str):
            continue
        response = resolver.deref(raw_response)
        if not isinstance(response, Mapping):
            continue
        body_path = f"{status_code}.body"

        if version < 3:
            schema = response.get("schema")
            _add_field(fields, body_path, False, schema, resolver, base=True)
            fields = _merge_fields(
                fields,
                _collect_fields(schema, body_path, False, resolver),
                "allOf",
            )
            headers = response.get("headers")
            if isinstance(headers, Mapping):
                for name, header in headers.items():
                    if isinstance(name, str):
                        path = f"{status_code}.header.{name}"
                        _add_field(fields, path, False, header, resolver)
        else:
            content = response.get("content")
            if isinstance(content, Mapping):
                for media_object in content.values():
                    if not isinstance(media_object, Mapping):
                        continue
                    schema = media_object.get("schema")
                    _add_field(fields, body_path, False, schema, resolver, base=True)
                    fields = _merge_fields(
                        fields,
                        _collect_fields(schema, body_path, False, resolver),
                        "allOf",
                    )
            headers = response.get("headers")
            if isinstance(headers, Mapping):
                for name, raw_header in headers.items():
                    if not isinstance(name, str):
                        continue
                    header = resolver.deref(raw_header)
                    if not isinstance(header, Mapping):
                        continue
                    path = f"{status_code}.header.{name}"
                    header_schemas = _schema_for_parameter(header, version)
                    for schema, _media_type in header_schemas:
                        _add_field(fields, path, False, schema, resolver)
    return fields


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    if ancestor == descendant:
        return False
    return (
        descendant.startswith(f"{ancestor}.")
        or descendant.startswith(f"{ancestor}[]")
        or descendant == f"{ancestor}[]"
    )


def _has_new_ancestor(
    path: str,
    new_fields: Mapping[str, _Field],
    old_fields: Mapping[str, _Field],
) -> bool:
    return any(
        candidate not in old_fields
        for candidate in new_fields
        if _is_ancestor(candidate, path)
    )


def _enum_change(
    old_field: _Field,
    new_field: _Field,
) -> tuple[str, list[typing.Any], list[typing.Any]] | None:
    if new_field.enum is None:
        return None
    if old_field.enum is None:
        return "added", [], list(new_field.enum)
    old_values = _enum_map(old_field.enum)
    new_values = _enum_map(new_field.enum)
    removed = [value for key, value in old_values.items() if key not in new_values]
    added = [value for key, value in new_values.items() if key not in old_values]
    if not removed:
        return None
    return "narrowed", removed, added


def _make_issue(
    *,
    method: str,
    path: str,
    location: str,
    property_path: str,
    kind: str,
    breaking: bool,
    message: str,
    old: typing.Any = None,
    new: typing.Any = None,
    status_code: str | None = None,
) -> CompatibilityIssue:
    return CompatibilityIssue(
        operation=f"{method} {path}",
        method=method,
        path=path,
        location=location,
        property=property_path,
        kind=kind,
        breaking=breaking,
        message=message,
        old=old,
        new=new,
        status_code=status_code,
    )


def _compare_operation(
    old_version: int,
    new_version: int,
    method: str,
    path: str,
    old_operation: Mapping[str, typing.Any],
    new_operation: Mapping[str, typing.Any],
    old_common_parameters: list[typing.Any],
    new_common_parameters: list[typing.Any],
    old_resolver: _Resolver,
    new_resolver: _Resolver,
) -> list[CompatibilityIssue]:
    issues: list[CompatibilityIssue] = []
    old_request = _request_fields(
        old_operation, old_common_parameters, old_resolver, old_version
    )
    new_request = _request_fields(
        new_operation, new_common_parameters, new_resolver, new_version
    )

    for property_path, new_field in new_request.items():
        old_field = old_request.get(property_path)
        if new_field.required and (old_field is None or not old_field.required):
            if not _has_new_ancestor(property_path, new_request, old_request):
                if old_field is None:
                    message = (
                        f"Required request property '{property_path}' was added "
                        f"to {method} {path}."
                    )
                    kind = "request.required.added"
                else:
                    message = (
                        f"Request property '{property_path}' became required "
                        f"in {method} {path}."
                    )
                    kind = "request.required.changed"
                issues.append(
                    _make_issue(
                        method=method,
                        path=path,
                        location="request",
                        property_path=property_path,
                        kind=kind,
                        breaking=True,
                        message=message,
                    )
                )

        if old_field is not None:
            enum_change = _enum_change(old_field, new_field)
            if enum_change is not None:
                change_kind, removed, added = enum_change
                issues.append(
                    _make_issue(
                        method=method,
                        path=path,
                        location="request",
                        property_path=property_path,
                        kind="request.enum.narrowed",
                        breaking=True,
                        message=(
                            f"Request enum for '{property_path}' was narrowed in "
                            f"{method} {path}"
                            + (
                                f"; removed value(s): {removed!r}."
                                if change_kind == "narrowed"
                                else f"; enum constraint {list(new_field.enum or ())!r} "
                                "was added."
                            )
                        ),
                        old=list(old_field.enum)
                        if old_field.enum is not None
                        else None,
                        new=list(new_field.enum)
                        if new_field.enum is not None
                        else None,
                    )
                )

    old_response = _response_fields(old_operation, old_resolver, old_version)
    new_response = _response_fields(new_operation, new_resolver, new_version)

    for property_path, new_field in new_response.items():
        old_field = old_response.get(property_path)
        if (
            old_field is None
            and not new_field.required
            and not new_field.base
            and not _has_new_ancestor(property_path, new_response, old_response)
        ):
            issues.append(
                _make_issue(
                    method=method,
                    path=path,
                    location="response",
                    property_path=property_path,
                    kind="response.optional.added",
                    breaking=False,
                    message=(
                        f"Optional response property '{property_path}' was added "
                        f"to {method} {path}."
                    ),
                    status_code=property_path.split(".", 1)[0],
                )
            )
        if old_field is not None:
            enum_change = _enum_change(old_field, new_field)
            if enum_change is not None:
                change_kind, removed, added = enum_change
                issues.append(
                    _make_issue(
                        method=method,
                        path=path,
                        location="response",
                        property_path=property_path,
                        kind="response.enum.narrowed",
                        breaking=False,
                        message=(
                            f"Response enum for '{property_path}' was narrowed in "
                            f"{method} {path}"
                            + (
                                f"; removed value(s): {removed!r}."
                                if change_kind == "narrowed"
                                else f"; enum constraint {list(new_field.enum or ())!r} "
                                "was added."
                            )
                        ),
                        old=list(old_field.enum)
                        if old_field.enum is not None
                        else None,
                        new=list(new_field.enum)
                        if new_field.enum is not None
                        else None,
                        status_code=property_path.split(".", 1)[0],
                    )
                )
    return issues


def _load_document(source: typing.Any) -> Mapping[str, typing.Any]:
    if callable(getattr(source, "to_dict", None)):
        source = source.to_dict()
    if isinstance(source, Mapping):
        return source
    if hasattr(source, "read"):
        text = source.read()
        filename = getattr(source, "name", None)
        return _parse_document(text, filename=filename)
    if isinstance(source, os.PathLike):
        path = Path(source)
        return _parse_document(path.read_text(encoding="utf-8"), filename=str(path))
    if isinstance(source, bytes):
        return _parse_document(source.decode("utf-8"))
    if isinstance(source, str):
        try:
            is_file = (
                "\n" not in source
                and "\r" not in source
                and len(source) < 4096
                and Path(source).is_file()
            )
        except OSError:
            is_file = False
        if is_file:
            path = Path(source)
            return _parse_document(path.read_text(encoding="utf-8"), filename=str(path))
        return _parse_document(source)
    raise TypeError(
        "Expected an OpenAPI document as a mapping, file path, file object, "
        f"YAML/JSON string, or APISpec instance; got {type(source).__name__}."
    )


def _parse_document(
    text: typing.Any, *, filename: str | None = None
) -> Mapping[str, typing.Any]:
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    if not isinstance(text, str):
        raise TypeError("Document content must be a string.")
    stripped = text.lstrip()
    if stripped.startswith("{") or (filename and filename.lower().endswith(".json")):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            if not isinstance(parsed, Mapping):
                raise ValueError("The OpenAPI document must be a mapping object.")
            return parsed
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError(
            "PyYAML is required to read YAML documents. Install apispec[yaml]."
        ) from error
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, Mapping):
        raise ValueError("The OpenAPI document must be a mapping object.")
    return parsed


def compare_specs(
    old_spec: typing.Any,
    new_spec: typing.Any,
    *,
    include_non_breaking: bool = True,
) -> CompatibilityReport:
    """Compare two OpenAPI documents for old-client compatibility.

    The comparison currently reports:

    * required request properties that were added or became required;
    * optional response properties that were added;
    * request and response enums that were narrowed.

    :param old_spec: The previously released document.
    :param new_spec: The document for the new release.
    :param include_non_breaking: Include non-breaking changes in the report.
    :return: A :class:`CompatibilityReport`.
    """
    old_document = _load_document(old_spec)
    new_document = _load_document(new_spec)
    old_resolver = _Resolver(old_document)
    new_resolver = _Resolver(new_document)

    old_operations = {
        _operation_key(operation): operation
        for operation in _iter_operations(old_document, old_resolver)
    }
    new_operations = {
        _operation_key(operation): operation
        for operation in _iter_operations(new_document, new_resolver)
    }

    old_version = _major_version(old_document)
    new_version = _major_version(new_document)

    changes: list[CompatibilityIssue] = []
    for key in sorted(
        set(old_operations) & set(new_operations),
        key=lambda item: (item[1], _METHOD_ORDER.get(item[0], 99)),
    ):
        old_method, old_path, old_operation, old_common_parameters = old_operations[key]
        _, _, new_operation, new_common_parameters = new_operations[key]
        changes.extend(
            _compare_operation(
                old_version,
                new_version,
                old_method,
                old_path,
                old_operation,
                new_operation,
                old_common_parameters,
                new_common_parameters,
                old_resolver,
                new_resolver,
            )
        )

    if not include_non_breaking:
        changes = [change for change in changes if change.breaking]
    return CompatibilityReport(changes=changes)


def find_breaking_changes(
    old_spec: typing.Any, new_spec: typing.Any
) -> list[CompatibilityIssue]:
    """Return only the breaking changes between two documents."""
    return compare_specs(
        old_spec, new_spec, include_non_breaking=False
    ).breaking_changes


def is_compatible(old_spec: typing.Any, new_spec: typing.Any) -> bool:
    """Return whether the new document is compatible with old clients."""
    return compare_specs(old_spec, new_spec).compatible


diff_specs = compare_specs
check_compatibility = compare_specs
compare_apispecs = compare_specs
check_breaking_changes = find_breaking_changes
get_breaking_changes = find_breaking_changes


def main(argv: Sequence[str] | None = None) -> int:
    """Run the compatibility checker from the command line."""
    parser = argparse.ArgumentParser(
        prog="apispec-compat",
        description="Check whether a new OpenAPI document breaks old clients.",
    )
    parser.add_argument("old", help="Previously released OpenAPI document")
    parser.add_argument("new", help="New OpenAPI document")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    parser.add_argument(
        "--breaking-only",
        action="store_true",
        help="Only report breaking changes",
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Exit with status 0 even when breaking changes are found",
    )
    args = parser.parse_args(argv)

    report = compare_specs(
        args.old, args.new, include_non_breaking=not args.breaking_only
    )
    if args.json:
        print(report.to_json(indent=2))
    else:
        print(report.summary())
        for change in report:
            label = "BREAKING" if change.breaking else "OK"
            print(f"[{label}] {change.operation}: {change.message}")
    if report.breaking and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

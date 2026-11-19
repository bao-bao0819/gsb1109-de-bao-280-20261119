"""Validation of example values found in an OpenAPI document.

The helpers in this module are used by :class:`apispec.APISpec` when example
validation is enabled while generating a specification.  Validation is
deliberately implemented without a runtime dependency on ``jsonschema``: it
walks the generated document so that ``$ref`` values are resolved against the
document that is actually being emitted.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, field

from .exceptions import ExampleValidationError

_MISSING = object()

READ = "read"
WRITE = "write"
NEUTRAL = "neutral"

_VALID_METHODS = {
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
}


class ExamplePath(list):
    """A list of property names and array indexes.

    The list representation is convenient for programmatic inspection.  It
    also compares equal to the common string representations of the same path
    (``$.pets[0].name`` and ``pets[0].name``) to make error handling pleasant.
    """

    def __str__(self) -> str:
        result = "$"
        for part in self:
            if isinstance(part, int):
                result += f"[{part}]"
            elif isinstance(part, str) and part.isidentifier():
                result += f".{part}"
            else:
                result += f"[{part!r}]"
        return result

    @property
    def property_path(self) -> str:
        """Return the path without the leading JSONPath ``$`` marker."""
        result = ""
        for part in self:
            if isinstance(part, int):
                result += f"[{part}]"
            else:
                result += f".{part}" if result else str(part)
        return result or "$"

    @property
    def path_string(self) -> str:
        return self.property_path

    @property
    def json_path(self) -> str:
        return str(self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            dot_index = self._dot_index_form()
            return other in {
                str(self),
                self.property_path,
                self.property_path.removeprefix("$."),
                dot_index,
                dot_index.removeprefix("$."),
            }
        if isinstance(other, (list, tuple)):
            return list(self) == list(other)
        return super().__eq__(other)

    def _dot_index_form(self) -> str:
        result = "$"
        for part in self:
            if isinstance(part, int):
                result += f".{part}"
            elif isinstance(part, str) and part.isidentifier():
                result += f".{part}"
            else:
                result += f"[{part!r}]"
        return result

    __hash__ = None

    def to_tuple(self) -> tuple:
        """Return the path as a tuple of property names and indexes."""
        return tuple(self)

    def to_list(self) -> list:
        return list(self)

    def to_json_pointer(self) -> str:
        """Return an RFC 6901 JSON Pointer for the path."""
        if not self:
            return ""
        tokens = []
        for part in self:
            token = str(part).replace("~", "~0").replace("/", "~1")
            tokens.append(token)
        return "/" + "/".join(tokens)


@dataclass
class ExampleValidationIssue:
    """A single invalid example value."""

    path: ExamplePath
    message: str
    example: str | None = None
    location: str = ""
    schema_path: ExamplePath = field(default_factory=ExamplePath)
    value: typing.Any = None

    @property
    def example_name(self) -> str | None:
        return self.example

    @property
    def attribute_path(self) -> str:
        return self.path.property_path

    @property
    def property_path(self) -> str:
        return self.path.property_path

    @property
    def json_path(self) -> str:
        return self.path.json_path

    @property
    def json_pointer(self) -> str:
        return self.path.to_json_pointer()

    @property
    def location_path(self) -> str:
        return self.location

    @property
    def message_for_user(self) -> str:
        return str(self)

    @property
    def path_string(self) -> str:
        return self.path.property_path

    def __str__(self) -> str:
        location = f"{self.location}: " if self.location else ""
        example = f" ({self.example})" if self.example else ""
        return f"{location}{self.path.json_path}{example}: {self.message}"


class _ReferenceResolver:
    """Resolve local JSON references in a generated OpenAPI document."""

    def __init__(self, document: dict) -> None:
        self.document = document

    def resolve(self, ref: str) -> typing.Any:
        if not isinstance(ref, str) or not ref.startswith("#"):
            return _MISSING
        pointer = ref[1:]
        if pointer == "":
            return self.document
        if not pointer.startswith("/"):
            return _MISSING
        node: typing.Any = self.document
        for raw_token in pointer.split("/")[1:]:
            token = raw_token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict):
                if token not in node:
                    return _MISSING
                node = node[token]
            elif isinstance(node, list):
                try:
                    node = node[int(token)]
                except (TypeError, ValueError, IndexError):
                    return _MISSING
            else:
                return _MISSING
        return node

    def deref(self, node: typing.Any) -> typing.Any:
        """Follow a chain of reference objects, with a cycle guard."""
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or ref in seen:
                return _MISSING
            seen.add(ref)
            target = self.resolve(ref)
            if target is _MISSING:
                return _MISSING
            siblings = {key: value for key, value in node.items() if key != "$ref"}
            if siblings:
                if not isinstance(target, dict):
                    return _MISSING
                node = {**target, **siblings}
            else:
                node = target
        return node


def _json_type(value: typing.Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _schema_type_list(schema: typing.Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    expected = schema.get("type")
    if expected is None:
        return []
    if isinstance(expected, str):
        return [expected]
    return list(expected)


def _matches_type(value: typing.Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (
            isinstance(value, float) and value.is_integer()
        )
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    # Unknown type names are left to the OpenAPI validator.
    return True


class _Validator:
    def __init__(self, document: dict) -> None:
        self.resolver = _ReferenceResolver(document)
        self._active: set[tuple[int, tuple, str]] = set()

    def resolve_schema(self, schema: typing.Any) -> typing.Any:
        if isinstance(schema, str):
            for pointer in (
                f"#/components/schemas/{schema}",
                f"#/definitions/{schema}",
            ):
                target = self.resolver.resolve(pointer)
                if target is not _MISSING:
                    return target
            return _MISSING
        seen: set[str] = set()
        while isinstance(schema, dict) and "$ref" in schema:
            ref = schema["$ref"]
            if not isinstance(ref, str) or ref in seen:
                return _MISSING
            seen.add(ref)
            target = self.resolver.resolve(ref)
            if target is _MISSING:
                return _MISSING
            siblings = {key: value for key, value in schema.items() if key != "$ref"}
            if siblings:
                if not isinstance(target, dict):
                    return _MISSING
                schema = {**target, **siblings}
            else:
                schema = target
        return schema

    def _has_flag(self, schema: typing.Any, flag: str) -> bool:
        resolved = self.resolve_schema(schema)
        if not isinstance(resolved, dict):
            return False
        if resolved.get(flag) is True:
            return True
        for sub_schema in resolved.get("allOf", []):
            if self._has_flag(sub_schema, flag):
                return True
        return False

    def _lookup_property(self, schema: dict, name: str) -> typing.Any:
        properties = schema.get("properties")
        if isinstance(properties, dict) and name in properties:
            return properties[name]
        for sub_schema in schema.get("allOf", []):
            resolved = self.resolve_schema(sub_schema)
            if isinstance(resolved, dict):
                found = self._lookup_property(resolved, name)
                if found is not None:
                    return found
        return None

    def validate(
        self,
        value: typing.Any,
        schema: typing.Any,
        path: ExamplePath | None = None,
        *,
        mode: str = NEUTRAL,
        example: str | None = None,
        location: str = "",
    ) -> list[ExampleValidationIssue]:
        path = ExamplePath() if path is None else path
        return self._validate(value, schema, path, mode, example, location)

    def _issue(
        self,
        path: ExamplePath,
        message: str,
        example: str | None,
        location: str,
    ) -> ExampleValidationIssue:
        return ExampleValidationIssue(
            ExamplePath(path), message, example=example, location=location
        )

    def _validate(
        self,
        value: typing.Any,
        schema: typing.Any,
        path: ExamplePath,
        mode: str,
        example: str | None,
        location: str,
    ) -> list[ExampleValidationIssue]:
        if schema is True:
            return []
        if schema is False:
            return [
                self._issue(
                    path, "no value is allowed by this schema", example, location
                )
            ]

        resolved = self.resolve_schema(schema)
        if resolved is _MISSING or not isinstance(resolved, dict):
            return []

        guard = (id(resolved), id(value), mode)
        if isinstance(value, (dict, list)) and guard in self._active:
            return []
        if isinstance(value, (dict, list)):
            self._active.add(guard)
        try:
            return self._validate_schema(value, resolved, path, mode, example, location)
        finally:
            if isinstance(value, (dict, list)):
                self._active.discard(guard)

    def _validate_schema(
        self,
        value: typing.Any,
        schema: dict,
        path: ExamplePath,
        mode: str,
        example: str | None,
        location: str,
    ) -> list[ExampleValidationIssue]:
        issues: list[ExampleValidationIssue] = []

        if value is None:
            if (
                schema.get("nullable") is True
                or schema.get("x-nullable") is True
                or "null" in _schema_type_list(schema)
            ):
                return issues

        if "const" in schema and value != schema["const"]:
            issues.append(
                self._issue(
                    path,
                    f"value must be {schema['const']!r}",
                    example,
                    location,
                )
            )
        if "enum" in schema and value not in schema["enum"]:
            issues.append(
                self._issue(
                    path,
                    f"value is not one of {schema['enum']!r}",
                    example,
                    location,
                )
            )

        expected_types = schema.get("type")
        if expected_types is not None:
            if isinstance(expected_types, str):
                expected = [expected_types]
            else:
                expected = list(expected_types)
            nullable = value is None and (
                schema.get("nullable") is True
                or schema.get("x-nullable") is True
                or "null" in expected
            )
            if not nullable and not any(_matches_type(value, t) for t in expected):
                actual_type = _json_type(value)
                expected_text = " or ".join(repr(t) for t in expected)
                issues.append(
                    self._issue(
                        path,
                        f"expected type {expected_text}, got {actual_type!r}",
                        example,
                        location,
                    )
                )
                return issues

        if "allOf" in schema:
            for sub_schema in schema["allOf"]:
                issues.extend(
                    self._validate(value, sub_schema, path, mode, example, location)
                )

        if "anyOf" in schema:
            alternatives = schema["anyOf"]
            results = [
                self._validate(value, sub_schema, path, mode, example, location)
                for sub_schema in alternatives
            ]
            if all(result for result in results):
                if len(results) == 1:
                    issues.extend(results[0])
                else:
                    issues.append(
                        self._issue(
                            path,
                            "value does not match any of the allowed schemas",
                            example,
                            location,
                        )
                    )

        if "oneOf" in schema:
            alternatives = schema["oneOf"]
            results = [
                self._validate(value, sub_schema, path, mode, example, location)
                for sub_schema in alternatives
            ]
            matching = sum(1 for result in results if not result)
            if matching == 0:
                if len(results) == 1:
                    issues.extend(results[0])
                else:
                    issues.append(
                        self._issue(
                            path,
                            "value does not match any of the allowed schemas",
                            example,
                            location,
                        )
                    )
            elif matching > 1:
                issues.append(
                    self._issue(
                        path,
                        "value matches more than one allowed schema",
                        example,
                        location,
                    )
                )

        if "not" in schema:
            if not self._validate(value, schema["not"], path, mode, example, location):
                issues.append(
                    self._issue(
                        path,
                        "value must not match the disallowed schema",
                        example,
                        location,
                    )
                )

        if isinstance(value, dict):
            issues.extend(
                self._validate_object(value, schema, path, mode, example, location)
            )

        if isinstance(value, list):
            issues.extend(
                self._validate_array(value, schema, path, mode, example, location)
            )

        issues.extend(
            self._validate_constraints(value, schema, path, example, location)
        )

        return issues

    def _validate_object(
        self,
        value: dict,
        schema: dict,
        path: ExamplePath,
        mode: str,
        example: str | None,
        location: str,
    ) -> list[ExampleValidationIssue]:
        issues: list[ExampleValidationIssue] = []
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}

        for name in schema.get("required", []):
            if name in value:
                continue
            prop_schema = self._lookup_property(schema, name)
            if mode == WRITE and self._has_flag(prop_schema, "readOnly"):
                continue
            if mode == READ and self._has_flag(prop_schema, "writeOnly"):
                continue
            issues.append(
                self._issue(
                    ExamplePath([*path, name]),
                    f"required property {name!r} is missing",
                    example,
                    location,
                )
            )

        if properties:
            for name, prop_schema in properties.items():
                if name not in value:
                    continue
                if mode == READ and self._has_flag(prop_schema, "writeOnly"):
                    issues.append(
                        self._issue(
                            ExamplePath([*path, name]),
                            f"writeOnly property {name!r} is not allowed in a "
                            "response example",
                            example,
                            location,
                        )
                    )
                    continue
                if mode == WRITE and self._has_flag(prop_schema, "readOnly"):
                    issues.append(
                        self._issue(
                            ExamplePath([*path, name]),
                            f"readOnly property {name!r} is not allowed in a "
                            "request example",
                            example,
                            location,
                        )
                    )
                    continue
                issues.extend(
                    self._validate(
                        value[name],
                        prop_schema,
                        ExamplePath([*path, name]),
                        mode,
                        example,
                        location,
                    )
                )

        additional = schema.get("additionalProperties", _MISSING)
        for name, prop_value in value.items():
            if name in properties or name in schema.get("patternProperties", {}):
                continue
            if additional is False:
                issues.append(
                    self._issue(
                        ExamplePath([*path, name]),
                        f"additional property {name!r} is not allowed",
                        example,
                        location,
                    )
                )
            elif isinstance(additional, (dict, bool)) and additional is not True:
                issues.extend(
                    self._validate(
                        prop_value,
                        additional,
                        ExamplePath([*path, name]),
                        mode,
                        example,
                        location,
                    )
                )

        return issues

    def _validate_array(
        self,
        value: list,
        schema: dict,
        path: ExamplePath,
        mode: str,
        example: str | None,
        location: str,
    ) -> list[ExampleValidationIssue]:
        issues: list[ExampleValidationIssue] = []
        items = schema.get("items")
        if isinstance(items, list):
            for index, item in enumerate(value):
                if index < len(items):
                    issues.extend(
                        self._validate(
                            item,
                            items[index],
                            ExamplePath([*path, index]),
                            mode,
                            example,
                            location,
                        )
                    )
        elif isinstance(items, (dict, bool)):
            for index, item in enumerate(value):
                issues.extend(
                    self._validate(
                        item,
                        items,
                        ExamplePath([*path, index]),
                        mode,
                        example,
                        location,
                    )
                )
        return issues

    def _validate_constraints(
        self,
        value: typing.Any,
        schema: dict,
        path: ExamplePath,
        example: str | None,
        location: str,
    ) -> list[ExampleValidationIssue]:
        issues: list[ExampleValidationIssue] = []

        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                issues.append(
                    self._issue(
                        path,
                        f"string is shorter than {schema['minLength']} characters",
                        example,
                        location,
                    )
                )
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                issues.append(
                    self._issue(
                        path,
                        f"string is longer than {schema['maxLength']} characters",
                        example,
                        location,
                    )
                )

        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                issues.append(
                    self._issue(
                        path,
                        f"array has fewer than {schema['minItems']} items",
                        example,
                        location,
                    )
                )
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                issues.append(
                    self._issue(
                        path,
                        f"array has more than {schema['maxItems']} items",
                        example,
                        location,
                    )
                )

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                issues.append(
                    self._issue(
                        path,
                        f"value is less than the minimum of {schema['minimum']}",
                        example,
                        location,
                    )
                )
            if "maximum" in schema and value > schema["maximum"]:
                issues.append(
                    self._issue(
                        path,
                        f"value is greater than the maximum of {schema['maximum']}",
                        example,
                        location,
                    )
                )

        return issues


class ExampleValidator:
    """Collect invalid example values from a generated OpenAPI document."""

    def __init__(
        self,
        document: dict,
        example_schemas: dict[str, typing.Any] | None = None,
    ) -> None:
        self.document = document
        self.resolver = _ReferenceResolver(document)
        self.validator = _Validator(document)
        self.issues: list[ExampleValidationIssue] = []
        self.example_schemas = example_schemas or {}

    def _example_schema(self, example: typing.Any) -> typing.Any:
        if not isinstance(example, dict):
            return _MISSING
        ref = example.get("$ref")
        if not isinstance(ref, str):
            return _MISSING
        prefix = "#/components/examples/"
        if not ref.startswith(prefix):
            return _MISSING
        name = ref[len(prefix) :].replace("~1", "/").replace("~0", "~")
        return self.example_schemas.get(name, _MISSING)

    def collect(self) -> list[ExampleValidationIssue]:
        self.issues = []
        if not isinstance(self.document, dict):
            return self.issues

        components = self.document.get("components")
        if isinstance(components, dict):
            for name, schema in (components.get("schemas") or {}).items():
                self._schema_examples(schema, f"components.schemas.{name}")
            for name, parameter in (components.get("parameters") or {}).items():
                self._parameter(parameter, f"components.parameters.{name}", WRITE)
            for name, header in (components.get("headers") or {}).items():
                self._header(header, f"components.headers.{name}", NEUTRAL)
            for name, response in (components.get("responses") or {}).items():
                self._response(response, f"components.responses.{name}")
            for name, request_body in (components.get("requestBodies") or {}).items():
                self._request_body(request_body, f"components.requestBodies.{name}")
            for name, callback in (components.get("callbacks") or {}).items():
                self._callback(callback, f"components.callbacks.{name}")
            referenced = self._referenced_examples()
            for name, example in (components.get("examples") or {}).items():
                if name in referenced:
                    # The using site reports the offending example with its
                    # own location and direction (request vs response).
                    continue
                schema = self.example_schemas.get(name, _MISSING)
                if schema is not _MISSING:
                    self._validate_example(
                        example,
                        schema,
                        f"components.examples.{name}",
                        NEUTRAL,
                        name,
                    )

        for path, path_item in (self.document.get("paths") or {}).items():
            self._path_item(path_item, f"paths.{path}")

        for path, path_item in (self.document.get("webhooks") or {}).items():
            self._path_item(path_item, f"webhooks.{path}")

        # OpenAPI 2 documents keep components at the top level.
        for name, schema in (self.document.get("definitions") or {}).items():
            self._schema_examples(schema, f"definitions.{name}")
        for name, parameter in (self.document.get("parameters") or {}).items():
            self._parameter(parameter, f"parameters.{name}", WRITE)
        for name, response in (self.document.get("responses") or {}).items():
            self._response(response, f"responses.{name}")

        return self.issues

    def _deref(self, node: typing.Any) -> typing.Any:
        return self.resolver.deref(node)

    def _example_value(self, example: typing.Any) -> typing.Any:
        seen: set[str] = set()
        while isinstance(example, dict):
            if "$ref" in example:
                ref = example["$ref"]
                if not isinstance(ref, str) or ref in seen:
                    return _MISSING
                seen.add(ref)
                target = self.resolver.resolve(ref)
                if target is _MISSING:
                    return _MISSING
                example = target
                continue
            if "value" in example:
                return example["value"]
            if "externalValue" in example:
                return _MISSING
            if "summary" in example or "description" in example:
                return _MISSING
            # Tolerate a literal value placed directly in an examples map.
            return example
        return example

    def _validate_example(
        self,
        example: typing.Any,
        schema: typing.Any,
        location: str,
        mode: str,
        name: str | None = None,
        *,
        example_object: bool = True,
    ) -> None:
        if schema is _MISSING:
            return
        value = self._example_value(example) if example_object else example
        if value is _MISSING:
            return
        self.issues.extend(
            self.validator.validate(
                value,
                schema,
                mode=mode,
                example=name,
                location=location,
            )
        )

    def _referenced_examples(self) -> set[str]:
        referenced: set[str] = set()
        prefix = "#/components/examples/"
        stack = [self.document]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref.startswith(prefix):
                    referenced.add(
                        ref[len(prefix) :].replace("~1", "/").replace("~0", "~")
                    )
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return referenced

    def _examples(
        self,
        examples: typing.Any,
        schema: typing.Any,
        location: str,
        mode: str,
    ) -> None:
        if not isinstance(examples, dict):
            return
        for name, example in examples.items():
            example_schema = schema
            if example_schema is _MISSING:
                example_schema = self._example_schema(example)
            self._validate_example(
                example, example_schema, f"{location}.examples.{name}", mode, name
            )

    def _media_type(self, media_type: typing.Any, location: str, mode: str) -> None:
        resolved = self._deref(media_type)
        if not isinstance(resolved, dict):
            return
        schema = resolved.get("schema", _MISSING)
        if "example" in resolved:
            self._validate_example(
                resolved["example"],
                schema,
                f"{location}.example",
                mode,
                example_object=False,
            )
        self._examples(resolved.get("examples"), schema, location, mode)

    def _content(self, content: typing.Any, location: str, mode: str) -> None:
        if not isinstance(content, dict):
            return
        for media_name, media_type in content.items():
            self._media_type(media_type, f"{location}.content.{media_name}", mode)

    def _parameter(self, parameter: typing.Any, location: str, mode: str) -> None:
        resolved = self._deref(parameter)
        if not isinstance(resolved, dict):
            return
        schema = resolved.get("schema", _MISSING)
        if schema is _MISSING and "type" in resolved:
            # OpenAPI 2 non-body parameters describe their type inline.
            schema = resolved
        # OpenAPI 2 uses "x-example" on non-body parameters.
        for example_key in ("example", "x-example"):
            if example_key in resolved:
                self._validate_example(
                    resolved[example_key],
                    schema,
                    f"{location}.{example_key}",
                    mode,
                    example_object=False,
                )
        self._examples(resolved.get("examples"), schema, location, mode)
        self._content(resolved.get("content"), location, mode)

    def _header(self, header: typing.Any, location: str, mode: str) -> None:
        self._parameter(header, location, mode)

    def _request_body(self, request_body: typing.Any, location: str) -> None:
        resolved = self._deref(request_body)
        if not isinstance(resolved, dict):
            return
        self._content(resolved.get("content"), location, WRITE)

    def _response(self, response: typing.Any, location: str) -> None:
        resolved = self._deref(response)
        if not isinstance(resolved, dict):
            return

        self._content(resolved.get("content"), location, READ)

        headers = resolved.get("headers")
        if isinstance(headers, dict):
            for name, header in headers.items():
                self._header(header, f"{location}.headers.{name}", READ)

        # OpenAPI 2 response examples are a map of media type to literal value.
        schema = resolved.get("schema", _MISSING)
        examples = resolved.get("examples")
        if isinstance(examples, dict):
            for media_name, example in examples.items():
                self._validate_example(
                    example,
                    schema,
                    f"{location}.examples.{media_name}",
                    READ,
                    media_name,
                    example_object=False,
                )

    def _operation(self, operation: typing.Any, location: str) -> None:
        resolved = self._deref(operation)
        if not isinstance(resolved, dict):
            return

        for index, parameter in enumerate(resolved.get("parameters") or []):
            self._parameter(parameter, f"{location}.parameters[{index}]", WRITE)

        if "requestBody" in resolved:
            self._request_body(resolved["requestBody"], f"{location}.requestBody")

        responses = resolved.get("responses")
        if isinstance(responses, dict):
            for code, response in responses.items():
                self._response(response, f"{location}.responses.{code}")

        callbacks = resolved.get("callbacks")
        if isinstance(callbacks, dict):
            for name, callback in callbacks.items():
                self._callback(callback, f"{location}.callbacks.{name}")

    def _path_item(self, path_item: typing.Any, location: str) -> None:
        resolved = self._deref(path_item)
        if not isinstance(resolved, dict):
            return

        for index, parameter in enumerate(resolved.get("parameters") or []):
            self._parameter(parameter, f"{location}.parameters[{index}]", WRITE)

        for method, operation in resolved.items():
            if method in _VALID_METHODS:
                self._operation(operation, f"{location}.{method}")

    def _callback(self, callback: typing.Any, location: str) -> None:
        resolved = self._deref(callback)
        if not isinstance(resolved, dict):
            return
        for expression, path_item in resolved.items():
            self._path_item(path_item, f"{location}.{expression}")

    def _schema_examples(self, schema: typing.Any, location: str) -> None:
        seen: set[int] = set()

        def walk(node: typing.Any, node_location: str) -> None:
            if not isinstance(node, dict):
                return
            node_id = id(node)
            if node_id in seen:
                return
            seen.add(node_id)

            if "example" in node:
                self._validate_example(
                    node["example"],
                    node,
                    f"{node_location}.example",
                    NEUTRAL,
                    example_object=False,
                )
            examples = node.get("examples")
            if isinstance(examples, list):
                for index, example in enumerate(examples):
                    self._validate_example(
                        example,
                        node,
                        f"{node_location}.examples[{index}]",
                        NEUTRAL,
                        example_object=False,
                    )

            properties = node.get("properties")
            if isinstance(properties, dict):
                for name, prop_schema in properties.items():
                    walk(prop_schema, f"{node_location}.properties.{name}")
            items = node.get("items")
            if isinstance(items, dict):
                walk(items, f"{node_location}.items")
            elif isinstance(items, list):
                for index, item in enumerate(items):
                    walk(item, f"{node_location}.items[{index}]")
            for keyword in ("allOf", "anyOf", "oneOf"):
                for index, sub_schema in enumerate(node.get(keyword) or []):
                    walk(sub_schema, f"{node_location}.{keyword}[{index}]")
            if isinstance(node.get("not"), dict):
                walk(node["not"], f"{node_location}.not")
            additional = node.get("additionalProperties")
            if isinstance(additional, dict):
                walk(additional, f"{node_location}.additionalProperties")

        walk(schema, location)


def validate_examples(
    document: dict,
    *,
    raise_errors: bool = False,
    example_schemas: dict[str, typing.Any] | None = None,
) -> list[ExampleValidationIssue]:
    """Validate all example values in a generated OpenAPI document.

    ``readOnly`` properties are optional in request examples, while
    ``writeOnly`` properties are rejected in response examples.  Local
    ``$ref`` values are followed against ``document``, including recursive
    schemas and array item schemas.

    :param dict document: A generated OpenAPI document.
    :param bool raise_errors: Raise :class:`ExampleValidationError` if any
        issue is found instead of returning the issues.
    :param dict example_schemas: Optional mapping of Example component names
        to schemas. Used for examples registered with
        ``Components.example(..., schema=...)``.
    """
    issues = ExampleValidator(document, example_schemas).collect()
    if issues and raise_errors:
        raise ExampleValidationError(issues)
    return issues


def check_examples(document: dict) -> list[ExampleValidationIssue]:
    """Validate examples and raise on the first batch of problems."""
    return validate_examples(document, raise_errors=True)

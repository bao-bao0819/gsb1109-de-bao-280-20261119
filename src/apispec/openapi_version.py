"""Helpers to render an OpenAPI document for a different OpenAPI 3 version.

OpenAPI 3.0 and 3.1 represent nullable values differently. 3.0 uses a
``nullable`` keyword, while 3.1 uses ``"null"`` in a type list or a
``{"type": "null"}`` schema. Converting a document must walk all schemas,
including schemas nested in properties, arrays, dictionaries and combinators.
"""

from __future__ import annotations

import typing

from .exceptions import APISpecError

NULL_SCHEMA = {"type": "null"}
NULLABLE_OBJECT_SCHEMA = {"type": "object", "nullable": True}

_SCHEMA_MAP_KEYS = (
    "properties",
    "patternProperties",
    "dependentSchemas",
    "definitions",
    "$defs",
)
_SCHEMA_LIST_KEYS = (
    "allOf",
    "oneOf",
    "anyOf",
    "prefixItems",
)
_SCHEMA_KEYS = (
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "propertyNames",
    "contains",
    "contentSchema",
    "not",
    "if",
    "then",
    "else",
)

_HTTP_METHODS = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)


def convert_document(
    document: dict[str, typing.Any], source_minor: int, target_minor: int
) -> None:
    """Convert all schemas in an OpenAPI 3 document in place.

    :param dict document: OpenAPI document
    :param int source_minor: minor version of the document's OpenAPI version
    :param int target_minor: minor version of the target OpenAPI version
    """
    if source_minor == target_minor:
        return
    components = document.get("components")
    if isinstance(components, dict):
        _convert_components(components, source_minor, target_minor)
    for key in ("paths", "webhooks"):
        paths = document.get(key)
        if isinstance(paths, dict):
            for path_item in paths.values():
                _convert_path_item(path_item, source_minor, target_minor)


def convert_components(
    components: dict[str, typing.Any], source_minor: int, target_minor: int
) -> None:
    """Convert all schemas in an OpenAPI 3 components object in place."""
    if source_minor != target_minor:
        _convert_components(components, source_minor, target_minor)


def _convert_components(components, source_minor, target_minor) -> None:
    schemas = components.get("schemas")
    if isinstance(schemas, dict):
        for schema in schemas.values():
            convert_schema(schema, source_minor, target_minor)

    parameters = components.get("parameters")
    if isinstance(parameters, dict):
        for parameter in parameters.values():
            _convert_parameter(parameter, source_minor, target_minor)

    headers = components.get("headers")
    if isinstance(headers, dict):
        for header in headers.values():
            _convert_parameter(header, source_minor, target_minor)

    responses = components.get("responses")
    if isinstance(responses, dict):
        for response in responses.values():
            _convert_response(response, source_minor, target_minor)

    request_bodies = components.get("requestBodies")
    if isinstance(request_bodies, dict):
        for request_body in request_bodies.values():
            _convert_request_body(request_body, source_minor, target_minor)

    callbacks = components.get("callbacks")
    if isinstance(callbacks, dict):
        for callback in callbacks.values():
            _convert_callback(callback, source_minor, target_minor)

    path_items = components.get("pathItems")
    if isinstance(path_items, dict):
        for path_item in path_items.values():
            _convert_path_item(path_item, source_minor, target_minor)


def _convert_path_item(path_item, source_minor, target_minor) -> None:
    if not isinstance(path_item, dict):
        return
    parameters = path_item.get("parameters")
    if isinstance(parameters, list):
        for parameter in parameters:
            _convert_parameter(parameter, source_minor, target_minor)
    for method in _HTTP_METHODS:
        operation = path_item.get(method)
        if isinstance(operation, dict):
            _convert_operation(operation, source_minor, target_minor)


def _convert_operation(operation, source_minor, target_minor) -> None:
    parameters = operation.get("parameters")
    if isinstance(parameters, list):
        for parameter in parameters:
            _convert_parameter(parameter, source_minor, target_minor)
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        _convert_request_body(request_body, source_minor, target_minor)
    responses = operation.get("responses")
    if isinstance(responses, dict):
        for response in responses.values():
            _convert_response(response, source_minor, target_minor)
    callbacks = operation.get("callbacks")
    if isinstance(callbacks, dict):
        for callback in callbacks.values():
            _convert_callback(callback, source_minor, target_minor)


def _convert_parameter(parameter, source_minor, target_minor) -> None:
    if not isinstance(parameter, dict):
        return
    schema = parameter.get("schema")
    if isinstance(schema, dict):
        convert_schema(schema, source_minor, target_minor)
    content = parameter.get("content")
    if isinstance(content, dict):
        for media_type in content.values():
            _convert_media_type(media_type, source_minor, target_minor)


def _convert_request_body(request_body, source_minor, target_minor) -> None:
    if not isinstance(request_body, dict):
        return
    content = request_body.get("content")
    if isinstance(content, dict):
        for media_type in content.values():
            _convert_media_type(media_type, source_minor, target_minor)


def _convert_response(response, source_minor, target_minor) -> None:
    if not isinstance(response, dict):
        return
    content = response.get("content")
    if isinstance(content, dict):
        for media_type in content.values():
            _convert_media_type(media_type, source_minor, target_minor)
    headers = response.get("headers")
    if isinstance(headers, dict):
        for header in headers.values():
            _convert_parameter(header, source_minor, target_minor)


def _convert_media_type(media_type, source_minor, target_minor) -> None:
    if not isinstance(media_type, dict):
        return
    schema = media_type.get("schema")
    if isinstance(schema, dict):
        convert_schema(schema, source_minor, target_minor)
    encoding = media_type.get("encoding")
    if isinstance(encoding, dict):
        for encoding_object in encoding.values():
            if not isinstance(encoding_object, dict):
                continue
            headers = encoding_object.get("headers")
            if isinstance(headers, dict):
                for header in headers.values():
                    _convert_parameter(header, source_minor, target_minor)


def _convert_callback(callback, source_minor, target_minor) -> None:
    if not isinstance(callback, dict):
        return
    for path_item in callback.values():
        _convert_path_item(path_item, source_minor, target_minor)


def convert_schema(schema, source_minor: int, target_minor: int) -> None:
    """Convert a schema between OpenAPI 3.0 and 3.1 in place."""
    if not isinstance(schema, dict) or source_minor == target_minor:
        return
    if target_minor < 1:
        _downgrade_schema_to_30(schema)
    else:
        _upgrade_schema_to_31(schema)


def _downgrade_schema_to_30(schema: dict) -> None:
    _downgrade_nullability_at_node(schema)
    _downgrade_ref_with_siblings(schema)

    for key in _SCHEMA_MAP_KEYS:
        mapping = schema.get(key)
        if isinstance(mapping, dict):
            for child in mapping.values():
                _downgrade_schema_to_30(child)
    for key in _SCHEMA_LIST_KEYS:
        schemas = schema.get(key)
        if isinstance(schemas, list):
            for child in schemas:
                _downgrade_schema_to_30(child)
    for key in _SCHEMA_KEYS:
        child = schema.get(key)
        if isinstance(child, dict):
            _downgrade_schema_to_30(child)
        elif isinstance(child, list):
            for item in child:
                _downgrade_schema_to_30(item)


def _downgrade_nullability_at_node(schema: dict) -> None:
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        null_positions = [
            position for position, item in enumerate(any_of) if item == NULL_SCHEMA
        ]
        if null_positions:
            non_null = [
                item
                for position, item in enumerate(any_of)
                if position not in null_positions
            ]
            if len(non_null) == 1 and set(non_null[0]) == {"$ref"}:
                if len(schema) == 1:
                    # Same shape as the 3.0 marshmallow converter for a
                    # nullable nested field without sibling keywords.
                    schema["anyOf"] = [NULLABLE_OBJECT_SCHEMA, non_null[0]]
                else:
                    schema["anyOf"] = [non_null[0], NULLABLE_OBJECT_SCHEMA]
            else:
                if non_null:
                    schema["anyOf"] = non_null
                else:
                    schema.pop("anyOf")
                schema["nullable"] = True

    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and NULL_SCHEMA in one_of:
        non_null = [item for item in one_of if item != NULL_SCHEMA]
        if non_null:
            schema["oneOf"] = non_null
        else:
            schema.pop("oneOf")
        schema["nullable"] = True

    schema_type = schema.get("type")
    if schema_type == "null":
        schema.pop("type")
        schema["nullable"] = True
    elif isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        schema.pop("type")
        if len(non_null_types) == 1:
            schema["type"] = non_null_types[0]
        elif len(non_null_types) > 1:
            type_schemas = [{"type": item} for item in non_null_types]
            existing_any_of = schema.get("anyOf")
            if isinstance(existing_any_of, list):
                schema["anyOf"] = [*type_schemas, *existing_any_of]
            else:
                schema["anyOf"] = type_schemas
        if len(non_null_types) != len(schema_type):
            schema["nullable"] = True
    elif "type" not in schema and "$ref" not in schema:
        enum = schema.get("enum")
        if isinstance(enum, list) and None in enum:
            # OpenAPI 3.0 only allows a null enum member through ``nullable``.
            schema["nullable"] = True


def _downgrade_ref_with_siblings(schema: dict) -> None:
    if "$ref" not in schema or len(schema) == 1:
        return
    ref = schema.pop("$ref")
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        all_of.insert(0, {"$ref": ref})
    else:
        schema["allOf"] = [{"$ref": ref}]


def _upgrade_schema_to_31(schema: dict) -> None:
    _upgrade_nullability_at_node(schema)
    _upgrade_single_ref_all_of(schema)

    for key in _SCHEMA_MAP_KEYS:
        mapping = schema.get(key)
        if isinstance(mapping, dict):
            for child in mapping.values():
                _upgrade_schema_to_31(child)
    for key in _SCHEMA_LIST_KEYS:
        schemas = schema.get(key)
        if isinstance(schemas, list):
            for child in schemas:
                _upgrade_schema_to_31(child)
    for key in _SCHEMA_KEYS:
        child = schema.get(key)
        if isinstance(child, dict):
            _upgrade_schema_to_31(child)
        elif isinstance(child, list):
            for item in child:
                _upgrade_schema_to_31(item)


def _upgrade_nullability_at_node(schema: dict) -> None:
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        nullable_object_positions = [
            position
            for position, item in enumerate(any_of)
            if item == NULLABLE_OBJECT_SCHEMA
        ]
        if nullable_object_positions:
            if len(any_of) == 2:
                other = next(
                    item
                    for position, item in enumerate(any_of)
                    if position != nullable_object_positions[0]
                )
                if "$ref" in other:
                    schema["anyOf"] = [other, NULL_SCHEMA]
                    any_of = schema["anyOf"]
                else:
                    any_of[nullable_object_positions[0]] = {"type": ["object", "null"]}
            else:
                for position in nullable_object_positions:
                    any_of[position] = {"type": ["object", "null"]}

    if schema.pop("nullable", False) is not True:
        return
    if any(item == NULLABLE_OBJECT_SCHEMA for item in schema.get("anyOf", [])):
        # Nullability is already carried by the branch list.
        return

    schema_type = schema.get("type")
    if schema_type is not None:
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        if "null" not in types:
            schema["type"] = [*types, "null"]
    elif "$ref" in schema or "allOf" in schema:
        # A reference or an ``allOf`` composition may carry sibling keywords,
        # so keep it intact as one branch of the union.
        existing_any_of = schema.pop("anyOf", [])
        base = {key: value for key, value in schema.items() if key != "nullable"}
        for key in base:
            schema.pop(key)
        schema["anyOf"] = [base, *existing_any_of, NULL_SCHEMA]
    elif isinstance(schema.get("anyOf"), list):
        if NULL_SCHEMA not in schema["anyOf"]:
            schema["anyOf"].append(NULL_SCHEMA)
    elif isinstance(schema.get("oneOf"), list):
        if NULL_SCHEMA not in schema["oneOf"]:
            schema["oneOf"].append(NULL_SCHEMA)
    else:
        # Without a type, a 3.0 ``nullable`` field accepts any value. 3.1 uses
        # an empty schema for that, so ``nullable`` simply disappears.
        enum = schema.get("enum")
        if isinstance(enum, list) and None not in enum:
            enum.append(None)


def _upgrade_single_ref_all_of(schema: dict) -> None:
    all_of = schema.get("allOf")
    if (
        isinstance(all_of, list)
        and len(all_of) == 1
        and isinstance(all_of[0], dict)
        and set(all_of[0]) == {"$ref"}
        and "anyOf" not in schema
        and "oneOf" not in schema
    ):
        schema.pop("allOf")
        schema["$ref"] = all_of[0]["$ref"]


def get_target_minor(openapi_version) -> int:
    """Return the minor version of a supported OpenAPI 3 version."""
    major = openapi_version.major
    if major != 3:
        raise APISpecError(
            "Exporting to a different OpenAPI major version is not supported: "
            f"{openapi_version}"
        )
    return openapi_version.minor

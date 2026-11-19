import pytest
from marshmallow import Schema, fields

from apispec import APISpec
from apispec.exceptions import APISpecError
from apispec.ext.marshmallow import MarshmallowPlugin

from .utils import get_schemas


def make_spec(openapi_version="3.1.0", schema=None):
    spec = APISpec(
        title="Versioned",
        version="1.0.0",
        openapi_version=openapi_version,
        plugins=(MarshmallowPlugin(),),
    )
    if schema is not None:
        spec.components.schema(schema.__name__, schema=schema)
    return spec


class ChildSchema(Schema):
    name = fields.Str()
    nickname = fields.Str(allow_none=True)


class NestedSchema(Schema):
    child = fields.Nested(ChildSchema, allow_none=True)
    described = fields.Nested(
        ChildSchema, allow_none=True, metadata={"description": "described"}
    )
    plain = fields.Nested(ChildSchema)
    children = fields.List(fields.Nested(ChildSchema, allow_none=True))
    matrix = fields.List(
        fields.List(fields.Nested(ChildSchema, allow_none=True), allow_none=True),
        allow_none=True,
    )
    strings = fields.List(
        fields.List(fields.Str(allow_none=True), allow_none=True), allow_none=True
    )
    mapping = fields.Dict(
        keys=fields.Str(),
        values=fields.List(fields.Str(allow_none=True), allow_none=True),
        allow_none=True,
    )


def test_same_components_render_31_30_31():
    spec = make_spec("3.1.0", NestedSchema)

    first_31 = spec.to_dict("3.1.0")
    document_30 = spec.to_dict("3.0.0")
    second_31 = spec.to_dict("3.1.0")

    assert first_31 == second_31
    assert first_31["openapi"] == "3.1.0"
    assert document_30["openapi"] == "3.0.0"
    assert "openapi_version" not in document_30

    document_31 = make_spec("3.1.0", NestedSchema).to_dict()
    assert first_31 == document_31


def test_export_does_not_mutate_registered_components():
    spec = make_spec("3.1.0", NestedSchema)
    registered = get_schemas(spec)

    spec.to_dict("3.0.0")

    assert get_schemas(spec) == registered
    # The registered components still use the 3.1 representation.
    child = registered["NestedSchema"]["properties"]["child"]
    assert child == {
        "anyOf": [{"$ref": "#/components/schemas/Child"}, {"type": "null"}]
    }


def test_30_export_keeps_nullability_of_every_nested_field():
    document = make_spec("3.1.0", NestedSchema).to_dict("3.0.0")
    nested = document["components"]["schemas"]["NestedSchema"]["properties"]

    def assert_nullable_30(schema):
        assert schema.get("nullable") is True or {
            "type": "object",
            "nullable": True,
        } in schema.get("anyOf", [])

    assert_nullable_30(nested["child"])
    assert_nullable_30(nested["described"])
    assert "anyOf" not in nested["plain"]
    assert "nullable" not in nested["plain"]

    assert_nullable_30(nested["children"]["items"])
    assert_nullable_30(nested["matrix"])
    assert_nullable_30(nested["matrix"]["items"])
    assert_nullable_30(nested["matrix"]["items"]["items"])
    assert_nullable_30(nested["strings"])
    assert_nullable_30(nested["strings"]["items"])
    assert_nullable_30(nested["strings"]["items"]["items"])
    assert_nullable_30(nested["mapping"])
    assert_nullable_30(nested["mapping"]["additionalProperties"])
    assert_nullable_30(nested["mapping"]["additionalProperties"]["items"])


def test_nullable_object_shape_matches_native_30():
    converted = make_spec("3.1.0", NestedSchema).to_dict("3.0.0")
    native = make_spec("3.0.0", NestedSchema).to_dict()

    assert (
        converted["components"]["schemas"]["Child"]
        == native["components"]["schemas"]["Child"]
    )
    # ``fields.Raw`` nullability is not representable in 3.1, so compare the
    # fields where nullability is encoded.
    converted_props = converted["components"]["schemas"]["NestedSchema"]["properties"]
    native_props = native["components"]["schemas"]["NestedSchema"]["properties"]
    for name in (
        "child",
        "described",
        "plain",
        "children",
        "matrix",
        "strings",
        "mapping",
    ):
        assert converted_props[name] == native_props[name]


def test_upgrade_from_30_matches_native_31():
    converted = make_spec("3.0.0", NestedSchema).to_dict("3.1.0")
    native = make_spec("3.1.0", NestedSchema).to_dict()

    converted_props = converted["components"]["schemas"]["NestedSchema"]["properties"]
    native_props = native["components"]["schemas"]["NestedSchema"]["properties"]
    assert converted_props == native_props


def test_nullable_type_list_is_downgraded():
    spec = APISpec(title="T", version="1", openapi_version="3.1.0")
    spec.components.schema(
        "Root",
        {
            "type": "object",
            "properties": {
                "text": {"type": ["string", "null"]},
                "numbers": {
                    "type": ["array", "null"],
                    "items": {"type": ["integer", "null"]},
                },
            },
        },
    )

    document = spec.to_dict("3.0.0")

    assert document["components"]["schemas"]["Root"]["properties"] == {
        "text": {"type": "string", "nullable": True},
        "numbers": {
            "type": "array",
            "nullable": True,
            "items": {"type": "integer", "nullable": True},
        },
    }


def test_to_yaml_accepts_target_version():
    spec = make_spec("3.1.0", NestedSchema)

    yaml_document = spec.to_yaml(openapi_version="3.0.0")

    assert "openapi: 3.0.0" in yaml_document
    assert "nullable: true" in yaml_document
    assert spec.openapi_version.major == 3
    assert spec.openapi_version.minor == 1


def test_components_can_be_rendered_for_another_version():
    spec = make_spec("3.1.0", NestedSchema)

    components_30 = spec.components.to_dict("3.0.0")
    components_31 = spec.components.to_dict("3.1.0")

    assert components_30["schemas"]["NestedSchema"]["properties"]["child"] == {
        "anyOf": [
            {"type": "object", "nullable": True},
            {"$ref": "#/components/schemas/Child"},
        ]
    }
    assert components_31["schemas"]["NestedSchema"]["properties"]["child"] == {
        "anyOf": [{"$ref": "#/components/schemas/Child"}, {"type": "null"}]
    }
    assert spec.components.to_dict() == components_31


def test_target_version_aliases():
    spec = make_spec("3.1.0", NestedSchema)

    assert spec.to_dict(target_version="3.0.0") == spec.to_dict("3.0.0")
    assert spec.to_dict(version="3.0.0") == spec.to_dict("3.0.0")


def test_cannot_export_to_another_major_version():
    spec = make_spec("3.1.0", NestedSchema)

    with pytest.raises(APISpecError, match="different OpenAPI major version"):
        spec.to_dict("2.0")

    with pytest.raises(APISpecError, match="only one"):
        spec.to_dict("3.0.0", target_version="3.1.0")

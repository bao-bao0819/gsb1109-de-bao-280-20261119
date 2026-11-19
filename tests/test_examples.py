import pytest
from marshmallow import Schema, fields

from apispec import APISpec
from apispec.examples import (
    ExamplePath,
    ExampleValidationIssue,
    validate_examples,
)
from apispec.exceptions import APISpecError, ExampleValidationError
from apispec.ext.marshmallow import MarshmallowPlugin


class TagSchema(Schema):
    name = fields.Str(required=True)


class ItemSchema(Schema):
    id = fields.Int(dump_only=True)
    token = fields.Str(load_only=True)
    name = fields.Str(required=True)
    tags = fields.List(fields.Nested(TagSchema))
    children = fields.List(fields.Nested(lambda: ItemSchema()))


def make_spec(openapi_version="3.0.0", **kwargs):
    return APISpec(
        title="Examples",
        version="1.0.0",
        openapi_version=openapi_version,
        plugins=[MarshmallowPlugin()],
        **kwargs,
    )


def spec_with_schemas(openapi_version="3.0.0", **kwargs):
    spec = make_spec(openapi_version, **kwargs)
    spec.components.schema("Tag", schema=TagSchema)
    spec.components.schema("Item", schema=ItemSchema)
    return spec


def response_example(spec, example, name="Example"):
    spec.components.response(
        "GetItem",
        {
            "content": {
                "application/json": {
                    "schema": "Item",
                    "examples": {name: {"value": example}},
                }
            }
        },
    )
    return spec


def request_example(spec, example):
    spec.path(
        "/items",
        operations={
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {"schema": "Item", "example": example}
                    }
                }
            }
        },
    )
    return spec


class TestExamplePath:
    def test_string_representation(self):
        path = ExamplePath(["items", 0, "name"])
        assert str(path) == "$.items[0].name"
        assert path.property_path == "items[0].name"
        assert path.path_string == "items[0].name"
        assert path.json_path == "$.items[0].name"

    def test_compares_to_common_string_forms(self):
        path = ExamplePath(["items", 0, "name"])
        assert path == "$.items[0].name"
        assert path == "items[0].name"
        assert path != "$.other"

    def test_non_identifier_keys_are_quoted(self):
        path = ExamplePath(["a-b", "c d"])
        assert str(path) == "$['a-b']['c d']"


class TestValidateExamplesFunction:
    def test_valid_examples_return_no_issues(self):
        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "schemas": {
                    "Tag": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                    "Item": {
                        "type": "object",
                        "properties": {
                            "tags": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/Tag"},
                            }
                        },
                    },
                }
            },
        }
        assert validate_examples(document) == []

    def test_reports_nested_array_element_path(self):
        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "schemas": {
                    "Tag": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    }
                }
            },
            "unused": {},
        }
        document["components"]["responses"] = {
            "Get": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "tags": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Tag"},
                                }
                            },
                        },
                        "examples": {
                            "bad": {"value": {"tags": [{"name": "ok"}, {"name": 3}]}}
                        },
                    }
                }
            }
        }
        issues = validate_examples(document)
        assert len(issues) == 1
        issue = issues[0]
        assert isinstance(issue, ExampleValidationIssue)
        assert issue.path == "$.tags[1].name"
        assert issue.location == (
            "components.responses.Get.content.application/json.examples.bad"
        )
        assert issue.example == "bad"

    def test_recursive_schema_with_many_relationship(self):
        def object_schema(extra=None):
            schema = {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Node"},
                    },
                },
                "required": ["id"],
            }
            if extra:
                schema["properties"].update(extra)
            return schema

        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "schemas": {"Node": object_schema()},
                "responses": {
                    "Get": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Node"},
                                "example": {
                                    "id": 1,
                                    "children": [
                                        {"id": 2},
                                        {"id": "three", "children": [{"id": 4}]},
                                    ],
                                },
                            }
                        }
                    }
                },
            },
        }
        issues = validate_examples(document)
        assert [issue.path for issue in issues] == ["$.children[1].id"]

    def test_request_example_may_omit_read_only_property(self):
        spec = spec_with_schemas(validate_examples=True)
        request_example(spec, {"name": "Only the name"})
        assert spec.to_dict()["paths"]["/items"]["post"]["requestBody"]

    def test_request_example_ignores_read_only_property(self):
        spec = spec_with_schemas(validate_examples=True)
        request_example(spec, {"id": 1, "name": "Has id"})
        with pytest.raises(ExampleValidationError) as exc_info:
            spec.to_dict()
        (issue,) = exc_info.value.issues
        assert issue.path == "$.id"
        assert "readOnly" in issue.message
        assert issue.location == (
            "paths./items.post.requestBody.content.application/json.example"
        )

    def test_response_example_requires_read_only_property_when_required(self):
        class RequiredIdSchema(Schema):
            id = fields.Int(dump_only=True, required=True)
            name = fields.Str()

        spec = make_spec(validate_examples=True)
        spec.components.schema("Item", schema=RequiredIdSchema)
        response_example(spec, {"name": "No id"})
        with pytest.raises(ExampleValidationError) as exc_info:
            spec.to_dict()
        (issue,) = exc_info.value.issues
        assert issue.path == "$.id"
        assert "missing" in issue.message

    def test_response_example_rejects_write_only_property(self):
        spec = spec_with_schemas(validate_examples=True)
        response_example(spec, {"id": 1, "name": "ok", "token": "secret"})
        with pytest.raises(ExampleValidationError) as exc_info:
            spec.to_dict()
        (issue,) = exc_info.value.issues
        assert issue.path == "$.token"
        assert "writeOnly" in issue.message

    def test_openapi_2_response_examples(self):
        spec = spec_with_schemas("2.0", validate_examples=True)
        spec.components.response(
            "GetItem",
            {
                "schema": "Item",
                "examples": {"application/json": {"name": 3}},
            },
        )
        with pytest.raises(ExampleValidationError) as exc_info:
            spec.to_dict()
        (issue,) = exc_info.value.issues
        assert issue.location == "responses.GetItem.examples.application/json"
        assert issue.path == "$.name"
        assert "expected type 'string'" in issue.message

    def test_openapi_2_body_parameter_examples(self):
        spec = spec_with_schemas("2.0", validate_examples=True)
        spec.path(
            "/items",
            operations={
                "post": {
                    "parameters": [
                        {
                            "in": "body",
                            "name": "body",
                            "schema": ItemSchema,
                        }
                    ],
                    "responses": {"200": {}},
                }
            },
        )
        assert spec.to_dict()["paths"]["/items"]["post"]["parameters"][0]

    def test_openapi_2_parameter_x_example(self):
        spec = make_spec("2.0", validate_examples=True)
        spec.path(
            "/items",
            operations={
                "get": {
                    "parameters": [
                        {
                            "in": "query",
                            "name": "limit",
                            "type": "integer",
                            "x-example": "ten",
                        }
                    ],
                    "responses": {"200": {}},
                }
            },
        )
        with pytest.raises(ExampleValidationError) as exc_info:
            spec.to_dict()
        (issue,) = exc_info.value.issues
        assert issue.path == "$"
        assert issue.location == "paths./items.get.parameters[0].x-example"

    def test_inline_object_schema_example(self):
        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "schemas": {
                    "Tag": {
                        "type": "object",
                        "properties": {"v": {"type": "string"}},
                    }
                },
                "responses": {
                    "Get": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "items": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/Tag"
                                            },
                                        },
                                        "by_name": {
                                            "type": "object",
                                            "additionalProperties": {
                                                "$ref": "#/components/schemas/Tag"
                                            },
                                        },
                                    },
                                },
                                "example": {
                                    "items": [{"v": 1}],
                                    "by_name": {"a": {"v": 2}},
                                },
                            }
                        }
                    }
                },
            },
        }
        issues = validate_examples(document)
        assert [issue.path for issue in issues] == [
            "$.items[0].v",
            "$.by_name.a.v",
        ]

    def test_nullable_all_of_item_accepts_null(self):
        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "schemas": {
                    "Tag": {
                        "type": "object",
                        "properties": {"v": {"type": "string"}},
                    }
                },
                "responses": {
                    "Get": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "tag": {
                                            "nullable": True,
                                            "allOf": [
                                                {"$ref": "#/components/schemas/Tag"}
                                            ],
                                        }
                                    },
                                },
                                "example": {"tag": None},
                            }
                        }
                    }
                },
            },
        }
        assert validate_examples(document) == []

    def test_openapi_31_integer_accepts_integral_float(self):
        document = {
            "openapi": "3.1.0",
            "paths": {},
            "components": {
                "responses": {
                    "Get": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "integer"},
                                "example": 3.0,
                            }
                        }
                    }
                }
            },
        }
        assert validate_examples(document) == []

    def test_one_of_reports_element_path(self):
        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "schemas": {
                    "Cat": {
                        "type": "object",
                        "properties": {"meow": {"type": "string"}},
                        "required": ["meow"],
                        "additionalProperties": False,
                    },
                    "Dog": {
                        "type": "object",
                        "properties": {"bark": {"type": "string"}},
                        "required": ["bark"],
                        "additionalProperties": False,
                    },
                },
                "responses": {
                    "Get": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {
                                        "oneOf": [
                                            {"$ref": "#/components/schemas/Cat"},
                                            {"$ref": "#/components/schemas/Dog"},
                                        ]
                                    },
                                },
                                "example": [
                                    {"meow": "a"},
                                    {"meow": "b", "bark": 3},
                                ],
                            }
                        }
                    }
                },
            },
        }
        issues = validate_examples(document)
        assert len(issues) == 1
        assert issues[0].path == "$[1]"
        assert "does not match any" in issues[0].message

    def test_lazy_example_component_is_checked_once_referenced(self):
        spec = make_spec(validate_examples=True)
        spec.components.schema("Tag", schema=TagSchema)
        spec.components.example("Bad", {"value": {"name": 3}}, lazy=True, schema="Tag")
        assert "Bad" not in spec.components.examples
        spec.components.response(
            "GetTag",
            {
                "content": {
                    "application/json": {
                        "schema": "Tag",
                        "examples": {"bad": "Bad"},
                    }
                }
            },
        )
        with pytest.raises(ExampleValidationError) as exc_info:
            spec.to_dict()
        (issue,) = exc_info.value.issues
        assert issue.location == (
            "components.responses.GetTag.content.application/json.examples.bad"
        )
        assert issue.path == "$.name"

    def test_unreferenced_example_component_uses_bound_schema(self):
        spec = make_spec(validate_examples=True)
        spec.components.schema("Tag", schema=TagSchema)
        spec.components.example("Bad", {"value": {"name": 3}}, schema="Tag")
        with pytest.raises(ExampleValidationError) as exc_info:
            spec.to_dict()
        (issue,) = exc_info.value.issues
        assert issue.location == "components.examples.Bad"
        assert issue.path == "$.name"

    def test_example_schema_is_not_emitted(self):
        spec = make_spec()
        spec.components.example(
            "TagValue", {"value": {"name": "ok"}}, schema={"type": "object"}
        )
        assert spec.to_dict()["components"]["examples"]["TagValue"] == {
            "value": {"name": "ok"}
        }

    def test_example_with_external_value_is_ignored(self):
        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "schemas": {
                    "Tag": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    }
                },
                "responses": {
                    "Get": {
                        "content": {
                            "application/json": {
                                "schema": "Tag",
                                "examples": {"external": {"externalValue": "x.json"}},
                            }
                        }
                    }
                },
            },
        }
        assert validate_examples(document) == []

    def test_raise_errors_argument(self):
        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "responses": {
                    "Get": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "string"},
                                "example": 3,
                            }
                        }
                    }
                }
            },
        }
        with pytest.raises(ExampleValidationError) as exc_info:
            validate_examples(document, raise_errors=True)
        assert exc_info.value.issues[0].path == "$"

    def test_schema_bound_example_component(self):
        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "schemas": {
                    "Tag": {"type": "object", "properties": {"v": {"type": "string"}}}
                },
                "examples": {
                    "Wrong": {"value": {"v": 3}},
                    "Right": {"value": {"v": "ok"}},
                },
            },
        }
        issues = validate_examples(
            document,
            example_schemas={"Wrong": "Tag", "Right": "Tag"},
        )
        assert len(issues) == 1
        assert issues[0].location == "components.examples.Wrong"

    def test_referenced_example_component_is_reported_at_use_site(self):
        document = {
            "openapi": "3.0.0",
            "paths": {},
            "components": {
                "schemas": {
                    "Tag": {"type": "object", "properties": {"v": {"type": "string"}}}
                },
                "examples": {"Wrong": {"value": {"v": 3}}},
                "responses": {
                    "Get": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Tag"},
                                "examples": {
                                    "w": {"$ref": "#/components/examples/Wrong"}
                                },
                            }
                        }
                    }
                },
            },
        }
        issues = validate_examples(document)
        assert len(issues) == 1
        assert issues[0].location == (
            "components.responses.Get.content.application/json.examples.w"
        )
        assert issues[0].path == "$.v"


class TestGenerationOption:
    def test_validation_is_off_by_default(self):
        spec = spec_with_schemas()
        response_example(spec, {"tags": [{"name": 3}]})
        assert spec.to_dict()

    @pytest.mark.parametrize(
        "option", ("validate_examples", "validate_example_values", "check_examples")
    )
    def test_option_can_be_passed_as_top_level_option(self, option):
        spec = make_spec(**{option: True})
        spec.components.schema("Tag", schema=TagSchema)
        spec.components.schema("Item", schema=ItemSchema)
        response_example(spec, {"name": "ok", "tags": [{"name": 3}]})
        with pytest.raises(ExampleValidationError):
            spec.to_dict()
        # The option is consumed and not emitted into the document.
        assert option not in spec.options

    @pytest.mark.parametrize(("value", "expected"), ((True, True), (False, False)))
    def test_public_validate_examples_attribute(self, value, expected):
        assert make_spec(validate_examples=value).validate_examples is expected
        assert make_spec().validate_examples is False

    def test_invalid_option_value(self):
        with pytest.raises(APISpecError, match="validate_examples"):
            make_spec(validate_examples="yes")

    def test_warn_mode_returns_document(self):
        spec = spec_with_schemas(validate_examples="warn")
        response_example(spec, {"tags": [{"name": 3}]})
        with pytest.warns(UserWarning, match=r"\$\.tags\[0\]\.name"):
            document = spec.to_dict()
        assert document["paths"] == {}

    def test_per_call_validation_override(self):
        spec = spec_with_schemas()
        response_example(spec, {"tags": [{"name": 3}]})
        with pytest.raises(ExampleValidationError):
            spec.to_dict(validate_examples=True)
        assert spec.to_dict(validate_examples=False)["paths"] == {}

    def test_get_example_issues_without_raising(self):
        spec = spec_with_schemas()
        response_example(spec, {"name": "ok", "tags": [{"name": 3}]})
        issues = spec.get_example_issues()
        assert [issue.path for issue in issues] == ["$.tags[0].name"]
        assert issues[0].location == (
            "components.responses.GetItem.content.application/json.examples.Example"
        )

    def test_to_yaml_raises(self):
        spec = spec_with_schemas(validate_examples=True)
        response_example(spec, {"tags": [{"name": 3}]})
        with pytest.raises(ExampleValidationError):
            spec.to_yaml()

    @pytest.mark.parametrize(
        ("plugin_option", "spec_option"),
        ((True, None), ("warn", None), (True, False)),
    )
    def test_plugin_option(self, plugin_option, spec_option):
        plugin = MarshmallowPlugin(validate_examples=plugin_option)
        spec = APISpec(
            title="Examples",
            version="1.0.0",
            openapi_version="3.0.0",
            plugins=[plugin],
            **({} if spec_option is None else {"validate_examples": spec_option}),
        )
        spec.components.schema("Tag", schema=TagSchema)
        spec.components.response(
            "GetTag",
            {
                "content": {
                    "application/json": {
                        "schema": "Tag",
                        "example": {"name": 3},
                    }
                }
            },
        )
        if spec_option is False:
            assert spec.to_dict()
        elif plugin_option == "warn":
            with pytest.warns(UserWarning, match=r"\$\.name"):
                assert spec.to_dict()
        else:
            with pytest.raises(ExampleValidationError):
                spec.to_dict()

    def test_error_message_lists_all_issues(self):
        spec = spec_with_schemas(validate_examples=True)
        response_example(
            spec,
            {
                "name": "ok",
                "tags": [{"name": 3}],
                "children": [{"name": "ok", "tags": [{"name": 4}]}],
            },
        )
        with pytest.raises(ExampleValidationError) as exc_info:
            spec.to_dict()
        paths = [str(issue.path) for issue in exc_info.value.issues]
        assert paths == ["$.tags[0].name", "$.children[0].tags[0].name"]
        message = str(exc_info.value)
        assert "$.tags[0].name" in message
        assert "Invalid example values" in message

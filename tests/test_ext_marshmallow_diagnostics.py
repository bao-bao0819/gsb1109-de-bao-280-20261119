import logging

from marshmallow import Schema, fields

from apispec import APISpec
from apispec.ext.marshmallow import MarshmallowPlugin


class Unsupported(fields.Field):
    pass


class Inner(Schema):
    broken = Unsupported()


class Middle(Schema):
    inner = fields.Nested(Inner)


class Outer(Schema):
    middle = fields.Nested(Middle)
    broken_list = fields.List(Unsupported())


def make_spec(openapi_version="3.0.0"):
    plugin = MarshmallowPlugin()
    spec = APISpec(
        title="Diagnostics",
        version="1.0.0",
        openapi_version=openapi_version,
        plugins=[plugin],
    )
    return spec, plugin


def register_outer(spec):
    spec.components.schema("Outer", schema=Outer)
    spec.path(
        "/things",
        operations={
            "get": {
                "operationId": "listThings",
                "responses": {
                    "200": {"content": {"application/json": {"schema": "Outer"}}}
                },
            }
        },
    )
    spec.path(
        "/things/import",
        operations={
            "post": {
                "operationId": "importThings",
                "requestBody": {"content": {"application/json": {"schema": Outer}}},
                "responses": {"200": {"description": "ok"}},
            }
        },
    )


def test_report_attributes_reuse_to_both_operations(caplog):
    spec, plugin = make_spec()
    register_outer(spec)

    with caplog.at_level(
        logging.WARNING, logger="apispec.ext.marshmallow.diagnostics"
    ):
        document = spec.to_dict()

    report = plugin.converter.build_conversion_report(log=False)
    assert len(report.issues) == 2
    assert len(report) == 4
    assert len(report.for_operation("/things", "get")) == 2
    assert len(report.for_operation("/things/import", "post")) == 2

    broken = next(issue for issue in report.issues if issue.field_name == "broken")
    assert broken.path == "Inner.broken"
    assert {(op["path"], op["method"]) for op in broken.operations} == {
        ("/things", "get"),
        ("/things/import", "post"),
    }

    nested = next(
        occurrence
        for occurrence in report
        if occurrence.field_name == "broken"
        and occurrence.reference.method == "get"
    )
    assert nested.schema_path == ("Outer", "Middle", "Inner")
    assert "nesting: Outer -> Middle -> Inner" in str(nested)

    # The generated document keeps its usual structure.
    assert "diagnostics" not in document
    assert "conversion" not in spec.to_yaml().lower()
    assert document["components"]["schemas"]["Outer"]["properties"]["middle"] == {
        "$ref": "#/components/schemas/Middle"
    }
    assert document["components"]["schemas"]["Inner"]["properties"]["broken"] == {}

    messages = [record.getMessage() for record in caplog.records]
    assert sum("listThings" in message for message in messages) == 2
    assert sum("importThings" in message for message in messages) == 2


def test_report_does_not_affect_schema_output():
    spec, plugin = make_spec()
    register_outer(spec)

    without_report = spec.to_dict()
    plugin.converter.build_conversion_report(log=False)
    assert spec.to_dict() == without_report


def test_report_component_only_schema():
    spec, plugin = make_spec()
    spec.components.schema("Inner", schema=Inner)
    spec.to_dict()

    report = plugin.converter.build_conversion_report(log=False)
    assert len(report) == 1
    occurrence = report[0]
    assert occurrence.reference.kind == "component"
    assert occurrence.reference.component_name == "Inner"
    assert occurrence.path == "Inner.broken"


def test_no_report_for_intentional_typeless_fields():
    class SchemaWithTypeless(Schema):
        raw = fields.Raw()
        field = fields.Field()
        function = fields.Function(lambda obj: obj)
        tuple_field = fields.Tuple([fields.Int()])

    spec, plugin = make_spec()
    spec.components.schema("Typeless", schema=SchemaWithTypeless)
    spec.to_dict()
    assert not plugin.converter.build_conversion_report(log=False)


def test_report_for_required_patch_target():
    spec, plugin = make_spec()
    spec.path(
        "/widgets",
        operations={
            "get": {
                "operationId": "getWidgets",
                "parameters": [{"in": "query", "schema": Outer}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    )
    report = plugin.converter.build_conversion_report(log=False)
    assert report
    assert all(
        occurrence.reference.operation_data["operationId"] == "getWidgets"
        for occurrence in report
    )


def test_report_before_and_after_component_registration():
    plugin = MarshmallowPlugin()
    spec = APISpec(
        title="Diagnostics",
        version="1.0.0",
        openapi_version="2.0",
        plugins=[plugin],
    )
    spec.path(
        "/a",
        operations={
            "get": {
                "operationId": "a",
                "responses": {"200": {"schema": Outer, "description": "ok"}},
            }
        },
    )
    first = plugin.converter.build_conversion_report(log=False)
    assert len(first) == 2

    spec.path(
        "/b",
        operations={
            "post": {
                "operationId": "b",
                "parameters": [{"in": "body", "schema": Outer}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    )
    second = plugin.converter.build_conversion_report(log=False)
    assert len(second) == 4


def test_report_inline_schema_used_by_two_operations():
    plugin = MarshmallowPlugin(schema_name_resolver=lambda schema: None)
    spec = APISpec(
        title="Diagnostics",
        version="1.0.0",
        openapi_version="3.0.0",
        plugins=[plugin],
    )

    class Inline(Schema):
        broken = Unsupported()

    class Root(Schema):
        inline = fields.Nested(lambda: Inline())

    spec.path(
        "/a",
        operations={
            "get": {
                "operationId": "a",
                "responses": {
                    "200": {"content": {"application/json": {"schema": Root}}}
                },
            }
        },
    )
    spec.path(
        "/b",
        operations={
            "post": {
                "operationId": "b",
                "requestBody": {"content": {"application/json": {"schema": Root}}},
                "responses": {"200": {"description": "ok"}},
            }
        },
    )
    spec.to_dict()
    report = plugin.converter.build_conversion_report(log=False)
    assert len(report.issues) == 1
    assert {occurrence.reference.operation_data["path"] for occurrence in report} == {
        "/a",
        "/b",
    }
    assert all(
        occurrence.path == "Root.inline.broken" for occurrence in report
    )


def test_spec_level_report_aggregates_plugins():
    spec, plugin = make_spec()
    register_outer(spec)
    spec.to_dict()

    report = spec.conversion_report
    assert report.reports == [plugin.conversion_report]
    assert len(report) == len(plugin.conversion_report)
    assert report.to_dict() == plugin.conversion_report.to_dict()


def test_spec_level_report_is_empty_without_diagnostics():
    spec = APISpec(
        title="Diagnostics",
        version="1.0.0",
        openapi_version="3.0.0",
        plugins=[MarshmallowPlugin()],
    )
    assert not spec.conversion_report


def test_conversion_report_json_serializable():
    import json

    spec, plugin = make_spec()
    register_outer(spec)
    spec.to_dict()
    json.dumps(spec.conversion_report.to_dict())


def test_list_nesting_path_is_reported():
    spec, plugin = make_spec()
    register_outer(spec)
    spec.to_dict()

    report = plugin.converter.build_conversion_report(log=False)
    list_issue = next(
        occurrence for occurrence in report if occurrence.field_name == "items"
    )
    assert list_issue.path == "Outer.broken_list.items"


def test_no_report_for_mapped_custom_field():
    class CustomString(fields.String):
        pass

    class Mapped(Schema):
        value = CustomString()

    spec, plugin = make_spec()
    spec.components.schema("Mapped", schema=Mapped)
    spec.to_dict()
    assert not plugin.converter.build_conversion_report(log=False)

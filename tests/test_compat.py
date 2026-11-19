import json

import yaml

from apispec import APISpec, compare_specs, find_breaking_changes, is_compatible
from apispec.compat import (
    CompatibilityIssue,
    CompatibilityReport,
    main,
)


def make_document(paths, schemas=None, version="3.0.2"):
    document = {
        "openapi": version,
        "info": {"title": "Jobs", "version": "1.0"},
        "paths": paths,
    }
    if schemas is not None:
        document["components"] = {"schemas": schemas}
    return document


def json_response(ref):
    return {
        "description": "ok",
        "content": {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}
        },
    }


def json_body(ref, required=True):
    return {
        "required": required,
        "content": {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}
        },
    }


class TestRequiredRequestProperty:
    def test_required_request_property_added_is_breaking(self):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                }
            },
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "required": ["priority"],
                    "properties": {
                        "name": {"type": "string"},
                        "priority": {"type": "integer"},
                    },
                }
            },
        )

        report = compare_specs(old, new)

        assert not report.compatible
        assert len(report.breaking_changes) == 1
        change = report.breaking_changes[0]
        assert change.operation == "POST /jobs"
        assert change.property == "body.priority"
        assert change.name == "priority"
        assert change.location == "request"
        assert change.kind == "request.required.added"
        assert change.status_code is None

    def test_optional_request_property_added_is_compatible(self):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {"JobIn": {"type": "object", "properties": {"name": {"type": "string"}}}},
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "priority": {"type": "integer"},
                    },
                }
            },
        )

        report = compare_specs(old, new)

        assert report.compatible
        assert report.breaking_changes == []

    def test_required_query_parameter_added_is_breaking(self):
        old = make_document(
            {
                "/jobs": {
                    "get": {
                        "parameters": [
                            {
                                "name": "team",
                                "in": "query",
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
        new = make_document(
            {
                "/jobs": {
                    "get": {
                        "parameters": [
                            {
                                "name": "team",
                                "in": "query",
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "region",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )

        report = compare_specs(old, new)

        assert not report.compatible
        assert [(c.property, c.kind) for c in report.breaking_changes] == [
            ("query.region", "request.required.added")
        ]


class TestOptionalResponseProperty:
    def test_optional_response_property_added_is_reported_but_not_breaking(self):
        old = make_document(
            {
                "/jobs/{id}": {
                    "get": {
                        "responses": {"200": json_response("JobOut")},
                    }
                }
            },
            {"JobOut": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        new = make_document(
            {
                "/jobs/{id}": {
                    "get": {
                        "responses": {"200": json_response("JobOut")},
                    }
                }
            },
            {
                "JobOut": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "eta": {"type": "string", "nullable": True},
                    },
                }
            },
        )

        report = compare_specs(old, new)

        assert report.compatible
        assert len(report.non_breaking_changes) == 1
        change = report.non_breaking_changes[0]
        assert change.operation == "GET /jobs/{id}"
        assert change.property == "200.body.eta"
        assert change.location == "response"
        assert change.status_code == "200"
        assert change.kind == "response.optional.added"
        assert not change.breaking

    def test_optional_response_property_is_not_reported_through_renamed_component(
        self,
    ):
        old = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {"JobOut": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        new = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobResult")}}}},
            {"JobResult": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )

        report = compare_specs(old, new)

        assert report.changes == []
        assert report.compatible

    def test_nested_optional_response_property_is_reported(self):
        old = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {
                "JobOut": {
                    "type": "object",
                    "properties": {
                        "owner": {"$ref": "#/components/schemas/Owner"},
                    },
                },
                "Owner": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        )
        new = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {
                "JobOut": {
                    "type": "object",
                    "properties": {
                        "owner": {"$ref": "#/components/schemas/Owner"},
                    },
                },
                "Owner": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "email": {"type": "string"},
                    },
                },
            },
        )

        report = compare_specs(old, new)

        assert [(c.property, c.kind) for c in report] == [
            ("200.body.owner.email", "response.optional.added")
        ]


class TestEnumNarrowing:
    def test_request_enum_narrowed_is_breaking(self):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["queued", "running", "done"],
                        }
                    },
                }
            },
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "enum": ["queued", "done"]}
                    },
                }
            },
        )

        report = compare_specs(old, new)

        assert not report.compatible
        change = report.breaking_changes[0]
        assert change.property == "body.state"
        assert change.kind == "request.enum.narrowed"
        assert change.old == ["queued", "running", "done"]
        assert change.new == ["queued", "done"]

    def test_enum_added_to_unconstrained_request_is_breaking(self):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {"JobIn": {"type": "object", "properties": {"state": {"type": "string"}}}},
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "enum": ["queued", "done"]}
                    },
                }
            },
        )

        report = compare_specs(old, new)

        assert [(c.property, c.kind) for c in report.breaking_changes] == [
            ("body.state", "request.enum.narrowed")
        ]
        assert report.breaking_changes[0].old is None

    def test_response_enum_narrowed_is_not_breaking(self):
        old = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {
                "JobOut": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["queued", "running", "done"],
                        }
                    },
                }
            },
        )
        new = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {
                "JobOut": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "enum": ["queued", "done"]}
                    },
                }
            },
        )

        report = compare_specs(old, new)

        assert report.compatible
        assert [(c.property, c.kind) for c in report] == [
            ("200.body.state", "response.enum.narrowed")
        ]

    def test_request_enum_widened_is_compatible(self):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "enum": ["queued", "done"]}
                    },
                }
            },
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["queued", "running", "done"],
                        }
                    },
                }
            },
        )

        report = compare_specs(old, new)

        assert report.changes == []
        assert report.compatible

    def test_enum_narrowed_in_array_items_is_breaking(self):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "properties": {
                        "tags": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["a", "b", "c"]},
                        }
                    },
                }
            },
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "properties": {
                        "tags": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["a", "b"]},
                        }
                    },
                }
            },
        )

        report = compare_specs(old, new)

        assert [(c.property, c.kind) for c in report.breaking_changes] == [
            ("body.tags[]", "request.enum.narrowed")
        ]

    def test_enum_from_all_of_is_intersected(self):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "allOf": [
                        {
                            "type": "object",
                            "properties": {
                                "state": {
                                    "type": "string",
                                    "enum": ["queued", "running", "done"],
                                }
                            },
                        }
                    ]
                }
            },
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "allOf": [
                        {
                            "type": "object",
                            "properties": {
                                "state": {
                                    "type": "string",
                                    "enum": ["queued", "done"],
                                }
                            },
                        }
                    ]
                }
            },
        )

        report = compare_specs(old, new)

        assert [(c.property, c.kind) for c in report.breaking_changes] == [
            ("body.state", "request.enum.narrowed")
        ]


class TestComponentRename:
    def test_renamed_component_with_equivalent_schema_is_not_reported(self):
        old = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {
                "JobOut": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "state": {"type": "string"},
                    },
                }
            },
        )
        new = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobResult")}}}},
            {
                "JobResult": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "state": {"type": "string"},
                    },
                }
            },
        )

        report = compare_specs(old, new)

        assert report.changes == []

    def test_renamed_nested_components_are_not_reported(self):
        old = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobPage")}}}},
            {
                "JobPage": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/JobOut"},
                        }
                    },
                },
                "JobOut": {
                    "type": "object",
                    "properties": {"owner": {"$ref": "#/components/schemas/Owner"}},
                },
                "Owner": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        )
        new = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobPageResult")}}}},
            {
                "JobPageResult": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/JobResult"},
                        }
                    },
                },
                "JobResult": {
                    "type": "object",
                    "properties": {"owner": {"$ref": "#/components/schemas/User"}},
                },
                "User": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        )

        report = compare_specs(old, new)

        assert report.changes == []

    def test_renamed_component_with_new_required_field_is_reported(self):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {"JobIn": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobRequest"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobRequest": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string"}},
                }
            },
        )

        report = compare_specs(old, new)

        assert [(c.property, c.kind) for c in report.breaking_changes] == [
            ("body.id", "request.required.changed")
        ]


class TestOpenAPI2:
    def test_required_body_schema_property_added_is_breaking(self):
        old = {
            "swagger": "2.0",
            "info": {"title": "Jobs", "version": "1.0"},
            "paths": {
                "/jobs": {
                    "post": {
                        "parameters": [
                            {
                                "name": "body",
                                "in": "body",
                                "required": True,
                                "schema": {"$ref": "#/definitions/JobIn"},
                            }
                        ],
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            "definitions": {
                "JobIn": {"type": "object", "properties": {"id": {"type": "string"}}}
            },
        }
        new = {
            "swagger": "2.0",
            "info": {"title": "Jobs", "version": "2.0"},
            "paths": {
                "/jobs": {
                    "post": {
                        "parameters": [
                            {
                                "name": "body",
                                "in": "body",
                                "required": True,
                                "schema": {"$ref": "#/definitions/JobIn"},
                            }
                        ],
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            "definitions": {
                "JobIn": {
                    "type": "object",
                    "required": ["priority"],
                    "properties": {
                        "id": {"type": "string"},
                        "priority": {"type": "integer"},
                    },
                }
            },
        }

        report = compare_specs(old, new)

        assert [(c.property, c.kind) for c in report.breaking_changes] == [
            ("body.priority", "request.required.added")
        ]

    def test_oas2_response_enum_narrowed_is_not_breaking(self):
        old = {
            "swagger": "2.0",
            "info": {"title": "Jobs", "version": "1.0"},
            "paths": {
                "/jobs": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "schema": {"$ref": "#/definitions/JobOut"},
                            }
                        }
                    }
                }
            },
            "definitions": {
                "JobOut": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "enum": ["done", "failed"]}
                    },
                }
            },
        }
        new = {
            "swagger": "2.0",
            "info": {"title": "Jobs", "version": "2.0"},
            "paths": {
                "/jobs": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "schema": {"$ref": "#/definitions/JobResult"},
                            }
                        }
                    }
                }
            },
            "definitions": {
                "JobResult": {
                    "type": "object",
                    "properties": {"state": {"type": "string", "enum": ["done"]}},
                }
            },
        }

        report = compare_specs(old, new)

        assert report.compatible
        assert [(c.property, c.kind) for c in report] == [
            ("200.body.state", "response.enum.narrowed")
        ]


class TestReport:
    def test_no_changes(self):
        document = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {"JobOut": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )

        report = compare_specs(document, document)

        assert report.changes == []
        assert report.compatible
        assert report.summary() == "No compatibility changes detected."

    def test_report_serialization(self):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {"JobIn": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string"}},
                }
            },
        )

        payload = json.loads(compare_specs(old, new).to_json())

        assert payload["compatible"] is False
        assert payload["breaking_count"] == 1
        assert payload["affected_operations"] == ["POST /jobs"]
        assert payload["affected_properties"] == ["body.id"]
        assert payload["changes"][0]["property"] == "body.id"
        assert payload["changes"][0]["severity"] == "breaking"

    def test_breaking_only_filter(self):
        old = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {"JobOut": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        new = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {
                "JobOut": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "eta": {"type": "string"},
                    },
                }
            },
        )

        assert compare_specs(old, new).changes != []
        assert compare_specs(old, new, include_non_breaking=False).changes == []
        assert find_breaking_changes(old, new) == []

    def test_removed_operation_is_out_of_scope(self):
        old = make_document(
            {
                "/jobs": {"get": {"responses": {"200": json_response("JobOut")}}},
                "/users": {"get": {"responses": {"200": json_response("JobOut")}}},
            },
            {"JobOut": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        new = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {"JobOut": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )

        report = compare_specs(old, new)

        assert report.changes == []
        assert report.compatible

    def test_accepts_apispec_instances(self):
        spec = APISpec(title="Jobs", version="1.0", openapi_version="3.0.2")
        spec.path(
            path="/jobs",
            operations={
                "get": {"responses": {200: {"description": "ok"}}},
            },
        )

        report = compare_specs(spec, spec)

        assert report.compatible

    def test_accepts_yaml_files(self, tmp_path):
        document = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {"JobOut": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        old_path = tmp_path / "old.yaml"
        new_path = tmp_path / "new.yaml"
        old_path.write_text(yaml.safe_dump(document), encoding="utf-8")
        document["components"]["schemas"]["JobOut"]["properties"]["eta"] = {
            "type": "string"
        }
        new_path.write_text(yaml.safe_dump(document), encoding="utf-8")

        report = compare_specs(str(old_path), new_path)

        assert [(c.property, c.kind) for c in report] == [
            ("200.body.eta", "response.optional.added")
        ]

    def test_is_compatible_helper(self):
        document = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {"JobOut": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        assert is_compatible(document, document)


class TestCli:
    def _write(self, path, document):
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return str(path)

    def test_main_reports_breaking_change(self, tmp_path, capsys):
        old = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {"JobIn": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        new = make_document(
            {
                "/jobs": {
                    "post": {
                        "requestBody": json_body("JobIn"),
                        "responses": {"201": {"description": "created"}},
                    }
                }
            },
            {
                "JobIn": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string"}},
                }
            },
        )

        exit_code = main(
            [
                self._write(tmp_path / "old.yaml", old),
                self._write(tmp_path / "new.yaml", new),
            ]
        )
        output = capsys.readouterr().out

        assert exit_code == 1
        assert "BREAKING" in output
        assert "body.id" in output

    def test_main_json_and_no_fail(self, tmp_path, capsys):
        old = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {"JobOut": {"type": "object", "properties": {"id": {"type": "string"}}}},
        )
        new = make_document(
            {"/jobs": {"get": {"responses": {"200": json_response("JobOut")}}}},
            {
                "JobOut": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "eta": {"type": "string"},
                    },
                }
            },
        )

        exit_code = main(
            [
                self._write(tmp_path / "old.yaml", old),
                self._write(tmp_path / "new.yaml", new),
                "--json",
                "--no-fail",
            ]
        )
        payload = json.loads(capsys.readouterr().out)

        assert exit_code == 0
        assert payload["compatible"] is True
        assert payload["changes"][0]["kind"] == "response.optional.added"


def test_compatibility_issue_is_mapping_like():
    issue = CompatibilityIssue(
        operation="GET /jobs",
        method="GET",
        path="/jobs",
        location="response",
        property="200.body.eta",
        kind="response.optional.added",
        breaking=False,
        message="eta added",
    )

    assert issue["property"] == "200.body.eta"
    assert issue.get("severity") == "non-breaking"
    assert issue.name == "eta"
    assert str(issue) == "eta added"


def test_compatibility_report_is_sequence():
    report = CompatibilityReport()

    assert len(report) == 0
    assert list(report) == []
    assert report.to_dict()["compatible"] is True

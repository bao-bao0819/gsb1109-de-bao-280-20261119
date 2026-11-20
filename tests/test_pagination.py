"""Tests for pagination parameter resolution and cursor simulation."""

from __future__ import annotations

import pytest

from apispec import APISpec
from apispec.pagination import (
    ParameterConflictError,
    parameter_checklist,
    simulate_pagination,
)

PATH = "/stations/{station_id}/records"


def _build_spec() -> APISpec:
    spec = APISpec(
        title="Pagination Test",
        version="1.0.0",
        openapi_version="3.0.0",
    )
    spec.components.parameter(
        "BackfillRegionQuery",
        "query",
        {
            "name": "region",
            "schema": {"type": "string", "default": "cn-backfill"},
        },
    )
    spec.components.parameter(
        "AuditRegionHeader",
        "header",
        {
            "name": "region",
            "schema": {"type": "string", "default": "audit/backfill"},
        },
    )

    # Common path: shared query region default + link-driven cursor.
    spec.path(
        path=PATH,
        parameters=[
            {
                "name": "station_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            },
            {
                "name": "region",
                "in": "query",
                "schema": {"type": "string", "default": "cn-east"},
            },
            {
                "name": "cursor",
                "in": "query",
                "schema": {"type": "string"},
            },
        ],
    )

    # Regular operation.
    spec.path(
        path=PATH,
        operations={
            "get": {
                "operationId": "listStationRecords",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "default": 50},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
    )

    # Backfill operation using component references.
    spec.path(
        path=PATH,
        operations={
            "post": {
                "operationId": "backfillStationRecords",
                "parameters": ["BackfillRegionQuery", "AuditRegionHeader"],
                "responses": {"202": {"description": "accepted"}},
            }
        },
    )

    # Modify backfill parameters again with inline definitions.
    spec.path(
        path=PATH,
        operations={
            "post": {
                "operationId": "backfillStationRecords",
                "parameters": [
                    {
                        "name": "region",
                        "in": "query",
                        "schema": {"type": "string", "default": "cn-backfill-v2"},
                    },
                    {
                        "name": "region",
                        "in": "header",
                        "schema": {"type": "string", "default": "audit/backfill-v2"},
                    },
                ],
                "responses": {"202": {"description": "accepted"}},
            }
        },
    )
    return spec


@pytest.fixture()
def spec() -> APISpec:
    return _build_spec()


def _by_name(checklist, location, name):
    return next(
        parameter
        for parameter in checklist
        if parameter.location == location and parameter.name == name
    )


class TestParameterChecklist:
    def test_path_level_query_default(self, spec):
        region = _by_name(parameter_checklist(spec, PATH, "get"), "query", "region")
        assert region.default == "cn-east"
        assert region.level == "path"
        assert region.source == "inline"

    def test_required_station_path_parameter(self, spec):
        station = _by_name(parameter_checklist(spec, PATH, "get"), "path", "station_id")
        assert station.required is True

    def test_backfill_overrides_path_region_default(self, spec):
        region = _by_name(parameter_checklist(spec, PATH, "post"), "query", "region")
        assert region.default == "cn-backfill-v2"
        assert region.level == "operation:post"
        assert region.overridden_path_default == "cn-east"

    def test_same_name_header_coexists_with_query(self, spec):
        checklist = parameter_checklist(spec, PATH, "post")
        query_region = _by_name(checklist, "query", "region")
        header_region = _by_name(checklist, "header", "region")
        assert query_region.default == "cn-backfill-v2"
        assert header_region.default == "audit/backfill-v2"

    def test_component_reference_is_recorded_as_source(self, spec):
        # The initial backfill registration used references; the final inline
        # re-registration wins, but a fresh reference-only operation keeps the
        # $ref source visible.
        spec.path(
            path="/ref-only/{station_id}",
            operations={
                "get": {
                    "parameters": ["BackfillRegionQuery"],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        )
        region = _by_name(
            parameter_checklist(spec, "/ref-only/{station_id}", "get"),
            "query",
            "region",
        )
        assert region.source == "$ref:#/components/parameters/BackfillRegionQuery"
        assert region.default == "cn-backfill"

    def test_reregistering_backfill_keeps_regular_scope(self, spec):
        regular = _by_name(parameter_checklist(spec, PATH, "get"), "query", "region")
        assert regular.default == "cn-east"
        assert regular.level == "path"
        limit = _by_name(parameter_checklist(spec, PATH, "get"), "query", "limit")
        assert limit.default == 50

    def test_unknown_path_or_operation_raises(self, spec):
        from apispec.exceptions import APISpecError

        with pytest.raises(APISpecError, match="not registered"):
            parameter_checklist(spec, "/missing", "get")
        with pytest.raises(APISpecError, match="not registered"):
            parameter_checklist(spec, PATH, "delete")


class TestSameLevelConflict:
    def test_inline_vs_component_reference_conflict(self, spec):
        spec.path(
            path="/conflict/{station_id}",
            operations={
                "get": {
                    "parameters": [
                        {
                            "name": "region",
                            "in": "query",
                            "schema": {"type": "string", "default": "inline"},
                        },
                        "BackfillRegionQuery",
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        )
        with pytest.raises(ParameterConflictError) as exc_info:
            parameter_checklist(spec, "/conflict/{station_id}", "get")
        conflict = exc_info.value.conflicts[0]
        assert conflict.level == "operation:get"
        assert conflict.name == "region"
        assert conflict.location == "query"
        assert conflict.sources == [
            "inline",
            "$ref:#/components/parameters/BackfillRegionQuery",
        ]

    def test_path_level_conflict_reports_path_level(self, spec):
        spec.path(
            path="/path-conflict",
            parameters=[
                {"name": "region", "in": "query", "schema": {"type": "string"}},
                "BackfillRegionQuery",
            ],
            operations={"get": {"responses": {"200": {"description": "ok"}}}},
        )
        with pytest.raises(ParameterConflictError) as exc_info:
            parameter_checklist(spec, "/path-conflict", "get")
        assert exc_info.value.conflicts[0].level == "path"

    def test_different_locations_same_name_are_not_conflicts(self, spec):
        checklist = parameter_checklist(spec, PATH, "post")
        names = {(p.location, p.name) for p in checklist}
        assert ("query", "region") in names
        assert ("header", "region") in names


class TestPaginationSimulation:
    def test_request_without_region_uses_effective_defaults(self, spec):
        material = {"path": {"station_id": "station-7"}, "query": {}, "header": {}}
        responses = [
            {"links": {"next": {"cursor": "cursor-a-2"}}},
            {"links": {}},
        ]
        plan = simulate_pagination(spec, PATH, "get", material, responses)

        assert len(plan.pages) == 2
        first_query = {v.name: v for v in plan.pages[0].query}
        assert first_query["region"].value == "cn-east"
        assert first_query["region"].origin == "default"
        assert first_query["limit"].value == 50
        assert "cursor" not in first_query

    def test_link_cursor_only_updates_cursor_and_keeps_station(self, spec):
        material = {"path": {"station_id": "station-7"}, "query": {}, "header": {}}
        responses = [
            {"links": {"next": {"cursor": "cursor-a-2"}}},
            {"links": {}},
        ]
        plan = simulate_pagination(spec, PATH, "get", material, responses)

        second_query = {v.name: v for v in plan.pages[1].query}
        assert second_query["cursor"].value == "cursor-a-2"
        assert second_query["cursor"].origin == "link-cursor"
        assert second_query["region"].value == "cn-east"
        assert all(page.path == "/stations/station-7/records" for page in plan.pages)

    def test_explicit_query_wins_and_header_does_not_override_query(self, spec):
        material = {
            "path": {"station_id": "station-7"},
            "query": {"region": "cn-explicit"},
            "header": {"region": "audit/ticket-42"},
        }
        responses = [
            {"links": {"next": {"cursor": "cursor-b-2"}}},
            {"links": {"next": {"cursor": "cursor-b-3"}}},
        ]
        plan = simulate_pagination(spec, PATH, "post", material, responses)

        for page in plan.pages:
            query_region = next(v for v in page.query if v.name == "region")
            header_region = next(v for v in page.headers if v.name == "region")
            assert query_region.value == "cn-explicit"
            assert query_region.origin == "explicit"
            assert header_region.value == "audit/ticket-42"
            assert header_region.location == "header"

    def test_backfill_defaults_apply_when_input_omits_region(self, spec):
        material = {"path": {"station_id": "station-7"}, "query": {}, "header": {}}
        responses = [{"links": {}}]
        plan = simulate_pagination(spec, PATH, "post", material, responses)
        query_region = next(v for v in plan.pages[0].query if v.name == "region")
        assert query_region.value == "cn-backfill-v2"
        header_region = next(v for v in plan.pages[0].headers if v.name == "region")
        assert header_region.value == "audit/backfill-v2"

    def test_stop_when_no_next_cursor(self, spec):
        material = {"path": {"station_id": "station-7"}, "query": {}, "header": {}}
        responses = [{"links": {}}]
        plan = simulate_pagination(spec, PATH, "get", material, responses)

        assert len(plan.pages) == 1
        assert "cursor" in plan.stop_reason
        assert plan.pages[0].stop_reason == plan.stop_reason

    def test_stop_when_link_next_has_null_cursor(self, spec):
        material = {"path": {"station_id": "station-7"}, "query": {}, "header": {}}
        responses = [{"links": {"next": {"cursor": None}}}]
        plan = simulate_pagination(spec, PATH, "get", material, responses)
        assert len(plan.pages) == 1
        assert "cursor" in plan.stop_reason

    def test_stop_when_required_station_missing(self, spec):
        material = {"path": {}, "query": {}, "header": {}}
        plan = simulate_pagination(
            spec, PATH, "get", material, [{"links": {"next": {"cursor": "x"}}}]
        )
        assert plan.pages == []
        assert "station_id" in plan.stop_reason
        assert "停止" in plan.stop_reason

    def test_max_pages_limit_reports_remaining_cursor(self, spec):
        material = {
            "path": {"station_id": "station-7"},
            "query": {"region": "cn-explicit"},
            "header": {},
        }
        responses = [
            {"links": {"next": {"cursor": "cursor-b-2"}}},
            {"links": {"next": {"cursor": "cursor-b-3"}}},
        ]
        plan = simulate_pagination(spec, PATH, "post", material, responses, max_pages=2)
        assert len(plan.pages) == 2
        assert "cursor-b-3" in plan.stop_reason

    def test_each_request_follows_its_own_link_chain(self, spec):
        responses_a = [
            {"links": {"next": {"cursor": "cursor-a-2"}}},
            {"links": {}},
        ]
        responses_b = [
            {"links": {"next": {"cursor": "cursor-b-2"}}},
            {"links": {"next": {"cursor": "cursor-b-3"}}},
        ]
        plan_a = simulate_pagination(
            spec,
            PATH,
            "get",
            {"path": {"station_id": "station-7"}, "query": {}, "header": {}},
            responses_a,
        )
        plan_b = simulate_pagination(
            spec,
            PATH,
            "post",
            {
                "path": {"station_id": "station-7"},
                "query": {"region": "cn-explicit"},
                "header": {"region": "audit/ticket-42"},
            },
            responses_b,
        )
        cursor_a = next(v for v in plan_a.pages[1].query if v.name == "cursor")
        cursor_b = next(v for v in plan_b.pages[1].query if v.name == "cursor")
        assert cursor_a.value == "cursor-a-2"
        assert cursor_b.value == "cursor-b-2"

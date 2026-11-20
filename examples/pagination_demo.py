"""Demo: verifiable query defaults and cursor pagination checks.

Run with::

    python examples/pagination_demo.py

The script only builds an APISpec and simulates requests from the generated
description. It never performs any HTTP call.
"""

from __future__ import annotations

from apispec import APISpec
from apispec.pagination import (
    ParameterConflictError,
    parameter_checklist,
    simulate_pagination,
)

PATH = "/stations/{station_id}/records"
METHOD_REGULAR = "get"
METHOD_BACKFILL = "post"


def build_spec() -> APISpec:
    spec = APISpec(
        title="Collector Pagination Demo",
        version="1.0.0",
        openapi_version="3.0.0",
    )

    # Reusable component definitions. One registration passes parameters as
    # component references instead of inline objects.
    spec.components.parameter(
        "BackfillRegionQuery",
        "query",
        {
            "name": "region",
            "description": "Region override used by backfill operations",
            "schema": {"type": "string", "default": "cn-backfill"},
        },
    )
    spec.components.parameter(
        "AuditRegionHeader",
        "header",
        {
            "name": "region",
            "description": "Audit source, shares the name 'region' with a query param",
            "schema": {"type": "string", "default": "audit/backfill"},
        },
    )

    # 1. Register the common path. region is a path-level query parameter with
    #    the default region; cursor is link-driven and has no default.
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
                "description": "Default region for every operation on this path",
                "schema": {"type": "string", "default": "cn-east"},
            },
            {
                "name": "cursor",
                "in": "query",
                "description": "Next-page cursor supplied by the previous response link",
                "schema": {"type": "string"},
            },
        ],
    )

    # 2. Register the regular summary-to-detail operation.
    spec.path(
        path=PATH,
        operations={
            METHOD_REGULAR: {
                "operationId": "listStationRecords",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "default": 50},
                    }
                ],
                "responses": {"200": {"description": "Record page"}},
            }
        },
    )

    # 3. Register the backfill operation. The query region is a component
    #    reference that overrides the path-level default for this operation,
    #    and a same-name header carries the audit source.
    spec.path(
        path=PATH,
        operations={
            METHOD_BACKFILL: {
                "operationId": "backfillStationRecords",
                "parameters": [
                    "BackfillRegionQuery",
                    "AuditRegionHeader",
                ],
                "responses": {"202": {"description": "Backfill accepted"}},
            }
        },
    )

    # 4. Modify the backfill parameters again: re-register with an inline
    #    region query (new default) and an adjusted audit header. Re-registering
    #    the regular operation is intentionally not part of this step.
    spec.path(
        path=PATH,
        operations={
            METHOD_BACKFILL: {
                "operationId": "backfillStationRecords",
                "parameters": [
                    {
                        "name": "region",
                        "in": "query",
                        "description": "Revised backfill region override",
                        "schema": {"type": "string", "default": "cn-backfill-v2"},
                    },
                    {
                        "name": "region",
                        "in": "header",
                        "description": "Revised audit source header",
                        "schema": {"type": "string", "default": "audit/backfill-v2"},
                    },
                ],
                "responses": {"202": {"description": "Backfill accepted"}},
            }
        },
    )

    return spec


def print_checklist(spec: APISpec, method: str, title: str) -> None:
    print(f"\n=== 参数清单：{title} ({method.upper()} {PATH}) ===")
    for parameter in parameter_checklist(spec, PATH, method):
        override = (
            f"，覆盖路径级默认 {parameter.overridden_path_default!r}"
            if parameter.overridden_path_default is not None
            else ""
        )
        print(
            f"- {parameter.location:6s} {parameter.name:10s} "
            f"required={parameter.required!s:5s} default={parameter.default!r:18s} "
            f"level={parameter.level} source={parameter.source}{override}"
        )


def print_plan(plan, title: str) -> None:
    print(f"\n=== 分页核对：{title} ===")
    for page in plan.pages:
        print(f"-- 第 {page.page} 页 {page.method.upper()} {page.path}")
        if page.query:
            print("   query:")
            for value in page.query:
                note = f" ({value.note})" if value.note else ""
                print(f"     {value.name}={value.value!r} [{value.origin}]{note}")
        if page.headers:
            print("   header:")
            for value in page.headers:
                note = f" ({value.note})" if value.note else ""
                print(f"     {value.name}={value.value!r} [{value.origin}]{note}")
    print(f"   {plan.stop_reason}")


def demo_same_level_conflict(spec: APISpec) -> None:
    print("\n=== 同层冲突检测（内联参数与组件引用同名同 in）===")
    spec.path(
        path="/conflict/{station_id}",
        parameters=[
            {
                "name": "station_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
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
    try:
        parameter_checklist(spec, "/conflict/{station_id}", "get")
    except ParameterConflictError as error:
        for conflict in error.conflicts:
            print(
                f"- 冲突层级={conflict.level} name={conflict.name!r} "
                f"in={conflict.location!r} sources={conflict.sources}"
            )


def main() -> None:
    spec = build_spec()

    print_checklist(spec, METHOD_REGULAR, "常规操作")
    print_checklist(spec, METHOD_BACKFILL, "补采操作")

    # Request A: no explicit region. The effective query default must come
    # from the regular operation / path-level definitions.
    request_a = {
        "path": {"station_id": "station-7"},
        "query": {},
        "header": {},
    }
    responses_a = [
        {"links": {"next": {"cursor": "cursor-a-2"}}},
        {"links": {}},
    ]
    plan_a = simulate_pagination(
        spec, PATH, METHOD_REGULAR, request_a, responses_a, max_pages=2
    )
    print_plan(plan_a, "请求 A（不显式传 region，常规操作）")

    # Request B: explicit query region plus the same-name header.
    request_b = {
        "path": {"station_id": "station-7"},
        "query": {"region": "cn-explicit"},
        "header": {"region": "audit/ticket-42"},
    }
    responses_b = [
        {"links": {"next": {"cursor": "cursor-b-2"}}},
        {"links": {"next": {"cursor": "cursor-b-3"}}},
    ]
    plan_b = simulate_pagination(
        spec, PATH, METHOD_BACKFILL, request_b, responses_b, max_pages=2
    )
    print_plan(plan_b, "请求 B（显式 query region + 同名 header，补采操作）")

    # Stop condition 1: required path parameter missing.
    plan_missing = simulate_pagination(
        spec,
        PATH,
        METHOD_REGULAR,
        {"path": {}, "query": {}, "header": {}},
        responses_a,
        max_pages=2,
    )
    print("\n=== 停止依据：缺少必填站点 ===")
    print(f"- pages={len(plan_missing.pages)} {plan_missing.stop_reason}")

    demo_same_level_conflict(spec)


if __name__ == "__main__":
    main()

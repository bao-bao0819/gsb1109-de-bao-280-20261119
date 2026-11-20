"""Derive verifiable parameter defaults and cursor pagination checks.

This module never performs HTTP calls. It reads the specification produced by
:class:`apispec.APISpec` and simulates what a collector would actually send:

* path-level and operation-level parameters are merged, with operation-level
  definitions overriding path-level definitions on the same ``name``/``in``;
* explicit request input takes precedence over the parameter schema default;
* a header parameter never overrides a query parameter, even when they share
  the same name;
* the next-page cursor is taken from the previous page response link and only
  replaces the cursor query parameter; required path parameters stay in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .exceptions import APISpecError

CURSOR_PARAM = "cursor"
NEXT_LINK_NAME = "next"

_PATH_PLACEHOLDER_RE = re.compile(r"\{([^}]+)\}")
_REF_PREFIX = "#/"


class ParameterConflictError(APISpecError):
    """Raised when one parameter scope defines the same name/in twice."""

    def __init__(self, conflicts: list[ParameterConflict]) -> None:
        self.conflicts = conflicts
        messages = [
            f"conflicting parameters at {conflict.level}: "
            f"name={conflict.name!r} in={conflict.location!r} "
            f"sources={conflict.sources}"
            for conflict in conflicts
        ]
        super().__init__("; ".join(messages))


@dataclass
class ParameterConflict:
    """Describes two parameters sharing name/in inside the same scope."""

    level: str
    name: str
    location: str
    sources: list[str]


@dataclass
class ResolvedParameter:
    """The single effective definition of a parameter for one operation."""

    name: str
    location: str
    required: bool
    default: Any
    level: str
    source: str
    overridden_path_default: Any = None


@dataclass
class EffectiveValue:
    """The value that would actually be sent for one parameter on one page."""

    name: str
    location: str
    value: Any
    origin: str
    note: str = ""


@dataclass
class PageCheck:
    """One simulated request page, fully resolved and ready to verify."""

    page: int
    method: str
    path: str
    query: list[EffectiveValue] = field(default_factory=list)
    headers: list[EffectiveValue] = field(default_factory=list)
    cursor: Any = None
    cursor_origin: str | None = None
    stop_reason: str | None = None


@dataclass
class PaginationPlan:
    """Parameter checklist plus the simulated page sequence."""

    path_template: str
    method: str
    checklist: list[ResolvedParameter]
    pages: list[PageCheck] = field(default_factory=list)
    stop_reason: str | None = None


def _deref(spec_dict: dict, ref: str) -> dict:
    """Resolve a local JSON pointer such as ``#/components/parameters/Foo``."""
    if not ref.startswith(_REF_PREFIX):
        raise APISpecError(f"Only local references are supported, got: {ref}")
    target: Any = spec_dict
    for part in ref[len(_REF_PREFIX) :].split("/"):
        if not isinstance(target, dict) or part not in target:
            raise APISpecError(f"Reference does not resolve: {ref}")
        target = target[part]
    if not isinstance(target, dict):
        raise APISpecError(f"Reference does not point to an object: {ref}")
    return target


def _entry_source(entry: dict) -> str:
    ref = entry.get("$ref")
    return f"$ref:{ref}" if ref else "inline"


def _resolve_entry(spec_dict: dict, entry: dict) -> tuple[str, dict]:
    if "$ref" in entry:
        ref = entry["$ref"]
        return f"$ref:{ref}", _deref(spec_dict, ref)
    return "inline", entry


def _collect_scope(
    spec_dict: dict,
    raw_parameters: list[dict],
    level: str,
) -> list[tuple[str, dict]]:
    """Resolve one parameter scope and flag same-scope name/in duplicates."""
    resolved: list[tuple[str, dict]] = []
    seen: dict[tuple[str, str], list[str]] = {}
    conflicts: list[ParameterConflict] = []
    for entry in raw_parameters:
        source, parameter = _resolve_entry(spec_dict, entry)
        if "name" not in parameter or "in" not in parameter:
            raise APISpecError(f"Parameter at {level} from {source} is missing name/in")
        key = (parameter["name"], parameter["in"])
        seen.setdefault(key, []).append(source)
        resolved.append((source, parameter))
    for (name, location), sources in seen.items():
        if len(sources) > 1:
            conflicts.append(
                ParameterConflict(
                    level=level,
                    name=name,
                    location=location,
                    sources=sources,
                )
            )
    if conflicts:
        raise ParameterConflictError(conflicts)
    return resolved


def _parameter_default(parameter: dict) -> Any:
    schema = parameter.get("schema")
    if isinstance(schema, dict) and "default" in schema:
        return schema["default"]
    return parameter.get("default")


def effective_parameters(
    spec: Any,
    path: str,
    method: str,
) -> list[ResolvedParameter]:
    """Return the effective parameter list for one registered operation.

    Operation-level parameters override path-level parameters sharing the
    same name/in; same-name parameters in different locations (for example a
    query ``region`` and a header ``region``) coexist.
    """
    spec_dict = spec.to_dict()
    paths = spec_dict.get("paths", {})
    if path not in paths:
        raise APISpecError(f"Path is not registered: {path}")
    path_item = paths[path]
    method = method.lower()
    if method not in path_item:
        raise APISpecError(f"Operation {method.upper()} is not registered on {path}")

    path_scope = _collect_scope(
        spec_dict, path_item.get("parameters", []), level="path"
    )
    operation_scope = _collect_scope(
        spec_dict,
        path_item[method].get("parameters", []),
        level=f"operation:{method}",
    )

    effective: dict[tuple[str, str], ResolvedParameter] = {}
    for source, parameter in path_scope:
        key = (parameter["name"], parameter["in"])
        effective[key] = ResolvedParameter(
            name=parameter["name"],
            location=parameter["in"],
            required=bool(parameter.get("required", False)),
            default=_parameter_default(parameter),
            level="path",
            source=source,
        )
    for source, parameter in operation_scope:
        key = (parameter["name"], parameter["in"])
        path_definition = effective.get(key)
        effective[key] = ResolvedParameter(
            name=parameter["name"],
            location=parameter["in"],
            required=bool(parameter.get("required", False)),
            default=_parameter_default(parameter),
            level=f"operation:{method}",
            source=source,
            overridden_path_default=(
                path_definition.default if path_definition is not None else None
            ),
        )
    return list(effective.values())


def parameter_checklist(
    spec: Any,
    path: str,
    method: str,
) -> list[ResolvedParameter]:
    """Public entry point: effective parameters with defaults and sources."""
    return effective_parameters(spec, path, method)


def _fill_path(
    template: str, path_values: dict[str, Any]
) -> tuple[str | None, str | None]:
    placeholders = _PATH_PLACEHOLDER_RE.findall(template)
    missing = [name for name in placeholders if name not in path_values]
    if missing:
        return None, f"缺少必填路径参数: {', '.join(missing)}"
    filled = template
    for name in placeholders:
        filled = filled.replace(f"{{{name}}}", str(path_values[name]))
    return filled, None


def _next_cursor(response: dict | None) -> tuple[Any, bool]:
    """Extract the cursor from a simulated response link.

    Convention: ``response["links"]["next"]["cursor"]``. Returns
    ``(cursor, found)``.
    """
    if not isinstance(response, dict):
        return None, False
    next_link = response.get("links", {}).get(NEXT_LINK_NAME)
    if not isinstance(next_link, dict) or CURSOR_PARAM not in next_link:
        return None, False
    cursor = next_link[CURSOR_PARAM]
    if cursor is None:
        return None, False
    return cursor, True


def simulate_pagination(
    spec: Any,
    path: str,
    method: str,
    material: dict[str, dict[str, Any]],
    simulated_responses: list[dict],
    max_pages: int = 2,
) -> PaginationPlan:
    """Simulate successive pages without sending any request.

    :param material: explicit request input with optional ``path``, ``query``
        and ``header`` maps. Missing query/header values fall back to the
        schema default of the effective parameter definition.
    :param simulated_responses: one response dict per page; the next cursor is
        read from ``links.next.cursor``.
    :param max_pages: maximum number of pages to simulate.
    """
    method = method.lower()
    checklist = effective_parameters(spec, path, method)
    plan = PaginationPlan(path_template=path, method=method, checklist=checklist)

    explicit_path = material.get("path", {})
    explicit_query = material.get("query", {})
    explicit_header = material.get("header", {})

    required_path_params = [
        parameter.name
        for parameter in checklist
        if parameter.location == "path" and parameter.required
    ]
    missing = [name for name in required_path_params if name not in explicit_path]
    if missing:
        plan.stop_reason = (
            f"停止：请求材料缺少必填路径参数 {missing}，"
            "没有已知站点身份，不生成任何后续页"
        )
        return plan

    first_path, fill_error = _fill_path(path, explicit_path)
    if first_path is None or fill_error is not None:
        plan.stop_reason = f"停止：{fill_error}"
        return plan

    query_defs = [p for p in checklist if p.location == "query"]
    header_defs = [p for p in checklist if p.location == "header"]
    cursor: Any = None
    cursor_origin: str | None = None

    for page_index in range(max_pages):
        page = PageCheck(
            page=page_index + 1,
            method=method,
            path=first_path,
            cursor_origin=cursor_origin,
        )

        for parameter in query_defs:
            if parameter.name == CURSOR_PARAM:
                if cursor is None:
                    continue
                page.query.append(
                    EffectiveValue(
                        name=parameter.name,
                        location="query",
                        value=cursor,
                        origin="link-cursor",
                        note=cursor_origin or "",
                    )
                )
                continue
            if parameter.name in explicit_query:
                page.query.append(
                    EffectiveValue(
                        name=parameter.name,
                        location="query",
                        value=explicit_query[parameter.name],
                        origin="explicit",
                    )
                )
            else:
                page.query.append(
                    EffectiveValue(
                        name=parameter.name,
                        location="query",
                        value=parameter.default,
                        origin="default",
                        note=f"来自{parameter.level}定义 {parameter.source}",
                    )
                )

        for parameter in header_defs:
            if parameter.name in explicit_header:
                page.headers.append(
                    EffectiveValue(
                        name=parameter.name,
                        location="header",
                        value=explicit_header[parameter.name],
                        origin="explicit",
                    )
                )
            else:
                page.headers.append(
                    EffectiveValue(
                        name=parameter.name,
                        location="header",
                        value=parameter.default,
                        origin="default",
                        note=f"来自{parameter.level}定义 {parameter.source}",
                    )
                )

        page.cursor = cursor
        plan.pages.append(page)

        response = (
            simulated_responses[page_index]
            if page_index < len(simulated_responses)
            else None
        )
        next_cursor, found = _next_cursor(response)
        if not found:
            page.stop_reason = (
                "停止：本页响应链接未给出下一页 cursor，不生成没有已知 cursor 的后续页"
            )
            plan.stop_reason = page.stop_reason
            return plan

        if page_index == max_pages - 1:
            page.stop_reason = (
                f"停止：已达模拟页数上限 {max_pages} 页；"
                f"链接仍提供下一页 cursor={next_cursor!r}，可继续核对"
            )
            plan.stop_reason = page.stop_reason
            return plan

        cursor = next_cursor
        cursor_origin = f"第 {page.page} 页响应 links.{NEXT_LINK_NAME}.{CURSOR_PARAM}"

    plan.stop_reason = plan.stop_reason or "停止：无更多模拟响应"
    return plan

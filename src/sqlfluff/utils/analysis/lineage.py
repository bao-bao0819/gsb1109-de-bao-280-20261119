"""Result-column level source tracing ("column lineage") for SQL.

This module answers a data-warehouse question: *for each column produced by
the final query, which physical table and column does it come from?*

The implementation deliberately works only from the real parse tree and from
column metadata supplied by the caller.  It never connects to a database and
never invents a table for a wildcard whose columns are not known.

The public entry point is :func:`trace_column_lineage` (also exported as
``get_column_lineage`` / ``column_lineage`` / ``analyze_column_lineage`` for
convenience).  The result is an ordered list of records, one per actual output
position.  Duplicate output names are kept as separate records; they are never
collapsed into a name-keyed mapping.

Scope rules implemented here
----------------------------

* CTE names are resolved in the nearest enclosing ``WITH`` scope, and a CTE
  column list (``WITH c (x, y) AS ...``) renames outputs by position.
* The same table alias may be used in an inner and an outer query without the
  two leaking into each other: every ``SELECT`` owns its own FROM bindings.
* A qualified wildcard (``t.*``) expands in the exact column order supplied by
  caller metadata (or the exact output order of a CTE/subquery).  An
  unqualified wildcard expands the FROM sources in order.
* A wildcard with no metadata stays unresolved.  No table or column is guessed
  for it, and later concrete outputs report an unknown global ordinal instead
  of a fabricated one.
* ``UNION``/``UNION ALL`` combines branches strictly by output position.  A
  branch's column alias is never used to pair columns with another branch.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, cast

from sqlfluff.core import FluffConfig, Linter
from sqlfluff.core.dialects.base import Dialect
from sqlfluff.core.dialects.common import get_from_clause_aliases, iter_raw_references
from sqlfluff.core.parser import BaseSegment

# ---------------------------------------------------------------------------
# Public result helpers
# ---------------------------------------------------------------------------

#: Status values used on output records.
STATUS_RESOLVED = "resolved"
STATUS_MULTIPLE = "multiple"
STATUS_PARTIAL = "partial"
STATUS_UNKNOWN = "unknown"


#: A result record is a plain ``dict`` so it is immediately JSON serialisable,
#: while also offering attribute access for callers that prefer it.
class ColumnRecord(dict[str, Any]):
    """A single result-column record (a ``dict`` with attribute access)."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - defensive sugar
            raise AttributeError(item) from exc


class LineageResult(list[ColumnRecord]):
    """Ordered result columns.

    ``LineageResult`` is a list (so existing list-oriented callers and JSON
    encoders work naturally) with a few convenience aliases.  Iteration order
    is the real output order: a wildcard which could not be expanded appears
    as one marker record at the position where its range starts.
    """

    @property
    def columns(self) -> "LineageResult":
        """Alias for the result itself."""
        return self

    def as_dict(self) -> list[dict[str, Any]]:
        """Return the result as plain dictionaries."""
        return [dict(record) for record in self]

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        """Return a JSON-friendly object with a ``columns`` key."""
        return {"columns": self.as_dict()}

    @property
    def unresolved(self) -> list[ColumnRecord]:
        """Records which contain at least one unresolved part."""
        return [record for record in self if record["unresolved"]]

    def by_name(self, name: str) -> list[ColumnRecord]:
        """All records with a given display name, in output order.

        This deliberately returns a list: duplicate output names are not
        collapsed, so callers can see every occurrence.
        """
        key = _normalise(name)
        return [
            record
            for record in self
            if record["name"] is not None and _normalise(record["name"]) == key
        ]

    def at(self, ordinal: int) -> Optional[ColumnRecord]:
        """The record at a proven 1-based output position, if any."""
        for record in self:
            if record["position_known"] and record["ordinal"] == ordinal:
                return record
        return None


# ---------------------------------------------------------------------------
# Internal model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    """One intermediate hop between a result column and a physical source."""

    table: Optional[str]
    column: Optional[str]
    kind: str
    #: The name written in this scope (a table alias or a CTE reference).
    alias: Optional[str] = None
    #: Distinguishes identically-named aliases which live in different scopes.
    scope: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "kind": self.kind,
            "alias": self.alias,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class _SourceRef:
    """A proven source column (or the end of a proven intermediate path)."""

    table: Optional[str]
    column: str
    table_alias: Optional[str] = None
    via: tuple[_Step, ...] = ()
    #: True when the value is computed by an expression somewhere in the path.
    computed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "table_alias": self.table_alias,
            "via": [step.as_dict() for step in self.via],
            "computed": self.computed,
        }

    def identity(
        self,
    ) -> tuple[Optional[str], str, Optional[str], tuple[_Step, ...], bool]:
        return (self.table, self.column, self.table_alias, self.via, self.computed)


@dataclass(frozen=True)
class _Unresolved:
    reason: str
    reference: Optional[str] = None
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "reference": self.reference,
            "detail": self.detail,
        }


@dataclass
class _Output:
    """An internal output column, before final position assignment."""

    name: Optional[str]
    sources: list[_SourceRef] = field(default_factory=list)
    unresolved: list[_Unresolved] = field(default_factory=list)
    expression: bool = False
    wildcard: bool = False
    #: True for the placeholder record representing an unexpanded star range.
    wildcard_range: bool = False
    references: list[str] = field(default_factory=list)
    status: str = STATUS_RESOLVED
    #: For a wildcard range, this is the proven start ordinal when known.
    start_position: Optional[int] = None
    position: Optional[int] = None
    position_lower_bound: Optional[int] = None
    position_known: bool = False
    evidence_count: int = 1


class _QueryNode:
    """A query scope: SELECT, set expression, or WITH compound statement."""

    def __init__(
        self,
        kind: str,
        segment: BaseSegment,
        dialect: Dialect,
        metadata: Optional[dict[str, list[str]]] = None,
        wildcard_columns: Optional[dict[str, list[str]]] = None,
        parent: Optional["_QueryNode"] = None,
        scope_label: Optional[str] = None,
    ) -> None:
        self.kind = kind
        self.segment = segment
        self.dialect = dialect
        self.metadata = metadata or {}
        self.wildcard_columns = wildcard_columns or {}
        self.parent = parent
        #: Deterministic identifier, derived from parse-tree order, so evidence
        #: from identically named aliases in different scopes stays distinct
        #: and reproducible.
        self.scope_id = scope_label or _scope_label(segment)
        self.ctes: dict[str, _QueryNode] = {}
        self.branches: list[_QueryNode] = []
        self.sources: list[_SourceBinding] = []
        self.column_aliases: Optional[list[str]] = None
        self._outputs: Optional[list[_Output]] = None
        self._building_sources = False
        self._sources_built = False

    # -- scope ---------------------------------------------------------
    def lookup_cte(self, name: str) -> Optional["_QueryNode"]:
        """Look up a CTE in this scope or an enclosing scope."""
        key = _normalise(name)
        node: Optional[_QueryNode] = self
        while node is not None:
            cte = node.ctes.get(key)
            if cte is not None:
                return cte
            node = node.parent
        return None

    # -- outputs -------------------------------------------------------
    def outputs(self) -> list[_Output]:
        """Return this scope's output columns in real output order."""
        if self._outputs is None:
            if self.kind == "with":
                main = self.branches[0] if self.branches else None
                raw = main.outputs() if main else []
            elif self.kind == "set":
                raw = self._set_outputs()
            elif self.kind == "values":
                raw = self._values_outputs()
            else:
                raw = self._select_outputs()
            if self.column_aliases is not None:
                raw = _apply_column_aliases(raw, self.column_aliases)
            if self.kind == "values":
                _assign_positions(raw)
            self._outputs = raw
        return list(self._outputs or [])

    def _values_outputs(self) -> list[_Output]:
        """Outputs for a VALUES clause (positional, no physical source).

        The number of columns comes from the first row.  Names come only from
        a CTE column list (or a derived-table alias column list); otherwise
        each position is an unnamed unresolved record rather than a guess.
        """
        width = _values_row_width(self.segment)
        names = self.column_aliases
        outputs: list[_Output] = []
        for index in range(width):
            name = names[index] if names and index < len(names) else None
            if name is None:
                outputs.append(
                    _Output(
                        name=None,
                        unresolved=[
                            _Unresolved(
                                "values_column_name_unknown",
                                reference=str(index + 1),
                                detail=(
                                    "VALUES output has no caller-provided "
                                    "column name at this position."
                                ),
                            )
                        ],
                        status=STATUS_UNKNOWN,
                        expression=True,
                    )
                )
            else:
                outputs.append(
                    _Output(name=name, status=STATUS_RESOLVED, expression=True)
                )
        return outputs

    def _select_outputs(self) -> list[_Output]:
        """Resolve every SELECT target, including wildcard expansion."""
        assert self.kind == "select"
        self._ensure_sources()
        select_clause = self.segment.get_child("select_clause")
        if select_clause is None:  # pragma: no cover - malformed parse
            return []
        outputs: list[_Output] = []
        for target in select_clause.get_children("select_clause_element"):
            wildcard = target.get_child("wildcard_expression")
            if wildcard is not None:
                outputs.extend(self._resolve_wildcard(target, wildcard))
            else:
                outputs.append(self._resolve_target(target))
        _assign_positions(outputs)
        return outputs

    def _ensure_sources(self) -> None:
        if self._sources_built or self._building_sources:
            return
        self._building_sources = True
        self._sources_built = True
        from_clause = self.segment.get_child("from_clause")
        if from_clause is None:
            self._building_sources = False
            return
        dialect_name = self.dialect.name
        for element, alias_info in get_from_clause_aliases(from_clause, dialect_name):
            selectable = _selectable_in_element(element)
            if selectable is not None:
                # A derived table: its outputs are the source columns.
                target = _build_query(
                    selectable,
                    self,
                    self.dialect,
                    self.metadata,
                    self.wildcard_columns,
                )
                reference = alias_info.ref_str or _last_name(
                    alias_info.object_reference
                )
                self.sources.append(
                    _SourceBinding(
                        reference=reference or "",
                        table_name=None,
                        table_alias=reference or None,
                        kind="subquery",
                        target=target,
                        element=element,
                        metadata=self.metadata,
                        wildcard_columns=self.wildcard_columns,
                        owner_scope=self.scope_id,
                    )
                )
                continue

            table_ref = alias_info.object_reference
            table_name = _table_name(table_ref, dialect_name)
            reference = alias_info.ref_str or _last_name(table_ref)
            if table_name and not _is_qualified(table_ref, dialect_name):
                cte = self.parent.lookup_cte(table_name) if self.parent else None
                if cte is not None:
                    self.sources.append(
                        _SourceBinding(
                            reference=reference or table_name,
                            table_name=table_name,
                            table_alias=alias_info.ref_str or None,
                            kind="cte",
                            target=cte,
                            element=element,
                            metadata=self.metadata,
                            wildcard_columns=self.wildcard_columns,
                            owner_scope=self.scope_id,
                        )
                    )
                    continue

            self.sources.append(
                _SourceBinding(
                    reference=reference or (table_name or ""),
                    table_name=table_name,
                    table_alias=alias_info.ref_str or None,
                    kind="physical",
                    target=None,
                    element=element,
                    metadata=self.metadata,
                    wildcard_columns=self.wildcard_columns,
                    owner_scope=self.scope_id,
                )
            )
        self._building_sources = False

    def _resolve_target(self, target: BaseSegment) -> _Output:
        alias_info = target.get_alias() if hasattr(target, "get_alias") else None
        name: Optional[str]
        if alias_info is not None:
            name = alias_info.alias_identifier_name
        else:
            direct_ref = target.get_child("column_reference")
            name = _last_name(direct_ref) if direct_ref is not None else None

        refs = list(
            target.recursive_crawl(
                "column_reference", no_recursive_seg_type="select_statement"
            )
        )
        sources: list[_SourceRef] = []
        unresolved: list[_Unresolved] = []
        for ref in refs:
            ref_sources, ref_unresolved = self._resolve_column_reference(ref)
            sources.extend(ref_sources)
            unresolved.extend(ref_unresolved)

        sources = _dedupe_sources(sources)
        status = _combine_status(sources, unresolved)
        # An expression is anything other than a bare column reference (even
        # when it carries an alias, which merely names the result), or a bare
        # reference whose value is computed further upstream in a CTE.
        has_expression_child = any(
            child.is_type(
                "expression",
                "binary_operator",
                "function",
                "cast_expression",
                "case_expression",
            )
            for child in target.segments
        )
        is_bare_reference = (
            len(refs) == 1
            and target.get_child("column_reference") is not None
            and not has_expression_child
            and not any(source.computed for source in sources)
        )
        if not is_bare_reference and not sources and not unresolved:
            # The output is computed from literals only, so it has no source
            # column.  Say so instead of claiming a resolved source.
            unresolved.append(
                _Unresolved(
                    "expression_without_source_column",
                    reference=target.raw.strip(),
                    detail=(
                        "The expression contains no column reference, so it "
                        "has no source table or column."
                    ),
                )
            )
        if not is_bare_reference:
            # The value of every contributing source is computed here.
            sources = [
                _SourceRef(
                    table=source.table,
                    column=source.column,
                    table_alias=source.table_alias,
                    via=source.via,
                    computed=True,
                )
                for source in sources
            ]
        status = _combine_status(sources, unresolved)
        return _Output(
            name=name,
            sources=sources,
            unresolved=unresolved,
            expression=not is_bare_reference,
            references=[ref.raw.strip() for ref in refs],
            status=status,
        )

    def _resolve_wildcard(
        self, target: BaseSegment, wildcard: BaseSegment
    ) -> list[_Output]:
        identifier = wildcard.get_child("wildcard_identifier")
        qualifier: Optional[str] = None
        if identifier is not None:
            text = identifier.raw.strip()
            if text != "*":
                qualifier = text.rsplit(".", 1)[0].strip()
                qualifier = _strip_quotes(qualifier)

        if qualifier is not None:
            binding = self._find_source(qualifier)
            if binding is None:
                return [
                    _Output(
                        name=f"{qualifier}.*",
                        unresolved=[
                            _Unresolved(
                                "unresolved_table_reference",
                                reference=qualifier,
                                detail=(
                                    "Qualified wildcard does not match a FROM "
                                    "source in this scope."
                                ),
                            )
                        ],
                        wildcard=True,
                        wildcard_range=True,
                        status=STATUS_UNKNOWN,
                        references=[target.raw.strip()],
                    )
                ]
            return binding.expand_star(target)

        if not self.sources:
            return [
                _Output(
                    name="*",
                    unresolved=[
                        _Unresolved(
                            "no_from_sources",
                            reference="*",
                            detail="Unqualified wildcard has no FROM source.",
                        )
                    ],
                    wildcard=True,
                    wildcard_range=True,
                    status=STATUS_UNKNOWN,
                    references=[target.raw.strip()],
                )
            ]

        outputs: list[_Output] = []
        for binding in self.sources:
            outputs.extend(binding.expand_star(target))
        return outputs

    def _find_source(self, qualifier: str) -> Optional["_SourceBinding"]:
        key = _normalise(qualifier)
        matches = [
            source
            for source in self.sources
            if _normalise(source.reference) == key
            or (
                source.table_name is not None
                and (
                    _normalise(source.table_name) == key
                    or _normalise(_last_name_text(source.table_name)) == key
                )
            )
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Duplicate aliases in one FROM are invalid SQL, but keep the first
            # deterministic binding rather than leaking into another scope.
            return matches[0]
        return None

    def _resolve_column_reference(
        self, ref: BaseSegment
    ) -> tuple[list[_SourceRef], list[_Unresolved]]:
        parts = [
            part.part
            for part in iter_raw_references(ref, self.dialect.name)
            if part.part
        ]
        ref_text = ref.raw.strip()
        if not parts:
            return [], [_Unresolved("empty_column_reference", reference=ref_text)]

        if len(parts) >= 2:
            column = parts[-1]
            qualifiers = [parts[-2], ".".join(parts[:-1])]
            for qualifier in qualifiers:
                binding = self._find_source(qualifier)
                if binding is not None:
                    return binding.resolve_column(column, ref_text)
            return [], [
                _Unresolved(
                    "unresolved_table_reference",
                    reference=ref_text,
                    detail=(
                        "Qualified column reference does not match a FROM "
                        "source in this scope."
                    ),
                )
            ]

        column = parts[0]
        definite: list[tuple[_SourceBinding, list[_SourceRef], list[_Unresolved]]] = []
        possible: list[_SourceBinding] = []
        for binding in self.sources:
            resolved, unresolved_parts = binding.resolve_column(column, ref_text)
            if resolved:
                definite.append((binding, resolved, unresolved_parts))
            elif not unresolved_parts:
                # The source explicitly has no such column; it is ruled out.
                continue
            elif binding.could_contain_column(column):
                possible.append(binding)

        if definite and possible:
            # Some sources prove the column, but others might also own it and
            # their metadata is incomplete.  Keep the proven sources while
            # recording that the answer may not be exclusive.
            sources: list[_SourceRef] = []
            unresolved: list[_Unresolved] = []
            for _, resolved, unresolved_parts in definite:
                sources.extend(resolved)
                unresolved.extend(unresolved_parts)
            for binding in possible:
                unresolved.append(
                    _Unresolved(
                        "ambiguous_unqualified_column",
                        reference=ref_text,
                        detail=(
                            f"Column {column!r} is proven on another source, "
                            f"but {binding.reference!r} may also own it and "
                            f"cannot be ruled out from the supplied metadata."
                        ),
                    )
                )
            return _dedupe_sources(sources), _dedupe_unresolved(unresolved)
        if len(definite) == 1:
            return definite[0][1], definite[0][2]
        if len(definite) > 1:
            # Several FROM sources own this name.  Report all of them as
            # evidence, without arbitrarily choosing one.
            sources = []
            unresolved = []
            for _, resolved, unresolved_parts in definite:
                sources.extend(resolved)
                unresolved.extend(unresolved_parts)
            sources = _dedupe_sources(sources)
            return sources, unresolved
        if possible:
            return [], [
                _Unresolved(
                    "column_may_be_in_unexpanded_wildcard",
                    reference=ref_text,
                    detail=(
                        "An unqualified column could belong to a source whose "
                        "columns are not known; no table is guessed."
                    ),
                )
            ]
        return [], [
            _Unresolved(
                "unqualified_column_not_resolved",
                reference=ref_text,
                detail="No FROM source has proven metadata for this column.",
            )
        ]

    # -- set expressions ----------------------------------------------
    def _set_outputs(self) -> list[_Output]:
        branch_outputs = [branch.outputs() for branch in self.branches]
        if not branch_outputs:
            return []
        if any(_has_unexpanded_range(outputs) for outputs in branch_outputs):
            return _combine_set_with_unknown(branch_outputs)
        return _combine_set_by_position(branch_outputs)


@dataclass
class _SourceBinding:
    """A FROM/JOIN source visible in one SELECT scope."""

    reference: str
    table_name: Optional[str]
    table_alias: Optional[str]
    kind: str
    target: Optional[_QueryNode]
    element: BaseSegment
    metadata: dict[str, list[str]] = field(default_factory=dict)
    #: Caller-supplied column order for otherwise unknown wildcard sources,
    #: keyed by table name, alias, or ``alias.*``.
    wildcard_columns: dict[str, list[str]] = field(default_factory=dict)
    #: Label of the SELECT scope in which this source is written.  Two scopes
    #: may both say ``s``; this keeps their evidence distinguishable.
    owner_scope: Optional[str] = None

    # ------------------------------------------------------------------
    def resolve_column(
        self, column: str, reference_text: Optional[str] = None
    ) -> tuple[list[_SourceRef], list[_Unresolved]]:
        """Resolve a column name through this binding to physical sources."""
        reference = reference_text or f"{self.reference}.{column}"
        if self.kind == "physical":
            metadata = self._physical_columns()
            if metadata is None:
                return [], [
                    _Unresolved(
                        "source_column_metadata_missing",
                        reference=reference,
                        detail=(
                            f"No caller-provided column metadata for physical "
                            f"table {self.table_name!r}; no column is guessed."
                        ),
                    )
                ]
            matched = [
                name for name in metadata if _normalise(name) == _normalise(column)
            ]
            if not matched:
                return [], [
                    _Unresolved(
                        "column_not_in_source_metadata",
                        reference=reference,
                        detail=(
                            f"Column {column!r} is not present in the supplied "
                            f"metadata for {self.table_name!r}."
                        ),
                    )
                ]
            return [
                _SourceRef(
                    table=self.table_name,
                    column=name,
                    table_alias=self.table_alias,
                )
                for name in matched
            ], []

        if self.target is None:  # pragma: no cover - defensive
            return [], [_Unresolved("unresolved_source", reference=reference)]

        outputs = self.target.outputs()
        matched_outputs = [
            output
            for output in outputs
            if output.name and _normalise(output.name) == _normalise(column)
        ]
        if matched_outputs:
            sources: list[_SourceRef] = []
            unresolved: list[_Unresolved] = []
            for output in matched_outputs:
                via_step = _Step(
                    table=self.reference or self.table_name,
                    column=output.name,
                    kind=self.kind,
                    alias=self.reference,
                    scope=self.owner_scope,
                )
                sources.extend(
                    _prepend_step(
                        _SourceRef(
                            table=source.table,
                            column=source.column,
                            table_alias=source.table_alias,
                            via=source.via,
                            computed=source.computed or output.expression,
                        ),
                        via_step,
                    )
                    for source in output.sources
                )
                # A matched CTE output may itself have unknown sources (e.g. it
                # was defined from an unexpanded star).  Propagate that fact.
                unresolved.extend(output.unresolved)
            return _dedupe_sources(sources), _dedupe_unresolved(unresolved)

        if any(output.wildcard_range and not output.sources for output in outputs):
            return [], [
                _Unresolved(
                    "column_may_be_in_unexpanded_wildcard",
                    reference=reference,
                    detail=(
                        f"{self.reference!r} contains an unexpanded wildcard, so "
                        f"column {column!r} cannot be proven."
                    ),
                )
            ]
        return [], [
            _Unresolved(
                "column_not_in_cte_output",
                reference=reference,
                detail=(
                    f"Column {column!r} is not an output of {self.reference!r} "
                    f"in this scope."
                ),
            )
        ]

    def could_contain_column(self, column: str) -> bool:
        """Whether this binding *could* own a column, without proving it."""
        if self.kind == "physical":
            return self._physical_columns() is None
        if self.target is None:  # pragma: no cover - defensive
            return True
        outputs = self.target.outputs()
        if any(
            output.name and _normalise(output.name) == _normalise(column)
            for output in outputs
        ):
            return True
        return any(output.wildcard_range for output in outputs)

    def expand_star(self, target_segment: BaseSegment) -> list[_Output]:
        """Expand one ``*`` occurrence for this binding, in exact order."""
        reference = target_segment.raw.strip()
        if self.kind == "physical":
            metadata = self._physical_columns()
            if metadata is None:
                return [
                    _Output(
                        name=f"{self.reference}.*" if self.reference else "*",
                        unresolved=[
                            _Unresolved(
                                "wildcard_not_expanded",
                                reference=reference,
                                detail=(
                                    f"No caller-provided column metadata for "
                                    f"physical table {self.table_name!r}; the "
                                    f"wildcard range is left unresolved."
                                ),
                            )
                        ],
                        wildcard=True,
                        wildcard_range=True,
                        status=STATUS_UNKNOWN,
                        references=[reference],
                    )
                ]
            return [
                _Output(
                    name=column,
                    sources=[
                        _SourceRef(
                            table=self.table_name,
                            column=column,
                            table_alias=self.table_alias,
                        )
                    ],
                    wildcard=True,
                    references=[reference],
                    status=STATUS_RESOLVED,
                )
                for column in metadata
            ]

        if self.target is None:  # pragma: no cover - defensive
            return [
                _Output(
                    name=f"{self.reference}.*" if self.reference else "*",
                    unresolved=[_Unresolved("unresolved_source", reference=reference)],
                    wildcard=True,
                    wildcard_range=True,
                    status=STATUS_UNKNOWN,
                    references=[reference],
                )
            ]

        outputs: list[_Output] = []
        for output in self.target.outputs():
            via_step = _Step(
                table=self.reference or self.table_name,
                column=output.name,
                kind=self.kind,
                alias=self.table_alias or self.reference,
                scope=self.owner_scope,
            )
            if output.wildcard_range and not output.sources:
                outputs.append(
                    _Output(
                        name=output.name,
                        unresolved=[
                            _Unresolved(
                                "wildcard_range_propagated",
                                reference=reference,
                                detail=(
                                    f"{self.reference!r} contains an unexpanded "
                                    f"wildcard range."
                                ),
                            )
                        ],
                        wildcard=True,
                        wildcard_range=True,
                        status=STATUS_UNKNOWN,
                        references=[reference],
                    )
                )
                continue
            outputs.append(
                _Output(
                    name=output.name,
                    sources=[
                        _prepend_step(
                            _SourceRef(
                                table=source.table,
                                column=source.column,
                                table_alias=source.table_alias,
                                via=source.via,
                                computed=source.computed or output.expression,
                            ),
                            via_step,
                        )
                        for source in output.sources
                    ],
                    unresolved=list(output.unresolved),
                    expression=output.expression,
                    wildcard=True,
                    references=[reference],
                    status=output.status,
                )
            )
        return outputs

    def _physical_columns(self) -> Optional[list[str]]:
        """Return the caller-supplied ordered columns for this physical table."""
        if self.table_name is None:
            return None
        keys = [
            _normalise(self.table_name),
            _normalise(_last_name_text(self.table_name)),
        ]
        for key in keys:
            if key in self.metadata:
                return self.metadata[key]
        # A caller may supplement an unknown wildcard's complete column order
        # separately (keyed by table name, alias, or ``alias.*``).  This is the
        # supported route to re-trace a previously unresolved star.
        wildcard_keys = [
            _normalise(self.reference),
            _normalise(f"{self.reference}.*"),
            _normalise(self.table_name),
        ]
        for key in wildcard_keys:
            if key in self.wildcard_columns:
                return self.wildcard_columns[key]
        return None


# ---------------------------------------------------------------------------
# Tree construction
# ---------------------------------------------------------------------------

_SELECTABLE_TYPES = (
    "select_statement",
    "set_expression",
    "with_compound_statement",
    "values_clause",
)


def _build_query(
    segment: BaseSegment,
    parent: Optional[_QueryNode],
    dialect: Dialect,
    metadata: dict[str, list[str]],
    wildcard_columns: Optional[dict[str, list[str]]] = None,
) -> _QueryNode:
    """Recursively build scopes from the real parse tree."""
    if segment.is_type("with_compound_statement"):
        node = _QueryNode(
            "with",
            segment,
            dialect,
            metadata=metadata,
            wildcard_columns=wildcard_columns,
            parent=parent,
        )
        # CTE definitions are direct children.  Build them in order so a CTE
        # can see the CTEs defined before it, and so name resolution never
        # accidentally reaches into a nested WITH.
        cte_segments = segment.get_children("common_table_expression")
        for cte in cte_segments:
            name_seg = cte.segments[0] if cte.segments else None
            if name_seg is None:  # pragma: no cover - malformed parse
                continue
            name = _strip_quotes(name_seg.raw)
            inner = next(
                (
                    seg
                    for seg in cte.recursive_crawl(*_SELECTABLE_TYPES)
                    if seg is not cte
                ),
                None,
            )
            if inner is None:  # pragma: no cover - unusual CTE body
                continue
            cte_node = _build_query(inner, node, dialect, metadata, wildcard_columns)
            cte_node.column_aliases = _cte_column_aliases(cte)
            node.ctes[_normalise(name)] = cte_node
        for main in segment.get_children(
            "select_statement", "set_expression", "values_clause"
        ):
            node.branches.append(
                _build_query(main, node, dialect, metadata, wildcard_columns)
            )
        if not node.branches:  # pragma: no cover - unusual parse shape
            for main in segment.recursive_crawl(
                "select_statement", "set_expression", recurse_into=False
            ):
                parent_ref = main.get_parent()
                if parent_ref and parent_ref[0] is segment:
                    node.branches.append(
                        _build_query(main, node, dialect, metadata, wildcard_columns)
                    )
        return node

    if segment.is_type("set_expression"):
        node = _QueryNode(
            "set",
            segment,
            dialect,
            metadata=metadata,
            wildcard_columns=wildcard_columns,
            parent=parent,
        )
        for branch in segment.get_children("select_statement", "set_expression"):
            node.branches.append(
                _build_query(branch, node, dialect, metadata, wildcard_columns)
            )
        if not node.branches:
            # Some dialects bracket each branch, e.g.
            # ``(SELECT ...) UNION ALL (SELECT ...)``.
            for bracketed in segment.get_children("bracketed"):
                inner = next(
                    bracketed.recursive_crawl(*_SELECTABLE_TYPES),
                    None,
                )
                if inner is not None:
                    node.branches.append(
                        _build_query(inner, node, dialect, metadata, wildcard_columns)
                    )
        return node

    if segment.is_type("values_clause"):
        return _QueryNode(
            "values",
            segment,
            dialect,
            metadata=metadata,
            wildcard_columns=wildcard_columns,
            parent=parent,
        )

    assert segment.is_type("select_statement"), f"Unexpected query segment {segment}"
    node = _QueryNode(
        "select",
        segment,
        dialect,
        metadata=metadata,
        wildcard_columns=wildcard_columns,
        parent=parent,
    )
    node._ensure_sources()
    return node


def _values_row_width(segment: BaseSegment) -> int:
    """Number of value columns in the first row of a VALUES clause."""
    row = segment.get_child("bracketed")
    if row is None:
        row = next(segment.recursive_crawl("bracketed"), None)
    if row is None:  # pragma: no cover - malformed parse
        return 1
    values = [
        child
        for child in row.segments
        if not child.is_type(
            "start_bracket",
            "end_bracket",
            "comma",
            "whitespace",
            "newline",
            "indent",
            "dedent",
        )
        and not child.is_meta
    ]
    return max(len(values), 1)


def _cte_column_aliases(cte: BaseSegment) -> Optional[list[str]]:
    column_list = cte.get_child("cte_column_list")
    if column_list is None:
        return None
    names = [
        _strip_quotes(segment.raw)
        for segment in column_list.recursive_crawl("identifier")
    ]
    return names or None


def _selectable_in_element(element: BaseSegment) -> Optional[BaseSegment]:
    """Return the selectable inside a FROM element, if it is a derived table.

    Derived tables may be wrapped in brackets (``FROM (SELECT ...) AS q`` or
    ``FROM (VALUES ...) AS v``), so the whole element is searched.  A plain
    table element contains no selectable and returns ``None``.
    """
    for candidate in element.recursive_crawl(
        "select_statement",
        "set_expression",
        "with_compound_statement",
        "values_clause",
    ):
        return candidate
    return None


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _assign_positions(outputs: list[_Output]) -> None:
    """Assign real ordinals, accounting for unresolved wildcard ranges."""
    known_count = 0
    unknown_started = False
    for output in outputs:
        if output.wildcard_range:
            output.position = None
            output.position_known = False
            output.start_position = None if unknown_started else known_count + 1
            output.position_lower_bound = known_count + 1
            unknown_started = True
            continue
        if unknown_started:
            output.position = None
            output.position_lower_bound = known_count + 1
            output.position_known = False
        else:
            known_count += 1
            output.position = known_count
            output.position_lower_bound = known_count
            output.position_known = True


def _apply_column_aliases(
    outputs: list[_Output], aliases: Sequence[str]
) -> list[_Output]:
    """Rename CTE outputs by position (``WITH c (x, y) AS ...``).

    Aliases are only applied to positions which are proven.  If the CTE body
    contains an unexpanded wildcard range, later output ordinals are unknown,
    so their names are left untouched (and flagged) rather than shifted.
    """
    renamed: list[_Output] = []
    unknown_started = False
    alias_index = 0
    for output in outputs:
        if output.wildcard_range:
            unknown_started = True
        if unknown_started:
            copy = _copy_output(output)
            copy.unresolved.append(
                _Unresolved(
                    "cte_column_alias_alignment_unknown",
                    reference=output.name,
                    detail=(
                        "The CTE body contains an unexpanded wildcard range, so "
                        "positional column aliases cannot be applied here."
                    ),
                )
            )
            copy.status = _combine_status(copy.sources, copy.unresolved)
            renamed.append(copy)
            continue
        if alias_index < len(aliases):
            copy = _copy_output(output)
            copy.name = aliases[alias_index]
            renamed.append(copy)
        else:
            # More outputs than supplied aliases: keep the original name but
            # flag the mismatch rather than dropping the column.
            copy = _copy_output(output)
            copy.unresolved.append(
                _Unresolved(
                    "cte_column_alias_count_mismatch",
                    reference=output.name,
                    detail="CTE column list has fewer names than query outputs.",
                )
            )
            copy.status = _combine_status(copy.sources, copy.unresolved)
            renamed.append(copy)
        alias_index += 1
    if not unknown_started and alias_index < len(aliases):
        for missing_name in aliases[alias_index:]:
            renamed.append(
                _Output(
                    name=missing_name,
                    unresolved=[
                        _Unresolved(
                            "cte_column_alias_count_mismatch",
                            reference=missing_name,
                            detail=(
                                "CTE column list has more names than query "
                                "outputs; this name has no expression."
                            ),
                        )
                    ],
                    status=STATUS_UNKNOWN,
                )
            )
    return renamed


def _copy_output(output: _Output) -> _Output:
    return _Output(
        name=output.name,
        sources=list(output.sources),
        unresolved=list(output.unresolved),
        expression=output.expression,
        wildcard=output.wildcard,
        wildcard_range=output.wildcard_range,
        references=list(output.references),
        status=output.status,
        start_position=output.start_position,
        position=output.position,
        position_lower_bound=output.position_lower_bound,
        position_known=output.position_known,
        evidence_count=output.evidence_count,
    )


def _has_unexpanded_range(outputs: list[_Output]) -> bool:
    return any(output.wildcard_range for output in outputs)


def _first_range_index(outputs: list[_Output]) -> int:
    """Index of the first unexpanded range, or the output count if none."""
    for index, output in enumerate(outputs):
        if output.wildcard_range:
            return index
    return len(outputs)


def _combine_set_by_position(branch_outputs: list[list[_Output]]) -> list[_Output]:
    """Combine set branches strictly by ordinal, ignoring branch aliases."""
    max_length = max(len(outputs) for outputs in branch_outputs)
    combined: list[_Output] = []
    for index in range(max_length):
        present = [outputs[index] for outputs in branch_outputs if index < len(outputs)]
        missing = len(branch_outputs) - len(present)
        name = next((output.name for output in present if output.name), None)
        sources: list[_SourceRef] = []
        unresolved: list[_Unresolved] = []
        references: list[str] = []
        expression = False
        wildcard = False
        for output in present:
            sources.extend(output.sources)
            unresolved.extend(output.unresolved)
            references.extend(output.references)
            expression = expression or output.expression
            wildcard = wildcard or output.wildcard
        if missing:
            unresolved.append(
                _Unresolved(
                    "set_branch_missing_column",
                    reference=str(index + 1),
                    detail=(
                        f"{missing} set branch(es) have no column at this "
                        f"position; alignment is by position."
                    ),
                )
            )
        sources = _dedupe_sources(sources)
        status = _combine_status(sources, unresolved)
        if status == STATUS_RESOLVED and len(present) > 1:
            status = STATUS_MULTIPLE
        combined.append(
            _Output(
                name=name,
                sources=sources,
                unresolved=_dedupe_unresolved(unresolved),
                expression=expression,
                wildcard=wildcard,
                references=references,
                status=status,
                position=index + 1,
                position_lower_bound=index + 1,
                position_known=True,
                evidence_count=len(present),
            )
        )
    return combined


def _combine_set_with_unknown(branch_outputs: list[list[_Output]]) -> list[_Output]:
    """Combine set branches when at least one branch has an unknown star.

    Only output ordinals which precede *every* branch's first unexpanded range
    are provable.  After that the number of columns contributed by the star is
    unknown, so later records deliberately keep ``position=None`` rather than
    pretending branch aliases establish alignment.
    """
    first_unknown = min(
        (_first_range_index(outputs) for outputs in branch_outputs),
        default=0,
    )
    combined: list[_Output] = []
    for index in range(first_unknown):
        present = [outputs[index] for outputs in branch_outputs if index < len(outputs)]
        sources: list[_SourceRef] = []
        unresolved: list[_Unresolved] = []
        for output in present:
            sources.extend(output.sources)
            unresolved.extend(output.unresolved)
        sources = _dedupe_sources(sources)
        status = _combine_status(sources, unresolved)
        if status == STATUS_RESOLVED and len(present) > 1:
            status = STATUS_MULTIPLE
        combined.append(
            _Output(
                name=next((output.name for output in present if output.name), None),
                sources=sources,
                unresolved=_dedupe_unresolved(unresolved),
                references=[
                    reference for output in present for reference in output.references
                ],
                expression=any(output.expression for output in present),
                status=status,
                position=index + 1,
                position_lower_bound=index + 1,
                position_known=True,
                evidence_count=len(present),
            )
        )

    # One marker for the unresolved range.
    references = [
        reference
        for outputs in branch_outputs
        for output in outputs
        if output.wildcard_range
        for reference in output.references
    ]
    combined.append(
        _Output(
            name="*",
            unresolved=[
                _Unresolved(
                    "wildcard_range_in_set_expression",
                    reference="*",
                    detail=(
                        "At least one set branch contains an unexpanded "
                        "wildcard range; later positions cannot be aligned by "
                        "ordinal."
                    ),
                )
            ],
            wildcard=True,
            wildcard_range=True,
            status=STATUS_UNKNOWN,
            references=references,
            start_position=first_unknown + 1,
            position_lower_bound=first_unknown + 1,
        )
    )

    # Explicit outputs which sit after an unresolved range in any branch.  We
    # report their existence and expression but not a fabricated source
    # alignment.
    max_length = max(len(outputs) for outputs in branch_outputs)
    relative_index = first_unknown
    for index in range(first_unknown + 1, max_length):
        present = [outputs[index] for outputs in branch_outputs if index < len(outputs)]
        if not present:
            continue
        relative_index += 1
        combined.append(
            _Output(
                name=next((output.name for output in present if output.name), None),
                unresolved=[
                    _Unresolved(
                        "alignment_unknown_after_wildcard",
                        reference=str(relative_index),
                        detail=(
                            "A prior unexpanded wildcard makes this column's "
                            "global ordinal and branch alignment unprovable."
                        ),
                    )
                ],
                expression=any(output.expression for output in present),
                references=[
                    reference for output in present for reference in output.references
                ],
                status=STATUS_UNKNOWN,
                position_lower_bound=first_unknown + 2,
            )
        )
    return combined


def _combine_status(
    sources: Sequence[_SourceRef], unresolved: Sequence[_Unresolved]
) -> str:
    if unresolved:
        return STATUS_PARTIAL if sources else STATUS_UNKNOWN
    if len(sources) > 1:
        return STATUS_MULTIPLE
    return STATUS_RESOLVED


def _dedupe_sources(sources: Iterable[_SourceRef]) -> list[_SourceRef]:
    seen: set[tuple[Optional[str], str, Optional[str], tuple[_Step, ...], bool]] = set()
    result: list[_SourceRef] = []
    for source in sources:
        key = source.identity()
        if key not in seen:
            seen.add(key)
            result.append(source)
    return result


def _dedupe_unresolved(unresolved: Iterable[_Unresolved]) -> list[_Unresolved]:
    seen: set[tuple[str, Optional[str], Optional[str]]] = set()
    result: list[_Unresolved] = []
    for item in unresolved:
        key = (item.reason, item.reference, item.detail)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _prepend_step(source: _SourceRef, step: _Step) -> _SourceRef:
    return _SourceRef(
        table=source.table,
        column=source.column,
        table_alias=source.table_alias,
        via=(step, *source.via),
        computed=source.computed,
    )


def _output_record(output: _Output) -> ColumnRecord:
    unresolved = [item.as_dict() for item in output.unresolved]
    sources = [source.as_dict() for source in output.sources]
    status = output.status
    if output.wildcard_range:
        status = STATUS_UNKNOWN
    return ColumnRecord(
        {
            "name": output.name,
            "ordinal": output.position,
            "position": output.position,
            "sequence": None,
            "position_known": output.position_known,
            "position_lower_bound": output.position_lower_bound,
            "start_position": output.start_position,
            "status": status,
            "resolved": status in (STATUS_RESOLVED, STATUS_MULTIPLE),
            "expression": output.expression,
            "wildcard": output.wildcard,
            "wildcard_range": output.wildcard_range,
            "sources": sources,
            "source_count": len(sources),
            "evidence_count": output.evidence_count,
            "references": output.references,
            "unresolved": unresolved,
        }
    )


# ---------------------------------------------------------------------------
# Identifier/metadata helpers
# ---------------------------------------------------------------------------


def _normalise(name: str) -> str:
    return _strip_quotes(name).strip().casefold()


def _strip_quotes(name: str) -> str:
    return name.strip().strip('`"[]')


def _scope_label(segment: BaseSegment) -> str:
    """A deterministic label for a query scope.

    Derived from the segment's position in the parse tree rather than from
    ``id()``/``uuid`` so that evidence from two scopes which both alias a table
    as ``s`` stays distinct *and* reproducible across runs.
    """
    try:
        line, pos = segment.get_start_loc()
        return f"{segment.type}@{line}:{pos}"
    except (AssertionError, AttributeError):  # pragma: no cover - malformed tree
        return segment.type


def _table_name(
    segment: Optional[BaseSegment], dialect_name: Optional[str] = None
) -> Optional[str]:
    """The normalised dotted name of a table reference.

    Quoting styles differ by dialect (``"s"."t"``, ``[s].[t]``, ``p.d.t``), so
    the parts are normalised and rejoined.  This is what caller metadata keys
    are matched against.
    """
    if segment is None:
        return None
    parts = [
        _strip_quotes(part.part)
        for part in iter_raw_references(segment, dialect_name)
        if part.part
    ]
    if parts:
        return ".".join(parts)
    return segment.raw.strip() or None


def _last_name_text(name: str) -> str:
    return _strip_quotes(name.rsplit(".", 1)[-1])


def _last_name(segment: Optional[BaseSegment]) -> Optional[str]:
    if segment is None:
        return None
    text = segment.raw.strip()
    if not text:
        return None
    return _last_name_text(text)


def _is_qualified(segment: Optional[BaseSegment], dialect_name: Optional[str]) -> bool:
    if segment is None:
        return False
    return (
        len([part for part in iter_raw_references(segment, dialect_name) if part.part])
        > 1
    )


def _normalise_metadata(
    metadata: Optional[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Normalise caller metadata to ``{normalised table name: [columns]}``.

    Accepted forms include::

        {"schema.table": ["id", "name"]}
        {"table": {"columns": ["id", "name"]}}
        {"table": [{"name": "id"}, {"name": "name"}]}

    Column order is preserved exactly as supplied.
    """
    if not metadata:
        return {}
    normalised: dict[str, list[str]] = {}
    for table, value in metadata.items():
        columns = _metadata_columns(value)
        if columns is None:
            continue
        keys = {
            _normalise(str(table)),
            _normalise(_last_name_text(str(table))),
        }
        for key in keys:
            if key:
                normalised.setdefault(key, columns)
    return normalised


def _metadata_columns(value: Any) -> Optional[list[str]]:
    if isinstance(value, Mapping):
        for key in ("columns", "column_names", "fields"):
            if key in value:
                return _metadata_columns(value[key])
        # A mapping of column name -> type also conveys order.
        if all(isinstance(item, str) for item in value.keys()):
            return [str(item) for item in value.keys()]
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        columns: list[str] = []
        for item in value:
            if isinstance(item, str):
                columns.append(item)
            elif isinstance(item, Mapping):
                name = (
                    item.get("name")
                    or item.get("column")
                    or item.get("column_name")
                    or item.get("field")
                )
                if name is not None:
                    columns.append(str(name))
            else:
                name = getattr(item, "name", None)
                if name is not None:
                    columns.append(str(name))
        return columns
    return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


class ColumnLineageTracer:
    """Trace result columns for a SQL string or an existing parse tree."""

    def __init__(
        self,
        dialect: Optional[str] = None,
        column_metadata: Optional[Mapping[str, Any]] = None,
        config: Optional[FluffConfig] = None,
        wildcard_columns: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.config = config or FluffConfig.from_kwargs(
            dialect=dialect or "ansi", require_dialect=True
        )
        self.dialect: Dialect = cast(Dialect, self.config.get("dialect_obj"))
        self.column_metadata = _normalise_metadata(column_metadata)
        #: Optional *complete* column order for sources whose wildcard could
        #: not be expanded from the main metadata.  Re-tracing with this filled
        #: in is the supported way to complete a previously unknown star.
        self.wildcard_columns = _normalise_metadata(wildcard_columns)

    @classmethod
    def from_config(
        cls,
        config: FluffConfig,
        column_metadata: Optional[Mapping[str, Any]] = None,
        wildcard_columns: Optional[Mapping[str, Any]] = None,
    ) -> "ColumnLineageTracer":
        """Build a tracer from an existing config (used by the simple API)."""
        return cls(
            config=config,
            column_metadata=column_metadata,
            wildcard_columns=wildcard_columns,
        )

    def trace(
        self,
        sql: str,
        wildcard_columns: Optional[Mapping[str, Any]] = None,
    ) -> LineageResult:
        """Parse ``sql`` and trace its final result columns.

        Passing ``wildcard_columns`` re-runs the trace with a completed column
        order for wildcard sources which were previously unknown.  Only the
        supplied columns are used; nothing is read from a database.
        """
        if wildcard_columns:
            self.wildcard_columns = _normalise_metadata(wildcard_columns)
        linter = Linter(config=self.config)
        parsed = linter.parse_string(sql)
        if parsed.violations:
            from sqlfluff.api.simple import APIParsingError

            raise APIParsingError(parsed.violations)
        root_variant = parsed.root_variant()
        assert root_variant is not None and root_variant.tree is not None
        return self.trace_tree(root_variant.tree)

    def trace_tree(
        self,
        tree: BaseSegment,
        wildcard_columns: Optional[Mapping[str, Any]] = None,
    ) -> LineageResult:
        """Trace an already parsed tree (no SQL string or database needed)."""
        if wildcard_columns:
            self.wildcard_columns = _normalise_metadata(wildcard_columns)
        segment = _find_outer_query(tree)
        if segment is None:
            raise ValueError("No SELECT, set expression or WITH statement found.")
        node = _build_query(
            segment,
            None,
            self.dialect,
            self.column_metadata,
            self.wildcard_columns,
        )
        outputs = node.outputs()
        result = LineageResult()
        for sequence, output in enumerate(outputs):
            record = _output_record(output)
            record["sequence"] = sequence
            result.append(record)
        return result


def _find_outer_query(tree: BaseSegment) -> Optional[BaseSegment]:
    """Find the first real query segment in a file/statement tree."""
    for segment in tree.recursive_crawl(*_SELECTABLE_TYPES):
        # Values clauses only matter when they are the whole query body; prefer
        # an actual SELECT when both exist.
        if segment.is_type("values_clause") and not segment.get_parent():
            continue
        return segment
    return None


def trace_column_lineage(
    sql: str,
    dialect: Optional[str] = None,
    column_metadata: Optional[Mapping[str, Any]] = None,
    *,
    table_columns: Optional[Mapping[str, Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    wildcard_columns: Optional[Mapping[str, Any]] = None,
    unknown_wildcards: Optional[Mapping[str, Any]] = None,
    config: Optional[FluffConfig] = None,
) -> LineageResult:
    """Trace each output column of ``sql`` to physical table/column sources.

    Args:
        sql: SQL text.  Only this text and the supplied metadata are used; no
            database connection is made.
        dialect: SQLFluff dialect name (defaults to ``ansi``).  For backward
            compatibility this parameter also accepts the metadata mapping
            when callers pass it as the second positional argument.
        column_metadata: Ordered column metadata keyed by physical table name.
            Values may be a list of column names, a list of mappings with a
            ``name`` field, or ``{"columns": [...]}``.
        table_columns: Alias for ``column_metadata``.
        metadata: Alias for ``column_metadata``.
        wildcard_columns: Complete column order for sources whose wildcard was
            previously unresolved.  Supplying it re-traces those stars (keyed
            by table, alias, or ``alias.*``).  Nothing is guessed beyond it.
        unknown_wildcards: Alias for ``wildcard_columns``.
        config: Optional pre-built :class:`FluffConfig`.

    Returns:
        A :class:`LineageResult`: an ordered list of records, one per actual
        output column (or one unresolved marker for an unexpandable wildcard
        range).  Duplicate output names remain separate records.
    """
    if isinstance(dialect, Mapping):
        column_metadata = cast(Mapping[str, Any], dialect)
        dialect = None
    column_metadata = column_metadata or table_columns or metadata
    tracer = ColumnLineageTracer(
        dialect=dialect,
        column_metadata=column_metadata,
        config=config,
        wildcard_columns=wildcard_columns or unknown_wildcards,
    )
    return tracer.trace(sql)


# Friendly aliases.  They all share the same behaviour and return type.
get_column_lineage = trace_column_lineage
column_lineage = trace_column_lineage
analyze_column_lineage = trace_column_lineage
trace_result_columns = trace_column_lineage


__all__ = (
    "ColumnLineageTracer",
    "ColumnRecord",
    "LineageResult",
    "trace_column_lineage",
    "get_column_lineage",
    "column_lineage",
    "analyze_column_lineage",
    "trace_result_columns",
)

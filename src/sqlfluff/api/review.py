"""Read-only review API for grouping SQLFluff hints by business field.

Jinja-templated reports often render several columns under the same
placeholder name (for example ``partner_id``) or from the same template
scope, so SQLFluff hints cannot be attributed by rendered name alone.
This module correlates hints from legacy report layouts - which only have
cross-layout field slots - with hints from a new rendering which carries
stable field IDs, and presents the result to reviewers.

Everything exposed here is read-only: the report is built from frozen
data structures and offers no way to mutate lint results, files or
configuration. The existing per-finding outputs of the other API modules
are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

__all__ = (
    "HintRecord",
    "FieldOccurrence",
    "EditSuggestion",
    "Diagnostic",
    "FieldReviewGroup",
    "ReviewReport",
    "build_review_report",
    "review_report_as_dict",
)

# Legacy field slots and the new field IDs they may correspond to.
# A and B may only be P or Q; C may be P, Q or R.
LEGACY_SLOT_CANDIDATES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "A": frozenset({"P", "Q"}),
        "B": frozenset({"P", "Q"}),
        "C": frozenset({"P", "Q", "R"}),
    }
)

# The full set of stable field IDs used by the new report rendering.
NEW_FIELD_IDS: frozenset[str] = frozenset({"P", "Q", "R"})

_SOURCE_KINDS = frozenset({"legacy_layout", "rendering", "external_output"})


@dataclass(frozen=True)
class HintRecord:
    """One raw SQLFluff hint together with its report context.

    Args:
        source_id: Identifier of the layout, rendering or external output
            containing this hint.
        source_kind: One of ``legacy_layout``, ``rendering`` or
            ``external_output``.
        rule_code: SQLFluff rule code that produced the hint.
        rule_name: Human-readable rule name.
        message: Rule message text.
        line_no: One-based line number of the finding.
        line_pos: One-based line position of the finding.
        description: What this concrete occurrence points at.
        rendered_name: Field name after Jinja rendering (may repeat across
            unrelated outputs, e.g. ``partner_id``).
        template_scope: Jinja template scope the field was rendered from,
            if known.
        slot: Legacy cross-layout field slot (``A``, ``B`` or ``C``).
            Required for ``legacy_layout`` hints.
        field_id: Stable new field ID (``P``, ``Q`` or ``R``). Required for
            ``rendering`` hints.
        edit_action: Short label for the edit the rule would apply.
        edit_detail: Human-readable detail for the edit suggestion.
    """

    source_id: str
    source_kind: str
    rule_code: str
    rule_name: str
    message: str
    line_no: int
    line_pos: int
    description: str
    rendered_name: str
    template_scope: Optional[str] = None
    slot: Optional[str] = None
    field_id: Optional[str] = None
    edit_action: str = "relabel"
    edit_detail: Optional[str] = None


@dataclass(frozen=True)
class FieldOccurrence:
    """A single concrete appearance attributed to a field identity.

    Args:
        source_id: Identifier of the layout, rendering or external output.
        source_kind: Kind of source the occurrence comes from.
        rule_code: SQLFluff rule code that produced the hint.
        line_no: One-based line number of the finding.
        line_pos: One-based line position of the finding.
        description: What this concrete occurrence points at.
        rendered_name: Field name after Jinja rendering.
        template_scope: Jinja template scope, if known.
        slot: Legacy field slot, when the occurrence comes from a layout.
        field_id: Stable field ID, when the occurrence comes from a
            rendering or a slot resolved to a unique ID.
        evidence: Reviewer-facing statements justifying this attribution.
    """

    source_id: str
    source_kind: str
    rule_code: str
    line_no: int
    line_pos: int
    description: str
    rendered_name: str
    template_scope: Optional[str]
    slot: Optional[str]
    field_id: Optional[str]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class EditSuggestion:
    """An edit suggestion for one occurrence, with conflict marking.

    Args:
        source_id: Identifier of the source containing the occurrence.
        rule_code: Rule code the suggestion belongs to.
        candidate_field_id: New field ID the edit would target. ``None`` for
            independent outputs that have no migration mapping to target.
        action: Short label for the edit.
        detail: Human-readable detail for the edit.
        conflicts: Whether the candidate set for this slot is not uniquely
            resolved, so competing edits conflict.
        current: Whether this candidate matches the currently confirmed
            mapping for the slot.
        evidence: Reviewer-facing statements backing the suggestion.
    """

    source_id: str
    rule_code: str
    candidate_field_id: Optional[str]
    action: str
    detail: str
    conflicts: bool
    current: bool
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Diagnostic:
    """All hints from one rule attached to one field identity.

    Args:
        rule_code: SQLFluff rule code.
        rule_name: Human-readable rule name.
        message: Rule message text.
        occurrences: Every concrete occurrence kept for expansion.
        edit_suggestions: Candidate edits for the occurrences, including
            conflicting ones.
    """

    rule_code: str
    rule_name: str
    message: str
    occurrences: tuple[FieldOccurrence, ...]
    edit_suggestions: tuple[EditSuggestion, ...]


@dataclass(frozen=True)
class FieldReviewGroup:
    """Review material collected for one field identity.

    A group is keyed by a stable field ID when the identity is confirmed,
    by ``slot:<slot>`` while a legacy slot remains unresolved, or by
    ``independent:<source_id>`` for outputs that never joined the
    migration.

    Args:
        key: Stable lookup key for the group.
        field_id: Stable field ID when confirmed, otherwise ``None``.
        legacy_slots: Legacy slots whose occurrences feed this group.
        status: ``confirmed``, ``unresolved`` or ``independent``.
        rationale: Reviewer-facing explanation of the attribution.
        diagnostics: Per-rule diagnostics, foldable but fully expandable.
    """

    key: str
    field_id: Optional[str]
    legacy_slots: frozenset[str]
    status: str
    rationale: str
    diagnostics: tuple[Diagnostic, ...]

    def diagnostic_summary(self) -> tuple[dict[str, Any], ...]:
        """Collapse repeated rule hints into counts for a folded view.

        Returns:
            One entry per rule with its occurrence and suggestion counts.
            The full occurrences remain available on the diagnostics.
        """
        return tuple(
            {
                "rule_code": diagnostic.rule_code,
                "rule_name": diagnostic.rule_name,
                "occurrence_count": len(diagnostic.occurrences),
                "edit_suggestion_count": len(diagnostic.edit_suggestions),
                "collapsed": True,
            }
            for diagnostic in self.diagnostics
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize the group to plain JSON-compatible data."""
        return {
            "key": self.key,
            "field_id": self.field_id,
            "legacy_slots": sorted(self.legacy_slots),
            "status": self.status,
            "rationale": self.rationale,
            "folded_summary": list(self.diagnostic_summary()),
            "diagnostics": [
                {
                    "rule_code": diagnostic.rule_code,
                    "rule_name": diagnostic.rule_name,
                    "message": diagnostic.message,
                    "occurrences": [
                        {
                            "source_id": occurrence.source_id,
                            "source_kind": occurrence.source_kind,
                            "rule_code": occurrence.rule_code,
                            "line_no": occurrence.line_no,
                            "line_pos": occurrence.line_pos,
                            "description": occurrence.description,
                            "rendered_name": occurrence.rendered_name,
                            "template_scope": occurrence.template_scope,
                            "slot": occurrence.slot,
                            "field_id": occurrence.field_id,
                            "evidence": list(occurrence.evidence),
                        }
                        for occurrence in diagnostic.occurrences
                    ],
                    "edit_suggestions": [
                        {
                            "source_id": suggestion.source_id,
                            "rule_code": suggestion.rule_code,
                            "candidate_field_id": suggestion.candidate_field_id,
                            "action": suggestion.action,
                            "detail": suggestion.detail,
                            "conflicts": suggestion.conflicts,
                            "current": suggestion.current,
                            "evidence": list(suggestion.evidence),
                        }
                        for suggestion in diagnostic.edit_suggestions
                    ],
                }
                for diagnostic in self.diagnostics
            ],
        }


@dataclass(frozen=True)
class ReviewReport:
    """The full read-only review report.

    Args:
        field_groups: Groups for migration fields and resolved or
            unresolved legacy slots.
        independent_outputs: Groups for outputs outside the migration
            batch. These are never merged on a shared rendered name.
        field_slot_mapping: Resolved mapping from legacy slot to unique
            new field ID, or ``None`` where unresolved.
        unresolved_slots: Slots without a unique attribution.
    """

    field_groups: tuple[FieldReviewGroup, ...]
    independent_outputs: tuple[FieldReviewGroup, ...]
    field_slot_mapping: Mapping[str, Optional[str]]
    unresolved_slots: frozenset[str]

    def group(self, key: str) -> Optional[FieldReviewGroup]:
        """Look up a field group by its stable key.

        Args:
            key: Group key (field ID, ``slot:<slot>`` or
                ``independent:<source_id>``).

        Returns:
            The matching group, or ``None`` when no group has that key.
        """
        for candidate in self.field_groups + self.independent_outputs:
            if candidate.key == key:
                return candidate
        return None

    def as_dict(self) -> dict[str, Any]:
        """Serialize the whole report to plain JSON-compatible data."""
        return {
            "field_slot_mapping": dict(self.field_slot_mapping),
            "unresolved_slots": sorted(self.unresolved_slots),
            "field_groups": [group.as_dict() for group in self.field_groups],
            "independent_outputs": [
                group.as_dict() for group in self.independent_outputs
            ],
        }


def _validate_hint(hint: HintRecord) -> None:
    if hint.source_kind not in _SOURCE_KINDS:
        raise ValueError(
            f"Hint {hint.source_id}: unknown source_kind "
            f"'{hint.source_kind}', expected one of {sorted(_SOURCE_KINDS)}"
        )
    if hint.source_kind == "legacy_layout":
        if hint.slot is None:
            raise ValueError(
                f"Hint {hint.source_id}: legacy_layout hints require a slot"
            )
        if hint.slot not in LEGACY_SLOT_CANDIDATES:
            raise ValueError(
                f"Hint {hint.source_id}: unknown legacy slot '{hint.slot}', "
                f"expected one of {sorted(LEGACY_SLOT_CANDIDATES)}"
            )
    if hint.source_kind == "rendering" and hint.field_id is None:
        raise ValueError(f"Hint {hint.source_id}: rendering hints require a field_id")
    if hint.field_id is not None and hint.field_id not in NEW_FIELD_IDS:
        raise ValueError(
            f"Hint {hint.source_id}: unknown field_id '{hint.field_id}', "
            f"expected one of {sorted(NEW_FIELD_IDS)}"
        )


def _build_candidates(
    extra_slot_candidates: Optional[Mapping[str, Sequence[str]]],
) -> dict[str, set[str]]:
    candidates: dict[str, set[str]] = {
        slot: set(ids) for slot, ids in LEGACY_SLOT_CANDIDATES.items()
    }
    if extra_slot_candidates:
        for slot, extra_ids in extra_slot_candidates.items():
            if slot not in candidates:
                raise ValueError(
                    f"Cannot add candidates for unknown slot '{slot}', "
                    f"expected one of {sorted(LEGACY_SLOT_CANDIDATES)}"
                )
            for field_id in extra_ids:
                if field_id not in NEW_FIELD_IDS:
                    raise ValueError(
                        f"Unknown candidate field ID '{field_id}', expected "
                        f"one of {sorted(NEW_FIELD_IDS)}"
                    )
                candidates[slot].add(field_id)
    return candidates


def _resolve_mapping(
    layout_slots: Mapping[str, Sequence[str]],
    candidates: Mapping[str, set[str]],
) -> dict[str, Optional[str]]:
    """Resolve slots to field IDs under the cross-layout distinctness rule.

    Within one legacy layout the two displayed slots are different business
    fields, so they must never resolve to the same new field ID. A slot is
    mapped only when every valid joint assignment gives it the same value.
    """
    slots = sorted(candidates)
    assignments: dict[str, str] = {}
    solutions: list[dict[str, str]] = []

    def _consistent(slot: str, value: str) -> bool:
        for members in layout_slots.values():
            if slot not in members:
                continue
            for other_slot in members:
                if other_slot != slot and assignments.get(other_slot) == value:
                    return False
        return True

    def _backtrack(index: int) -> None:
        if index == len(slots):
            solutions.append(dict(assignments))
            return
        slot = slots[index]
        for value in sorted(candidates[slot]):
            if _consistent(slot, value):
                assignments[slot] = value
                _backtrack(index + 1)
                assignments.pop(slot)

    _backtrack(0)

    mapping: dict[str, Optional[str]] = {}
    for slot in slots:
        values = {solution[slot] for solution in solutions}
        mapping[slot] = next(iter(values)) if len(values) == 1 else None
    return mapping


def _slot_evidence(
    slot: str,
    candidates: Mapping[str, set[str]],
    layout_slots: Mapping[str, Sequence[str]],
    mapping: Mapping[str, Optional[str]],
) -> tuple[str, ...]:
    evidence = [
        f"migration record: slot {slot} is the same business field in every "
        "legacy layout",
        f"slot {slot} candidate new field IDs: {sorted(candidates[slot])}",
    ]
    for layout_id, members in sorted(layout_slots.items()):
        if slot in members:
            evidence.append(
                f"migration record: layout {layout_id} displays slots "
                f"{members[0]}/{members[1]} as two distinct business fields"
            )
    resolved = mapping[slot]
    if resolved is not None:
        evidence.append(
            f"joint deduction across all layouts leaves {resolved} as the "
            f"only field ID slot {slot} can take"
        )
    else:
        evidence.append(
            f"no single layout alone fixes slot {slot}; joint assignments "
            "still allow more than one field ID"
        )
    return tuple(evidence)


def _build_occurrence(
    hint: HintRecord,
    field_id: Optional[str],
    evidence: tuple[str, ...],
) -> FieldOccurrence:
    return FieldOccurrence(
        source_id=hint.source_id,
        source_kind=hint.source_kind,
        rule_code=hint.rule_code,
        line_no=hint.line_no,
        line_pos=hint.line_pos,
        description=hint.description,
        rendered_name=hint.rendered_name,
        template_scope=hint.template_scope,
        slot=hint.slot,
        field_id=field_id,
        evidence=evidence,
    )


def _build_group(
    key: str,
    field_id: Optional[str],
    legacy_slots: frozenset[str],
    status: str,
    rationale: str,
    bucket: dict[
        str, list[tuple[HintRecord, Optional[str], tuple[str, ...], frozenset[str]]]
    ],
) -> FieldReviewGroup:
    rule_bucket: dict[str, list[HintRecord]] = {}
    suggestion_bucket: dict[str, list[EditSuggestion]] = {}
    entries = sorted(
        bucket[key], key=lambda item: (item[0].rule_code, item[0].source_id)
    )
    for hint, resolved_id, evidence, candidate_ids in entries:
        rule_bucket.setdefault(hint.rule_code, []).append(hint)
        if hint.source_kind == "legacy_layout":
            assert hint.slot is not None
            unresolved = status == "unresolved"
            for candidate_id in sorted(candidate_ids):
                suggestion_bucket.setdefault(hint.rule_code, []).append(
                    EditSuggestion(
                        source_id=hint.source_id,
                        rule_code=hint.rule_code,
                        candidate_field_id=candidate_id,
                        action=hint.edit_action,
                        detail=hint.edit_detail
                        or f"relabel slot {hint.slot} as field {candidate_id}",
                        conflicts=unresolved,
                        current=(not unresolved and resolved_id == candidate_id),
                        evidence=evidence,
                    )
                )
        elif hint.source_kind == "external_output":
            suggestion_bucket.setdefault(hint.rule_code, []).append(
                EditSuggestion(
                    source_id=hint.source_id,
                    rule_code=hint.rule_code,
                    candidate_field_id=None,
                    action=hint.edit_action,
                    detail=hint.edit_detail
                    or "no relabel: output is outside the migration batch",
                    conflicts=False,
                    current=False,
                    evidence=evidence,
                )
            )
        else:
            assert hint.field_id is not None
            suggestion_bucket.setdefault(hint.rule_code, []).append(
                EditSuggestion(
                    source_id=hint.source_id,
                    rule_code=hint.rule_code,
                    candidate_field_id=hint.field_id,
                    action=hint.edit_action,
                    detail=hint.edit_detail
                    or f"keep field {hint.field_id} label from rendering",
                    conflicts=False,
                    current=True,
                    evidence=evidence,
                )
            )

    diagnostics = tuple(
        Diagnostic(
            rule_code=rule_code,
            rule_name=hints[0].rule_name,
            message=hints[0].message,
            occurrences=tuple(
                _build_occurrence(hint, resolved_id, evidence)
                for hint, resolved_id, evidence, _ in sorted(
                    (entry for entry in entries if entry[0].rule_code == rule_code),
                    key=lambda item: (
                        item[0].source_id,
                        item[0].line_no,
                        item[0].line_pos,
                        item[0].description,
                    ),
                )
            ),
            edit_suggestions=tuple(
                sorted(
                    suggestion_bucket.get(rule_code, []),
                    key=lambda item: (
                        item.source_id,
                        item.candidate_field_id,
                    ),
                )
            ),
        )
        for rule_code, hints in sorted(rule_bucket.items())
    )
    return FieldReviewGroup(
        key=key,
        field_id=field_id,
        legacy_slots=legacy_slots,
        status=status,
        rationale=rationale,
        diagnostics=diagnostics,
    )


def build_review_report(
    hints: Sequence[HintRecord],
    layout_slots: Mapping[str, Sequence[str]],
    extra_slot_candidates: Optional[Mapping[str, Sequence[str]]] = None,
) -> ReviewReport:
    """Build a read-only report grouping hints by business field.

    Args:
        hints: Raw SQLFluff hints with their report context.
        layout_slots: Mapping of legacy layout ID to the two distinct slots
            it displays (for example ``{"layout_1": ("A", "B")}``).
        extra_slot_candidates: Optional additional candidate field IDs for
            legacy slots, representing reviewer-added hypotheses. Passing
            ``None`` or omitting a slot leaves the migration candidates
            untouched.

    Returns:
        A frozen :class:`ReviewReport`. Field groups are keyed
        deterministically so shuffling input order never changes identity.

    Raises:
        ValueError: If a hint or candidate is missing required context or
            references an unknown slot or field ID.
    """
    for hint in hints:
        _validate_hint(hint)

    candidates = _build_candidates(extra_slot_candidates)
    mapping = _resolve_mapping(layout_slots, candidates)
    unresolved_slots = frozenset(
        slot for slot, field_id in mapping.items() if field_id is None
    )

    # Entries are grouped under a stable key together with the field ID the
    # occurrence is attributed to (if any), its evidence statements and the
    # candidate field IDs that apply when the entry comes from a legacy slot.
    bucket_type = dict[
        str,
        list[
            tuple[
                HintRecord,
                Optional[str],
                tuple[str, ...],
                frozenset[str],
            ]
        ],
    ]
    field_buckets: bucket_type = {}
    independent_buckets: bucket_type = {}
    group_slots: dict[str, set[str]] = {}

    for hint in hints:
        if hint.source_kind == "external_output":
            key = f"independent:{hint.source_id}"
            evidence: tuple[str, ...] = (
                f"output {hint.source_id} renders '{hint.rendered_name}'"
                + (f" from scope {hint.template_scope}" if hint.template_scope else "")
                + " but is not part of this migration batch",
                "kept independent: shared rendered names or template scopes "
                "outside the migration are not identity evidence",
            )
            independent_buckets.setdefault(key, []).append(
                (hint, hint.field_id, evidence, frozenset())
            )
            continue

        if hint.source_kind == "rendering":
            assert hint.field_id is not None
            key = hint.field_id
            evidence = (
                f"rendering {hint.source_id} directly labels this occurrence "
                f"with stable field ID {hint.field_id}",
            )
            field_buckets.setdefault(key, []).append(
                (hint, hint.field_id, evidence, frozenset())
            )
            continue

        assert hint.slot is not None
        slot = hint.slot
        resolved_id = mapping[slot]
        evidence = _slot_evidence(slot, candidates, layout_slots, mapping)
        if resolved_id is None:
            key = f"slot:{slot}"
        else:
            key = resolved_id
        field_buckets.setdefault(key, []).append(
            (hint, resolved_id, evidence, frozenset(candidates[slot]))
        )
        group_slots.setdefault(key, set()).add(slot)

    field_groups = []
    for key in sorted(field_buckets):
        if key in NEW_FIELD_IDS:
            field_id: Optional[str] = key
            status = "confirmed"
            slots = frozenset(group_slots.get(key, set()))
            if slots:
                rationale = (
                    f"field {key} is confirmed by the new rendering and by "
                    f"legacy slot(s) {sorted(slots)} whose joint cross-layout "
                    "deduction leaves this as the only possible field ID"
                )
            else:
                rationale = (
                    f"field {key} is confirmed directly by its stable field "
                    "ID in the new rendering"
                )
        else:
            slot = key.split(":", 1)[1]
            field_id = None
            status = "unresolved"
            slots = frozenset({slot})
            rationale = (
                f"slot {slot} cannot be fixed by any single layout and the "
                f"joint assignments still allow {sorted(candidates[slot])}; "
                "hints and candidate edits remain viewable pending review"
            )
        field_groups.append(
            _build_group(key, field_id, slots, status, rationale, field_buckets)
        )

    independent_groups = []
    for key in sorted(independent_buckets):
        source_id = key.split(":", 1)[1]
        independent_groups.append(
            _build_group(
                key,
                None,
                frozenset(),
                "independent",
                f"output {source_id} did not participate in the migration; "
                "its identity is not decided by layout order or shared names",
                independent_buckets,
            )
        )

    return ReviewReport(
        field_groups=tuple(field_groups),
        independent_outputs=tuple(independent_groups),
        field_slot_mapping=MappingProxyType(dict(sorted(mapping.items()))),
        unresolved_slots=unresolved_slots,
    )


def review_report_as_dict(
    hints: Sequence[HintRecord],
    layout_slots: Mapping[str, Sequence[str]],
    extra_slot_candidates: Optional[Mapping[str, Sequence[str]]] = None,
) -> dict[str, Any]:
    """Build the report and serialize it for read-only programmatic use.

    Args:
        hints: Raw SQLFluff hints with their report context.
        layout_slots: Mapping of legacy layout ID to the two distinct slots
            it displays.
        extra_slot_candidates: Optional additional candidate field IDs for
            legacy slots.

    Returns:
        A JSON-compatible dictionary containing folded rule summaries plus
        fully expandable occurrences and edit suggestions.
    """
    return build_review_report(
        hints, layout_slots, extra_slot_candidates=extra_slot_candidates
    ).as_dict()

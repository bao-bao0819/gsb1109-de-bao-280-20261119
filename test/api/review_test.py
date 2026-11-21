"""Tests for the read-only field review API."""

import copy

import pytest

from sqlfluff.api.review import (
    HintRecord,
    ReviewReport,
    build_review_report,
    review_report_as_dict,
)

# Two rules produce diagnostics for the migration batch.
RULE_RF01 = ("RF01", "references.special_chars", "Reference contains special chars.")
RULE_CV02 = ("CV02", "convention.count_rows", "Use 'COUNT(*)' rather than 'COUNT(1)'.")

# Migration records: each legacy layout shows exactly two slots and the two
# columns in one layout are always different business fields.
LAYOUT_SLOTS = {
    "layout_ab": ("A", "B"),
    "layout_bc": ("B", "C"),
    "layout_ca": ("C", "A"),
}


def _hint(
    source_id: str,
    source_kind: str,
    rule: tuple[str, str, str],
    line_no: int,
    description: str,
    rendered_name: str = "partner_id",
    template_scope: str | None = "partners.partner_id",
    slot: str | None = None,
    field_id: str | None = None,
    edit_detail: str | None = None,
) -> HintRecord:
    """Build a hint record for the migration scenario."""
    rule_code, rule_name, message = rule
    return HintRecord(
        source_id=source_id,
        source_kind=source_kind,
        rule_code=rule_code,
        rule_name=rule_name,
        message=message,
        line_no=line_no,
        line_pos=5,
        description=description,
        rendered_name=rendered_name,
        template_scope=template_scope,
        slot=slot,
        field_id=field_id,
        edit_detail=edit_detail,
    )


def _migration_hints() -> list[HintRecord]:
    """Hints from the three layouts and one stable-ID rendering.

    Every rendered field is called ``partner_id`` from the same template
    scope, so rendered names cannot decide identity.
    """
    return [
        _hint(
            "layout_ab", "legacy_layout", RULE_RF01, 10, "A occurrence in A/B", slot="A"
        ),
        _hint(
            "layout_ab", "legacy_layout", RULE_CV02, 11, "B occurrence in A/B", slot="B"
        ),
        _hint(
            "layout_bc", "legacy_layout", RULE_RF01, 20, "B occurrence in B/C", slot="B"
        ),
        _hint(
            "layout_bc", "legacy_layout", RULE_CV02, 21, "C occurrence in B/C", slot="C"
        ),
        _hint(
            "layout_ca", "legacy_layout", RULE_RF01, 30, "C occurrence in C/A", slot="C"
        ),
        _hint(
            "layout_ca", "legacy_layout", RULE_CV02, 31, "A occurrence in C/A", slot="A"
        ),
        _hint(
            "rendering_new",
            "rendering",
            RULE_RF01,
            40,
            "P occurrence with stable new ID",
            field_id="P",
        ),
        _hint(
            "rendering_new",
            "rendering",
            RULE_CV02,
            41,
            "Q occurrence with stable new ID",
            field_id="Q",
        ),
        _hint(
            "rendering_new",
            "rendering",
            RULE_RF01,
            42,
            "R occurrence with stable new ID",
            field_id="R",
        ),
    ]


def _external_hints() -> list[HintRecord]:
    """Same-name, same-scope outputs that never joined the migration."""
    return [
        _hint(
            "other_report_x",
            "external_output",
            RULE_RF01,
            50,
            "partner_id in unrelated report X",
            field_id=None,
        ),
        _hint(
            "other_report_y",
            "external_output",
            RULE_CV02,
            60,
            "partner_id in unrelated report Y",
            field_id=None,
        ),
    ]


def test__review__joint_deduction_pins_c_to_r() -> None:
    """No single layout fixes C, but the three layouts together force C=R."""
    report = build_review_report(_migration_hints(), LAYOUT_SLOTS)

    assert dict(report.field_slot_mapping) == {"A": None, "B": None, "C": "R"}
    assert report.unresolved_slots == frozenset({"A", "B"})

    group_r = report.group("R")
    assert group_r is not None
    assert group_r.status == "confirmed"
    assert group_r.legacy_slots == frozenset({"C"})

    c_occurrences = [
        occurrence
        for diagnostic in group_r.diagnostics
        for occurrence in diagnostic.occurrences
        if occurrence.slot == "C"
    ]
    assert {occurrence.source_id for occurrence in c_occurrences} == {
        "layout_bc",
        "layout_ca",
    }
    assert all(occurrence.field_id == "R" for occurrence in c_occurrences)
    # Every concrete occurrence carries reviewer-facing evidence, including
    # the point that no single layout alone confirms C.
    evidence = c_occurrences[0].evidence
    assert any(
        "same business field in every legacy layout" in line for line in evidence
    )
    assert any("layout_bc displays slots B/C" in line for line in evidence)
    assert any("only field ID slot C can take" in line for line in evidence)


def test__review__a_and_b_remain_unresolved_but_viewable() -> None:
    """A and B stay undecided; their hints and candidate edits are viewable."""
    report = build_review_report(_migration_hints(), LAYOUT_SLOTS)

    for slot in ("A", "B"):
        group = report.group(f"slot:{slot}")
        assert group is not None
        assert group.status == "unresolved"
        assert group.field_id is None

        suggestions = [
            suggestion
            for diagnostic in group.diagnostics
            for suggestion in diagnostic.edit_suggestions
            if suggestion.source_id.startswith("layout")
        ]
        assert {suggestion.candidate_field_id for suggestion in suggestions} == {
            "P",
            "Q",
        }
        assert all(suggestion.conflicts for suggestion in suggestions)
        assert all(not suggestion.current for suggestion in suggestions)

        occurrences = [
            occurrence
            for diagnostic in group.diagnostics
            for occurrence in diagnostic.occurrences
        ]
        # The same slot keeps one identity across the layouts it appears in.
        assert {occurrence.slot for occurrence in occurrences} == {slot}
        assert all(occurrence.field_id is None for occurrence in occurrences)


def test__review__rendering_diagnosis_goes_directly_to_field() -> None:
    """Hints carrying a stable new ID are always attributed to that field."""
    report = build_review_report(_migration_hints(), LAYOUT_SLOTS)

    for field_id in ("P", "Q", "R"):
        group = report.group(field_id)
        assert group is not None
        rendered = [
            occurrence
            for diagnostic in group.diagnostics
            for occurrence in diagnostic.occurrences
            if occurrence.source_id == "rendering_new"
        ]
        assert rendered, field_id
        assert all(occurrence.field_id == field_id for occurrence in rendered)
        assert all(
            "stable field ID" in occurrence.evidence[0] for occurrence in rendered
        )

    # P and Q have no legacy slot feeding them yet.
    assert report.group("P").legacy_slots == frozenset()
    assert report.group("Q").legacy_slots == frozenset()


def test__review__adding_r_candidate_to_b_breaks_unique_c() -> None:
    """Once B may be R, C loses its unique attribution; direct IDs hold."""
    report = build_review_report(
        _migration_hints(),
        LAYOUT_SLOTS,
        extra_slot_candidates={"B": ("R",)},
    )

    assert dict(report.field_slot_mapping) == {"A": None, "B": None, "C": None}
    assert report.unresolved_slots == frozenset({"A", "B", "C"})
    assert report.group("slot:C") is not None
    assert report.group("R") is not None

    group_b = report.group("slot:B")
    suggestions = {
        suggestion.candidate_field_id
        for diagnostic in group_b.diagnostics
        for suggestion in diagnostic.edit_suggestions
    }
    assert suggestions == {"P", "Q", "R"}

    # The rendering's direct R diagnosis is still attributed to field R.
    group_r = report.group("R")
    direct_r = [
        occurrence
        for diagnostic in group_r.diagnostics
        for occurrence in diagnostic.occurrences
        if occurrence.source_id == "rendering_new"
    ]
    assert direct_r
    assert all(occurrence.field_id == "R" for occurrence in direct_r)


def test__review__removing_candidate_restores_c_mapping() -> None:
    """Dropping the extra candidate returns C to its unique R attribution."""
    hints = _migration_hints()
    conflicted = build_review_report(
        hints, LAYOUT_SLOTS, extra_slot_candidates={"B": ("R",)}
    )
    restored = build_review_report(hints, LAYOUT_SLOTS)

    assert conflicted.field_slot_mapping["C"] is None
    assert restored.field_slot_mapping["C"] == "R"
    assert restored.unresolved_slots == frozenset({"A", "B"})


def test__review__external_outputs_stay_independent() -> None:
    """Same-name, same-scope outputs without migration evidence stay separate."""
    report = build_review_report(
        [*_migration_hints(), *_external_hints()], LAYOUT_SLOTS
    )

    keys = {group.key for group in report.independent_outputs}
    assert keys == {
        "independent:other_report_x",
        "independent:other_report_y",
    }
    for group in report.independent_outputs:
        assert group.status == "independent"
        assert group.field_id is None
        assert group.legacy_slots == frozenset()
        evidence = group.diagnostics[0].occurrences[0].evidence
        assert any("not part of this migration batch" in line for line in evidence)

    # External occurrences never leak into the migration field groups.
    migration_sources = {
        occurrence.source_id
        for group in report.field_groups
        for diagnostic in group.diagnostics
        for occurrence in diagnostic.occurrences
    }
    assert "other_report_x" not in migration_sources
    assert "other_report_y" not in migration_sources


def test__review__order_does_not_decide_identity() -> None:
    """Shuffling layouts, candidates and rendering leaves the report equal."""
    hints = _migration_hints()
    shuffled = list(reversed(hints))
    shuffled_layouts = {
        "layout_ca": ("C", "A"),
        "layout_ab": ("A", "B"),
        "layout_bc": ("B", "C"),
    }

    first = build_review_report(hints, LAYOUT_SLOTS)
    second = build_review_report(shuffled, shuffled_layouts)

    assert first.as_dict() == second.as_dict()
    assert [group.key for group in second.field_groups] == sorted(
        group.key for group in second.field_groups
    )


def test__review__external_identity_is_order_insensitive() -> None:
    """Independence holds regardless of where external hints appear."""
    external = _external_hints()
    first = build_review_report([*_migration_hints(), *external], LAYOUT_SLOTS)
    second = build_review_report([*external, *_migration_hints()], LAYOUT_SLOTS)

    assert [group.key for group in first.independent_outputs] == [
        group.key for group in second.independent_outputs
    ]
    assert first.as_dict() == second.as_dict()


def test__review__repeated_hints_fold_but_occurrences_expand() -> None:
    """Repeated rule hints collapse to counts while occurrences stay full."""
    repeated = [
        *_migration_hints(),
        _hint(
            "layout_ab",
            "legacy_layout",
            RULE_RF01,
            12,
            "Second A occurrence in A/B",
            slot="A",
        ),
    ]
    report = build_review_report(repeated, LAYOUT_SLOTS)
    group_a = report.group("slot:A")

    rf01 = next(
        diagnostic
        for diagnostic in group_a.diagnostics
        if diagnostic.rule_code == "RF01"
    )
    summary = next(
        item for item in group_a.diagnostic_summary() if item["rule_code"] == "RF01"
    )
    assert summary["occurrence_count"] == 2
    assert summary["collapsed"] is True
    assert len(rf01.occurrences) == 2
    assert len(rf01.edit_suggestions) >= 2

    descriptions = {occurrence.description for occurrence in rf01.occurrences}
    assert "A occurrence in A/B" in descriptions
    assert "Second A occurrence in A/B" in descriptions


def test__review__dict_payload_is_json_compatible_and_complete() -> None:
    """The programmatic payload keeps evidence, occurrences and suggestions."""
    import json

    payload = review_report_as_dict(
        [*_migration_hints(), *_external_hints()], LAYOUT_SLOTS
    )
    json.dumps(payload)

    assert payload["field_slot_mapping"] == {"A": None, "B": None, "C": "R"}
    group_r = next(group for group in payload["field_groups"] if group["key"] == "R")
    assert group_r["folded_summary"]
    diagnostic = group_r["diagnostics"][0]
    assert diagnostic["occurrences"][0]["evidence"]
    assert diagnostic["edit_suggestions"]
    assert payload["independent_outputs"]


def test__review__report_is_read_only() -> None:
    """The frozen report rejects mutation of its public structures."""
    report = build_review_report(_migration_hints(), LAYOUT_SLOTS)

    with pytest.raises((AttributeError, TypeError)):
        report.unresolved_slots = frozenset()  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        report.field_groups[0].diagnostics[0].occurrences.append(None)  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        report.field_slot_mapping["A"] = "R"  # type: ignore[index]


def test__review__validation_rejects_invalid_context() -> None:
    """Hints and candidates must reference known slots and field IDs."""
    bad_slot = _hint("x", "legacy_layout", RULE_RF01, 1, "bad", slot="Z")
    with pytest.raises(ValueError, match="unknown legacy slot"):
        build_review_report([bad_slot], LAYOUT_SLOTS)

    missing_slot = _hint("x", "legacy_layout", RULE_RF01, 1, "missing")
    with pytest.raises(ValueError, match="require a slot"):
        build_review_report([missing_slot], LAYOUT_SLOTS)

    missing_id = _hint("x", "rendering", RULE_RF01, 1, "missing")
    with pytest.raises(ValueError, match="require a field_id"):
        build_review_report([missing_id], LAYOUT_SLOTS)

    with pytest.raises(ValueError, match="Cannot add candidates for unknown slot"):
        build_review_report(
            _migration_hints(), LAYOUT_SLOTS, extra_slot_candidates={"Z": ("R",)}
        )
    with pytest.raises(ValueError, match="Unknown candidate field ID"):
        build_review_report(
            _migration_hints(), LAYOUT_SLOTS, extra_slot_candidates={"B": ("Z",)}
        )

    bad_kind = HintRecord(
        source_id="x",
        source_kind="mystery",
        rule_code=RULE_RF01[0],
        rule_name=RULE_RF01[1],
        message=RULE_RF01[2],
        line_no=1,
        line_pos=5,
        description="bad kind",
        rendered_name="partner_id",
    )
    with pytest.raises(ValueError, match="unknown source_kind"):
        build_review_report([bad_kind], LAYOUT_SLOTS)

    bad_id = HintRecord(
        source_id="x",
        source_kind="rendering",
        rule_code=RULE_RF01[0],
        rule_name=RULE_RF01[1],
        message=RULE_RF01[2],
        line_no=1,
        line_pos=5,
        description="bad id",
        rendered_name="partner_id",
        field_id="Z",
    )
    with pytest.raises(ValueError, match="unknown field_id"):
        build_review_report([bad_id], LAYOUT_SLOTS)


def test__review__group_lookup_misses_unknown_keys() -> None:
    """Unknown group keys resolve to None rather than raising."""
    report: ReviewReport = build_review_report(_migration_hints(), LAYOUT_SLOTS)
    assert report.group("slot:Z") is None
    assert report.group("independent:nobody") is None
    # Input hints are not retained or mutated by the read-only build.
    original = _migration_hints()
    snapshot = copy.deepcopy(original)
    build_review_report(original, LAYOUT_SLOTS)
    assert original == snapshot

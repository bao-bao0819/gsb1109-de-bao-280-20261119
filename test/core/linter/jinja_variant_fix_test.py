"""Tests for resolving source fixes proposed by repeated template expansions.

When a Jinja template contains a loop, one source range is rendered several
times. Lint rules see each expansion and may propose a fix for the same source
range from each of them. Those proposals are not always compatible:

* Sometimes every expansion agrees, and the fix can be written once.
* Sometimes they disagree, and writing either one would change an expansion
  which renders differently.
* Sometimes only one expansion proposes a fix, but the others were already
  correct, so applying the fix would break them.

These tests drive the real fix pipeline with deliberately contrived rules so
that each situation is reproducible, then check what was written to the source
file and what was reported for manual handling.

See: src/sqlfluff/core/linter/patch.py
"""

import logging

import pytest

from sqlfluff.core import Linter
from sqlfluff.core.config import FluffConfig
from sqlfluff.core.parser import WhitespaceSegment
from sqlfluff.core.rules import BaseRule, LintResult
from sqlfluff.core.rules.crawlers import RootOnlyCrawler
from sqlfluff.core.rules.fix import LintFix

# A loop which repeats a field fragment twice, plus an independent statement
# after the loop. The source range containing `x` is rendered twice.
LOOP_SQL = """{% for x in [1, 2] %}
SELECT foo = x, {{ x }};
{% endfor %}
SELECT bar = y;
"""

# The source offset of `x` inside `foo = x` on line 2, and the range which
# covers the whole `foo = x` field fragment.
FIELD_SOURCE_SLICE = slice(29, 36)


def _configure(*rule_codes, **overrides):
    config = {
        "dialect": "ansi",
        "templater": "jinja",
        "rules": ",".join(rule_codes),
        "render_variant_limit": 5,
    }
    config.update(overrides)
    return FluffConfig(overrides=config)


def _run_fix(sql, rule_class, **overrides):
    """Lint with a single contrived rule and return the LintedFile."""
    linter = Linter(
        config=_configure(rule_class.__name__.removeprefix("Rule_"), **overrides),
        user_rules=[rule_class],
    )
    return linter, linter.lint_string(sql, fname="test.sql", fix=True)


def _conflict_summary(linted):
    return [
        {
            "range": conflict.source_range,
            "reason": conflict.reason,
            "fix_texts": conflict.fix_texts,
            "line_no": conflict.line_no,
            "line_pos": conflict.line_pos,
        }
        for conflict in linted.source_fix_conflicts()
    ]


class Rule_T911(BaseRule):
    """Rewrite each loop expansion of `x` to the same value, and `y` to `Y`.

    Both expansions of the loop body propose the *same* edit, so it is safe to
    write it to the source once. The fix outside the loop is independent.
    """

    groups = ("all",)
    crawl_behaviour = RootOnlyCrawler()
    is_fix_compatible = True

    def _eval(self, context):
        results = []
        for seg in context.segment.raw_segments:
            if seg.raw == "x" and seg.is_code:
                results.append(
                    LintResult(
                        anchor=seg,
                        fixes=[LintFix.replace(seg, [seg.edit("z")])],
                    )
                )
            elif seg.raw == "y" and seg.is_code:
                results.append(
                    LintResult(
                        anchor=seg,
                        fixes=[LintFix.replace(seg, [seg.edit("Y")])],
                    )
                )
        return results


class Rule_T912(BaseRule):
    """Rewrite each loop expansion of `x` differently, and `y` to `Y`.

    The two expansions disagree about the same source range, so neither edit
    may be written. The independent fix outside the loop must still be saved.
    """

    groups = ("all",)
    crawl_behaviour = RootOnlyCrawler()
    is_fix_compatible = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen = 0

    def _eval(self, context):
        results = []
        for seg in context.segment.raw_segments:
            if seg.raw == "x" and seg.is_code:
                self._seen += 1
                value = "X1" if self._seen == 1 else "X2"
                results.append(
                    LintResult(
                        anchor=seg,
                        fixes=[LintFix.replace(seg, [seg.edit(value)])],
                    )
                )
            elif seg.raw == "y" and seg.is_code:
                results.append(
                    LintResult(
                        anchor=seg,
                        fixes=[LintFix.replace(seg, [seg.edit("Y")])],
                    )
                )
        return results


class Rule_T913(BaseRule):
    """Rewrite only the first expansion of `x`, and `y` to `Y`.

    The second expansion was already correct. Writing the first expansion's
    edit would also change the second one, so it must be withheld.
    """

    groups = ("all",)
    crawl_behaviour = RootOnlyCrawler()
    is_fix_compatible = True

    def _eval(self, context):
        results = []
        for seg in context.segment.raw_segments:
            if (
                seg.raw == "x"
                and seg.is_code
                and seg.pos_marker.templated_slice.start < 20
            ):
                results.append(
                    LintResult(
                        anchor=seg,
                        fixes=[LintFix.replace(seg, [seg.edit("z")])],
                    )
                )
            elif seg.raw == "y" and seg.is_code:
                results.append(
                    LintResult(
                        anchor=seg,
                        fixes=[LintFix.replace(seg, [seg.edit("Y")])],
                    )
                )
        return results


class Rule_T914(BaseRule):
    """Insert different amounts of whitespace before `a` in each expansion.

    The insertions land on the same source point with different text, so this
    is an insertion conflict.
    """

    groups = ("all",)
    crawl_behaviour = RootOnlyCrawler()
    is_fix_compatible = True

    def _eval(self, context):
        results = []
        for seg in context.segment.raw_segments:
            if seg.is_code and seg.raw == "a":
                start = seg.pos_marker.templated_slice.start
                width = "  " if start < 20 else "      "
                results.append(
                    LintResult(
                        anchor=seg,
                        fixes=[LintFix.create_before(seg, [WhitespaceSegment(width)])],
                    )
                )
            elif seg.is_code and seg.raw == "u":
                results.append(
                    LintResult(
                        anchor=seg,
                        fixes=[LintFix.replace(seg, [seg.edit("U")])],
                    )
                )
        return results


def test__fix__loop_same_fix_merges_once_and_independent_fix_saved():
    """Agreed loop edits collapse to one source edit; others still apply."""
    linter, linted = _run_fix(LOOP_SQL, Rule_T911)
    assert linted.source_fix_conflicts() == []

    fixed_sql, changed = linted.fix_string()

    assert changed
    # The `x` field is rewritten once in the loop body, and `y` outside the
    # loop is fixed independently.
    assert "SELECT foo = z, {{ x }};" in fixed_sql
    assert "SELECT bar = Y;" in fixed_sql
    # Both loop iterations render the fixed body.
    assert fixed_sql.count("foo = z") == 1

    # Running fix again does not change the already safe source.
    linted_again = linter.lint_string(fixed_sql, fname="test.sql", fix=True)
    fixed_again, changed_again = linted_again.fix_string()
    assert not changed_again
    assert fixed_again == fixed_sql
    assert linted_again.source_fix_conflicts() == []


def test__fix__loop_incompatible_fix_withheld_and_reported():
    """Conflicting loop edits are withheld; the report locates the source."""
    _linter, linted = _run_fix(LOOP_SQL, Rule_T912)

    fixed_sql, changed = linted.fix_string()

    # The loop body keeps its original text: neither incompatible edit won.
    assert "SELECT foo = x, {{ x }};" in fixed_sql
    # The independent, unambiguous edit outside the loop is still saved.
    assert changed
    assert "SELECT bar = Y;" in fixed_sql

    conflicts = linted.source_fix_conflicts()
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.source_range == (
        FIELD_SOURCE_SLICE.start,
        FIELD_SOURCE_SLICE.stop,
    )
    assert conflict.source_str == "foo = x"
    assert conflict.is_incompatible
    assert sorted(conflict.fix_texts) == ["foo = X1", "foo = X2"]
    # The conflict points at the real source position (line 2, `foo = x`).
    assert (conflict.line_no, conflict.line_pos) == (2, 8)
    # Both expansions are recorded as evidence, at their templated positions.
    assert len(conflict.source_occurrences) == 2
    templated_starts = sorted(
        templated_slice.start for _, templated_slice in conflict.source_occurrences
    )
    assert templated_starts == [8, 28]


def test__fix__loop_unconfirmed_fix_withheld():
    """An edit proposed by only one expansion is withheld as unconfirmed."""
    _linter, linted = _run_fix(LOOP_SQL, Rule_T913)

    fixed_sql, changed = linted.fix_string()

    assert "SELECT foo = x, {{ x }};" in fixed_sql
    assert changed
    assert "SELECT bar = Y;" in fixed_sql

    conflicts = linted.source_fix_conflicts()
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.is_unconfirmed
    assert conflict.source_range == (
        FIELD_SOURCE_SLICE.start,
        FIELD_SOURCE_SLICE.stop,
    )
    assert conflict.expected_occurrences == 2
    assert conflict.proposed_occurrences == 1


def test__fix__loop_conflict_report_contains_expansion_evidence():
    """The JSON report carries source ranges and per-expansion evidence."""
    _linter, linted = _run_fix(LOOP_SQL, Rule_T912)

    report = linted.fix_report()

    assert report["conflict_count"] == 1
    conflict = report["conflicts"][0]
    assert conflict["source_range"] == [29, 36]
    assert conflict["source_str"] == "foo = x"
    assert conflict["reason"] == "incompatible"
    assert conflict["fix_texts"] == ["foo = X1", "foo = X2"]
    # Every proposal records the render it came from and where in the
    # templated output that expansion sits.
    assert {candidate["variant_idx"] for candidate in conflict["candidates"]} == {0}
    assert sorted(
        candidate["templated_slice"] for candidate in conflict["candidates"]
    ) == [[8, 15], [28, 35]]


@pytest.mark.parametrize("swap", [False, True])
def test__fix__loop_conflict_is_order_independent(swap):
    """Swapping the expansion order must not select the other winner."""
    sql = LOOP_SQL
    # `Rule_T912` assigns "X1" to the first expansion it sees. Reversing the
    # loop input would flip which expansion gets which text, but either way
    # the source range is incompatible and must stay untouched.
    if swap:
        sql = """{% for x in [2, 1] %}
SELECT foo = x, {{ x }};
{% endfor %}
SELECT bar = y;
"""
    _linter, linted = _run_fix(sql, Rule_T912)

    fixed_sql, _ = linted.fix_string()
    assert "SELECT foo = x, {{ x }};" in fixed_sql
    conflicts = linted.source_fix_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].is_incompatible
    # Order changes only which text is associated with which expansion, not
    # the set of conflicting texts.
    assert sorted(conflicts[0].fix_texts) == ["foo = X1", "foo = X2"]


def test__fix__conflicting_insertions_at_same_source_point():
    """Different insertions at one source point are withheld and reported."""
    sql = """{% for x in [1, 2] %}
SELECT {{ x }} a;
{% endfor %}
SELECT b FROM u;
"""
    _linter, linted = _run_fix(sql, Rule_T914)

    fixed_sql, changed = linted.fix_string()

    # `a` is untouched in the loop (no arbitrary insertion won).
    assert "SELECT {{ x }} a;" in fixed_sql
    # The independent edit outside the loop is saved.
    assert changed
    assert "FROM U;" in fixed_sql

    conflicts = linted.source_fix_conflicts()
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.is_incompatible
    # Both proposals are anchored on the same source character (`a`), and the
    # differing inserted whitespace makes them incompatible.
    assert conflict.source_range == (37, 38)
    assert conflict.source_str == "a"
    assert len(conflict.fix_texts) == 2
    assert all(text.endswith("a") for text in conflict.fix_texts)


def test__fix__safe_rerun_makes_no_further_changes():
    """Once safe edits are written, a rerun must not change them again."""
    linter, linted = _run_fix(LOOP_SQL, Rule_T912)
    first_pass, changed = linted.fix_string()
    assert changed
    assert "SELECT bar = Y;" in first_pass

    second_linted = linter.lint_string(first_pass, fname="test.sql", fix=True)
    second_pass, changed_again = second_linted.fix_string()
    assert not changed_again
    assert second_pass == first_pass
    # The incompatible range is still reported (it is still unfixed), but no
    # new safe edit is introduced.
    assert len(second_linted.source_fix_conflicts()) == 1


def test__fix__conflict_is_logged_as_manual_review(caplog):
    """Conflicts are surfaced in logs so users know manual handling is needed."""
    with caplog.at_level(logging.WARNING, logger="sqlfluff.linter"):
        _linter, _linted = _run_fix(LOOP_SQL, Rule_T912)

    assert "Manual review required." in caplog.text
    assert "incompatible" in caplog.text

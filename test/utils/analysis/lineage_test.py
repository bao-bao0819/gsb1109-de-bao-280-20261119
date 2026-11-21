"""Tests for result-column level source tracing."""

import pytest

from sqlfluff.core import FluffConfig, Linter
from sqlfluff.utils.analysis.lineage import (
    ColumnLineageTracer,
    trace_column_lineage,
)


def _by_sequence(result):
    return [(record["sequence"], record) for record in result]


def _source_pairs(record):
    return [(source["table"], source["column"]) for source in record["sources"]]


def _reasons(record):
    return [item["reason"] for item in record["unresolved"]]


def test_lineage_duplicate_output_names_are_not_overwritten():
    """Two outputs with the same display name stay as two records."""
    result = trace_column_lineage(
        "SELECT a.id AS dup, a.name AS dup FROM alpha AS a",
        dialect="ansi",
        column_metadata={"alpha": ["id", "name"]},
    )
    assert [record["name"] for record in result] == ["dup", "dup"]
    assert [record["ordinal"] for record in result] == [1, 2]
    assert _source_pairs(result[0]) == [("alpha", "id")]
    assert _source_pairs(result[1]) == [("alpha", "name")]
    assert all(record["status"] == "resolved" for record in result)


def test_lineage_same_cte_name_maps_to_different_physical_tables():
    """Two CTEs produce the same display name from different physical tables."""
    sql = """
    WITH first AS (SELECT s.id AS k FROM t1 AS s),
         second AS (SELECT s.id AS k FROM t2 AS s)
    SELECT first.k, second.k FROM first JOIN second ON first.k = second.k
    """
    result = trace_column_lineage(
        sql,
        dialect="ansi",
        column_metadata={"t1": ["id"], "t2": ["id"]},
    )
    assert [record["name"] for record in result] == ["k", "k"]
    assert _source_pairs(result[0]) == [("t1", "id")]
    assert _source_pairs(result[1]) == [("t2", "id")]
    # The CTE hop is evidence of how the column was reached.
    assert result[0]["sources"][0]["via"][0]["kind"] == "cte"
    assert result[0]["sources"][0]["via"][0]["table"] == "first"
    assert result[1]["sources"][0]["via"][0]["table"] == "second"


def test_lineage_inner_and_outer_aliases_do_not_leak_scope():
    """The same alias in inner and outer scopes must not resolve across."""
    sql = """
    WITH outer_cte AS (SELECT s.id AS id FROM t1 AS s)
    SELECT s.id FROM outer_cte AS s
    """
    result = trace_column_lineage(sql, dialect="ansi", column_metadata={"t1": ["id"]})
    assert len(result) == 1
    source = result[0]["sources"][0]
    assert (source["table"], source["column"]) == ("t1", "id")
    # Only the outer scope's hop should be recorded. The inner `s` is a
    # different scope and must not appear as an intermediate reference.
    assert [step["table"] for step in source["via"]] == ["s"]
    assert source["via"][0]["scope"].startswith("select_statement@")


def test_lineage_nested_same_alias_keeps_two_scopes_distinct():
    """Nested queries which both alias a source as `s` stay distinguishable."""
    sql = """
    WITH base AS (SELECT s.id AS id FROM t1 AS s),
         renamed AS (SELECT base.id AS id FROM base AS s)
    SELECT s.id FROM renamed AS s
    """
    result = trace_column_lineage(sql, dialect="ansi", column_metadata={"t1": ["id"]})
    source = result[0]["sources"][0]
    assert (source["table"], source["column"]) == ("t1", "id")
    scopes = [step["scope"] for step in source["via"]]
    assert len(scopes) == 2
    assert len(set(scopes)) == 2


def test_lineage_cte_column_list_renames_by_position():
    """`WITH c (x, y)` renames outputs by position, not by source name."""
    sql = """
    WITH c (x, y) AS (
        SELECT t.val, t.id FROM t1 AS t
    )
    SELECT c.x, c.y FROM c
    """
    result = trace_column_lineage(
        sql, dialect="ansi", column_metadata={"t1": ["id", "val"]}
    )
    assert [record["name"] for record in result] == ["x", "y"]
    assert _source_pairs(result[0]) == [("t1", "val")]
    assert _source_pairs(result[1]) == [("t1", "id")]


def test_lineage_cte_expression_reversed_to_intermediate_reference():
    """An expression inside a CTE is traced back through its input column."""
    sql = """
    WITH c AS (
        SELECT (t.id + t.val) * 2 AS doubled FROM t1 AS t
    )
    SELECT c.doubled FROM c
    """
    result = trace_column_lineage(
        sql, dialect="ansi", column_metadata={"t1": ["id", "val"]}
    )
    assert result[0]["name"] == "doubled"
    assert result[0]["expression"] is True
    assert _source_pairs(result[0]) == [("t1", "id"), ("t1", "val")]
    assert result[0]["status"] == "multiple"
    assert result[0]["references"] == ["c.doubled"]


def test_lineage_known_star_expands_in_supplied_column_order():
    """A qualified star over known metadata expands in exact order."""
    sql = "SELECT k.* FROM known AS k"
    result = trace_column_lineage(
        sql,
        dialect="ansi",
        column_metadata={"known": ["zeta", "alpha", "mid"]},
    )
    assert [record["name"] for record in result] == ["zeta", "alpha", "mid"]
    assert [record["ordinal"] for record in result] == [1, 2, 3]
    assert all(record["wildcard"] for record in result)
    assert [_source_pairs(record) for record in result] == [
        [("known", "zeta")],
        [("known", "alpha")],
        [("known", "mid")],
    ]


def test_lineage_unknown_star_is_unresolved_and_never_guesses_tables():
    """A star with no metadata stays unresolved and names no fake source."""
    sql = "SELECT u.* FROM unknown AS u"
    result = trace_column_lineage(sql, dialect="ansi")
    assert len(result) == 1
    record = result[0]
    assert record["wildcard_range"] is True
    assert record["status"] == "unknown"
    assert record["sources"] == []
    assert record["ordinal"] is None
    assert record["position_known"] is False
    assert record["start_position"] == 1
    assert _reasons(record) == ["wildcard_not_expanded"]


def test_lineage_known_and_unknown_stars_distinguish_proven_positions():
    """Outputs after an unknown range report a bound, not a fake ordinal."""
    sql = (
        "SELECT k.*, u.*, k.id AS again "
        "FROM known AS k JOIN unknown AS u ON k.id = u.id"
    )
    result = trace_column_lineage(
        sql, dialect="ansi", column_metadata={"known": ["id", "name"]}
    )
    # Known star expands; unknown star is a single marker; the tail column is
    # still separately proven but its global ordinal cannot be determined.
    assert [record["name"] for record in result] == ["id", "name", "u.*", "again"]
    assert [record["ordinal"] for record in result] == [1, 2, None, None]
    assert result[2]["wildcard_range"] is True
    assert result[2]["start_position"] == 3
    assert result[2]["sources"] == []
    # `again` is proven to known.id, but its position is only a lower bound.
    assert _source_pairs(result[3]) == [("known", "id")]
    assert result[3]["status"] == "resolved"
    assert result[3]["position_known"] is False
    assert result[3]["position_lower_bound"] == 3


def test_lineage_union_all_aligns_strictly_by_position():
    """UNION branches pair by ordinal even when aliases disagree."""
    sql = """
    SELECT a.id AS x, a.val AS y FROM t1 AS a
    UNION ALL
    SELECT b.val AS x, b.id AS y FROM t2 AS b
    """
    result = trace_column_lineage(
        sql,
        dialect="ansi",
        column_metadata={"t1": ["id", "val"], "t2": ["id", "val"]},
    )
    assert [record["name"] for record in result] == ["x", "y"]
    assert _source_pairs(result[0]) == [("t1", "id"), ("t2", "val")]
    assert _source_pairs(result[1]) == [("t1", "val"), ("t2", "id")]
    assert [record["status"] for record in result] == ["multiple", "multiple"]
    assert [record["evidence_count"] for record in result] == [2, 2]


def test_lineage_union_expression_columns_aggregate_per_position():
    """An expression branch contributes evidence only at its own position."""
    sql = """
    SELECT a.id, a.val + 1 AS total FROM t1 AS a
    UNION ALL
    SELECT b.key, b.amount * 2 AS other FROM t2 AS b
    """
    result = trace_column_lineage(
        sql,
        dialect="ansi",
        column_metadata={"t1": ["id", "val"], "t2": ["key", "amount"]},
    )
    assert [record["name"] for record in result] == ["id", "total"]
    assert _source_pairs(result[0]) == [("t1", "id"), ("t2", "key")]
    assert _source_pairs(result[1]) == [("t1", "val"), ("t2", "amount")]
    assert result[1]["expression"] is True


def test_lineage_union_with_unknown_star_refuses_alignment():
    """An unexpanded star in a UNION branch blocks positional alignment."""
    sql = """
    SELECT a.id FROM t1 AS a
    UNION ALL
    SELECT b.key, b.* FROM t2 AS b
    """
    result = trace_column_lineage(
        sql,
        dialect="ansi",
        column_metadata={"t1": ["id"]},
    )
    # Position 1 precedes every branch's unknown range, so it is provable
    # (even though one side has no metadata).
    first = result[0]
    assert first["name"] == "id"
    assert first["position_known"] is True
    assert first["ordinal"] == 1
    assert any(
        (source["table"], source["column"]) == ("t1", "id")
        for source in first["sources"]
    )
    # From the unknown range onward nothing can be aligned by ordinal.
    marker = result[1]
    assert marker["wildcard_range"] is True
    assert marker["status"] == "unknown"
    assert marker["sources"] == []
    assert marker["position_known"] is False
    assert marker["start_position"] == 2
    assert "wildcard_range_in_set_expression" in _reasons(marker)


def test_lineage_qualified_column_on_unknown_source_not_guessed():
    """A qualified column on an unknown table is unresolved, not invented."""
    sql = "SELECT u.id FROM unknown AS u"
    result = trace_column_lineage(sql, dialect="ansi")
    assert result[0]["sources"] == []
    assert result[0]["status"] == "unknown"
    assert _reasons(result[0]) == ["source_column_metadata_missing"]


def test_lineage_metadata_forms_preserve_order():
    """Metadata may be strings, mappings or ``{"columns": [...]}``."""
    sql = "SELECT k.* FROM known AS k"
    expected = ["b", "a"]
    for metadata in (
        {"known": ["b", "a"]},
        {"known": [{"name": "b"}, {"name": "a"}]},
        {"known": {"columns": ["b", "a"]}},
        {"known": {"b": "int", "a": "text"}},
    ):
        result = trace_column_lineage(sql, dialect="ansi", column_metadata=metadata)
        assert [record["name"] for record in result] == expected


def test_lineage_accepts_positional_metadata():
    """``dialect`` may be omitted when metadata is the second argument."""
    result = trace_column_lineage("SELECT k.* FROM known AS k", {"known": ["a", "b"]})
    assert [record["name"] for record in result] == ["a", "b"]


def test_lineage_trace_tree_uses_real_parse_tree():
    """Tracing an existing tree needs no SQL string and no database."""
    linter = Linter(dialect="ansi")
    parsed = linter.parse_string("SELECT a.id FROM alpha AS a")
    tracer = ColumnLineageTracer(
        config=FluffConfig.from_kwargs(dialect="ansi"),
        column_metadata={"alpha": ["id", "name"]},
    )
    result = tracer.trace_tree(parsed.tree)
    assert _source_pairs(result[0]) == [("alpha", "id")]
    assert result[0]["status"] == "resolved"


def test_lineage_derived_table_resolves_through_subquery():
    """A derived table is resolved through its own SELECT outputs."""
    sql = "SELECT q.x FROM (SELECT t.id AS x FROM t1 AS t) AS q"
    result = trace_column_lineage(sql, dialect="ansi", column_metadata={"t1": ["id"]})
    assert result[0]["name"] == "x"
    assert _source_pairs(result[0]) == [("t1", "id")]
    assert result[0]["sources"][0]["via"][0]["kind"] == "subquery"


def test_lineage_unresolved_column_is_explicit():
    """A column absent from known metadata is reported, not fabricated."""
    result = trace_column_lineage(
        "SELECT a.missing FROM alpha AS a",
        dialect="ansi",
        column_metadata={"alpha": ["id"]},
    )
    assert result[0]["sources"] == []
    assert _reasons(result[0]) == ["column_not_in_source_metadata"]


def test_lineage_unknown_source_cannot_be_ruled_out():
    """A proven column plus an unknown source is reported as non-exclusive."""
    sql = "SELECT id FROM known AS k JOIN unknown AS u ON k.id = u.id"
    result = trace_column_lineage(
        sql, dialect="ansi", column_metadata={"known": ["id"]}
    )
    assert _source_pairs(result[0]) == [("known", "id")]
    assert result[0]["status"] == "partial"
    assert "ambiguous_unqualified_column" in _reasons(result[0])


def test_lineage_parsing_errors_are_raised():
    """Invalid SQL raises the usual API parsing error."""
    from sqlfluff.api import APIParsingError

    with pytest.raises(APIParsingError):
        trace_column_lineage("SELECT FROM", dialect="ansi")


def test_lineage_result_serialises():
    """Records are plain dictionaries and serialise as a list."""
    result = trace_column_lineage(
        "SELECT a.id FROM alpha AS a",
        dialect="ansi",
        column_metadata={"alpha": ["id"]},
    )
    assert result.as_dict() == [dict(record) for record in result]
    assert result.to_dict()["columns"][0]["name"] == "id"


def test_lineage_unknown_star_can_be_completed_and_retraced():
    """Supplying the unknown star's full order re-traces later positions."""
    sql = (
        "SELECT k.*, u.*, k.id AS again "
        "FROM known AS k JOIN unknown AS u ON k.id = u.id"
    )
    known = {"known": ["id", "name"]}

    before = trace_column_lineage(sql, dialect="ansi", column_metadata=known)
    assert before[2]["wildcard_range"] is True
    assert before[2]["sources"] == []
    assert before[3]["ordinal"] is None

    after = trace_column_lineage(
        sql,
        dialect="ansi",
        column_metadata=known,
        wildcard_columns={"unknown": ["u1", "u2", "u3"]},
    )
    assert [record["name"] for record in after] == [
        "id",
        "name",
        "u1",
        "u2",
        "u3",
        "again",
    ]
    assert [record["ordinal"] for record in after] == [1, 2, 3, 4, 5, 6]
    assert after[3]["sources"] == [
        {
            "table": "unknown",
            "column": "u2",
            "table_alias": "u",
            "via": [],
            "computed": False,
        }
    ]
    assert after[-1]["ordinal"] == 6
    assert after[-1]["status"] == "resolved"


def test_lineage_completed_union_star_aligns_by_position():
    """Once a UNION star is completed, branches align by ordinal again."""
    sql = "SELECT a.id AS k FROM t1 AS a UNION ALL SELECT b.* FROM t2 AS b"
    completed = trace_column_lineage(
        sql,
        dialect="ansi",
        column_metadata={"t1": ["id", "val"]},
        wildcard_columns={"t2": ["x", "y"]},
    )
    assert [record["ordinal"] for record in completed] == [1, 2]
    assert _source_pairs(completed[0]) == [("t1", "id"), ("t2", "x")]
    assert _source_pairs(completed[1]) == [("t2", "y")]
    assert completed[1]["status"] == "partial"
    assert "set_branch_missing_column" in _reasons(completed[1])


def test_lineage_expression_sources_marked_computed():
    """Expression-derived values are flagged on their source evidence."""
    sql = "SELECT a.val + 1 AS bumped FROM alpha AS a"
    result = trace_column_lineage(
        sql, dialect="ansi", column_metadata={"alpha": ["val"]}
    )
    assert result[0]["expression"] is True
    assert result[0]["sources"][0]["computed"] is True
    assert _source_pairs(result[0]) == [("alpha", "val")]


def test_lineage_values_clause_uses_positions_not_guessed_names():
    """A VALUES clause keeps one record per positional column."""
    result = trace_column_lineage("VALUES (1, 2), (3, 4)", dialect="ansi")
    assert [record["ordinal"] for record in result] == [1, 2]
    assert [record["name"] for record in result] == [None, None]
    assert all(record["status"] == "unknown" for record in result)
    assert all(_reasons(record) == ["values_column_name_unknown"] for record in result)


def test_lineage_values_clause_named_by_cte_column_list():
    """A CTE column list names VALUES outputs by position."""
    sql = "WITH c (a, b) AS (VALUES (1, 2)) SELECT c.a, c.b FROM c"
    result = trace_column_lineage(sql, dialect="ansi")
    assert [record["name"] for record in result] == ["a", "b"]
    assert [record["ordinal"] for record in result] == [1, 2]


def test_lineage_bracketed_set_branch_positions():
    """Bracketed UNION branches still align by output position."""
    sql = "(SELECT a.id FROM t1 AS a) UNION ALL (SELECT b.id FROM t2 AS b)"
    result = trace_column_lineage(
        sql,
        dialect="ansi",
        column_metadata={"t1": ["id"], "t2": ["id"]},
    )
    assert len(result) == 1
    assert _source_pairs(result[0]) == [("t1", "id"), ("t2", "id")]


def test_lineage_table_name_normalisation_across_dialects():
    """Quoted/qualified table names still match metadata keys."""
    cases = [
        ("ansi", 'SELECT x.id FROM "sch"."tbl" AS x', "sch.tbl"),
        ("postgres", "SELECT x.id FROM public.t1 AS x", "public.t1"),
        ("bigquery", "SELECT x.id FROM `p.d.t1` AS x", "p.d.t1"),
        ("tsql", "SELECT x.id FROM [dbo].[t1] AS x", "dbo.t1"),
    ]
    for dialect, sql, table in cases:
        result = trace_column_lineage(
            sql, dialect=dialect, column_metadata={table: ["id"]}
        )
        assert result[0]["status"] == "resolved", (dialect, result[0])
        assert result[0]["sources"][0]["table"] == table


def test_lineage_literal_output_has_no_source():
    """A literal-only expression reports no source rather than a fake one."""
    result = trace_column_lineage("SELECT 1 AS n", dialect="ansi")
    assert result[0]["sources"] == []
    assert result[0]["status"] == "unknown"
    assert _reasons(result[0]) == ["expression_without_source_column"]


def test_lineage_result_lookup_keeps_duplicates():
    """Name lookup returns every duplicate; positional lookup is exact."""
    result = trace_column_lineage(
        "SELECT a.id AS dup, a.name AS dup FROM alpha AS a",
        dialect="ansi",
        column_metadata={"alpha": ["id", "name"]},
    )
    assert len(result.by_name("dup")) == 2
    assert _source_pairs(result.at(1)) == [("alpha", "id")]
    assert _source_pairs(result.at(2)) == [("alpha", "name")]
    assert result.at(3) is None


def test_lineage_cte_alias_count_mismatch_is_flagged():
    """A CTE column list with too many names flags the missing expression."""
    sql = "WITH c (a, b) AS (SELECT t.id FROM t1 AS t) SELECT c.a, c.b FROM c"
    result = trace_column_lineage(sql, dialect="ansi", column_metadata={"t1": ["id"]})
    assert [record["name"] for record in result] == ["a", "b"]
    assert result[1]["status"] == "unknown"
    assert _reasons(result[1]) == ["cte_column_alias_count_mismatch"]

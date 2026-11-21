"""Trace wide-table result columns back to source tables and columns.

This example shows result-column level lineage: for every column a query
outputs, which physical table and column does it come from?

Only the real parse tree and the caller-supplied column metadata are used.
No database connection is made and no table is guessed.

The sample below deliberately contains every situation the feature handles:

* two CTEs which both output a column called ``k`` but read different
  physical tables (``t1`` and ``t2``);
* the same table alias ``s`` in an inner and an outer scope;
* two outputs with the same display name (``k``);
* a known qualified star (``s.*`` over CTE metadata) and an unqualified star
  over a table with no metadata;
* a SELECT branch containing expressions, merged with ``UNION ALL`` and
  aligned strictly by output position.

Run with::

    python examples/07_column_lineage.py
"""

import json

import sqlfluff
from sqlfluff.utils.analysis.lineage import ColumnLineageTracer

SQL = """
WITH first AS (
    -- Same output name `k`, different physical table (t1).
    SELECT inner_s.id AS k, inner_s.val AS v
    FROM t1 AS inner_s
),
second AS (
    -- Same output name `k`, different physical table (t2).
    SELECT inner_s.id AS k, inner_s.val AS v
    FROM t2 AS inner_s
)
SELECT
    first.k,          -- duplicate output name `k`
    second.k,         -- duplicate output name `k`
    outer_s.*,        -- qualified star: CTE outputs (known order)
    unknown_s.*,      -- star over a table with no metadata (unknown)
    first.v + second.v AS total
FROM first AS outer_s  -- outer alias `s`-like scope, distinct from CTE scope
JOIN second
    ON outer_s.k = second.k
JOIN unknown_table AS unknown_s
    ON unknown_s.id = outer_s.k
UNION ALL
SELECT
    x.k,
    y.k,
    x.*,
    unknown_s.*,
    x.v * 2 AS total
FROM first AS x
JOIN second AS y
    ON x.k = y.k
JOIN unknown_table AS unknown_s
    ON unknown_s.id = x.k
"""

# Column metadata is the *only* schema information used.  Order matters:
# stars expand in exactly this order.
COLUMN_METADATA = {
    "t1": ["id", "val"],
    "t2": ["id", "val"],
    # `unknown_table` is intentionally absent so its star stays unresolved.
}

# Descriptions used in the printed summary.  These mirror the record fields:
# - resolved: exactly one proven physical source
# - multiple: several proven sources (e.g. a UNION branch per source)
# - partial:  some proof plus an explicitly recorded gap
# - unknown:  nothing can be proven; no table is guessed
STATUS_NOTES = {
    "resolved": "one determined source",
    "multiple": "multiple evidence (one per UNION branch / expression input)",
    "partial": "partially determined; see unresolved reasons",
    "unknown": "undetermined; not guessed",
}


def main() -> None:
    """Run the trace and print a human-readable summary plus JSON."""
    records = sqlfluff.trace(
        SQL,
        dialect="ansi",
        column_metadata=COLUMN_METADATA,
    )

    print("=" * 78)
    print("Result columns in real output order")
    print("=" * 78)
    for record in records:
        ordinal = record["ordinal"]
        position = (
            f"ordinal {ordinal}"
            if record["position_known"]
            else f"ordinal unknown (>= {record['position_lower_bound']})"
        )
        print(f"\n[{record['sequence']}] name={record['name']!r} {position}")
        print(f"    status={record['status']} ({STATUS_NOTES[record['status']]})")
        if record["wildcard_range"]:
            print(
                f"    unexpanded wildcard range starting at {record['start_position']}"
            )
        for source in record["sources"]:
            via = " -> ".join(
                f"{step['table']}.{step['column']}" for step in source["via"]
            )
            suffix = f" via {via}" if via else ""
            computed = " (computed)" if source["computed"] else ""
            print(f"    source: {source['table']}.{source['column']}{computed}{suffix}")
        for item in record["unresolved"]:
            print(f"    unresolved: {item['reason']} ({item['detail']})")

    print("\n" + "=" * 78)
    print("JSON")
    print("=" * 78)
    print(json.dumps(records, indent=2))

    # The unknown star can be completed separately and the whole query
    # re-traced by real position.  Only the supplied order is used.
    completed = sqlfluff.trace(
        SQL,
        dialect="ansi",
        column_metadata=COLUMN_METADATA,
        wildcard_columns={"unknown_table": ["id", "note"]},
    )
    print("\n" + "=" * 78)
    print("After completing the unknown star's column order")
    print("=" * 78)
    for record in completed:
        print(
            f"[{record['sequence']}] {record['name']!r} "
            f"ordinal={record['ordinal']} status={record['status']}"
        )

    # The same trace can start from an existing parse tree instead of SQL
    # text, using the lower-level tracer.
    linter = sqlfluff.core.Linter(dialect="ansi")
    parsed = linter.parse_string("SELECT a.id FROM alpha AS a")
    tree_records = ColumnLineageTracer(
        config=linter.config, column_metadata={"alpha": ["id"]}
    ).trace_tree(parsed.tree)
    assert tree_records[0]["sources"][0]["table"] == "alpha"


if __name__ == "__main__":
    main()

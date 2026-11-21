# Column Lineage

SQLFluff can trace each **result column** of a query back to the physical
source table and column it comes from. This is intended for data warehouse
work, where a wide table is built from many upstream tables and every output
column needs a traceable source.

The analysis uses **only** the real parse tree of the SQL and the column
metadata you supply. It never connects to a database, and it never guesses a
table or column when the information is not available.

## Quick start

```python
import sqlfluff

records = sqlfluff.trace(
    """
    WITH first AS (SELECT s.id AS k FROM t1 AS s),
         second AS (SELECT s.id AS k FROM t2 AS s)
    SELECT first.k, second.k
    FROM first JOIN second ON first.k = second.k
    """,
    dialect="ansi",
    column_metadata={"t1": ["id"], "t2": ["id"]},
)

for record in records:
    print(record["name"], record["ordinal"], record["sources"])
```

Each record is a plain dictionary and the result is JSON serialisable. The
lower-level entry point is
`sqlfluff.utils.analysis.lineage.trace_column_lineage`, which can also trace an
already parsed tree with `ColumnLineageTracer.trace_tree()`.

## Metadata

Metadata describes the ordered columns of a physical table. Order matters:
stars expand in exactly the order supplied.

```python
column_metadata = {
    "schema.table": ["id", "name", "created_at"],
    # A list of mappings also works:
    "other_table": [{"name": "id"}, {"name": "value"}],
    # As does an explicit columns key, or a name -> type mapping:
    "third_table": {"columns": ["a", "b"]},
    "fourth_table": {"x": "int", "y": "text"},
}
```

A table with no metadata is treated as **unknown**. A star over it is left
unresolved, and no source is invented for it.

## Reading a record

| Field | Meaning |
| --- | --- |
| `name` | Output display name (duplicates are allowed). |
| `ordinal` | Real 1-based output position, or `None` when it cannot be proven. |
| `position_known` | Whether `ordinal` is proven. |
| `position_lower_bound` | Smallest possible ordinal when it is not proven. |
| `start_position` | For an unexpanded star, where its range starts. |
| `status` | `resolved`, `multiple`, `partial` or `unknown`. |
| `sources` | Proven physical sources, each with `table`, `column`, `table_alias`, `via` and `computed`. |
| `via` | Intermediate hops (CTE/subquery alias and column, with a scope label). |
| `expression` | Whether the output is computed rather than a bare column. |
| `wildcard` / `wildcard_range` | Whether this came from a star, and whether it is an unexpanded marker. |
| `unresolved` | Explicit reasons why something is not known. |

`status` values:

- **resolved** — exactly one proven source.
- **multiple** — several proven sources (for example one per `UNION` branch,
  or several inputs to an expression).
- **partial** — some sources are proven, but at least one gap is recorded.
- **unknown** — nothing is proven. No table is guessed.

## Scope rules

- CTE names resolve in the nearest enclosing `WITH` scope. A CTE column list
  such as `WITH c (x, y) AS (...)` renames outputs **by position**.
- The same table alias may appear in an inner and an outer query. Each
  `SELECT` owns its own `FROM` bindings, so the two never leak into each
  other. The `via` steps carry a `scope` label so identically named aliases
  remain distinguishable.
- Nested scopes shadow outer names; lookup stops at the nearest match.

## Stars

- A qualified star (`t.*`) expands using the exact column order from metadata,
  or the exact output order of a CTE or derived table.
- An unqualified star (`*`) expands each `FROM` source in order.
- A star with no metadata becomes a single **unresolved range** record:
  `sources` is empty, no table is named, and `position_known` is false.
- Later concrete outputs are still reported separately. Their sources may be
  fully proven, but their global `ordinal` stays unknown (only
  `position_lower_bound` is available) because the star's width is unknown.

### Completing an unknown star

To finish a previously unresolved star, supply its complete ordered columns and
re-trace. This is the supported way to "fill in the gap" — it still uses only
caller metadata.

```python
records = sqlfluff.trace(
    sql,
    dialect="ansi",
    column_metadata={"known": ["id", "name"]},
    wildcard_columns={"unknown_table": ["u1", "u2", "u3"]},
)
```

After this, every output has a real ordinal again and the later columns are
retraced by position.

## UNION / UNION ALL

Set branches are combined **strictly by output position**. A branch alias
never pairs a column with another branch: in

```sql
SELECT a.id AS x, a.val AS y FROM t1 AS a
UNION ALL
SELECT b.val AS x, b.id AS y FROM t2 AS b
```

position 1 draws from `t1.id` and `t2.val`, even though both branches call it
`x`. If any branch contains an unexpanded star, positions after the first
unknown range are explicitly marked unaligned rather than being paired by
alias.

## Guarantees

- Duplicate output names are never overwritten: each real output position is
  its own record.
- Unresolved columns are reported with a reason; a missing metadata entry never
  turns into a fabricated table.
- Only the parse tree and caller metadata are read. Nothing is fetched from a
  database.

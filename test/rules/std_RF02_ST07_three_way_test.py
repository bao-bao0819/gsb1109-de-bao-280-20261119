"""Executable regressions for RF02 and ST07 on a three-way reconciliation.

The query under test reconciles three tables with two consecutive
``FULL OUTER JOIN ... USING`` clauses over the same key. A single-row CTE
joined by a comma precedes the join chain, and a correlated subquery reuses
an outer table alias in the select list.

The test data deliberately encode every FULL OUTER edge case:

* key 1 exists in A and B;
* key 2 exists only in A and C;
* key 3 exists only in B and C;
* key 4 exists only in C;
* each side carries its own NULL-key orphan (NULLs never match in a USING /
  ``=`` join, so the three orphans stay as three separate rows);
* every status records its source table, so a wrong join condition shows up
  in the row multiset rather than only in the keys;
* C keeps a legacy batch key that simply swaps 2 and 3, so matching on it
  preserves the returned keys but mis-pairs the sourced statuses.

SQLite (>= 3.39 for FULL OUTER JOIN / USING) is used as an independent
execution oracle. The lint assertions use the two dialects the fixed rules
support here: ANSI and postgres. These tests pin behaviour only; no rule code
is changed.
"""

import sqlite3
from collections import Counter
from contextlib import closing
from typing import Optional

import pytest

from sqlfluff.core import FluffConfig, Linter
from sqlfluff.core.errors import SQLBaseError
from sqlfluff.core.types import ConfigMappingType

# FULL OUTER JOIN and the ``IS NOT DISTINCT FROM`` operator both landed in
# SQLite 3.39.0; skip the executable checks on older bundled runtimes.
pytestmark = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 39, 0),
    reason="FULL OUTER JOIN / IS NOT DISTINCT FROM require SQLite >= 3.39",
)

DIALECTS = ["ansi", "postgres"]

# Shared table shape and data. C.legacy_k swaps the current keys 2 and 3.
SETUP_SQL = """
CREATE TABLE a (k INTEGER, status TEXT);
CREATE TABLE b (k INTEGER, status TEXT);
CREATE TABLE c (k INTEGER, legacy_k INTEGER, status TEXT);
INSERT INTO a VALUES (1, 'a1'), (2, 'a2'), (NULL, 'an');
INSERT INTO b VALUES (1, 'b1'), (3, 'b3'), (NULL, 'bn');
INSERT INTO c VALUES
    (1, 1, 'c1'),
    (3, 2, 'c2'),
    (2, 3, 'c3'),
    (4, 4, 'c4'),
    (NULL, NULL, 'cn');
"""

# The full reconciliation. The inner source aliases B's key to ``kk`` so that
# a bare ``k`` inside the subquery can only resolve to the outer merged key;
# ``inner_select`` / ``inner_where`` distinguish the outer-reference and
# inner-shadowing RF02 variants.
FULL_TEMPLATE = """WITH seed AS (
    SELECT 1 AS guard
)
SELECT
    k,
    a.status AS a_status,
    b.status AS b_status,
    c.status AS c_status,
    (
        SELECT {inner_select}
        FROM (SELECT k AS kk, status FROM b) AS b
        WHERE {inner_where}
    ) AS inner_status
FROM seed,
    a
    FULL OUTER JOIN b USING (k)
    FULL OUTER JOIN c USING (k)
ORDER BY k IS NULL, k
"""

FULL_OUTER_REFERENCE = FULL_TEMPLATE.format(
    inner_select="status", inner_where="b.kk = k"
)

FULL_INNER_SHADOW = FULL_TEMPLATE.format(
    inner_select="b.status", inner_where="b.kk = b.kk"
)

# Same join chain without the scalar subquery, used as the pure reconciliation
# oracle for the ON rewrites.
FULL_PLAIN = """WITH seed AS (
    SELECT 1 AS guard
)
SELECT
    k,
    a.status AS a_status,
    b.status AS b_status,
    c.status AS c_status
FROM seed,
    a
    FULL OUTER JOIN b USING (k)
    FULL OUTER JOIN c USING (k)
ORDER BY k IS NULL, k
"""

# Isolation 1: drop the second join and read only qualified columns.
ISOLATION_ONE = """SELECT a.k AS a_k, b.k AS b_k, a.status, b.status
FROM (SELECT 1 AS guard) AS seed, a FULL OUTER JOIN b USING (k)
"""
ISOLATION_ONE_FIXED = """SELECT a.k AS a_k, b.k AS b_k, a.status, b.status
FROM (SELECT 1 AS guard) AS seed, a FULL OUTER JOIN b ON a.k = b.k
"""

# Isolation 2: complete the first join inside a derived table that keeps the
# merged key, leaving a single later USING to fix.
ISOLATION_TWO = """SELECT ab.k, ab.a_status, ab.b_status, c.status AS c_status
FROM (
    SELECT k, a.status AS a_status, b.status AS b_status
    FROM a FULL OUTER JOIN b USING (k)
) AS ab
FULL OUTER JOIN c USING (k)
"""
ISOLATION_TWO_FIXED = """SELECT ab.k, ab.a_status, ab.b_status, c.status AS c_status
FROM (
    SELECT k, a.status AS a_status, b.status AS b_status
    FROM a FULL OUTER JOIN b USING (k)
) AS ab
FULL OUTER JOIN c ON ab.k = c.k
"""

# A migration that converts only the first USING to ON but keeps the second.
# This must never be offered as a fix: it is invalid against the real data.
REJECTED_FIRST_ONLY = """WITH seed AS (SELECT 1 AS guard)
SELECT
    k,
    a.status AS a_status,
    b.status AS b_status,
    c.status AS c_status
FROM seed,
    a
    FULL OUTER JOIN b ON a.k = b.k
    FULL OUTER JOIN c USING (k)
ORDER BY k IS NULL, k
"""

# All-ON second-join contrasts. The first join is finished inside a derived
# table that exposes the merged key, so every variant is valid ON SQL that
# ST07/RF02 leave alone. They differ only in how the second join reaches C.
DERIVED_TEMPLATE = """WITH ab AS (
    SELECT
        COALESCE(a.k, b.k) AS k,
        a.status AS a_status,
        b.status AS b_status,
        b.k AS b_k
    FROM a FULL OUTER JOIN b ON a.k = b.k
)
SELECT
    COALESCE(ab.k, c.k) AS cur_k,
    ab.a_status,
    ab.b_status,
    c.status AS c_status
FROM ab FULL OUTER JOIN c {condition}
ORDER BY cur_k IS NULL, cur_k
"""

CORRECT_CONDITION = "ON ab.k = c.k"
B_KEY_ONLY_CONDITION = "ON ab.b_k = c.k"
NULL_MATCH_CONDITION = "ON ab.k IS NOT DISTINCT FROM c.k"
LEGACY_KEY_CONDITION = "ON ab.k = c.legacy_k"

# A legal direct-chain ON rewrite: explicit COALESCE keys and only qualified
# column references. It must be accepted by the rules and return the same
# result as the USING chain.
LEGAL_FULL_ON = """WITH seed AS (SELECT 1 AS guard)
SELECT
    COALESCE(a.k, b.k, c.k) AS cur_k,
    a.status AS a_status,
    b.status AS b_status,
    c.status AS c_status
FROM seed,
    a
    FULL OUTER JOIN b ON a.k = b.k
    FULL OUTER JOIN c ON COALESCE(a.k, b.k) = c.k
ORDER BY cur_k IS NULL, cur_k
"""

# The independently expected reconciliation: ordered columns, sourced-status
# row multiset and the three retained NULL orphans.
EXPECTED_COLUMNS = ["k", "a_status", "b_status", "c_status"]
# ON rewrites expose the key through an explicit COALESCE alias instead.
EXPECTED_CURRENT_KEY_COLUMNS = ["cur_k", "a_status", "b_status", "c_status"]
EXPECTED_ROWS = [
    (1, "a1", "b1", "c1"),
    (2, "a2", None, "c3"),
    (3, None, "b3", "c2"),
    (4, None, None, "c4"),
    (None, "an", None, None),
    (None, None, "bn", None),
    (None, None, None, "cn"),
]
EXPECTED_NULL_ORPHANS = [
    (None, "an", None, None),
    (None, None, "bn", None),
    (None, None, None, "cn"),
]

# The correlated subquery's value, aligned with EXPECTED_ROWS by key. With the
# genuine outer reference it mirrors B's status for B-present rows and is NULL
# otherwise, so it differs from the outer ``b_status`` on the A/C/NULL rows.
OUTER_INNER_STATUS = ["b1", None, "b3", None, None, None, None]
FULL_OUTER_REFERENCE_ROWS = [
    row + (inner,) for row, inner in zip(EXPECTED_ROWS, OUTER_INNER_STATUS)
]
# The shadowing form never reaches the outer query, so its self-contained
# predicate matches B's first row and returns a constant for every outer row.
FULL_INNER_SHADOW_ROWS = [row + ("b1",) for row in EXPECTED_ROWS]

# What each mistaken second-join condition actually returns.
B_KEY_ONLY_ROWS = [
    (1, "a1", "b1", "c1"),
    (2, "a2", None, None),
    (2, None, None, "c3"),
    (3, None, "b3", "c2"),
    (4, None, None, "c4"),
    (None, "an", None, None),
    (None, None, "bn", None),
    (None, None, None, "cn"),
]
NULL_MATCH_ROWS = [
    (1, "a1", "b1", "c1"),
    (2, "a2", None, "c3"),
    (3, None, "b3", "c2"),
    (4, None, None, "c4"),
    (None, "an", None, "cn"),
    (None, None, "bn", "cn"),
]
# Keys and NULL counts are identical to the expected multiset; only the C
# statuses of keys 2 and 3 are swapped.
LEGACY_KEY_ROWS = [
    (1, "a1", "b1", "c1"),
    (2, "a2", None, "c2"),
    (3, None, "b3", "c3"),
    (4, None, None, "c4"),
    (None, "an", None, None),
    (None, None, "bn", None),
    (None, None, None, "cn"),
]

# Independent expectation for isolation one (A FULL OUTER B only): the shared
# key matches, the two single-side keys stay, and both NULL orphans survive.
ISOLATION_ONE_ROWS = [
    (1, 1, "a1", "b1"),
    (2, None, "a2", None),
    (None, 3, None, "b3"),
    (None, None, "an", None),
    (None, None, None, "bn"),
]

# Independent expectation for isolation two. The derived table only carries
# A/B keys, so the C-only key 4 reaches the result through ``c`` while the
# projected bare ``ab.k`` is NULL there; the three NULL orphans are retained.
ISOLATION_TWO_ROWS = [
    (1, "a1", "b1", "c1"),
    (2, "a2", None, "c3"),
    (3, None, "b3", "c2"),
    (None, "an", None, None),
    (None, None, "bn", None),
    (None, None, None, "c4"),
    (None, None, None, "cn"),
]


@pytest.fixture()
def connection() -> sqlite3.Connection:
    """Provide a fresh in-memory database loaded with the reconciliation data."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(SETUP_SQL)
        yield conn


def _fetch(connection: sqlite3.Connection, sql: str) -> tuple[list[str], list[tuple]]:
    cursor = connection.execute(sql)
    columns = [description[0] for description in cursor.description]
    return columns, cursor.fetchall()


def _linter(
    dialect: str, rules: list[str], configs: Optional[ConfigMappingType] = None
) -> Linter:
    return Linter(
        config=FluffConfig(
            configs=configs,
            overrides={"dialect": dialect, "rules": ",".join(rules)},
        )
    )


def _diagnostics(
    dialect: str,
    sql: str,
    rules: list[str],
    configs: Optional[ConfigMappingType] = None,
) -> list[SQLBaseError]:
    return _linter(dialect, rules, configs).lint_string(sql).violations


def _fixed(
    dialect: str,
    sql: str,
    rules: list[str],
    configs: Optional[ConfigMappingType] = None,
) -> str:
    return _linter(dialect, rules, configs).lint_string(sql, fix=True).fix_string()[0]


def test_data_encodes_the_three_way_distribution(
    connection: sqlite3.Connection,
) -> None:
    """The fixture data really has the key distribution the comments claim."""
    non_null = "SELECT k FROM {table} WHERE k IS NOT NULL"
    keys_a = {row[0] for row in connection.execute(non_null.format(table="a"))}
    keys_b = {row[0] for row in connection.execute(non_null.format(table="b"))}
    keys_c = {row[0] for row in connection.execute(non_null.format(table="c"))}
    assert keys_a == {1, 2}
    assert keys_b == {1, 3}
    assert keys_c == {1, 2, 3, 4}
    # One NULL-key orphan per side.
    null_count = "SELECT COUNT(*) FROM {table} WHERE k IS NULL"
    for table in ("a", "b", "c"):
        assert connection.execute(null_count.format(table=table)).fetchone() == (1,)
    # The legacy batch key only swaps current keys 2 and 3.
    legacy = dict(
        connection.execute("SELECT k, legacy_k FROM c WHERE k IS NOT NULL").fetchall()
    )
    assert legacy == {1: 1, 2: 3, 3: 2, 4: 4}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_full_query_two_unfixable_using_diagnostics(
    connection: sqlite3.Connection, dialect: str
) -> None:
    """Both USING clauses are flagged in place but neither is fixed."""
    violations = _diagnostics(dialect, FULL_OUTER_REFERENCE, ["ST07"])
    assert [v.line_no for v in violations] == [16, 17]
    assert all(v.rule_code() == "ST07" for v in violations)
    assert all(not v.fixes for v in violations)
    # Fixing must leave the statement byte-for-byte unchanged.
    assert _fixed(dialect, FULL_OUTER_REFERENCE, ["ST07"]) == FULL_OUTER_REFERENCE
    # The query still executes and returns the expected reconciliation.
    columns, rows = _fetch(connection, FULL_OUTER_REFERENCE)
    assert columns == EXPECTED_COLUMNS + ["inner_status"]
    assert Counter(rows) == Counter(FULL_OUTER_REFERENCE_ROWS)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_full_query_rf02_external_reference_positions(
    connection: sqlite3.Connection, dialect: str
) -> None:
    """RF02 reports the two external references at their original positions."""
    violations = _diagnostics(dialect, FULL_OUTER_REFERENCE, ["RF02"])
    assert [(v.line_no, v.line_pos, v.desc()) for v in violations] == [
        (
            10,
            16,
            "Unqualified reference 'status' found in select with more than one "
            "referenced table/view.",
        ),
        (
            12,
            22,
            "Unqualified reference 'k' found in select with more than one "
            "referenced table/view.",
        ),
    ]
    assert all(not v.fixes for v in violations)
    # The same statement still runs: the lint diagnostics do not rewrite it.
    _fetch(connection, FULL_OUTER_REFERENCE)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_rf02_outer_reference_ignored_by_config(dialect: str) -> None:
    """Switching on the ignore option silences the genuine outer references."""
    configs = {
        "rules": {
            "references.qualification": {
                "subqueries_ignore_external_references": True
            }
        }
    }
    assert _diagnostics(dialect, FULL_OUTER_REFERENCE, ["RF02"], configs) == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_rf02_inner_shadowing_is_clean(
    connection: sqlite3.Connection, dialect: str
) -> None:
    """Qualified inner references shadow the outer name and never get flagged."""
    assert _diagnostics(dialect, FULL_INNER_SHADOW, ["RF02"]) == []
    configs = {
        "rules": {
            "references.qualification": {
                "subqueries_ignore_external_references": True
            }
        }
    }
    assert _diagnostics(dialect, FULL_INNER_SHADOW, ["RF02"], configs) == []
    # The two variants are distinct at runtime: the shadowed form returns a
    # constant inner value while the outer-reference form correlates. Each is
    # checked against its own independently computed oracle.
    _, shadow_rows = _fetch(connection, FULL_INNER_SHADOW)
    assert Counter(shadow_rows) == Counter(FULL_INNER_SHADOW_ROWS)
    _, outer_rows = _fetch(connection, FULL_OUTER_REFERENCE)
    assert Counter(outer_rows) == Counter(FULL_OUTER_REFERENCE_ROWS)
    assert Counter(shadow_rows) != Counter(outer_rows)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_isolation_one_fix_joins_the_two_tables_of_this_join(
    connection: sqlite3.Connection, dialect: str
) -> None:
    """The first USING fix pairs the comma-join base ``a`` with ``b``."""
    fixed = _fixed(dialect, ISOLATION_ONE, ["ST07"])
    assert fixed == ISOLATION_ONE_FIXED
    assert "ON a.k = b.k" in fixed
    assert "USING" not in fixed
    assert _diagnostics(dialect, fixed, ["ST07"]) == []
    # Independent oracle: the fixed join still returns the A/B reconciliation.
    columns, fixed_rows = _fetch(connection, fixed)
    assert columns == ["a_k", "b_k", "status", "status"]
    assert Counter(fixed_rows) == Counter(ISOLATION_ONE_ROWS)
    # Original and fixed statements return identical columns and rows.
    assert _fetch(connection, ISOLATION_ONE) == _fetch(connection, fixed)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_isolation_two_fix_joins_derived_table_to_c(
    connection: sqlite3.Connection, dialect: str
) -> None:
    """The isolated second USING is fixed onto ``ab`` and ``c`` only."""
    fixed = _fixed(dialect, ISOLATION_TWO, ["ST07"])
    assert fixed == ISOLATION_TWO_FIXED
    assert "ON ab.k = c.k" in fixed
    # The inner USING remains as an unfixable diagnostic.
    residual = _diagnostics(dialect, fixed, ["ST07"])
    assert len(residual) == 1
    assert not residual[0].fixes
    # Independent oracle: the fixed second join reproduces the derived
    # reconciliation, including key 4 and all three NULL orphans.
    columns, fixed_rows = _fetch(connection, fixed)
    assert columns == EXPECTED_COLUMNS
    assert Counter(fixed_rows) == Counter(ISOLATION_TWO_ROWS)
    assert _fetch(connection, ISOLATION_TWO) == _fetch(connection, fixed)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_converting_only_the_first_using_is_not_offered_and_is_invalid(
    connection: sqlite3.Connection, dialect: str
) -> None:
    """A first-USING-only rewrite is unfixable and fails against real data."""
    violations = _diagnostics(dialect, REJECTED_FIRST_ONLY, ["ST07"])
    assert len(violations) == 1
    assert not violations[0].fixes
    assert _fixed(dialect, REJECTED_FIRST_ONLY, ["ST07"]) == REJECTED_FIRST_ONLY
    # The bare merged key is ambiguous once the first join is ON, so the
    # rejected statement does not execute at all.
    with pytest.raises(sqlite3.OperationalError, match="ambiguous"):
        connection.execute(REJECTED_FIRST_ONLY).fetchall()


def test_correct_on_second_join_matches_using_oracle(
    connection: sqlite3.Connection,
) -> None:
    """The correct ON contrast reproduces the full USING reconciliation."""
    sql = DERIVED_TEMPLATE.format(condition=CORRECT_CONDITION)
    for dialect in DIALECTS:
        assert _diagnostics(dialect, sql, ["ST07", "RF02"]) == []
    columns, rows = _fetch(connection, sql)
    assert columns == EXPECTED_CURRENT_KEY_COLUMNS
    assert Counter(rows) == Counter(EXPECTED_ROWS)
    assert Counter(row for row in rows if row[0] is None) == Counter(
        EXPECTED_NULL_ORPHANS
    )


def test_b_key_only_contrast_duplicates_a_key(connection: sqlite3.Connection) -> None:
    """Joining C on B's key alone duplicates key 2 and drops its C pairing."""
    sql = DERIVED_TEMPLATE.format(condition=B_KEY_ONLY_CONDITION)
    for dialect in DIALECTS:
        assert _diagnostics(dialect, sql, ["ST07", "RF02"]) == []
    columns, rows = _fetch(connection, sql)
    assert columns == EXPECTED_CURRENT_KEY_COLUMNS
    assert Counter(rows) == Counter(B_KEY_ONLY_ROWS)
    # Explicit, rerunnable diff against the independent expectation.
    assert Counter(rows) != Counter(EXPECTED_ROWS)
    assert [row[0] for row in rows].count(2) == 2
    mismatched = [
        row for row in rows if row in B_KEY_ONLY_ROWS and row not in EXPECTED_ROWS
    ]
    assert (2, "a2", None, None) in mismatched
    assert (2, None, None, "c3") in mismatched


def test_null_match_contrast_merges_the_null_orphans(
    connection: sqlite3.Connection,
) -> None:
    """Letting NULLs match each other collapses the three NULL orphans."""
    sql = DERIVED_TEMPLATE.format(condition=NULL_MATCH_CONDITION)
    for dialect in DIALECTS:
        assert _diagnostics(dialect, sql, ["ST07", "RF02"]) == []
    columns, rows = _fetch(connection, sql)
    assert columns == EXPECTED_CURRENT_KEY_COLUMNS
    assert Counter(rows) == Counter(NULL_MATCH_ROWS)
    assert Counter(rows) != Counter(EXPECTED_ROWS)
    # The A and B orphans now wrongly inherit C's NULL-row status.
    null_rows = [row for row in rows if row[0] is None]
    assert Counter(null_rows) == Counter(
        [(None, "an", None, "cn"), (None, None, "bn", "cn")]
    )


def test_legacy_key_contrast_keeps_keys_but_swaps_sourced_statuses(
    connection: sqlite3.Connection,
) -> None:
    """The legacy-key trap is invisible to key-only comparisons.

    The returned keys, their multiplicities and the NULL orphans are
    identical to the expected reconciliation; only the C statuses paired with
    keys 2 and 3 are swapped. Comparing keys (even with duplicate counts)
    therefore passes while the full row multiset fails.
    """
    sql = DERIVED_TEMPLATE.format(condition=LEGACY_KEY_CONDITION)
    for dialect in DIALECTS:
        assert _diagnostics(dialect, sql, ["ST07", "RF02"]) == []
    columns, rows = _fetch(connection, sql)
    assert columns == EXPECTED_CURRENT_KEY_COLUMNS

    # Key-only comparison, including multiplicities, wrongly passes.
    assert Counter(row[0] for row in rows) == Counter(row[0] for row in EXPECTED_ROWS)
    assert Counter(row for row in rows if row[0] is None) == Counter(
        EXPECTED_NULL_ORPHANS
    )

    # The full sourced-status multiset is where the mismatch surfaces:
    # key 2 carries c2 instead of c3, key 3 carries c3 instead of c2.
    assert Counter(rows) != Counter(EXPECTED_ROWS)
    assert Counter(rows) == Counter(LEGACY_KEY_ROWS)
    by_key = {row[0]: row for row in rows if row[0] in (2, 3)}
    assert by_key[2] == (2, "a2", None, "c2")
    assert by_key[3] == (3, None, "b3", "c3")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_legal_coalesce_on_chain_matches_using_oracle(
    connection: sqlite3.Connection, dialect: str
) -> None:
    """An explicit COALESCE-key, fully-qualified ON rewrite is accepted and equal."""
    assert _diagnostics(dialect, LEGAL_FULL_ON, ["ST07", "RF02"]) == []
    columns, on_rows = _fetch(connection, LEGAL_FULL_ON)
    assert columns == EXPECTED_CURRENT_KEY_COLUMNS
    _, using_rows = _fetch(connection, FULL_PLAIN)
    # Rerunnable oracle check: a future rule change that drifts from the
    # independent expectation fails here with both multisets visible.
    assert Counter(on_rows) == Counter(using_rows) == Counter(EXPECTED_ROWS)

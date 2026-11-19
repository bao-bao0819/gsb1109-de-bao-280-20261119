#!/usr/bin/env python3
"""Reproduce Beanquery price-date lookup and inverse-quote ordering.

Run with the project virtualenv:

    .venv/bin/python repro/reproduce.py

Each scenario loads one minimal ledger and reports, for the 100 AAA position
carried at cost 10.00 CNY:

  * the rate returned by getprice('AAA', 'CNY', <date>)
  * the market value returned by value(sum(position), <date>)

Three query modes are compared:

  explicit   getprice/value receive 2026-10-31 explicitly
  default    neither function receives a date (Beanquery uses the latest price)
  close      no date, plus FROM ... CLOSE ON 2026-10-31 (only truncates entries)

The script asserts every observed value, so a non-zero exit status means the
behavior changed.
"""

from __future__ import annotations

import pathlib
import sys
import textwrap
from decimal import Decimal

import beanquery
from beancount import loader
from beancount.core import data, prices
from beanquery import query

HERE = pathlib.Path(__file__).resolve().parent

EXPLICIT = """
    SELECT getprice('AAA', 'CNY', 2026-10-31) AS rate,
           value(sum(position), 2026-10-31) AS mv
    FROM postings
    WHERE account = 'Assets:Broker'
"""

DEFAULT = """
    SELECT getprice('AAA', 'CNY') AS rate,
           value(sum(position)) AS mv
    FROM postings
    WHERE account = 'Assets:Broker'
"""

CLOSE = """
    SELECT getprice('AAA', 'CNY') AS rate,
           value(sum(position)) AS mv
    FROM postings CLOSE ON 2026-10-31
    WHERE account = 'Assets:Broker'
"""

MODES = (("explicit 2026-10-31", EXPLICIT), ("no date", DEFAULT), ("no date + CLOSE", CLOSE))

# (rate, value currency amount) for each mode.
EXPECTED = {
    "minimal-oct-only.bean": {
        "explicit 2026-10-31": ("10.00", "1000.00", "CNY"),
        "no date": ("10.00", "1000.00", "CNY"),
        "no date + CLOSE": ("10.00", "1000.00", "CNY"),
    },
    "minimal.bean": {
        "explicit 2026-10-31": ("10.00", "1000.00", "CNY"),
        "no date": ("12.00", "1200.00", "CNY"),
        "no date + CLOSE": ("12.00", "1200.00", "CNY"),
    },
    "minimal-two-sources.bean": {
        "explicit 2026-10-31": ("10", "1000", "CNY"),
        "no date": ("10", "1000", "CNY"),
        "no date + CLOSE": ("10", "1000", "CNY"),
    },
    "minimal-two-sources-today.bean": {
        "explicit 2026-10-31": ("12.5", "1250.0", "CNY"),
        "no date": ("12.00", "1200.00", "CNY"),
        "no date + CLOSE": ("12.00", "1200.00", "CNY"),
    },
    "minimal-two-sources-forward.bean": {
        "explicit 2026-10-31": ("12.5", "1250.0", "CNY"),
        "no date": ("12.5", "1250.0", "CNY"),
        "no date + CLOSE": ("12.5", "1250.0", "CNY"),
    },
    "minimal-two-sources-forward-today.bean": {
        "explicit 2026-10-31": ("12.5", "1250.0", "CNY"),
        "no date": ("12.00", "1200.00", "CNY"),
        "no date + CLOSE": ("12.00", "1200.00", "CNY"),
    },
}

# Archived reports: (report name, rate, value number, value currency).
EXPECTED_ARCHIVED = {
    "archived-before.bean": [("month_end", "10", "1000", "CNY"), ("month_end_pinned", "10", "1000", "CNY")],
    "archived-after.bean": [("month_end", "12.00", "1200.00", "CNY"), ("month_end_pinned", "12.5", "1250.0", "CNY")],
}


def load_path(path: pathlib.Path):
    entries, errors, options = loader.load_file(path)
    if errors:
        raise RuntimeError(f"{path.name}: {errors}")
    return entries, options


def run_sql(path: pathlib.Path, sql: str):
    entries, options = load_path(path)
    _, rows = query.run_query(entries, options, textwrap.dedent(sql))
    rate, mv = rows[0]
    # value() on an inventory returns an Inventory; unwrap its single position.
    if hasattr(mv, "get_only_position"):
        mv = mv.get_only_position().units
    return rate, mv.number, mv.currency


def quote_summary(path: pathlib.Path):
    """Count source quotes per direction before the inverse merge."""
    entries, _ = load_path(path)
    forward = [e for e in entries if isinstance(e, data.Price) and (e.currency, e.amount.currency) == ("AAA", "CNY")]
    reverse = [e for e in entries if isinstance(e, data.Price) and (e.currency, e.amount.currency) == ("CNY", "AAA")]
    return forward, reverse


def merged(path: pathlib.Path):
    entries, _ = load_path(path)
    pm = prices.build_price_map(entries)
    return pm.forward_pairs, pm[("AAA", "CNY")], pm[("CNY", "AAA")]


def fmt_quotes(label: str, quotes):
    body = ", ".join(f"{q.date} {q.amount.number} {q.amount.currency}" for q in quotes) or "-"
    return f"{label}: {len(quotes)} [{body}]"


def scenario(path: pathlib.Path):
    forward, reverse = quote_summary(path)
    forward_pairs, aaa_cny, cny_aaa = merged(path)
    print(f"\n{path.name}")
    print("  source prices")
    print(f"    {fmt_quotes('AAA->CNY', forward)}")
    print(f"    {fmt_quotes('CNY->AAA', reverse)}")
    print(f"  merged forward_pairs: {forward_pairs}")
    print(f"  merged AAA->CNY: {aaa_cny}")
    print(f"  merged CNY->AAA: {cny_aaa}")

    failures = []
    for mode, sql in MODES:
        rate, number, currency = run_sql(path, sql)
        expected = EXPECTED[path.name][mode]
        ok = (rate, number, currency) == tuple(Decimal(x) if i < 2 else x for i, x in enumerate(expected))
        print(f"    {mode:<20} rate={rate!s:<8} value={number} {currency}  {'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append((path.name, mode, expected, (rate, number, currency)))
    return failures


def run_named(path: pathlib.Path) -> list[tuple[str, Decimal, Decimal, str]]:
    """Replay Query directives exactly like the interactive shell does."""
    entries, errors, options = loader.load_file(path)
    context = beanquery.connect("beancount:", entries=entries, errors=errors, options=options)
    results = []
    for entry in entries:
        if not isinstance(entry, data.Query):
            continue
        statement = context.parse(entry.query_string)
        if not statement.from_clause.close:
            statement.from_clause.close = entry.date
        rate, mv = context.execute(statement).fetchall()[0]
        if hasattr(mv, "get_only_position"):
            mv = mv.get_only_position().units
        results.append((entry.name, rate, mv.number, mv.currency))
    return results


def archived(path: pathlib.Path) -> list:
    print(f"\n{path.name}")
    failures = []
    for name, rate, number, currency in run_named(path):
        expected = next(item for item in EXPECTED_ARCHIVED[path.name] if item[0] == name)
        ok = (name, rate, number, currency) == (name, Decimal(expected[1]), Decimal(expected[2]), expected[3])
        print(f"    {name:<20} rate={rate!s:<8} value={number} {currency}  {'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append((path.name, name, expected, (name, rate, number, currency)))
    return failures


def main() -> int:
    print("=" * 78)
    print("Part 1 - the same minimal ledger, three query modes")
    print("=" * 78)
    failures = []
    for name in EXPECTED:
        failures += scenario(HERE / name)

    print()
    print("=" * 78)
    print("Part 2 - an archived named report re-run after today's price is appended")
    print("=" * 78)
    for name in EXPECTED_ARCHIVED:
        failures += archived(HERE / name)

    print()
    if failures:
        print("FAILED")
        for failure in failures:
            print(" ", failure)
        return 1
    print("All observed values match the documented expectations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

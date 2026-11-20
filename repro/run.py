#!/usr/bin/env python3
"""Reproduce the Datasette streaming CSV truncation by capturing real HTTP.

Run from the repository root:

    .venv/bin/python -m repro.run            # human readable
    .venv/bin/python -m repro.run --json     # machine readable
    .venv/bin/python -m repro.run --raw DIR  # also write the wire bytes

Exit status is 0 when every asserted expectation holds. Nothing is downloaded
over a network socket: uvicorn's own h11 protocol object is driven with the
request bytes and records what it writes (see repro/httpwire.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

from .diagnostics import diagnose
from .fixtures import PAGE_SIZE, ROW_COUNT, make_datasette, prepare
from .httpwire import capture
from .receiver import receive
from .scenarios import PARTIAL_KEYS, SCENARIOS
from .server_side import page_next, probe_renderer


async def capture_all(raw_dir: pathlib.Path | None):
    results = {}
    for item in SCENARIOS:
        ds = make_datasette(max_stream_mb=item.max_stream_mb)
        await prepare(ds)
        try:
            wire = await capture(ds.app(), item.path)
        finally:
            ds.close()
        report = diagnose(item, wire)
        results[item.key] = report
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            stem = raw_dir / item.key
            stem.with_suffix(".http").write_bytes(wire.raw)
            # Decoded payload, for tools that want the CSV without chunk framing
            stem.with_suffix(".csv").write_bytes(wire.body)
    results["_page_next"] = await probe_page_next()
    return results


def probe_path(csv_path: str) -> str:
    """Change only the .csv extension in the path, before any query string."""
    base, separator, query = csv_path.partition("?")
    base = base[: -len(".csv")] + ".probe"
    return base + separator + query


async def probe_page_next():
    """Ask a probe renderer what page['next'] the CSV renderer throws away."""
    probes = {}
    with probe_renderer():
        for item in SCENARIOS:
            ds = make_datasette(max_stream_mb=item.max_stream_mb)
            await prepare(ds)
            try:
                probes[item.key] = await page_next(ds, probe_path(item.path))
            finally:
                ds.close()
    return probes


def check(results) -> list[str]:
    failures = []
    short = results["complete_short"]
    long_ = results["complete_long"]
    first = results["fail_first_page"]
    timeout = results["timeout_next_page"]
    limit = results["size_limit"]
    empty = results["size_limit_before_any_body"]

    if not (short.wire.complete and short.ends_on_record_boundary):
        failures.append("complete_short should be a clean, terminated response")
    if not (long_.wire.complete and long_.ends_on_record_boundary):
        failures.append("complete_long should be a clean, terminated response")
    if long_.record_count != ROW_COUNT:
        failures.append(f"complete_long should have {ROW_COUNT} rows")
    if len(long_.wire.body_chunks) != 2:
        failures.append("complete_long should flush two pages")
    if short.record_count != PAGE_SIZE:
        failures.append("complete_short should have one page of rows")

    if first.wire.status == 200:
        failures.append("fail_first_page must not start a 200 response")
    if first.wire.status != 400 or not first.wire.complete:
        failures.append("fail_first_page should be a complete 400 error")
    if first.parse.records:
        failures.append("fail_first_page should not contain CSV records")

    if not empty.wire.response_started or empty.wire.status != 200:
        failures.append("size_limit_before_any_body should have started a 200")
    if empty.wire.complete or empty.wire.body:
        failures.append("size_limit_before_any_body should close before any body")
    if empty.record_count != 0:
        failures.append("size_limit_before_any_body should expose no records")

    for partial in (timeout, limit):
        if not partial.wire.response_started or partial.wire.status != 200:
            failures.append(f"{partial.scenario} should have started a 200")
        if partial.wire.complete:
            failures.append(f"{partial.scenario} must not end cleanly")
        if partial.wire.client_error is None:
            failures.append(f"{partial.scenario} needs a transport error")
        if partial.wire.body != short.wire.body:
            failures.append(
                f"{partial.scenario} first-page bytes should equal complete_short"
            )
        if partial.wire.headers != short.wire.headers:
            failures.append(
                f"{partial.scenario} headers should equal complete_short"
            )
        if not partial.ends_on_record_boundary:
            failures.append(
                f"{partial.scenario} body should end exactly on a record boundary"
            )
        if partial.record_count != PAGE_SIZE:
            failures.append(f"{partial.scenario} should expose one page of rows")

    if timeout.parse.records != short.parse.records:
        failures.append("timeout first page records should equal complete_short")
    if limit.parse.records != short.parse.records:
        failures.append("size-limit first page records should equal complete_short")
    return failures


def print_human(results):
    print("=" * 78)
    print("Datasette streaming CSV - completion vs truncation evidence")
    print("=" * 78)
    for item in SCENARIOS:
        report = results[item.key]
        d = report.as_dict()
        print(f"\n## {item.key}: {item.title}")
        print(f"   GET {item.path}")
        print(
            "   HTTP     status={status} content-length={cl} transfer-encoding={te}".format(
                status=d["http"]["status"],
                cl=d["http"]["content_length"],
                te=d["http"]["transfer_encoding"],
            )
        )
        print(f"   Link     {d['http']['link']!r}")
        print(
            "   Wire     complete={c} chunks={chunks} sizes={sizes}".format(
                c=d["transport"]["client_saw_complete_http_message"],
                chunks=d["transport"]["wire_chunks"],
                sizes=d["transport"]["chunk_sizes"],
            )
        )
        print(f"            error={d['transport']['client_error']!r}")
        print(
            "   CSV      records={r} data_rows={rows} boundary={b} residual={res}B".format(
                r=d["csv"]["complete_records_including_header"],
                rows=d["csv"]["data_rows"],
                b=d["csv"]["ends_on_record_boundary"],
                res=d["csv"]["residual_bytes"],
            )
        )
        print(f"   sha256   {d['csv']['sha256_of_all_body_bytes']}")
        print(f"   expected {item.expectation}")

    print("\n" + "-" * 78)
    print("Same receiver, same bytes: normal short download vs aborted stream")
    print("-" * 78)
    short = results["complete_short"]
    for key in PARTIAL_KEYS:
        partial = results[key]
        print(f"\n{key}")
        print(
            "  body identical: {body}   headers identical: {headers}".format(
                body=partial.wire.body == short.wire.body,
                headers=partial.wire.headers == short.wire.headers,
            )
        )
        for policy in ("trusting", "strict"):
            rows: list[tuple[str, ...]] = []
            good = receive(short.wire, policy=policy, into=rows)
            bad = receive(partial.wire, policy=policy, into=rows)
            print(
                f"  {policy:8} short: committed={good.committed} rows={good.rows} "
                f"({good.reason})"
            )
            print(
                f"  {policy:8} {key}: committed={bad.committed} rows={bad.rows} "
                f"({bad.reason})"
            )

    print("\n" + "-" * 78)
    print("Server-side cursor, as passed to an output renderer (not to CSV)")
    print("-" * 78)
    for item in SCENARIOS:
        value = results["_page_next"][item.key]
        if value.startswith("<"):
            value = "<error response: the query failed, as expected>"
        print(f"  {item.key:28} page['next'] = {value}")
    print("  (the CSV renderer never emits this value)")

    print("\n" + "-" * 78)
    print("What the transport proves")
    print("-" * 78)
    for label, report in (
        ("complete_short", results["complete_short"]),
        ("complete_long", results["complete_long"]),
        ("timeout_next_page", results["timeout_next_page"]),
        ("size_limit", results["size_limit"]),
        ("size_limit_before_any_body", results["size_limit_before_any_body"]),
    ):
        print(
            f"  {label:18} final chunk seen={report.wire.complete!s:5} "
            f"chunks={len(report.wire.body_chunks)}"
        )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--raw", type=pathlib.Path)
    args = parser.parse_args(argv)

    results = asyncio.run(capture_all(args.raw))
    failures = check(results)
    if args.json:
        payload = {
            "scenarios": [results[item.key].as_dict() for item in SCENARIOS],
            "checks_failed": failures,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_human(results)
        print()
        if failures:
            print("FAILED CHECKS:")
            for failure in failures:
                print(f"  - {failure}")
        else:
            print("All asserted expectations held.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

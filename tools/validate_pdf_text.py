#!/usr/bin/env python3
"""Validate converted statement text without touching the original PDF.

This is stage two of the PDF import diagnostics.  It runs entirely on
the ``.txt`` produced by ``tools/pdf_extract.py`` (pages separated by
form feeds, ``\\f``) and separates two fundamentally different kinds of
null values:

* ``conversion`` findings: the converter returned no usable text for a
  page (zero alphanumeric characters).  No field rule could recover data
  that is not in the text; the fix belongs to PDF conversion/OCR.
* ``recognition`` findings: text is present, but a field rule did not
  match or rejected the value.  These mirror real importer behavior,
  for example ``re.search("Date: ...")`` returning nothing in
  ``examples/importers/acme.py`` or ``decimal.Decimal("")`` failing on an
  empty amount column in ``beangulp/importers/csvbase.py``.

A ``.txt.json`` sidecar written by the converter is used when present
but is not required, so the check can be re-run on any converted text.
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

# Document-level rules, mirroring the regexes used by examples/importers/acme.py.
BANK_HEADER_RE = re.compile(r"^(ACME|DEMO) Bank", re.MULTILINE)
DOC_DATE_RE = re.compile(r"^Date:\s*(.*)$", re.MULTILINE)
DOC_BALANCE_RE = re.compile(r"^Balance:\s*([-$\d,.(]+)\s*$", re.MULTILINE)

# A typical statement transaction row: posting date, description, amount.
TXN_ROW_RE = re.compile(
    r"^\s*(\d{1,2}/\d{1,2}/\d{2,4})"  # group 1: posting date
    r"\s+(.*?)"  # group 2: description
    r"\s+([-(]?\$?[\d,]+(?:\.\d{2})?\)?)\s*$",  # group 3: amount
    re.MULTILINE,
)
DATE_PREFIX_RE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{2,4})\b", re.MULTILINE)
AMOUNT_RE = re.compile(r"[-($]?[\d,]+(?:\.\d{2})?\)?$")
DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y")

try:
    # acme.py parses document dates with dateutil, which accepts
    # "September 30, 2015" style values. Use it when available.
    from dateutil.parser import parse as parse_datetime
except ImportError:  # pragma: no cover
    parse_datetime = None


def parse_row_date(value):
    """Strict row-date parser, mirroring csvbase.Date with an explicit format."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_doc_date(value):
    """Lenient document-date parser, mirroring acme.py's dateutil call."""
    value = value.strip()
    if parse_datetime is not None:
        try:
            return parse_datetime(value).date()
        except (ValueError, OverflowError, TypeError):
            return None
    return parse_row_date(value)


def split_pages(text):
    pages = text.split("\f")
    if pages and pages[-1] == "":
        pages.pop()
    return pages


def load_sidecar(txt_path):
    sidecar_path = txt_path.with_suffix(".txt.json")
    if sidecar_path.exists():
        return json.loads(sidecar_path.read_text(encoding="utf-8"))
    return None


def finding(layer, page, field, message, evidence=None):
    entry = {"layer": layer, "page": page, "field": field, "message": message}
    if evidence is not None:
        entry["evidence"] = evidence
    return entry


def check_page(page_number, page_text, conversion_empty):
    findings = []
    if conversion_empty:
        findings.append(
            finding(
                "conversion",
                page_number,
                "*",
                "page has no extractable text; every field on this page is a "
                "conversion null, not a field-rule null",
            )
        )
        return findings

    # Document-level rules only apply to the first page, as in acme.py.
    if page_number == 1:
        if not BANK_HEADER_RE.search(page_text):
            findings.append(
                finding(
                    "recognition",
                    page_number,
                    "identify",
                    "bank header pattern not found; identify() would ignore this file",
                )
            )
        match = DOC_DATE_RE.search(page_text)
        if not match or not match.group(1).strip():
            findings.append(
                finding(
                    "recognition",
                    page_number,
                    "date",
                    "'Date:' pattern absent or empty; importer.date() would return None",
                )
            )
        elif parse_doc_date(match.group(1)) is None:
            findings.append(
                finding(
                    "recognition",
                    page_number,
                    "date",
                    "date text present but unparseable",
                    evidence=match.group(1).strip(),
                )
            )
        if not DOC_BALANCE_RE.search(page_text):
            findings.append(
                finding(
                    "recognition",
                    page_number,
                    "balance",
                    "'Balance:' pattern absent or amount not recognized",
                )
            )

    # Transaction row rules. Rows starting with a date are statement entries;
    # each is checked like the csvbase Column/Amount descriptors.
    for line in page_text.splitlines():
        if not DATE_PREFIX_RE.match(line):
            continue
        row = TXN_ROW_RE.match(line)
        if row is None:
            findings.append(
                finding(
                    "recognition",
                    page_number,
                    "transaction_row",
                    "date-prefixed row did not match the full row rule; "
                    "the row would be dropped or raise during field parsing",
                    evidence=line.strip(),
                )
            )
            continue
        date_text, description, amount_text = row.groups()
        if parse_row_date(date_text) is None:
            findings.append(
                finding(
                    "recognition",
                    page_number,
                    "transaction_date",
                    "date value rejected by the strict row-date parser",
                    evidence=date_text,
                )
            )
        if not description.strip():
            findings.append(
                finding(
                    "recognition",
                    page_number,
                    "narration",
                    "description captured empty by the field rule (text exists)",
                )
            )
        if not AMOUNT_RE.search(amount_text):
            findings.append(
                finding(
                    "recognition",
                    page_number,
                    "amount",
                    "amount value rejected by the numeric field rule (text exists)",
                    evidence=amount_text,
                )
            )
    return findings


def validate(txt_path, expect_pages=None):
    text = txt_path.read_text(encoding="utf-8", errors="replace")
    pages = split_pages(text)
    sidecar = load_sidecar(txt_path)
    sidecar_empty = set(sidecar["empty_pages"]) if sidecar else set()

    # The true PDF page count comes from the converter sidecar when
    # available, or from an explicit --expect-pages override. A
    # discrepancy means whole pages vanished during conversion and left
    # no form-feed trace behind.
    expected_pages = expect_pages
    if expected_pages is None and sidecar and sidecar.get("pdf_page_count"):
        expected_pages = sidecar["pdf_page_count"]

    findings = []
    if expected_pages is not None and len(pages) < expected_pages:
        for missing in range(len(pages) + 1, expected_pages + 1):
            findings.append(
                finding(
                    "conversion",
                    missing,
                    "*",
                    f"page missing from converted text entirely (text has "
                    f"{len(pages)} pages, expected {expected_pages}); "
                    "nothing for field rules to parse on this page",
                )
            )

    for index, page_text in enumerate(pages, start=1):
        alnum = sum(char.isalnum() for char in page_text)
        conversion_empty = (index in sidecar_empty) or alnum == 0
        findings.extend(check_page(index, page_text, conversion_empty))

    conversion_count = sum(f["layer"] == "conversion" for f in findings)
    recognition_count = sum(f["layer"] == "recognition" for f in findings)
    return {
        "text_file": str(txt_path),
        "page_count": len(pages),
        "expected_pages": expected_pages,
        "sidecar": str(txt_path.with_suffix(".txt.json")) if sidecar else None,
        "conversion_findings": conversion_count,
        "recognition_findings": recognition_count,
        "findings": findings,
    }


def print_human(report):
    expected = report.get("expected_pages")
    suffix = f", expected {expected}" if expected else ""
    print(f"== {report['text_file']} ({report['page_count']} pages{suffix})")
    if not report["findings"]:
        print("   no null values detected")
        return
    for item in report["findings"]:
        tag = "CONVERSION " if item["layer"] == "conversion" else "RECOGNITION"
        line = f"  [{tag}] p{item['page']} {item['field']}: {item['message']}"
        print(line)
        if "evidence" in item:
            print(f"               evidence: {item['evidence']!r}")
    print(
        f"   summary: conversion={report['conversion_findings']} "
        f"recognition={report['recognition_findings']}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "texts", nargs="+", type=Path, help="Converted .txt file(s) to validate"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--expect-pages",
        type=int,
        default=None,
        help="Known PDF page count; flag pages missing from "
        "the converted text (also read from the "
        "converter sidecar automatically)",
    )
    parser.add_argument(
        "--strict-rules",
        action="store_true",
        help="Also fail when recognition (field-rule) findings exist",
    )
    args = parser.parse_args()

    reports = [validate(path, args.expect_pages) for path in args.texts]
    if args.json:
        print(
            json.dumps(
                reports if len(reports) > 1 else reports[0], indent=2, ensure_ascii=False
            )
        )
    else:
        for report in reports:
            print_human(report)

    conversion_failures = sum(r["conversion_findings"] for r in reports)
    rule_failures = sum(r["recognition_findings"] for r in reports)
    if conversion_failures or (args.strict_rules and rule_failures):
        sys.exit(1)


if __name__ == "__main__":
    main()

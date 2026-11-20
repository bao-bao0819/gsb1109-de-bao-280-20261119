#!/usr/bin/env python3
"""Replay the statement line plugin over stored per-page text.

This is the rerun harness for the counterexample. It only needs the text
artifacts in casework/pages/ and the classification snapshot; it never opens
the PDF. Replacing a pageN.txt file with converted text from another tool is
enough to re-check whether a failure is a conversion problem (no usable
amount text) or a plugin rule rejection (layout mismatch).

Usage:
  .venv/bin/python casework/tools/run_case.py
  .venv/bin/python casework/tools/run_case.py --page 3 casework/pages/page3.txt
"""

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "casework"))

from beancount.parser import printer

from importer.statement_plugin import (
    AMOUNT_PATTERN,
    NO_USABLE_AMOUNT_TEXT,
    extract_lines,
)

CASEWORK = ROOT / "casework"
PAGES_DIR = CASEWORK / "pages"
EVIDENCE = CASEWORK / "evidence"


def load_classification():
    path = EVIDENCE / "classification.json"
    if not path.exists():
        return {}
    return {page["page"]: page for page in json.loads(path.read_text())["pages"]}


def replay_page(page_number, text, source):
    amount_tokens = AMOUNT_PATTERN.findall(text)
    result = extract_lines(text, source=source, page=page_number)
    rendered = [printer.format_entry(entry) for entry in result["entries"]]
    return {
        "page": page_number,
        "source_text": source,
        "amount_tokens_in_text_layer": amount_tokens,
        "status": result["status"],
        "entries_count": len(result["entries"]),
        "rejected": result["rejected"],
        "entries_beancount": rendered,
    }


def run_all():
    classification = load_classification()
    records = []
    for text_path in sorted(PAGES_DIR.glob("page*.txt")):
        page_number = int(text_path.stem.replace("page", ""))
        text = text_path.read_text()
        record = replay_page(page_number, text, str(text_path))
        record["classification"] = classification.get(page_number, {})
        records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", type=int, help="replay a single text file only")
    parser.add_argument("textfile", nargs="?", help="text file for --page replay")
    args = parser.parse_args()

    if args.page:
        if not args.textfile:
            parser.error("--page requires a text file")
        record = replay_page(args.page, pathlib.Path(args.textfile).read_text(),
                             args.textfile)
        json.dump(record, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    records = run_all()
    (EVIDENCE / "extraction_report.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n"
    )

    bean_lines = [";; -*- mode: beancount -*-", ""]
    for record in records:
        bean_lines.append(f"**** page {record['page']} ({record['status']})")
        bean_lines.append("")
        if record["entries_beancount"]:
            bean_lines.extend(record["entries_beancount"])
        else:
            bean_lines.append(f"; no entries: {record['status']}")
            for rejected in record["rejected"]:
                bean_lines.append(f"; rejected line {rejected['lineno']}: "
                                  f"{rejected['detail']}")
        bean_lines.append("")
    (EVIDENCE / "entries.beancount").write_text("\n".join(bean_lines))

    summary = [
        "# Per-page evidence summary",
        "",
        "| page | classifier label | image coverage | text chars | "
        "amount tokens in text layer | plugin status | entries |",
        "|---|---|---|---|---|---|---|",
    ]
    for record in records:
        cls = record["classification"]
        summary.append(
            f"| {record['page']} | {cls.get('classification', '-')} | "
            f"{cls.get('image_coverage', '-')} | {cls.get('text_chars', '-')} | "
            f"{', '.join(record['amount_tokens_in_text_layer']) or '-'} | "
            f"{record['status']} | {record['entries_count']} |"
        )
    summary += [
        "",
        "Plugin status meanings:",
        "",
        "- `no_usable_amount_text`: text layer has no amount token; any amount "
        "on the page exists only in raster pixels (conversion layer gap).",
        "- `layout_mismatch`: an amount token is in the text layer but the row "
        "does not match the plugin's date/description/amount template "
        "(plugin rule rejection).",
        "- `extracted`: entries emitted; see entries.beancount.",
        "",
    ]
    (EVIDENCE / "summary.md").write_text("\n".join(summary))

    for record in records:
        print(f"page {record['page']}: {record['status']} "
              f"amount_tokens={record['amount_tokens_in_text_layer']} "
              f"entries={record['entries_count']} rejected={len(record['rejected'])}")
    print(f"\nwrote {EVIDENCE / 'extraction_report.json'}")
    print(f"wrote {EVIDENCE / 'entries.beancount'}")


if __name__ == "__main__":
    main()

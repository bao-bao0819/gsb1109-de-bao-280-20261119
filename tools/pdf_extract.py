#!/usr/bin/env python3
"""Convert PDFs to per-page text and record conversion statistics.

This is stage one of the PDF import diagnostics.  Two backends are
supported:

* ``pdftotext`` from poppler, the converter called by
  ``examples/importers/acme.py``;
* a macOS PDFKit helper (``tools/pdfkit_text.swift``) used as an offline
  fallback when neither ``pdftotext`` nor ``pymupdf`` is installed.

Both backends write the form-feed character (``\\f``) between pages, the
same convention ``pdftotext`` uses, so the number of extracted pages can
be compared with the PDF page count.  A ``.json`` sidecar next to each
``.txt`` output records per-page character counts and the pages where the
converter returned no extractable text.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWIFT_SOURCE = REPO_ROOT / "tools" / "pdfkit_text.swift"
COMPILED_HELPER = REPO_ROOT / ".tmp" / "pdfkit_text"
MODULE_CACHE = REPO_ROOT / ".tmp" / "modcache"
WORK_TMP = REPO_ROOT / ".tmp"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "build" / "pdf_text"


def ensure_pdfkit_helper():
    """Compile the PDFKit helper on demand and return its path."""
    if COMPILED_HELPER.exists():
        return COMPILED_HELPER
    WORK_TMP.mkdir(exist_ok=True)
    MODULE_CACHE.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TMPDIR=str(WORK_TMP))
    cmd = [
        "swiftc",
        "-module-cache-path",
        str(MODULE_CACHE),
        str(SWIFT_SOURCE),
        "-o",
        str(COMPILED_HELPER),
    ]
    subprocess.run(cmd, check=True, env=env, capture_output=True, text=True)
    return COMPILED_HELPER


def extract_with_pdftotext(pdf_path):
    """Mirror the exact pdftotext invocation used by the ACME importer."""
    result = subprocess.run(
        ["pdftotext", str(pdf_path), "-"],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode(errors="replace")


def extract_with_pdfkit(pdf_path):
    """Extract text through the macOS PDFKit helper.

    The helper writes per-page statistics, including the true PDF page
    count, on stderr.
    """
    helper = ensure_pdfkit_helper()
    env = dict(os.environ, TMPDIR=str(WORK_TMP))
    result = subprocess.run(
        [str(helper), "--plain", str(pdf_path)],
        check=True,
        capture_output=True,
        env=env,
    )
    text = result.stdout.decode(errors="replace")
    pdf_page_count = None
    for line in result.stderr.decode(errors="replace").splitlines():
        if line.startswith("total_pages:"):
            pdf_page_count = int(line.split(":", 1)[1].strip())
    return text, pdf_page_count


def split_pages(text):
    """Split converter output on form feeds, dropping a trailing separator."""
    pages = text.split("\f")
    if pages and pages[-1] == "":
        pages.pop()
    return pages


def page_report(pages):
    """Build per-page statistics, flagging pages without alphanumeric text."""
    report = []
    empty_pages = []
    for number, page in enumerate(pages, start=1):
        alnum = sum(char.isalnum() for char in page)
        entry = {
            "page": number,
            "chars": len(page),
            "alnum": alnum,
            "empty": alnum == 0,
        }
        report.append(entry)
        if alnum == 0:
            empty_pages.append(number)
    return report, empty_pages


def convert(pdf_path, backend):
    """Run the selected backend and return text, backend name, PDF page count."""
    if backend == "pdftotext":
        text = extract_with_pdftotext(pdf_path)
        # pdftotext writes one form feed per page, including the last one.
        return text, "pdftotext", text.count("\f")
    if backend == "pdfkit":
        text, page_count = extract_with_pdfkit(pdf_path)
        return text, "pdfkit", page_count
    if shutil.which("pdftotext"):
        text = extract_with_pdftotext(pdf_path)
        return text, "pdftotext", text.count("\f")
    text, page_count = extract_with_pdfkit(pdf_path)
    return text, "pdfkit-fallback", page_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path, help="PDF documents to convert")
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for .txt outputs and .json sidecars",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "pdftotext", "pdfkit"),
        default="auto",
        help="Conversion backend (default: auto)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any page converts to empty text",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    failures = 0

    for pdf_path in args.pdfs:
        try:
            text, backend, pdf_page_count = convert(pdf_path, args.backend)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"ERROR {pdf_path}: {exc}", file=sys.stderr)
            failures += 1
            continue

        pages = split_pages(text)
        report, empty_pages = page_report(pages)
        txt_path = args.output_dir / f"{pdf_path.stem}.txt"
        txt_path.write_text(text, encoding="utf-8")

        text_page_count = len(pages)
        missing_pages = []
        if pdf_page_count is not None and text_page_count < pdf_page_count:
            missing_pages = list(range(text_page_count + 1, pdf_page_count + 1))
        sidecar = {
            "source_pdf": str(pdf_path),
            "text_output": str(txt_path),
            "backend": backend,
            "pdf_page_count": pdf_page_count,
            "text_page_count": text_page_count,
            "missing_pages": missing_pages,
            "empty_pages": empty_pages,
            "pages": report,
        }
        sidecar_path = args.output_dir / f"{pdf_path.stem}.txt.json"
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        if missing_pages:
            status = "MISSING"
        elif empty_pages:
            status = "EMPTY-PAGES"
        else:
            status = "ok"
        print(
            f"{status:11} {pdf_path} -> {txt_path} "
            f"[backend={backend}, pdf_pages={pdf_page_count}, "
            f"text_pages={text_page_count}, missing={missing_pages or '-'}, "
            f"empty={empty_pages or '-'}]"
        )
        if empty_pages or missing_pages:
            failures += 1

    if failures and args.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()

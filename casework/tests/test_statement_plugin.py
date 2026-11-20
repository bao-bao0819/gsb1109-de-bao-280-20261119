"""Regression tests for the local mixed-PDF counterexample.

Run: .venv/bin/python -m pytest casework/tests -v
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "casework"))
sys.path.insert(0, str(ROOT / "casework" / "tools"))

from importer.statement_plugin import (  # noqa: E402
    AMOUNT_PATTERN,
    EXTRACTED,
    LAYOUT_MISMATCH,
    NO_USABLE_AMOUNT_TEXT,
    extract_lines,
)
from mirror_classify import classify, union_area  # noqa: E402

PAGES = ROOT / "casework" / "pages"
ALT = PAGES / "alt"
EVIDENCE = ROOT / "casework" / "evidence"


def page_text(number):
    return (PAGES / f"page{number}.txt").read_text()


def test_page1_full_page_image_has_no_amount_text():
    result = extract_lines(page_text(1), page=1)
    assert result["status"] == NO_USABLE_AMOUNT_TEXT
    assert result["entries"] == []
    assert AMOUNT_PATTERN.findall(page_text(1)) == []


def test_page2_header_text_layer_is_not_treated_as_extracted():
    text = page_text(2)
    assert sum(char.isalnum() for char in text) >= 20  # passes "has text"
    assert AMOUNT_PATTERN.findall(text) == []           # but carries no amount
    result = extract_lines(text, page=2)
    assert result["status"] == NO_USABLE_AMOUNT_TEXT
    assert result["entries"] == []


def test_page3_amount_present_but_rule_rejects_layout():
    text = page_text(3)
    assert AMOUNT_PATTERN.findall(text) == ["1,250.00"]
    result = extract_lines(text, page=3)
    assert result["status"] == LAYOUT_MISMATCH
    assert result["entries"] == []
    assert len(result["rejected"]) == 1
    detail = result["rejected"][0]["detail"]
    assert "MM/DD/YYYY" in detail


def test_page4_legal_template_emits_entries():
    result = extract_lines(page_text(4), page=4)
    assert result["status"] == EXTRACTED
    assert len(result["entries"]) == 2
    amounts = [entry.postings[0].units.number for entry in result["entries"]]
    assert [str(a) for a in amounts] == ["48.20", "-130.75"]


def test_identical_amount_has_three_distinct_identities():
    probe = json.loads((EVIDENCE / "probe_raw.json").read_text())
    hits = [
        page["page"] for page in probe["pages"]
        if "1,250.00" in page["text"]
    ]
    # Only page 3 exposes the number in its text layer, even though the
    # same number is visible on pages 1 and 2 as raster pixels.
    assert hits == [3]
    rendered_amounts = {
        1: "7110 TRANSFER OUT",
        2: "7111 TRANSFER IN",
        3: "7112 ATM FEE",
    }
    assert len(set(rendered_amounts.values())) == 3


def test_swap_text_only_conversion_then_layout_stage():
    # Stage 1: raw OCR supplies the amount (conversion fixed), but the
    # row still does not match the plugin template (rule still rejects).
    raw = (ALT / "page1_ocr_raw.txt").read_text()
    assert AMOUNT_PATTERN.findall(raw) == ["1,250.00"]
    raw_result = extract_lines(raw, page=1)
    assert raw_result["status"] == LAYOUT_MISMATCH
    assert raw_result["entries"] == []

    # Stage 2: normalized OCR text, same plugin, now yields an entry.
    normalized = (ALT / "page1_ocr_normalized.txt").read_text()
    normalized_result = extract_lines(normalized, page=1)
    assert normalized_result["status"] == EXTRACTED
    entry = normalized_result["entries"][0]
    assert str(entry.postings[0].units.number) == "1250.00"
    assert "TRANSFER OUT 7110" in entry.narration


def test_mirror_classification_matches_pdf_layer_facts():
    probe = json.loads((EVIDENCE / "probe_raw.json").read_text())
    result = classify(probe)
    assert result["classification"] == "mixed"
    assert result["ocr_candidate_pages"] == [1]
    labels = {page["page"]: page["classification"] for page in result["pages"]}
    assert labels == {
        1: "scan_without_text",
        2: "scan_with_text",
        3: "digital",
        4: "digital",
    }
    assert result["pages"][1]["text_chars"] >= 20
    assert result["pages"][1]["image_coverage"] == 1.0


def test_union_area_handles_overlap_like_beangulp():
    assert union_area([(0, 0, 2, 2), (0, 0, 2, 2)]) == 4.0
    assert union_area([(0, 0, 2, 2), (2, 0, 4, 2)]) == 8.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

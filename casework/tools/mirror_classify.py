#!/usr/bin/env python3
"""Mirror beangulp.pdf.classify_pdf() on pdf_probe JSON output.

The repository's classifier depends on PyMuPDF, which is not installable
in this offline environment. This script reproduces its two computations
exactly from the local pdf_probe.swift output:

  * image coverage = union area of image bboxes / page box area
    (same rectangle-union algorithm as beangulp.pdf._union_area)
  * text_chars     = count of alphanumeric characters in the text layer
    (str.isalnum(), matching the PyMuPDF get_text() count)

and applies the same thresholds and the same document/page labels, so
the per-page classification here is comparable byte-for-byte in meaning
to tools/classify_pdf.py once PyMuPDF is available.

Usage: mirror_classify.py probe.json [--image-threshold 0.8]
       [--min-text-chars 20]
"""

import argparse
import json
import sys


def union_area(rectangles):
    edges = sorted({x for rect in rectangles for x in (rect[0], rect[2])})
    area = 0.0
    for left, right in zip(edges, edges[1:]):
        intervals = sorted(
            (rect[1], rect[3])
            for rect in rectangles
            if rect[0] < right and rect[2] > left
        )
        height = 0.0
        end = float("-inf")
        for bottom, top in intervals:
            height += max(0.0, top - max(bottom, end))
            end = max(end, top)
        area += (right - left) * height
    return area


def classify(probe, image_threshold=0.8, min_text_chars=20):
    pages = []
    for entry in probe["pages"]:
        x0, y0, x1, y1 = entry["media_box"]
        bounds_area = max(0.0, (x1 - x0) * (y1 - y0))
        rectangles = [image["bbox"] for image in entry["images"]]
        coverage = union_area(rectangles) / bounds_area if bounds_area else 0.0
        text_chars = sum(char.isalnum() for char in entry["text"])
        has_text = text_chars >= min_text_chars
        if coverage >= image_threshold:
            label = "scan_with_text" if has_text else "scan_without_text"
        elif has_text:
            label = "digital"
        else:
            label = "unknown"
        pages.append({
            "page": entry["page"],
            "classification": label,
            "image_coverage": round(coverage, 4),
            "text_chars": text_chars,
        })

    scanned = [page for page in pages if page["classification"].startswith("scan_")]
    if len(scanned) == len(pages):
        document_class = "scanned"
    elif scanned:
        document_class = "mixed"
    elif all(page["classification"] == "digital" for page in pages):
        document_class = "digital"
    else:
        document_class = "unknown"
    return {
        "file": probe["file"],
        "classification": document_class,
        "has_scanned_pages": bool(scanned),
        "ocr_candidate_pages": [
            page["page"] for page in pages
            if page["classification"] == "scan_without_text"
        ],
        "pages": pages,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probe")
    parser.add_argument("--image-threshold", type=float, default=0.8)
    parser.add_argument("--min-text-chars", type=int, default=20)
    args = parser.parse_args()
    with open(args.probe, encoding="utf-8") as handle:
        probe = json.load(handle)
    result = classify(probe, args.image_threshold, args.min_text_chars)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

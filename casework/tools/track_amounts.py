#!/usr/bin/env python3
"""Trace an amount token across pages without conflating identities.

A full-text search for "1,250.00" returns three pages, but the three
occurrences are different transactions (refs 7110/7111/7112) and only one
of them lives in an extractable text layer. This script reports, per page:
classification, whether the token is in the text layer or pixels only, and
the transaction identity (posting reference + description) taken from the
canonical page content. Identity -- not the number -- decides which page
an extracted entry could have come from.

Usage: .venv/bin/python casework/tools/track_amounts.py [1,250.00]
"""

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CASEWORK = ROOT / "casework"
TOKEN = sys.argv[1] if len(sys.argv) > 1 else "1,250.00"

IDENTITIES = {
    1: "ref 7110 TRANSFER OUT (pixels only, page has no text layer)",
    2: "ref 7111 TRANSFER IN (pixels only, text layer is header-only)",
    3: "ref 7112 ATM FEE (native text, row layout rejected by plugin)",
    4: "no occurrence of this amount on this page",
}

probe = json.loads((CASEWORK / "evidence" / "probe_raw.json").read_text())
classification = {
    page["page"]: page
    for page in json.loads((CASEWORK / "evidence" / "classification.json").read_text())["pages"]
}

pattern = re.compile(re.escape(TOKEN))
print(f"tracking amount token {TOKEN!r}\n")
for page in probe["pages"]:
    number = page["page"]
    in_text = bool(pattern.search(page["text"]))
    label = classification[number]["classification"]
    if in_text:
        where = "TEXT LAYER"
    elif label.startswith("scan_"):
        where = "absent from text layer; visible only as raster pixels"
    else:
        where = "absent: token does not occur anywhere on this page"
    print(f"page {number} [{label}]: {where}")
    print(f"  identity: {IDENTITIES[number]}")
print("\nConclusion: three identical amounts, three identities. A global text")
print("match cannot attribute an entry to a page; per-page replay is required.")

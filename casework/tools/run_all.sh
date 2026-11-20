#!/usr/bin/env bash
# One-command rebuild of the whole local counterexample, offline.
#
# Prereqs (already present on this machine):
#   /usr/local/bin/python3 with Pillow  -> builds the PDF (raster pages)
#   swiftc / CoreGraphics / PDFKit      -> structural probe + PNG render
#   .venv (beancount, beangulp)         -> classification mirror + plugin
#
# Outputs land in casework/evidence/ and casework/pages/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CASE="$ROOT/casework"
BIN="$CASE/tools/bin"
mkdir -p "$BIN" "$CASE/evidence/render" "$CASE/pages"

export TMPDIR="$BIN"

echo "== 1/6 build mixed PDF (system Python + Pillow)"
/usr/local/bin/python3 "$CASE/tools/make_pdf.py"

echo "== 2/6 compile Swift probe and renderer"
swiftc -module-cache-path "$BIN/modcache" \
    "$CASE/tools/pdf_probe.swift" -o "$BIN/pdf_probe"
swiftc -module-cache-path "$BIN/modcache" \
    "$CASE/tools/render_pdf.swift" -o "$BIN/render_pdf"

echo "== 3/6 structural probe and mirrored beangulp classification"
"$BIN/pdf_probe" "$CASE/pdf/mixed_statement.pdf" \
    > "$CASE/evidence/probe_raw.json"
"$ROOT/.venv/bin/python" "$CASE/tools/mirror_classify.py" \
    "$CASE/evidence/probe_raw.json" \
    > "$CASE/evidence/classification.json"
"$BIN/pdf_probe" "$ROOT/examples/Downloads/Statement.pdf" \
    > "$CASE/evidence/upstream_statement_probe.json"
"$BIN/pdf_probe" "$ROOT/examples/tests/acme/acmebank1.pdf" \
    > "$CASE/evidence/upstream_acmebank_probe.json"

echo "== 4/6 export per-page text and rendered PNGs"
"$ROOT/.venv/bin/python" - "$CASE" <<'PY'
import json, pathlib, sys
case = pathlib.Path(sys.argv[1])
probe = json.loads((case / "evidence" / "probe_raw.json").read_text())
for page in probe["pages"]:
    (case / "pages" / f"page{page['page']}.txt").write_text(page["text"])
PY
"$BIN/render_pdf" "$CASE/pdf/mixed_statement.pdf" \
    "$CASE/evidence/render" 1.5 >/dev/null

echo "== 5/6 replay the line-extraction plugin on each page text"
"$ROOT/.venv/bin/python" "$CASE/tools/run_case.py"

echo "== 6/6 regression tests"
"$ROOT/.venv/bin/python" -m pytest "$CASE/tests" -q

echo
"$ROOT/.venv/bin/python" "$CASE/tools/track_amounts.py"

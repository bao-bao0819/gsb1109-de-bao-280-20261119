#!/usr/bin/env bash
# Pure-CLI, re-runnable evidence collection on the FIXED SQLFluff only.
# No database and no package mutation: uses sqlfluff's duckdb dialect grammar
# (statement shape taken from test/fixtures/dialects/duckdb/copy_from_database.sql).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SF="$HERE/../.venv/bin/sqlfluff"
TPL="$HERE/template/load_copies.sql"
OUT="$HERE/evidence/cli"
REND="$HERE/evidence/render"
mkdir -p "$OUT" "$REND"

echo "== fixed versions =="
"$SF" --version | tee "$OUT/version.txt"
"$HERE/../.venv/bin/python" -c "import jinja2;print('jinja2', jinja2.__version__)" | tee -a "$OUT/version.txt"

# 1) Full config matrix: JSON diagnostics + persisted per-rule timing CSV.
for cfg in "$HERE"/configs/*.sqlfluff; do
  name="$(basename "$cfg" .sqlfluff)"
  "$SF" lint --nofail --ignore-local-config --config "$cfg" \
      --format json --persist-timing "$OUT/${name}.timing.csv" \
      "$TPL" > "$OUT/${name}.json"
  echo "linted $name -> $OUT/${name}.json"
done

# 2) Rendered output (shows main + auto-completed SELECT variants).
for name in duck_empty duck_missing duck_select duck_rev_empty_first duck_rev_empty_last; do
  "$SF" render --ignore-local-config --config "$HERE/configs/${name}.sqlfluff" \
      "$TPL" > "$REND/${name}.render.txt"
done

# 3) Byte-for-byte proof: missing-key main SQL == empty-string main SQL.
"$HERE/../.venv/bin/python" "$HERE/export_renders.py" > "$REND/render_hashes.txt"
if cmp -s "$HERE/rendered/duck_missing_v0.sql" "$HERE/rendered/duck_empty_v0.sql"; then
  echo "BYTE-IDENTICAL main render: duck_missing_v0 == duck_empty_v0" | tee "$OUT/cmp_missing_vs_empty.txt"
fi
shasum -a 256 "$HERE/rendered/duck_missing_v0.sql" "$HERE/rendered/duck_empty_v0.sql" \
  | tee -a "$OUT/cmp_missing_vs_empty.txt"

# 4) Single-dimension controls with the template switched OFF (raw templater),
#    all run against the SAME exported main-render SQL where applicable.
echo
echo "== template-OFF single-dimension controls =="
run_raw() { # tag dialect rules file
  local tag="$1" dial="$2" rules="$3" file="$4"
  "$SF" lint --nofail --ignore-local-config --templater raw --dialect "$dial" \
      --rules "$rules" --format json "$file" \
    | "$HERE/../.venv/bin/python" "$HERE/summarize.py" "$tag"
}
run_raw "baseline/empty  duck " duckdb   "LT01,AM04" "$HERE/rendered/duck_empty_v0.sql"
run_raw "only-add-param  duck " duckdb   "LT01,AM04" "$HERE/rendered/duck_normal_v0.sql"
run_raw "only-dialect    pg   " postgres "LT01,CP01" "$HERE/rendered/pg_empty_v0.sql"
run_raw "only-branch     duck " duckdb   "LT01,AM04" "$HERE/rendered/duck_select_v0.sql"
run_raw "only-branch     pg   " postgres "LT01,CP01" "$HERE/rendered/pg_select_v0.sql"

# 5) Isolated single-COPY controls: attribute the failure to one statement.
echo
echo "== isolated single-statement controls (raw) =="
for f in copy_fixed_only copy_param_complete copy_param_empty select_control; do
  for dial in duckdb postgres; do
    run_raw "$f/$dial" "$dial" "LT01,AM04,CP01" "$HERE/controls/${f}.sql"
  done
done

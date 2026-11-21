"""Assert the core triage invariants against the collected evidence."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
findings = {
    (r["dialect"], r["label"]): r
    for r in json.loads((HERE / "evidence" / "findings.json").read_text())
}
checks = []


def check(name, cond):
    checks.append((name, bool(cond)))


for dialect in ("duckdb", "postgres"):
    short = "duck" if dialect == "duckdb" else "pg"
    missing = findings[(dialect, "missing")]
    empty = findings[(dialect, "empty")]
    normal = findings[(dialect, "normal")]

    # 1. Missing-key and empty-string render byte-identical main SQL.
    check(f"{dialect}: missing/empty main SQL identical",
          missing["variants"][0]["sha"] == empty["variants"][0]["sha"])
    # 2. Same macro call source location and same PRS code/position.
    mp = [p for p in missing["parse_findings"] if p["is_root_variant"]]
    ep = [p for p in empty["parse_findings"] if p["is_root_variant"]]
    check(f"{dialect}: identical PRS source position",
          mp and ep and mp[0]["src_line"] == ep[0]["src_line"]
          and mp[0]["src_pos"] == ep[0]["src_pos"]
          and mp[0]["range"]["raw"] == ep[0]["range"]["raw"])
    # 3. Only the missing-key case carries a TMP (Jinja undefined) finding.
    check(f"{dialect}: missing has TMP, empty has none",
          any(t["code"] == "TMP" for t in missing["templater_violations"])
          and not empty["templater_violations"])
    # 4. Complete COPY: clean in duckdb, rejected only in postgres.
    normal_prs = [p for p in normal["parse_findings"] if p["is_root_variant"]]
    if dialect == "duckdb":
        check("duckdb: complete COPY has no PRS", not normal_prs)
    else:
        check("postgres: complete COPY is PRS (dialect difference)", bool(normal_prs))
    # 5. Enabled rule set differs by dialect; LT01 always ran.
    check(f"{dialect}: LT01 entered rule phase", "LT01" in missing["rules_that_ran"])
    check(f"{dialect}: differentiator rule ran",
          ("AM04" if dialect == "duckdb" else "CP01") in missing["rules_that_ran"])

# 6. Reversed order changes the parser error range width (do not infer order from it).
rf = findings[("duckdb", "rev_empty_first")]
rl = findings[("duckdb", "rev_empty_last")]
w_first = next(p["range"]["raw_len"] for p in rf["parse_findings"] if p["is_root_variant"])
w_last = next(p["range"]["raw_len"] for p in rl["parse_findings"] if p["is_root_variant"])
check("duckdb: reversed-order range widths differ (112 vs 33)",
      w_first == 112 and w_last == 33)

# 7. The auto-completed SELECT parses in both and only raises LT01.
for dialect in ("duckdb", "postgres"):
    sel = findings[(dialect, "select")]
    codes = {v["code"] for v in sel["final_violations"]}
    check(f"{dialect}: SELECT branch parses, only LT01", codes == {"LT01"})

width = max(len(n) for n, _ in checks)
for name, ok in checks:
    print(f"[{'PASS' if ok else 'FAIL'}] {name.ljust(width)}")
print(f"\n{sum(ok for _, ok in checks)}/{len(checks)} invariants hold")
raise SystemExit(0 if all(ok for _, ok in checks) else 1)

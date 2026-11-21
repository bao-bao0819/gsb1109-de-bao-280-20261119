"""Reproducible driver for the COPY FROM DATABASE load-alert investigation.

Runs the fixed SQLFluff 4.5.0 (this repo's editable install) over one Jinja
template under a matrix of dialects / external-parameter inputs / branches /
parameter orders, and records, for every concrete COPY occurrence:

  * template source location (macro body line, call line)
  * the actual rendered location and the parser error range
  * TMP (Jinja undefined) vs PRS (parse) vs lint rule evidence
  * the enabled rule set and per-rule timing proving the rule phase ran

Output: evidence/findings.json plus a human-readable stdout summary.
It only observes SQLFluff; it never mutates the package or a database.
"""
import hashlib
import json
from pathlib import Path

from sqlfluff.core import FluffConfig, Linter

HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "template" / "load_copies.sql"
TEMPLATE = TEMPLATE_PATH.read_text()
EVIDENCE = HERE / "evidence"
EVIDENCE.mkdir(exist_ok=True)

RULES = {"duckdb": "LT01,AM04", "postgres": "LT01,CP01"}

# label -> (load_branch, params value or None for missing key)
INPUTS = {
    "normal": (True, "pgdb"),
    "empty": (True, ""),
    "missing": (True, None),
    "select": (False, "pgdb"),
    "rev_empty_first": (True, ",pgdb"),
    "rev_empty_last": (True, "pgdb,"),
}


def sha(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


def analyze(dialect: str, label: str):
    load_branch, params = INPUTS[label]
    context = {"load_branch": load_branch}
    if params is not None:
        context["params"] = params
    cfg = FluffConfig(
        configs={
            "core": {"dialect": dialect, "rules": RULES[dialect]},
            "templater": {"jinja": {"context": context}},
        }
    )
    lnt = Linter(config=cfg)
    rulepack = lnt.get_rulepack(cfg)
    enabled = sorted(rulepack.codes())

    rendered = lnt.render_string(TEMPLATE, "load_copies.sql", cfg, "utf-8")
    tmp = [
        {"code": e.rule_code(), "src_line": e.line_no, "src_pos": e.line_pos,
         "desc": e.desc()}
        for e in rendered.templater_violations
    ]
    variants = []
    for i, vf in enumerate(rendered.templated_variants):
        variants.append({"idx": i, "sha": sha(vf.templated_str),
                         "sql": vf.templated_str})

    parsed = Linter.parse_rendered(rendered)
    root_variant = parsed.root_variant()
    parse_findings = []
    for vi, pv in enumerate(parsed.parsed_variants):
        for e in pv.violations():
            seg = getattr(e, "segment", None)
            rng = None
            if seg is not None and seg.pos_marker is not None:
                pm = seg.pos_marker
                end = seg.get_end_point_marker()
                rng = {
                    "raw": seg.raw,
                    "raw_len": len(seg.raw),
                    "tpl_start": [pm.working_line_no, pm.working_line_pos],
                    "tpl_end": [end.working_line_no, end.working_line_pos],
                    "src_pos": list(pm.source_position()),
                }
            parse_findings.append({
                "variant": vi, "is_root_variant": pv is root_variant,
                "code": e.rule_code(), "src_line": e.line_no,
                "src_pos": e.line_pos, "desc": e.desc(), "range": rng,
            })

    linted = Linter.lint_parsed(parsed, rulepack)
    final = [{"code": e.rule_code(), "src_line": e.line_no,
              "src_pos": e.line_pos, "desc": e.desc()}
             for e in linted.get_violations()]
    rule_timing = [code for code, _, _ in linted.timings.rule_timings]

    return {
        "dialect": dialect, "label": label, "context": context,
        "enabled_rules": enabled, "variant_count": len(variants),
        "variants": variants, "templater_violations": tmp,
        "parse_findings": parse_findings, "final_violations": final,
        "rules_that_ran": rule_timing,
    }


def main():
    results = []
    for dialect in ("duckdb", "postgres"):
        for label in INPUTS:
            results.append(analyze(dialect, label))
    out = EVIDENCE / "findings.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    print(f"sqlfluff fixed version: {Linter.__module__}")
    for r in results:
        print("\n" + "=" * 78)
        print(f"{r['dialect']:8s} {r['label']:16s} context={r['context']}")
        print(f"  enabled_rules={r['enabled_rules']} rules_that_ran={r['rules_that_ran']}")
        print(f"  variants={r['variant_count']} "
              f"shas={[v['sha'] for v in r['variants']]}")
        for t in r["templater_violations"]:
            print(f"  TMP src L{t['src_line']}:{t['src_pos']} {t['desc']}")
        for p in r["parse_findings"]:
            rng = p["range"] or {}
            span = f"len={rng.get('raw_len')} tpl{rng.get('tpl_start')}->{rng.get('tpl_end')}"
            print(f"  PRS v{p['variant']} root={p['is_root_variant']} "
                  f"srcL{p['src_line']}:{p['src_pos']} {span} raw={rng.get('raw')!r}")
        for f in r["final_violations"]:
            print(f"  FINAL {f['code']} srcL{f['src_line']}:{f['src_pos']} {f['desc'][:60]!r}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()

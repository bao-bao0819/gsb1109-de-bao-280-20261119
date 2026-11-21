"""Export every rendered variant to rendered/ and record hashes.

This gives us literal SQL files for:
  * byte-for-byte comparison of the missing-key vs empty-string main render
  * a "template off" re-run (same SQL through the raw templater)
The FluffConfig context here mirrors each generated .sqlfluff exactly.
"""
import hashlib
from pathlib import Path

from sqlfluff.core import FluffConfig, Linter

HERE = Path(__file__).resolve().parent
TEMPLATE = (HERE / "template" / "load_copies.sql").read_text()
OUT = HERE / "rendered"
OUT.mkdir(exist_ok=True)

# (label, dialect, load_branch, params-or-None). None => key absent.
CASES = [
    ("normal", "duckdb", True, "pgdb"),
    ("empty", "duckdb", True, ""),
    ("missing", "duckdb", True, None),
    ("select", "duckdb", False, "pgdb"),
    ("normal", "postgres", True, "pgdb"),
    ("empty", "postgres", True, ""),
    ("missing", "postgres", True, None),
    ("select", "postgres", False, "pgdb"),
]

summary = []
for label, dialect, load_branch, params in CASES:
    context = {"load_branch": load_branch}
    if params is not None:
        context["params"] = params
    cfg = FluffConfig(
        configs={
            "core": {"dialect": dialect},
            "templater": {"jinja": {"context": context}},
        }
    )
    lnt = Linter(config=cfg)
    rendered = lnt.render_string(TEMPLATE, "load_copies.sql", cfg, "utf-8")
    short = "duck" if dialect == "duckdb" else "pg"
    for i, variant in enumerate(rendered.templated_variants):
        sql = variant.templated_str
        fname = OUT / f"{short}_{label}_v{i}.sql"
        fname.write_text(sql)
        digest = hashlib.sha256(sql.encode()).hexdigest()[:16]
        summary.append((f"{short}_{label}_v{i}", digest, repr(sql)))

for name, digest, rep in summary:
    print(f"{name:22s} sha256={digest}  sql={rep}")

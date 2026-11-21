"""Generate the fixed .sqlfluff config matrix for the investigation.

Every config is plain INI so the whole study is re-runnable with the fixed
sqlfluff CLI alone (no Python needed to reproduce once these are written).
Lists are encoded as comma separated strings because the template loop calls
params.split(",").
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = HERE / "configs"
CFG.mkdir(exist_ok=True)

# Deliberately different enabled rule sets per dialect so "the SELECT parses in
# both, but the rule configuration differs" is observable. LT01 fires on the
# SELECT control in both; AM04 (duckdb) vs CP01 (postgres) differentiate them.
DIALECT_RULES = {"duckdb": "LT01,AM04", "postgres": "LT01,CP01"}


def write(name, dialect, load_branch, rules, params_line=None):
    lines = [
        "[sqlfluff]",
        f"dialect = {dialect}",
        "templater = jinja",
        f"rules = {rules}",
        "",
        "[sqlfluff:templater:jinja:context]",
        f"load_branch = {'true' if load_branch else 'false'}",
    ]
    if params_line is not None:
        lines.append(params_line)  # already "key = value"
    (CFG / name).write_text("\n".join(lines) + "\n")


for dialect, rules in DIALECT_RULES.items():
    short = "duck" if dialect == "duckdb" else "pg"
    # Three external-parameter inputs on the load branch.
    write(f"{short}_normal.sqlfluff", dialect, True, rules, "params = pgdb")
    write(f"{short}_empty.sqlfluff", dialect, True, rules, "params =")
    write(f"{short}_missing.sqlfluff", dialect, True, rules, None)
    # SELECT control branch (switch outside the macro).
    write(f"{short}_select.sqlfluff", dialect, False, rules, "params = pgdb")
    # Reversed parameter order fed to the SAME loop: empty first vs empty last.
    write(f"{short}_rev_empty_first.sqlfluff", dialect, True, rules, "params = ,pgdb")
    write(f"{short}_rev_empty_last.sqlfluff", dialect, True, rules, "params = pgdb,")

print("wrote configs:")
for p in sorted(CFG.glob("*.sqlfluff")):
    print(" ", p.name)

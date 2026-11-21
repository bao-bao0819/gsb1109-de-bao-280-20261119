"""Read a sqlfluff --format json payload on stdin and print a compact summary.

Usage: <sqlfluff lint --format json ...> | python summarize.py TAG
"""
import json
import sys

tag = sys.argv[1] if len(sys.argv) > 1 else "-"
data = json.load(sys.stdin)
record = data[0]
violations = [(v["code"], v["start_line_no"]) for v in record["violations"]]
rules_ran = sorted(k for k in record.get("timings", {}) if k.isupper())
print(f"{tag:28s} {violations or 'CLEAN'} rules_ran={rules_ran}")

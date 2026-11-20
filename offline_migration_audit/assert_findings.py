"""对回放结果做因果特征断言：任一断言失败即说明回放结论不可信。"""
import json
import os

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
findings = json.load(open(os.path.join(REPORT_DIR, "replay_findings.json"), encoding="utf-8"))
checklist = json.load(
    open(os.path.join(REPORT_DIR, "warehouse_checklist.json"), encoding="utf-8")
)

by_batch = {f["batch"]: f for f in findings if "batch" in f and "main_diff" in f}
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# 路线 A：save 的更新与新建都有真实历史
s1 = by_batch["BATCH-S1"]
check("A1 save 更新主表 1001", any(c["sku"] == "SKU-1001" for c in s1["main_diff"]["changed"]))
check("A2 save 新建主表 1003", s1["main_diff"]["added"] == ["SKU-1003"])
check(
    "A3 save 产出 ~ 与 + 各一条且均判真实",
    {(h["sku"], h["history_type"], h["verdict"].startswith("真实")) for h in s1["new_history"]}
    == {("SKU-1001", "~", True), ("SKU-1003", "+", True)},
)

# 路线 B：主表改动确定，历史零新增（缺历史的根因）
s2 = by_batch["BATCH-S2"]
check("B1 update 主表改动 2 行", len(s2["main_diff"]["changed"]) == 2)
check("B2 update 历史零新增", s2["new_history"] == [])

# 路线 C：同批新 SKU 真历史 + 重放值假创建历史
s3 = by_batch["BATCH-S3"]
check("C1 ignore_conflicts 主表只新增 1003", s3["main_diff"]["added"] == ["SKU-1003"])
check("C2 主表对 1001 无任何写入", s3["main_diff"]["changed"] == [])
check("C3 新增历史 2 行", len(s3["new_history"]) == 2)
hist_1001 = next(h for h in s3["new_history"] if h["sku"] == "SKU-1001")
hist_1003 = next(h for h in s3["new_history"] if h["sku"] == "SKU-1003")
check("C4 1001 被写成假 '+' 创建", hist_1001["history_type"] == "+" and hist_1001["verdict"].startswith("假"))
check("C5 1003 是真实 '+' 创建", hist_1003["history_type"] == "+" and hist_1003["verdict"].startswith("真实"))

# 路线 C 变体：异值冲突静默吞掉，主表历史都为空
s3b = by_batch["BATCH-S3B"]
check("C6 异值冲突主表零变化", s3b["main_diff"] == {"added": [], "removed": [], "changed": []})
check("C7 异值冲突历史零新增", s3b["new_history"] == [])

# 组合场景
combo = next(f for f in findings if f["scenario"].startswith("组合"))
st1, st2, st3 = combo["steps"]
check("D1 组合步骤1 update 无历史", st1["new_history"] == [] and len(st1["main_diff"]["changed"]) == 1)
check(
    "D2 组合步骤2 一真一假",
    {(h["sku"], h["verdict"].startswith("假")) for h in st2["new_history"]}
    == {("SKU-1004", False), ("SKU-1001", True)},
)
check("D3 组合步骤3 主表零写入", st3["main_diff"] == {"added": [], "removed": [], "changed": []})
check("D4 重放仍产出 2 条假 '+'", len(st3["new_history"]) == 2 and all(
    h["verdict"].startswith("假") for h in st3["new_history"]
))

# 旧迁移：最终表业务态相同，历史形态不同
legacy = next(f for f in findings if f["scenario"].startswith("旧迁移"))
check("E1 两路线最终业务表一致", legacy["final_main_identical"] is True)
check(
    "E2 save 路线含 '~' 真实更新",
    any(h["history_type"] == "~" for h in legacy["via_save_history"]),
)
check(
    "E3 bulk 路线对 1001 是假 '+'",
    any(h["sku"] == "SKU-1001" and h["verdict"].startswith("假") for h in legacy["via_bulk_history"]),
)

# 清单计数与可定位性
missing = checklist["1_缺历史_主表改动无历史"]
fake = checklist["2_假历史_主表无新写入却有创建行"]
gap = checklist["4_仍缺证据_不得凭值补写"]
check("F1 缺历史清单 3 条且批号可定位", len(missing) == 3 and all(m["batch"] for m in missing))
check("F2 假历史清单均带 history_id", all(x["history_id"] is not None for x in fake))
check("F3 缺证据项均给出所需证据", all(x.get("needed_evidence") for x in gap))

print()
if failures:
    print(f"{len(failures)} 项断言失败：{failures}")
    raise SystemExit(1)
print("全部因果断言通过。")

"""
离线商品迁移审计回放（只读诊断，不修改任何已有审计数据）。

在独立 sqlite 小表上复现三条迁移入口对主表 / simple-history 历史表的真实影响：
  路线 A：普通 Model.save()
  路线 B：QuerySet.update()
  路线 C：bulk_create_with_history(ignore_conflicts=True)
另含组合场景（先 update 再同值冲突、冲突批次重放）与“只保留最终表”的旧迁移推演。

每个批号通过 history_change_reason 落盘，便于数仓按批号 + history_id 定位。
"""
import json
import os
from datetime import datetime

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "replay_settings")
django.setup()

from django.core.management import call_command  # noqa: E402

from replay_app.models import Product  # noqa: E402
from simple_history.utils import bulk_create_with_history  # noqa: E402

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
os.makedirs(REPORT_DIR, exist_ok=True)

# 每个批号一个固定 history_date，保证回放顺序可辨识
DATES = {
    "BASE-000": datetime(2026, 11, 20, 9, 0, 0),
    "BATCH-S1": datetime(2026, 11, 20, 10, 0, 0),
    "BATCH-S2": datetime(2026, 11, 20, 11, 0, 0),
    "BATCH-S3": datetime(2026, 11, 20, 12, 0, 0),
    "BATCH-S3B": datetime(2026, 11, 20, 12, 30, 0),
    "BATCH-C1-UPDATE": datetime(2026, 11, 20, 13, 0, 0),
    "BATCH-C1-CONFLICT": datetime(2026, 11, 20, 13, 30, 0),
    "BATCH-C1-REPLAY": datetime(2026, 11, 20, 14, 0, 0),
}

findings = []


def reset_tables():
    # 先删主表（会触发删除信号产生 '-' 历史），再直接清空历史表，保证起始态干净
    Product.objects.all().delete()
    Product.history.model.objects.all().delete()


def main_snapshot():
    return {
        r.sku: {
            "id": r.id,
            "name": r.name,
            "price": r.price,
            "source_batch": r.source_batch,
        }
        for r in Product.objects.order_by("sku")
    }


def hist_snapshot():
    # 注意：历史表自身主键是 history_id；被跟踪的原表主键也叫 id，同一商品多行重复
    return [
        {
            "history_id": h.history_id,
            "product_id": h.id,
            "sku": h.sku,
            "name": h.name,
            "price": h.price,
            "source_batch": h.source_batch,
            "history_type": h.history_type,
            "history_date": h.history_date.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": h.history_change_reason,
        }
        for h in Product.history.order_by("history_id")
    ]


def diff_main(before, after):
    added, removed, changed = [], [], []
    for sku, row in after.items():
        if sku not in before:
            added.append(sku)
        elif row != before[sku]:
            changed.append(
                {
                    "sku": sku,
                    "before": {k: before[sku][k] for k in ("name", "price", "source_batch")},
                    "after": {k: row[k] for k in ("name", "price", "source_batch")},
                }
            )
    for sku in before:
        if sku not in after:
            removed.append(sku)
    return {"added": added, "removed": removed, "changed": changed}


def diff_hist(before, after, main_before_ids=None):
    """返回新增历史，并标注证据性质。

    判定规则：
      '+' 且对应主表主键在操作前已存在 → 假创建历史（主表本步无新写入）
      '+' 且对应主表主键是本步新出现   → 真实创建历史
      '~'                              → 真实更新历史
    """
    before_ids = {h["history_id"] for h in before}
    tagged = []
    for h in after:
        if h["history_id"] in before_ids:
            continue
        verdict = "真实更新历史(~)"
        if h["history_type"] == "+":
            if main_before_ids and h["product_id"] in main_before_ids:
                verdict = "假创建历史：主表本步无新写入，却出现 '+' 行"
            else:
                verdict = "真实创建历史：本步新插入主表行"
        tagged.append({**h, "verdict": verdict})
    return tagged


def seed_baseline():
    """相同起始记录：两条已存在 SKU，各带一条真实 '+' 历史（BASE-000）。"""
    for sku, name, price in [
        ("SKU-1001", "红色钢笔", 1000),
        ("SKU-1002", "蓝色铅笔", 500),
    ]:
        p = Product(sku=sku, name=name, price=price, source_batch="BASE-000")
        p._change_reason = "BASE-000"
        p._history_date = DATES["BASE-000"]
        p.save()


def run_scenario(title, batch_key, operation, narrative):
    print("\n" + "=" * 78)
    print(f"场景：{title}（批号 {batch_key}）")
    print("=" * 78)
    reset_tables()
    seed_baseline()
    main_before, hist_before = main_snapshot(), hist_snapshot()
    print(f"起始主表 {len(main_before)} 行；起始历史 {len(hist_before)} 行")

    operation()

    main_after, hist_after = main_snapshot(), hist_snapshot()
    main_diff = diff_main(main_before, main_after)
    main_before_ids = {row["id"] for row in main_before.values()}
    new_hist = diff_hist(hist_before, hist_after, main_before_ids)

    print("\n主表变化：")
    print(json.dumps(main_diff, ensure_ascii=False, indent=2))
    print(f"\n新增历史 {len(new_hist)} 行：")
    print(json.dumps(new_hist, ensure_ascii=False, indent=2))

    record = {
        "scenario": title,
        "batch": batch_key,
        "narrative": narrative,
        "main_diff": main_diff,
        "new_history": new_hist,
        "main_rows_after": main_after,
        "history_rows_after": hist_after,
    }
    findings.append(record)
    return record


# ---------------------------------------------------------------- 路线 A：save
def op_save():
    p = Product.objects.get(sku="SKU-1001")
    p.price = 1200
    p._change_reason = "BATCH-S1"
    p._history_date = DATES["BATCH-S1"]
    p.save()  # 更新既有 SKU
    new_p = Product(
        sku="SKU-1003", name="绿色尺子", price=800, source_batch="BATCH-S1"
    )
    new_p._change_reason = "BATCH-S1"
    new_p._history_date = DATES["BATCH-S1"]
    new_p.save()  # 新建 SKU


# ------------------------------------------------ 路线 B：QuerySet.update
def op_qs_update():
    # 与路线 A 相同的目标改动，但走 QuerySet.update
    Product.objects.filter(sku="SKU-1001").update(price=1200)
    Product.objects.filter(sku="SKU-1002").update(name="蓝色铅笔(改)")


# ---------------- 路线 C：bulk_create_with_history(ignore_conflicts=True)
def op_bulk_ignore_mixed():
    # 同一批：一个全新 SKU + 一个与库内既有 SKU 全字段一致的重放值
    batch = [
        Product(sku="SKU-1003", name="绿色尺子", price=800, source_batch="BATCH-S3"),
        Product(  # 与 SKU-1001 现状完全一致的重放
            sku="SKU-1001", name="红色钢笔", price=1000, source_batch="BASE-000"
        ),
    ]
    bulk_create_with_history(
        batch,
        Product,
        ignore_conflicts=True,
        default_change_reason="BATCH-S3",
        default_date=DATES["BATCH-S3"],
    )


def op_bulk_ignore_same_sku_diff_value():
    # 诊断变体：同 SKU 但字段值不同 → 主表冲突被忽略，重选条件匹配不到，历史也没有
    batch = [
        Product(
            sku="SKU-1001", name="红色钢笔", price=9999, source_batch="BATCH-S3B"
        ),
    ]
    bulk_create_with_history(
        batch,
        Product,
        ignore_conflicts=True,
        default_change_reason="BATCH-S3B",
        default_date=DATES["BATCH-S3B"],
    )


# ------------------------------------------------ 组合 C1：update + 冲突 + 重放
def run_combo():
    title = "组合：先 QuerySet.update，再送同值冲突批次，再整批重放一次"
    print("\n" + "=" * 78)
    print(f"场景：{title}")
    print("=" * 78)
    reset_tables()
    seed_baseline()
    steps = []

    # 步骤 1：update 改价（无历史）
    m0, h0 = main_snapshot(), hist_snapshot()
    Product.objects.filter(sku="SKU-1001").update(price=1300)
    steps.append(
        {
            "step": "1 QuerySet.update 改价 1000→1300",
            "batch": "BATCH-C1-UPDATE",
            "main_diff": diff_main(m0, main_snapshot()),
            "new_history": diff_hist(h0, hist_snapshot(), {r["id"] for r in m0.values()}),
        }
    )

    # 步骤 2：同值冲突批次（含一个新 SKU）
    m1, h1 = main_snapshot(), hist_snapshot()
    bulk_create_with_history(
        [
            Product(
                sku="SKU-1004", name="白色橡皮", price=300, source_batch="BATCH-C1-CONFLICT"
            ),
            Product(  # 按 SKU-1001 当前值（1300）原样重放
                sku="SKU-1001", name="红色钢笔", price=1300, source_batch="BASE-000"
            ),
        ],
        Product,
        ignore_conflicts=True,
        default_change_reason="BATCH-C1-CONFLICT",
        default_date=DATES["BATCH-C1-CONFLICT"],
    )
    steps.append(
        {
            "step": "2 bulk ignore_conflicts：新 SKU-1004 + SKU-1001 同值重放",
            "batch": "BATCH-C1-CONFLICT",
            "main_diff": diff_main(m1, main_snapshot()),
            "new_history": diff_hist(h1, hist_snapshot(), {r["id"] for r in m1.values()}),
        }
    )

    # 步骤 3：把步骤 2 的批次原封不动重放一次
    m2, h2 = main_snapshot(), hist_snapshot()
    bulk_create_with_history(
        [
            Product(
                sku="SKU-1004", name="白色橡皮", price=300, source_batch="BATCH-C1-CONFLICT"
            ),
            Product(
                sku="SKU-1001", name="红色钢笔", price=1300, source_batch="BASE-000"
            ),
        ],
        Product,
        ignore_conflicts=True,
        default_change_reason="BATCH-C1-REPLAY",
        default_date=DATES["BATCH-C1-REPLAY"],
    )
    steps.append(
        {
            "step": "3 同一冲突批次再重放一次",
            "batch": "BATCH-C1-REPLAY",
            "main_diff": diff_main(m2, main_snapshot()),
            "new_history": diff_hist(h2, hist_snapshot(), {r["id"] for r in m2.values()}),
        }
    )

    for step in steps:
        print(f"\n--- {step['step']}（批号 {step['batch']}）")
        print("主表变化：", json.dumps(step["main_diff"], ensure_ascii=False))
        print("新增历史：", json.dumps(step["new_history"], ensure_ascii=False, indent=2))

    findings.append(
        {
            "scenario": title,
            "batch": "BATCH-C1-*",
            "steps": steps,
            "main_rows_after": main_snapshot(),
            "history_rows_after": hist_snapshot(),
        }
    )


# ---------------- 旧迁移 C2：只保留最终表，无逐步记录
def run_legacy_final_only():
    title = "旧迁移：只保留最终表，沿哪一路发生不可判"
    print("\n" + "=" * 78)
    print(f"场景：{title}")
    print("=" * 78)
    target = {
        "SKU-1001": {"name": "红色钢笔", "price": 1300, "source_batch": "LEGACY"},
        "SKU-1005": {"name": "黄色便签", "price": 200, "source_batch": "LEGACY"},
    }

    # 路线甲：全部 save 到达同一最终态
    reset_tables()
    seed_baseline()
    p = Product.objects.get(sku="SKU-1001")
    p.price = 1300
    p.source_batch = "LEGACY"
    p._change_reason = "LEGACY-ROUTE-SAVE"
    p._history_date = datetime(2026, 11, 19, 10, 0, 0)
    p.save()
    p2 = Product(sku="SKU-1005", name="黄色便签", price=200, source_batch="LEGACY")
    p2._change_reason = "LEGACY-ROUTE-SAVE"
    p2._history_date = datetime(2026, 11, 19, 10, 5, 0)
    p2.save()
    via_save = {"main": main_snapshot(), "history": hist_snapshot()}

    # 路线乙：update + bulk ignore_conflicts 到达同一最终态
    reset_tables()
    seed_baseline()
    Product.objects.filter(sku="SKU-1001").update(price=1300, source_batch="LEGACY")
    bulk_create_with_history(
        [
            Product(
                sku="SKU-1005", name="黄色便签", price=200, source_batch="LEGACY"
            ),
            Product(  # 同值重放 SKU-1001 现状
                sku="SKU-1001", name="红色钢笔", price=1300, source_batch="LEGACY"
            ),
        ],
        Product,
        ignore_conflicts=True,
        default_change_reason="LEGACY-ROUTE-BULK",
        default_date=datetime(2026, 11, 19, 10, 10, 0),
    )
    via_bulk = {"main": main_snapshot(), "history": hist_snapshot()}

    def business_view(snap):
        # 比较最终表时剔除自增主键，只看业务字段（数仓面对的“最终状态”）
        return {
            sku: {k: v for k, v in row.items() if k != "id"}
            for sku, row in snap.items()
        }

    same_main = business_view(via_save["main"]) == business_view(via_bulk["main"])

    def tag_legacy(rows):
        seen_products = set()
        tagged = []
        for h in rows:
            verdict = "真实更新历史(~)"
            if h["history_type"] == "+":
                if h["product_id"] in seen_products:
                    verdict = "假创建历史：该 product_id 此前已有 '+' 行，主表并未新插入"
                else:
                    verdict = "真实创建历史"
            tagged.append({**h, "verdict": verdict})
            seen_products.add(h["product_id"])
        return tagged

    via_save["history"] = tag_legacy(via_save["history"])
    via_bulk["history"] = tag_legacy(via_bulk["history"])
    print(f"\n两条路线到达的最终主表完全一致：{same_main}")
    print(f"路线甲（save）历史行数：{len(via_save['history'])}")
    print(f"路线乙（update+bulk）历史行数：{len(via_bulk['history'])}")
    print("\n路线甲历史：")
    print(json.dumps(via_save["history"], ensure_ascii=False, indent=2))
    print("\n路线乙历史：")
    print(json.dumps(via_bulk["history"], ensure_ascii=False, indent=2))

    findings.append(
        {
            "scenario": title,
            "batch": "LEGACY",
            "final_main_identical": same_main,
            "via_save_history": via_save["history"],
            "via_bulk_history": via_bulk["history"],
        }
    )


def build_checklist():
    """从回放结果汇总数仓三类清单：缺历史 / 假历史 / 仍缺证据。"""
    missing, fake, real, evidence_gap = [], [], [], []

    single_scenarios = [f for f in findings if "main_diff" in f and "new_history" in f]
    for f in single_scenarios:
        changed_skus = [c["sku"] for c in f["main_diff"]["changed"]]
        hist_by_sku = {}
        for h in f["new_history"]:
            hist_by_sku.setdefault(h["sku"], []).append(h)
        for sku in changed_skus:
            if sku not in hist_by_sku:
                missing.append(
                    {
                        "batch": f["batch"],
                        "scenario": f["scenario"],
                        "sku": sku,
                        "reason": "主表确认改动，但该批无任何历史行（QuerySet.update 不发 post_save）",
                        "history_id": None,
                    }
                )
        # 主表本步完全无写入，却出现历史 → 假历史 / 静默吞写
        if not f["main_diff"]["added"] and not f["main_diff"]["changed"]:
            if f["new_history"]:
                for h in f["new_history"]:
                    fake.append(
                        {
                            "batch": f["batch"],
                            "scenario": f["scenario"],
                            "sku": h["sku"],
                            "history_id": h["history_id"],
                            "reason": h["verdict"],
                        }
                    )
            else:
                evidence_gap.append(
                    {
                        "batch": f["batch"],
                        "scenario": f["scenario"],
                        "sku": "(批次内全部对象)",
                        "reason": "主表与历史均无写入：ignore_conflicts 静默丢弃且按值重选落空，"
                        "需调迁移批次原始报文/日志才能证明该写入曾被尝试",
                        "needed_evidence": "该批号的原始批次报文（含对象与字段值）及 bulk_create 返回行数",
                    }
                )
        for h in f["new_history"]:
            if h["verdict"].startswith("假"):
                fake.append(
                    {
                        "batch": f["batch"],
                        "scenario": f["scenario"],
                        "sku": h["sku"],
                        "history_id": h["history_id"],
                        "reason": h["verdict"],
                    }
                )
            elif h["verdict"].startswith("真实"):
                real.append(
                    {
                        "batch": f["batch"],
                        "sku": h["sku"],
                        "history_id": h["history_id"],
                        "reason": h["verdict"],
                    }
                )

    combo = next(f for f in findings if f["scenario"].startswith("组合"))
    for step in combo["steps"]:
        changed = [c["sku"] for c in step["main_diff"]["changed"]]
        added = step["main_diff"]["added"]
        if changed and not step["new_history"]:
            for sku in changed:
                missing.append(
                    {
                        "batch": step["batch"],
                        "scenario": combo["scenario"],
                        "sku": sku,
                        "reason": "主表确认改动，但 update 步骤无历史行",
                        "history_id": None,
                    }
                )
        for h in step["new_history"]:
            entry = {
                "batch": step["batch"],
                "scenario": combo["scenario"],
                "sku": h["sku"],
                "history_id": h["history_id"],
            }
            if h["verdict"].startswith("假"):
                fake.append({**entry, "reason": h["verdict"]})
            else:
                real.append({**entry, "reason": h["verdict"]})
        if not changed and not added and step["new_history"]:
            for h in step["new_history"]:
                # 重放步骤：主表零写入，全部记为假历史（上面已收），补一条重放说明
                pass

    legacy = next(f for f in findings if f["scenario"].startswith("旧迁移"))
    for h in legacy["via_bulk_history"]:
        if h["verdict"].startswith("假"):
            fake.append(
                {
                    "batch": "LEGACY",
                    "scenario": legacy["scenario"],
                    "sku": h["sku"],
                    "history_id": h["history_id"],
                    "reason": "若旧迁移走 update+bulk 路线则为假创建；最终表本身无法判定路线",
                }
            )
    evidence_gap.append(
        {
            "batch": "LEGACY",
            "scenario": legacy["scenario"],
            "sku": "SKU-1001 / SKU-1005 等全部旧迁移行",
            "reason": "两条路线（纯 save vs update+bulk ignore_conflicts）到达业务上完全相同的最终表，"
            "但历史形态不同（'~' 真实更新 vs 多余 '+' 假创建）；仅凭最终表与值相等无法区分",
            "needed_evidence": "①旧迁移当时的操作入口日志/调度记录（save、QuerySet.update 或 "
            "bulk_create_with_history 之一）；②迁移批次的原始报文与 bulk_create 返回行数；"
            "③历史库在迁移时点的备份/binlog。补齐前不得按值相等补写历史",
        }
    )

    return {
        "1_缺历史_主表改动无历史": missing,
        "2_假历史_主表无新写入却有创建行": fake,
        "3_真实历史_对照基线": real,
        "4_仍缺证据_不得凭值补写": evidence_gap,
    }


def main():
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    call_command("migrate", run_syncdb=True, verbosity=0)

    run_scenario(
        "路线A 普通 save：更新 1001 + 新建 1003",
        "BATCH-S1",
        op_save,
        "save 触发 post_save 信号，更新与新建都应生成历史",
    )
    run_scenario(
        "路线B QuerySet.update：更新 1001/1002",
        "BATCH-S2",
        op_qs_update,
        "QuerySet.update 是 SQL 级批量 UPDATE，不发 post_save，历史表无任何新增",
    )
    run_scenario(
        "路线C bulk_create_with_history(ignore_conflicts=True)：新 SKU + 同值重放",
        "BATCH-S3",
        op_bulk_ignore_mixed,
        "新 SKU 真实入库并带真实 '+' 历史；同值重放被 INSERT OR IGNORE 丢弃，"
        "但工具按字段值重选主表，把既有行又写了一条 '+' 假创建历史",
    )
    run_scenario(
        "路线C诊断变体：同 SKU 异值冲突",
        "BATCH-S3B",
        op_bulk_ignore_same_sku_diff_value,
        "冲突被忽略且重选条件匹配不到任何行，主表与历史都无写入，静默吞掉",
    )
    run_combo()
    run_legacy_final_only()

    checklist = build_checklist()
    with open(
        os.path.join(REPORT_DIR, "warehouse_checklist.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(checklist, f, ensure_ascii=False, indent=2)
    with open(os.path.join(REPORT_DIR, "replay_findings.json"), "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=2)
    print("\n三类清单：")
    print(json.dumps(checklist, ensure_ascii=False, indent=2))
    print(f"\n结构化结果已写入 {REPORT_DIR}")


if __name__ == "__main__":
    main()

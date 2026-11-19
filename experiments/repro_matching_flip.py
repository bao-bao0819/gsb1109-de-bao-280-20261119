"""
可复跑实验：ignore_order 下列表元素的配对选择在什么时候改变。

运行方式（在仓库根目录）：
    .venv/bin/python experiments/repro_matching_flip.py

同一份录入清单 INPUT，分别只改 ID、描述、阈值，观察 DeepDiff 报告何时在
  - 字段级 values_changed
  - 整条记录 iterable_item_added + iterable_item_removed（删除 + 新增）
之间翻转。

结论对应的源码位置（deepdiff 版本见 ../deepdiff/__init__.py）：
  1. 完全相等的元素按 DeepHash 直接匹配，不进入配对流程。
  2. 交集门：diff.py `_diff_iterable_with_deephash`
       (added+removed)/(t1 元素数+t2 元素数+1) > cutoff_intersection_for_pairs(默认 0.7)
     时根本不做配对，元素按位置报整项 values_changed。
  3. 距离门：diff.py `_get_most_in_common_pairs_in_iterables`
       两个候选元素的 deep_distance >= cutoff_distance_for_pairs(默认 0.3) 即拒绝配对
     （注意是 >=，距离恰好 0.3 也会被拒绝）。
  4. 收尾合并：model.py `TreeResult.mutual_add_removes_to_become_value_changes`
     同一路径上的 added/removed 会被合并成 values_changed；但 iterable_match_keys
     产生的项带 paired_by_key=True（diff.py:1095/1106），不参与合并，
     因此“按 ID 分组”时改 ID 会稳定地呈现为删除 + 新增。
"""

from deepdiff import DeepDiff

# ----------------------------------------------------------------------
# 同一份录入清单（t1）。所有变体都从它派生，每次只动一个维度。
# 描述用单词列表表示，便于让“大改描述”产生可计算的配对距离。
# ----------------------------------------------------------------------
INPUT = [
    {"id": 1, "desc": ["the", "quick", "brown", "fox"], "threshold": 100.0},
    {"id": 2, "desc": ["keep"],                     "threshold": 200.0},
]
RECORD_1, RECORD_2 = INPUT

DESC_REWRITTEN = ["completely", "different", "wording", "now"]

MATCH_BY_ID = {"root": "id"}


def kind(report):
    """把报告归类成便于断言的形态。"""
    if not report:
        return "empty"
    if "iterable_item_added" in report or "iterable_item_removed" in report:
        return "added+removed"
    if "values_changed" in report:
        return "values_changed"
    return str(sorted(report))


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def check(label, report, expected):
    actual = kind(report)
    status = "OK " if actual == expected else "FAIL"
    print(f"[{status}] {label}: {actual}")
    print(f"       {report}")
    assert actual == expected, f"{label}: expected {expected}, got {actual}"


# ----------------------------------------------------------------------
# A. “按 ID 分组” = iterable_match_keys={"root": "id"} + ignore_order=True
#    配对只认 id 键，cutoff_* 两个阈值在这条路径上完全被旁路。
# ----------------------------------------------------------------------
section("A. 按 ID 分组（iterable_match_keys）：改 ID 才会删除+新增")

id_changed = [{**RECORD_1, "id": 999}, RECORD_2]
desc_changed = [{**RECORD_1, "desc": DESC_REWRITTEN}, RECORD_2]
threshold_changed = [{**RECORD_1, "threshold": 0.0}, RECORD_2]
reordered = [RECORD_2, RECORD_1]

check("A1 改 ID 1->999",
      DeepDiff(INPUT, id_changed, ignore_order=True, iterable_match_keys=MATCH_BY_ID),
      "added+removed")
check("A2 大改描述（ID 不变）",
      DeepDiff(INPUT, desc_changed, ignore_order=True, iterable_match_keys=MATCH_BY_ID),
      "values_changed")
check("A3 阈值 100->0（ID 不变）",
      DeepDiff(INPUT, threshold_changed, ignore_order=True, iterable_match_keys=MATCH_BY_ID),
      "values_changed")
check("A4 仅调换顺序",
      DeepDiff(INPUT, reordered, ignore_order=True, iterable_match_keys=MATCH_BY_ID),
      "empty")

# 无论怎么调 cutoff，按 ID 分组的结果都不变——阈值管不到这条路径。
baseline = DeepDiff(INPUT, id_changed, ignore_order=True, iterable_match_keys=MATCH_BY_ID)
for kwargs in (
    {"cutoff_distance_for_pairs": 0.01},
    {"cutoff_distance_for_pairs": 1.0, "cutoff_intersection_for_pairs": 0.01},
):
    assert DeepDiff(INPUT, id_changed, ignore_order=True,
                    iterable_match_keys=MATCH_BY_ID, **kwargs) == baseline
print("[OK ] A5 调整 cutoff_distance/intersection 对按 ID 分组的结果无影响")

# ----------------------------------------------------------------------
# B. 不用 match keys，纯 ignore_order：配对由 deep_distance 与
#    cutoff_distance_for_pairs 决定。先看候选对之间的实际距离。
# ----------------------------------------------------------------------
section("B. 纯 ignore_order：描述大改的配对距离 vs cutoff_distance_for_pairs")

pair_distance = DeepDiff(RECORD_1, desc_changed[0], get_deep_distance=True)["deep_distance"]
print(f"记录1 改写描述后的 deep_distance = {pair_distance:.4f}（4 个单词全换 = 4/22）")
assert 0.1 < pair_distance < 0.3

print("\nB1. 位置不变时，即使配对被拒绝，同路径 added/removed 也会被合并回 values_changed：")
check("B1a cutoff=0.3 默认（0.1818<0.3，接受配对，下钻到单词）",
      DeepDiff(INPUT, desc_changed, ignore_order=True),
      "values_changed")
check("B1b cutoff=0.1（0.1818>=0.1，拒绝配对，整项对比）",
      DeepDiff(INPUT, desc_changed, ignore_order=True, cutoff_distance_for_pairs=0.1),
      "values_changed")
assert "root[0]" in DeepDiff(INPUT, desc_changed, ignore_order=True,
                             cutoff_distance_for_pairs=0.1)["values_changed"]
print("       注意：cutoff=0.1 时路径是 root[0] 整条记录，而非 root[0]['desc'][i]")

print("\nB2. 同时调换顺序时，配对被拒绝会导致路径错位，合并失效 -> 删除+新增：")
reordered_and_rewritten = [RECORD_2, desc_changed[0]]
check("B2a cutoff=0.3 默认（接受跨位置配对）",
      DeepDiff(INPUT, reordered_and_rewritten, ignore_order=True),
      "values_changed")
check("B2b cutoff=0.1（拒绝配对：root[0] 删除 + root[1] 新增）",
      DeepDiff(INPUT, reordered_and_rewritten, ignore_order=True,
               cutoff_distance_for_pairs=0.1),
      "added+removed")
check("B2c cutoff=1.0（强制接受配对，回到字段级修改）",
      DeepDiff(INPUT, reordered_and_rewritten, ignore_order=True,
               cutoff_distance_for_pairs=1.0),
      "values_changed")

# ----------------------------------------------------------------------
# C. 阈值（数值字段）这一维度：记录级距离只数“改动的叶子数”，与数值幅度无关。
# ----------------------------------------------------------------------
section("C. 改阈值：数值幅度不影响记录级距离，平坦记录永远过不了 0.3 的门")

flat = [
    {"id": 1, "desc": "alpha", "threshold": 100.0},
    {"id": 2, "desc": "beta", "threshold": 200.0},
]
flat_record = flat[0]
d_small = DeepDiff(flat_record, {**flat_record, "threshold": 101.0},
                   get_deep_distance=True)["deep_distance"]
d_huge = DeepDiff(flat_record, {**flat_record, "threshold": 1000.0},
                  get_deep_distance=True)["deep_distance"]
print(f"100->101 的记录级距离   = {d_small:.4f}")
print(f"100->1000 的记录级距离  = {d_huge:.4f}  （相同：都是 1 个叶子/14）")
assert d_small == d_huge == 1 / 14
check("C1 平坦记录改阈值，默认 cutoff",
      DeepDiff(flat, [{**flat_record, "threshold": 0.0}, flat[1]], ignore_order=True),
      "values_changed")

# 只有在“纯数字列表”这种最短结构里，数值幅度本身才决定配对，
# 且边界是 >=：100 与 0 的距离恰好 0.3，默认即被拒绝配对。
d_boundary = DeepDiff(100.0, 0.0, get_deep_distance=True)["deep_distance"]
print(f"\n纯数字 100.0 vs 0.0 的距离 = {d_boundary}，恰好等于默认 cutoff，按 >= 规则拒绝配对")
assert d_boundary == 0.3
check("C2 纯数字列表 [100]->[0]（单元素残留仍被同路径合并）",
      DeepDiff([100.0], [0.0], ignore_order=True),
      "values_changed")

# ----------------------------------------------------------------------
# D. 交集门 cutoff_intersection_for_pairs：改动的记录占比太高时，整体不配对。
# ----------------------------------------------------------------------
section("D. 两条记录都改：比例 4/5=0.8 > 0.7，交集门直接关闭配对")

both_changed = [
    {**RECORD_1, "desc": ["the", "quick", "brown", "cat"]},
    {**RECORD_2, "desc": ["kept"]},
]
ratio = 4 / 5
print(f"added+removed 哈希数 / 总哈希数 = {ratio} > 0.7")
default_report = DeepDiff(INPUT, both_changed, ignore_order=True)
open_report = DeepDiff(INPUT, both_changed, ignore_order=True,
                       cutoff_intersection_for_pairs=1.0)
check("D1 默认 0.7：不配对，按位置报整项 values_changed", default_report, "values_changed")
check("D2 放宽到 1.0：恢复配对，下钻到具体单词", open_report, "values_changed")
assert "root[0]" in default_report["values_changed"]
assert any(path.startswith("root[0]['desc']") for path in open_report["values_changed"])

print("\n全部断言通过。")
print("""
差异解释速查
------------
- 按 ID 分组（iterable_match_keys）：身份就是 id。
  * 改 ID -> 旧 id 匹配不上 -> 删除旧记录 + 新增新记录，且 paired_by_key 阻止合并。
  * 改描述/阈值、调顺序 -> id 仍匹配 -> 字段级 values_changed，顺序变化报告为空。
  * cutoff_distance_for_pairs / cutoff_intersection_for_pairs 对此路径无效。
- 纯 ignore_order（无 match keys）：身份由 DeepHash + deep_distance 贪心配对决定。
  * 交集门（默认 0.7）：改动元素占比超过阈值则整轮不配对，只按位置出整项结果。
  * 距离门（默认 0.3，>= 即拒绝）：描述大改使距离越过 cutoff 时配对被拒；
    若同时发生重排，路径错位让同路径合并失效，就出现删除 + 新增。
    把 cutoff 调到 1.0 可强制配对、恢复字段级修改报告。
  * 记录级距离 = diff 操作数 / 两侧叶子总数，只数改动叶子数；
    所以平坦记录上无论阈值数值改多大，距离都只有 1/14≈0.0714，不会越过 0.3。
""")

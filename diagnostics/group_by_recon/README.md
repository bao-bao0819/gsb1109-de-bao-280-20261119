# group_by / ignore_order 对账歧义定位（固定快照取证）

目标：同一份物料目录开启 `group_by` + `ignore_order=True` 后，预期的一条
描述更新有时变成"一删一增"。本目录只用**固定快照 + 可复跑插桩**定位原因，
不修改任何 deepdiff 算法，也不把结论简化为"调阈值"。

## 业务前提

- 业务身份必须由 `(supplier, id)` 共同确认；单独的 `id` 不唯一。
- 供应商 A、B 各有一条 `id=7`；A 另有 `id=8`。
- 某些导入会把两条 `id=7` 的顺序互换。
- 描述可以被大幅改写。

## 文件与运行

- `snapshots.py`：固定输入快照（含两种导入顺序、两个对照、组内多行、交集门控数据）。
- `run_recon.py`：取证脚本，临时包裹两个内部方法，不改库代码：
  - `_group_iterable_to_dict` 的实际产物（哪些记录真正进入比较）；
  - `_get_most_in_common_pairs_in_iterables` 的候选、深距离、最终成配对；
  - `_report_result` 在合并前的原始 `iterable_item_added/removed` 事件。
- `expected_output.txt`：一次参考运行的完整输出。

```bash
.venv/bin/python diagnostics/group_by_recon/run_recon.py
# 可复现性自检（两次运行应无差异）
.venv/bin/python diagnostics/group_by_recon/run_recon.py > /tmp/run2.txt
diff diagnostics/group_by_recon/expected_output.txt /tmp/run2.txt
```

## 使用的选项

- 有损输入 A：`group_by='id', ignore_order=True`，并遍历
  `cutoff_distance_for_pairs in {1.0, 0.3, 0.05, 0.0}`。
- 忠实输入 B：`group_by=lambda r: (r['supplier'], r['id'])`
  加 `group_by_sort_key='id'`（保留每个业务键下的实际各条），
  `ignore_order=True`，阈值取 `{1.0, 0.3, 0.05}`。
- 对照 C：仅改 `id`（描述几乎不变）与仅微调描述（`id` 不变）。
- 门控 D：`cutoff_intersection_for_pairs` 取 `0.7`（默认）与 `0.5`。

## 三条必须区分的路径

### 路径 1：记录在进入配对前就被覆盖（有损分组）

`deepdiff/diff.py:2390` 在单键 `group_by` 且无 `group_by_sort_key` 时执行
`result[key1] = row`，同一键的后一条直接覆盖前一条。

- 分组后 t1 的键 `7` 只剩 B-7，A-7 在任何距离计算之前就消失。
- SWAP 顺序下 t2 的键 `7` 只剩 A-7，于是 B-7 被当成改成了 A-7，
  报告 `root[7]` 下供应商与描述同时 `values_changed`。
- ORIG 顺序下 t2 的键 `7` 只剩 B-7，与 t1 存活的 B-7 完全相同，报告为空——
  A-7 的真实描述改写被**静默吞掉**。
- 四个 `cutoff_distance_for_pairs` 输出完全相同：该路径根本不经过无序配对，
  调距离阈值对它无效。

### 路径 2：记录仍在候选中，但因距离没配上（忠实分组 + 阈值）

忠实分组下 A-7 的两个版本都保留，真实深距离为 `0.0714`。

- `cutoff=0.3`（默认）/`1.0`：`0.0714 < cutoff`，配对成功，报描述级
  `values_changed`（`root['A'][7][0]['desc']`）。
- `cutoff=0.05`：`0.0714 >= cutoff`，`deepdiff/diff.py:1609` 丢弃该候选，
  配对数为 0，但记录始终在候选集合里，没有被覆盖。
- 这才是阈值真正起作用的路径，与路径 1 是不同根因。

### 路径 3：业务身份本身改变（组键变化）

- 多组并存时，`id 8 -> 9`（或供应商变化）使业务键消失/新增，报
  `dictionary_item_removed: root['A'][8]` 与
  `dictionary_item_added: root['A'][9]`，即组级增删。
- 描述编辑发生在存活组内部，始终是 `values_changed`，不会产生组增删。
- 注意：单组数据时整根字典会退化成 `values_changed: root`，因此需要多组
  快照才能稳定观察到身份变更的增删类别。

## "一删一增"从哪里来，以及顺序互换为何关键

1. 配对失败时，引擎先分别报 `iterable_item_added` 与
   `iterable_item_removed`（见 `deepdiff/diff.py:1954` 与 `:1978`）。
2. `deepdiff/model.py:59` 的
   `mutual_add_removes_to_become_value_changes` 会把**同一路径**上的
   删+增合并成 `values_changed`。
   - 同索引同路径（如两侧都是 `root['A'][7][0]`）→ 合并，最终只见
     `values_changed`，看不到底下其实发生过删+增。
   - 不同索引路径（默认 `cutoff_intersection=0.7` 的多变动列表里，
     增在 `root[0]`、删在 `root[1]`）→ 不满足同路径条件，
     `iterable_item_added/removed` 被保留，表现为"一删一增"。
3. 交集门控（`deepdiff/diff.py:1792`）：
   `(added+removed)/(总哈希数+1) > cutoff_intersection_for_pairs` 时
   完全跳过配对。同一数据在 `0.7` 下保留增删，在 `0.5` 下门控关闭配对、
   退回按位置比较，只剩 `values_changed`。
4. 两条 `id=7` 互换位置的影响仅作用于**有损分组**：覆盖取"每组最后一条"，
   顺序决定了 t1/t2 各自存活的是 A-7 还是 B-7，从而在"错误的供应商+描述
   变更"与"空报告"之间切换。忠实分组用 `(supplier,id)` 作键，行序不改变
   分组结果，互换位置不影响结论。

## 为什么只看最终差异无法分辨路径

- 路径 1（覆盖）与路径 2（距离拒配）在某些输入上都可能只给出
  `values_changed`，但一个记录已在配对前消失、另一个仍在候选中；
  最终报告不含"哪些记录进入了比较"这一信息。
- 同路径的原始删+增已在 `model.py` 合并，最终字典里看不到
  `iterable_item_added/removed`，无法回溯配对是否失败。
- 路径 1 对 `cutoff_distance_for_pairs` 完全不敏感，路径 2 才敏感；
  仅靠"调阈值后结果是否变化"也无法区分，必须像本脚本一样同时固定
  分组产物、候选距离、成配对数和合并前事件。

## 结论（仅定位，不改算法）

- "一删一增"与"一条描述更新"之间的摇摆，至少由三个相互独立的机制叠加：
  单键 `group_by` 的同键覆盖、距离/交集阈值对配对的控制、同路径增删的
  事后合并。
- 复现与核实以 `run_recon.py` 的四段输出为准；每段都同时给出进入比较的
  实际记录、候选与距离、成配对和最终差异类别。
- 本目录未改动 `deepdiff/` 下任何源码。

## 附：取证中发现的一个非确定性

`deepdiff/model.py:74` 的 `mutual_paths` 用普通 `set` 推导，其遍历顺序受
`PYTHONHASHSEED` 影响。差异**类别与路径集合**完全稳定，但多路径合并后
`values_changed` 的列出顺序在不同进程间可能互换（如 `root[0]`/`root[1]`）。
`run_recon.py` 在打印最终报告时对路径排序，因此参考输出可逐字节复现；
已用 `PYTHONHASHSEED=1` 与 `42` 验证一致。这也说明直接对最终字典做字符串
比较会引入与业务无关的噪声，比较时应按路径集合而非顺序判断。

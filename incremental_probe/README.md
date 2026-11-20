# 增量数据工厂下 Patsy 分类因子冻结诊断

同一公式在整表 `dmatrix` 上能出矩阵，切到 `incr_dbuilder` + 分块回放后却报
“类别不匹配”。本目录用本地 patsy 源码（未改任何 patsy 文件）复现并定位：
模式是在 **构建期类型检查的第一块** 被冻结的，整表可跑并不能保证增量接口可跑。

## 运行方式

```bash
# 带逐块扫描日志的完整探针
python3 incremental_probe/probe_incr_patsy.py

# 可重复的 pytest 证据（若环境装了 rerunfailures 需禁用其套接字）
python3 -m pytest -p no:rerunfailures incremental_probe/ -v
```

探针在运行时包装 `guess_categorical` 与 `CategoricalSniffer.sniff` 记录判定，
不修改 patsy 源码。验证环境：Python 3.14 / pandas 3.0.3 / numpy 2.4.6 /
本地 patsy 1.0.3。

## 冻结实际发生在哪一步

- 入口 `patsy/highlevel.py:66` `incr_dbuilder` →
  `patsy/build.py:693` `design_matrix_builders`。
- 真正冻结模式的是 `patsy/build.py:485` `_examine_factor_types`：它逐块迭代，
  对每个因子调 `guess_categorical`（`patsy/categorical.py:132`）。
  - 数值判定后立即 `examine_needed.remove(factor)`（`patsy/build.py:498-504`），
    后续分块对该因子 **不再检查**，因子被永久记为 numerical。
  - 分类分支由 `CategoricalSniffer.sniff`（`patsy/categorical.py:189`）处理，
    返回 `done=True` 时同样移出检查集（`patsy/build.py:496-497`）。
- 回放阶段 `build_design_matrices` → `_eval_factor`（`patsy/build.py:66`）按冻结
  下来的 FactorInfo 走数值或分类两条不同路径，分类校验集中在
  `categorical_to_int`（`patsy/categorical.py:327`）。

## 四条可区分的路径（均有小数据实证）

探针每次都记录“实际扫描到哪块、最终 levels、最终列顺序”。

1. 首块使因子过早成为数值（P1）
   - 首块 `grp` 全空且被上游推成 `float64`：`guess_categorical` 对 float 返回
     False，检查在 chunk0 即终止，因子冻结为 numerical，列是
     `['Intercept', 'x', 'grp']`，chunk1 的分类列根本没被看到。
   - 回放 chunk1 时数值分支报 `non-numeric data of type 'object'`
     （字典/ndarray 载体可看到这条本意的 PatsyError）。
   - pandas 3 附带次生问题：`_eval_factor` 报错句引用 `result.dtype`
     （`patsy/build.py:83`），而 `preserve_pandas=True` 下 result 是 DataFrame，
     DataFrame 已无 `.dtype`，本意错误被 `AttributeError` 掩盖。这是 patsy 自身
     缺陷，本诊断不修改它，只在证据中并列展示。
   - 对照：首块同为全空但保持 `object` dtype 时，sniffer 不结束、继续读到
     chunk1 的 Categorical，因子正确冻结为分类，levels=`('c','a','b')`，
     列顺序 `['Intercept', 'grp[T.a]', 'grp[T.b]', 'x']`。

2. 类型检查采用首个明确类别声明（P2）
   - 公式写 `C(grp, levels=['c','a','b'])` 时，无论首块是 float 还是 object，
     `_CategoricalBox.levels` 在 chunk0 即令 sniffer `done=True`
     （`patsy/categorical.py:193-196`），levels 冻结为该声明，扫描只到 chunk0。
   - 后块若给出冲突的 `C(levels=...)` 声明，因因子已离开检查集而不会被采用；
     回放时 `categorical_to_int` 的 box 分支（`patsy/categorical.py:342-349`）
     抛 `mismatching levels`。首个明确声明赢。

3. 预测载体水平不匹配（P3，值都已知）
   - 自然列构建器（levels=`('c','a','b')`）遇到声明为
     `categories=['a','b']` 的 pandas Categorical：
     `categorical_to_int` 的 pandas 分支（`patsy/categorical.py:331-337`）
     直接比较 categories 元组，抛
     `mismatching levels: expected ('c', 'a', 'b'), got ('a', 'b')`。
   - 显式 `C()` 构建器对同一载体却放行：求值时列被重新装箱成带冻结 levels 的
     `_CategoricalBox`，pandas 自带 categories 不再被比较，改走按标签值映射
     （实测 `b→2, a→1`）。两条公式行为不同，证据中分别列出。

4. 真正的未知标签（P4）
   - 载体含训练期未出现的 `'zzz_new'`（object Series）：任何构建器都在
     `categorical_to_int` 的逐项映射处抛
     `observation with value 'zzz_new' does not match any of the expected levels`
     （`patsy/categorical.py:372-387`）。这与 P3 的“水平声明不一致”是不同错误。

## 为什么整表可跑不是增量接口的保证

把 float 全空首块与后续字符串块 `pd.concat` 后，列被治愈成 `object`，一次性
`dmatrix` 成功（但 levels 按字典序变成 `a,b,c`，列名 `grp[T.b]/grp[T.c]`）。
增量接口的类型检查是 **逐块** 的：float 首块已单独把因子冻结成 numerical，
拼接后的“治愈”不会发生。因此整表通过不能为增量流水线背书。

## 上游修正（维持原训练列顺序，不动 Patsy）

见 `upstream_schema.py` 的 `normalize_block`，在数据工厂出口、进入 patsy 前对
每个分块执行：

- `reindex(columns=TRAIN_COLUMNS)` 强制原训练列顺序 `['x', 'grp']`；
- 先按冻结 levels 校验标签，未知标签在边界显式拒绝（避免 pandas 静默转 NaN）；
- 把 `grp` 统一构造成 `pd.Categorical(values, categories=('c','a','b'))`，
  全空首块也携带显式 levels，于是首块即被识别为分类且 levels 一次冻结正确。

修正后（探针 SECTION 5/6）：自然列公式在首块即冻结为分类，
levels=`('c','a','b')`；重排载体经规范化后放行，未知标签在边界被拒；
有效已知样本的增量矩阵与按既定顺序训练的整表基准在 **列顺序和数值** 上完全一致
（EQUIVALENCE: PASS，pytest 中固化为断言）。

## 交付文件

- `probe_incr_patsy.py`：带运行时埋点的完整复现，输出存于 `last_run.txt`。
- `upstream_schema.py`：上游 schema 闸门（修正方案，无 patsy 改动）。
- `test_incremental_freeze.py`：7 条 pytest，用实际值固化上述四条路径与基准等价。

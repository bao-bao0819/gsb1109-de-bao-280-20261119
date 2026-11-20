# 本地最小反例：混合 PDF 账单里金额在哪一层消失

目标：在**完全离线、不接 OCR 平台**的前提下，定位 beangulp 示例抽取流程
丢失金额的层级，并留下财务可查看、可重跑的证据。

金额消失只有两个互斥层级，本反例把它们分开：

1. **转换层**：金额只存在于整页图像里，PDF 文本层没有金额 token
   （`no_usable_amount_text`）。
2. **插件规则层**：金额能从文本层读到，但行布局不符合插件的列假设，
   被行规则拒绝（`layout_mismatch`）。

另有一页正文与合法模板匹配，作为**阳性对照**，证明插件在假设成立时
确实能产生分录（`extracted`）。

## 四个页的设计

样本：`pdf/mixed_statement.pdf`（4 页，数字 `1,250.00` 出现三次、身份各不相同）。

| 页 | 结构 | 文本层里的金额 | 肉眼可见金额（身份） | 分类 | 插件结果 |
|---|---|---|---|---|---|
| 1 | 整页 JPEG 图像，无文本层 | 无 | 1,250.00（7110 TRANSFER OUT） | `scan_without_text` | `no_usable_amount_text`，0 分录 |
| 2 | 整页 JPEG 图像 + 仅页眉原生文本（34 个字母数字，过 20 字符阈值） | 无 | 1,250.00（7111 TRANSFER IN） | `scan_with_text` | `no_usable_amount_text`，0 分录 |
| 3 | 原生数字文本，无图像 | 有 `1,250.00`，但列序为 ISO 日期/金额/描述 | 1,250.00（7112 ATM FEE） | `digital` | `layout_mismatch`，0 分录 |
| 4 | 原生数字文本，列序合法（MM/DD/YYYY 描述 金额） | `48.20`、`-130.75` | 同文本层 | `digital` | `extracted`，2 分录 |

要点：

- **页 2 不能因为"有文本"就算金额已提取**：页眉文本通过了分类器的
  `min_text_chars=20` 检查，但不含任何金额 token，插件状态仍是
  `no_usable_amount_text`。
- **全文搜 `1,250.00` 会命中三个不同身份的交易**，只有页 3 的数字在
  文本层里；`tools/track_amounts.py` 按页核对身份（7110/7111/7112），
  避免用相同数字反推目标页已被抽到。

## 环境约束与等价实现

本机离线，`pymupdf` 无法安装（PyPI 不可达），`pdftotext` 也不可用，
仓库自带的 `examples/importers/acme.py` 在本机直接因
`FileNotFoundError: pdftotext` 失败。因此：

- `tools/pdf_probe.swift`（CoreGraphics + PDFKit）：逐页输出文本层和
  每个栅格图像的 bbox，等价于 PyMuPDF 的 `get_text()` 与
  `get_image_info()` 两个信号；不做渲染 OCR。
- `tools/mirror_classify.py`：用与 `beangulp.pdf._union_area` 相同的
  矩形并集算法和相同阈值（0.8 / 20 字符）复刻 `classify_pdf()` 的标签，
  输出字段与 `tools/classify_pdf.py` 一一对应。装上 pymupdf 后可直接
  用原工具对同一 PDF 复核，结论应一致。
- `tools/render_pdf.swift`：把每页渲染成 PNG，供财务肉眼核对像素里的
  金额（扫描页的金额只在这些 PNG 里可见）。

## 一键重跑

```bash
bash casework/tools/run_all.sh
```

会依次：生成 PDF → 编译 Swift 工具 → 探针与分类 → 导出每页文本和
PNG → 回放行抽取 → 跑 8 项 pytest。全部离线，约几秒。

## 只换文本即可复核：转换还是插件条件

插件入口 `importer/statement_plugin.py::extract_lines(text)` 只吃单页
文本，不碰 PDF。把转换结果换成新的文本文件即可单独重放：

```bash
# 阶段一：OCR 把金额带进了文本（转换层已修复），但行仍是图像里的
# Posting Ref 布局，插件依旧拒绝 -> layout_mismatch
.venv/bin/python casework/tools/run_case.py --page 1 \
    casework/pages/alt/page1_ocr_raw.txt

# 阶段二：同一插件，只把文本规范化成合法行模板 -> extracted，1 条分录
.venv/bin/python casework/tools/run_case.py --page 1 \
    casework/pages/alt/page1_ocr_normalized.txt
```

这组对照（`casework/tests/test_statement_plugin.py` 中
`test_swap_text_only_conversion_then_layout_stage` 锁定）说明：
先补转换才能拿到金额 token，再改行布局才能过插件规则，两者缺一不可。

## 证据与产物索引

- `pdf/mixed_statement.pdf` — 最小反例 PDF（可由 `make_pdf.py` 重建）
- `pages/page{1..4}.txt` — 每页真实文本层（页 1 为空、页 2 仅页眉）
- `pages/alt/` — OCR 原始文本与规范化文本两种替换输入
- `evidence/probe_raw.json` — 逐页文本层 + 图像 bbox 探针结果
- `evidence/classification.json` — 镜像 `classify_pdf()` 的逐页分类
- `evidence/extraction_report.json` — 每页金额 token、插件状态、拒绝原因、分录
- `evidence/entries.beancount` — 实际产生的分录（仅页 4 有 2 条）
- `evidence/summary.md` — 一页式财务对照表
- `evidence/render/page{1..4}.png` — 每页渲染图，肉眼核对像素金额
- `evidence/upstream_*_probe.json` — 对仓库自带两份 PDF 的探针对照
  （均为 0 图像的数字文本页）
- `tests/test_statement_plugin.py` — 8 项回归测试

## 结论

- 缺可用金额文本的页：**页 1、页 2**（金额仅在栅格像素；页 2 的页眉
  文本层不含金额，不能据此认定已提取）。
- 有原始金额但被规则拒掉的页：**页 3**（金额在文本层，行布局违反
  MM/DD/YYYY + 描述 + 金额在末列的假设，拒绝原因逐条记录在
  `extraction_report.json`）。
- 插件本身能出分录的证据：**页 4** 产生 2 条 Beancount 交易。
- 下次复核只需替换 `pages/` 下的文本：状态从
  `no_usable_amount_text` 变为 `layout_mismatch` 说明是转换问题已解决、
  卡在插件条件；变为 `extracted` 说明两层都通过。

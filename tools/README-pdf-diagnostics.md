# PDF 导入两阶段诊断工具

排查"PDF 里看得见金额，导入后缺页/空值"时，把流程拆成两个独立阶段，
分别用两个入口检查：

## 阶段一：PDF → 文本（转换层）

```sh
.venv/bin/python tools/pdf_extract.py document.pdf [-o build/pdf_text]
```

- 优先调用 `pdftotext`（与 `examples/importers/acme.py` 完全相同的
  `pdftotext file -` 调用）；本机没有 poppler 时自动回退到
  `tools/pdfkit_text.swift`（macOS PDFKit，首次运行自动用 `swiftc` 编译）。
- 每个 PDF 产出 `build/pdf_text/<name>.txt`（页与页之间用换页符 `\f`
  分隔，与 `pdftotext` 一致）和 `<name>.txt.json` sidecar。
- sidecar 记录 `pdf_page_count`、`text_page_count`、`missing_pages` 和
  逐页 `empty_pages`（无任何字母数字的页）。整页丢失或空白页属于
  **转换层空值**，字段规则不可能从不存在的文本里恢复数据。
- `--strict` 会在出现空页/缺页时以非零状态退出。

## 阶段二：文本 → 分录（字段识别规则层）

```sh
.venv/bin/python tools/validate_pdf_text.py build/pdf_text/<name>.txt
```

- **只读取 `.txt` 文本**，不再触碰原始 PDF；把 `.txt` 单独拷到任何地方
  都能重跑，这就是文本侧的验证入口。
- 每条发现标注层级：
  - `conversion`：该页没有可提取文本（或通过 `--expect-pages N` /
    sidecar 发现整页缺失）。修复方向是 OCR 或更换转换后端。
  - `recognition`：文本存在，但字段规则没匹配上或拒绝了值，对应真实
    插件行为，例如 `acme.py` 的 `re.search("Date: ...")` 返回空、
    `csvbase.Amount.parse("")` 抛 `decimal.InvalidOperation`、
    `csvbase.Date` 严格 `strptime` 拒绝非法日期。
- `--json` 输出机器可读结果；`--strict-rules` 让识别层发现也触发非零退出。
- 无 sidecar 时，空页仍可由文本中的换页符计数发现；若连换页符都被
  转换器吞掉，用 `--expect-pages` 传入已知 PDF 页数即可报出缺页。

## 可区分的示例

`tools/make_sample_pdf.swift` 生成两页诊断样本 `build/sample_statement.pdf`：

- 第 1 页原生文本，含一行缺金额的交易（`11/19/2026 Lunch`）和一行非法
  日期（`11/99/2026 ...`）——文本都在，报 `recognition`；
- 第 2 页完全没有文字层——报 `conversion`，与字段规则无关。

复现命令：

```sh
./.tmp/make_sample_pdf build/sample_statement.pdf   # 或 swift 直接编译运行
.venv/bin/python tools/pdf_extract.py build/sample_statement.pdf
.venv/bin/python tools/validate_pdf_text.py build/pdf_text/sample_statement.txt
```

真实样本 `examples/Downloads/Statement.pdf` 还演示了另一种识别层问题：
文本忠实转出 `Date: March 31,2016`（逗号后无空格），但
`dateutil` 4.x 将其解析成 2026 年而非 2016 年——转换无误，是字段规则
对文本的解释问题。

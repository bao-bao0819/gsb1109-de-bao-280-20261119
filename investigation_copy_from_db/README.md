# COPY FROM DATABASE 装载告警 · 可复跑分诊手册（值班员版）

只观察**固定版 SQLFluff**，不改库、不连库。PostgreSQL 配置迁往 DuckDB 后那几次
“看起来一样”的装载告警，根因其实有三种，本手册用可复跑的对照把它们分开。

## 1. 固定版本与事实基线

- SQLFluff `4.5.0`（本仓库的可编辑安装，`.venv`），Jinja2 `3.1.6`，Python 3.12。
- 不需要安装 duckdb Python 包，也不需要真实数据库：使用的是 SQLFluff 内置
  **duckdb 方言语法**，语句形态取自固定夹具
  `test/fixtures/dialects/duckdb/copy_from_database.sql`
  （`COPY FROM DATABASE <src> TO <dest>;`）。
- 合法形态在 duckdb 方言可解析；postgres 方言的 COPY 语法不含 `FROM DATABASE`，
  因此同样的完整 COPY 在 postgres 方言下被拒（这是方言差异，不是缺参数）。

一键复跑（纯 CLI，证据落盘到 `evidence/`）：

```bash
cd investigation_copy_from_db
bash run_cli_matrix.sh          # 全矩阵 lint + 计时 + 渲染 + 逐字比对
../.venv/bin/python run_investigation.py   # 带位置/错误范围/规则阶段的结构化证据 -> evidence/findings.json
```

> 本机沙箱会阻止 SQLFluff 向上遍历父目录找配置，所有 CLI 都已带
> `--ignore-local-config`，只用本目录 `configs/` 里的显式配置。

## 2. 小模板（装载语句只放在一个 Jinja 宏里）

`template/load_copies.sql`，关键源位置：

| 源位置 | 内容 | 角色 |
| --- | --- | --- |
| L7 | `{% macro load_copies(param_src) -%}` | 宏定义 |
| **L8** | `COPY FROM DATABASE pg_meta ...; COPY FROM DATABASE {{ param_src }} ...;` | 宏体内两条 COPY（第二条的 `{{` 起于 L8:42） |
| L10 | `{% if load_branch -%}` | **宏外的 Jinja 开关**（装载 / SELECT 对照） |
| L11 | `{% for param_src in params.split(",") -%}` | 循环；`params` 在 **L11:21** |
| **L12** | `{{ load_copies(param_src) }}` | **宏调用源行**；一次调用发出 L8 那两条 COPY |
| L15 | `select  a from t` | SELECT 对照（L15:7 有双空格，稳定触发 LT01） |

三种外部参数输入都经 `[sqlfluff:templater:jinja:context]` 注入：

- 正常值：`params = pgdb`
- 显式空串：`params =`（INI 保留为空字符串 `""`，键**存在**）
- 缺失键：该 context 段里**不写** `params`（键不存在）

## 3. 实际规则配置（两方言刻意不同）

两方言默认规则包同为 79 条且代码一致，因此这里**显式**配置成不同启用集，
让“同一条 SELECT 两边都能解析，但规则配置不同”可被观察：

| 方言 | 启用规则 | 含义 |
| --- | --- | --- |
| duckdb | `LT01,AM04` | layout.spacing；ambiguous.column_count |
| postgres | `LT01,CP01` | layout.spacing；capitalisation.keywords |

`LT01` 在两方言都对 SELECT 的双空格触发，证明规则阶段确实执行；差异化规则
（AM04 vs CP01）证明两边配置不同。规则“实际进入规则阶段”的证据有两份：

- CLI 计时 CSV（如 `evidence/cli/duck_empty.timing.csv`）含独立列 `AM04,LT01`；
  pg 文件含 `CP01,LT01`。
- `evidence/findings.json` 每个用例的 `rules_that_ran` 列出真正跑过的规则码。

## 4. 核心对照：缺失键 vs 显式空串（两者渲染成逐字相同的主 SQL）

以 duckdb 装载分支为例（postgres 主 SQL 文本相同，仅解析范围不同）：

| 维度 | 缺失键 `duck_missing` | 显式空串 `duck_empty` |
| --- | --- | --- |
| 主渲染（变体 v0）SQL | `... COPY FROM DATABASE  TO analytics;`（双空格、库名为空） | **逐字相同** |
| 主 SQL 哈希 sha256 | `11fc4eea500e73c8…` | `11fc4eea500e73c8…`（`cmp` 判定 BYTE-IDENTICAL，见 `evidence/cli/cmp_missing_vs_empty.txt`） |
| 宏调用源位置 | **L12:1**（同一条 `load_copies` 调用） | **L12:1**（同一处） |
| 渲染位置 | L3:42 起 | L3:42 起 |
| SQL 解析告警 | `PRS` L12:1，圈中 `COPY FROM DATABASE  TO analytics;`（len=33） | **完全相同的 PRS** |
| Jinja 证据 | 另有 **`TMP` L11:21 `Undefined jinja template variable: 'params'`** | 无 TMP |

结论：**同一份主 SQL、同一个宏调用源位置、同一条 PRS**，唯一能区分二者的是
Jinja 层证据——缺失键多一条 `TMP`（机制：`UndefinedRecorder.__str__` 把未定义
变量渲染成空串并记名，见 `src/sqlfluff/core/templaters/jinja.py:165`）。
空串则是参数已给、但拼出的装载 SQL 本身不完整，因此没有 TMP，只有 PRS。

## 5. 每个具体 COPY 出现的三因判定（决策规则）

对宏体 L8 发出的**每一条** COPY 单独定性，禁止用同处另一条的结果代证：

1. **键缺失（Jinja 问题）**：出现 `TMP`（未定义变量）→ 查 Jinja 证据。
   即使它恰好渲染成空库名并伴随 PRS，根因仍是参数没传进来。
2. **显式空串（装载 SQL 不完整）**：无 TMP，但参数化那条 COPY 渲染成
   `COPY FROM DATABASE  TO ...;` 并被解析器拒（两方言都拒）。参数已给，是 SQL 残缺。
3. **完整 COPY（仅方言差异）**：参数化 COPY 完整（如 `... pgdb ...`）时，
   duckdb 方言 `CLEAN`，**只有** postgres 方言拒绝 → 这才是 PostgreSQL→DuckDB
   的方言差异。隔离单语句基线（`controls/`，raw 渲染器）：

| 隔离语句 | duckdb | postgres |
| --- | --- | --- |
| `copy_fixed_only.sql`（固定有效库名） | CLEAN | PRS |
| `copy_param_complete.sql`（参数完整） | CLEAN | PRS |
| `copy_param_empty.sql`（空库名） | PRS | PRS |
| `select_control.sql` | LT01（可解析） | LT01（可解析） |

**同源于 L12 的另一条 COPY、或自动补出的 SELECT，即使能解析或只报规则，也不能
给失败的那次出现充当通过证据。** 根变体存在时，补渲染变体的解析失败不计入失败，
但其规则告警会并入（机制见 `linter.py:757` 起）；所以“SELECT 报了 LT01”只能证明
规则阶段跑过，不能证明装载 COPY 正确。

## 6. 反转参数顺序与“错误范围”陷阱

把参数顺序反转后喂给**同一个循环**（`params = ,pgdb` 空在前 / `params = pgdb,`
空在后），两条 COPY 仍由同一宏调用行发出，但解析器圈选的范围不同（duckdb）：

| 用例 | 坏 COPY 位置 | PRS 源位置 | 错误范围（渲染坐标） | 圈入文本长度 |
| --- | --- | --- | --- | --- |
| `rev_empty_first`（空在前） | 第一次迭代 | L11:42 | L3:42 → L4:79 | **112**（吞掉坏句 + 后面整行好 COPY） |
| `rev_empty_last`（空在后） | 第二次迭代 | L12:1 | L4:42 → L4:75 | **33**（只圈坏句自身） |

postgres 方言更宽：任一顺序都倾向从 L3:1 起把整段圈进一个不可解析区间
（`rev_*` 长度 153）。因此**不要用错误范围的宽窄或起点判断“哪一条先出错”**：
范围受语句顺序和方言恢复策略影响。要定位具体出错 COPY，用第 5 节的隔离单语句
对照（`controls/copy_param_empty.sql` 等）逐句核实。

## 7. 自动补渲染（SELECT 分支）与四个单维对照

开关 `load_branch=false` 时，主渲染即 SELECT；装载分支下 SQLFluff 还会自动补渲染
出未进入的 SELECT 变体（`render` 输出里的 Variant 2）。两方言下该 SELECT 都能
解析并只报 LT01，但启用规则集不同（AM04 vs CP01）。

用主渲染的**同一份 SQL** 关闭模板（`--templater raw`）复跑，每次只改一个维度：

| 对照（相对基线 empty/duck） | 结果 | 说明 |
| --- | --- | --- |
| 基线：empty 主 SQL，duckdb | PRS | 残缺 SQL |
| **只补参数**（normal 主 SQL，duckdb） | CLEAN | 补齐即合法，证明 duckdb 语法没问题 |
| **只换方言**（同一份 empty SQL，postgres） | PRS | 残缺在两方言都失败 |
| **只切分支**（SELECT 主 SQL，两方言） | 仅 LT01 | SELECT 可解析，规则阶段照跑 |
| 完整 normal 主 SQL 换 postgres | PRS | 完整 COPY 仅 postgres 拒 → 方言差异 |

## 8. 位置无法直接取得时

模板映射拿不到精确位置时，用**可复跑的单调用对照**核实：把单次宏调用产物落成
`rendered/*_v0.sql`，再以 `--templater raw` 对该纯 SQL 复跑（见 `run_cli_matrix.sh`
第 4、5 段与 `controls/`），即可在脱离 Jinja 的情况下确认是哪一条 COPY、在哪个
渲染位置失败，再经切片映射回到模板 L8/L12。

## 9. 产物清单

- `template/load_copies.sql`：小模板（宏 + 宏外开关 + 循环）。
- `configs/*.sqlfluff`：两方言 × 正常/空串/缺失/SELECT/两种反转顺序，共 12 份。
- `controls/*.sql`：隔离单语句与 SELECT 基线。
- `rendered/`：各变体的字面 SQL（用于逐字比对与关模板复跑）。
- `evidence/findings.json`：全矩阵的变体、TMP/PRS、错误范围、源/渲染位置、规则阶段。
- `evidence/cli/*.json`、`*.timing.csv`：纯 CLI 的告警与每规则计时。
- `evidence/cli/cmp_missing_vs_empty.txt`：缺失键与空串主 SQL 字节相同的证明。
- `evidence/render/*.render.txt`、`cli_run.log`：渲染变体与完整运行日志。
- 驱动：`generate_configs.py`、`export_renders.py`、`run_investigation.py`、
  `run_cli_matrix.sh`、`summarize.py`。

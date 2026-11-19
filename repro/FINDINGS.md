# Beanquery 价格日期与反向报价复现

## 交付物

- `repro/minimal-oct-only.bean`：只有一个 2026-10-31 来源价，无 11 月价格。
- `repro/minimal.bean`：同上，追加 2026-11-20 收盘价。
- `repro/minimal-two-sources.bean`：2026-10-31 同时有反报价 `CNY 0.08 AAA`（前）和正报价 `AAA 10.00 CNY`（后），无 11 月价格。
- `repro/minimal-two-sources-today.bean`：同上，追加 2026-11-20 收盘价。
- `repro/minimal-two-sources-forward.bean`：同日两条来源价，正报价在前、反报价在后，无 11 月价格。
- `repro/minimal-two-sources-forward-today.bean`：同上，追加 2026-11-20 收盘价。
- `repro/archived-before.bean` / `repro/archived-after.bean`：模拟已归档的命名报表，在补价前后各重跑一次。
- `repro/reproduce.py`：带断言的一键复跑脚本。

所有场景持有 `100 AAA`，成本 `10.00 CNY`，成本额 `1000.00 CNY`；`AAA` 对 `CNY` 的十月来源价均折算为 `10.00`。

```bash
.venv/bin/python repro/reproduce.py
```

脚本正常退出（退出码 0）表示所有观测值仍与结论一致。

## 日期参数何时参与找价

调用链是：

- BQL `getprice(base, quote, date)` → `prices.get_price(price_map, pair, date)`（`beanquery/query_env.py:518`）
- BQL `value(pos[, date])` → `convert.get_value(..., date)` → `prices.get_price(...)`（`beanquery/query_env.py:502`，`.venv/lib/python3.12/site-packages/beancount/core/convert.py:109`）

`prices.get_price()` 对 `date` 的处理只有两条分支（`.venv/lib/python3.12/site-packages/beancount/core/prices.py:347`）：

- `date is None`：委托 `get_latest_price()`，取该货币对**整份账簿的最后一个价点**，与报表所属月份无关。
- `date` 是具体日期：在该货币对的价点序列上做 `bisect_right`，取 `<= date` 的最后一个价点；若 `date` 早于第一个价点，返回 `(None, None)`，`value()` 便原样返回持仓而不折算。

因此只有函数签名里真正带上日期实参时，日期才参与找价。`FROM ... CLOSE ON <date>` 不在此列：它只在 `_BeancountTable.prepare()` 里裁剪分录（`beanquery/sources/beancount.py:261`），价表 `PricesTable.price_map` 仍由**全量 entries** 构建（`beanquery/sources/beancount.py:177`）。命名查询的 `Query` 日期也只经 `BQLShell.parse()` 填入缺省的 `from_clause.close`（`beanquery/shell.py:386`），不会传给 `getprice()` 或 `value()`。

同理，`WHERE date <= 2026-10-31` 只是过滤被求值的持仓行，不会成为估值日。实测 `minimal-two-sources-today.bean` 上这两种写法仍分别得到 `12.00`（不传日期）与 `12.50`（显式传 10-31），与不加 `WHERE` 时一致。

场景 1（`minimal-oct-only.bean`）说明“没有歧义时三种写法一致”：显式传 `2026-10-31`、不传日期、不传日期加 `CLOSE ON 2026-10-31` 都是 `rate=10.00`、`value=1000.00 CNY`。此时账簿里唯一的价点就是十月末价，默认值恰好等于历史值。

场景 2（`minimal.bean`）只补一条 `2026-11-20 price AAA 12.00 CNY`，账簿无任何反向报价：

| 查询写法 | rate | value |
| --- | --- | --- |
| `getprice(..., 2026-10-31)` / `value(..., 2026-10-31)` | 10.00 | 1000.00 CNY |
| 不传日期 | 12.00 | 1200.00 CNY |
| 不传日期 + `CLOSE ON 2026-10-31` | 12.00 | 1200.00 CNY |

这里 10.00 → 12.00 的变化**完全来自查询默认**：显式日期的归档报表仍是 1000.00，只有省略日期（或以为 `CLOSE` 会顺带定住估值日）的报表跳到 1200.00。补价没有改变任何历史价点，`AAA/CNY` 在 10-31 仍是 10.00。

## 反向报价的顺序如何影响结果

`build_price_map()` 先按 `(base, quote)` 收集原始价点，再把互为倒数的两向合并（`.venv/lib/python3.12/site-packages/beancount/core/prices.py:60`）。关键在 `prices.py:102-113`：

- `remove` 取**价点条数较少**的一向；条数相等时 `len(bq) < len(qb)` 为假，于是删除 `(quote, base)`，即保留**先出现在 `price_map` 字典里的那一向**（账簿中先出现的那个方向）。
- 被删一向的每个价点取倒数后 `extend` 到保留一向的列表尾部。
- 随后 `sorted_uniquify(..., last=True)`（`prices.py:119`）按日期排序去重，**同一天保留排序后位于最后的那条**。`sorted` 是稳定排序，同日原有价点在前、后追加的倒数在后，因此同日冲突时**数值上胜出的是被删一向的隐含汇率**：条数不等时是少数派，条数相等时是后出现的那个方向。

具体到本账簿，十月同日两条来源为 `CNY 0.08 AAA` 与 `AAA 10.00 CNY`。二者隐含汇率不一致（`1/0.08 = 12.5`，而正报是 `10.00`），属于**账簿自身的价格冲突**，与查询默认无关。

场景 3a / 3b（反报在前）：

| 账簿 | 两向条数 | 被删一向 | 10-31 合并后的 `AAA/CNY` | 显式 10-31 | 不传日期 |
| --- | --- | --- | --- | --- | --- |
| `minimal-two-sources.bean`（无 11 月价） | 反 1 : 正 1 | 正报（后出现） | 10.00（`forward_pairs` 为 `CNY/AAA`） | 1000.00 CNY | 1000.00 CNY |
| `minimal-two-sources-today.bean`（补 11-20 价） | 反 1 : 正 2 | 反报（少数派） | **12.50** | **1250.00 CNY** | 1200.00 CNY |

无 11 月价时两向各一条，删除后出现的正报，保留反报并把正报的倒数 `1/10.00 = 0.10` 追加到 `CNY/AAA`，同日 `last=True` 取到 0.10，于是 `AAA/CNY` 仍是 10.00。补上 11-20 后正报变成两条、反报仍一条，改为删除反报，把 `1/0.08 = 12.5` 追加到正报价点尾部，同日覆盖掉原来的 10.00，于是 10-31 的历史价变成 12.50。

这解释了两个看似无关的现象同时发生：显式传上月末日期的归档报表从 1000.00 变成 1250.00（**账簿价格冲突**，且被新价点触发的条数变化改变），而省略日期的报表从 1000.00 变成 1200.00（**查询默认**取到了 11-20 的 12.00）。

场景 3c / 3d（正报在前）作对照：

| 账簿 | 两向条数 | 被删一向 | 10-31 合并后的 `AAA/CNY` | 显式 10-31 | 不传日期 |
| --- | --- | --- | --- | --- | --- |
| `minimal-two-sources-forward.bean`（无 11 月价） | 正 1 : 反 1 | 反报（后出现） | 12.50 | 1250.00 CNY | 1250.00 CNY |
| `minimal-two-sources-forward-today.bean`（补 11-20 价） | 正 2 : 反 1 | 反报（少数派） | 12.50 | 1250.00 CNY | 1200.00 CNY |

正报在前时，删除后出现的反报，反报的倒数 12.5 覆盖同日正报的 10.00，所以十月价从一开始就是 12.50，补不补 11 月价都不影响显式查询；补价只改变“不传日期”的取值。可见“先写哪一向”决定同日冲突的数值胜者，新增价点通过改变两向条数，又可能把胜者换掉——3a → 3b 正是这种情况。

## 哪些变化来自查询默认，哪些属于账簿自身的价格冲突

- **查询默认**：`getprice()` / `value()` 省略日期时取全账簿最新价。场景 2 的 10.00 → 12.00、场景 3b 与 3d 的“不传日期”列都是这一类。`CLOSE ON` 和命名查询的 `Query` 日期都不会改变这一点，只有把日期写进函数实参才能钉住历史估值。
- **账簿自身的价格冲突**：同日同时存在 `AAA → CNY` 与 `CNY → AAA` 且隐含汇率不一致（10.00 对 12.50）。场景 3a/3b 中显式 10-31 从 1000.00 变成 1250.00，属于这一类；它与 11-20 的 12.00 无关，是补价改变了两向条数、进而改变合并方向的副作用。

归档报表 `archived-*.bean` 把两类变化叠在一起：`month_end`（不传日期）在补价后由 1000.00 变 1200.00，纯属默认最新价；`month_end_pinned`（函数内写死 2026-10-31）由 1000.00 变 1250.00，则是账簿冲突被新价点触发。两者数值不同，正好可作为区分依据。

## 复跑结果摘要

```
Scenario 1  minimal-oct-only.bean             explicit 10.00 / default 10.00 / close 10.00
Scenario 2  minimal.bean                      explicit 10.00 / default 12.00 / close 12.00
Scenario 3a minimal-two-sources.bean          explicit 10.00 / default 10.00 / close 10.00
Scenario 3b minimal-two-sources-today.bean    explicit 12.50 / default 12.00 / close 12.00
Scenario 3c minimal-two-sources-forward.bean  explicit 12.50 / default 12.50 / close 12.50
Scenario 3d minimal-two-sources-forward-today  explicit 12.50 / default 12.00 / close 12.00
Archived before  month_end 10.00   month_end_pinned 10.00
Archived after   month_end 12.00   month_end_pinned 12.50
```

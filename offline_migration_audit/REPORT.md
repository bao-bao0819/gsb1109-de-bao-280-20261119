# 离线商品迁移：主表与审计历史对不上的因果回放与数仓清单

## 1. 结论速览

在 django-simple-history 现有三个写入入口上用相同起始记录回放后，三类异常的因果全部可复现、可区分：

| 入口 | 主表 | 历史表 | 性质 |
|---|---|---|---|
| 普通 `Model.save()` | 改动/插入均落库 | 更新写 `~`、新建写 `+`，一一对应 | 正常基线，无漏记 |
| `QuerySet.update()` | SQL 级 UPDATE 落库 | **零新增** | 缺历史（漏记，非冲突） |
| `bulk_create_with_history(ignore_conflicts=True)` | 仅未冲突对象插入 | 新对象真实 `+`；**被忽略的同值冲突对象也被按主表现状补写 `+`** | 同批并存真实历史与假创建历史 |
| 同上，冲突对象字段值与库内不一致 | 主表零写入（INSERT 被 IGNORE） | **零新增**（按值重选落空） | 静默吞写，需外部证据才能证明曾尝试写入 |

关键代码因果（本仓库版本，未做任何修改）：

- `simple_history/models.py:653` 的 `post_save` 只挂在 Django 信号上；`QuerySet.update()` 是 SQL 批量 UPDATE，不发 `post_save`，因此历史表完全不参与——这是**缺历史**的根因，与冲突无关。
- `simple_history/utils.py:124-178`：`ignore_conflicts=True` 时第一事务的 `bulk_create` 拿不到主键（第 129 行条件显式排除该情形），进入第二事务；第二事务用每个输入对象的**字段值**构造 `Q` 回主表重选（第 145-168 行），把匹配到的行**无条件按 `history_type='+'` 写历史**（`manager.py:228`）。被 IGNORE 的同值冲突对象其值恰好匹配库内既有行，于是既有行被写成一条“创建”历史——**主表本步没有新写入，却出现创建行**，即假历史。
- 若冲突对象的值与库内行不一致（如价格不同），重选 `Q` 匹配不到任何行，主表与历史都没有痕迹——写入被静默丢弃。

## 2. 回放设计（不触碰任何已有审计数据）

- 独立工程 `offline_migration_audit/`，独立 sqlite 库 `replay.db`，每次运行删库重建；仓库自带的测试模型与历史数据全程未读未写。
- 回放模型 `replay_app/models.py`：`Product(sku unique, name, price, source_batch)` + `HistoricalRecords()`，与线上商品表约束同构。
- 每个批号通过 `_change_reason` / `default_change_reason` 落到 `history_change_reason`，并用固定 `history_date` 保证时序可辨；数仓可直接按批号 + `history_id` 定位。
- 相同起始记录：`SKU-1001`、`SKU-1002` 各一条真实 `+` 基线历史（批号 `BASE-000`）。

场景清单：

1. 路线 A `BATCH-S1`：`save()` 更新 1001 + 新建 1003。
2. 路线 B `BATCH-S2`：`QuerySet.update()` 更新 1001、1002。
3. 路线 C `BATCH-S3`：同一 bulk 批次含新 SKU-1003 和与 1001 现状全字段一致的重放值，`ignore_conflicts=True`。
4. 路线 C 变体 `BATCH-S3B`：同 SKU 异值冲突。
5. 组合 `BATCH-C1-*`：先 update 改价 1001（无历史）→ 同值冲突批次（新 1004 + 重放 1001）→ 整批再重放一次。
6. 旧迁移 `LEGACY`：两条不同路线（纯 save / update+bulk）推演到业务上完全相同的最终表。

## 3. 数仓三类清单

机器可读版本：`reports/warehouse_checklist.json`；完整回放流水：`reports/replay_findings.json`。
下列 `history_id` 为回放库中的值，用于演示定位方式；线上排查时以对应批号在历史表中查出的真实 `history_id` 为准。

### 3.1 缺历史：主表确有改动但未生成历史（确属漏记，建议补录）

| 批号 | SKU | 证据 | 为什么是漏记而非重复/冲突 |
|---|---|---|---|
| `BATCH-S2` | SKU-1001 | 主表 price 1000→1200，历史表该批 0 行 | 走的是 `QuerySet.update`，Django 不发 `post_save`，simple_history 无任何钩子被触发；不存在冲突对象，也不存在可重复的历史行 |
| `BATCH-S2` | SKU-1002 | 主表 name 变更，历史表该批 0 行 | 同上 |
| `BATCH-C1-UPDATE` | SKU-1001 | 主表 price 1000→1300，update 步骤历史 0 行 | 同上；后续 `BATCH-C1-CONFLICT` 出现的 1001 `+` 行是 bulk 路线制造的**假创建**，不能当作这次改价的历史 |

补录方式：用 `bulk_update_with_history`（本库 `utils.py:181`，写 `~` 型）或对锁定后的对象逐个 `save()` 补一条 `~` 历史，`history_change_reason` 写明源批号与“补录”字样；补录前先按第 3.2 节剔除同 SKU 的假 `+` 行，避免把假创建误当重复而漏补。

### 3.2 假历史：主表无新写入却出现疑似创建行（建议标记/隔离，不可当真）

| 批号 | SKU | 回放 history_id | 证据 |
|---|---|---|---|
| `BATCH-S3` | SKU-1001 | 15 | 主表对 1001 零写入，却新增 `+` 行，`product_id` 指向操作前已存在的主表行 |
| `BATCH-C1-CONFLICT` | SKU-1001 | 26 | 同上；同批 SKU-1004（id 25）是真实插入的真 `+`，同批一真一假 |
| `BATCH-C1-REPLAY` | SKU-1004 | 27 | 重放时主表零写入，1004 既有行被再次写成 `+` |
| `BATCH-C1-REPLAY` | SKU-1001 | 28 | 同上，重放一次就多一条假 `+`，重放 N 次累积 N 条 |

判定规则（回放脚本已自动标注）：`history_type='+'` 且该行的主表主键在操作开始前已存在 → 假创建；主表主键本步才出现 → 真创建。重放场景证明：**假历史会随重放无限累积，且两条假行字段值完全相同，单看历史内容无法去重，必须结合批号与主表写入时间判断。**

### 3.3 仍缺证据：不得凭值相等补写

| 批号 | 情形 | 还需哪一项证据 |
|---|---|---|
| `BATCH-S3B` | 同 SKU 异值冲突：主表、历史双双零写入，系统内无任何痕迹 | 该批号的**原始批次报文**（对象与字段值）+ `bulk_create` 返回行数，才能证明写入曾被尝试且被 IGNORE |
| `LEGACY`（只保留最终表的旧迁移） | 纯 save 与 update+bulk 两条路线到达**业务上完全相同的最终表**，但历史形态不同：save 路线给 1001 留 `~` 真实更新；bulk 路线给 1001 留多余的 `+` 假创建（回放 history_id 42） | ①旧迁移的操作入口/调度日志（确认是 save、`QuerySet.update` 还是 `bulk_create_with_history`）；②批次原始报文与 bulk 返回行数；③迁移时点的历史库备份或 binlog。**三者补齐前，不得因为“值与最终表相等”就补写历史**——值相等在 bulk 路线下恰恰是假历史的产生条件 |

## 4. 能区分两个原因的回放对照

“主表与历史对不上”有两个独立原因，回放给出了可区分的判别特征：

1. **漏记（信号绕过）**：主表 diff 非空、历史新增为 0 ⇒ 只可能是 `QuerySet.update` 路线（路线 B、组合步骤 1）。
2. **假历史（按值重选）**：主表 diff 为空、历史却新增 `+`，且 `+` 行的主表主键在操作前已存在 ⇒ 只可能是 `bulk_create_with_history(ignore_conflicts=True)` 路线（路线 C、组合步骤 2/3）。
3. 两者在组合场景中同时出现且互不掩盖：update 的改价在步骤 1 确定漏记，bulk 步骤不会替它补上；bulk 制造的假 `+` 也不会让漏记变得“看起来有历史”。
4. 异值冲突（S3B）是第三种形态：主表与历史都为空，系统内不可证，必须靠批次报文——这把“可在库内定性”与“必须外部取证”的边界划清了。

## 5. 复现方式

```bash
cd offline_migration_audit
../.venv/bin/python run_replay.py      # 重建 replay.db 并跑全部场景
../.venv/bin/python assert_findings.py # 22 项因果断言，全 PASS 才算复现成功
```

- `run_replay.py`：场景、快照 diff、真伪标注、清单生成。
- `assert_findings.py`：固化每条因果特征，防止结论来自偶然。
- `reports/`：`replay_findings.json`（逐步流水）、`warehouse_checklist.json`（三类清单）。
- 诊断全程只读使用 simple_history 源码与公共 API，未修改库代码，也未写入任何既有审计数据；所有写入只发生在每次重建的 `replay.db`。

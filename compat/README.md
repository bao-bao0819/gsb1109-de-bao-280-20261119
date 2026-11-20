# 采集接口兼容性对照

本目录用两份 OpenAPI 3.0 契约比较“旧采集器/旧解析器是否还能继续工作”。只分析契约，不生成客户端代码。

> 工作区原先只包含 `apispec` 源码快照，没有随附任务中的两份 APISpec。以下契约是按题意重建的最小可复现输入；判定器本身接受任意路径的 OpenAPI 3 文档。若拿到真实契约，把 `specs/` 下文件替换为真实版本即可，结论规则不变。

## 输入

| 文件 | 含义 |
| --- | --- |
| `specs/old.yaml` | 旧契约：两个 bulk 分支都接受 `marker: bulk`，`region` 查询参数可选 |
| `specs/renamed.yaml` | 只把循环组件 `EventNode`/`EventEdge` 改名为 `RecordNode`/`RecordEdge`，并调换 `oneOf` 分支顺序 |
| `specs/narrowed-bulk.yaml` | 把 `bulk` 从 items 分支的 `marker.enum` 中移除，其余不变 |
| `specs/tightened-request.yaml` | 把 `region` 查询参数从可选改为必填 |

操作位置固定为 `paths./events.post`，响应位置为
`paths./events.post.responses.200.content['application/json'].schema`。

响应分支用 `x-collector-table` 标明落表：`bulk_count` 或 `bulk_items`。这是分表标记，不是组件名推断。

## 结论

| 对照 | 请求 | 响应 | 判定 |
| --- | --- | --- | --- |
| 旧 vs 仅重命名/换序 | 等价 | 等价 | 不是数据接口破坏 |
| 旧 vs bulk 从 items 分支移除 | 等价 | 破坏 | 新响应不能继续分表 |
| 旧 vs `region` 收紧为必填 | 破坏 | 等价 | 旧请求不能继续发送 |

### 仅重命名/换序

- `EventNode -> RecordNode`、`EventEdge -> RecordEdge` 的本地 `$ref` 形成一一对应，并且递归到循环引用后终止。
- `required`、`enum`、`type`、`items` 以及未建模关键字的值都没有变化。
- `oneOf` 的顺序不影响匹配语义；分支按表标记和必填字段对应。
- 因此新契约接受的有效响应集合与旧契约相同，不能判为数据接口破坏。

### 新响应不能继续分表

反例（`narrowed-bulk.yaml`）：

```json
{"marker": "bulk", "count": 1, "items": [{"id": "node-1"}]}
```

- 位置：`paths./events.post.responses.200.content['application/json'].schema.oneOf`
- 新契约：items 分支的 `marker.enum` 只剩 `single`，该报文只命中 count 分支（1 支）。
- 旧契约：count 分支和 items 分支都接受 `bulk`，同一报文命中 2 支。
- 旧解析器把“命中两支”视为不合格，无法决定送入 `bulk_count` 还是 `bulk_items`。因此这是**新响应不能继续分表**，不是请求发送问题。

### 旧请求不能继续发送

反例（`tightened-request.yaml`）：

```http
POST /events
X-Tenant: tenant-a
Content-Type: application/json

{"kind": "single", "payload": "hello"}
```

没有 `region` 查询参数。

- 位置：`paths./events.post.parameters[query:region].required`
- 旧契约：`required: false`，上述请求合法。
- 新契约：`required: true`，上述请求被拒绝。
- 因此这是**旧请求不能继续发送**；采集器若仍按旧规范省略 `region`，新服务端会返回参数校验错误。它与响应分表是两类不同的破坏。

## 判定范围

判定器 `contracts.py` 只使用以下规则证明行为：

- 对象 `required`
- 标量 `enum`
- `oneOf`
- 文档内本地 `$ref`（`#/...`）
- 为到达上述规则而需要的结构化 `type` 和数组 `items`

兼容方向分开处理：

- 请求：旧契约允许的请求必须仍被新契约允许（旧 ⊆ 新）。
- 响应：新契约某个分支允许的响应必须仍被旧契约同一分支允许，且落同一张表（新 ⊆ 旧）。

样例只用于给出**反例见证**，不用来证明兼容。兼容结论来自逐字段结构比较和引用双射；只靠“组件改名了”或“几条样例能过”不会得出兼容。反例中的数值（`count: 1`、单元素 `items`、`id` 长度）都落在两版声明的其他边界之内，因此“命中两支”不是忽略 `minimum`/`minItems` 造成的假象。

以下情况会明确报告为 `UNKNOWN`，不会当成兼容：

- 变化的 `allOf`、`anyOf`、`not`、`minimum`、`maximum`、`minLength`、`maxLength`、`pattern`、`minItems`、`maxItems`、`additionalProperties`、`discriminator` 等未建模关键字；
- 外部引用或只在一侧出现的引用；
- 响应分支缺少分表标记；
- 新响应出现旧契约没有的字段；
- 旧请求字段在新契约中消失；
- 嵌套 `oneOf`；
- 非对象/非数组且无法用 `required`/`enum` 证明的 schema。

## 运行

```bash
python -m compat.report
python -m pytest tests/test_compat_contracts.py -q
```

`compat.report` 输出四组比较的结论、具体 JSON 位置和反例；测试覆盖重命名等价、分支换序、bulk 双分支反例、请求收紧反例，以及未知范围。

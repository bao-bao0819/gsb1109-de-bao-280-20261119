# PolyModelType 认领行为排查报告（Schematics 2.1.1 固定版）

- 环境：工作区源码快照 `schematics/__init__.py`（2.1.1），`.venv` Python 3.12.13，库文件零改动。
- 复跑：`.venv/bin/python poly_claim_investigation.py`（21 项自检全部 PASS 时退出码 0）。
- 场景：同一时间戳、同一单位值的 Sensor / Manual 两条记录；原始键 `sensor_tag` / `manual_tag`
  可区分来源，转换后都存成 `marker`；另有未声明认领函数的后备类型 `LegacyReading`，
  以及一条类型选对但单位非法（`unit=celsius`）的记录，全部放进外层
  `ListType(PolyModelType(...))`。

## 机制定位（库内位置）

- `PolyModelType._convert`（`schematics/types/compound.py:382`）：
  `context.validate` 为假时，已是合法模型实例的输入**直接原样返回，跳过认领**（:387-389）；
  为真时（一切验证阶段）捷径被跳过，**对任何输入重新 `find_model`**（:399）。
- `find_model`（`schematics/types/compound.py:402`）：无 `claim_function` 时逐个调用
  `_claim_polymorphic`；**第一个不声明认领函数的类成为静默兜底**（:411-428）；
  歧义或无匹配抛的是**裸 `Exception`**（:408、:431），不是 `BaseError`。
- 验证阶段入口：`validation_converter`（`schematics/transforms.py:386`）→
  `BaseType.validate` 先 `convert`（`schematics/types/base.py:317`）→ 回到 `_convert`，
  此时 `context.validate=True`。
- `Model.__init__(validate=True)`（`schematics/models.py:232-238`）：先以 `validate=True`
  跑一遍 `_convert`，**再调一次 `self.validate()`** —— 构造即验证实际执行**两遍**验证。
- 重复认领时认领函数拿到的不是原字典，而是**转换后的模型实例**
  （`Model.__contains__`，`schematics/models.py:401`，按转换后字段名判断）；
  子模型重建时经 `_dict`（`schematics/compat.py:90`）只带转换后字段名。

## 实际观测（脚本 P0–P5）

- **P0 直接验证字典（对照）**：独立字段 `convert(dict)` 与 `validate(dict)` 都认领原始字典，
  得到 `SensorReading`；`SensorReading(r4, validate=True)` 直接报
  `{'unit': ["Value (celsius) must be one of ['C', 'F', 'K']."]}`。
- **P1 仅导入（validate=False）**：3 条记录只触发 2 次 `find_model` ——
  预构造的 `ManualReading` 实例走 `is_allowed_model` 捷径，**认领被跳过**；
  无来源标记的记录被后备类型 `LegacyReading` **默默接收**；此后两条记录的普通字段同为
  `[marker, occurred_at, unit]`，**仅凭普通字段已无法还原来源**（`to_primitive()`
  暂时还能借 `serialized_name` 还原 `sensor_tag`）。
- **P2 构造后再 validate**：验证阶段对每条记录**重复认领**，认领输入是转换后的实例；
  `SensorReading` 实例因 `marker` 命中 Manual 的兼容写法被**重新认领成
  `ManualReading`**（即入库日志中“实际已是 Sensor、随后按 Manual 校验”的来源）；
  翻转后连 `to_primitive()` 都导出 `manual_tag`，来源彻底不可恢复。
- **P2b 含坏单位**：坏单位作为字段错误沿 `readings -> 索引 3 -> unit` 传播，
  消息与 P0 直接验证完全一致；**验证失败后 `batch.readings` 抛
  `UndefinedValueError`**（partial_data 不含该字段），属不可稳定回报位置。
- **P3 构造时 validate=True**：`__init__` 执行两遍验证 —— 第一遍认领原始字典
  （R1 判为 Sensor），第二遍对实例重复认领（R1 仍被翻转成 Manual）；
  预构造实例同样被重复认领。**同一负载，P1 导入后是 Sensor，P2/P3 验证后是 Manual。**
- **P4 不稳定回报位置**：双标记记录使两个认领同时命中，`find_model` 抛裸
  `Exception("Got ambiguous input...")`，**绕过 `ListType` 按索引收集，整批崩溃**；
  被洗成 `LegacyReading` 的错误预构造实例（`strict=False` 丢弃 rogue 键）在朴素配置下
  导入走捷径、验证回落兜底，**静默通过**。
- **P5 判别契约**：混传负载 `[正常字典, 错误预构造实例, 未知来源字典, 坏单位字典,
  合法 legacy 字典]` 一次 `DataError` 收齐：索引 1、2 为
  `ConversionError("cannot determine reading source unambiguously")`，
  索引 3 为 `unit` 字段错误，索引 0、4 正常；R1 在导入+验证两阶段子类稳定不再翻转。

## 结论：哪处判别可信

- **可信**：对**原始字典**（或实例 `to_primitive()` 后的序列化形态）运行的认领逻辑，
  且认领恰好唯一命中、未知来源显式报错。即 P0/P5 展示的判别点。
- **不可信**：
  - 导入阶段对预构造实例的“通过”（认领被跳过，不代表已判别）；
  - 验证阶段库内自动重复认领的结果（输入已是转换后形态，来源键不可见）；
  - 无认领函数的后备类型（静默吞掉未知来源）；
  - 首次导入之后仅凭模型普通字段（`marker`）或实例类型做的任何来源推断。

## 给接入方的判别契约

1. **只在最原始的输入上判别**：实例一律先 `to_primitive()` 还原序列化键，
   再与原字典走同一套认领规则（契约实现见脚本 `contract_claim`）。
2. **恰好一个认领者**：零个或多个命中都抛 `ConversionError`（属 `BaseError`，
   能被 `ListType` 按索引收集），不要抛裸 `Exception`。
3. **后备类型必须显式命中**（如要求 `legacy_ref` 非空），禁止“无函数即默认”吞掉未知来源。
4. **预构造实例不视为已判别**：入库前以 `type(item) is contract_claim(None, item)` 复核；
   混传时统一走契约函数。
5. **导入完成即来源封存**：首次 `import_loop` 之后普通字段无法还原来源，
   需在边界处保留原字典或显式落一个 `source` 判别字段。

## 不能稳定回报的阶段与位置（实测）

- 导入阶段（`context.validate=False`）：预构造实例不触发任何认领日志。
- 验证阶段：认领输入是转换后实例，日志中的“认领输入”不再是原始数据。
- `find_model` 的歧义/无匹配：裸 `Exception`，不进 `DataError.errors`，无索引路径。
- 验证失败后：外层字段（如 `readings`）从模型上读取抛 `UndefinedValueError`。
- `Model.__init__(validate=True)`：静默执行两遍验证，第二遍的认领输入已是实例。

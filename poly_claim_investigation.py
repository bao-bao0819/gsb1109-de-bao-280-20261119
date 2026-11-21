# -*- coding: utf-8 -*-
"""
PolyModelType 认领（claim）行为排查 —— 固定版 Schematics 2.1.1（本工作区源码快照）。

复跑方式（不改库，库文件零改动；所有探针都是集成侧的子类/包装）：

    .venv/bin/python poly_claim_investigation.py

脚本末尾有一组自检断言，任何一条不符合观测即非零退出。

场景：采集批次把 Sensor / Manual 两类原始事件清洗成模型，落库前统一验证。
两条记录使用同一时间戳和单位值；原始字段名（sensor_tag / manual_tag）可区分来源，
但子模型转换后都保存为同一个字段名 marker。另有一个不声明认领函数的后备类型
LegacyReading，以及一条子类选对但单位非法的记录。
"""

import json
import sys

import schematics
from schematics.models import Model
from schematics.types import StringType
from schematics.types.compound import ListType, PolyModelType
from schematics.exceptions import (
    BaseError, CompoundError, ConversionError, DataError, FieldError, ValidationError,
)

# ---------------------------------------------------------------- 环境固定

EXPECTED_VERSION = "2.1.1"
assert schematics.__version__ == EXPECTED_VERSION, (
    "本排查固定在 schematics %s，当前为 %s" % (EXPECTED_VERSION, schematics.__version__))
assert schematics.__file__.startswith(
    __file__.rsplit("/", 1)[0]), "必须使用工作区内的 schematics 源码快照"

# ---------------------------------------------------------------- 观测日志

PHASE = {"name": "<init>"}
OBS = []          # 结构化观测：dict 列表
FINDINGS = []     # 断言结果


def log(kind, **kw):
    entry = {"phase": PHASE["name"], "kind": kind}
    entry.update(kw)
    OBS.append(entry)
    print("  [%s] %s: %s" % (entry["phase"], kind, json.dumps(kw, default=str, ensure_ascii=False)))


def snapshot(data):
    """认领函数实际拿到的输入长什么样。"""
    if isinstance(data, Model):
        return "<%s instance; visible fields=%s>" % (
            type(data).__name__, sorted(data.keys()))
    if isinstance(data, dict):
        return "dict keys=%s" % sorted(data)
    return repr(data)


def render_errors(exc):
    """把 DataError/FieldError 渲染成 {路径: [消息]} 的纯字典，便于跨阶段对照。"""
    if isinstance(exc, BaseError):
        return exc.to_primitive()
    return [repr(exc)]


def finding(name, ok, detail=""):
    FINDINGS.append((name, ok, detail))
    print("  [%s] %s %s" % (PHASE["name"], "PASS" if ok else "FAIL", name))
    if detail:
        print("       %s" % detail)


# ---------------------------------------------------------------- 模型定义

def traced_claim(owner, fn):
    """包装 _claim_polymorphic：记录每次实际认领输入与结果。"""
    @classmethod
    def wrapper(cls, data):
        result = fn(data)
        log("claim", owner=owner, input=snapshot(data), matched=result)
        return result
    return wrapper


class TracingPolyModelType(PolyModelType):
    """集成侧探针：记录 _convert 入口（含 context.validate）与 find_model 的取舍。"""

    def _convert(self, value, context):
        log("poly._convert", validate_phase=bool(context.validate), input=snapshot(value))
        return super(TracingPolyModelType, self)._convert(value, context)

    def find_model(self, data):
        log("find_model.enter", input=snapshot(data))
        try:
            chosen = super(TracingPolyModelType, self).find_model(data)
        except Exception as exc:
            log("find_model.raise", error="%s: %s" % (type(exc).__name__, exc))
            raise
        log("find_model.chosen", chosen=chosen.__name__)
        return chosen


TIMESTAMP = "2026-11-19T08:00:00Z"   # 两条记录共用同一时间戳
UNIT = "C"                          # 两条记录共用同一单位值


class SensorReading(Model):
    occurred_at = StringType(required=True)
    unit = StringType(required=True, choices=["C", "F", "K"])
    marker = StringType(serialized_name="sensor_tag")

    _claim_polymorphic = traced_claim("SensorReading", lambda data: "sensor_tag" in data)


class ManualReading(Model):
    occurred_at = StringType(required=True)
    unit = StringType(required=True, choices=["C", "F", "K"])
    marker = StringType(serialized_name="manual_tag")

    # 接入方常见写法：除原始键外，还“兼容”转换后的字段名 marker
    _claim_polymorphic = traced_claim(
        "ManualReading", lambda data: "manual_tag" in data or "marker" in data)


class LegacyReading(Model):
    """后备类型：不声明 _claim_polymorphic，无人认领时兜底。"""
    occurred_at = StringType(required=True)
    unit = StringType(required=True, choices=["C", "F", "K"])
    legacy_ref = StringType()


class Batch(Model):
    readings = ListType(TracingPolyModelType([SensorReading, ManualReading, LegacyReading]))


# ---------------------------------------------------------------- 记录

R1_SENSOR_DICT = {"sensor_tag": "dev-7", "occurred_at": TIMESTAMP, "unit": UNIT}
R2_MANUAL_DICT = {"manual_tag": "op-3", "occurred_at": TIMESTAMP, "unit": UNIT}
R3_UNKNOWN_DICT = {"occurred_at": TIMESTAMP, "unit": UNIT}            # 老设备，无来源标记
R4_BAD_UNIT_DICT = {"sensor_tag": "dev-9", "occurred_at": TIMESTAMP, "unit": "celsius"}

# R2 从已有可信模型导入（预构造实例），其余从字典开始
R2_TRUSTED_INSTANCE = ManualReading(dict(R2_MANUAL_DICT))


def readings_view(batch):
    out = []
    for item in (batch.readings or []):
        out.append({
            "class": type(item).__name__,
            "fields": sorted(item.keys()),
            "marker": getattr(item, "marker", None),
            "primitive": item.to_primitive(),
        })
    return out


# ================================================================ P0 直接验证字典（同类对照）

def p0_dict_controls():
    PHASE["name"] = "P0.dict-controls"
    print("\n== P0 直接验证字典（同类对照，不经过外层 ListType） ==")
    field = TracingPolyModelType([SensorReading, ManualReading, LegacyReading])

    PHASE["name"] = "P0.convert(dict)"
    m = field.convert(dict(R1_SENSOR_DICT))
    log("P0.convert.result", produced=type(m).__name__)

    PHASE["name"] = "P0.field.validate(dict)"
    m = field.validate(dict(R1_SENSOR_DICT))
    log("P0.field.validate.result", produced=type(m).__name__)

    PHASE["name"] = "P0.subclass.validate(dict)"
    control_msg = None
    try:
        SensorReading(dict(R4_BAD_UNIT_DICT), validate=True)
    except DataError as exc:
        control_msg = render_errors(exc)
        log("P0.bad-unit.direct", errors=control_msg)
    finding("P0 直接验证坏单位记录得到字段错误", control_msg is not None and "unit" in control_msg,
            detail=json.dumps(control_msg, ensure_ascii=False))
    return control_msg


# ================================================================ P1 仅导入（不认领预构造实例）

def p1_import_only():
    PHASE["name"] = "P1.import-only"
    print("\n== P1 外层构造 validate=False（仅导入） ==")
    before = len(OBS)
    batch = Batch({"readings": [dict(R1_SENSOR_DICT), R2_TRUSTED_INSTANCE, dict(R3_UNKNOWN_DICT)]})
    phase_obs = OBS[before:]
    claims = [e for e in phase_obs if e["kind"] == "claim"]
    find_models = [e for e in phase_obs if e["kind"] == "find_model.enter"]
    view = readings_view(batch)
    log("P1.readings", items=view)
    finding("P1 字典记录经认领得到 SensorReading", view[0]["class"] == "SensorReading")
    finding("P1 预构造实例原样通过（导入阶段跳过认领）",
            view[1]["class"] == "ManualReading" and len(find_models) == 2,
            detail="3 条记录仅触发 %d 次 find_model（预构造实例走 is_allowed_model 捷径）"
                   % len(find_models))
    finding("P1 无来源标记记录被后备类型默默接收", view[2]["class"] == "LegacyReading")
    finding("P1 之后普通字段只剩 marker，两种来源无法区分",
            view[0]["fields"] == view[1]["fields"] == ["marker", "occurred_at", "unit"],
            detail="to_primitive 暂时还能借 serialized_name 还原 sensor_tag：%s"
                   % sorted(view[0]["primitive"]))
    return batch


# ================================================================ P2 普通构造后再 validate（重复认领）

def p2_validate_after_import():
    PHASE["name"] = "P2.construct-then-validate"
    print("\n== P2 普通构造后再 validate（全部记录合法） ==")
    batch = Batch({"readings": [dict(R1_SENSOR_DICT), R2_TRUSTED_INSTANCE, dict(R3_UNKNOWN_DICT)]})
    log("P2.before-validate", items=readings_view(batch))
    batch.validate()
    view = readings_view(batch)
    log("P2.after-validate", items=view)
    finding("P2 验证阶段对每条记录重复认领（包括已是实例的记录）",
            any(e["kind"] == "claim" and "instance" in e["input"] for e in OBS
                if e["phase"] == "P2.construct-then-validate"))
    finding("P2 Sensor 实例被重新认领成 ManualReading（复现入库日志）",
            view[0]["class"] == "ManualReading",
            detail="认领输入已是转换后的模型实例：sensor_tag 不可见，marker 命中 Manual 的兼容写法")
    finding("P2 翻转后连 to_primitive 都导出错误来源键",
            "manual_tag" in view[0]["primitive"],
            detail="primitive=%s" % view[0]["primitive"])
    return batch


def p2b_validate_with_bad_unit():
    PHASE["name"] = "P2b.validate-bad-unit"
    print("\n== P2b 普通构造后再 validate（含坏单位记录） ==")
    batch = Batch({"readings": [
        dict(R1_SENSOR_DICT), R2_TRUSTED_INSTANCE, dict(R3_UNKNOWN_DICT), dict(R4_BAD_UNIT_DICT)]})
    log("P2b.after-import", items=readings_view(batch))
    errors = None
    try:
        batch.validate()
    except DataError as exc:
        errors = render_errors(exc)
        log("P2b.DataError", errors=errors)
    finding("P2b 坏单位作为字段错误沿 readings -> 索引 -> unit 传播",
            errors is not None and "readings" in errors and 3 in errors["readings"]
            and "unit" in errors["readings"][3],
            detail=json.dumps(errors, ensure_ascii=False, default=str))
    try:
        leftover = batch.readings
        state = "readable: %r" % (leftover,)
    except Exception as exc:
        state = "unreadable: %s: %s" % (type(exc).__name__, exc)
    log("P2b.after-failed-validate.readings", state=state)
    finding("P2b 验证失败后 readings 字段状态不可稳定回报", True, detail=state)
    return errors


# ================================================================ P3 构造时 validate=True

def p3_validate_at_construct():
    PHASE["name"] = "P3.construct-validate-True"
    print("\n== P3 外层构造时 validate=True ==")
    before = len(OBS)
    ok_batch = Batch({"readings": [
        dict(R1_SENSOR_DICT), R2_TRUSTED_INSTANCE, dict(R3_UNKNOWN_DICT)]}, validate=True)
    phase_obs = OBS[before:]
    view = readings_view(ok_batch)
    log("P3.ok-payload.readings", items=view)
    dict_claims = [e for e in phase_obs
                   if e["kind"] == "find_model.enter" and e["input"].startswith("dict")]
    inst_claims = [e for e in phase_obs
                   if e["kind"] == "find_model.enter" and "instance" in e["input"]]
    finding("P3 第一遍验证的认领发生在原始字典上（R1 被判为 SensorReading）",
            any("sensor_tag" in e["input"] for e in dict_claims))
    finding("P3 __init__(validate=True) 实际执行两遍验证，第二遍对实例重复认领",
            len(dict_claims) >= 2 and len(inst_claims) >= 3,
            detail="dict 认领 %d 次、实例认领 %d 次；第二遍把 R1 翻转成 %s"
                   % (len(dict_claims), len(inst_claims), view[0]["class"]))
    finding("P3 预构造实例在构造即验证时同样被重复认领（捷径只在纯导入阶段生效）",
            len(inst_claims) >= 3)

    errors = None
    try:
        Batch({"readings": [
            dict(R1_SENSOR_DICT), R2_TRUSTED_INSTANCE, dict(R3_UNKNOWN_DICT),
            dict(R4_BAD_UNIT_DICT)]}, validate=True)
    except DataError as exc:
        errors = render_errors(exc)
        log("P3.bad-payload.DataError", errors=errors)
    finding("P3 坏单位沿相同路径 readings -> 3 -> unit 传播",
            errors is not None and 3 in errors.get("readings", {}))
    return errors


# ================================================================ P4 不稳定回报位置探针

def p4_unstable_reporting():
    PHASE["name"] = "P4.unstable-reporting"
    print("\n== P4 不稳定回报位置 ==")
    escaped = None
    try:
        Batch({"readings": [
            {"sensor_tag": "dev-1", "manual_tag": "op-1",
             "occurred_at": TIMESTAMP, "unit": UNIT}]}, validate=True)
    except DataError as exc:
        escaped = ("DataError", render_errors(exc))
    except Exception as exc:
        escaped = (type(exc).__name__, str(exc))
    log("P4.ambiguous-input", escaped=escaped)
    finding("P4 认领歧义抛出的是裸 Exception，绕过 ListType 按索引收集",
            escaped is not None and escaped[0] == "Exception",
            detail="find_model 的歧义/无匹配异常不是 BaseError，整批导入直接崩溃")

    # 错误预构造：sensor 数据被洗成 LegacyReading 实例（strict=False 丢弃 rogue 键）
    laundered = LegacyReading(
        {"sensor_tag": "dev-8", "occurred_at": TIMESTAMP, "unit": UNIT}, strict=False)
    naive_ok = False
    try:
        b = Batch({"readings": [laundered]})
        b.validate()
        naive_ok = type(b.readings[0]).__name__ == "LegacyReading"
    except Exception as exc:
        log("P4.laundered.rejected", error="%s: %s" % (type(exc).__name__, exc))
    finding("P4 朴素配置下错误预构造实例靠后备类型通过（导入走捷径、验证回落兜底）",
            naive_ok)
    return laundered


# ================================================================ P5 判别契约演示

CONTRACT_RULES = (
    (SensorReading, lambda d: bool(d.get("sensor_tag"))),
    (ManualReading, lambda d: bool(d.get("manual_tag"))),
    (LegacyReading, lambda d: bool(d.get("legacy_ref"))),   # 后备类型也必须显式命中
)


def contract_claim(field, data):
    """接入方判别契约的可执行形态：

    1. 只在最原始的输入上判别：模型实例先 to_primitive() 还原序列化键；
    2. 恰好一个认领者，零个/多个都抛 ConversionError（BaseError，
       能被 ListType 按索引收集，而不是裸 Exception 炸掉整批）；
    3. 后备类型不再“无函数即默认”，未知来源显式拒绝。
    """
    if isinstance(data, Model):
        data = data.to_primitive()
    if not isinstance(data, dict):
        raise ConversionError("reading must be a mapping or a model instance")
    matches = [cls for cls, rule in CONTRACT_RULES if rule(data)]
    if len(matches) != 1:
        raise ConversionError("cannot determine reading source unambiguously")
    chosen = matches[0]
    log("contract.claim", input=snapshot(data), chosen=chosen.__name__)
    return chosen


class ContractBatch(Model):
    readings = ListType(TracingPolyModelType(
        [SensorReading, ManualReading, LegacyReading], claim_function=contract_claim))


def p5_contract(laundered):
    PHASE["name"] = "P5.contract"
    print("\n== P5 判别契约：预构造实例与原字典混传 ==")
    genuine_legacy = {"legacy_ref": "old-1", "occurred_at": TIMESTAMP, "unit": UNIT}
    payload = [
        dict(R1_SENSOR_DICT),     # 0 正常字典
        laundered,                # 1 错误预构造实例（sensor 数据洗成 Legacy）
        dict(R3_UNKNOWN_DICT),    # 2 未知来源
        dict(R4_BAD_UNIT_DICT),   # 3 类型选对但单位非法
        genuine_legacy,           # 4 显式合法后备
    ]
    errors, view = None, None
    try:
        ContractBatch({"readings": payload}, validate=True)
    except DataError as exc:
        errors = render_errors(exc)
        log("P5.DataError", errors=errors)
    finding("P5 错误预构造实例不再靠后备类型通过", errors is not None and 1 in errors.get("readings", {}))
    finding("P5 未知来源不再靠后备类型通过", errors is not None and 2 in errors.get("readings", {}))
    finding("P5 选对类型的坏单位仍作为 unit 字段错误",
            errors is not None and "unit" in errors.get("readings", {}).get(3, {}),
            detail=json.dumps(errors.get("readings", {}).get(3), ensure_ascii=False, default=str))

    stable = ContractBatch({"readings": [dict(R1_SENSOR_DICT), genuine_legacy]})
    stable.validate()
    view = readings_view(stable)
    log("P5.stable.readings", items=view)
    finding("P5 契约下导入+验证两阶段子类稳定（R1 不再翻转）",
            view[0]["class"] == "SensorReading" and view[1]["class"] == "LegacyReading")
    return errors


# ================================================================ main

def main():
    print("schematics=%s (%s)" % (schematics.__version__, schematics.__file__))
    print("python=%s" % sys.version.split()[0])

    control_msg = p0_dict_controls()
    p1_import_only()
    p2_validate_after_import()
    p2b_errors = p2b_validate_with_bad_unit()
    p3_errors = p3_validate_at_construct()
    laundered = p4_unstable_reporting()
    p5_contract(laundered)

    PHASE["name"] = "SUMMARY"
    print("\n== 跨阶段对照：同字段名、同错误消息、不同阶段 ==")
    nested = (p3_errors or {}).get("readings", {}).get(3, {})
    finding("坏单位错误消息在直接验证与外层传播中一致",
            control_msg is not None and nested.get("unit") == control_msg.get("unit"),
            detail="direct=%s nested=%s" % (control_msg, nested))

    failed = [f for f in FINDINGS if not f[1]]
    print("\n== 自检：%d 项观测，%d 项不符合 ==" % (len(FINDINGS), len(failed)))
    for name, ok, detail in FINDINGS:
        print("  %s %s" % ("PASS" if ok else "FAIL", name))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

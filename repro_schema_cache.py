"""最小复现：两个接口共用 ProductSchema，定位"完整子对象泄漏"是声明问题还是缓存问题。

运行：.venv/bin/python repro_schema_cache.py
环境：apispec 6.10.1 + marshmallow 4.4.0 + OpenAPI 3.0.2

背景知识（源码依据）：
  - 缓存键 make_schema_key = (类, only, exclude, load_only, dump_only, partial)
    见 src/apispec/ext/marshmallow/common.py:111
  - converter.refs 按该键缓存"键 -> 组件名"，键相同才复用同一组件
    见 src/apispec/ext/marshmallow/openapi.py:117
  - resolve_schema_instance 遇到"类"会 cls()、遇到字符串走 registry get_class()()，
    两种实例化都不带 only —— only 只能靠传 Schema 实例存活
    见 src/apispec/ext/marshmallow/common.py:17
  - 默认命名器只看类名；不同键同名时生成 Product1 并发 UserWarning
    见 src/apispec/ext/marshmallow/common.py:127

判定口径（两个独立问题，别混在一起）：
  A. 部分接口的 $ref 是否指向了完整接口注册的组件？  -> 缓存复用问题
  B. 部分接口自己的组件属性有没有被收窄？            -> 声明是否生效
"""

from __future__ import annotations

import warnings

from apispec import APISpec
from apispec.ext.marshmallow import MarshmallowPlugin
from apispec.ext.marshmallow.common import (
    make_schema_key,
    resolve_schema_instance,
)
from marshmallow import Schema, fields

PARTIAL_FIELDS = ("id", "name")


class ProductSchema(Schema):
    id = fields.Int()
    name = fields.Str()
    price = fields.Decimal()
    stock = fields.Int()


class ProductSummarySchema(ProductSchema):
    # 只覆盖字段元数据，不删字段：marshmallow 继承语义下四个字段都还在
    name = fields.Str(metadata={"description": "short display name"})


class ProductNarrowSchema(ProductSchema):
    # 真正收窄字段的子类：Meta.exclude 会并入实例 .exclude，进入缓存键
    class Meta:
        exclude = ("price", "stock")


def new_spec() -> tuple[APISpec, MarshmallowPlugin]:
    plugin = MarshmallowPlugin()
    spec = APISpec(
        title="demo",
        version="1",
        openapi_version="3.0.2",
        plugins=[plugin],
    )
    return spec, plugin


def add_interface(spec: APISpec, path: str, schema) -> list[warnings.WarningMessage]:
    """模拟一个 GET 接口，响应体引用商品 schema；返回注册期间的告警。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        spec.path(
            path=path,
            operations={
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"schema": schema},
                            },
                        },
                    },
                },
            },
        )
    return caught


def ref_name(spec: APISpec, path: str) -> str:
    media = spec._paths[path]["get"]["responses"]["200"]["content"][
        "application/json"
    ]
    return media["schema"]["$ref"].rsplit("/", 1)[-1]


def props(spec: APISpec, component_name: str) -> tuple[str, ...]:
    return tuple(sorted(spec.components.schemas[component_name]["properties"]))


def key_of(schema) -> tuple:
    return make_schema_key(resolve_schema_instance(schema))


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def dump_state(spec: APISpec, plugin: MarshmallowPlugin) -> None:
    print("converter.refs（缓存键 -> 组件名）:")
    for key, name in plugin.converter.refs.items():
        cls, only, exclude, load_only, dump_only, partial = key
        print(
            f"  {name:16s} <- {cls.__name__:22s} only={set(only) if only else None}"
            f" exclude={set(exclude) if exclude else set()}"
        )
    print("components.schemas（实际生成的组件）:")
    for name in spec.components.schemas:
        print(f"  {name:16s} properties={props(spec, name)}")


verdict: dict[str, str] = {}

# ---------------------------------------------------------------------------
# 情况 1：only —— 必须传实例。键不同 -> 两个独立组件，与注册顺序无关
# ---------------------------------------------------------------------------
section("情况 1：only（部分接口传 ProductSchema(only=...) 实例）")
partial_instance = ProductSchema(only=PARTIAL_FIELDS)
print(f"两接口缓存键相同？ {key_of(partial_instance) == key_of(ProductSchema)}  "
      f"（False 才会生成独立组件）")

spec, plugin = new_spec()
caught = add_interface(spec, "/products/summary", partial_instance)
caught += add_interface(spec, "/products", ProductSchema)
dump_state(spec, plugin)
summary_ref, full_ref = ref_name(spec, "/products/summary"), ref_name(spec, "/products")
print(f"\n部分接口 -> {summary_ref}{props(spec, summary_ref)}")
print(f"完整接口 -> {full_ref}{props(spec, full_ref)}")
print(f"同名冲突告警（不同键抢 Product 名字）: {len(caught)} 条")
print("注意：部分接口先注册时它占用了 Product 这个名字，完整组件反而叫 Product1。")
verdict["1 only 实例"] = (
    "隔离正确，无缓存复用" if props(spec, summary_ref) == PARTIAL_FIELDS else "异常"
)

# ---------------------------------------------------------------------------
# 情况 2：子类覆盖字段 —— 类不同 -> 键不同 -> 独立组件；但覆盖元数据不收窄字段
# ---------------------------------------------------------------------------
section("情况 2：子类覆盖字段")
print(f"子类与父类缓存键相同？ {key_of(ProductSummarySchema) == key_of(ProductSchema)}")
spec, plugin = new_spec()
add_interface(spec, "/products/summary", ProductSummarySchema)
add_interface(spec, "/products", ProductSchema)
dump_state(spec, plugin)
summary_ref = ref_name(spec, "/products/summary")
print(f"\n部分接口 -> {summary_ref}{props(spec, summary_ref)}")
print("组件是独立的（没复用 Product），但四个字段都在：覆盖字段元数据不等于裁剪字段。")

spec2, plugin2 = new_spec()
add_interface(spec2, "/products/summary", ProductNarrowSchema)
narrow_ref = ref_name(spec2, "/products/summary")
print(f"改用 Meta.exclude 收窄的子类 -> {narrow_ref}{props(spec2, narrow_ref)}")
verdict["2 子类覆盖字段"] = "独立组件，字段未收窄（继承语义，非缓存问题）"

# ---------------------------------------------------------------------------
# 情况 3a：先后注册 —— 声明都正确时，调换顺序只改变命名，不改变组件内容
# ---------------------------------------------------------------------------
section("情况 3a：先后注册（正确声明，两种顺序对照）")
for label, first_path, first, second_path, second in (
    ("部分先注册", "/s", partial_instance, "/f", ProductSchema),
    ("完整先注册", "/f", ProductSchema, "/s", partial_instance),
):
    spec, plugin = new_spec()
    add_interface(spec, first_path, first)
    add_interface(spec, second_path, second)
    r1, r2 = ref_name(spec, first_path), ref_name(spec, second_path)
    print(f"[{label}] {first_path} -> {r1}{props(spec, r1)} | "
          f"{second_path} -> {r2}{props(spec, r2)}")
verdict["3a 顺序（正确声明）"] = "两种顺序组件内容一致，仅名字分配不同"

# ---------------------------------------------------------------------------
# 情况 3b：声明错误 —— 部分接口传"类"，cls() 丢掉 only，两键完全相同
# ---------------------------------------------------------------------------
section("情况 3b：声明错误（部分接口传类而非 only 实例）—— 这条路径复用完整组件")
spec, plugin = new_spec()
print(f"两接口缓存键相同？ {key_of(ProductSchema) == key_of(ProductSchema)}")
add_interface(spec, "/products/full", ProductSchema)          # 完整接口先注册
caught = add_interface(spec, "/products/summary", ProductSchema)  # 本意只要部分字段
dump_state(spec, plugin)
summary_ref = ref_name(spec, "/products/summary")
print(f"\n部分接口 -> {summary_ref}{props(spec, summary_ref)}")
print(f"告警数: {len(caught)}（键相同是静默命中缓存，连告警都没有）")
verdict["3b 传类丢 only"] = (
    "❌ 复用完整组件，根因是声明：only 在进入生成器前已丢失"
    if set(props(spec, summary_ref)) > set(PARTIAL_FIELDS) else "异常"
)

# ---------------------------------------------------------------------------
# 情况 3c：声明错误 —— YAML 里只能写字符串类名，registry 实例化同样丢 only
# ---------------------------------------------------------------------------
section("情况 3c：声明错误（YAML 风格字符串引用 'ProductSchema'）")
spec, plugin = new_spec()
add_interface(spec, "/products/full", ProductSchema)
add_interface(spec, "/products/summary", "ProductSchema")
dump_state(spec, plugin)
summary_ref = ref_name(spec, "/products/summary")
print(f"\n部分接口 -> {summary_ref}{props(spec, summary_ref)}")
verdict["3c 字符串引用"] = (
    "❌ 同样复用完整组件；YAML 字符串无法携带 only，需改用具名子类或实例注册"
    if set(props(spec, summary_ref)) > set(PARTIAL_FIELDS) else "异常"
)

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
section("汇总：怎么用这份输出区分声明问题 vs 生成器缓存问题")
for name, result in verdict.items():
    print(f"  {name:22s}: {result}")

print("""
排查步骤：
  1. 打印 make_schema_key(resolve_schema_instance(你的入参))，与完整接口的键对比。
  2. 键不同却拿到同一组件内容 -> 才是生成器缓存 bug（本版本复现不出，且会伴随
     'Multiple schemas resolved to the name ...' 告警，组件名为 Product/Product1）。
  3. 键相同 -> 声明问题：检查你传的是类/字符串（only 被 cls() 丢弃），还是
     Schema(only=...) 实例。YAML 字符串路径天然无法表达 only，应改为预注册具名
     组件（如 ProductNarrowSchema）后在 YAML 里 $ref 该组件名。
""")

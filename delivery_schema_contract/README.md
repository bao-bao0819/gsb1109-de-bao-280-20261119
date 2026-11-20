# Delivery-note schema contract

This directory reproduces why a published model schema rejects the JSON a
warehouse consumer actually receives.  It does not modify mashumaro, the
schema builder, or the model's serialization hooks.

## Contents

| Path | Role |
|---|---|
| `models.py` | Declared model, aliases, annotated `Decimal` policy, caller dialect, envelope hook |
| `contract.py` | Builds the published schema, runs four configurations, validates the decoded JSON, and prints the report |
| `test_contract.py` | Executable checks, including the two independent-fix cases |
| `expected/schema_delivery_note.json` | Published schema as built from the model |
| `expected/schema_wrapped_delivery_note.json` | Schema built from the envelope subclass |
| `expected/report.json` | Full deterministic report for all four configurations |

Run from the repository root:

```bash
PYTHONPATH=. python3 -m delivery_schema_contract.contract
PYTHONPATH=. python3 -m delivery_schema_contract.contract --check
PYTHONPATH=. python3 -m pytest -p no:rerunfailures \
    delivery_schema_contract/test_contract.py -q
```

`jsonschema>=4` is needed for validation.  The report itself only uses
`build_json_schema`, `to_dict`, `to_json`, and `json.loads`.

## The three layers

Keep these separate when comparing artifacts.

**Declared model.** `DeliveryNote` declares
`amount_groups: Annotated[list[list[Decimal]], Alias("amountGroups")]` and
`invoice_id: Annotated[str, Alias("invoiceId")]`.  Both aliases are primary
and are what the schema publishes.  The model's `Config` registers
`DecimalAsText` for `Decimal`; its `serialize` return annotation is `str`.
`CentsDialect` is declared next to it but is intentionally *not* attached to
any `Config`.

**Runtime transformation.** `to_dict`/`to_json` apply the selected policy to
each amount, then `__post_serialize__` (when the subclass defines it) wraps
the finished dictionary.  The caller dialect is selected per call and, when
it matches `Decimal`, it wins over the model strategy.  The envelope hook is
not a field type, so it is invisible to the model's field walk.

**Final data.** The consumer parses `json_text` with `json.loads`.  This is
the value passed to `Draft202012Validator`.  `to_dict` and the decoded JSON
are asserted equal for every configuration, so the envelope and numbers are
not introduced by the JSON string layer.

## Published schema

`build_json_schema(DeliveryNote)` produces:

```json
{
  "type": "object",
  "title": "DeliveryNote",
  "properties": {
    "invoiceId": {"type": "string"},
    "amountGroups": {
      "type": "array",
      "items": {"type": "array", "items": {"type": "string"}}
    }
  },
  "additionalProperties": false,
  "required": ["invoiceId", "amountGroups"]
}
```

The amount leaf is `{"type": "string"}` with no `format`.  It is inferred from
the **return annotation** of the winning model strategy
(`DecimalAsText.serialize -> str`), not from the bare `Decimal` schema
(`{"type": "string", "format": "decimal"}`).  The alias property names come
from the first `Alias(...)` annotation; `Instance.alias` implements that
precedence in `mashumaro/jsonschema/schema.py:154`.

## Four configurations

Input business value for every row:

```python
DeliveryNote(
    invoice_id="INV-2026-1119",
    amount_groups=[[Decimal("12.34"), Decimal("0.50")], [Decimal("7.00")]],
)
```

| Case | Caller dialect | Envelope | First amount in decoded JSON | First schema error |
|---|---|---|---|---|
| `default` | no | no | `$.amountGroups.0.0` = `"12.34"` | none |
| `dialect` | `CentsDialect` | no | `$.amountGroups.0.0` = `1234` | `type` at `$.amountGroups.0.0`, schema `/properties/amountGroups/items/items/type` |
| `envelope` | no | `WrappedDeliveryNote` | `$.payload.amountGroups.0.0` = `"12.34"` | `additionalProperties` at `$`, schema `/additionalProperties` |
| `both` | `CentsDialect` | `WrappedDeliveryNote` | `$.payload.amountGroups.0.0` = `1234` | `additionalProperties` at `$`, schema `/additionalProperties` |

Decoded payloads (identical to the corresponding `to_dict` results):

`default`:

```json
{"invoiceId": "INV-2026-1119", "amountGroups": [["12.34", "0.50"], ["7.00"]]}
```

`dialect`:

```json
{"invoiceId": "INV-2026-1119", "amountGroups": [[1234, 50], [700]]}
```

`envelope`:

```json
{"payload": {"invoiceId": "INV-2026-1119", "amountGroups": [["12.34", "0.50"], ["7.00"]]}}
```

`both`:

```json
{"payload": {"invoiceId": "INV-2026-1119", "amountGroups": [[1234, 50], [700]]}}
```

## Why the same schema can report different first errors

The schema is a single root object with `additionalProperties: false`.  The
two failures enter at different points of the same declared tree:

- **Envelope:** the root receives `{"payload": ...}` instead of
  `invoiceId`/`amountGroups`.  Validation fails before any nested amount is
  visited: `payload` is an additional property and both required keys are
  missing.  Every reported error has instance path `$`.
- **Numeric representation:** the root shape is correct.  Validation descends
  `amountGroups -> items (array of runs) -> items (array of amounts) -> type`
  and rejects `1234`.  The instance path is `$.amountGroups.0.0` and the
  schema path is `/properties/amountGroups/items/items/type`.

When both are enabled, the envelope error is emitted first.  Per the report,
all errors in `both` are at `$`; the numeric error is not shown until the
payload is manually unwrapped from `payload`, after which the first error is
`type` at `$.amountGroups.0.0`.  This is error ordering, not a different root
cause.

## Actual policy winner

Runtime selection is first match in this order
(`mashumaro/core/meta/code/builder.py:1262`):

1. caller dialect passed to `to_dict(..., dialect=...)`;
2. `Config.dialect`;
3. `Config.serialization_strategy`;
4. builder default dialect.

`CentsDialect` occupies level 1 and matches `Decimal`, so it wins over
`DecimalAsText` at level 3.  With no caller dialect, the model strategy wins
and produces strings.

## Where the schema infers the amount type

`build_json_schema(DeliveryNote)` has no runtime dialect argument.  Its
`dialect` parameter is a `JSONSchemaDialect`, not a mashumaro `Dialect`;
passing `CentsDialect` there fails with `AttributeError: ... has no attribute
'all_refs'`.

For the field, `mashumaro/jsonschema/schema.py:382` asks the model for an
overridden serialization method.  `Instance.get_overridden_serialization_method`
walks the same strategy chain without a caller dialect, so it finds
`DecimalAsText.serialize`.  Its return annotation `str` is substituted at
`mashumaro/jsonschema/schema.py:405`, and the leaf is rendered as a string.
The nested list shape comes from `list[list[Decimal]]`; the aliases come from
the `Annotated` metadata.

## Does the final envelope fall within schema coverage?

No.  `WrappedDeliveryNote` adds only `__post_serialize__`, which runs after
all fields are packed (`mashumaro/core/meta/code/builder.py:1002`).  The
model walk used by schema generation sees dataclass fields, not hooks, so
`build_json_schema(WrappedDeliveryNote)` publishes exactly the same object
shape as `DeliveryNote`:

- same `properties`;
- same `required`;
- same `additionalProperties`;
- no `payload` string anywhere in the schema.

The schema therefore covers the pre-hook object, not the final envelope.

## Independent fixes

`contract.py::independent_checks` pins the four cross-checks:

| Change | Result |
|---|---|
| Use the model policy, still wrap | amount is `"12.34"`, but first error remains `additionalProperties` at `$` |
| Use caller dialect, no wrap | root keys are correct, but first error remains `type` at `$.amountGroups.0.0` |
| Both enabled | root error hides the numeric error; after manual unwrap the `type` error appears |
| Default (no dialect, no wrap) | no errors |

This demonstrates that correcting the numeric representation does not fix
the envelope, and correcting the envelope does not fix the numeric
representation.  No test asserts a change to the schema generator or treats
the hook as a model constraint.

## Two related boundaries

- `DeliveryNote.from_dict` is not a schema check.  Feeding the `CentsDialect`
  payload to the default deserializer succeeds but yields
  `Decimal("1234")`, not `Decimal("12.34")`.  Only `from_dict(...,
  dialect=CentsDialect)` reverses the cents policy; validity comes from the
  JSON Schema validator, not from the model loader.
- Passing `CentsDialect` as `build_json_schema(..., dialect=...)` does not
  apply the number policy.  That argument expects a `JSONSchemaDialect` and
  raises `AttributeError: ... has no attribute 'all_refs'`; the published
  schema is always the model-declared shape.

"""Declared models for the delivery-note schema contract.

This module contains only what the producer declares:

* the dataclass field types, including the nested amount list
  ``list[list[Decimal]]``;
* the external aliases, declared with ``Annotated[..., Alias(...)]``;
* the annotated default number policy (``DecimalAsText``), registered on the
  model;
* the caller dialect (``CentsDialect``), which is never registered on the
  model and only reaches ``to_dict``/``to_json`` as a keyword argument;
* the fixed-envelope serialization hook (``WrappedDeliveryNote``).

No schema is generated here.  See ``contract.py`` for the runtime
transformations and the final validation.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

from mashumaro.config import ADD_DIALECT_SUPPORT, BaseConfig
from mashumaro.dialect import Dialect
from mashumaro.mixins.json import DataClassJSONMixin
from mashumaro.types import Alias, SerializationStrategy


class DecimalAsText(SerializationStrategy, use_annotations=True):
    """Declared default number policy: ``Decimal`` travels as text.

    The annotations are the contract the JSON Schema builder follows:
    ``serialize`` returns ``str``, so the schema for an amount is a string.
    ``use_annotations=True`` also makes the return type drive the generated
    packer recursively.
    """

    def serialize(self, value: Decimal) -> str:
        return format(value, "f")

    def deserialize(self, value: str) -> Decimal:
        return Decimal(value)


class CentsDialect(Dialect):
    """Caller dialect: replace the model policy for ``Decimal`` with cents.

    This is deliberately a *caller* override.  It is not referenced by any
    model ``Config``; it is supplied per call as ``to_dict(dialect=...)`` and
    ``to_json(dialect=...)``.
    """

    serialization_strategy = {
        Decimal: {
            "serialize": lambda value: int(value * 100),
            "deserialize": lambda value: Decimal(value) / 100,
        }
    }


@dataclass
class DeliveryNote(DataClassJSONMixin):
    """The delivered document; both fields use external aliases.

    ``amount_groups`` is the nested amount list: a list of amount runs, each
    run being a list of ``Decimal`` values.
    """

    invoice_id: Annotated[str, Alias("invoiceId")]
    amount_groups: Annotated[list[list[Decimal]], Alias("amountGroups")]

    class Config(BaseConfig):
        # Required so the caller dialect is accepted by to_dict/to_json.
        code_generation_options = [ADD_DIALECT_SUPPORT]
        # Aliases are the external names for both consumers.
        serialize_by_alias = True
        # Declared model policy.  It is only consulted when the caller does
        # not supply a dialect that already matches ``Decimal``.
        serialization_strategy = {Decimal: DecimalAsText()}


@dataclass
class WrappedDeliveryNote(DeliveryNote):
    """Same declared fields plus a fixed envelope applied after packing."""

    def __post_serialize__(self, d: dict[Any, Any]) -> dict[Any, Any]:
        return {"payload": d}

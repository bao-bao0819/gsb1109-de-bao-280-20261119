from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from mashumaro import field_options
from mashumaro.types import SerializationStrategy

__all__ = ["Bill", "ContractRateStrategy", "Fee"]


class ContractRateStrategy(SerializationStrategy):
    """Pre-existing strategy for the contract rate field only.

    Rates travel as percent strings with exactly four fractional
    digits, e.g. Decimal("0.125") <-> "12.5000%". All arithmetic runs
    inside a local context so the caller's decimal context (precision,
    rounding, flags) is left untouched.
    """

    def serialize(self, value: Decimal) -> str:
        with localcontext() as ctx:
            ctx.prec = max(len(value.as_tuple().digits) + 6, 2)
            percent = (value * 100).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_EVEN
            )
        return f"{percent}%"

    def deserialize(self, value: Any) -> Decimal:
        text = str(value)
        if not text.endswith("%"):
            raise ValueError(f"contract rate must end with '%': {text!r}")
        with localcontext() as ctx:
            ctx.prec = max(len(text) + 2, 2)
            return Decimal(text[:-1]) / 100


@dataclass
class Fee:
    label: str
    amount: Decimal


@dataclass
class Bill:
    bill_no: str
    total: Decimal
    fees: list[Fee]
    rate: Decimal = field(
        metadata=field_options(serialization_strategy=ContractRateStrategy())
    )

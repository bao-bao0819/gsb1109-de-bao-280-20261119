from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from mashumaro.dialect import Dialect
from mashumaro.types import SerializationStrategy

__all__ = [
    "ArchiveDecimalStrategy",
    "ArchiveDialect",
    "SummaryDecimalStrategy",
    "SummaryDialect",
]

TWO_PLACES = Decimal("0.01")


def _parse_decimal(value: Any) -> Decimal:
    # The local context keeps InvalidOperation flags (raised for
    # malformed input) from leaking into the caller's context while
    # still honoring its traps, so invalid values keep raising.
    with localcontext():
        return Decimal(str(value))


class ArchiveDecimalStrategy(SerializationStrategy):
    """Lossless Decimal <-> string for the archive interface.

    Significant digits, trailing zeros and the sign of zero are kept
    verbatim; no arithmetic is ever applied to the value. Negative
    exponents use fixed-point notation so scientific notation never
    drops coefficient digits (str(Decimal("-0.0000000001")) would
    read back as "-1E-10").
    """

    def serialize(self, value: Decimal) -> str:
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < 0:
            return format(value, "f")
        return str(value)

    def deserialize(self, value: Any) -> Decimal:
        return _parse_decimal(value)


class SummaryDecimalStrategy(SerializationStrategy):
    """Fixed two-place, half-even Decimal <-> string for summaries.

    Quantization is decided from the source Decimal itself (never via
    float) and runs in a local context whose precision is derived from
    the value's magnitude, so numbers with more digits than the
    caller's context precision still quantize correctly and the
    caller's context stays byte-identical, success or failure.
    """

    def serialize(self, value: Decimal) -> str:
        if not isinstance(value, Decimal):
            raise TypeError(
                f"summary amounts must be Decimal, got {type(value).__name__}"
            )
        with localcontext() as ctx:
            # integer digits of the value, plus room for a carry into a
            # new most-significant digit and the two fractional digits
            ctx.prec = max(value.adjusted() + 1, 0) + 3
            quantized = value.quantize(TWO_PLACES, rounding=ROUND_HALF_EVEN)
        return str(quantized)

    def deserialize(self, value: Any) -> Decimal:
        return _parse_decimal(value)


class ArchiveDialect(Dialect):
    serialization_strategy = {Decimal: ArchiveDecimalStrategy()}


class SummaryDialect(Dialect):
    serialization_strategy = {Decimal: SummaryDecimalStrategy()}

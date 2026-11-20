import json
from contextlib import contextmanager
from decimal import (
    ROUND_DOWN,
    Decimal,
    InvalidOperation,
    getcontext,
    localcontext,
)

import pytest

from finance import api
from finance.models import Bill, Fee
from mashumaro.codecs.basic import BasicDecoder, BasicEncoder
from mashumaro.exceptions import InvalidFieldValue

BIG_TOTAL = Decimal("123456789012345678901234567890.123456789")
TRAILING_ZEROS = Decimal("1.5000")
NEGATIVE_ZERO = Decimal("-0.000")
TINY_NEGATIVE = Decimal("-0.0000000001")
RATE = Decimal("0.125")


def make_bill(**overrides) -> Bill:
    kwargs = {
        "bill_no": "BAO-DE-126",
        "total": BIG_TOTAL,
        "fees": [
            Fee("service", TRAILING_ZEROS),
            Fee("adjustment", NEGATIVE_ZERO),
            Fee("rounding", TINY_NEGATIVE),
        ],
        "rate": RATE,
    }
    kwargs.update(overrides)
    return Bill(**kwargs)


@contextmanager
def constrained_decimal_context():
    """A caller context with low precision and non-default rounding."""
    with localcontext() as ctx:
        ctx.prec = 10
        ctx.rounding = ROUND_DOWN
        yield


def context_snapshot():
    ctx = getcontext()
    return (
        ctx.prec,
        ctx.rounding,
        ctx.Emin,
        ctx.Emax,
        ctx.capitals,
        ctx.clamp,
        dict(ctx.traps),
        dict(ctx.flags),
    )


def test_archive_dict_roundtrip_preserves_decimal_text():
    with constrained_decimal_context():
        before = context_snapshot()
        bill = make_bill()

        data = api.to_archive_dict(bill)
        assert data["total"] == "123456789012345678901234567890.123456789"
        amounts = [fee["amount"] for fee in data["fees"]]
        assert amounts == ["1.5000", "-0.000", "-0.0000000001"]

        restored = api.bill_from_archive_dict(data)
        assert restored.total.as_tuple() == BIG_TOTAL.as_tuple()
        for fee, original in zip(restored.fees, bill.fees):
            assert fee.amount.as_tuple() == original.amount.as_tuple()
        assert context_snapshot() == before


def test_archive_json_roundtrip_uses_strings_and_preserves_decimal_text():
    with constrained_decimal_context():
        before = context_snapshot()
        bill = make_bill()

        payload = api.to_archive_json(bill)
        decoded = json.loads(payload)
        assert isinstance(decoded["total"], str)
        assert decoded["total"] == str(BIG_TOTAL)
        assert [f["amount"] for f in decoded["fees"]] == [
            "1.5000",
            "-0.000",
            "-0.0000000001",
        ]

        restored = api.bill_from_archive_json(payload)
        assert restored.total.as_tuple() == BIG_TOTAL.as_tuple()
        assert restored.fees[1].amount.as_tuple() == NEGATIVE_ZERO.as_tuple()
        assert restored.fees[1].amount.is_signed()
        assert context_snapshot() == before


def test_summary_dict_writes_fixed_two_places_half_even():
    with constrained_decimal_context():
        before = context_snapshot()
        bill = make_bill(
            total=Decimal("123456789012345678901234567890.125"),
            fees=[
                Fee("half-up-even", Decimal("2.675")),
                Fee("half-down-even", Decimal("2.665")),
                Fee("carry", Decimal("999999999.995")),
                Fee("tiny-negative", Decimal("-0.004")),
                Fee("tiny-positive", Decimal("0.001")),
                Fee("negative-zero", Decimal("-0.000")),
                Fee("zero", Decimal("0")),
            ],
        )

        data = api.to_summary_dict(bill)
        # decided from the source Decimal, not via float: float(2.675)
        # is 2.67499999999999982... and would round to "2.67"
        amounts = [fee["amount"] for fee in data["fees"]]
        assert amounts == [
            "2.68",
            "2.66",
            "1000000000.00",
            "-0.00",
            "0.00",
            "-0.00",
            "0.00",
        ]
        # more digits than the caller's prec=10, half-even on .125
        assert data["total"] == "123456789012345678901234567890.12"
        assert context_snapshot() == before


def test_summary_json_roundtrip_keeps_negative_zero_sign():
    with constrained_decimal_context():
        before = context_snapshot()
        bill = make_bill(
            total=Decimal("-0.000"),
            fees=[Fee("zero", Decimal("0.00"))],
        )

        payload = api.to_summary_json(bill)
        decoded = json.loads(payload)
        assert decoded["total"] == "-0.00"
        assert decoded["fees"][0]["amount"] == "0.00"

        restored = api.bill_from_summary_json(payload)
        assert restored.total == Decimal("-0.00")
        assert restored.total.is_signed()
        assert restored.total.as_tuple().exponent == -2
        assert not restored.fees[0].amount.is_signed()
        assert context_snapshot() == before


def test_summary_dict_roundtrip_reads_back_two_place_values():
    with constrained_decimal_context():
        bill = make_bill(
            total=Decimal("2.675"), fees=[Fee("f", Decimal("1.5"))]
        )
        restored = api.bill_from_summary_dict(api.to_summary_dict(bill))
        assert restored.total == Decimal("2.68")
        assert restored.total.as_tuple().exponent == -2
        assert restored.fees[0].amount == Decimal("1.50")


def test_field_level_strategy_applies_only_to_its_own_field():
    with constrained_decimal_context():
        bill = make_bill()
        for data in (api.to_archive_dict(bill), api.to_summary_dict(bill)):
            assert data["rate"] == "12.5000%"
            assert not data["total"].endswith("%")
            assert all(not f["amount"].endswith("%") for f in data["fees"])

        restored = api.bill_from_summary_dict(api.to_summary_dict(bill))
        assert restored.rate == RATE
        restored = api.bill_from_archive_dict(api.to_archive_dict(bill))
        assert restored.rate == RATE


def test_default_codecs_keep_library_decimal_format():
    with constrained_decimal_context():
        bill = make_bill()
        encoder = BasicEncoder(Bill)
        decoder = BasicDecoder(Bill)

        data = encoder.encode(bill)
        assert data["total"] == str(BIG_TOTAL)
        assert [f["amount"] for f in data["fees"]] == [
            str(TRAILING_ZEROS),
            str(NEGATIVE_ZERO),
            str(TINY_NEGATIVE),
        ]
        assert data["rate"] == "12.5000%"  # field-level strategy still wins

        restored = decoder.decode(data)
        assert restored.total.as_tuple() == BIG_TOTAL.as_tuple()


def test_alternating_dialects_do_not_leak_rules_or_touch_context():
    with constrained_decimal_context():
        before = context_snapshot()
        bill = make_bill(total=Decimal("123456789012345678901234567890.125"))

        first_round = (
            api.to_archive_dict(bill),
            api.to_summary_dict(bill),
            api.to_archive_json(bill),
            api.to_summary_json(bill),
        )
        assert first_round[0]["total"].endswith(".125")
        assert first_round[1]["total"].endswith(".12")
        assert json.loads(first_round[2])["total"].endswith(".125")
        assert json.loads(first_round[3])["total"].endswith(".12")

        readbacks = (
            api.bill_from_archive_dict(first_round[0]),
            api.bill_from_summary_dict(first_round[1]),
            api.bill_from_archive_json(first_round[2]),
            api.bill_from_summary_json(first_round[3]),
        )
        assert readbacks[0].total.as_tuple() == bill.total.as_tuple()
        assert readbacks[1].total == Decimal(
            "123456789012345678901234567890.12"
        )
        assert readbacks[2].total.as_tuple() == bill.total.as_tuple()
        assert readbacks[3].total == readbacks[1].total

        second_round = (
            api.to_archive_dict(bill),
            api.to_summary_dict(bill),
            api.to_archive_json(bill),
            api.to_summary_json(bill),
        )
        assert second_round == first_round
        assert context_snapshot() == before


def test_invalid_amount_raises_and_leaves_context_and_exports_intact():
    with constrained_decimal_context():
        before = context_snapshot()
        bill = make_bill()
        archive_before = api.to_archive_dict(bill)
        summary_before = api.to_summary_dict(bill)

        bad_dict = api.to_archive_dict(bill)
        bad_dict["total"] = "not-a-decimal"
        bad_json = json.dumps(bad_dict)

        for decode in (
            api.bill_from_archive_dict,
            api.bill_from_summary_dict,
        ):
            with pytest.raises(InvalidFieldValue) as exc_info:
                decode(bad_dict)
            assert isinstance(exc_info.value.__context__, InvalidOperation)
            assert context_snapshot() == before
        for decode in (
            api.bill_from_archive_json,
            api.bill_from_summary_json,
        ):
            with pytest.raises(InvalidFieldValue) as exc_info:
                decode(bad_json)
            assert isinstance(exc_info.value.__context__, InvalidOperation)
            assert context_snapshot() == before

        # the next export round is unaffected by the failures
        assert api.to_archive_dict(bill) == archive_before
        assert api.to_summary_dict(bill) == summary_before
        assert context_snapshot() == before


def test_invalid_rate_raises_and_leaves_context_intact():
    with constrained_decimal_context():
        before = context_snapshot()
        data = api.to_archive_dict(make_bill())
        data["rate"] = "12.5"  # missing the agreed '%' suffix
        with pytest.raises(InvalidFieldValue) as exc_info:
            api.bill_from_archive_dict(data)
        assert isinstance(exc_info.value.__context__, ValueError)
        assert context_snapshot() == before

import datetime
import unittest

from beangulp import date_utils


class TestDateUtils(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(datetime.date(2021, 7, 4), date_utils.parse_date("2021-07-04"))

    def test_parse_date_without_period_is_unchanged(self):
        # A date that already carries a year parses exactly as before.
        self.assertEqual(
            datetime.date(2021, 7, 4), date_utils.parse_date("2021-07-04", period=None)
        )

    def test_period_crosses_year_boundary(self):
        period = date_utils.Period("2023-12-15", "2024-01-15")
        self.assertEqual(
            datetime.date(2023, 12, 31), date_utils.parse_date("12-31", period=period)
        )
        self.assertEqual(
            datetime.date(2024, 1, 5), date_utils.parse_date("01-05", period=period)
        )

    def test_period_accepts_tuple_and_dict(self):
        self.assertEqual(
            datetime.date(2023, 12, 31),
            date_utils.parse_date("12/31", period=("2023-12-15", "2024-01-15")),
        )
        self.assertEqual(
            datetime.date(2023, 12, 31),
            date_utils.parse_date(
                "12/31", period={"start": "2023-12-15", "end": "2024-01-15"}
            ),
        )

    def test_explicit_year_wins_over_period(self):
        period = date_utils.Period("2023-12-15", "2024-01-15")
        self.assertEqual(
            datetime.date(2019, 12, 31),
            date_utils.parse_date("2019-12-31", period=period),
        )

    def test_date_outside_period_is_reported(self):
        period = date_utils.Period("2024-01-01", "2024-01-31")
        with self.assertRaises(date_utils.DateOutOfPeriodError) as ctx:
            date_utils.parse_date("12-31", period=period)
        self.assertEqual("12-31", ctx.exception.value)
        self.assertEqual([], ctx.exception.candidates)
        self.assertIn("outside the statement period", str(ctx.exception))

    def test_wide_period_is_ambiguous(self):
        period = date_utils.Period("2023-01-01", "2025-01-31")
        with self.assertRaises(date_utils.AmbiguousDateError) as ctx:
            date_utils.parse_date("12-31", period=period)
        self.assertEqual("12-31", ctx.exception.value)
        self.assertEqual(
            [datetime.date(2023, 12, 31), datetime.date(2024, 12, 31)],
            ctx.exception.candidates,
        )
        self.assertIn("possible years: 2023, 2024", str(ctx.exception))

    def test_date_format_with_period(self):
        period = date_utils.Period("2023-12-15", "2024-01-15")
        self.assertEqual(
            datetime.date(2023, 12, 31),
            date_utils.parse_date_format("12/31", "%m/%d", period),
        )
        self.assertEqual(
            datetime.date(2024, 1, 5),
            date_utils.parse_date_format("05.01.", "%d.%m.", period),
        )

    def test_date_format_with_year_ignores_period(self):
        self.assertEqual(
            datetime.date(2020, 12, 31),
            date_utils.parse_date_format("31.12.2020", "%d.%m.%Y", None),
        )

    def test_date_format_without_year_keeps_legacy_behavior(self):
        # Without a period the format is applied as before.
        self.assertEqual(
            datetime.date(1900, 12, 31),
            date_utils.parse_date_format("12/31", "%m/%d"),
        )

    def test_period_rejects_reversed_bounds(self):
        with self.assertRaisesRegex(ValueError, "precedes start date"):
            date_utils.Period("2024-01-15", "2023-12-15")

    def test_period_bounds_require_a_year(self):
        with self.assertRaisesRegex(ValueError, "must include a year"):
            date_utils.Period("12-15", "01-15")

    def test_receipt_records_original_cell_value(self):
        problem = date_utils.DateProblem(
            "12-31",
            "the date falls outside the statement period",
            period=date_utils.Period("2024-01-01", "2024-01-31"),
            filename="statement.csv",
            line=7,
            column="[DATE]",
        )
        receipt = date_utils.Receipt([problem])
        self.assertIn("statement.csv:7", str(receipt))
        self.assertIn("'12-31'", str(receipt))
        self.assertEqual("12-31", problem["value"])
        self.assertEqual("12-31", problem.as_dict()["value"])

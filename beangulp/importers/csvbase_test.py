import datetime
import decimal
import re
import unittest

from beancount.core import data
from beancount.core.number import D
from beancount.parser import cmptest
from beancount.utils.test_utils import docfile
from beangulp.importers.csvbase import (
    _chomp,
    Column,
    Columns,
    Date,
    Amount,
    CSVMeta,
    CSVReader,
    Order,
    Importer,
    CreditOrDebit,
    CreditOrDebitError,
)


class TestColumn(unittest.TestCase):
    def test_index_spec(self):
        column = Column(0)
        func = column.getter(None)
        value = func(
            (
                "0",
                "1",
                "2",
                "3",
            )
        )
        self.assertEqual(value, "0")

    def test_named_spec(self):
        column = Column("Num")
        func = column.getter({"Num": 1})
        value = func(
            (
                "0",
                "1",
                "2",
                "3",
            )
        )
        self.assertEqual(value, "1")

    def test_named_errrors(self):
        column = Column("A")
        with self.assertRaisesRegex(KeyError, "Cannot find column 'A' in "):
            column.getter({"B": 0, "C": 1})
        with self.assertRaisesRegex(KeyError, "Column 'A' cannot be found in "):
            column.getter(None)

    def test_strip(self):
        # The default field parser strips whitespace.
        column = Column(0)
        func = column.getter(None)
        value = func((" value ",))
        self.assertEqual(value, "value")

    def test_default_value(self):
        column = Column(0, default=42)
        func = column.getter(None)
        value = func(("",))
        self.assertEqual(value, 42)

    def test_default_value_none(self):
        column = Column(0, default=None)
        func = column.getter(None)
        value = func(("",))
        self.assertIsNone(value)


class TestDateColumn(unittest.TestCase):
    def test_default_format(self):
        column = Date(0)
        func = column.getter(None)
        value = func(("2021-05-16",))
        self.assertEqual(value, datetime.date(2021, 5, 16))

    def test_custom_format(self):
        column = Date(0, "%d.%m.%Y")
        func = column.getter(None)
        value = func(("16.05.2021",))
        self.assertEqual(value, datetime.date(2021, 5, 16))

    def test_default_value(self):
        column = Date(0, default=datetime.date.today())
        func = column.getter(None)
        value = func(("",))
        self.assertEqual(value, datetime.date.today())

    def test_default_value_none(self):
        column = Date(0, default=None)
        func = column.getter(None)
        value = func(("",))
        self.assertIsNone(value)


class TestColumnsColumn(unittest.TestCase):
    def test_default_sep(self):
        column = Columns(0, 1)
        func = column.getter(None)
        value = func(
            (
                "0",
                "1",
                "2",
                "3",
            )
        )
        self.assertEqual(value, "0 1")

    def test_custom_sep(self):
        column = Columns(0, 1, sep=": ")
        func = column.getter(None)
        value = func(
            (
                "0",
                "1",
                "2",
                "3",
            )
        )
        self.assertEqual(value, "0: 1")

    def test_default_value(self):
        column = Columns(0, 1, default="something")
        func = column.getter(None)
        value = func(
            (
                "",
                "",
            )
        )
        self.assertEqual(value, "something")

    def test_default_value_none(self):
        column = Columns(0, 1, default=None)
        func = column.getter(None)
        value = func(
            (
                "",
                "",
            )
        )
        self.assertIsNone(value)

    def test_some_empty(self):
        column = Columns(0, 1, default=None)
        func = column.getter(None)
        value = func(
            (
                "this",
                "",
            )
        )
        self.assertEqual(value, "this")


class TestAmountColumn(unittest.TestCase):
    def test_parse_decimal(self):
        column = Amount(0)
        func = column.getter(None)
        value = func(("1.0",))
        self.assertIsInstance(value, decimal.Decimal)
        self.assertEqual(value, decimal.Decimal("1.0"))

    def test_parse_subs_one(self):
        column = Amount(0, subs={",": ""})
        func = column.getter(None)
        value = func(("1,000.00",))
        self.assertIsInstance(value, decimal.Decimal)
        self.assertEqual(value, decimal.Decimal("1000.00"))

    def test_parse_subs_two(self):
        column = Amount(0, subs={"\\.": "", ",": "."})
        func = column.getter(None)
        value = func(("1.000,00",))
        self.assertIsInstance(value, decimal.Decimal)
        self.assertEqual(value, decimal.Decimal("1000.00"))

    def test_parse_subs_currency(self):
        column = Amount(0, subs={"\\$(.*)": "\\1", ",": ""})
        func = column.getter(None)
        value = func(("$1,000.00",))
        self.assertIsInstance(value, decimal.Decimal)
        self.assertEqual(value, decimal.Decimal("1000.00"))

    def test_default_value(self):
        column = Amount(0, default=decimal.Decimal(42))
        func = column.getter(None)
        value = func(("",))
        self.assertEqual(value, decimal.Decimal(42))

    def test_default_value_none(self):
        column = Amount(0, default=None)
        func = column.getter(None)
        value = func(("",))
        self.assertIsNone(value)

    def test_parse_negate(self):
        column = Amount(0, negate=True)
        func = column.getter(None)
        value = func(("123.45",))
        self.assertIsInstance(value, decimal.Decimal)
        self.assertEqual(value, -decimal.Decimal("123.45"))


class TestCreditOrDebitColumn(unittest.TestCase):
    def test_parse_credit(self):
        column = CreditOrDebit(0, 1)
        func = column.getter(None)
        value = func(("1.0", ""))
        self.assertEqual(value, decimal.Decimal("1.0"))

    def test_parse_debit(self):
        column = CreditOrDebit(0, 1)
        func = column.getter(None)
        value = func(("", "1.0"))
        self.assertEqual(value, -decimal.Decimal("1.0"))

    def test_parse_subs(self):
        column = CreditOrDebit(0, 1, subs={",": ""})
        func = column.getter(None)
        value = func(("1,000.00", ""))
        self.assertIsInstance(value, decimal.Decimal)
        self.assertEqual(value, decimal.Decimal("1000.00"))

    def test_default_value(self):
        column = CreditOrDebit(0, 1, default=decimal.Decimal(42))
        func = column.getter(None)
        value = func(("", ""))
        self.assertEqual(value, decimal.Decimal(42))

    def test_default_value_none(self):
        column = CreditOrDebit(0, 1, default=None)
        func = column.getter(None)
        value = func(("", ""))
        self.assertIsNone(value)

    def test_both_columns(self):
        column = CreditOrDebit(0, 1)
        func = column.getter(None)
        with self.assertRaisesRegex(
            ValueError, "The credit and debit fields cannot be populated at the same time"
        ):
            func(("1.0", "2.0"))

    def test_neither_columns(self):
        column = CreditOrDebit(0, 1)
        func = column.getter(None)
        with self.assertRaisesRegex(
            ValueError, "Neither credit or debit fields are populated"
        ):
            func(("", ""))

    def test_column_policy_negative_debit_is_reversal(self):
        column = CreditOrDebit(0, 1)
        func = column.getter(None)
        # A negative debit reverses the original (negated) direction,
        # thus it becomes a positive amount.
        value = func(("", "-1.250"))
        self.assertEqual(value, decimal.Decimal("1.250"))

    def test_column_policy_negative_credit_rejected(self):
        column = CreditOrDebit(0, 1)
        func = column.getter(None)
        with self.assertRaisesRegex(CreditOrDebitError, "sign convention"):
            func(("-1.250", ""))

    def test_signed_policy_negative_debit_is_expense(self):
        column = CreditOrDebit(0, 1, sign_policy=CreditOrDebit.SIGN_SIGNED)
        func = column.getter(None)
        value = func(("", "-1.250"))
        self.assertEqual(value, decimal.Decimal("-1.250"))

    def test_signed_policy_positive_debit_is_refund(self):
        column = CreditOrDebit(0, 1, sign_policy=CreditOrDebit.SIGN_SIGNED)
        func = column.getter(None)
        value = func(("", "1.250"))
        self.assertEqual(value, decimal.Decimal("1.250"))

    def test_signed_policy_credit_unchanged(self):
        column = CreditOrDebit(0, 1, sign_policy=CreditOrDebit.SIGN_SIGNED)
        func = column.getter(None)
        value = func(("1.250", ""))
        self.assertEqual(value, decimal.Decimal("1.250"))

    def test_signed_policy_negative_credit_rejected(self):
        column = CreditOrDebit(0, 1, sign_policy=CreditOrDebit.SIGN_SIGNED)
        func = column.getter(None)
        with self.assertRaisesRegex(CreditOrDebitError, "sign convention"):
            func(("-1.250", ""))

    def test_both_columns_error_type(self):
        column = CreditOrDebit(0, 1)
        func = column.getter(None)
        with self.assertRaises(CreditOrDebitError):
            func(("1.0", "2.0"))


class TestCSVMeta(unittest.TestCase):
    def test_collect_fields(self):
        class CSVTest(metaclass=CSVMeta):
            first = Column(0)
            second = Column(1)

        # pylint: disable=no-member
        self.assertEqual(CSVTest.columns.keys(), {"first", "second"})


class TestCSVReader(unittest.TestCase):
    @docfile
    def test_named_columns(self, filename):
        """\
        First, Second
        1, 2
        3, 4
        """

        class Reader(CSVReader):
            first = Column("First")
            second = Column("Second")

        reader = Reader()
        rows = list(reader.read(filename))
        self.assertEqual(len(rows), 2)
        # tuple elements
        self.assertEqual(rows[0][0], "1")
        # leading space is preserved
        self.assertEqual(rows[1][1], " 4")
        # attribute accessors
        self.assertEqual(rows[0].first, "1")
        self.assertEqual(rows[1].second, "4")

    @docfile
    def test_named_no_enough_lines(self, filename):
        """\
        # comment
        """

        class Reader(CSVReader):
            first = Column("First")
            second = Column("Second")

        reader = Reader()
        with self.assertRaisesRegex(IndexError, "The input file does not contain "):
            list(reader.read(filename))

    @docfile
    def test_indexed_columns_names_false(self, filename):
        """\
        First, Second
        1, 2
        3, 4
        """

        class Reader(CSVReader):
            first = Column(0)
            second = Column(1)
            names = False

        reader = Reader()
        rows = list(reader.read(filename))
        self.assertEqual(len(rows), 3)
        # tuple elements
        self.assertEqual(rows[0][0], "First")
        # leading space is preserved
        self.assertEqual(rows[2][1], " 4")
        # attribute accessors
        self.assertEqual(rows[0].first, "First")
        self.assertEqual(rows[2].second, "4")

    @docfile
    def test_indexed_columns_names_true(self, filename):
        """\
        First, Second
        1, 2
        3, 4
        """

        class Reader(CSVReader):
            first = Column(0)
            second = Column(1)

        reader = Reader()
        rows = list(reader.read(filename))
        self.assertEqual(len(rows), 2)
        # tuple elements
        self.assertEqual(rows[0][0], "1")
        # leading space is preserved
        self.assertEqual(rows[1][1], " 4")
        # attribute accessors
        self.assertEqual(rows[0].first, "1")
        self.assertEqual(rows[1].second, "4")

    @docfile
    def test_comments(self, filename):
        """\
        First, Second
        # ignore
        a, b
        c, d
        """

        class Reader(CSVReader):
            first = Column(0)
            second = Column(1)

        reader = Reader()
        rows = list(reader.read(filename))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "a")

    @docfile
    def test_no_comments(self, filename):
        """\
        First, Second
        # ignore
        a, b
        c, d
        """

        class Reader(CSVReader):
            first = Column(0)
            second = Column(1)
            comments = False

        reader = Reader()
        rows = list(reader.read(filename))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], "# ignore")

    @docfile
    def test_custom_comments(self, filename):
        """\
        First, Second
        # ignore
        ; ignore
        a, b
        c, d
        """

        class Reader(CSVReader):
            first = Column(0)
            second = Column(1)
            comments = ";"

        reader = Reader()
        rows = list(reader.read(filename))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], "# ignore")

    @docfile
    def test_skiplines(self, filename):
        """\
        Skip
        Skip
        First, Second
        a, b
        """

        class Reader(CSVReader):
            first = Column(0)
            second = Column(1)
            skiplines = 2

        reader = Reader()
        rows = list(reader.read(filename))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "a")

    @docfile
    def test_footer(self, filename):
        """\
        Header
        First, Second
        a, b
        Footer
        Footer
        """

        class Reader(CSVReader):
            first = Column(0)
            second = Column(1)
            header = 1
            footer = 2

        reader = Reader()
        rows = list(reader.read(filename))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "a")


class Base(Importer):
    def identify(self, filepath):
        return True


class TestImporter(cmptest.TestCase):
    @docfile
    def test_date(self, filename):
        """\
        2021-05-17, Test, 1.00
        2021-05-18, Test, 1.00
        2021-05-16, Test, 1.00
        """

        class CSVImporter(Base):
            date = Date(0)

        importer = CSVImporter("Account:CSV", "EUR")
        date = importer.date(filename)
        self.assertEqual(date, datetime.date(2021, 5, 18))

    @docfile
    def test_extract(self, filename):
        """\
        2021-05-17, Test, 1.00
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            names = False

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-17 * "Test"
          Assets:CSV  1.00 EUR
        """,
        )

    @docfile
    def test_extract_payee(self, filename):
        """\
        2021-05-17, Payee, Test, 1.00
        """

        class CSVImporter(Base):
            date = Date(0)
            payee = Column(1)
            narration = Column(2)
            amount = Amount(3)
            names = False

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-17 * "Payee" "Test"
          Assets:CSV  1.00 EUR
        """,
        )

    @docfile
    def test_extract_account(self, filename):
        """\
        2021-05-17, Test, Assets:Test, 1.00
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            account = Column(2)
            amount = Amount(3)
            names = False

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-17 * "Test"
          Assets:Test  1.00 EUR
        """,
        )

    @docfile
    def test_extract_balance(self, filename):
        """\
        2021-05-17, Test A, 1.00, 1.00
        2021-05-18, Test B, 2.00, 3.00
        2021-05-18, Test C, 1.00, 4.00
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            balance = Amount(3)
            names = False

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-17 * "Test A"
          Assets:CSV  1.00 EUR
        2021-05-18 * "Test B"
          Assets:CSV  2.00 EUR
        2021-05-18 * "Test C"
          Assets:CSV  1.00 EUR
        2021-05-19 balance Assets:CSV  4.00 EUR
        """,
        )

    @docfile
    def test_extract_balance_order(self, filename):
        """\
        2021-05-18, Test B, 2.00, 4.00
        2021-05-18, Test C, 1.00, 2.00
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            balance = Amount(3)
            names = False
            order = Order.DESCENDING

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-18 * "Test C"
          Assets:CSV  1.00 EUR
        2021-05-18 * "Test B"
          Assets:CSV  2.00 EUR
        2021-05-19 balance Assets:CSV  4.00 EUR
        """,
        )

    @docfile
    def test_extract_flag(self, filename):
        """\
        2021-05-17, Test, 1.00, !
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            flag = Column(3)
            names = False

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-17 ! "Test"
          Assets:CSV  1.00 EUR
        """,
        )

    @docfile
    def test_extract_link_and_tag(self, filename):
        """\
        2021-05-17, Test, 1.00, link, tag
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            link = Column(3)
            tag = Column(4)
            names = False

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-17 * "Test" ^link #tag
          Assets:CSV  1.00 EUR
        """,
        )

    @docfile
    def test_extract_currency(self, filename):
        """\
        2021-05-17, Test A US, 1.00, 1.00, USD
        2021-05-17, Test A EU, 2.00, 2.00, EUR
        2021-05-18, Test B US, 2.00, 3.00, USD
        2021-05-18, Test B EU, 3.00, 5.00, EUR
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            balance = Amount(3)
            currency = Column(4)
            names = False

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-17 * "Test A US"
          Assets:CSV  1.00 USD
        2021-05-17 * "Test A EU"
          Assets:CSV  2.00 EUR
        2021-05-18 * "Test B US"
          Assets:CSV  2.00 USD
        2021-05-18 * "Test B EU"
          Assets:CSV  3.00 EUR
        2021-05-19 balance Assets:CSV  3.00 USD
        2021-05-19 balance Assets:CSV  5.00 EUR
        """,
        )

    @docfile
    def test_extract_metadata(self, filename):
        """\
        2021-05-17, Test, 1.00, data, 42
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            meta = Column(3)
            data = Amount(4)
            names = False

            def metadata(self, filepath, lineno, row):
                meta = super().metadata(filepath, lineno, row)
                for field in "meta", "data":
                    meta[field] = getattr(row, field)
                return meta

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-17 * "Test"
          meta: "data"
          data: 42
          Assets:CSV  1.00 EUR
        """,
        )

    @docfile
    def test_extract_finalize(self, filename):
        """\
        2021-05-17, Test, -1.00, Testing
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            category = Column(3)
            names = False

            def finalize(self, txn, row):
                posting = data.Posting(
                    "Expenses:" + row.category,
                    # This could be None in a real importer. However,
                    # the trsting framework accepts only complete
                    # transactions, thus we do the booking manually.
                    -txn.postings[0].units,
                    None,
                    None,
                    None,
                    None,
                )
                txn.postings.append(posting)
                return txn

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqualEntries(
            entries,
            """
        2021-05-17 * "Test"
          Assets:CSV  -1.00 EUR
          Expenses:Testing  1.00 EUR
        """,
        )

    @docfile
    def test_extract_finalize_remove(self, filename):
        """\
        2021-05-17, Test, -1.00, Testing
        2021-05-18, Drop, -2.00, Testing
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            names = False

            def finalize(self, txn, row):
                if txn.narration == "Drop":
                    return None
                return txn

        importer = CSVImporter("Assets:CSV", "EUR")
        entries = importer.extract(filename, [])
        self.assertEqual(len(entries), 1)

    @docfile
    def test_report_exception(self, filename):
        """\
        2025-01-25, Test, -1.00
        2025-01-26, Test, invalid
        """

        class CSVImporter(Base):
            date = Date(0)
            narration = Column(1)
            amount = Amount(2)
            names = False

        importer = CSVImporter("Assets:CSV", "EUR")
        msg = re.escape(
            f"Error processing {filename} line 3 with values ('2025-01-26', ' Test', ' invalid'"
        )
        with self.assertRaisesRegex(RuntimeError, msg) as ctx:
            importer.extract(filename, [])
        self.assertIsInstance(ctx.exception.__cause__, decimal.InvalidOperation)


class ReconcileImporter(Importer):
    def identify(self, filepath):
        return True

    date = Date("Date")
    narration = Column("Description")
    amount = CreditOrDebit("Credit", "Debit")
    balance = Amount("Balance")


class SignedReconcileImporter(ReconcileImporter):
    amount = CreditOrDebit(
        "Credit", "Debit", sign_policy=CreditOrDebit.SIGN_SIGNED
    )


class TestReconciliation(cmptest.TestCase):
    def _importer(self, cls, currency, opening, precision):
        importer = cls(f"Assets:Bank:{currency}", currency)
        importer.reconcile = True
        importer.opening_balances = {currency: decimal.Decimal(opening)}
        importer.precision = precision
        return importer

    @docfile
    def test_column_policy_expense_refund_and_rejections(self, filename):
        """\
        Date,Description,Credit,Debit,Balance
        2025-02-01,Coffee,,1.250,3.750
        2025-02-02,Coffee refund,,-1.250,5.000
        2025-02-03,Both columns populated,0.250,0.250,5.000
        2025-02-04,Negative credit not allowed,-0.300,,5.000
        2025-02-05,Later valid credit,0.500,,5.500
        """
        importer = self._importer(
            ReconcileImporter, "BHD", "5.000", {"BHD": 3}
        )
        entries = importer.extract(filename, [])

        txns = [e for e in entries if isinstance(e, data.Transaction)]
        notes = [e for e in entries if isinstance(e, data.Note)]
        balances = [e for e in entries if isinstance(e, data.Balance)]

        # The two valid rows produce an expense followed by its refund,
        # converted verbatim into signed, fixed-point amounts.
        self.assertEqual(len(txns), 3)
        self.assertEqual(txns[0].postings[0].units, data.Amount(-D("1.250"), "BHD"))
        self.assertEqual(txns[1].postings[0].units, data.Amount(D("1.250"), "BHD"))
        # Per-row book balances are retained for comparison.
        self.assertEqual(txns[0].meta["balance"], data.Amount(D("3.750"), "BHD"))
        self.assertEqual(txns[0].meta["computed_balance"], data.Amount(D("3.750"), "BHD"))
        self.assertEqual(txns[1].meta["balance"], data.Amount(D("5.000"), "BHD"))

        # Rows parsed after the chain was interrupted are kept but
        # flagged, and do not absorb the uncertain balances.
        self.assertEqual(txns[0].flag, "*")
        self.assertEqual(txns[1].flag, "*")
        self.assertEqual(txns[2].flag, "!")
        self.assertIn("reconcile", txns[2].meta)
        self.assertEqual(txns[2].postings[0].units, data.Amount(D("0.500"), "BHD"))

        # Both invalid rows are rejected with their raw columns intact.
        # The both-columns row would naively net to zero and keep the
        # ending balance at 5.000, but it must still be rejected.
        self.assertEqual(len(notes), 3)
        comments = " ".join(n.comment for n in notes)
        self.assertIn("'0.250', '0.250'", comments)
        self.assertIn("cannot be populated at the same time", comments)
        self.assertIn("-0.300", comments)
        self.assertIn("sign convention", comments)
        # The last statement balance is only reported, never asserted.
        self.assertIn("5.500", comments)

        # Only the balance confirmed before the interruption is asserted.
        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].amount, data.Amount(D("5.000"), "BHD"))

    @docfile
    def test_signed_policy_expense_refund_and_rejections(self, filename):
        """\
        Date,Description,Credit,Debit,Balance
        2025-02-01,Coffee,,-1.250,3.750
        2025-02-02,Coffee refund,,1.250,5.000
        2025-02-03,Negative credit not allowed,-0.300,,5.000
        2025-02-04,Both columns populated,0.250,0.250,5.000
        """
        importer = self._importer(
            SignedReconcileImporter, "BHD", "5.000", {"BHD": 3}
        )
        entries = importer.extract(filename, [])

        txns = [e for e in entries if isinstance(e, data.Transaction)]
        notes = [e for e in entries if isinstance(e, data.Note)]
        balances = [e for e in entries if isinstance(e, data.Balance)]

        # Globally signed values are taken verbatim: negative debit is
        # the expense and positive debit is the refund.
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].postings[0].units, data.Amount(-D("1.250"), "BHD"))
        self.assertEqual(txns[1].postings[0].units, data.Amount(D("1.250"), "BHD"))
        self.assertEqual(txns[0].meta["computed_balance"], data.Amount(D("3.750"), "BHD"))
        self.assertEqual(txns[1].meta["computed_balance"], data.Amount(D("5.000"), "BHD"))

        comments = " ".join(n.comment for n in notes)
        self.assertIn("-0.300", comments)
        self.assertIn("sign convention", comments)
        self.assertIn("'0.250', '0.250'", comments)

        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].amount, data.Amount(D("5.000"), "BHD"))

    @docfile
    def test_zero_decimal_currency_precision(self, filename):
        """\
        Date,Description,Credit,Debit,Balance
        2025-03-01,Rent,,200,800.4
        2025-03-02,Rent refund,200,,1000
        """

        class JPYImporter(ReconcileImporter):
            pass

        importer = self._importer(JPYImporter, "JPY", "1000", {"JPY": 0})
        entries = importer.extract(filename, [])

        txns = [e for e in entries if isinstance(e, data.Transaction)]
        balances = [e for e in entries if isinstance(e, data.Balance)]
        self.assertEqual(txns[0].postings[0].units, data.Amount(-D("200"), "JPY"))
        self.assertEqual(txns[1].postings[0].units, data.Amount(D("200"), "JPY"))
        # 800.4 and the computed 800 match at the zero-decimal JPY precision.
        self.assertEqual(txns[0].meta["computed_balance"], data.Amount(D("800"), "JPY"))
        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].amount, data.Amount(D("1000"), "JPY"))

    def test_quantize_precision(self):
        importer = ReconcileImporter("Assets:Bank:BHD", "BHD")
        importer.precision = {"BHD": 3, "JPY": 0}
        self.assertEqual(
            importer._quantize(D("1.2509"), "BHD"), D("1.251")
        )
        self.assertEqual(importer._quantize(D("800.4"), "JPY"), D("800"))
        # Unknown currencies and missing precision are left untouched.
        self.assertEqual(importer._quantize(D("1.23456"), "USD"), D("1.23456"))
        importer.precision = 2
        self.assertEqual(importer._quantize(D("1.235"), "BHD"), D("1.24"))

    @docfile
    def test_disabled_reconciliation_keeps_strict_contract(self, filename):
        """\
        Date,Description,Credit,Debit,Balance
        2025-02-03,Both columns populated,0.100,0.200,5.000
        """
        importer = ReconcileImporter("Assets:Bank:BHD", "BHD")
        # reconcile defaults to False: invalid rows still abort extraction.
        with self.assertRaises(RuntimeError):
            importer.extract(filename, [])


class TestChomp(unittest.TestCase):

    def test_header(self):
        self.assertEqual(list(_chomp(range(10), 2, 0)), [2, 3, 4, 5, 6, 7, 8, 9])

    def test_footer(self):
        self.assertEqual(list(_chomp(range(10), 0, 3)), [0, 1, 2, 3, 4, 5, 6])

    def test_header_and_footer(self):
        self.assertEqual(list(_chomp(range(10), 2, 3)), [2, 3, 4, 5, 6])

    def test_short(self):
        self.assertEqual(list(_chomp(range(1), 2, 3)), [])

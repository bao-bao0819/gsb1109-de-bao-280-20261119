import datetime
import textwrap
import unittest

from decimal import Decimal

from beancount import loader
from beancount.core import data
from beancount.core.amount import Amount

import beanquery


class TestDepartmentResolution(unittest.TestCase):
    maxDiff = 8192

    INPUT = """
    2020-01-01 open Expenses:Food
      department: "Kitchen"
    2020-01-01 open Expenses:Food:Sub
    2020-01-01 open Assets:Cash
    2025-06-15 close Expenses:Food

    2025-04-10 * "April lunch"
      Expenses:Food:Sub  10.00 USD
      Assets:Cash

    2025-06-20 * "June snack"
      Expenses:Food:Sub   5.00 USD
      Assets:Cash

    2025-07-01 * "Shared trip"
      department: "Travel"
      Expenses:Food:Sub   8.00 USD
        department: ""
      Assets:Cash

    2025-07-02 * "Explicit after close"
      Expenses:Food:Sub   3.00 USD
        department: "Front"
      Assets:Cash
    """

    def setUp(self):
        entries, errors, options = loader.load_string(textwrap.dedent(self.INPUT))
        assert not errors
        self.ctx = beanquery.connect('beancount:', entries=entries, errors=errors, options=options)

    def execute(self, query):
        return self.ctx.execute(query).fetchall()

    def test_resolve(self):
        # The April posting inherits the ancestor account default still
        # in effect at that date. The June posting finds no usable
        # default because the ancestor is closed in June. The posting
        # explicitly clearing the department and the posting with an
        # explicitly declared department take precedence over any
        # inherited value, even after the ancestor has been closed.
        rows = self.execute('''
            SELECT date, account, department, department_source
            WHERE account ~ 'Expenses'
            ORDER BY date
        ''')
        self.assertEqual(rows, [
            (datetime.date(2025, 4, 10), 'Expenses:Food:Sub', 'Kitchen', 'default'),
            (datetime.date(2025, 6, 20), 'Expenses:Food:Sub', None, 'missing'),
            (datetime.date(2025, 7, 1), 'Expenses:Food:Sub', None, 'cleared'),
            (datetime.date(2025, 7, 2), 'Expenses:Food:Sub', 'Front', 'posting'),
        ])

    def test_shared_transaction(self):
        # Two postings of the same transaction: one explicitly clears
        # the department, the other does not declare one and inherits
        # the transaction level value. The two must not be mixed up.
        rows = self.execute('''
            SELECT account, department, department_source
            WHERE date = 2025-07-01
            ORDER BY account
        ''')
        self.assertEqual(rows, [
            ('Assets:Cash', 'Travel', 'transaction'),
            ('Expenses:Food:Sub', None, 'cleared'),
        ])

    def test_filter_default(self):
        rows = self.execute('''
            SELECT department, count(*)
            WHERE department_source = 'default'
            GROUP BY department
        ''')
        self.assertEqual(rows, [('Kitchen', 1)])

    def test_group_by_source(self):
        rows = self.execute('''
            SELECT department_source, count(*)
            GROUP BY department_source
            ORDER BY department_source
        ''')
        self.assertEqual(rows, [
            ('cleared', 1),
            ('default', 1),
            ('missing', 4),
            ('posting', 1),
            ('transaction', 1),
        ])

    def test_raw_meta_functions(self):
        # The metadata accessors directly reading the posting, the
        # transaction or the open directive keep returning the raw
        # declared values.
        rows = self.execute('''
            SELECT meta('department'), entry_meta('department')
            WHERE date = 2025-07-01
            ORDER BY account
        ''')
        self.assertEqual(rows, [
            (None, 'Travel'),
            ('', 'Travel'),
        ])
        rows = self.execute('''
            SELECT DISTINCT open_meta('Expenses:Food', 'department')
        ''')
        self.assertEqual(rows, [('Kitchen',)])


class TestDepartmentObjects(unittest.TestCase):
    """Department resolution with entries built as Beancount objects."""

    def test_absent_key_vs_declared_empty(self):
        def meta(**kwargs):
            m = data.new_metadata('<test>', 0)
            m.update(kwargs)
            return m

        _, _, options = loader.load_string('')
        entries = [
            data.Open(meta(department='Kitchen'), datetime.date(2020, 1, 1), 'Expenses:Food', None, None),
            data.Open(meta(), datetime.date(2020, 1, 1), 'Expenses:Food:Sub', None, None),
            data.Open(meta(), datetime.date(2020, 1, 1), 'Assets:Cash', None, None),
        ]
        for day, posting_meta in ((1, meta(department=None)), (2, meta())):
            entries.append(data.Transaction(
                meta(), datetime.date(2021, 1, day), '*', None, 'narration', None, None, [
                    data.Posting('Expenses:Food:Sub', Amount(Decimal('1.00'), 'USD'), None, None, None, posting_meta),
                    data.Posting('Assets:Cash', Amount(Decimal('-1.00'), 'USD'), None, None, None, None),
                ]))
        ctx = beanquery.connect('beancount:', entries=entries, errors=[], options=options)
        rows = ctx.execute('''
            SELECT date, account, department, department_source
            ORDER BY date, account
        ''').fetchall()
        self.assertEqual(rows, [
            # The department key is present and explicitly empty.
            (datetime.date(2021, 1, 1), 'Assets:Cash', None, 'missing'),
            (datetime.date(2021, 1, 1), 'Expenses:Food:Sub', None, 'cleared'),
            # The department key is absent from the posting metadata.
            (datetime.date(2021, 1, 2), 'Assets:Cash', None, 'missing'),
            (datetime.date(2021, 1, 2), 'Expenses:Food:Sub', 'Kitchen', 'default'),
        ])


if __name__ == '__main__':
    unittest.main()

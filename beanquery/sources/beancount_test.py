import textwrap
import unittest

import beanquery

from beancount import loader


class TestStableReferences(unittest.TestCase):

    def connect(self, text):
        entries, errors, options = loader.load_string(textwrap.dedent(text), dedent=True)
        self.assertFalse(errors)
        return beanquery.connect('beancount:', entries=entries, errors=[], options=options)

    def query(self, conn, query):
        cursor = conn.execute(query)
        return cursor.fetchall()

    def test_postings_ref(self):
        conn = self.connect("""
          2026-01-01 open Assets:Cash
          2026-01-01 open Expenses:Food
          2026-01-01 open Expenses:Fees

          2026-01-02 * "Grocery"
            document: "V-001"
            Expenses:Food  10.00 USD
            Expenses:Fees   1.00 USD
            Assets:Cash

          2026-01-03 * "No voucher"
            Expenses:Food   5.00 USD
            Assets:Cash
        """)
        self.assertFalse(conn.errors)
        rows = self.query(conn, 'SELECT ref, account FROM postings')
        self.assertEqual(rows, [
            ('V-001#1', 'Expenses:Food'),
            ('V-001#2', 'Expenses:Fees'),
            ('V-001#3', 'Assets:Cash'),
            (None, 'Expenses:Food'),
            (None, 'Assets:Cash'),
        ])

    def test_ref_stable_after_reorder(self):
        template = """
          2026-01-01 open Assets:Cash
          2026-01-01 open Expenses:Food
          2026-01-01 open Expenses:Fees

          {transactions}
        """
        txn_a = """
          2026-01-02 * "Grocery"
            document: "V-001"
            Expenses:Food  10.00 USD
            Assets:Cash
        """
        txn_b = """
          2026-01-03 * "Fee"
            document: "V-002"
            Expenses:Fees   1.00 USD
            Assets:Cash
        """
        first = self.connect(template.format(transactions=txn_a + txn_b))
        second = self.connect(template.format(transactions=txn_b + txn_a))
        query = 'SELECT ref, account, number FROM postings WHERE ref IS NOT NULL ORDER BY ref'
        self.assertEqual(
            sorted(self.query(first, query)),
            sorted(self.query(second, query)))

    def test_ref_missing_voucher(self):
        conn = self.connect("""
          2026-01-01 open Assets:Cash
          2026-01-01 open Expenses:Food

          2026-01-02 * "No voucher"
            Expenses:Food   5.00 USD
            Assets:Cash
        """)
        # No stable reference can be established but the filename and
        # lineno columns keep working as usual.
        rows = self.query(conn, 'SELECT ref, filename, lineno FROM postings')
        self.assertEqual(len(rows), 2)
        for ref, filename, lineno in rows:
            self.assertIsNone(ref)
            self.assertTrue(filename)
            self.assertIsInstance(lineno, int)

    def test_ref_duplicate_voucher(self):
        conn = self.connect("""
          2026-01-01 open Assets:Cash
          2026-01-01 open Expenses:Food
          2026-01-01 open Expenses:Fees

          2026-01-02 * "Grocery"
            document: "V-001"
            Expenses:Food  10.00 USD
            Assets:Cash

          2026-01-03 * "Fee"
            document: "V-001"
            Expenses:Fees   1.00 USD
            Assets:Cash
        """)
        # The duplicated voucher number is reported once for each
        # transaction carrying it.
        self.assertEqual(
            sorted(error.message for error in conn.errors),
            ['cannot establish stable reference: duplicate external voucher number "V-001"'] * 2)
        # No stable reference is established for the postings of these
        # transactions but filename and lineno keep working.
        rows = self.query(conn, 'SELECT ref, filename, lineno FROM postings')
        self.assertEqual(len(rows), 4)
        for ref, filename, lineno in rows:
            self.assertIsNone(ref)
            self.assertTrue(filename)
            self.assertIsInstance(lineno, int)

    def test_entries_ref(self):
        conn = self.connect("""
          2026-01-01 open Assets:Cash
          2026-01-01 open Expenses:Food

          2026-01-02 * "Grocery"
            document: "V-001"
            Expenses:Food  10.00 USD
            Assets:Cash

          2026-01-03 * "No voucher"
            Expenses:Food   5.00 USD
            Assets:Cash
        """)
        rows = self.query(conn, "SELECT type, ref FROM entries WHERE type = 'transaction'")
        self.assertEqual(rows, [
            ('transaction', 'V-001'),
            ('transaction', None),
        ])
        # Directives other than transactions have no stable reference.
        rows = self.query(conn, "SELECT DISTINCT ref FROM entries WHERE type = 'open'")
        self.assertEqual(rows, [(None,)])

    def test_transactions_ref(self):
        conn = self.connect("""
          2026-01-01 open Assets:Cash
          2026-01-01 open Expenses:Food

          2026-01-02 * "Grocery"
            document: "V-001"
            Expenses:Food  10.00 USD
            Assets:Cash
        """)
        rows = self.query(conn, 'SELECT ref FROM transactions')
        self.assertEqual(rows, [('V-001',)])


if __name__ == '__main__':
    unittest.main()

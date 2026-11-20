__copyright__ = "Copyright (C) 2014-2017  Martin Blais"
__license__ = "GNU GPLv2"

import os
import tempfile
import textwrap
import unittest

from beancount import loader

import beanquery
from beanquery import references


FIELDS = {
    'institution_field': 'institution',
    'transaction_field': 'external_id',
    'leg_field': 'leg',
}


def _query(input_string, query, **kwargs):
    entries, errors, options = loader.load_string(textwrap.dedent(input_string))
    ctx = beanquery.connect('beancount:', entries=entries, errors=errors, options=options, **kwargs)
    return ctx.execute(query).fetchall()


class TestReferenceEncoding(unittest.TestCase):

    def test_make_reference(self):
        self.assertEqual('BankA:TX1:L1', references.make_reference('BankA', 'TX1', 'L1'))

    def test_components_are_escaped(self):
        # A separator in a component must not allow two different keys to
        # encode to the same reference.
        self.assertNotEqual(references.make_reference('A:B', 'C', 'D'),
                            references.make_reference('A', 'B:C', 'D'))

    def test_fields_from_params(self):
        fields = references.fields_from_params(
            {'institution': 'bank', 'transaction': 'txn', 'leg': 'leg'})
        self.assertEqual(references.ReferenceFields('bank', 'txn', 'leg'), fields)
        explicit = references.fields_from_params(
            {'institution': 'ignored'}, institution_field='declared')
        self.assertEqual('declared', explicit.institution)


class TestPostingReferences(unittest.TestCase):

    INPUT = """
        2026-01-01 open Assets:Bank
        2026-01-01 open Expenses:Foo

        2026-01-02 * "Pay"
          institution: "BankA"
          external_id: "TX1"
          Assets:Bank   -10 USD
            leg: "L1"
          Expenses:Foo   10 USD
            leg: "L2"
    """

    def test_undeclared_fields_leave_references_unavailable(self):
        rows = _query(self.INPUT, 'SELECT reference, reference_status FROM postings')
        self.assertEqual([(None, 'missing'), (None, 'missing')], rows)

    def test_undeclared_fields_keep_existing_columns(self):
        rows = _query(self.INPUT, 'SELECT id, filename, lineno FROM postings ORDER BY lineno')
        self.assertEqual(2, len(rows))
        for row_id, filename, lineno in rows:
            self.assertEqual(rows[0][0], row_id)
            self.assertEqual('<string>', filename)
            self.assertIsInstance(lineno, int)

    def test_reference_columns(self):
        rows = _query(self.INPUT, """
            SELECT account, reference, reference_institution, reference_transaction,
                   reference_leg, reference_status, reference_conflict
            FROM postings ORDER BY lineno
        """, **FIELDS)
        self.assertEqual([
            ('Assets:Bank', 'BankA:TX1:L1', 'BankA', 'TX1', 'L1', 'ok', None),
            ('Expenses:Foo', 'BankA:TX1:L2', 'BankA', 'TX1', 'L2', 'ok', None),
        ], rows)

    def test_reference_query(self):
        rows = _query(self.INPUT, "SELECT account FROM postings WHERE reference = 'BankA:TX1:L1'", **FIELDS)
        self.assertEqual([('Assets:Bank',)], rows)

    def test_reference_through_dsn_parameters(self):
        entries, errors, options = loader.load_string(textwrap.dedent(self.INPUT))
        ctx = beanquery.connect(
            'beancount:?institution_field=institution&transaction_field=external_id&leg_field=leg',
            entries=entries, errors=errors, options=options)
        rows = ctx.execute('SELECT reference FROM postings ORDER BY lineno').fetchall()
        self.assertEqual([('BankA:TX1:L1',), ('BankA:TX1:L2',)], rows)

    def test_query_scoped_declaration(self):
        rows = _query(self.INPUT, """
            SELECT reference, posting_reference('institution', 'external_id', 'leg')
            FROM postings ORDER BY lineno
        """)
        self.assertEqual([
            ('BankA:TX1:L1', 'BankA:TX1:L1'),
            ('BankA:TX1:L2', 'BankA:TX1:L2'),
        ], rows)

    def test_query_scoped_declaration_is_order_independent(self):
        rows = _query(self.INPUT, """
            SELECT reference FROM postings
            WHERE posting_reference('institution', 'external_id', 'leg') IS NOT NULL
            ORDER BY reference
        """)
        self.assertEqual([('BankA:TX1:L1',), ('BankA:TX1:L2',)], rows)

    def test_query_scoped_declaration_requires_literals(self):
        with self.assertRaises(beanquery.CompilationError):
            _query(self.INPUT, """
                SELECT posting_reference(any_meta('institution'), 'external_id', 'leg')
                FROM postings
            """)


class TestReferenceStability(unittest.TestCase):

    INPUT = """
        2026-01-01 open Assets:Bank
        2026-01-01 open Expenses:Foo
        2026-01-01 open Expenses:Bar

        2026-01-02 * "Pay"
          institution: "BankA"
          external_id: "TX1"
          Assets:Bank   -10 USD
            leg: "L1"
          Expenses:Foo   10 USD
            leg: "L2"
    """

    def references(self, input_string):
        rows = _query(input_string, 'SELECT reference, account FROM postings ORDER BY reference', **FIELDS)
        return dict(rows)

    def test_reorder_and_amount_correction(self):
        expected = self.references(self.INPUT)
        changed = """
            2026-01-01 open Assets:Bank
            2026-01-01 open Expenses:Foo

            2026-01-02 * "Pay"
              institution: "BankA"
              external_id: "TX1"
              Expenses:Foo   12 USD
                leg: "L2"
              Assets:Bank   -12 USD
                leg: "L1"
        """
        self.assertEqual(expected, self.references(changed))
        self.assertEqual({'BankA:TX1:L1': 'Assets:Bank', 'BankA:TX1:L2': 'Expenses:Foo'},
                         self.references(changed))

    def test_same_leg_account_and_amount(self):
        # Two legs with the same account and amount must still resolve by
        # their own leg numbers.
        two_legs = """
            2026-01-01 open Assets:Bank
            2026-01-01 open Expenses:Foo

            2026-01-02 * "Pay"
              institution: "BankA"
              external_id: "TX1"
              Assets:Bank   -10 USD
                leg: "L1"
              Assets:Bank   -10 USD
                leg: "L2"
              Expenses:Foo   20 USD
                leg: "L3"
        """
        rows = _query(two_legs, 'SELECT reference, account, number FROM postings ORDER BY reference', **FIELDS)
        self.assertEqual([
            ('BankA:TX1:L1', 'Assets:Bank', -10),
            ('BankA:TX1:L2', 'Assets:Bank', -10),
            ('BankA:TX1:L3', 'Expenses:Foo', 20),
        ], [(r, a, int(n)) for r, a, n in rows])

    def test_moved_to_another_file(self):
        source = """
            2026-01-01 open Assets:Bank
            2026-01-01 open Expenses:Foo
        """
        transaction = """
            2026-01-02 * "Pay"
              institution: "BankA"
              external_id: "TX1"
              Assets:Bank   -10 USD
                leg: "L1"
              Expenses:Foo   10 USD
                leg: "L2"
        """
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, 'first.bean')
            second = os.path.join(directory, 'second.bean')
            loader_file = os.path.join(directory, 'ledger.bean')
            with open(first, 'w') as file:
                file.write(textwrap.dedent(source) + textwrap.dedent(transaction))
            with open(loader_file, 'w') as file:
                file.write(f'include "{os.path.basename(first)}"\n')
            before = _query_from_file(loader_file, 'SELECT reference, filename FROM postings ORDER BY reference', **FIELDS)

            with open(first, 'w') as file:
                file.write(textwrap.dedent(source))
            with open(second, 'w') as file:
                file.write(textwrap.dedent(transaction))
            with open(loader_file, 'w') as file:
                file.write(f'include "{os.path.basename(first)}"\ninclude "{os.path.basename(second)}"\n')
            after = _query_from_file(loader_file, 'SELECT reference, filename FROM postings ORDER BY reference', **FIELDS)

        self.assertEqual([reference for reference, _ in before], [reference for reference, _ in after])
        self.assertEqual([first, first], [filename for _, filename in before])
        self.assertEqual([second, second], [filename for _, filename in after])


class TestReferenceConflicts(unittest.TestCase):

    def test_same_number_at_two_institutions(self):
        input_string = """
            2026-01-01 open Assets:Bank
            2026-01-01 open Expenses:Foo

            2026-01-02 * "Pay A"
              institution: "BankA"
              external_id: "TX1"
              Assets:Bank   -10 USD
                leg: "L1"
              Expenses:Foo   10 USD
                leg: "L2"

            2026-01-03 * "Pay B"
              institution: "BankB"
              external_id: "TX1"
              Assets:Bank   -20 USD
                leg: "L1"
              Expenses:Foo   20 USD
                leg: "L2"
        """
        rows = _query(input_string, 'SELECT reference FROM postings ORDER BY reference', **FIELDS)
        self.assertEqual([('BankA:TX1:L1',), ('BankA:TX1:L2',),
                          ('BankB:TX1:L1',), ('BankB:TX1:L2',)], rows)

    def test_duplicate_transaction_number_conflict(self):
        input_string = """
            2026-01-01 open Assets:Bank
            2026-01-01 open Expenses:Foo

            2026-01-02 * "Pay A"
              institution: "BankA"
              external_id: "TX1"
              Assets:Bank   -10 USD
                leg: "L1"
              Expenses:Foo   10 USD
                leg: "L2"

            2026-01-05 * "Pay B"
              institution: "BankA"
              external_id: "TX1"
              Assets:Bank   -20 USD
                leg: "L1"
              Expenses:Foo   20 USD
                leg: "L2"
        """
        rows = _query(input_string, """
            SELECT reference, reference_status, reference_conflict FROM postings
            ORDER BY lineno
        """, **FIELDS)
        self.assertEqual([(None, 'transaction-conflict', 'transaction')] * 4, rows)

    def test_duplicate_leg_conflict(self):
        input_string = """
            2026-01-01 open Assets:Bank
            2026-01-01 open Expenses:Foo

            2026-01-02 * "Pay"
              institution: "BankA"
              external_id: "TX1"
              Assets:Bank   -10 USD
                leg: "L1"
              Assets:Bank   -10 USD
                leg: "L1"
              Expenses:Foo   20 USD
                leg: "L3"
        """
        rows = _query(input_string, """
            SELECT reference, reference_status, reference_conflict FROM postings
            ORDER BY lineno
        """, **FIELDS)
        self.assertEqual([
            (None, 'leg-conflict', 'leg'),
            (None, 'leg-conflict', 'leg'),
            ('BankA:TX1:L3', 'ok', None),
        ], rows)

    def test_missing_leg_is_not_fabricated(self):
        input_string = """
            2026-01-01 open Assets:Bank
            2026-01-01 open Expenses:Foo

            2026-01-02 * "Pay"
              institution: "BankA"
              external_id: "TX1"
              Assets:Bank   -10 USD
                leg: "L1"
              Expenses:Foo   10 USD
        """
        rows = _query(input_string, """
            SELECT account, reference, reference_status, reference_leg FROM postings
            ORDER BY lineno
        """, **FIELDS)
        self.assertEqual([
            ('Assets:Bank', 'BankA:TX1:L1', 'ok', 'L1'),
            ('Expenses:Foo', None, 'missing', None),
        ], rows)

    def test_leg_field_on_transaction_is_not_shared(self):
        # A leg number attached only to the transaction does not identify
        # a leg, and must not be copied to every posting.
        input_string = """
            2026-01-01 open Assets:Bank
            2026-01-01 open Expenses:Foo

            2026-01-02 * "Pay"
              institution: "BankA"
              external_id: "TX1"
              leg: "L1"
              Assets:Bank   -10 USD
              Expenses:Foo   10 USD
        """
        rows = _query(input_string, 'SELECT reference FROM postings', **FIELDS)
        self.assertEqual([(None,), (None,)], rows)


def _query_from_file(filename, query, **kwargs):
    entries, errors, options = loader.load_file(filename)
    ctx = beanquery.connect('beancount:', entries=entries, errors=errors, options=options, **kwargs)
    return ctx.execute(query).fetchall()

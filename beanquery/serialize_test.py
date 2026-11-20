__copyright__ = "Copyright (C) 2024  Martin Blais"
__license__ = "GNU GPLv2"

import datetime
import enum
import io
import json
import textwrap
import unittest

from decimal import Decimal

from beancount import loader
from beancount.core.amount import Amount
from beancount.core.inventory import Inventory
from beancount.core.number import D, MISSING
from beancount.core.position import Cost, Position

import beanquery

from beanquery import query_render
from beanquery import serialize
from beanquery.cursor import Column


class Foo(enum.Enum):
    SHORT = 1
    LONG = 2


def roundtrip(value):
    return serialize.deserialize_value(json.loads(json.dumps(serialize.serialize_value(value))))


class TestSerializeValue(unittest.TestCase):

    def test_scalars(self):
        self.assertEqual(roundtrip(''), '')
        self.assertEqual(roundtrip('text'), 'text')
        self.assertIsNone(roundtrip(None))
        self.assertEqual(roundtrip(42), 42)
        self.assertEqual(roundtrip(True), True)
        self.assertEqual(roundtrip(False), False)

    def test_decimal_exact(self):
        # No binary floating point and no display quantization: the
        # full precision and trailing digits must survive.
        for value in ('1.23456789012345', '1.23456789012346', '100.00', '1E+4', '0.00001'):
            result = roundtrip(D(value))
            self.assertIsInstance(result, Decimal)
            self.assertEqual(result, D(value))
        self.assertNotEqual(roundtrip(D('1.23456789012345')), roundtrip(D('1.23456789012346')))
        node = serialize.serialize_value(D('1E+4'))
        self.assertNotIn('E', node['value'])

    def test_date_and_enum(self):
        self.assertEqual(roundtrip(datetime.date(2022, 1, 5)), datetime.date(2022, 1, 5))
        self.assertEqual(roundtrip(Foo.SHORT), 'SHORT')

    def test_set_and_dict(self):
        self.assertEqual(roundtrip({'b', 'a', 'c'}), {'a', 'b', 'c'})
        self.assertEqual(roundtrip({'k': D('1.5')}), {'k': D('1.5')})

    def test_missing_sentinel(self):
        amount = Amount(MISSING, 'USD')
        result = roundtrip(amount)
        self.assertEqual(result.currency, 'USD')
        self.assertIs(result.number, MISSING)

    def test_amount(self):
        self.assertEqual(roundtrip(Amount(D('100.00'), 'USD')), Amount(D('100.00'), 'USD'))

    def test_cost_and_position(self):
        cost = Cost(D('100'), 'USD', datetime.date(2022, 2, 1), 'lot-a')
        position = Position(Amount(D('5'), 'HOOL'), cost)
        result = roundtrip(position)
        self.assertEqual(result, position)
        self.assertEqual(result.cost.date, datetime.date(2022, 2, 1))
        self.assertEqual(result.cost.label, 'lot-a')

    def test_position_without_cost(self):
        position = Position(Amount(D('3'), 'USD'), None)
        result = roundtrip(position)
        self.assertEqual(result, position)
        self.assertIsNone(result.cost)

    def test_inventory_multiple_lots(self):
        inventory = Inventory()
        inventory.add_position(Position(Amount(D('5'), 'HOOL'), Cost(D('100'), 'USD', datetime.date(2022, 2, 1), None)))
        inventory.add_position(Position(Amount(D('2'), 'HOOL'), Cost(D('200'), 'USD', None, 'lot-b')))
        result = roundtrip(inventory)
        self.assertIsInstance(result, Inventory)
        lots = result.get_positions()
        self.assertEqual(len(lots), 2)
        self.assertEqual(sorted(p.units.number for p in lots), [D('2'), D('5')])
        self.assertEqual(sorted(p.cost.number for p in lots), [D('100'), D('200')])
        self.assertTrue(any(p.cost.label == 'lot-b' for p in lots))

    def test_empty_inventory(self):
        result = roundtrip(Inventory())
        self.assertIsInstance(result, Inventory)
        self.assertTrue(result.is_empty())


class TestSerializeResult(unittest.TestCase):

    def test_columns_and_roundtrip(self):
        columns = [Column('a', int), Column('b', str)]
        rows = [(1, 'x'), (2, '')]
        document = serialize.serialize_result(columns, rows)
        self.assertEqual(document['beanquery'], serialize.FORMAT_VERSION)
        self.assertEqual([c['name'] for c in document['columns']], ['a', 'b'])
        self.assertEqual([c['type'] for c in document['columns']], ['int', 'str'])
        self.assertEqual([c['index'] for c in document['columns']], [0, 1])
        names, result_rows = serialize.deserialize_result(json.loads(json.dumps(document)))
        self.assertEqual(names, [('a', 'int'), ('b', 'str')])
        self.assertEqual(result_rows, [(1, 'x'), (2, '')])

    def test_duplicate_column_names_are_positional(self):
        columns = [Column('x', str), Column('x', str), Column('amount', Decimal), Column('amount', Amount)]
        rows = [('', 'src', D('1.5'), Amount(D('7'), 'USD'))]
        document = serialize.serialize_result(columns, rows)
        # Repeated names and ordering are fully represented.
        self.assertEqual([c['name'] for c in document['columns']], ['x', 'x', 'amount', 'amount'])
        self.assertEqual([c['index'] for c in document['columns']], [0, 1, 2, 3])
        _, result_rows = serialize.deserialize_result(json.loads(json.dumps(document)))
        row = result_rows[0]
        self.assertEqual(row[0], '')          # empty string
        self.assertEqual(row[1], 'src')       # distinct value under the same alias
        self.assertEqual(row[2], D('1.5'))
        self.assertEqual(row[3], Amount(D('7'), 'USD'))

    def test_empty_result_keeps_schema(self):
        columns = [Column('x', str), Column('x', str), Column('hold', Inventory)]
        document = serialize.serialize_result(columns, [])
        self.assertEqual(document['rows'], [])
        self.assertEqual([c['name'] for c in document['columns']], ['x', 'x', 'hold'])
        self.assertEqual([c['type'] for c in document['columns']], ['str', 'str', 'inventory'])
        names, rows = serialize.deserialize_result(json.loads(json.dumps(document)))
        self.assertEqual(rows, [])
        self.assertEqual(names, [('x', 'str'), ('x', 'str'), ('hold', 'inventory')])


LEDGER = textwrap.dedent("""
    2022-01-01 open Assets:Broker
    2022-01-01 open Assets:Bank USD

    2022-02-01 * "First" ""
      Assets:Broker  5 HOOL {100.00 USD}
      Assets:Bank

    2022-02-02 * "Second" ""
      Assets:Broker  2 HOOL {200.00 USD}
      Assets:Bank
""")


class TestRealQuery(unittest.TestCase):

    def setUp(self):
        entries, errors, options = loader.load_string(LEDGER)
        self.assertFalse(errors)
        self.context = beanquery.connect('beancount:', entries=entries, errors=errors, options=options)

    def execute_document(self, query):
        cursor = self.context.execute(query)
        document = serialize.serialize_result(cursor.description, cursor.fetchall())
        # The reader only relies on the serialized document itself.
        return json.loads(json.dumps(document))

    def test_duplicate_aliases_and_swap(self):
        query_a = (
            "SELECT narration AS x, payee AS x, "
            "1.00000000000001 AS amount, units(position) AS amount, position "
            "WHERE account = 'Assets:Broker' ORDER BY date")
        query_b = (
            "SELECT payee AS x, narration AS x, "
            "units(position) AS amount, 1.00000000000001 AS amount, position "
            "WHERE account = 'Assets:Broker' ORDER BY date")

        doc_a = self.execute_document(query_a)
        doc_b = self.execute_document(query_b)
        names = [c['name'] for c in doc_a['columns']]
        self.assertEqual(names, ['x', 'x', 'amount', 'amount', 'position'])

        _, rows_a = serialize.deserialize_result(doc_a)
        _, rows_b = serialize.deserialize_result(doc_b)

        # The same positional reader resolves each value after the swap.
        self.assertEqual(rows_a[0][0], '')
        self.assertEqual(rows_a[0][1], 'First')
        self.assertEqual(rows_b[0][0], 'First')
        self.assertEqual(rows_b[0][1], '')
        self.assertIsInstance(rows_a[0][2], Decimal)
        self.assertIsInstance(rows_a[0][3], Amount)
        self.assertIsInstance(rows_b[0][2], Amount)
        self.assertIsInstance(rows_b[0][3], Decimal)
        self.assertEqual(rows_a[0][2], rows_b[0][3])
        self.assertEqual(rows_a[0][4], rows_b[0][4])

    def test_same_currency_distinct_lots(self):
        document = self.execute_document(
            "SELECT sum(position) AS holdings WHERE account = 'Assets:Broker'")
        _, rows = serialize.deserialize_result(document)
        lots = rows[0][0].get_positions()
        self.assertEqual(len(lots), 2)
        self.assertEqual({p.units.currency for p in lots}, {'HOOL'})
        self.assertEqual(sorted(p.cost.number for p in lots), [D('100.00'), D('200.00')])

    def test_render_json(self):
        cursor = self.context.execute(
            "SELECT narration AS x, payee AS x, position WHERE account = 'Assets:Broker' AND year = 1999")
        out = io.StringIO()
        query_render.render_json(cursor.description, cursor.fetchall(), None, out)
        document = json.loads(out.getvalue())
        self.assertEqual(document['rows'], [])
        self.assertEqual([c['name'] for c in document['columns']], ['x', 'x', 'position'])


if __name__ == '__main__':
    unittest.main()

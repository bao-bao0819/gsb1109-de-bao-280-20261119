__copyright__ = "Copyright (C) 2014-2016  Martin Blais"
__license__ = "GNU GPLv2"

import datetime
import enum
import io
import json
import unittest

from decimal import Decimal

from beancount.core import display_context
from beancount.core.amount import Amount, A
from beancount.core.inventory import Inventory, from_string as inventory_from_string
from beancount.core.number import D
from beancount.core.position import Cost, Position, from_string as position_from_string

from beanquery.cursor import Column
from beanquery.query_render_jsonl import render_jsonl


class Foo(enum.Enum):
    SHORT = 1
    LONG = 2


class TestRenderJSONL(unittest.TestCase):

    def setUp(self):
        self.dcontext = display_context.DisplayContext()

    def render(self, columns, rows):
        out = io.StringIO()
        render_jsonl(columns, rows, self.dcontext, out)
        lines = out.getvalue().splitlines()
        return [json.loads(line) for line in lines]

    def test_one_object_per_row(self):
        records = self.render(
            [Column('x', int), Column('y', int)],
            [(1, 2), (3, 4)])
        self.assertEqual(records, [
            {'x': 1, 'y': 2},
            {'x': 3, 'y': 4},
        ])

    def test_column_names_are_preserved_verbatim(self):
        records = self.render(
            [Column('payee', str), Column('some weird name', str)],
            [('a', 'b')])
        self.assertEqual(set(records[0]), {'payee', 'some weird name'})

    def test_scalars(self):
        records = self.render(
            [Column('i', int), Column('b', bool), Column('s', str),
             Column('d', datetime.date), Column('e', Foo), Column('q', Decimal)],
            [(7, True, 'text', datetime.date(2023, 8, 14), Foo.LONG, D('1.2300'))])
        self.assertEqual(records, [{
            'i': 7,
            'b': True,
            's': 'text',
            'd': '2023-08-14',
            'e': 'LONG',
            'q': '1.2300',
        }])

    def test_missing_empty_string_and_empty_inventory_are_distinct(self):
        records = self.render(
            [Column('note', str), Column('bag', Inventory)],
            [('present', Inventory()),
             ('', Inventory()),
             (None, None)])
        # A value, the empty string and a missing value do not collide.
        self.assertEqual(records[0]['note'], 'present')
        self.assertEqual(records[1]['note'], '')
        self.assertIsNone(records[2]['note'])
        # An empty inventory is an empty list, a missing inventory is null.
        self.assertEqual(records[0]['bag'], [])
        self.assertEqual(records[1]['bag'], [])
        self.assertIsNone(records[2]['bag'])

    def test_amount_is_split_into_number_and_currency(self):
        records = self.render(
            [Column('price', Amount)],
            [(A('1.2300 USD'),), (None,)])
        self.assertEqual(records[0], {
            'price.number': '1.2300',
            'price.currency': 'USD',
        })
        # A missing amount still exposes both fields, both null.
        self.assertEqual(records[1], {
            'price.number': None,
            'price.currency': None,
        })

    def test_decimal_round_trips_at_full_precision(self):
        value = D('0.123456789012345678901234567890')
        records = self.render([Column('q', Decimal)], [(value,)])
        rendered = records[0]['q']
        self.assertIsInstance(rendered, str)
        self.assertEqual(Decimal(rendered), value)

    def test_amount_decimal_round_trips_at_full_precision(self):
        value = A('0.123456789012345678901234567890 ETH')
        records = self.render([Column('price', Amount)], [(value,)])
        number = records[0]['price.number']
        self.assertEqual(Decimal(number), value.number)
        self.assertEqual(records[0]['price.currency'], 'ETH')

    def test_position_with_cost(self):
        position = Position(
            Amount(D('5'), 'HOOL'),
            Cost(D('500.23'), 'USD', datetime.date(2023, 8, 14), 'label'))
        records = self.render([Column('pos', Position)], [(position,)])
        self.assertEqual(records[0], {
            'pos': {
                'units': {'number': '5', 'currency': 'HOOL'},
                'cost': {
                    'number': '500.23',
                    'currency': 'USD',
                    'date': '2023-08-14',
                    'label': 'label',
                },
            },
        })

    def test_position_without_cost(self):
        position = Position(Amount(D('3'), 'USD'), None)
        records = self.render([Column('pos', Position)], [(position,), (None,)])
        self.assertEqual(records[0], {
            'pos': {
                'units': {'number': '3', 'currency': 'USD'},
                'cost': None,
            },
        })
        self.assertIsNone(records[1]['pos'])

    def test_position_round_trips(self):
        position = Position(
            Amount(D('5'), 'HOOL'),
            Cost(D('500.23'), 'USD', datetime.date(2023, 8, 14), 'label'))
        records = self.render([Column('pos', Position)], [(position,)])
        encoded = records[0]['pos']
        cost = encoded['cost']
        text = (f"{encoded['units']['number']} {encoded['units']['currency']} "
                f"{{{cost['number']} {cost['currency']}, {cost['date']}, "
                f"\"{cost['label']}\"}}")
        self.assertEqual(position_from_string(text), position)

    def test_cost_without_optional_fields(self):
        records = self.render(
            [Column('cost', Cost)],
            [(Cost(D('100'), 'USD', None, None),)])
        self.assertEqual(records[0], {
            'cost': {
                'number': '100',
                'currency': 'USD',
                'date': None,
                'label': None,
            },
        })

    def test_inventory_is_an_array_of_positions(self):
        inventory = Inventory()
        inventory.add_amount(Amount(D('100'), 'USD'))
        inventory.add_amount(Amount(D('5'), 'HOOL'))
        records = self.render([Column('bag', Inventory)], [(inventory,)])
        self.assertEqual(records[0], {
            'bag': [
                {'units': {'number': '100', 'currency': 'USD'}, 'cost': None},
                {'units': {'number': '5', 'currency': 'HOOL'}, 'cost': None},
            ],
        })

    def test_inventory_round_trips(self):
        inventory = Inventory()
        inventory.add_amount(Amount(D('100'), 'USD'))
        inventory.add_amount(Amount(D('5'), 'HOOL'))
        records = self.render([Column('bag', Inventory)], [(inventory,)])
        text = '(' + ', '.join(
            f"{pos['units']['number']} {pos['units']['currency']}"
            for pos in records[0]['bag']) + ')'
        self.assertEqual(inventory_from_string(text), inventory)

    def test_set_and_dict(self):
        records = self.render(
            [Column('tags', set), Column('meta', dict)],
            [({'b', 'a'}, {'k': 'v'})])
        self.assertEqual(records[0]['tags'], ['a', 'b'])
        self.assertEqual(records[0]['meta'], {'k': 'v'})

    def test_structured_value_behind_generic_object_type(self):
        records = self.render(
            [Column('thing', object)],
            [(A('1.2300 USD'),)])
        self.assertEqual(records[0], {
            'thing': {'number': '1.2300', 'currency': 'USD'},
        })

    def test_containers_encode_elements_recursively(self):
        records = self.render(
            [Column('values', object)],
            [([D('1.5'), D('2.25')],)])
        self.assertEqual(records[0], {'values': ['1.5', '2.25']})

    def test_missing_value_is_null_for_non_amount_columns(self):
        records = self.render(
            [Column('note', str), Column('q', Decimal), Column('bag', Inventory)],
            [(None, None, None)])
        self.assertEqual(records[0], {'note': None, 'q': None, 'bag': None})

    def test_output_is_line_delimited_json(self):
        out = io.StringIO()
        render_jsonl(
            [Column('x', int)], [(1,), (2,)], self.dcontext, out)
        text = out.getvalue()
        # Exactly one JSON object per line, terminated by a newline.
        self.assertEqual(text, '{"x": 1}\n{"x": 2}\n')
        for line in text.splitlines():
            json.loads(line)


if __name__ == '__main__':
    unittest.main()

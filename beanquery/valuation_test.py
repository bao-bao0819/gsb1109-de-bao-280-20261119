__copyright__ = "Copyright (C) 2014-2017  Martin Blais"
__license__ = "GNU GPLv2"

import datetime
import textwrap
import unittest

from decimal import Decimal

from beancount import loader
from beancount.core import amount
from beancount.core import data
from beancount.core import inventory
from beancount.core import position
from beancount.core import prices

from beanquery import valuation


def make_position(units, currency, cost_currency, cost_number=None):
    cost = data.Cost(Decimal(cost_number), cost_currency, None, None) if cost_number else None
    return position.Position(amount.Amount(Decimal(units), currency), cost)


class ValuationTest(unittest.TestCase):

    def setUp(self):
        entries, _, _ = loader.load_string(textwrap.dedent("""
          2026-11-19 price AAA 110 USD
          2026-11-20 price CCC  60 USD
        """))
        self.price_map = prices.build_price_map(entries)
        self.date = datetime.date(2026, 11, 30)

    def test_position_valuation_priced(self):
        result = valuation.position_valuation(
            make_position(10, 'AAA', 'USD', 100), self.price_map, self.date)
        self.assertEqual(result.total, inventory.Inventory(
            [position.Position(amount.Amount(Decimal('1100'), 'USD'), None)]))
        self.assertEqual(result.missing, set())
        self.assertTrue(result.complete)

    def test_position_valuation_missing_price(self):
        result = valuation.position_valuation(
            make_position(7, 'BBB', 'USD', 50), self.price_map, self.date)
        self.assertTrue(result.total.is_empty())
        self.assertEqual(result.missing, {'BBB'})
        self.assertFalse(result.complete)

    def test_position_valuation_without_cost(self):
        # Positions without a cost currency cannot be valued and are
        # passed through, mirroring beancount's get_value().
        result = valuation.position_valuation(
            make_position(5, 'USD', None), self.price_map, self.date)
        self.assertEqual(result.total.get_currency_units('USD'),
                         amount.Amount(Decimal('5'), 'USD'))
        self.assertEqual(result.missing, set())

    def test_position_valuation_respects_date(self):
        # The CCC price is dated 2026-11-20.
        result = valuation.position_valuation(
            make_position(7, 'CCC', 'USD', 50), self.price_map,
            datetime.date(2026, 11, 19))
        self.assertEqual(result.missing, {'CCC'})
        self.assertTrue(result.total.is_empty())

        result = valuation.position_valuation(
            make_position(7, 'CCC', 'USD', 50), self.price_map,
            datetime.date(2026, 11, 20))
        self.assertEqual(result.missing, set())
        self.assertEqual(result.total.get_currency_units('USD'),
                         amount.Amount(Decimal('420'), 'USD'))

    def test_inventory_valuation_mixes_subtotal_and_missing(self):
        inv = inventory.Inventory([
            make_position(10, 'AAA', 'USD', 100),
            make_position(5, 'BBB', 'USD', 20),
            make_position(7, 'CCC', 'USD', 50),
        ])
        result = valuation.inventory_valuation(inv, self.price_map, self.date)
        self.assertEqual(result.total.get_currency_units('USD'),
                         amount.Amount(Decimal('1520'), 'USD'))
        self.assertEqual(result.missing, {'BBB'})
        self.assertFalse(result.complete)

    def test_missing_commodities_are_deduplicated(self):
        inv = inventory.Inventory([
            make_position(7, 'BBB', 'USD', 50),
            make_position(3, 'BBB', 'USD', 55),
        ])
        result = valuation.inventory_valuation(inv, self.price_map, self.date)
        self.assertEqual(result.missing, {'BBB'})

    def test_merge_valuations(self):
        priced = valuation.position_valuation(
            make_position(10, 'AAA', 'USD', 100), self.price_map, self.date)
        missing = valuation.position_valuation(
            make_position(7, 'BBB', 'USD', 50), self.price_map, self.date)
        merged = valuation.merge_valuations(valuation.Valuation(), priced)
        valuation.merge_valuations(merged, missing)
        self.assertEqual(merged.total.get_currency_units('USD'),
                         amount.Amount(Decimal('1100'), 'USD'))
        self.assertEqual(merged.missing, {'BBB'})
        self.assertFalse(merged.complete)

    def test_merge_valuation_ignores_none(self):
        result = valuation.merge_valuations(valuation.Valuation(), None)
        self.assertTrue(result.complete)


if __name__ == '__main__':
    unittest.main()

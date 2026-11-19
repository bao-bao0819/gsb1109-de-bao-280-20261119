"""Valuation with an explicit list of positions missing a market price.

The :class:`Valuation` object pairs the market value that could be
computed from the available prices with the set of commodities for
which no price could be found.  This allows valuation queries to return
a partial subtotal together with the list of commodities that still
need a closing price, instead of silently dropping the unpriced
positions.
"""
__copyright__ = "Copyright (C) 2014-2017  Martin Blais"
__license__ = "GNU GPLv2"

from beancount.core import amount
from beancount.core import inventory
from beancount.core import position
from beancount.core import prices


class Valuation:
    """Market value of a set of positions and its price coverage.

    Attributes:
      total: An Inventory containing the amounts that could be valued
        at market price. Positions that could not be valued are not
        included here.
      missing: A set of commodity names for which no market price was
        available. The valuation is complete only when this set is
        empty.
    """

    __slots__ = ('total', 'missing')

    def __init__(self, total=None, missing=None):
        self.total = total if total is not None else inventory.Inventory()
        self.missing = set(missing) if missing is not None else set()

    @property
    def complete(self):
        """Whether every position could be valued at market price."""
        return not self.missing

    def __eq__(self, other):
        if not isinstance(other, Valuation):
            return NotImplemented
        return self.total == other.total and self.missing == other.missing

    def __repr__(self):
        return f'{type(self).__name__}(total={self.total!r}, missing={sorted(self.missing)!r})'


def _position_value(pos, price_map, date):
    """Value a position and report whether a price had to be looked up.

    Returns a pair ``(amount, priced)``. ``priced`` is False when the
    position has no cost currency and is passed through unchanged,
    mirroring beancount's ``get_value()``. Otherwise ``amount`` is the
    market value or None if no price is available.
    """
    units = pos.units
    cost = pos.cost

    # Mirror beancount's get_value(): without a cost or a posting price
    # there is no currency to convert the units to, thus nothing is
    # missing and the units are passed through.
    value_currency = cost.currency if isinstance(cost, position.Cost) else None
    if value_currency is None:
        return units, False

    _, price_number = prices.get_price(price_map, (units.currency, value_currency), date)
    if price_number is None:
        return None, True
    return amount.Amount(units.number * price_number, value_currency), True


def position_valuation(pos, price_map, date=None):
    """Value a single position, reporting a missing price if any."""
    result = Valuation()
    value, priced = _position_value(pos, price_map, date)
    if priced and value is None:
        result.missing.add(pos.units.currency)
    elif value is not None:
        result.total.add_amount(value)
    return result


def inventory_valuation(inv, price_map, date=None):
    """Value an inventory, collecting the commodities missing a price.

    Positions sharing a currency for which a price is available are
    aggregated in the subtotal while the unpriced currencies are
    reported separately. Each call only uses the prices relevant to the
    given inventory and date, so distinct groups never borrow a price
    found for another group.
    """
    result = Valuation()
    for pos in inv:
        value, priced = _position_value(pos, price_map, date)
        if priced and value is None:
            result.missing.add(pos.units.currency)
        elif value is not None:
            result.total.add_amount(value)
    return result


def merge_valuations(result, other):
    """Aggregate two valuations, as used by the ``sum()`` aggregate."""
    if other is None:
        return result
    result.total.add_inventory(other.total)
    result.missing.update(other.missing)
    return result

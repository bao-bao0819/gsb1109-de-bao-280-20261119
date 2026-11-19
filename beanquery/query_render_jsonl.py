"""Machine-readable line-delimited JSON (JSONL) rendering.

This renderer emits one JSON object per result row, with one field per
query column.  It is intended for feeding query results to downstream
programs rather than for human consumption and addresses the
ambiguities of the text and CSV renderers:

* Amount values are split into separate ``number`` and ``currency``
  fields instead of being joined into a single display string.
* Numbers, including high precision :class:`decimal.Decimal` values,
  are emitted verbatim using their canonical string representation,
  which round-trips exactly through :class:`decimal.Decimal`.
* A missing value (``NULL``), the empty string and an empty inventory
  are represented distinctly as JSON ``null``, ``""`` and ``[]``
  respectively so consumers do not have to guess which is which.
* Positions and inventories are expanded into structured objects and
  arrays instead of being squeezed into a single display cell.

The ``dcontext`` argument is accepted for renderer signature
compatibility but intentionally unused: numbers are never quantized so
that their full precision is preserved.
"""
import datetime
import enum
import json

from decimal import Decimal

from beancount.core import amount
from beancount.core import inventory
from beancount.core import position


def _encode_amount(value):
    return {
        'number': str(value.number),
        'currency': value.currency,
    }


def _encode_cost(value):
    return {
        'number': None if value.number is None else str(value.number),
        'currency': value.currency,
        'date': None if value.date is None else value.date.isoformat(),
        'label': value.label,
    }


def _encode_position(value):
    return {
        'units': _encode_amount(value.units),
        'cost': None if value.cost is None else _encode_cost(value.cost),
    }


def _encode_inventory(value):
    return [_encode_position(pos) for pos in value.get_positions()]


def _encode(value):
    """Encode a value as a JSON-compatible object, recursively.

    Structured beancount types are expanded regardless of the declared
    column type so the encoding stays correct even when they appear
    nested inside a container or behind the generic ``object`` type.
    """
    if value is None:
        return None
    if isinstance(value, amount.Amount):
        return _encode_amount(value)
    if isinstance(value, position.Position):
        return _encode_position(value)
    if isinstance(value, position.Cost):
        return _encode_cost(value)
    if isinstance(value, inventory.Inventory):
        return _encode_inventory(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, (set, frozenset)):
        return [_encode(item) for item in sorted(value)]
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


def render_jsonl(columns, rows, dcontext, file, **kwargs):
    """Render query results as one JSON object per line (JSONL).

    Args:
      columns: A list of :class:`beanquery.cursor.Column` describing the
        table columns. The column names are used verbatim as JSON field
        names.
      rows: The data to render, as an iterable of tuples.
      dcontext: A DisplayContext. Accepted for signature compatibility and
        unused, as numbers are rendered at full precision without
        quantization.
      file: A text file object the JSON lines are written to.
    """
    for row in rows:
        record = {}
        for column, value in zip(columns, row):
            name = column.name
            if column.datatype is amount.Amount:
                # Split the amount so the number and the currency never
                # have to be parsed back out of a single string. A
                # missing amount still exposes both fields, both null.
                record[name + '.number'] = None if value is None else str(value.number)
                record[name + '.currency'] = None if value is None else value.currency
            else:
                record[name] = _encode(value)

        json.dump(record, file, ensure_ascii=False)
        file.write('\n')

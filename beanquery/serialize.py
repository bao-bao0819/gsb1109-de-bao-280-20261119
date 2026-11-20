"""Machine-readable serialization of query results.

This module converts the native Python values returned by query
execution into structures that can be encoded as JSON and converted
back without relying on display formatting.  In particular:

* values are identified by their position, so columns sharing the same
  name can be told apart;
* exact :class:`decimal.Decimal` values are serialized as strings and
  never go through binary floating point or display quantization;
* the empty string, a missing value (``NULL``) and the absence of a
  position are represented by distinct encodings;
* beancount amounts, costs, positions and inventories retain their
  full structure, so distinct cost lots are never merged into a single
  string or number.

"""
import datetime
import enum

from decimal import Decimal

from beancount.core import amount
from beancount.core import inventory
from beancount.core import position
from beancount.core.number import MISSING

from . import types


# Version of the exchange format produced by this module.
FORMAT_VERSION = '1.0'


def column_type(datatype):
    """Return the stable type name documenting a column."""
    return types.name(datatype)


def serialize_value(value):
    """Convert a native query value into a JSON-compatible structure."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        # Format without an exponent so that the exact digits survive
        # unchanged and the string round-trips through Decimal().
        return {'dtype': 'decimal', 'value': format(value, 'f')}
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, datetime.date):
        return {'dtype': 'date', 'value': value.isoformat()}
    if isinstance(value, enum.Enum):
        return {'dtype': 'enum', 'name': value.name}
    if isinstance(value, amount.Amount):
        return {'dtype': 'amount', 'number': serialize_value(value.number), 'currency': value.currency}
    if isinstance(value, position.Cost):
        return {
            'dtype': 'cost',
            'number': serialize_value(value.number),
            'currency': value.currency,
            'date': serialize_value(value.date),
            'label': serialize_value(value.label),
        }
    if isinstance(value, position.Position):
        return {'dtype': 'position', 'units': serialize_value(value.units), 'cost': serialize_value(value.cost)}
    if isinstance(value, inventory.Inventory):
        return {
            'dtype': 'inventory',
            'positions': [serialize_value(pos) for pos in value.get_positions()],
        }
    if isinstance(value, (set, frozenset)):
        return {'dtype': 'set', 'values': [serialize_value(item) for item in sorted(value, key=_sortkey)]}
    if isinstance(value, dict):
        return {'dtype': 'dict', 'value': {str(key): serialize_value(item) for key, item in value.items()}}
    if value is MISSING:
        return {'dtype': 'missing'}
    return {'dtype': 'object', 'repr': str(value)}


def _sortkey(value):
    # Sorting sets requires homogeneous, orderable members.  This is
    # the case for the sets produced by BQL (sets of strings).  Fall
    # back to the string representation for anything else.
    try:
        return (0, value)
    except TypeError:
        return (1, str(value))


def serialize_row(row):
    """Convert a result row into a JSON-compatible list."""
    return [serialize_value(value) for value in row]


def serialize_result(columns, rows):
    """Build the self-describing document for a query result."""
    return {
        'beanquery': FORMAT_VERSION,
        'columns': [
            {'name': column.name, 'type': column_type(column.datatype), 'index': index}
            for index, column in enumerate(columns)
        ],
        'rows': [serialize_row(row) for row in rows],
    }


def deserialize_value(node):
    """Rebuild a native query value from its serialized form."""
    if node is None:
        return None
    if isinstance(node, bool):
        return node
    if isinstance(node, int):
        return node
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return [deserialize_value(item) for item in node]

    dtype = node.get('dtype')
    if dtype == 'decimal':
        return Decimal(node['value'])
    if dtype == 'date':
        return datetime.date.fromisoformat(node['value'])
    if dtype == 'enum':
        # The concrete enum class is not known to the reader.  Return
        # the member name, which uniquely identifies the value within
        # its column.
        return node['name']
    if dtype == 'missing':
        return MISSING
    if dtype == 'amount':
        return amount.Amount(deserialize_value(node['number']), node['currency'])
    if dtype == 'cost':
        return position.Cost(
            deserialize_value(node['number']),
            node['currency'],
            deserialize_value(node['date']),
            deserialize_value(node['label']),
        )
    if dtype == 'position':
        return position.Position(deserialize_value(node['units']), deserialize_value(node['cost']))
    if dtype == 'inventory':
        result = inventory.Inventory()
        for item in node['positions']:
            result.add_position(deserialize_value(item))
        return result
    if dtype == 'set':
        return {deserialize_value(item) for item in node['values']}
    if dtype == 'dict':
        return {key: deserialize_value(item) for key, item in node['value'].items()}
    if dtype == 'object':
        return node['repr']
    raise ValueError(f'unknown serialized value type: {dtype!r}')


def deserialize_result(document):
    """Rebuild columns and rows from a serialized result document.

    Returns a tuple ``(columns, rows)`` where ``columns`` is a list of
    ``(name, type)`` pairs and ``rows`` is a list of tuples holding the
    reconstructed native values.  Only the document itself is required
    to perform the conversion.
    """
    columns = [(column['name'], column['type']) for column in document['columns']]
    rows = [tuple(deserialize_value(cell) for cell in row) for row in document['rows']]
    return columns, rows

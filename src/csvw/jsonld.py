"""
Functionality to transform CSVW row values to RDF.
"""
import re
import json
import math
from typing import TYPE_CHECKING, Any, Union
import decimal
import pathlib
import datetime
import collections
from collections.abc import Iterable
import dataclasses

from rdflib import Graph, URIRef, Literal
from rfc3986 import URIReference
from isodate.duration import Duration

from .utils import is_url

if TYPE_CHECKING:
    from .metadata import Table, Column  # pragma: no cover

__all__ = ['group_triples', 'to_json', 'Triple', 'format_value', 'expand_value_url']


def format_value(value: Any, col: 'Column') -> str:  # pylint: disable=R0911
    """
    Format values as JSON-LD literals.
    """
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        res = value.isoformat()
        if col and col.datatype.base == 'time':
            res = res.split('T')[-1]
        if col and col.datatype.base == 'date':
            res = re.sub('T[0-9.:]+', '', res)
        if isinstance(value, (datetime.datetime, datetime.time)):
            stamp, _, milliseconds = res.partition('.')
            return f'{stamp}.{milliseconds.rstrip("0")}' if milliseconds \
                else stamp.replace('+00:00', 'Z')
        return res  # pragma: no cover
    if isinstance(value, datetime.timedelta):
        return col.datatype.formatted(value)
    if isinstance(value, Duration):
        return col.datatype.formatted(value)
    if isinstance(value, URIReference):
        return value.unsplit()
    if isinstance(value, bytes):
        return col.datatype.formatted(value)
    if isinstance(value, pathlib.Path):
        return str(value)
    if isinstance(value, decimal.Decimal):
        value = float(value)
    if isinstance(value, float):
        return 'NaN' if math.isnan(value) else (
            f"{'-' if value < 0 else ''}INF" if math.isinf(value) else value)
    return value


def expand_value_url(  # pylint: disable=R0913,R0917
        table: 'Table',
        value_url,
        row: collections.OrderedDict[str, Any],
        val: Any,
        rownum: int,
        name: str = None,
        per_item: bool = False,
        qname: bool = False,
) -> Any:
    """
    Expand a `valueUrl` template for a cell value.

    If `per_item` is true and `val` is a list (i.e. the column is list-valued), the template is
    expanded once per non-null item, with the variable for the column bound to that item. All
    other variables are taken from `row`, and each result is resolved against the table's base.
    """
    if per_item and isinstance(val, list):
        res = []
        for item in val:
            if item is None or item == '':
                continue
            item_row = collections.OrderedDict(row)
            if name is not None:
                item_row[name] = item
            res.append(
                table.expand(value_url, item_row, _row=rownum, _name=name, qname=qname))
        return res
    return table.expand(value_url, row, _row=rownum, _name=name, qname=qname)


@dataclasses.dataclass
class Triple:
    """
    A table cell's data as RDF triple.
    """
    about: str
    property: str
    value: str

    def as_rdflib_triple(self) -> tuple[URIRef, URIRef, Union[URIRef, Literal]]:
        """The triple suitable for inclusion in an rdflib.Graph."""
        return (
            URIRef(self.about),
            URIRef(self.property),
            URIRef(self.value) if is_url(self.value) else Literal(self.value))

    @classmethod
    def from_col(  # pylint: disable=R0913,R0917
            cls,
            table: 'Table',
            col: 'Column',
            row: collections.OrderedDict[str, Any],
            prop: str,
            val: Any,
            rownum: int,
            per_item: bool = False,
    ) -> Union['Triple', list['Triple']]:
        """
        Instantiate a triple from the data (and metadata) of a column value.

        If `per_item` is true and the column is list-valued, a list of triples - one per
        non-null value - is returned.
        """
        _name = col.header if col else None

        propertyUrl = col.propertyUrl if col \
            else table.inherit('propertyUrl')  # pylint: disable=C0103
        if propertyUrl:
            prop = table.expand(propertyUrl, row, _row=rownum, _name=_name, qname=True)

        is_type = prop == 'rdf:type'
        valueUrl = col.valueUrl if col else table.inherit('valueUrl')  # pylint: disable=C0103
        s = None
        aboutUrl = col.aboutUrl if col else None  # pylint: disable=invalid-name
        if aboutUrl:
            s = table.expand(aboutUrl, row, _row=rownum, _name=_name) or s

        if valueUrl:
            # The variable used for the current cell in a URI template is the column's header.
            vals = expand_value_url(
                table,
                valueUrl,
                row,
                val,
                rownum,
                name=_name or (prop if not col else None),
                per_item=per_item,
                qname=is_type,
            )
            if isinstance(vals, list):
                return [
                    cls(about=s, property=prop, value=format_value(v, col)) for v in vals]
            return cls(about=s, property=prop, value=format_value(vals, col))
        return cls(about=s, property=prop, value=format_value(val, col))


def frame(data: list[dict]) -> list:
    """
    Inline referenced items to force a deterministic graph layout.

    .. see:: https://w3c.github.io/json-ld-framing/#introduction
    """
    items, refs = collections.OrderedDict(), {}
    for item in data:
        itemid = item.get('@id')
        if itemid:
            items[itemid] = item
        for vs in item.values():
            for v in [vs] if not isinstance(vs, list) else vs:
                if isinstance(v, dict):
                    refid = v.get('@id')
                    if refid:
                        refs.setdefault(refid, (v, []))[1].append(item)
    for ref, subjects in refs.values():
        if len(subjects) == 1 and ref['@id'] in items:
            ref.update(items.pop(ref['@id']))
    return list(items.values())


def to_json(obj, flatten_list=False):
    """
    Simplify JSON-LD data by refactoring trivial objects.
    """
    if isinstance(obj, dict):
        if '@value' in obj:
            obj = obj['@value']
        if len(obj) == 1 and '@id' in obj:
            obj = obj['@id']
    if isinstance(obj, dict):
        return {
            '@type' if k == 'rdf:type' else k: to_json(v, flatten_list=flatten_list)
            for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) == 1 and flatten_list:
            return to_json(obj[0], flatten_list=flatten_list)
        return [to_json(v, flatten_list=flatten_list) for v in obj]
    return obj


def _merged_triples(triples: Iterable[Triple]) -> list[Triple]:
    merged = []
    for triple in triples:
        if isinstance(triple.value, list):
            # We check, whether a list-valued triple for the same property is already present.
            for t in merged:
                if t.property == triple.property and isinstance(t.value, list):
                    t.value.extend(triple.value)
                    break
            else:
                merged.append(triple)
        else:
            merged.append(triple)
    return merged


def _extract_grouped_triples(triples) -> tuple[collections.OrderedDict[str, Triple], list[Triple]]:
    """Return triples grouped by property and purge these from `triples`."""
    grouped, rem = collections.OrderedDict(), []
    for triple in triples:
        if triple.about is None and triple.property == '@id':
            grouped[triple.property] = triple.value
            continue
        if not triple.about:
            # For test48
            if triple.property in grouped:
                if not isinstance(grouped[triple.property], list):
                    grouped[triple.property] = [grouped[triple.property]]
                grouped[triple.property].append(triple.value)
            else:
                grouped[triple.property] = triple.value
            continue
        rem.append(triple)
    return grouped, rem


def group_triples(triples: Iterable[Triple]) -> list[dict]:
    """
    Group and frame triples into a `list` of JSON objects.
    """
    grouped, triples = _extract_grouped_triples(_merged_triples(triples))
    if not triples:  # All grouped.
        return [grouped]

    g = Graph()
    for triple in triples:
        g.add(triple.as_rdflib_triple())
    if '@id' in grouped:
        for prop, val in grouped.items():
            if prop != '@id':
                g.add(Triple(about=grouped['@id'], property=prop, value=val).as_rdflib_triple())

    res = g.serialize(format='json-ld')
    # Frame and simplify the resulting objects, augment with list index:
    res = [(i, to_json(v, flatten_list=True)) for i, v in enumerate(frame(json.loads(res)))]
    # Sort the objects making sure the one with the row's aboutUrl as @id comes first:
    res = [k[1] for k in sorted(
        res, key=lambda o: -1 if o[1].get('@id') == grouped.get('@id') else o[0])]
    # If there's no aboutUrl for the row and we have only one object from triples, we just merge
    # the properties into a single object.
    if grouped and ('@id' not in grouped) and len(res) == 1:
        grouped.update(res[0])
        return [grouped]

    return res

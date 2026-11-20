#  Copyright 2013 Lars Butler & individual contributors
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
import decimal
import io
import itertools
import tokenize

import geomet
from geomet import util

"""
Well-Known Text (WKT) is a text-based format for representing geometric objects
in a human-readable way with standard text strings.

Examples:

A point in WKT looks like this:
POINT (30 10)

A line string (a connected sequence of points):
LINESTRING (30 10, 10 30, 40 40)

A polygon:
POLYGON ((30 10, 40 40, 20 40, 10 20, 30 10))
"""

INVALID_WKT_FMT = 'Invalid WKT: `%s`'


def dump(obj, dest_file, decimals=16, exact=False, use_float=None):
    """
    Dump GeoJSON-like `dict` to WKT and write it to the `dest_file`.

    :param dict obj:
        A GeoJSON-like dictionary. It must at least the keys 'type' and
        'coordinates'.
    :param dest_file:
        Open and writable file-like object.
    :param bool exact:
        Accepted for symmetry with :func:`loads`; coordinate formatting
        already keeps integral values exact.
    :param bool use_float:
        Backwards-compatible alias for ``exact=not use_float``.
    """
    dest_file.write(dumps(
        obj,
        decimals=decimals,
        exact=exact,
        use_float=use_float,
    ))


def load(source_file, exact=False, use_float=None):
    """
    Load a GeoJSON `dict` object from a ``source_file`` containing WKT.

    :param source_file:
        Open and readable file-like object.
    :param bool exact:
        If `True`, coordinates are parsed without losing digits.  Integral
        values are returned as :class:`int` and other values as
        :class:`decimal.Decimal`.  The default preserves the historical
        `float` behaviour.
    :param bool use_float:
        Backwards-compatible alias for ``exact=not use_float``.

    :returns:
        A GeoJSON `dict` representing the geometry read from the file.
    """
    return loads(
        source_file.read(),
        exact=_exact_flag(exact, use_float),
    )


def load_exact(source_file):
    """Load WKT from ``source_file`` without narrowing coordinates to float."""
    return load(source_file, exact=True)


def loads_exact(string):
    """Parse WKT ``string`` without narrowing coordinates to float."""
    return loads(string, exact=True)


def dumps_exact(obj, decimals=16):
    """
    Dump a GeoJSON-like `dict` to WKT, keeping integer/decimal coordinates
    exact at the requested number of places.
    """
    return dumps(obj, decimals=decimals, exact=True)


def dumps(obj, decimals=16, exact=False, use_float=None):
    """
    Dump a GeoJSON-like `dict` to a WKT string.

    :param int decimals:
        Number of digits to display after the decimal point.
    :param bool exact:
        Accepted for symmetry with :func:`loads`; coordinate formatting
        already keeps integral values exact.
    :param bool use_float:
        Backwards-compatible alias for ``exact=not use_float``.
    """
    if use_float is not None:
        exact = _exact_flag(exact, use_float)
    try:
        geom_type = obj['type']
        exporter = _dumps_registry.get(geom_type)

        if exporter is None:
            _unsupported_geom_type(geom_type)

        # Check for empty cases
        if geom_type == 'GeometryCollection':
            if len(obj['geometries']) == 0:
                return 'GEOMETRYCOLLECTION EMPTY'
        else:
            # Geom has no coordinate values at all, and must be empty.
            if len(list(util.flatten_multi_dim(obj['coordinates']))) == 0:
                return '%s EMPTY' % geom_type.upper()
    except KeyError:
        raise geomet.InvalidGeoJSONException('Invalid GeoJSON: %s' % obj)

    result = exporter(obj, decimals)
    # Try to get the SRID from `meta.srid`
    meta_srid = obj.get('meta', {}).get('srid')
    # Also try to get it from `crs.properties.name`:
    crs_srid = obj.get('crs', {}).get('properties', {}).get('name')
    if crs_srid is not None:
        # Shave off the EPSG prefix to give us the SRID:
        crs_srid = crs_srid.replace('EPSG', '')

    if (meta_srid is not None and
            crs_srid is not None and
            str(meta_srid) != str(crs_srid)):
        raise ValueError(
            'Ambiguous CRS/SRID values: %s and %s' % (meta_srid, crs_srid)
        )
    srid = meta_srid or crs_srid

    # TODO: add tests for CRS input
    if srid is not None:
        # Prepend the SRID
        result = 'SRID=%s;%s' % (srid, result)
    return result


def _assert_next_token(sequence, expected):
    next_token = next(sequence)
    if not next_token == expected:
        raise ValueError(
            'Expected "%s" but found "%s"' % (expected, next_token)
        )


def loads(string, exact=False, use_float=None):
    """
    Construct a GeoJSON `dict` from WKT (`string`).

    :param bool exact:
        If `True`, coordinates are parsed without losing digits.  Integral
        values are returned as :class:`int` and other values as
        :class:`decimal.Decimal`, so values beyond the precision of a
        `float` keep their exact decimal representation.  The default
        preserves the historical `float` behaviour.
    :param bool use_float:
        Backwards-compatible alias for ``exact=not use_float``; when it is
        given it takes precedence over `exact`.
    """
    exact = _exact_flag(exact, use_float)
    tokens = _tokenize_wkt(string)
    geom_type_or_srid = next(tokens)
    srid = None
    geom_type = geom_type_or_srid
    if geom_type_or_srid == 'SRID':
        # The geometry WKT contains an SRID header.
        _assert_next_token(tokens, '=')
        srid = int(next(tokens))
        _assert_next_token(tokens, ';')
        # We expected the geometry type to be next:
        geom_type = next(tokens)
    else:
        geom_type = geom_type_or_srid

    importer = _loads_registry.get(geom_type)

    if importer is None:
        _unsupported_geom_type(geom_type)

    peek = next(tokens)
    if peek == 'EMPTY':
        if geom_type == 'GEOMETRYCOLLECTION':
            return dict(type='GeometryCollection', geometries=[])
        else:
            return dict(type=_type_map_caps_to_mixed[geom_type],
                        coordinates=[])

    # Put the peeked element back on the head of the token generator
    tokens = itertools.chain([peek], tokens)
    result = importer(tokens, string, exact=exact)
    if srid is not None:
        result['meta'] = dict(srid=srid)
    return result


def _tokenize_wkt(string):
    """
    Since the tokenizer treats "-" and numeric strings as separate values,
    combine them and yield them as a single token. This utility encapsulates
    parsing of negative numeric values from WKT can be used generically in all
    parsers.
    """
    sio = io.StringIO(string)
    # NOTE: This is not the intended purpose of `tokenize`, but it works.
    tokens = (x[1] for x in tokenize.generate_tokens(sio.readline))
    negative = False
    for t in tokens:
        if t == '-':
            negative = True
            continue
        elif t == '':
            # Ignore empty string tokens.
            # This can happen in python3.12, seemingly due to
            # https://peps.python.org/pep-0701/#changes-to-the-tokenize-module
            continue
        else:
            if negative:
                yield '-%s' % t
            else:
                yield t
            negative = False


def _unsupported_geom_type(geom_type):
    raise ValueError("Unsupported geometry type '%s'" % geom_type)


def _exact_flag(exact, use_float):
    """Resolve the historical `use_float` alias against `exact`."""
    if use_float is None:
        return exact
    return not use_float


def _round_and_pad(value, decimals):
    """
    Round the input value to `decimals` places, and pad with 0's
    if the resulting value is less than `decimals`.

    :param value:
        The value to round
    :param decimals:
        Number of decimals places which should be displayed after the rounding.
    :return:
        str of the rounded value
    """
    if decimals == 0:
        # if get a `decimals` value of 0, we want to return an int.
        if isinstance(value, decimal.Decimal):
            return repr(int(_quantize_decimal(value, decimals)))
        return repr(int(round(value, decimals)))

    if isinstance(value, decimal.Decimal):
        return _format_decimal(value, decimals)

    if isinstance(value, int):
        # Integers are formatted exactly instead of going through a float,
        # which would collapse neighbours above 2**53.
        return _format_decimal(decimal.Decimal(value), decimals)

    rounded = round(value, decimals)

    if 'e' in repr(rounded):
        rounded = format(rounded, '.{}f'.format(decimals))
    else:
        rounded = repr(rounded)
    rounded += '0' * (decimals - len(rounded.split('.')[1]))
    return rounded


def _quantize_decimal(value, decimals):
    """Round a Decimal to `decimals` places, half-even, without context loss.
    """
    with decimal.localcontext() as ctx:
        digits = len(value.as_tuple().digits)
        ctx.prec = max(ctx.prec, value.adjusted() + decimals + 2,
                       digits + decimals + 2)
        ctx.rounding = decimal.ROUND_HALF_EVEN
        return value.quantize(decimal.Decimal(1).scaleb(-decimals))


def _format_decimal(value, decimals):
    """
    Render a :class:`decimal.Decimal` with exactly `decimals` places.

    The value is rounded using the current decimal context (half-even by
    default), matching :func:`round` for floats.  Trailing zeros are kept so
    that the output has the same shape as :func:`_round_and_pad`.
    """
    return format(_quantize_decimal(value, decimals),
                  '.{}f'.format(decimals))


def _parse_coordinate(token, exact):
    """
    Convert a WKT numeric token to a Python number.

    With ``exact=True``, values whose fractional part is only padding zeros
    (such as ``12.0000``) are returned as :class:`int`, so counts larger
    than 2**53 survive.  Negative zero keeps its sign.  A value is narrowed
    to a `float` only when the float's decimal form is the same number, so
    ordinary coordinates keep the historical representation and its
    rounding behaviour, while a large value with a non-zero fractional part
    stays a :class:`decimal.Decimal` instead of being mistaken for a padded
    integer.  With ``exact=False`` the historical ``float`` conversion is
    used for every value.
    """
    if not exact:
        return float(token)

    value = decimal.Decimal(token)
    if not value.is_finite():
        return float(token)
    if value.is_zero() and value.is_signed():
        # Keep the sign of a negative zero; int() would discard it.
        return -0.0
    if value == value.to_integral_value():
        return int(value)

    as_float = float(token)
    if decimal.Decimal(repr(as_float)) == value:
        return as_float
    return value


def _dump_point(obj, decimals):
    """
    Dump a GeoJSON-like Point object to WKT.

    :param dict obj:
        A GeoJSON-like `dict` representing a Point.
    :param int decimals:
        int which indicates the number of digits to display after the
        decimal point when formatting coordinates.

    :returns:
        WKT representation of the input GeoJSON Point ``obj``.
    """
    coords = obj['coordinates']

    if not coords:
        fmt = 'EMPTY'
    else:
        fmt = '(%s)' % (
            ' '.join(_round_and_pad(c, decimals) for c in coords)
        )

    return 'POINT %s' % fmt


def _dump_linestring(obj, decimals):
    """
    Dump a GeoJSON-like LineString object to WKT.

    Input parameters and return value are the LINESTRING equivalent to
    :func:`_dump_point`.
    """
    coords = obj['coordinates']

    if not coords:
        fmt = 'EMPTY'
    else:
        fmt = '(%s)' % (
            ', '.join(
                ' '.join(_round_and_pad(c, decimals) for c in pt)
                for pt in coords
            )
        )

    return 'LINESTRING %s' % fmt


def _dump_polygon(obj, decimals):
    """
    Dump a GeoJSON-like Polygon object to WKT.

    Input parameters and return value are the POLYGON equivalent to
    :func:`_dump_point`.
    """
    coords = obj['coordinates']

    if not coords:
        fmt = 'EMPTY'
    else:
        rings = (', '.join(' '.join(_round_and_pad(c, decimals)
                                    for c in pt) for pt in ring)
                 for ring in coords)

        fmt = '(%s)' % ', '.join('(%s)' % r for r in rings)

    return 'POLYGON %s' % fmt


def _dump_multipoint(obj, decimals):
    """
    Dump a GeoJSON-like MultiPoint object to WKT.

    Input parameters and return value are the MULTIPOINT equivalent to
    :func:`_dump_point`.
    """
    coords = obj['coordinates']

    if not coords:
        fmt = "EMPTY"
    else:
        points = (' '.join(_round_and_pad(c, decimals)
                           for c in pt) for pt in coords)
        # Add parens around each point.
        fmt = '(%s)' % ', '.join('(%s)' % pt for pt in points)

    return 'MULTIPOINT %s' % fmt


def _dump_multilinestring(obj, decimals):
    """
    Dump a GeoJSON-like MultiLineString object to WKT.

    Input parameters and return value are the MULTILINESTRING equivalent to
    :func:`_dump_point`.
    """
    coords = obj['coordinates']

    if not coords:
        fmt = 'EMPTY'
    else:
        linestrs = (
            '(%s)' %
            ', '.join(
                ' '.join(
                    _round_and_pad(
                        c,
                        decimals) for c in pt
                ) for pt in linestr) for linestr in coords)

        fmt = '(%s)' % ', '.join(ls for ls in linestrs)

    return 'MULTILINESTRING %s' % fmt


def _dump_multipolygon(obj, decimals):
    """
    Dump a GeoJSON-like MultiPolygon object to WKT.

    Input parameters and return value are the MULTIPOLYGON equivalent to
    :func:`_dump_point`.
    """
    coords = obj['coordinates']
    if not coords:
        fmt = 'EMPTY'
    else:
        fmt = '(%s)' % (
            # join the polygons in the multipolygon
            ', '.join(
                # join the rings in a polygon,
                # and wrap in parens
                '(%s)' % ', '.join(
                    # join the points in a ring,
                    # and wrap in parens
                    '(%s)' % ', '.join(
                        # join coordinate values of a vertex
                        ' '.join(_round_and_pad(c, decimals) for c in pt)
                        for pt in ring)
                    for ring in poly)
                for poly in coords)
        )

    return 'MULTIPOLYGON %s' % fmt


def _dump_geometrycollection(obj, decimals):
    """
    Dump a GeoJSON-like GeometryCollection object to WKT.

    Input parameters and return value are the GEOMETRYCOLLECTION equivalent to
    :func:`_dump_point`.

    The WKT conversions for each geometry in the collection are delegated to
    their respective functions.
    """
    geoms = obj['geometries']
    if not geoms:
        fmt = 'EMPTY'
    else:
        geoms_wkt = []
        for geom in geoms:
            geom_type = geom['type']
            geoms_wkt.append(_dumps_registry.get(geom_type)(geom, decimals))
        fmt = '(%s)' % ','.join(geoms_wkt)
    return 'GEOMETRYCOLLECTION %s' % fmt


def _load_point(tokens, string, exact=False):
    """
    :param tokens:
        A generator of string tokens for the input WKT, beginning just after
        the geometry type. The geometry type is consumed before we get to
        here. For example, if :func:`loads` is called with the input
        'POINT(0.0 1.0)', ``tokens`` would generate the following values:

        .. code-block:: python
            ['(', '0.0', '1.0', ')']
    :param str string:
        The original WKT string.

    :returns:
        A GeoJSON `dict` Point representation of the WKT ``string``.
    """
    next_token = next(tokens)

    if next_token == 'EMPTY':
        return dict(type='Point', coordinates=[])
    elif not next_token == '(':
        raise ValueError(INVALID_WKT_FMT % string)

    coords = []
    try:
        for t in tokens:
            if t == ')':
                break
            else:
                coords.append(_parse_coordinate(t, exact))
    except tokenize.TokenError:
        raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='Point', coordinates=coords)


def _load_linestring(tokens, string, exact=False):
    """
    Has similar inputs and return value to to :func:`_load_point`, except is
    for handling LINESTRING geometry.

    :returns:
        A GeoJSON `dict` LineString representation of the WKT ``string``.
    """
    next_token = next(tokens)

    if next_token == 'EMPTY':
        return dict(type='LineString', coordinates=[])
    elif not next_token == '(':
        raise ValueError(INVALID_WKT_FMT % string)

    # a list of lists
    # each member list represents a point
    coords = []
    try:
        pt = []
        for t in tokens:
            if t == ')':
                coords.append(pt)
                break
            elif t == ',':
                # it's the end of the point
                coords.append(pt)
                pt = []
            else:
                pt.append(_parse_coordinate(t, exact))
    except tokenize.TokenError:
        raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='LineString', coordinates=coords)


def _load_polygon(tokens, string, exact=False):
    """
    Has similar inputs and return value to to :func:`_load_point`, except is
    for handling POLYGON geometry.

    :returns:
        A GeoJSON `dict` Polygon representation of the WKT ``string``.
    """
    next_token = next(tokens)
    if next_token == 'EMPTY':
        return dict(type='Polygon', coordinates=[])

    open_parens = next(tokens), next_token
    if not open_parens == ('(', '('):
        raise ValueError(INVALID_WKT_FMT % string)

    # coords contains a list of rings
    # each ring contains a list of points
    # each point is a list of 2-4 values
    coords = []

    ring = []
    on_ring = True
    try:
        pt = []
        for t in tokens:
            if t == ')' and on_ring:
                # The ring is finished
                ring.append(pt)
                coords.append(ring)
                on_ring = False
            elif t == ')' and not on_ring:
                # it's the end of the polygon
                break
            elif t == '(':
                # it's a new ring
                ring = []
                pt = []
                on_ring = True
            elif t == ',' and on_ring:
                # it's the end of a point
                ring.append(pt)
                pt = []
            elif t == ',' and not on_ring:
                # there's another ring.
                # do nothing
                pass
            else:
                pt.append(_parse_coordinate(t, exact))
    except tokenize.TokenError:
        raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='Polygon', coordinates=coords)


def _load_multipoint(tokens, string, exact=False):
    """
    Has similar inputs and return value to to :func:`_load_point`, except is
    for handling MULTIPOINT geometry.

    :returns:
        A GeoJSON `dict` MultiPoint representation of the WKT ``string``.
    """
    next_token = next(tokens)

    if next_token == 'EMPTY':
        return dict(type='MultiPoint', coordinates=[])
    elif not next_token == '(':
        raise ValueError(INVALID_WKT_FMT % string)

    coords = []
    pt = []

    paren_depth = 1
    try:
        for t in tokens:
            if t == '(':
                paren_depth += 1
            elif t == ')':
                paren_depth -= 1
                if paren_depth == 0:
                    break
            elif t == ',':
                # the point is done
                coords.append(pt)
                pt = []
            else:
                pt.append(_parse_coordinate(t, exact))
    except tokenize.TokenError:
        raise ValueError(INVALID_WKT_FMT % string)

    # Given the way we're parsing, we'll probably have to deal with the last
    # point after the loop
    if len(pt) > 0:
        coords.append(pt)

    return dict(type='MultiPoint', coordinates=coords)


def _load_multipolygon(tokens, string, exact=False):
    """
    Has similar inputs and return value to to :func:`_load_point`, except is
    for handling MULTIPOLYGON geometry.

    :returns:
        A GeoJSON `dict` MultiPolygon representation of the WKT ``string``.
    """
    next_token = next(tokens)

    if next_token == 'EMPTY':
        return dict(type='MultiPolygon', coordinates=[])
    elif not next_token == '(':
        raise ValueError(INVALID_WKT_FMT % string)

    polygons = []
    while True:
        try:
            poly = _load_polygon(tokens, string, exact=exact)
            polygons.append(poly['coordinates'])
            t = next(tokens)
            if t == ')':
                # we're done; no more polygons.
                break
        except (StopIteration, tokenize.TokenError):
            # If we reach this, the WKT is not valid.
            raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='MultiPolygon', coordinates=polygons)


def _load_multilinestring(tokens, string, exact=False):
    """
    Has similar inputs and return value to to :func:`_load_point`, except is
    for handling MULTILINESTRING geometry.

    :returns:
        A GeoJSON `dict` MultiLineString representation of the WKT ``string``.
    """
    next_token = next(tokens)

    if next_token == 'EMPTY':
        return dict(type='MultiLineString', coordinates=[])
    elif not next_token == '(':
        raise ValueError(INVALID_WKT_FMT % string)

    linestrs = []
    while True:
        try:
            linestr = _load_linestring(tokens, string, exact=exact)
            linestrs.append(linestr['coordinates'])
            t = next(tokens)
            if t == ')':
                # we're done; no more linestrings.
                break
        except (StopIteration, tokenize.TokenError):
            # If we reach this, the WKT is not valid.
            raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='MultiLineString', coordinates=linestrs)


def _load_geometrycollection(tokens, string, exact=False):
    """
    Has similar inputs and return value to to :func:`_load_point`, except is
    for handling GEOMETRYCOLLECTIONs.

    Delegates parsing to the parsers for the individual geometry types.

    :returns:
        A GeoJSON `dict` GeometryCollection representation of the WKT
        ``string``.
    """
    next_token = next(tokens)

    if next_token == 'EMPTY':
        return dict(type='GeometryCollection', geometries=[])
    elif not next_token == '(':
        raise ValueError(INVALID_WKT_FMT % string)

    geoms = []
    result = dict(type='GeometryCollection', geometries=geoms)
    while True:
        try:
            t = next(tokens)
            if t == ')':
                break
            elif t == ',':
                # another geometry still
                continue
            else:
                geom_type = t
                load_func = _loads_registry.get(geom_type)
                geom = load_func(tokens, string, exact=exact)
                geoms.append(geom)
        except (StopIteration, tokenize.TokenError):
            raise ValueError(INVALID_WKT_FMT % string)
    return result


_dumps_registry = {
    'Point': _dump_point,
    'LineString': _dump_linestring,
    'Polygon': _dump_polygon,
    'MultiPoint': _dump_multipoint,
    'MultiLineString': _dump_multilinestring,
    'MultiPolygon': _dump_multipolygon,
    'GeometryCollection': _dump_geometrycollection,
}


_loads_registry = {
    'POINT': _load_point,
    'LINESTRING': _load_linestring,
    'POLYGON': _load_polygon,
    'MULTIPOINT': _load_multipoint,
    'MULTILINESTRING': _load_multilinestring,
    'MULTIPOLYGON': _load_multipolygon,
    'GEOMETRYCOLLECTION': _load_geometrycollection,
}

_type_map_caps_to_mixed = dict(
    POINT='Point',
    LINESTRING='LineString',
    POLYGON='Polygon',
    MULTIPOINT='MultiPoint',
    MULTILINESTRING='MultiLineString',
    MULTIPOLYGON='MultiPolygon',
    GEOMETRYCOLLECTION='GeometryCollection',
)

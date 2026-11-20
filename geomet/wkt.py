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
import io
import itertools
import re
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


def dump(obj, dest_file):
    """
    Dump GeoJSON-like `dict` to WKT and write it to the `dest_file`.

    :param dict obj:
        A GeoJSON-like dictionary. It must at least the keys 'type' and
        'coordinates'.
    :param dest_file:
        Open and writable file-like object.
    """
    dest_file.write(dumps(obj))


def load(source_file):
    """
    Load a GeoJSON `dict` object from a ``source_file`` containing WKT.

    :param source_file:
        Open and readable file-like object.

    :returns:
        A GeoJSON `dict` representing the geometry read from the file.
    """
    return loads(source_file.read())


def dumps(obj, decimals=16):
    """
    Dump a GeoJSON-like `dict` to a WKT string.
    """
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


def loads(string):
    """
    Construct a GeoJSON `dict` from WKT (`string`).
    """
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
    result = importer(tokens, string)
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


# ---------------------------------------------------------------------------
# Strict WKT reading, intended for cleaning data before ingestion.
#
# A CSV geometry cell must hold exactly one WKT geometry. The strict reader
# therefore rejects trailing/extra content and incomplete geometries instead
# of silently ignoring them, and reports every error against the original,
# unmodified cell text (no trimming, line splitting or SRID stripping).
# ---------------------------------------------------------------------------

_SRID_INTEGER_RE = re.compile(r'\d+\Z')


def _line_starts(text):
    """Return the character offset at which each 1-based line starts."""
    starts = [0]
    for index, char in enumerate(text):
        if char == '\n':
            starts.append(index + 1)
    return starts


def _line_col(text, line_starts, position):
    """
    Convert an absolute character ``position`` in ``text`` to a 1-based
    ``(line, column)`` pair. Line offsets are anchored at the starts of the
    original lines, so both ``\\n`` and ``\\r\\n`` count as a single line
    break and multi-line cells are located correctly without normalising
    the text first.
    """
    line = 1
    while line < len(line_starts) and line_starts[line] <= position:
        line += 1
    column = position - line_starts[line - 1]
    return line, column + 1


def _strict_error(text, line_starts, position, message):
    line, column = _line_col(text, line_starts, position)
    raise geomet.InvalidWKTException(
        '%s (at character %d, line %d, column %d)'
        % (message, position + 1, line, column),
        text=text,
        position=position + 1,
        line=line,
        column=column,
    )


def _strict_tokens(text, line_starts):
    """
    Tokenize ``text`` while retaining the span of every token.

    Yields ``(token, start_offset, end_offset)`` tuples whose offsets refer
    to the original cell text. Like :func:`_tokenize_wkt`, a leading ``-``
    is joined to the following number so negative coordinates stay a single
    token.
    """
    sio = io.StringIO(text)
    pending_minus = None
    try:
        for tok in tokenize.generate_tokens(sio.readline):
            kind = tok[0]
            string = tok[1]
            start_row, start_col = tok[2]
            end_row, end_col = tok[3]
            if kind in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                        tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            start = line_starts[start_row - 1] + start_col
            end = line_starts[end_row - 1] + end_col
            if string == '-':
                pending_minus = start
                continue
            if pending_minus is not None:
                yield ('-' + string, pending_minus, end)
                pending_minus = None
            else:
                yield (string, start, end)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # An unclosed bracket/construct: simply stop, the structural scan
        # below reports the missing closing parenthesis.
        pass
    if pending_minus is not None:
        yield ('-', pending_minus, pending_minus + 1)


class _StrictParser(object):
    """Position-aware parser enforcing the single-geometry-per-cell rule."""

    _EOF = object()

    def __init__(self, text):
        self.text = text
        self.line_starts = _line_starts(text)
        self.tokens = list(_strict_tokens(text, self.line_starts))
        self.cursor = 0
        self.reject_stray_characters()

    def reject_stray_characters(self):
        # Python's tokenizer silently drops comments and a few stray
        # characters. WKT has neither, so any non-whitespace character not
        # covered by a token span is reported at its exact original offset.
        covered = [False] * len(self.text)
        for _token, start, end in self.tokens:
            for index in range(start, min(end, len(covered))):
                covered[index] = True
        for index, char in enumerate(self.text):
            if not covered[index] and not char.isspace():
                self.error(
                    index,
                    'Unexpected character %r in WKT value' % char,
                )

    def error(self, position, message):
        _strict_error(self.text, self.line_starts, position, message)

    def end_position(self):
        return len(self.text)

    def peek(self):
        if self.cursor >= len(self.tokens):
            return self._EOF
        return self.tokens[self.cursor]

    def advance(self):
        token = self.peek()
        if token is not self._EOF:
            self.cursor += 1
        return token

    def expect(self, expected):
        token = self.peek()
        if token is self._EOF:
            self.error(
                self.end_position(),
                'Unexpected end of value: expected %r' % expected,
            )
        value, position, _ = token
        if value != expected:
            self.error(
                position,
                'Expected %r but found %r' % (expected, value),
            )
        self.advance()

    def parse(self):
        if not self.tokens:
            self.error(0, 'Empty WKT value: expected a geometry')
        srid = None
        if self.peek()[0] == 'SRID':
            srid = self.parse_srid_header()
        geom_type = self.expect_geom_keyword()
        result = self.parse_geometry(geom_type)
        leftover = self.peek()
        if leftover is not self._EOF:
            value, position, _ = leftover
            self.error(
                position,
                'Unexpected extra content %r after the complete geometry; '
                'each cell must contain exactly one WKT geometry' % value,
            )
        if srid is not None:
            result['meta'] = dict(srid=srid)
        return result

    def parse_srid_header(self):
        self.expect('SRID')
        self.expect('=')
        number = self.peek()
        if number is self._EOF or not _SRID_INTEGER_RE.match(number[0]):
            position = (self.end_position() if number is self._EOF
                        else number[1])
            self.error(
                position,
                'Invalid SRID header: expected an integer after "SRID="',
            )
        self.advance()
        self.expect(';')
        return int(number[0])

    def expect_geom_keyword(self):
        token = self.peek()
        if token is self._EOF:
            self.error(
                self.end_position(),
                'Unexpected end of value: expected a geometry type keyword',
            )
        value, position, _ = token
        if value not in _loads_registry:
            self.error(
                position,
                'Unsupported or unexpected geometry type %r' % value,
            )
        self.advance()
        return value

    def parse_geometry(self, geom_type):
        token = self.peek()
        if token is self._EOF:
            self.error(
                self.end_position(),
                'Expected "EMPTY" or "(" after %s' % geom_type,
            )
        if token[0] == 'EMPTY':
            self.advance()
            if geom_type == 'GEOMETRYCOLLECTION':
                return dict(type='GeometryCollection', geometries=[])
            return dict(type=_type_map_caps_to_mixed[geom_type],
                        coordinates=[])
        if geom_type == 'GEOMETRYCOLLECTION':
            return self.parse_geometrycollection_body()
        return self.parse_coordinate_body(geom_type)

    def parse_geometrycollection_body(self):
        self.expect('(')
        geoms = []
        while True:
            token = self.peek()
            if token is self._EOF:
                self.error(
                    self.end_position(),
                    'Unclosed GEOMETRYCOLLECTION: expected another geometry '
                    'or ")"',
                )
            if token[0] == ')':
                self.advance()
                return dict(type='GeometryCollection', geometries=geoms)
            sub_type = self.expect_geom_keyword()
            geoms.append(self.parse_geometry(sub_type))
            separator = self.peek()
            if separator is self._EOF:
                self.error(
                    self.end_position(),
                    'Unclosed GEOMETRYCOLLECTION: expected "," or ")"',
                )
            if separator[0] == ')':
                self.advance()
                return dict(type='GeometryCollection', geometries=geoms)
            if separator[0] == ',':
                self.advance()
                continue
            # The collection was never closed before another keyword
            # appeared: report the syntax position inside the first item
            # rather than pretending a boundary was found.
            self.error(
                separator[1],
                'Incomplete GEOMETRYCOLLECTION: expected "," or ")" after '
                'the previous geometry but found %r' % separator[0],
            )

    def scan_coordinate_span(self, open_index):
        """
        Validate the coordinate construct starting at ``open_index`` and
        return the index of its matching closing parenthesis.
        """
        depth = 0
        index = open_index
        while index < len(self.tokens):
            value, position, _ = self.tokens[index]
            if value == '(':
                depth += 1
            elif value == ')':
                depth -= 1
                if depth == 0:
                    return index
            elif value != ',':
                try:
                    float(value)
                except ValueError:
                    self.error(
                        position,
                        'Expected a coordinate number but found %r' % value,
                    )
            index += 1
        self.error(
            self.end_position(),
            'Unclosed geometry: %d opening parenthesis was never closed'
            % depth,
        )

    def parse_coordinate_body(self, geom_type):
        token = self.peek()
        if token is self._EOF or token[0] != '(':
            position = (self.end_position() if token is self._EOF
                        else token[1])
            self.error(
                position,
                'Expected "(" after geometry type %s' % geom_type,
            )
        open_index = self.cursor
        close_index = self.scan_coordinate_span(open_index)
        span = [item[0] for item in self.tokens[open_index:close_index + 1]]
        # Reuse the existing importer so successful strict reads produce the
        # exact same GeoJSON as the lenient reader. The structural scan
        # above already validated the span, so failures here become strict
        # errors rather than leaking a different exception type.
        importer = _loads_registry[geom_type]
        try:
            result = importer(iter(span), self.text)
        except (ValueError, StopIteration, tokenize.TokenError):
            self.error(
                self.tokens[open_index][1],
                'Invalid %s geometry' % geom_type,
            )
        self.cursor = close_index + 1
        return result


def loads_strict(string):
    """
    Construct a GeoJSON `dict` from a WKT cell, enforcing strict rules.

    Unlike :func:`loads`, the input must contain exactly one complete WKT
    geometry. Only leading and trailing whitespace (including newlines and
    CRLF) is tolerated around that geometry: any extra content after the
    geometry, or an incomplete geometry, raises
    :class:`geomet.InvalidWKTException`.

    Error positions refer to the original ``string``: nothing is trimmed,
    split by line or has its ``SRID=...;`` header removed before locating
    the offending character, so the supplier can fix the original cell.

    :param str string:
        The raw WKT cell text.

    :returns:
        A GeoJSON `dict` representing the single geometry in the cell.

    :raises geomet.InvalidWKTException:
        If the cell is empty, holds more than one geometry / trailing
        content, or its single geometry is syntactically incomplete.
    """
    if not isinstance(string, str):
        raise TypeError(
            'WKT input must be a string, not %s' % type(string).__name__
        )
    return _StrictParser(string).parse()


def load_strict(source_file):
    """
    Strict counterpart of :func:`load` for a readable file-like object.
    """
    return loads_strict(source_file.read())


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
    if isinstance(value, int) and decimals != 0:
        # If the coordinate is an integer and decimals is non-zero,
        # convert it to a float for padding.
        value = float(value)

    elif decimals == 0:
        # if get a `decimals` value of 0, we want to return an int.
        return repr(int(round(value, decimals)))

    rounded = round(value, decimals)

    if 'e' in repr(rounded):
        rounded = format(rounded, '.{}f'.format(decimals))
    else:
        rounded = repr(rounded)
    rounded += '0' * (decimals - len(rounded.split('.')[1]))
    return rounded


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


def _load_point(tokens, string):
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
                coords.append(float(t))
    except tokenize.TokenError:
        raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='Point', coordinates=coords)


def _load_linestring(tokens, string):
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
                pt.append(float(t))
    except tokenize.TokenError:
        raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='LineString', coordinates=coords)


def _load_polygon(tokens, string):
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
                pt.append(float(t))
    except tokenize.TokenError:
        raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='Polygon', coordinates=coords)


def _load_multipoint(tokens, string):
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
                pt.append(float(t))
    except tokenize.TokenError:
        raise ValueError(INVALID_WKT_FMT % string)

    # Given the way we're parsing, we'll probably have to deal with the last
    # point after the loop
    if len(pt) > 0:
        coords.append(pt)

    return dict(type='MultiPoint', coordinates=coords)


def _load_multipolygon(tokens, string):
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
            poly = _load_polygon(tokens, string)
            polygons.append(poly['coordinates'])
            t = next(tokens)
            if t == ')':
                # we're done; no more polygons.
                break
        except (StopIteration, tokenize.TokenError):
            # If we reach this, the WKT is not valid.
            raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='MultiPolygon', coordinates=polygons)


def _load_multilinestring(tokens, string):
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
            linestr = _load_linestring(tokens, string)
            linestrs.append(linestr['coordinates'])
            t = next(tokens)
            if t == ')':
                # we're done; no more linestrings.
                break
        except (StopIteration, tokenize.TokenError):
            # If we reach this, the WKT is not valid.
            raise ValueError(INVALID_WKT_FMT % string)

    return dict(type='MultiLineString', coordinates=linestrs)


def _load_geometrycollection(tokens, string):
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
                geom = load_func(tokens, string)
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

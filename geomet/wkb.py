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
import binascii
import struct

from geomet.util import block_splitter
from geomet.util import take
from geomet.util import as_bin_str
from geomet.util import flatten_multi_dim
from itertools import chain

"""
Well-Known Binary (WKB) is a format used to encode simple geometric features
using a platform-independent byte array. It's commonly used for transferring
spatial data between systems or applications. By using WKB, systems can share
geometry data without relying on their internal storage formats, improving
compatibility and interoperability.
"""

#: '\x00': The first byte of any WKB string. Indicates big endian byte
#: ordering for the data.
BIG_ENDIAN = b'\x00'
#: '\x01': The first byte of any WKB string. Indicates little endian byte
#: ordering for the data.
LITTLE_ENDIAN = b'\x01'
#: High byte in a 4-byte geometry type field to indicate that a 4-byte SRID
#: field follows.
SRID_FLAG = b'\x20'

#: Mapping of GeoJSON geometry types to the "2D" 4-byte binary string
#: representation for WKB. "2D" indicates that the geometry is 2-dimensional,
#: X and Y components.
#: NOTE: Byte ordering is big endian.
WKB_2D = {
    'Point': b'\x00\x00\x00\x01',
    'LineString': b'\x00\x00\x00\x02',
    'Polygon': b'\x00\x00\x00\x03',
    'MultiPoint': b'\x00\x00\x00\x04',
    'MultiLineString': b'\x00\x00\x00\x05',
    'MultiPolygon': b'\x00\x00\x00\x06',
    'GeometryCollection': b'\x00\x00\x00\x07',
}

#: Mapping of GeoJSON geometry types to the "Z" 4-byte binary string
#: representation for WKB. "Z" indicates that the geometry is 3-dimensional,
#: with X, Y, and Z components.
#: NOTE: Byte ordering is big endian.
WKB_Z = {
    'Point': b'\x00\x00\x03\xe9',
    'LineString': b'\x00\x00\x03\xea',
    'Polygon': b'\x00\x00\x03\xeb',
    'MultiPoint': b'\x00\x00\x03\xec',
    'MultiLineString': b'\x00\x00\x03\xed',
    'MultiPolygon': b'\x00\x00\x03\xee',
    'GeometryCollection': b'\x00\x00\x03\xef',
}

#: Mapping of GeoJSON geometry types to the "M" 4-byte binary string
#: representation for WKB. "M" indicates that the geometry is 2-dimensional,
#: with X, Y, and M ("Measure") components.
#: NOTE: Byte ordering is big endian.
WKB_M = {
    'Point': b'\x00\x00\x07\xd1',
    'LineString': b'\x00\x00\x07\xd2',
    'Polygon': b'\x00\x00\x07\xd3',
    'MultiPoint': b'\x00\x00\x07\xd4',
    'MultiLineString': b'\x00\x00\x07\xd5',
    'MultiPolygon': b'\x00\x00\x07\xd6',
    'GeometryCollection': b'\x00\x00\x07\xd7',
}

#: Mapping of GeoJSON geometry types to the "ZM" 4-byte binary string
#: representation for WKB. "ZM" indicates that the geometry is 4-dimensional,
#: with X, Y, Z, and M ("Measure") components.
#: NOTE: Byte ordering is big endian.
WKB_ZM = {
    'Point': b'\x00\x00\x0b\xb9',
    'LineString': b'\x00\x00\x0b\xba',
    'Polygon': b'\x00\x00\x0b\xbb',
    'MultiPoint': b'\x00\x00\x0b\xbc',
    'MultiLineString': b'\x00\x00\x0b\xbd',
    'MultiPolygon': b'\x00\x00\x0b\xbe',
    'GeometryCollection': b'\x00\x00\x0b\xbf',
}

#: Mapping of dimension types to maps of GeoJSON geometry type -> 4-byte binary
#: string representation for WKB.
_WKB = {
    '2D': WKB_2D,
    'Z': WKB_Z,
    'M': WKB_M,
    'ZM': WKB_ZM,
}

#: Mapping from binary geometry type (as a 4-byte binary string) to GeoJSON
#: geometry type.
#: NOTE: Byte ordering is big endian.
_BINARY_TO_GEOM_TYPE = dict(
    chain(*((reversed(x) for x in wkb_map.items())
            for wkb_map in _WKB.values()))
)

_INT_TO_DIM_LABEL = {2: '2D', 3: 'Z', 4: 'ZM'}


class WKBError(ValueError):
    """Base class for errors raised while decoding a WKB record."""


class WKBCoordinateBudgetExceeded(WKBError):
    """Raised when a record contains more vertices than the caller's
    coordinate budget allows.

    The check happens before the coordinates of the offending ring,
    linestring, or point are consumed, so the input stream is left positioned
    at the start of that coordinate run.

    Attributes:
        budget: The coordinate budget in effect for the record.
        vertices_counted: Vertices already counted within the record when
            the over-limit member was encountered.
        vertices_required: Total vertices the member declares, including the
            portion that would exceed the budget.
        layer: Path of geometry-type labels from the record root to the
            member whose vertex count exceeds the budget.
    """

    def __init__(self, budget, vertices_counted, vertices_required, layer):
        self.budget = budget
        self.vertices_counted = vertices_counted
        self.vertices_required = vertices_required
        self.layer = list(layer)
        super().__init__(
            "Coordinate budget of %d vertices exceeded at %s: %d vertices "
            "already counted, member declares %d more"
            % (budget, ' > '.join(self.layer) or 'record',
               vertices_counted, vertices_required)
        )


class WKBTruncated(WKBError):
    """Raised when a record ends before all of its declared structure and
    coordinates have been read.

    Attributes:
        layer: Path of geometry-type labels from the record root to the
            member whose payload could not be read in full.
        expected_bytes: Number of bytes the member still needed.
        available_bytes: Number of bytes actually left in the record.
    """

    def __init__(self, layer, expected_bytes, available_bytes):
        self.layer = list(layer)
        self.expected_bytes = expected_bytes
        self.available_bytes = available_bytes
        super().__init__(
            "Truncated WKB payload at %s: needed %d bytes but only %d "
            "remained" % (' > '.join(self.layer) or 'record',
                          expected_bytes, available_bytes)
        )


class WKBDepthExceeded(WKBError):
    """Raised when nested geometry collections go deeper than the caller's
    depth limit.

    Attributes:
        max_depth: The nesting depth limit in effect for the record.
        depth: The depth of the first member that would exceed the limit.
        layer: Path of geometry-type labels from the record root to the
            member that would exceed the depth limit.
    """

    def __init__(self, max_depth, depth, layer):
        self.max_depth = max_depth
        self.depth = depth
        self.layer = list(layer)
        super().__init__(
            "Geometry nesting depth limit of %d exceeded at %s: depth %d"
            % (max_depth, ' > '.join(self.layer), depth)
        )


class _DecodeContext:
    """Per-record bookkeeping for optional coordinate and depth budgets.

    A single context is shared by every geometry in one record, so the
    coordinate count accumulates across nested polygons, multipolygons and
    geometry collections. Empty members (for example a polygon ring or a
    linestring declaring zero vertices) consume no coordinate budget but are
    still recorded as a layer for depth accounting.

    When no budget is supplied the counters still run, but they can never
    trip a limit, preserving the historical behavior of ``loads``.
    """

    def __init__(self, coordinate_budget=None, max_depth=None):
        self.coordinate_budget = coordinate_budget
        self.max_depth = max_depth
        self.vertices = 0
        self.depth = 0
        self.layer = []

    def enter(self, geom_type):
        self.layer.append(geom_type)
        self.depth += 1

    def leave(self):
        self.depth -= 1
        self.layer.pop()

    def check_depth(self):
        if self.max_depth is None:
            return
        if self.depth > self.max_depth:
            raise WKBDepthExceeded(self.max_depth, self.depth, self.layer)

    def reserve_vertices(self, num_vertices):
        """Account for ``num_vertices`` vertices before their coordinates
        are read. Raises without consuming any of the coordinate bytes when
        the declared count would exceed the remaining budget."""
        if num_vertices == 0:
            return
        if self.coordinate_budget is not None:
            if self.vertices + num_vertices > self.coordinate_budget:
                raise WKBCoordinateBudgetExceeded(
                    self.coordinate_budget, self.vertices,
                    num_vertices, self.layer
                )
        self.vertices += num_vertices


def _read_exact(num_bytes, stream, ctx):
    """Read exactly ``num_bytes`` from ``stream`` or report a truncated
    record without borrowing bytes from whatever follows it."""
    data = as_bin_str(take(num_bytes, stream))
    if len(data) != num_bytes:
        raise WKBTruncated(ctx.layer, num_bytes, len(data))
    return data


def _read_count(endian_token, stream, ctx):
    """Read a 4-byte member count at the current decode layer."""
    [count] = struct.unpack('%sl' % endian_token,
                            _read_exact(4, stream, ctx))
    return count


def _get_geom_type(type_bytes):
    """Get the GeoJSON geometry type label from a WKB type byte string.

    :param type_bytes:
        4 byte string in big endian byte order containing a WKB type number.
        It may also contain a "has SRID" flag in the high byte (the first type,
        since this is big endian byte order), indicated as 0x20. If the SRID
        flag is not set, the high byte will always be null (0x00).
    :returns:
        3-tuple ofGeoJSON geometry type label, the bytes representing the
        geometry type, and a separate "has SRID" flag. If the input
        `type_bytes` contains an SRID flag, it will be removed.

        >>> # Z Point, with SRID flag
        >>> _get_geom_type(b'\\x20\\x00\\x03\\xe9') == (
        ... 'Point', b'\\x00\\x00\\x03\\xe9', True)
        True

        >>> # 2D MultiLineString, without SRID flag
        >>> _get_geom_type(b'\\x00\\x00\\x00\\x05') == (
        ... 'MultiLineString', b'\\x00\\x00\\x00\\x05', False)
        True

    """
    # slice off the high byte, which may contain the SRID flag
    high_byte = bytes([type_bytes[0]])
    has_srid = high_byte == b'\x20'
    if has_srid:
        # replace the high byte with a null byte
        type_bytes = as_bin_str(b'\x00' + type_bytes[1:])
    else:
        type_bytes = as_bin_str(type_bytes)

    # look up the geometry type
    geom_type = _BINARY_TO_GEOM_TYPE.get(type_bytes)
    return geom_type, type_bytes, has_srid


def dump(obj, dest_file):
    """
    Dump GeoJSON-like `dict` to WKB and write it to the `dest_file`.

    :param dict obj:
        A GeoJSON-like dictionary. It must at least the keys 'type' and
        'coordinates'.
    :param dest_file:
        Open and writable file-like object.
    """
    dest_file.write(dumps(obj))


def load(source_file):
    """
    Load a GeoJSON `dict` object from a ``source_file`` containing WKB (as a
    byte string).

    :param source_file:
        Open and readable file-like object.

    :returns:
        A GeoJSON `dict` representing the geometry read from the file.
    """
    return loads(source_file.read())


def dumps(obj, big_endian=True):
    """
    Dump a GeoJSON-like `dict` to a WKB string.

    .. note::
        The dimensions of the generated WKB will be inferred from the first
        vertex in the GeoJSON `coordinates`. It will be assumed that all
        vertices are uniform. There are 4 types:

        - 2D (X, Y): 2-dimensional geometry
        - Z (X, Y, Z): 3-dimensional geometry
        - M (X, Y, M): 2-dimensional geometry with a "Measure"
        - ZM (X, Y, Z, M): 3-dimensional geometry with a "Measure"

        If the first vertex contains 2 values, we assume a 2D geometry.
        If the first vertex contains 3 values, this is slightly ambiguous and
        so the most common case is chosen: Z.
        If the first vertex contains 4 values, we assume a ZM geometry.

        The WKT/WKB standards provide a way of differentiating normal (2D), Z,
        M, and ZM geometries (http://en.wikipedia.org/wiki/Well-known_text),
        but the GeoJSON spec does not. Therefore, for the sake of interface
        simplicity, we assume that geometry that looks 3D contains XYZ
        components, instead of XYM.

        If the coordinates list contains no coordinate values, including
        within nested structures such as [[[[], []], []]], the geometry is
        considered empty. Geometries other than points have a valid "empty"
        representation in WKB; however, without knowing the number of
        coordinate values per vertex, the type is ambiguous, and thus we don't
        know if the geometry type is 2D, Z, M, or ZM. In such cases, a
        ValueError will be raised.

    :param dict obj:
        GeoJson-like `dict` object.
    :param bool big_endian:
        Defaults to `True`. If `True`, data values in the generated WKB will
        be represented using big endian byte order. Else, little endian.

    :returns:
        A WKB binary string representing of the ``obj``.
    """
    return _dumps(obj, big_endian)


def _dumps(obj, big_endian=True, include_meta=True):
    """
    Basically perform the action of dumps, but with some extra flags for
    behavior specifically needed by the geopackage...package.
    """
    geom_type = obj['type']
    if include_meta:
        meta = obj.get('meta', {})
    else:
        meta = {}

    exporter = _dumps_registry.get(geom_type)
    if exporter is None:
        _unsupported_geom_type(geom_type)

    # Check for empty geometries. GeometryCollections have a slightly different
    # JSON/dict structure, but that's handled.
    coords_or_geoms = obj.get('coordinates', obj.get('geometries'))
    if len(list(flatten_multi_dim(coords_or_geoms))) == 0:
        raise ValueError(
            'Empty geometries cannot be represented in WKB. Reason: The '
            'dimensionality of the WKB would be ambiguous.'
        )

    return exporter(obj, big_endian, meta)


def loads(string, coordinate_budget=None, max_depth=None):
    """
    Construct a GeoJSON `dict` from WKB (`string`).

    The resulting GeoJSON `dict` will include the SRID as an integer in the
    `meta` object. This was an arbitrary decision made by `geomet, the
    discussion of which took place here:
    https://github.com/geomet/geomet/issues/28.

    In order to be consistent with other libraries [1] and (deprecated)
    specifications [2], also include the same information in a `crs`
    object. This isn't ideal, but the `crs` member is no longer part of
    the GeoJSON standard, according to RFC7946 [3]. However, it's still
    useful to include this information in GeoJSON payloads because it
    supports conversion to EWKT/EWKB (which are canonical formats used by
    PostGIS and the like).

    Example:

        {'type': 'Point',
         'coordinates': [0.0, 1.0],
         'meta': {'srid': 4326},
         'crs': {'type': 'name', 'properties': {'name': 'EPSG4326'}}}

    NOTE(larsbutler): I'm not sure if it's valid to just prefix EPSG
    (European Petroluem Survey Group) to an SRID like this, but we'll
    stick with it for now until it becomes a problem.

    NOTE(larsbutler): Ideally, we should use URNs instead of this
    notation, according to the new GeoJSON spec [4]. However, in
    order to be consistent with [1], we'll stick with this approach
    for now.

    :param bytes string:
        A single WKB record. When a budget or depth limit is given, the
        record boundary is the caller's responsibility: exactly the bytes
        of this record must be supplied so a failed decode can never
        consume bytes that belong to the next record.
    :param int coordinate_budget:
        Optional maximum number of vertices the whole record may contain.
        The count accumulates across the entire record, including the
        vertices of polygon interior rings and any nested members. Empty
        members consume no vertex budget but still occupy a layer. When a
        member declares more vertices than the budget allows,
        :class:`WKBCoordinateBudgetExceeded` is raised before any of that
        member's coordinate bytes are read. Defaults to `None`, which
        preserves the historical unbounded behavior.
    :param int max_depth:
        Optional maximum nesting depth, counted in geometry layers (for
        example a geometry collection holding another collection is
        depth 2). The first member that would exceed the limit raises
        :class:`WKBDepthExceeded` before its body is read. Defaults to
        `None`, which preserves the historical unbounded behavior.

    A record which ends before its declared payload is fully read raises
    :class:`WKBTruncated`. Every budget-related error carries the layer
    path at which it occurred and the counted (or over-limit) metrics, so
    bulk loaders can distinguish coordinate overflow from structurally
    incomplete input.

    References:

    [1] - https://github.com/bryanjos/geo/issues/76
    [2] - http://geojson.org/geojson-spec.html#coordinate-reference-system-objects
    [3] - https://tools.ietf.org/html/rfc7946#appendix-B.1
    [4] - https://tools.ietf.org/html/rfc7946#section-4
    """  # noqa
    ctx = _DecodeContext(coordinate_budget=coordinate_budget,
                         max_depth=max_depth)
    result, has_srid, srid = _loads_geometry(iter(string), ctx)
    if has_srid:
        # As mentioned in the docstring above, include both approaches to
        # indicating the SRID.
        result['meta'] = {'srid': int(srid)}
        result['crs'] = {
            'type': 'name',
            'properties': {'name': 'EPSG%s' % srid},
        }
    return result


def _loads_geometry(string, ctx):
    """Parse one geometry, including its header, from a shared stream.

    A single :class:`_DecodeContext` is shared by every geometry in a
    record so that coordinate and depth accounting span nested members.
    Returns a ``(geometry, has_srid, srid)`` tuple.
    """
    # endianness = string[0:1]
    endianness = _read_exact(1, string, ctx)
    if endianness == BIG_ENDIAN:
        big_endian = True
    elif endianness == LITTLE_ENDIAN:
        big_endian = False
    else:
        raise ValueError("Invalid endian byte: '0x%s'. Expected 0x00 or 0x01"
                         % binascii.hexlify(endianness.encode()).decode())

    endian_token = '>' if big_endian else '<'
    # type_bytes = string[1:5]
    type_bytes = _read_exact(4, string, ctx)
    if not big_endian:
        # To identify the type, order the type bytes in big endian:
        type_bytes = type_bytes[::-1]

    geom_type, type_bytes, has_srid = _get_geom_type(type_bytes)
    srid = None
    if has_srid:
        srid_field = _read_exact(4, string, ctx)
        [srid] = struct.unpack('%si' % endian_token, srid_field)

    importer = _loads_registry.get(geom_type)

    if importer is None:
        _unsupported_geom_type(geom_type)

    ctx.enter(geom_type)
    try:
        ctx.check_depth()
        result = importer(big_endian, type_bytes, string, ctx)
    finally:
        ctx.leave()
    return result, has_srid, srid


def _unsupported_geom_type(geom_type):
    raise ValueError("Unsupported geometry type '%s'" % geom_type)


# TODO: dont default meta to none
def _header_bytefmt_byteorder(geom_type, num_dims, big_endian, meta=None):
    """
    Utility function to get the WKB header (endian byte + type header), byte
    format string, and byte order string.
    """
    dim = _INT_TO_DIM_LABEL.get(num_dims)
    if dim is None:
        pass  # TODO: raise

    type_byte_str = _WKB[dim][geom_type]
    srid = meta.get('srid')
    if srid is not None:
        # Add the srid flag
        type_byte_str = SRID_FLAG + type_byte_str[1:]

    if big_endian:
        header = BIG_ENDIAN
        byte_fmt = b'>'
        byte_order = '>'
    else:
        header = LITTLE_ENDIAN
        byte_fmt = b'<'
        byte_order = '<'
        # reverse the byte ordering for little endian
        type_byte_str = type_byte_str[::-1]

    header += type_byte_str
    if srid is not None:
        srid = int(srid)

        if big_endian:
            srid_header = struct.pack('>i', srid)
        else:
            srid_header = struct.pack('<i', srid)
        header += srid_header
    byte_fmt += b'd' * num_dims

    return header, byte_fmt, byte_order


def _dump_point(obj, big_endian, meta):
    """
    Dump a GeoJSON-like `dict` to a point WKB string.

    :param dict obj:
        GeoJson-like `dict` object.
    :param bool big_endian:
        If `True`, data values in the generated WKB will be represented using
        big endian byte order. Else, little endian.
    :param dict meta:
        Metadata associated with the GeoJSON object. Currently supported
        metadata:

        - srid: Used to support EWKT/EWKB. For example, ``meta`` equal to
          ``{'srid': '4326'}`` indicates that the geometry is defined using
          Extended WKT/WKB and that it bears a Spatial Reference System
          Identifier of 4326. This ID will be encoded into the resulting
          binary.

        Any other meta data objects will simply be ignored by this function.

    :returns:
        A WKB binary string representing of the Point ``obj``.
    """
    coords = obj['coordinates']
    num_dims = len(coords)

    wkb_string, byte_fmt, _ = _header_bytefmt_byteorder(
        'Point', num_dims, big_endian, meta
    )

    wkb_string += struct.pack(byte_fmt, *coords)
    return wkb_string


def _dump_linestring(obj, big_endian, meta):
    """
    Dump a GeoJSON-like `dict` to a linestring WKB string.

    Input parameters and output are similar to :func:`_dump_point`.
    """
    coords = obj['coordinates']
    vertex = coords[0]
    # Infer the number of dimensions from the first vertex
    num_dims = len(vertex)

    wkb_string, byte_fmt, byte_order = _header_bytefmt_byteorder(
        'LineString', num_dims, big_endian, meta
    )
    # append number of vertices in linestring
    wkb_string += struct.pack('%sl' % byte_order, len(coords))

    for vertex in coords:
        wkb_string += struct.pack(byte_fmt, *vertex)

    return wkb_string


def _dump_polygon(obj, big_endian, meta):
    """
    Dump a GeoJSON-like `dict` to a polygon WKB string.

    Input parameters and output are similar to :funct:`_dump_point`.
    """
    coords = obj['coordinates']
    vertex = coords[0][0]
    # Infer the number of dimensions from the first vertex
    num_dims = len(vertex)

    wkb_string, byte_fmt, byte_order = _header_bytefmt_byteorder(
        'Polygon', num_dims, big_endian, meta
    )

    # number of rings:
    wkb_string += struct.pack('%sl' % byte_order, len(coords))
    for ring in coords:
        # number of verts in this ring:
        wkb_string += struct.pack('%sl' % byte_order, len(ring))
        for vertex in ring:
            wkb_string += struct.pack(byte_fmt, *vertex)

    return wkb_string


def _dump_multipoint(obj, big_endian, meta):
    """
    Dump a GeoJSON-like `dict` to a multipoint WKB string.

    Input parameters and output are similar to :funct:`_dump_point`.
    """
    coords = obj['coordinates']
    vertex = coords[0]
    num_dims = len(vertex)

    wkb_string, byte_fmt, byte_order = _header_bytefmt_byteorder(
        'MultiPoint', num_dims, big_endian, meta
    )

    point_type = _WKB[_INT_TO_DIM_LABEL.get(num_dims)]['Point']
    if big_endian:
        point_type = BIG_ENDIAN + point_type
    else:
        point_type = LITTLE_ENDIAN + point_type[::-1]

    wkb_string += struct.pack('%sl' % byte_order, len(coords))
    for vertex in coords:
        # POINT type strings
        wkb_string += point_type
        wkb_string += struct.pack(byte_fmt, *vertex)

    return wkb_string


def _dump_multilinestring(obj, big_endian, meta):
    """
    Dump a GeoJSON-like `dict` to a multilinestring WKB string.

    Input parameters and output are similar to :funct:`_dump_point`.
    """
    coords = obj['coordinates']
    vertex = coords[0][0]
    num_dims = len(vertex)

    wkb_string, byte_fmt, byte_order = _header_bytefmt_byteorder(
        'MultiLineString', num_dims, big_endian, meta
    )

    ls_type = _WKB[_INT_TO_DIM_LABEL.get(num_dims)]['LineString']
    if big_endian:
        ls_type = BIG_ENDIAN + ls_type
    else:
        ls_type = LITTLE_ENDIAN + ls_type[::-1]

    # append the number of linestrings
    wkb_string += struct.pack('%sl' % byte_order, len(coords))

    for linestring in coords:
        wkb_string += ls_type
        # append the number of vertices in each linestring
        wkb_string += struct.pack('%sl' % byte_order, len(linestring))
        for vertex in linestring:
            wkb_string += struct.pack(byte_fmt, *vertex)

    return wkb_string


def _dump_multipolygon(obj, big_endian, meta):
    """
    Dump a GeoJSON-like `dict` to a multipolygon WKB string.

    Input parameters and output are similar to :funct:`_dump_point`.
    """
    coords = obj['coordinates']
    vertex = coords[0][0][0]
    num_dims = len(vertex)

    wkb_string, byte_fmt, byte_order = _header_bytefmt_byteorder(
        'MultiPolygon', num_dims, big_endian, meta
    )

    poly_type = _WKB[_INT_TO_DIM_LABEL.get(num_dims)]['Polygon']
    if big_endian:
        poly_type = BIG_ENDIAN + poly_type
    else:
        poly_type = LITTLE_ENDIAN + poly_type[::-1]

    # apped the number of polygons
    wkb_string += struct.pack('%sl' % byte_order, len(coords))

    for polygon in coords:
        # append polygon header
        wkb_string += poly_type
        # append the number of rings in this polygon
        wkb_string += struct.pack('%sl' % byte_order, len(polygon))
        for ring in polygon:
            # append the number of vertices in this ring
            wkb_string += struct.pack('%sl' % byte_order, len(ring))
            for vertex in ring:
                wkb_string += struct.pack(byte_fmt, *vertex)

    return wkb_string


def _dump_geometrycollection(obj, big_endian, meta):
    # TODO: handle empty collections
    geoms = obj['geometries']
    # determine the dimensionality (2d, 3d, 4d) of the collection
    # by sampling the first geometry
    first_geom = geoms[0]
    rest = geoms[1:]

    first_wkb = dumps(first_geom, big_endian=big_endian)
    first_type = first_wkb[1:5]
    if not big_endian:
        first_type = first_type[::-1]

    if first_type in WKB_2D.values():
        num_dims = 2
    elif first_type in WKB_Z.values():
        num_dims = 3
    elif first_type in WKB_ZM.values():
        num_dims = 4

    wkb_string, byte_fmt, byte_order = _header_bytefmt_byteorder(
        'GeometryCollection', num_dims, big_endian, meta
    )
    # append the number of geometries
    wkb_string += struct.pack('%sl' % byte_order, len(geoms))

    wkb_string += first_wkb
    for geom in rest:
        wkb_string += dumps(geom, big_endian=big_endian)

    return wkb_string


def _load_point(big_endian, type_bytes, data_bytes, ctx):
    """
    Convert byte data for a Point to a GeoJSON `dict`.

    :param bool big_endian:
        If `True`, interpret the ``data_bytes`` in big endian order, else
        little endian.
    :param str type_bytes:
        4-byte integer (as a binary string) indicating the geometry type
        (Point) and the dimensions (2D, Z, M or ZM). For consistency, these
        bytes are expected to always be in big endian order, regardless of the
        value of ``big_endian``.
    :param str data_bytes:
        Coordinate data in a binary string.
    :param ctx:
        :class:`_DecodeContext` tracking the record's coordinate and depth
        budgets.

    :returns:
        GeoJSON `dict` representing the Point geometry.
    """
    endian_token = '>' if big_endian else '<'

    if type_bytes == WKB_2D['Point']:
        ctx.reserve_vertices(1)
        coords = struct.unpack('%sdd' % endian_token,
                               _read_exact(16, data_bytes, ctx))
    elif type_bytes == WKB_Z['Point']:
        ctx.reserve_vertices(1)
        coords = struct.unpack('%sddd' % endian_token,
                               _read_exact(24, data_bytes, ctx))
    elif type_bytes == WKB_M['Point']:
        # NOTE: The use of XYM types geometries is quite rare. In the interest
        # of removing ambiguity, we will treat all XYM geometries as XYZM when
        # generate the GeoJSON. A default Z value of `0.0` will be given in
        # this case.
        ctx.reserve_vertices(1)
        coords = list(struct.unpack('%sddd' % endian_token,
                                    _read_exact(24, data_bytes, ctx)))
        coords.insert(2, 0.0)
    elif type_bytes == WKB_ZM['Point']:
        ctx.reserve_vertices(1)
        coords = struct.unpack('%sdddd' % endian_token,
                               _read_exact(32, data_bytes, ctx))

    return dict(type='Point', coordinates=list(coords))


def _load_linestring(big_endian, type_bytes, data_bytes, ctx):
    endian_token = '>' if big_endian else '<'

    is_m = False

    if type_bytes in WKB_2D.values():
        num_dims = 2
    elif type_bytes in WKB_Z.values():
        num_dims = 3
    elif type_bytes in WKB_M.values():
        num_dims = 3
        is_m = True
    elif type_bytes in WKB_ZM.values():
        num_dims = 4

    coords = []
    num_verts = _read_count(endian_token, data_bytes, ctx)
    # Reserve the whole run up front so an over-budget linestring stops
    # before any of its coordinate bytes are consumed.
    ctx.reserve_vertices(num_verts)

    for _ in range(num_verts):
        vert_wkb = _read_exact(8 * num_dims, data_bytes, ctx)
        fmt = '%s' + 'd' * num_dims
        vert = list(struct.unpack(fmt % endian_token, vert_wkb))
        if is_m:
            vert.insert(2, 0.0)

        coords.append(vert)

    return dict(type='LineString', coordinates=list(coords))


def _load_polygon(big_endian, type_bytes, data_bytes, ctx):
    endian_token = '>' if big_endian else '<'
    data_bytes = iter(data_bytes)

    is_m = False

    if type_bytes in WKB_2D.values():
        num_dims = 2
    elif type_bytes in WKB_Z.values():
        num_dims = 3
    elif type_bytes in WKB_M.values():
        num_dims = 3
        is_m = True
    elif type_bytes in WKB_ZM.values():
        num_dims = 4

    coords = []
    num_rings = _read_count(endian_token, data_bytes, ctx)

    for ring_index in range(num_rings):
        ring = []
        num_verts = _read_count(endian_token, data_bytes, ctx)

        ctx.layer.append('ring[%d]' % ring_index)
        try:
            # An empty ring declares no vertices and spends no coordinate
            # budget, but it still occupies this layer.
            ctx.reserve_vertices(num_verts)

            verts_wkb = _read_exact(8 * num_verts * num_dims,
                                    data_bytes, ctx)
            verts = block_splitter(verts_wkb, 8)
            verts = (b''.join(bytes([y]) for y in x) for x in verts)
            for vert_wkb in block_splitter(verts, num_dims):
                values = [struct.unpack('%sd' % endian_token, x)[0]
                          for x in vert_wkb]
                if is_m:
                    values.insert(2, 0.0)
                ring.append(values)
        finally:
            ctx.layer.pop()
        coords.append(ring)

    return dict(type='Polygon', coordinates=coords)


def _load_multipoint(big_endian, type_bytes, data_bytes, ctx):
    endian_token = '>' if big_endian else '<'
    data_bytes = iter(data_bytes)

    is_m = False

    if type_bytes in WKB_2D.values():
        num_dims = 2
    elif type_bytes in WKB_Z.values():
        num_dims = 3
    elif type_bytes in WKB_M.values():
        num_dims = 3
        is_m = True
    elif type_bytes in WKB_ZM.values():
        num_dims = 4

    if is_m:
        dim = 'M'
    else:
        dim = _INT_TO_DIM_LABEL[num_dims]

    coords = []
    num_points = _read_count(endian_token, data_bytes, ctx)

    for point_index in range(num_points):
        point_endian = _read_exact(1, data_bytes, ctx)
        point_type = _read_exact(4, data_bytes, ctx)

        ctx.layer.append('point[%d]' % point_index)
        try:
            ctx.reserve_vertices(1)
            values = struct.unpack(
                '%s%s' % (endian_token, 'd' * num_dims),
                _read_exact(8 * num_dims, data_bytes, ctx)
            )
        finally:
            ctx.layer.pop()
        values = list(values)
        if is_m:
            values.insert(2, 0.0)

        if big_endian:
            assert point_endian == BIG_ENDIAN
            assert point_type == _WKB[dim]['Point']
        else:
            assert point_endian == LITTLE_ENDIAN
            assert point_type[::-1] == _WKB[dim]['Point']

        coords.append(list(values))

    return dict(type='MultiPoint', coordinates=coords)


def _load_multilinestring(big_endian, type_bytes, data_bytes, ctx):
    endian_token = '>' if big_endian else '<'
    data_bytes = iter(data_bytes)

    is_m = False

    if type_bytes in WKB_2D.values():
        num_dims = 2
    elif type_bytes in WKB_Z.values():
        num_dims = 3
    elif type_bytes in WKB_M.values():
        num_dims = 3
        is_m = True
    elif type_bytes in WKB_ZM.values():
        num_dims = 4

    if is_m:
        dim = 'M'
    else:
        dim = _INT_TO_DIM_LABEL[num_dims]

    num_ls = _read_count(endian_token, data_bytes, ctx)
    coords = []

    for linestring_index in range(num_ls):
        ls_endian = _read_exact(1, data_bytes, ctx)
        ls_type = _read_exact(4, data_bytes, ctx)
        if big_endian:
            assert ls_endian == BIG_ENDIAN
            assert ls_type == _WKB[dim]['LineString']
        else:
            assert ls_endian == LITTLE_ENDIAN
            assert ls_type[::-1] == _WKB[dim]['LineString']

        num_verts = _read_count(endian_token, data_bytes, ctx)
        num_values = num_dims * num_verts

        ctx.layer.append('linestring[%d]' % linestring_index)
        try:
            ctx.reserve_vertices(num_verts)
            values = struct.unpack(
                endian_token + 'd' * num_values,
                _read_exact(8 * num_values, data_bytes, ctx)
            )
        finally:
            ctx.layer.pop()

        values = list(block_splitter(values, num_dims))
        if is_m:
            for v in values:
                v.insert(2, 0.0)
        coords.append(values)

    return dict(type='MultiLineString', coordinates=coords)


def _load_multipolygon(big_endian, type_bytes, data_bytes, ctx):
    endian_token = '>' if big_endian else '<'

    is_m = False

    if type_bytes in WKB_2D.values():
        num_dims = 2
    elif type_bytes in WKB_Z.values():
        num_dims = 3
    elif type_bytes in WKB_M.values():
        num_dims = 3
        is_m = True
    elif type_bytes in WKB_ZM.values():
        num_dims = 4

    if is_m:
        dim = 'M'
    else:
        dim = _INT_TO_DIM_LABEL[num_dims]

    num_polys = _read_count(endian_token, data_bytes, ctx)
    coords = []
    for polygon_index in range(num_polys):
        polygon = []
        poly_endian = _read_exact(1, data_bytes, ctx)
        poly_type = _read_exact(4, data_bytes, ctx)
        if big_endian:
            assert poly_endian == BIG_ENDIAN
            assert poly_type == _WKB[dim]['Polygon']
        else:
            assert poly_endian == LITTLE_ENDIAN
            assert poly_type[::-1] == _WKB[dim]['Polygon']

        num_rings = _read_count(endian_token, data_bytes, ctx)
        ctx.layer.append('polygon[%d]' % polygon_index)
        try:
            for ring_index in range(num_rings):
                ring = []
                num_verts = _read_count(endian_token, data_bytes, ctx)

                ctx.layer.append('ring[%d]' % ring_index)
                try:
                    ctx.reserve_vertices(num_verts)
                    for _ in range(num_verts):
                        vert_wkb = _read_exact(8 * num_dims, data_bytes, ctx)
                        fmt = '%s' + 'd' * num_dims
                        vert = list(struct.unpack(fmt % endian_token,
                                                  vert_wkb))
                        if is_m:
                            vert.insert(2, 0.0)
                        ring.append(vert)
                finally:
                    ctx.layer.pop()

                polygon.append(ring)
        finally:
            ctx.layer.pop()

        coords.append(polygon)

    return dict(type='MultiPolygon', coordinates=coords)


def _check_dimensionality(geom, num_dims):
    def first_geom(gc):
        for g in gc['geometries']:
            if not g['type'] == 'GeometryCollection':
                return g

    first_vert = {
        'Point': lambda x: x['coordinates'],
        'LineString': lambda x: x['coordinates'][0],
        'Polygon': lambda x: x['coordinates'][0][0],
        'MultiLineString': lambda x: x['coordinates'][0][0],
        'MultiPolygon': lambda x: x['coordinates'][0][0][0],
        'GeometryCollection': first_geom,
    }
    if not len(first_vert[geom['type']](geom)) == num_dims:
        error = 'Cannot mix dimensionality in a geometry'
        raise Exception(error)


def _load_geometrycollection(big_endian, type_bytes, data_bytes, ctx):
    endian_token = '>' if big_endian else '<'

    is_m = False

    if type_bytes in WKB_2D.values():
        num_dims = 2
    elif type_bytes in WKB_Z.values():
        num_dims = 3
    elif type_bytes in WKB_M.values():
        num_dims = 3
        is_m = True
    elif type_bytes in WKB_ZM.values():
        num_dims = 4

    geometries = []
    num_geoms = _read_count(endian_token, data_bytes, ctx)
    for geom_index in range(num_geoms):
        ctx.layer.append('geometry[%d]' % geom_index)
        try:
            geometry, _, _ = _loads_geometry(data_bytes, ctx)
        finally:
            ctx.layer.pop()
        if is_m:
            _check_dimensionality(geometry, 4)
        else:
            _check_dimensionality(geometry, num_dims)
        # TODO(LB): Add type assertions for the geometry; collections should
        # not mix 2d, 3d, 4d, etc.
        geometries.append(geometry)

    return dict(type='GeometryCollection', geometries=geometries)


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
    'Point': _load_point,
    'LineString': _load_linestring,
    'Polygon': _load_polygon,
    'MultiPoint': _load_multipoint,
    'MultiLineString': _load_multilinestring,
    'MultiPolygon': _load_multipolygon,
    'GeometryCollection': _load_geometrycollection,
}

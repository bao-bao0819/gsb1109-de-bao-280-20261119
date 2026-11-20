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
import itertools
import collections.abc as collections


def block_splitter(data, block_size):
    """
    Creates a generator by slicing ``data`` into chunks of ``block_size``.

    >>> data = range(10)
    >>> list(block_splitter(data, 2))
    [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9]]

    If ``data`` cannot be evenly divided by ``block_size``, the last block will
    simply be the remainder of the data. Example:

    >>> data = range(10)
    >>> list(block_splitter(data, 3))
    [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]

    If the ``block_size`` is greater than the total length of ``data``, a
    single block will be generated:

    >>> data = range(3)
    >>> list(block_splitter(data, 4))
    [[0, 1, 2]]

    :param data:
        Any iterable. If ``data`` is a generator, it will be exhausted,
        obviously.
    :param int block_size:
        Desired (maximum) block size.
    """
    buf = []
    for i, datum in enumerate(data):
        buf.append(datum)
        if len(buf) == block_size:
            yield buf
            buf = []

    # If there's anything leftover (a partial block),
    # yield it as well.
    if buf:
        yield buf


def take(n, iterable):
    """
    Return first n items of the iterable as a list

    Copied shamelessly from
    http://docs.python.org/2/library/itertools.html#recipes.
    """
    return list(itertools.islice(iterable, n))


def as_bin_str(a_list):
    return bytes(a_list)


def round_geom(geom, precision=None):
    """Round coordinates of a geometric object to given precision."""
    if geom['type'] == 'Point':
        x, y = geom['coordinates']
        xp, yp = [x], [y]
        if precision is not None:
            xp = [round(v, precision) for v in xp]
            yp = [round(v, precision) for v in yp]
        new_coords = tuple(zip(xp, yp))[0]
    if geom['type'] in ['LineString', 'MultiPoint']:
        xp, yp = zip(*geom['coordinates'])
        if precision is not None:
            xp = [round(v, precision) for v in xp]
            yp = [round(v, precision) for v in yp]
        new_coords = tuple(zip(xp, yp))
    elif geom['type'] in ['Polygon', 'MultiLineString']:
        new_coords = []
        for piece in geom['coordinates']:
            xp, yp = zip(*piece)
            if precision is not None:
                xp = [round(v, precision) for v in xp]
                yp = [round(v, precision) for v in yp]
            new_coords.append(tuple(zip(xp, yp)))
    elif geom['type'] == 'MultiPolygon':
        parts = geom['coordinates']
        new_coords = []
        for part in parts:
            inner_coords = []
            for ring in part:
                xp, yp = zip(*ring)
                if precision is not None:
                    xp = [round(v, precision) for v in xp]
                    yp = [round(v, precision) for v in yp]
                inner_coords.append(tuple(zip(xp, yp)))
            new_coords.append(inner_coords)
    return {'type': geom['type'], 'coordinates': new_coords}


def flatten_multi_dim(sequence):
    """Flatten a multi-dimensional array-like to a single dimensional sequence
    (as a generator).
    """
    for x in sequence:
        if (isinstance(x, collections.Iterable)
                and not isinstance(x, str)):
            for y in flatten_multi_dim(x):
                yield y
        else:
            yield x


class MixedDimensionalityError(ValueError):
    """
    Raised when the vertices of a single geometry are not uniform in the
    number of coordinate values they carry (for example, a polygon ring whose
    exterior vertices are XYZ while one interior vertex is only XY).

    The ``path`` records how to reach the offending vertex from the top-level
    object. Positions are 1-based and describe the GeoJSON nesting, for
    example ``MultiPolygon -> polygon[2] -> ring[2] -> vertex[2]`` or, for a
    member of a collection,
    ``GeometryCollection -> MultiPolygon[2] -> ...``.

    :ivar path: The list of path components from the root geometry to the
        offending vertex.
    :ivar expected_dims: The number of coordinate values every preceding
        vertex in the same uniform scope used.
    :ivar actual_dims: The number of coordinate values found on the
        offending vertex.
    """

    def __init__(self, path, expected_dims, actual_dims):
        self.path = list(path)
        self.expected_dims = expected_dims
        self.actual_dims = actual_dims
        location = ' -> '.join(self.path)
        message = (
            'Cannot mix dimensionality in a geometry: expected %d '
            'coordinate value(s) per vertex but found %d at %s'
            % (expected_dims, actual_dims, location)
        )
        super(MixedDimensionalityError, self).__init__(message)


def validate_geometry(geom):
    """
    Validate structural rules shared by every geometry writer.

    The check currently enforces that coordinate vertices which must share a
    dimensionality carry the same number of coordinate values:

    - Every vertex of a Point, LineString or MultiPoint is uniform.
    - Every vertex within a polygon ring is uniform.
    - The rings of a (Multi)Polygon and the parts of a MultiLineString are
      each validated independently.
    - GeometryCollections are descended into member by member. Sibling
      members are separate geometries and may legitimately differ in
      dimensionality; only the contents of each member are validated.

    Empty geometries (and empty nested containers) hold no vertices and
    therefore never trigger a dimensionality error.

    :raises MixedDimensionalityError: if a vertex breaks the dimensionality
        of the scope it belongs to. The exception's ``path`` locates the
        offending polygon, ring and vertex (and collection member, when
        applicable).
    """
    _validate_geometry(geom, [geom['type']])


def _validate_geometry(geom, path):
    geom_type = geom['type']
    if geom_type == 'GeometryCollection':
        for index, member in enumerate(geom['geometries']):
            member_path = path + ['%s[%d]' % (member['type'], index + 1)]
            _validate_geometry(member, member_path)
        return

    coordinates = geom['coordinates']
    if geom_type == 'Point' or len(coordinates) == 0:
        # A Point is a single vertex, and empty containers carry no
        # vertices; neither can be internally inconsistent.
        return

    if geom_type == 'LineString':
        _require_uniform_vertices(coordinates, path, 'vertex')
    elif geom_type == 'MultiPoint':
        _require_uniform_vertices(coordinates, path, 'point')
    elif geom_type == 'Polygon':
        _validate_rings(coordinates, path)
    elif geom_type == 'MultiLineString':
        for index, linestring in enumerate(coordinates):
            line_path = path + ['linestring[%d]' % (index + 1)]
            _require_uniform_vertices(linestring, line_path, 'vertex')
    elif geom_type == 'MultiPolygon':
        for index, polygon in enumerate(coordinates):
            polygon_path = path + ['polygon[%d]' % (index + 1)]
            _validate_rings(polygon, polygon_path)
    else:
        raise ValueError("Unsupported geometry type '%s'" % geom_type)


def _validate_rings(rings, path):
    for index, ring in enumerate(rings):
        ring_path = path + ['ring[%d]' % (index + 1)]
        _require_uniform_vertices(ring, ring_path, 'vertex')


def _require_uniform_vertices(vertices, path, label):
    expected_dims = None
    for index, vertex in enumerate(vertices):
        dims = len(vertex)
        if expected_dims is None:
            expected_dims = dims
        elif dims != expected_dims:
            raise MixedDimensionalityError(
                path + ['%s[%d]' % (label, index + 1)],
                expected_dims,
                dims,
            )


def endian_token(is_little_endian):
    if is_little_endian:
        return '<'
    else:
        return '>'

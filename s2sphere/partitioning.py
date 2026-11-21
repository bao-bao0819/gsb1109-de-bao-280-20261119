"""Chunked S2 partition-key ingestion with optional fast XYZ routing.

The fixed :mod:`s2sphere` library is always treated as the authority for
routing a latitude/longitude to an :class:`~s2sphere.CellId` and for taking
parent cells.  A data source may additionally supply quickly computed XYZ
samples whose three float components have known absolute error bounds.  Those
samples are only trusted for a row when *every* direction compatible with the
error bounds routes to the same requested cell; otherwise the row falls back
to the reliable scalar path.

The module supports two error models:

* :class:`FastSample` -- three independent, per-axis absolute errors;
* :class:`DeviceSample` together with a :class:`DeviceHeader` -- a device
  batch whose samples share an unknown scalar bias ``b`` applied through fixed
  per-axis gains (for example ``b, b, b/4``) on top of per-observation axis
  errors.  The header's scalar-confirmed calibration points narrow ``b`` to a
  feasible interval before any record may use the fast path.

Inputs are consumed from an iterator of unknown length in fixed-size chunks
so that each chunk can be written to a table immediately.
"""

from __future__ import absolute_import, division, print_function

import math
from itertools import islice

from future.builtins import int

from .sphere import CellId, LatLng, Point


#: A row may ask for any S2 level; when omitted the leaf level is used.
DEFAULT_LEVEL = CellId.MAX_LEVEL


class RouteSource(object):
    """Labels the path that produced a row's partition key."""

    #: The fast XYZ sample was proven to share the scalar cell, so the row may
    #: be written from the fast data.
    FAST = 'fast'

    #: The fast sample could not guarantee ownership (or was not supplied), so
    #: the fixed library routed the row.
    SCALAR = 'scalar'


class PartitionError(ValueError):
    """Raised (and attached to a row result) for invalid input records."""

    NON_FINITE = 'non_finite'
    INVALID_COORDINATE = 'invalid_coordinate'
    INVALID_LEVEL = 'invalid_level'

    def __init__(self, code, message):
        super(PartitionError, self).__init__(message)
        self.code = code


class FastSample(object):
    """A fast XYZ estimate with independent per-axis absolute errors.

    The true point lies within the axis-aligned box

    ``[x - ex, x + ex] x [y - ey, y + ey] x [z - ez, z + ez]``.
    """

    def __init__(self, point, error):
        if isinstance(point, Point):
            point = (point[0], point[1], point[2])
        self.point = Point(point[0], point[1], point[2])
        if isinstance(error, (int, float)):
            error = (error, error, error)
        if len(error) != 3 or any(e < 0 for e in error):
            raise ValueError('error must be three non-negative bounds')
        self.error = tuple(float(e) for e in error)

    def is_finite(self):
        return all(_is_finite(v) for v in
                   (self.point[0], self.point[1], self.point[2]))

    def generators(self, resolver):
        """Zonotope generators for the independent per-axis box."""
        ex, ey, ez = self.error
        return [
            ((-1.0, 0.0, 0.0), -ex, ex),
            ((0.0, -1.0, 0.0), -ey, ey),
            ((0.0, 0.0, -1.0), -ez, ez),
        ]


class DeviceHeader(object):
    """Batch header describing a device's shared bias and calibration.

    :param device_id: identifier of the device batch.
    :param version: header version; the same device may ship several.
    :param axis_gains: gains ``(gx, gy, gz)`` mapping the scalar bias ``b``
        onto the three axes, e.g. ``(1.0, 1.0, 0.25)``.
    :param b_range: ``(low, high)`` initial feasible interval for ``b``.
    :param calibration: iterable of ``(fast_xyz, trusted_point)`` pairs already
        confirmed against the scalar library.  ``fast_xyz`` is a tuple or
        :class:`Point` and ``trusted_point`` is a :class:`Point`.
    :param calibration_error: absolute bound on each residual axis component
        of the calibration points.
    """

    def __init__(self, device_id, version, axis_gains, b_range,
                 calibration, calibration_error):
        if len(axis_gains) != 3:
            raise ValueError('axis_gains must have three components')
        self.device_id = device_id
        self.version = version
        self.axis_gains = tuple(float(g) for g in axis_gains)
        self.b_range = (float(b_range[0]), float(b_range[1]))
        if self.b_range[0] > self.b_range[1]:
            raise ValueError('b_range low must not exceed high')
        self.calibration = []
        for fast_xyz, trusted in calibration:
            if isinstance(fast_xyz, Point):
                fast_xyz = (fast_xyz[0], fast_xyz[1], fast_xyz[2])
            self.calibration.append((tuple(fast_xyz), trusted))
        self.calibration_error = float(calibration_error)


class DeviceSample(FastSample):
    """A fast XYZ sample belonging to a calibrated device batch."""

    def __init__(self, point, error, device_id, version):
        super(DeviceSample, self).__init__(point, error)
        self.device_id = device_id
        self.version = version

    def generators(self, resolver):
        model = resolver.calibrated_model(self.device_id, self.version)
        if model is None:
            # The matching header has not been delivered: never borrow a bias
            # from another device or version, so the fast path is unavailable.
            return None
        axis_gains, b_low, b_high = model
        gx, gy, gz = axis_gains
        ex, ey, ez = self.error
        return [
            ((-gx, -gy, -gz), b_low, b_high),
            ((-1.0, 0.0, 0.0), -ex, ex),
            ((0.0, -1.0, 0.0), -ey, ey),
            ((0.0, 0.0, -1.0), -ez, ez),
        ]


class PartitionResult(object):
    """The resolved partition key for one input row.

    ``cell_id`` is ``None`` when ``error`` is set, otherwise it is the
    authoritative :class:`~s2sphere.CellId` at the requested level.
    """

    __slots__ = ('index', 'obs_id', 'level', 'cell_id', 'source', 'error')

    def __init__(self, index, obs_id, level, cell_id, source, error):
        self.index = index
        self.obs_id = obs_id
        self.level = level
        self.cell_id = cell_id
        self.source = source
        self.error = error

    @property
    def face(self):
        return None if self.cell_id is None else self.cell_id.face()

    @property
    def ok(self):
        return self.error is None

    def __repr__(self):
        return ('PartitionResult(index={!r}, obs_id={!r}, level={!r}, '
                'cell_id={!r}, source={!r}, error={!r})').format(
                    self.index, self.obs_id, self.level, self.cell_id,
                    self.source, self.error)


class PartitionResolver(object):
    """Resolves rows to partition keys and retains device calibrations."""

    def __init__(self, default_level=DEFAULT_LEVEL):
        self.default_level = default_level
        # key (device_id, version) -> (axis_gains, b_low, b_high)
        self._models = {}

    def register_header(self, header):
        """Calibrate a device header and remember its feasible bias range."""
        b_low, b_high = header.b_range
        gains = header.axis_gains
        bound = header.calibration_error
        for fast_xyz, trusted in header.calibration:
            for axis in range(3):
                residual = fast_xyz[axis] - trusted[axis]
                gain = gains[axis]
                if gain == 0.0:
                    if abs(residual) > bound:
                        raise PartitionError(
                            PartitionError.INVALID_COORDINATE,
                            'calibration point violates zero-gain axis bound')
                    continue
                # residual = gain * b + epsilon, |epsilon| <= bound
                candidates = (
                    (residual - bound) / gain,
                    (residual + bound) / gain,
                )
                b_low = max(b_low, min(candidates))
                b_high = min(b_high, max(candidates))
        if b_low > b_high:
            raise PartitionError(
                PartitionError.INVALID_COORDINATE,
                'calibration points admit no shared bias for device {!r} '
                'version {!r}'.format(header.device_id, header.version))
        self._models[(header.device_id, header.version)] = (
            gains, b_low, b_high)

    def calibrated_model(self, device_id, version):
        return self._models.get((device_id, version))

    def resolve(self, row, index):
        obs_id = row.get('obs_id')
        level = row.get('level', self.default_level)

        level_error = _validate_level(level)
        coordinate_error = _validate_coordinates(row.get('lat'),
                                                 row.get('lng'))
        if level_error is not None or coordinate_error is not None:
            return PartitionResult(
                index, obs_id, level if level_error is None else None,
                None, None, level_error or coordinate_error)

        lat_lng = LatLng.from_degrees(row['lat'], row['lng'])
        reference = CellId.from_lat_lng(lat_lng).parent(level)
        sample = row.get('fast')

        if not _sample_is_trustworthy(sample, resolver=self):
            return PartitionResult(index, obs_id, level, reference,
                                   RouteSource.SCALAR, None)

        if _guarantees_cell(sample, reference, level, self):
            return PartitionResult(index, obs_id, level, reference,
                                   RouteSource.FAST, None)
        return PartitionResult(index, obs_id, level, reference,
                               RouteSource.SCALAR, None)


def partition_row(row, level=DEFAULT_LEVEL, resolver=None):
    """Resolve a single row dict to a :class:`PartitionResult`.

    Pass a :class:`PartitionResolver` that already holds the relevant
    :class:`DeviceHeader` when the row carries a :class:`DeviceSample`.
    """
    resolver = resolver or PartitionResolver(default_level=level)
    return resolver.resolve(row, index=row.get('index', 0))


def partition_rows(rows, chunk_size=1024, level=DEFAULT_LEVEL, resolver=None):
    """Yield successive list batches of partition results.

    :param rows: iterable of row dicts (and optionally :class:`DeviceHeader`
        markers) of any length; only the current chunk is materialized.
    :param chunk_size: positive number of input records pulled per batch.
    :param level: default S2 level used when a row does not specify one.
    :param resolver: an existing :class:`PartitionResolver`; a fresh one is
        created when omitted.  Pass one in to retain calibration state across
        several calls.
    :returns: generator yielding a list of :class:`PartitionResult` per chunk.
    """
    if not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError('chunk_size must be a positive integer')
    resolver = resolver or PartitionResolver(default_level=level)
    iterator = iter(rows)
    index = 0
    while True:
        batch = list(islice(iterator, chunk_size))
        if not batch:
            return
        results = []
        for row in batch:
            if isinstance(row, DeviceHeader):
                resolver.register_header(row)
                continue
            results.append(resolver.resolve(row, index))
            index += 1
        yield results


def partition_all(rows, chunk_size=1024, level=DEFAULT_LEVEL, resolver=None):
    """Convenience wrapper that flattens :func:`partition_rows`."""
    for batch in partition_rows(rows, chunk_size, level, resolver):
        for result in batch:
            yield result


def _is_finite(value):
    return (isinstance(value, (int, float)) and
            not math.isnan(value) and not math.isinf(value))


def _validate_level(level):
    if not isinstance(level, int) or isinstance(level, bool):
        return PartitionError(PartitionError.INVALID_LEVEL,
                              'level must be an integer')
    if level < 0 or level > CellId.MAX_LEVEL:
        return PartitionError(
            PartitionError.INVALID_LEVEL,
            'level {} outside 0..{}'.format(level, CellId.MAX_LEVEL))
    return None


def _validate_coordinates(lat, lng):
    if not (_is_finite(lat) and _is_finite(lng)):
        return PartitionError(PartitionError.NON_FINITE,
                              'latitude and longitude must be finite')
    if not LatLng.from_degrees(lat, lng).is_valid():
        return PartitionError(PartitionError.INVALID_COORDINATE,
                              'latitude/longitude outside valid range')
    return None


def _sample_is_trustworthy(sample, resolver):
    if not isinstance(sample, FastSample) or not sample.is_finite():
        return False
    if isinstance(sample, DeviceSample):
        return sample.generators(resolver) is not None
    return True


def _guarantees_cell(sample, reference, level, resolver):
    generators = sample.generators(resolver)
    if generators is None:
        return False
    # Every direction compatible with the error bounds must route to the same
    # cell as the scalar reference.  The (possibly biased) fast centre itself
    # is not required to lie in the cell; only feasible true points matter.
    for vertex in _zonotope_vertices(sample.point, generators):
        if CellId.from_point(vertex).parent(level) != reference:
            return False
    return True


def _zonotope_vertices(center, generators):
    """All vertices of the bounded zonotope center + sum [lo, hi] * vector."""
    vertices = [center]
    for vector, low, high in generators:
        grown = []
        for vertex in vertices:
            grown.append(Point(vertex[0] + vector[0] * low,
                               vertex[1] + vector[1] * low,
                               vertex[2] + vector[2] * low))
            grown.append(Point(vertex[0] + vector[0] * high,
                               vertex[1] + vector[1] * high,
                               vertex[2] + vector[2] * high))
        vertices = grown
    return vertices

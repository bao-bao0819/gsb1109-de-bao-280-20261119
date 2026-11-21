from __future__ import print_function, unicode_literals, division

import math
import random
import unittest

from s2sphere import CellId, LatLng, Point
from s2sphere import (
    DEFAULT_LEVEL,
    DeviceHeader,
    DeviceSample,
    FastSample,
    PartitionError,
    PartitionResolver,
    RouteSource,
    partition_all,
    partition_row,
    partition_rows,
)
from s2sphere.sphere import face_uv_to_xyz, xyz_to_face_uv


# A deep fine level and a coarser parent level used by the edge fixtures.
FINE_LEVEL = 20
COARSE_LEVEL = 14
FINE_GRID = 1 << FINE_LEVEL
# A level-20 grid line safely inside a face, away from coarse-cell borders.
EDGE_S_LINE = (FINE_GRID // 3) / FINE_GRID

AXIS_GAINS = (1.0, 1.0, 0.25)


def st_point(face, s, t):
    u = CellId.st_to_uv(s)
    v = CellId.st_to_uv(t)
    return face_uv_to_xyz(face, u, v).normalize()


def shift_s(point, delta_s):
    """Return a unit point shifted by delta_s along the face s-axis."""
    face, u, v = xyz_to_face_uv(point)
    s = CellId.uv_to_st(u)
    t = CellId.uv_to_st(v)
    return st_point(face, s + delta_s, t)


def _canonical(point):
    lat_lng = LatLng.from_point(point)
    return LatLng.from_degrees(lat_lng.lat().degrees,
                               lat_lng.lng().degrees).to_point()


def row(obs_id, point, level=None, fast=None):
    lat_lng = LatLng.from_point(point)
    data = {
        'obs_id': obs_id,
        'lat': lat_lng.lat().degrees,
        'lng': lat_lng.lng().degrees,
    }
    if level is not None:
        data['level'] = level
    if fast is not None:
        data['fast'] = fast
    return data


def scalar_cell(point, level):
    return CellId.from_point(point).parent(level)


class CountingIterator(object):
    """Iterator that records how many items were actually pulled."""

    def __init__(self, items):
        self._items = iter(items)
        self.pulled = 0
        self.exhausted = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            value = next(self._items)
        except StopIteration:
            self.exhausted = True
            raise
        self.pulled += 1
        return value

    next = __next__


def signature(results):
    return tuple(
        (r.index, r.obs_id,
         None if r.cell_id is None else r.cell_id.id(),
         r.source,
         None if r.error is None else r.error.code)
        for r in results
    )


class TestScalarChunking(unittest.TestCase):

    def test_default_and_explicit_level_match_scalar_library(self):
        point = LatLng.from_degrees(37.794, -122.408).to_point()
        [result] = list(partition_all([row('a', point)]))
        self.assertEqual(result.level, DEFAULT_LEVEL)
        self.assertEqual(result.cell_id, scalar_cell(point, DEFAULT_LEVEL))
        self.assertEqual(result.source, RouteSource.SCALAR)

        [coarse] = list(partition_all([row('a', point, level=10)]))
        self.assertEqual(coarse.cell_id, scalar_cell(point, 10))
        self.assertEqual(coarse.cell_id.level(), 10)

    def test_single_point_entry_matches_chunked_path(self):
        point = LatLng.from_degrees(12.3, -45.6).to_point()
        single = partition_row(row('s', point, level=13))
        [batched] = list(partition_all([row('s', point, level=13)]))
        self.assertEqual(single.cell_id, batched.cell_id)
        self.assertEqual(single.source, batched.source)
        # The single-point entry keeps working without any fast calculator.
        self.assertEqual(partition_row(row('s', point)).source,
                         RouteSource.SCALAR)

    def test_duplicate_observation_ids_are_all_retained(self):
        points = [LatLng.from_degrees(lat, 10.0).to_point()
                  for lat in (1.0, 2.0, 3.0)]
        rows = [row('dup', point, level=8) for point in points]
        results = list(partition_all(rows))
        self.assertEqual(len(results), 3)
        self.assertEqual([r.obs_id for r in results], ['dup', 'dup', 'dup'])
        self.assertEqual([r.index for r in results], [0, 1, 2])

    def test_invalid_rows_keep_index_and_their_own_error(self):
        good = LatLng.from_degrees(10.0, 10.0).to_point()
        rows = [
            row('bad-lat', LatLng.from_degrees(0.0, 0.0).to_point(), level=5),
            row('bad-lat', LatLng.from_degrees(0.0, 0.0).to_point(), level=5),
            row('ok', good, level=5),
            row('bad-level', good, level=31),
            row('neg-level', good, level=-1),
            row('nan-lat', good, level=5),
            row('inf-lng', good, level=5),
        ]
        rows[0]['lat'] = 91.0
        rows[1]['lat'] = math.nan
        rows[3]['level'] = 31
        rows[5]['lat'] = math.nan
        rows[6]['lng'] = math.inf

        results = list(partition_all(rows))
        self.assertEqual([r.index for r in results], list(range(7)))

        self.assertEqual(results[0].error.code,
                         PartitionError.INVALID_COORDINATE)
        self.assertIsNone(results[0].cell_id)
        self.assertEqual(results[1].error.code, PartitionError.NON_FINITE)
        self.assertEqual(results[5].error.code, PartitionError.NON_FINITE)
        self.assertEqual(results[6].error.code, PartitionError.NON_FINITE)
        for bad in (3, 4):
            self.assertEqual(results[bad].error.code,
                             PartitionError.INVALID_LEVEL)
            self.assertIsNone(results[bad].cell_id)

        self.assertIsNone(results[2].error)
        self.assertEqual(results[2].cell_id, scalar_cell(good, 5))

    def test_non_integer_level_is_rejected(self):
        point = LatLng.from_degrees(0.0, 0.0).to_point()
        data = row('x', point)
        data['level'] = '10'
        result = partition_row(data)
        self.assertEqual(result.error.code, PartitionError.INVALID_LEVEL)

    def test_results_are_independent_of_chunk_size(self):
        rng = random.Random(123)
        points = [LatLng.from_degrees(rng.uniform(-80, 80),
                                      rng.uniform(-170, 170)).to_point()
                  for _ in range(29)]
        rows = [row('p{}'.format(i), point, level=rng.choice([0, 6, 14, 30]))
                for i, point in enumerate(points)]

        reference = signature(list(partition_all(rows)))
        for chunk_size in (1, 2, 3, 7, 13, 100):
            batches = list(partition_rows(rows, chunk_size=chunk_size))
            self.assertTrue(all(isinstance(batch, list) for batch in batches))
            self.assertTrue(all(len(batch) <= chunk_size for batch in batches))
            self.assertEqual(signature([r for batch in batches
                                        for r in batch]), reference)

    def test_taking_first_chunk_does_not_read_the_next_chunk(self):
        points = [LatLng.from_degrees(float(i), 0.0).to_point()
                  for i in range(10)]
        rows = [row(i, point, level=4) for i, point in enumerate(points)]

        source = CountingIterator(rows)
        batches = partition_rows(source, chunk_size=3)
        first = next(batches)
        self.assertEqual(len(first), 3)
        self.assertEqual(source.pulled, 3)
        self.assertFalse(source.exhausted)
        # No eager pull beyond the delivered chunk.
        self.assertEqual(source.pulled, 3)

    def test_full_consumption_chunk_size_independent_and_ordered(self):
        rng = random.Random(9)
        rows = [row(i, LatLng.from_degrees(rng.uniform(-70, 70),
                                           rng.uniform(-170, 170)).to_point(),
                    level=11)
                for i in range(17)]
        flattened = {
            size: signature(list(partition_all(rows, chunk_size=size)))
            for size in (1, 4, 8, 17)}
        for size, sig in flattened.items():
            self.assertEqual(sig, flattened[1])
            self.assertEqual([item[0] for item in sig], list(range(17)))


class TestFastGuarantee(unittest.TestCase):

    def setUp(self):
        # Trusted points near a deep level-20 edge and near a cell centre.
        boundary_raw = st_point(0, EDGE_S_LINE + 0.25 / FINE_GRID, 0.42)
        nearby_raw = st_point(0, EDGE_S_LINE + 0.47 / FINE_GRID, 0.421)
        # Canonicalize through degrees, exactly as an ingested row does.
        self.boundary = _canonical(boundary_raw)
        self.nearby = _canonical(nearby_raw)

        # Per-axis XYZ envelopes verified against the guarantee engine: the
        # boundary envelope reaches fine cells on both sides of the level-20
        # edge but stays inside a single coarse cell; the nearby envelope is
        # tight enough to stay in one fine cell.
        self.boundary_error = (0.5 / FINE_GRID,) * 3
        self.nearby_error = (0.05 / FINE_GRID,) * 3
        # Different in-envelope fast centres delivered across chunk sizes.
        self.boundary_centres = [
            shift_s(self.boundary, d / FINE_GRID) for d in (-0.2, 0.0, 0.2)]
        self.nearby_centres = [
            shift_s(self.nearby, d / FINE_GRID) for d in (-0.03, 0.0, 0.03)]

    def test_safe_ordinary_row_uses_fast_data(self):
        point = LatLng.from_degrees(-33.8, 151.2).to_point()
        sample = FastSample(point, 1e-12)
        [result] = list(partition_all([row('safe', point, level=12,
                                           fast=sample)]))
        self.assertEqual(result.source, RouteSource.FAST)
        self.assertEqual(result.cell_id, scalar_cell(point, 12))

    def test_fast_path_visible_with_unique_coordinates(self):
        rng = random.Random(55)
        coords = set()
        rows = []
        while len(rows) < 12:
            lat = rng.uniform(-70, 70)
            lng = rng.uniform(-170, 170)
            if (lat, lng) in coords:
                continue
            coords.add((lat, lng))
            point = LatLng.from_degrees(lat, lng).to_point()
            rows.append(row('u{}'.format(len(rows)), point, level=10,
                            fast=FastSample(point, 1e-13)))
        results = list(partition_all(rows))
        self.assertTrue(all(r.source == RouteSource.FAST for r in results))
        self.assertEqual(len({(r.obs_id) for r in results}), 12)

    def test_boundary_row_never_changes_ownership_from_fast_bias(self):
        from s2sphere.partitioning import _guarantees_cell, _zonotope_vertices
        resolver = PartitionResolver()
        fine_ref = scalar_cell(self.boundary, FINE_LEVEL)
        coarse_ref = scalar_cell(self.boundary, COARSE_LEVEL)
        envelope = FastSample(self.boundary, self.boundary_error)
        vertices = _zonotope_vertices(envelope.point,
                                      envelope.generators(resolver))
        # The feasible directions cross the level-20 edge (more than one fine
        # cell) but stay inside a single coarse cell.
        self.assertGreater(len({scalar_cell(v, FINE_LEVEL).id()
                                for v in vertices}), 1)
        self.assertEqual(len({scalar_cell(v, COARSE_LEVEL).id()
                              for v in vertices}), 1)
        self.assertFalse(_guarantees_cell(envelope, fine_ref,
                                          FINE_LEVEL, resolver))
        self.assertTrue(_guarantees_cell(envelope, coarse_ref,
                                         COARSE_LEVEL, resolver))

        for centre in self.boundary_centres:
            sample = FastSample(centre, self.boundary_error)
            fine_row = row('edge', self.boundary, level=FINE_LEVEL,
                           fast=sample)
            coarse_row = row('edge', self.boundary, level=COARSE_LEVEL,
                             fast=sample)
            fine_result = partition_row(fine_row)
            coarse_result = partition_row(coarse_row)
            # Fine ownership cannot be guaranteed, but the scalar cell is
            # returned unchanged no matter which side the fast value favours.
            self.assertEqual(fine_result.source, RouteSource.SCALAR)
            self.assertEqual(fine_result.cell_id, fine_ref)
            # Coarse ownership is guaranteed, so fast data is acceptable.
            self.assertEqual(coarse_result.source, RouteSource.FAST)
            self.assertEqual(coarse_result.cell_id, coarse_ref)

    def test_nearby_row_is_fast_at_fine_level(self):
        fine_ref = scalar_cell(self.nearby, FINE_LEVEL)
        for centre in self.nearby_centres:
            result = partition_row(
                row('near', self.nearby, level=FINE_LEVEL,
                    fast=FastSample(centre, self.nearby_error)))
            self.assertEqual(result.source, RouteSource.FAST)
            self.assertEqual(result.cell_id, fine_ref)

    def test_swapping_requested_levels_keeps_scalar_ownership(self):
        # First configuration: boundary asks for the fine level, nearby for
        # the coarse level.  Then exchange the two requested levels.
        configs = [
            (FINE_LEVEL, COARSE_LEVEL),
            (COARSE_LEVEL, FINE_LEVEL),
        ]
        for boundary_level, nearby_level in configs:
            boundary_sample = FastSample(self.boundary_centres[0],
                                         self.boundary_error)
            nearby_sample = FastSample(self.nearby_centres[0],
                                       self.nearby_error)
            boundary = partition_row(row('edge', self.boundary,
                                         level=boundary_level,
                                         fast=boundary_sample))
            nearby = partition_row(row('near', self.nearby,
                                       level=nearby_level,
                                       fast=nearby_sample))
            self.assertEqual(boundary.cell_id,
                             scalar_cell(self.boundary, boundary_level))
            self.assertEqual(nearby.cell_id,
                             scalar_cell(self.nearby, nearby_level))
            self.assertEqual(boundary.source,
                             RouteSource.FAST if boundary_level == COARSE_LEVEL
                             else RouteSource.SCALAR)
            self.assertEqual(nearby.source, RouteSource.FAST)

    def test_different_fast_values_and_chunk_sizes_agree(self):
        # Deliver a different in-envelope fast value per stream, then repeat
        # across chunk sizes; ownership must be identical every time.
        streams = []
        for centre in self.boundary_centres:
            streams.append([
                row('edge', self.boundary, level=FINE_LEVEL,
                    fast=FastSample(centre, self.boundary_error)),
                row('edge', self.boundary, level=COARSE_LEVEL,
                    fast=FastSample(centre, self.boundary_error)),
                row('near', self.nearby, level=FINE_LEVEL,
                    fast=FastSample(self.nearby_centres[-1],
                                    self.nearby_error)),
            ])
        baseline = signature(list(partition_all(streams[0])))
        for stream in streams[1:]:
            for chunk_size in (1, 2, 3, 5):
                self.assertEqual(
                    signature(list(partition_all(
                        stream, chunk_size=chunk_size))), baseline)

    def test_face_seam_exact_seam_and_poles_match_scalar(self):
        from s2sphere.partitioning import _guarantees_cell
        resolver = PartitionResolver()
        # An exact cube-face seam point: the point-based guarantee agrees with
        # the fixed library routing (the lat/lng round-trip used by rows is
        # deliberately avoided for this exact tie).
        seam = Point(1.0, 1.0, 0.3).normalize()
        seam_ref = scalar_cell(seam, 6)
        self.assertEqual(seam_ref.face(), 1)
        self.assertTrue(_guarantees_cell(FastSample(seam, 0.0), seam_ref,
                                         6, resolver))

        # Points on either side of the seam, plus the two poles, go through
        # the normal lat/lng row ingestion path.
        for obs, point, expected_face in (
                ('face0', Point(1.0 + 1e-9, 1.0, 0.3).normalize(), 0),
                ('face1', Point(1.0, 1.0 + 1e-9, 0.3).normalize(), 1),
                ('north', LatLng.from_degrees(90.0, 0.0).to_point(), 2),
                ('south', LatLng.from_degrees(-90.0, 0.0).to_point(), 5)):
            result = partition_row(row(obs, point, level=6,
                                       fast=FastSample(point, 0.0)))
            self.assertEqual(result.source, RouteSource.FAST)
            self.assertEqual(result.face, expected_face)
            self.assertEqual(result.cell_id, scalar_cell(point, 6))

        # An envelope wide enough to cross the seam cannot be trusted, yet the
        # returned ownership is still the scalar answer.
        face_zero = Point(1.0 + 1e-9, 1.0, 0.3).normalize()
        result = partition_row(row('face0', face_zero, level=6,
                                   fast=FastSample(face_zero, 0.01)))
        self.assertEqual(result.source, RouteSource.SCALAR)
        self.assertEqual(result.cell_id, scalar_cell(face_zero, 6))

    def test_non_finite_fast_sample_falls_back_to_scalar(self):
        point = LatLng.from_degrees(10.0, 10.0).to_point()
        sample = FastSample(Point(math.nan, point[1], point[2]), 1e-9)
        result = partition_row(row('x', point, level=10, fast=sample))
        self.assertEqual(result.source, RouteSource.SCALAR)
        self.assertEqual(result.cell_id, scalar_cell(point, 10))


def calibration_points(seed, bias, gains, count=4):
    rng = random.Random(seed)
    points = []
    for _ in range(count):
        trusted = LatLng.from_degrees(
            rng.uniform(-60, 60), rng.uniform(-170, 170)).to_point()
        fast = (trusted[0] + bias * gains[0],
                trusted[1] + bias * gains[1],
                trusted[2] + bias * gains[2])
        points.append((fast, trusted))
    return points


class TestDeviceCalibration(unittest.TestCase):

    def setUp(self):
        self.bias_one = 0.00061
        self.header_one_range = (-0.01, 0.01)
        self.header_one = DeviceHeader(
            'dev1', 1, AXIS_GAINS, self.header_one_range,
            calibration_points(7, self.bias_one, AXIS_GAINS), 5e-10)

        self.boundary = _canonical(
            st_point(0, EDGE_S_LINE + 0.25 / FINE_GRID, 0.42))
        self.nearby = _canonical(
            st_point(0, EDGE_S_LINE + 0.47 / FINE_GRID, 0.421))
        self.boundary_error = (0.5 / FINE_GRID,) * 3
        self.nearby_error = (0.05 / FINE_GRID,) * 3

        self.bias_two = 0.0008
        self.header_two_range = (-0.02, 0.02)
        self.header_two = DeviceHeader(
            'dev2', 1, AXIS_GAINS, self.header_two_range,
            calibration_points(11, self.bias_two, AXIS_GAINS), 8e-6)
        # Record C sits just on the face-0 side of the face 0/2 cube seam.
        self.record_c = _canonical(Point(0.8 + 7.1e-6, 0.25, 0.8).normalize())
        self.record_c_error = (5e-8,) * 3
        self.record_c_level = 12

    def _device_sample(self, point, error, device_id, version, bias):
        return DeviceSample(
            (point[0] + bias * AXIS_GAINS[0],
             point[1] + bias * AXIS_GAINS[1],
             point[2] + bias * AXIS_GAINS[2]),
            error, device_id, version)

    def test_header_narrows_bias_and_enables_fast_path(self):
        resolver = PartitionResolver()
        resolver.register_header(self.header_one)
        _, low, high = resolver.calibrated_model('dev1', 1)
        self.assertTrue(low <= self.bias_one <= high)
        self.assertLess(high - low,
                        self.header_one_range[1] - self.header_one_range[0])

        sample = self._device_sample(self.boundary, self.boundary_error,
                                     'dev1', 1, self.bias_one)
        coarse_ref = scalar_cell(self.boundary, COARSE_LEVEL)
        from s2sphere.partitioning import _guarantees_cell
        self.assertTrue(_guarantees_cell(
            sample, coarse_ref, COARSE_LEVEL, resolver))

        # The same record is not provable from the wide header range alone.
        wide = PartitionResolver()
        wide.register_header(DeviceHeader(
            'dev1', 1, AXIS_GAINS, self.header_one_range, [], 5e-10))
        self.assertFalse(_guarantees_cell(
            sample, coarse_ref, COARSE_LEVEL, wide))

        # And it is scalar when no header has been delivered at all.
        self.assertFalse(_guarantees_cell(
            sample, coarse_ref, COARSE_LEVEL, PartitionResolver()))

    def test_record_a_coarse_only_after_calibration(self):
        resolver = PartitionResolver()
        resolver.register_header(self.header_one)
        sample = self._device_sample(self.boundary, self.boundary_error,
                                     'dev1', 1, self.bias_one)
        coarse = resolver.resolve(
            row('A', self.boundary, level=COARSE_LEVEL, fast=sample), 0)
        fine = resolver.resolve(
            row('A', self.boundary, level=FINE_LEVEL, fast=sample), 1)
        self.assertEqual(coarse.source, RouteSource.FAST)
        self.assertEqual(coarse.cell_id,
                         scalar_cell(self.boundary, COARSE_LEVEL))
        self.assertEqual(fine.source, RouteSource.SCALAR)
        self.assertEqual(fine.cell_id,
                         scalar_cell(self.boundary, FINE_LEVEL))

    def test_record_b_guaranteed_at_fine_level(self):
        resolver = PartitionResolver()
        resolver.register_header(self.header_one)
        sample = self._device_sample(self.nearby, self.nearby_error,
                                     'dev1', 1, self.bias_one)
        result = resolver.resolve(
            row('B', self.nearby, level=FINE_LEVEL, fast=sample), 0)
        self.assertEqual(result.source, RouteSource.FAST)
        self.assertEqual(result.cell_id,
                         scalar_cell(self.nearby, FINE_LEVEL))

    def test_record_c_joint_feasibility_pins_single_face(self):
        from s2sphere.partitioning import _zonotope_vertices
        resolver = PartitionResolver()
        resolver.register_header(self.header_two)
        _, low, high = resolver.calibrated_model('dev2', 1)
        sample = self._device_sample(self.record_c, self.record_c_error,
                                     'dev2', 1, self.bias_two)
        reference = scalar_cell(self.record_c, self.record_c_level)

        # Per-axis independent extremes suggest both faces 0 and 2 are
        # individually reachable...
        independent = []
        for axis in range(3):
            span = [-AXIS_GAINS[axis] * high - self.record_c_error[axis],
                    -AXIS_GAINS[axis] * low + self.record_c_error[axis]]
            vector = [0.0, 0.0, 0.0]
            vector[axis] = 1.0
            independent.append((tuple(vector), min(span), max(span)))
        independent_faces = {
            CellId.from_point(vertex).face()
            for vertex in _zonotope_vertices(sample.point, independent)}
        self.assertEqual(independent_faces, {0, 2})

        # ...but every jointly feasible direction lands on face 0.
        joint_faces = {
            CellId.from_point(vertex).face()
            for vertex in _zonotope_vertices(
                sample.point, sample.generators(resolver))}
        self.assertEqual(joint_faces, {0})

        result = resolver.resolve(
            row('C', self.record_c, level=self.record_c_level,
                fast=sample), 0)
        self.assertEqual(result.source, RouteSource.FAST)
        self.assertEqual(result.face, 0)
        self.assertEqual(result.cell_id, reference)

        # With only the uncalibrated header range the ownership is not proven.
        wide = PartitionResolver()
        wide.register_header(DeviceHeader(
            'dev2', 1, AXIS_GAINS, self.header_two_range, [], 8e-6))
        wide_result = wide.resolve(
            row('C', self.record_c, level=self.record_c_level, fast=sample),
            0)
        self.assertEqual(wide_result.source, RouteSource.SCALAR)

    def test_no_bias_borrowed_across_device_or_version(self):
        resolver = PartitionResolver()
        resolver.register_header(self.header_one)
        foreign = self._device_sample(self.record_c, self.record_c_error,
                                      'dev2', 1, self.bias_two)
        # dev2 has not registered: must not borrow dev1's calibrated bias.
        self.assertEqual(
            resolver.resolve(row('foreign', self.record_c, level=12,
                                 fast=foreign), 0).source,
            RouteSource.SCALAR)

        version_two = self._device_sample(self.nearby, self.nearby_error,
                                          'dev1', 2, self.bias_one)
        # Version 2 of dev1 is unknown even though version 1 is registered.
        self.assertEqual(
            resolver.resolve(row('v2', self.nearby, level=FINE_LEVEL,
                                 fast=version_two), 1).source,
            RouteSource.SCALAR)

    def test_interleaved_devices_and_header_versions(self):
        bias_v2 = 0.0003
        header_one_v2 = DeviceHeader(
            'dev1', 2, AXIS_GAINS, (-0.01, 0.01),
            calibration_points(23, bias_v2, AXIS_GAINS), 5e-10)

        sample_one_v1 = self._device_sample(
            self.nearby, self.nearby_error, 'dev1', 1, self.bias_one)
        sample_one_v2 = self._device_sample(
            self.nearby, self.nearby_error, 'dev1', 2, bias_v2)
        sample_two = self._device_sample(
            self.record_c, self.record_c_error, 'dev2', 1, self.bias_two)

        stream = [
            self.header_one,
            row('d1-v1', self.nearby, level=FINE_LEVEL, fast=sample_one_v1),
            self.header_two,
            row('d2', self.record_c, level=12, fast=sample_two),
            header_one_v2,
            row('d1-v2', self.nearby, level=FINE_LEVEL, fast=sample_one_v2),
            # A late v1 record must still use the v1 calibration, not v2.
            row('d1-v1-late', self.nearby, level=FINE_LEVEL,
                fast=sample_one_v1),
        ]
        results = [r for batch in partition_rows(stream, chunk_size=2)
                   for r in batch]
        sources = {r.obs_id: r.source for r in results}
        self.assertEqual(sources['d1-v1'], RouteSource.FAST)
        self.assertEqual(sources['d2'], RouteSource.FAST)
        self.assertEqual(sources['d1-v2'], RouteSource.FAST)
        self.assertEqual(sources['d1-v1-late'], RouteSource.FAST)
        # Every cell still equals the authoritative scalar answer.
        by_obs = {r.obs_id: r for r in results}
        self.assertEqual(by_obs['d2'].cell_id,
                         scalar_cell(self.record_c, 12))
        self.assertEqual(by_obs['d1-v2'].cell_id,
                         scalar_cell(self.nearby, FINE_LEVEL))

    def test_prior_chunk_header_used_while_reads_stay_bounded(self):
        sample = self._device_sample(self.boundary, self.boundary_error,
                                     'dev1', 1, self.bias_one)
        stream = [
            row('plain', self.nearby, level=8),
            row('plain2', self.nearby, level=8),
            self.header_one,           # last item of the first chunk
            row('A', self.boundary, level=COARSE_LEVEL, fast=sample),
            row('A-fine', self.boundary, level=FINE_LEVEL, fast=sample),
        ]
        source = CountingIterator(stream)
        batches = partition_rows(source, chunk_size=3)
        first = next(batches)
        self.assertEqual(source.pulled, 3)
        self.assertEqual(len(first), 2)  # header does not produce a row
        second = next(batches)
        self.assertEqual(source.pulled, 5)
        self.assertEqual(second[0].source, RouteSource.FAST)
        self.assertEqual(second[1].source, RouteSource.SCALAR)

    def test_inconsistent_calibration_raises(self):
        bad_header = DeviceHeader(
            'bad', 1, AXIS_GAINS, (-1e-6, 1e-6),
            [((0.01, 0.0, 0.0), Point(0.0, 0.0, 1.0))], 1e-9)
        with self.assertRaises(PartitionError):
            PartitionResolver().register_header(bad_header)


if __name__ == '__main__':
    unittest.main()

from __future__ import print_function, unicode_literals, division

import math
import unittest

from s2sphere import LatLng, RegionCoverer

from s2sphere.coverage import (
    AuthorizationRecord,
    build_work_orders,
    dispatch,
    single_rectangle_intersection,
)

A = 'supplier-a'
B = 'supplier-b'


def rec(supplier, number, lat_lo, lng_lo, lat_hi, lng_hi):
    return AuthorizationRecord.from_degrees(
        supplier, number, lat_lo, lng_lo, lat_hi, lng_hi)


def base_records():
    # A1: -70 westward to -110; B1: 110 eastward to 70, same latitude band.
    # A2 crosses the antimeridian over the northern half; B2 only touches
    # the band on one edge and adds a point authorization on the seam.
    return [
        rec(A, 'A1', 0, -110, 10, -70),
        rec(B, 'B1', 0, 110, 10, 70),
        rec(A, 'A2', 5, 160, 10, -160),
        rec(B, 'B2', -10, 110, 0, 70),
        rec(B, 'B2P', 5, 180, 5, 180),
    ]


def find_order(orders, expected):
    matches = [order for order in orders if order.auth_set == expected]
    assert len(matches) == 1, 'expected one order for {}, got {}'.format(
        expected, matches)
    return matches[0]


def dimensions(order):
    return sorted({tile.dimension for tile in order.tiles})


class WorkOrderTest(unittest.TestCase):

    def setUp(self):
        self.orders = build_work_orders(base_records(), A, B)

    def test_fixed_complete_authorization_sets(self):
        sets = [order.auth_set for order in self.orders]
        self.assertEqual(
            sorted(tuple(sorted(auth)) for auth in sets),
            sorted([
                ('A1', 'B1'),
                ('A1', 'B1', 'B2'),
                ('A2', 'B1'),
                ('A2', 'B1', 'B2P'),
            ]))
        for order in self.orders:
            self.assertTrue(order.auth_numbers[A])
            self.assertTrue(order.auth_numbers[B])
            self.assertEqual(
                order.auth_set,
                frozenset().union(*order.auth_numbers.values()))

    def test_stable_work_order_ids(self):
        self.assertEqual(
            [order.work_order_id for order in self.orders],
            list(range(len(self.orders))))

    def test_interior_edge_and_vertex_are_separate_orders(self):
        interior = find_order(self.orders, frozenset(('A1', 'B1')))
        edge = find_order(self.orders, frozenset(('A1', 'B1', 'B2')))
        point = find_order(self.orders, frozenset(('A2', 'B1', 'B2P')))
        self.assertEqual(dimensions(interior), [2])
        self.assertEqual(dimensions(edge), [1])
        self.assertEqual(dimensions(point), [0])

    def test_each_location_has_a_unique_owner(self):
        observations = {
            'central': (5, -90),
            'dateline-plus': (2, 180),
            'dateline-minus': (2, -180),
            'empty-longitude': (5, 0),
            'empty-band': (2, 130),
            'a2-edge': (5, 170),
            'b2-contact': (0, -90),
            'b2-point': (5, 180),
        }
        owners = {}
        for name, (lat, lng) in observations.items():
            owner = dispatch(
                self.orders, LatLng.from_degrees(lat, lng))
            owners[name] = owner

        self.assertEqual(
            owners['central'].auth_set, frozenset(('A1', 'B1')))
        self.assertIsNone(owners['dateline-plus'])
        self.assertIsNone(owners['dateline-minus'])
        self.assertIsNone(owners['empty-longitude'])
        self.assertIsNone(owners['empty-band'])
        self.assertEqual(
            owners['a2-edge'].auth_set, frozenset(('A2', 'B1')))
        self.assertEqual(
            owners['b2-contact'].auth_set,
            frozenset(('A1', 'B1', 'B2')))
        self.assertEqual(
            owners['b2-point'].auth_set,
            frozenset(('A2', 'B1', 'B2P')))

        # A dense grid must obey the dimension-priority partition: for every
        # dimension, at most one work order geometrically contains the point,
        # so the lowest-dimension match is a globally unique owner.
        for lat in range(-9, 11):
            for lng in range(-179, 180, 7):
                point = LatLng.from_degrees(lat, lng)
                owners_by_dimension = {}
                for order in self.orders:
                    for tile in order.tiles:
                        if tile.contains(point):
                            owners_by_dimension.setdefault(
                                tile.dimension, set()).add(
                                    order.work_order_id)
                for claimant_ids in owners_by_dimension.values():
                    self.assertEqual(len(claimant_ids), 1)
                if owners_by_dimension:
                    lowest = min(owners_by_dimension)
                    self.assertEqual(
                        dispatch(self.orders, point).work_order_id,
                        next(iter(owners_by_dimension[lowest])))

    def test_antimeridian_observation_dispatched_once(self):
        point_order = find_order(
            self.orders, frozenset(('A2', 'B1', 'B2P')))
        coverer = RegionCoverer()
        coverer.min_level = 4
        coverer.max_level = 6
        coverer.max_cells = 30
        candidates = [
            (math.pi, math.radians(5)),
            (-math.pi, math.radians(5)),
        ]
        kept = point_order.filter_candidates(candidates, coverer)
        self.assertEqual(len(kept), 1)

    def test_bounds_overread_is_filtered_by_exact_boundary(self):
        edge_order = find_order(
            self.orders, frozenset(('A1', 'B1', 'B2')))
        coverer = RegionCoverer()
        coverer.min_level = 4
        coverer.max_level = 6
        coverer.max_cells = 30
        on_edge = (math.radians(-90), 0.0)
        off_edge = (math.radians(-90), math.radians(1.0))
        kept = edge_order.filter_candidates(
            [on_edge, off_edge], coverer)
        self.assertEqual(kept, [on_edge])

    def test_coverer_tiles_are_feedable_one_by_one(self):
        coverer = RegionCoverer()
        coverer.min_level = 6
        coverer.max_level = 8
        coverer.max_cells = 24
        for order in self.orders:
            accumulated = []
            for tile in order.tiles:
                covering = coverer.get_covering(tile.bounds)
                self.assertTrue(all(cell_id.id() > 0 for cell_id in covering))
                accumulated.extend(covering)
            self.assertTrue(order.covering(coverer))
            self.assertLessEqual(
                len(order.covering(coverer)), len(accumulated))


class SplitInvarianceTest(unittest.TestCase):

    @staticmethod
    def signature(orders):
        def tile_key(tile):
            return (tile.lat_lo, tile.lat_hi,
                    tile.segment.lo, tile.segment.hi)

        return sorted(
            (tuple((supplier, tuple(sorted(order.auth_numbers[supplier])))
                   for supplier in order.suppliers),
             tuple(sorted(tile_key(tile) for tile in order.tiles)))
            for order in orders)

    @staticmethod
    def candidate_fingerprint(orders):
        coverer = RegionCoverer()
        coverer.min_level = 7
        coverer.max_level = 7
        coverer.max_cells = 64
        return sorted(
            (order.work_order_id,
             tuple(sorted(cell_id.id() for cell_id in
                          order.covering(coverer))),
             tuple(sorted(order.auth_set)))
            for order in orders)

    def assert_same_plan(self, expected_records, actual_records):
        expected = build_work_orders(expected_records, A, B)
        actual = build_work_orders(actual_records, A, B)
        self.assertEqual(self.signature(expected), self.signature(actual))
        self.assertEqual(
            self.candidate_fingerprint(expected),
            self.candidate_fingerprint(actual))

    def test_latitude_split_with_same_number_adds_no_order(self):
        split = [
            rec(B, 'B1', 0, 110, 10, 70),
            rec(A, 'A1', 5, -110, 10, -70),
            rec(A, 'A1', 0, -110, 5, -70),
            rec(A, 'A1', 0, -110, 5, -70),
            rec(A, 'A2', 5, 160, 10, -160),
            rec(B, 'B2', -10, 110, 0, 70),
            rec(B, 'B2P', 5, 180, 5, 180),
        ]
        self.assert_same_plan(base_records(), split)

    def test_dateline_split_with_same_number_adds_no_order(self):
        split = [
            record for record in base_records()
            if record.auth_number != 'A2']
        split.extend([
            rec(A, 'A2', 5, 160, 10, 180),
            rec(A, 'A2', 5, -180, 10, -160),
            rec(A, 'A2', 5, -180, 10, -160),
        ])
        self.assert_same_plan(base_records(), split)

    def test_true_permission_boundary_is_kept(self):
        records = [
            rec(A, 'AX', 0, -10, 10, 0),
            rec(A, 'AY', 0, 0, 10, 10),
            rec(B, 'BX', 0, -10, 10, 0),
            rec(B, 'BY', 0, 0, 10, 10),
        ]
        orders = build_work_orders(records, A, B)
        boundary = [
            order for order in orders if dimensions(order) == [1]]
        self.assertEqual(len(boundary), 1)
        self.assertEqual(
            boundary[0].auth_set,
            frozenset(('AX', 'AY', 'BX', 'BY')))
        # Even when both interiors produce the same scan cells, the edge
        # order survives because ownership follows authorization, not cells.
        coverer = RegionCoverer()
        self.assertTrue(boundary[0].covering(coverer))


class ScalingTest(unittest.TestCase):

    def test_disjoint_bands_produce_no_work_orders(self):
        records = []
        for k in range(8):
            records.append(rec(A, 'A%d' % k, 0, k * 20, 10, k * 20 + 5))
            records.append(
                rec(B, 'B%d' % k, 0, -k * 20 - 15, 10, -k * 20 - 10))
        self.assertEqual(build_work_orders(records, A, B), [])

    def test_only_real_intersection_emits_an_order(self):
        records = [
            rec(A, 'A%d' % k, 0, k * 20, 10, k * 20 + 5)
            for k in range(8)]
        records += [
            rec(B, 'B%d' % k, 0, -k * 20 - 15, 10, -k * 20 - 10)
            for k in range(8)]
        records.append(rec(B, 'BHIT', 0, 40, 10, 45))
        orders = build_work_orders(records, A, B)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].auth_set, frozenset(('A2', 'BHIT')))


class SingleRectangleIntersectionTest(unittest.TestCase):

    def test_plain_intersection(self):
        first = rec(A, 'x', 0, -10, 10, 10).rect
        second = rec(B, 'y', 5, 0, 15, 20).rect
        pieces = single_rectangle_intersection(first, second)
        self.assertEqual(len(pieces), 1)
        self.assertAlmostEqual(pieces[0].lat_lo().radians,
                               math.radians(5))
        self.assertAlmostEqual(pieces[0].lat_hi().radians,
                               math.radians(10))
        self.assertAlmostEqual(pieces[0].lng_lo().radians, 0)
        self.assertAlmostEqual(pieces[0].lng_hi().radians,
                               math.radians(10))

    def test_antimeridian_intersection_splits_into_pieces(self):
        first = rec(A, 'x', 0, 160, 10, -160).rect
        second = rec(B, 'y', 0, 170, 10, -170).rect
        pieces = single_rectangle_intersection(first, second)
        self.assertEqual(len(pieces), 2)
        endpoints = sorted(
            value
            for piece in pieces
            for value in (piece.lng_lo().radians, piece.lng_hi().radians))
        self.assertAlmostEqual(endpoints[0], math.radians(-170))
        self.assertAlmostEqual(endpoints[1], math.radians(170))
        self.assertAlmostEqual(endpoints[2], math.pi)
        self.assertAlmostEqual(endpoints[3], math.pi)
        # The two seam-facing pieces together cover 170..180 and 180..-170.
        inverted = [piece for piece in pieces if piece.lng().is_inverted()]
        self.assertEqual(len(inverted), 1)

    def test_disjoint_intersection_is_empty(self):
        first = rec(A, 'x', 0, -10, 10, 0).rect
        second = rec(B, 'y', 0, 10, 10, 20).rect
        self.assertEqual(single_rectangle_intersection(first, second), [])


if __name__ == '__main__':
    unittest.main()

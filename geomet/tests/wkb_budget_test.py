#  Copyright 2026 Lars Butler & individual contributors
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
import struct
import unittest

from geomet import wkb

BIG_ENDIAN = b'\x00'
LITTLE_ENDIAN = b'\x01'
POINT_TYPE = b'\x00\x00\x00\x01'
LINESTRING_TYPE = b'\x00\x00\x00\x02'
POLYGON_TYPE = b'\x00\x00\x00\x03'
MULTILINESTRING_TYPE = b'\x00\x00\x00\x05'
COLLECTION_TYPE = b'\x00\x00\x00\x07'


def vertex(x, y, big_endian=True):
    fmt = '>dd' if big_endian else '<dd'
    return struct.pack(fmt, x, y)


def count(value, big_endian=True):
    fmt = '>l' if big_endian else '<l'
    return struct.pack(fmt, value)


def polygon_wkb(rings, big_endian=True):
    endian = BIG_ENDIAN if big_endian else LITTLE_ENDIAN
    ptype = POLYGON_TYPE if big_endian else POLYGON_TYPE[::-1]
    out = endian + ptype + count(len(rings), big_endian)
    for ring in rings:
        out += count(len(ring), big_endian)
        for x, y in ring:
            out += vertex(x, y, big_endian)
    return out


def point_wkb(x, y, big_endian=True):
    endian = BIG_ENDIAN if big_endian else LITTLE_ENDIAN
    ptype = POINT_TYPE if big_endian else POINT_TYPE[::-1]
    return endian + ptype + vertex(x, y, big_endian)


def linestring_wkb(vertices, big_endian=True):
    endian = BIG_ENDIAN if big_endian else LITTLE_ENDIAN
    ltype = LINESTRING_TYPE if big_endian else LINESTRING_TYPE[::-1]
    out = endian + ltype + count(len(vertices), big_endian)
    for x, y in vertices:
        out += vertex(x, y, big_endian)
    return out


def collection_wkb(members, big_endian=True):
    endian = BIG_ENDIAN if big_endian else LITTLE_ENDIAN
    ctype = COLLECTION_TYPE if big_endian else COLLECTION_TYPE[::-1]
    out = endian + ctype + count(len(members), big_endian)
    return out + b''.join(members)


def chunked(data, size):
    """Yield the byte string in segments of at most ``size`` bytes."""
    return itertools.chain.from_iterable(
        data[i:i + size] for i in range(0, len(data), size)
    )


class CoordinateBudgetTestCase(unittest.TestCase):

    def setUp(self):
        self.outer = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 0.0)]
        self.inner_three = [(2.0, 2.0), (8.0, 2.0), (2.0, 8.0)]
        self.inner_four = self.inner_three + [(5.0, 5.0)]

        # Exactly 7 vertices: outer ring (4) + last inner ring (3).
        self.exact_record = polygon_wkb([self.outer, self.inner_three])
        # One extra vertex, only in the last inner ring: 8 vertices.
        self.over_record = polygon_wkb([self.outer, self.inner_four])
        # Counts stay within budget, but payload ends inside the inner ring.
        inner_start = (len(BIG_ENDIAN + POLYGON_TYPE) + 4 + 4
                       + len(self.outer) * 16 + 4)
        self.truncated_record = self.exact_record[:inner_start + 16]
        self.assertLess(len(self.truncated_record),
                        len(self.exact_record))

    def test_exact_budget_decodes_fully(self):
        result = wkb.loads(self.exact_record, coordinate_budget=7)
        self.assertEqual('Polygon', result['type'])
        self.assertEqual(4, len(result['coordinates'][0]))
        self.assertEqual(3, len(result['coordinates'][1]))

    def test_extra_inner_vertex_exceeds_budget_at_that_ring(self):
        with self.assertRaises(wkb.WKBCoordinateBudgetExceeded) as ctx:
            wkb.loads(self.over_record, coordinate_budget=7)
        error = ctx.exception
        self.assertEqual(7, error.budget)
        self.assertEqual(4, error.vertices_counted)
        self.assertEqual(4, error.vertices_required)
        self.assertEqual(['Polygon', 'ring[1]'], error.layer)
        self.assertIsInstance(error, ValueError)

    def test_budget_error_stops_before_ring_coordinates(self):
        stream = iter(self.over_record)
        with self.assertRaises(wkb.WKBCoordinateBudgetExceeded):
            wkb.loads(stream, coordinate_budget=7)
        ring_payload = b''.join(
            vertex(x, y) for x, y in self.inner_four
        )
        self.assertEqual(ring_payload, bytes(stream))

    def test_truncated_record_within_budget_reports_short_payload(self):
        with self.assertRaises(wkb.WKBTruncated) as ctx:
            wkb.loads(self.truncated_record, coordinate_budget=7)
        error = ctx.exception
        self.assertEqual(['Polygon', 'ring[1]'], error.layer)
        self.assertEqual(48, error.expected_bytes)
        self.assertEqual(16, error.available_bytes)
        self.assertIsInstance(error, ValueError)

    def test_budget_accumulates_in_segmented_stream(self):
        result = wkb.loads(chunked(self.exact_record, 7),
                           coordinate_budget=7)
        self.assertEqual(3, len(result['coordinates'][1]))

        with self.assertRaises(wkb.WKBCoordinateBudgetExceeded) as ctx:
            wkb.loads(chunked(self.over_record, 7), coordinate_budget=7)
        self.assertEqual(['Polygon', 'ring[1]'], ctx.exception.layer)

    def test_little_endian_accounts_vertices_the_same_way(self):
        record = polygon_wkb([self.outer, self.inner_four],
                             big_endian=False)
        with self.assertRaises(wkb.WKBCoordinateBudgetExceeded) as ctx:
            wkb.loads(record, coordinate_budget=7)
        self.assertEqual(4, ctx.exception.vertices_counted)
        self.assertEqual(['Polygon', 'ring[1]'], ctx.exception.layer)

    def test_default_loads_keeps_unbounded_behavior(self):
        result = wkb.loads(self.over_record)
        self.assertEqual(
            [5.0, 5.0], result['coordinates'][1][-1]
        )

    def test_empty_member_spends_no_budget_but_keeps_layer(self):
        record = polygon_wkb([self.outer, []])
        result = wkb.loads(record, coordinate_budget=4)
        self.assertEqual([], result['coordinates'][1])

        with self.assertRaises(wkb.WKBCoordinateBudgetExceeded) as ctx:
            wkb.loads(
                polygon_wkb([[], self.inner_three]), coordinate_budget=0
            )
        self.assertEqual(0, ctx.exception.vertices_counted)
        self.assertEqual(['Polygon', 'ring[1]'], ctx.exception.layer)

    def test_empty_linestring_spends_no_budget(self):
        record = (
            BIG_ENDIAN + LINESTRING_TYPE + count(0)
        )
        result = wkb.loads(record, coordinate_budget=0)
        self.assertEqual([], result['coordinates'])

    def test_budget_accumulates_across_collection_members(self):
        record = collection_wkb([
            point_wkb(1.0, 2.0),
            linestring_wkb([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)]),
        ])
        # One point already counted; the linestring's three vertices do not
        # fit in a budget of three.
        with self.assertRaises(wkb.WKBCoordinateBudgetExceeded) as ctx:
            wkb.loads(record, coordinate_budget=3)
        error = ctx.exception
        self.assertEqual(1, error.vertices_counted)
        self.assertEqual(3, error.vertices_required)
        self.assertEqual(
            ['GeometryCollection', 'geometry[1]', 'LineString'],
            error.layer,
        )

    def test_budget_accumulates_across_nested_linestrings(self):
        first = linestring_wkb([(0.0, 0.0), (1.0, 1.0)])
        second = linestring_wkb([(2.0, 2.0), (3.0, 3.0), (4.0, 4.0)])
        record = (
            BIG_ENDIAN
            + MULTILINESTRING_TYPE
            + count(2)
            + first
            + second
        )
        with self.assertRaises(wkb.WKBCoordinateBudgetExceeded) as ctx:
            wkb.loads(record, coordinate_budget=4)
        error = ctx.exception
        self.assertEqual(2, error.vertices_counted)
        self.assertEqual(3, error.vertices_required)
        self.assertEqual(
            ['MultiLineString', 'linestring[1]'], error.layer
        )


class DepthLimitTestCase(unittest.TestCase):

    def setUp(self):
        self.leaf = polygon_wkb(
            [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]]
        )
        # GeometryCollection > GeometryCollection > GeometryCollection
        # > Polygon, i.e. three collection layers.
        self.nested = collection_wkb(
            [collection_wkb([collection_wkb([self.leaf])])]
        )

    def test_first_over_deep_member_is_named(self):
        with self.assertRaises(wkb.WKBDepthExceeded) as ctx:
            wkb.loads(self.nested, max_depth=2)
        error = ctx.exception
        self.assertEqual(2, error.max_depth)
        self.assertEqual(3, error.depth)
        self.assertEqual(
            [
                'GeometryCollection', 'geometry[0]',
                'GeometryCollection', 'geometry[0]',
                'GeometryCollection',
            ],
            error.layer,
        )
        self.assertIsInstance(error, ValueError)

    def test_depth_limit_allows_records_at_the_limit(self):
        # GeometryCollection (depth 1) holding a Polygon (depth 2).
        at_limit = collection_wkb([self.leaf])
        result = wkb.loads(at_limit, max_depth=2)
        self.assertEqual('GeometryCollection', result['type'])

    def test_depth_error_does_not_read_the_over_deep_body(self):
        stream = iter(self.nested)
        with self.assertRaises(wkb.WKBDepthExceeded):
            wkb.loads(stream, max_depth=2)
        # The over-deep collection's header is read, but its member count
        # and leaf body are left untouched.
        self.assertEqual(count(1) + self.leaf, bytes(stream))

    def test_default_loads_has_no_depth_limit(self):
        # A point sibling at each collection level keeps dimensionality
        # checks well defined.
        nested_with_points = collection_wkb([
            point_wkb(9.0, 9.0),
            collection_wkb([
                point_wkb(8.0, 8.0),
                collection_wkb([
                    point_wkb(7.0, 7.0),
                    self.leaf,
                ]),
            ]),
        ])
        result = wkb.loads(nested_with_points)
        self.assertEqual('GeometryCollection', result['type'])


class ErrorContractTestCase(unittest.TestCase):

    def test_budget_errors_are_distinct_from_truncation(self):
        self.assertTrue(
            issubclass(wkb.WKBCoordinateBudgetExceeded, wkb.WKBError)
        )
        self.assertTrue(issubclass(wkb.WKBTruncated, wkb.WKBError))
        self.assertTrue(
            issubclass(wkb.WKBDepthExceeded, wkb.WKBError)
        )
        self.assertNotEqual(
            wkb.WKBCoordinateBudgetExceeded, wkb.WKBTruncated
        )

    def test_truncated_header_uses_record_layer(self):
        with self.assertRaises(wkb.WKBTruncated) as ctx:
            wkb.loads(BIG_ENDIAN + POLYGON_TYPE)
        self.assertEqual(['Polygon'], ctx.exception.layer)
        self.assertEqual(4, ctx.exception.expected_bytes)
        self.assertEqual(0, ctx.exception.available_bytes)


if __name__ == '__main__':
    unittest.main()

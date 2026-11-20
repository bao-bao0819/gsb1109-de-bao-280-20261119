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
import unittest

from geomet import util
from geomet.util import MixedDimensionalityError, validate_geometry


# A MultiPolygon whose first polygon is legal while the second polygon has a
# dimensionality conflict: the second polygon's exterior ring is entirely
# XYZ while its interior ring has an XY vertex in the middle.
MIXED_MULTIPOLYGON = {
    'type': 'MultiPolygon',
    'coordinates': [
        [
            [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)],
        ],
        [
            [(0.0, 0.0, 1.0), (10.0, 0.0, 1.0),
             (10.0, 10.0, 1.0), (0.0, 0.0, 1.0)],
            [(2.0, 2.0, 1.0), (3.0, 3.0),
             (4.0, 4.0, 1.0), (2.0, 2.0, 1.0)],
        ],
    ],
}

# The same bad MultiPolygon placed as the second member of a collection whose
# first member is a legal XYZ line.
MIXED_COLLECTION = {
    'type': 'GeometryCollection',
    'geometries': [
        {
            'type': 'LineString',
            'coordinates': [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0),
                            (2.0, 2.0, 2.0)],
        },
        MIXED_MULTIPOLYGON,
    ],
}

# A legal collection: uniform XYZ throughout, several geometry types and a
# nested (multi-level) collection member.
LEGAL_COLLECTION = {
    'type': 'GeometryCollection',
    'geometries': [
        {'type': 'Point', 'coordinates': [1.0, 2.0, 3.0]},
        {'type': 'LineString',
         'coordinates': [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]},
        {'type': 'Polygon', 'coordinates': [
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
             [1.0, 1.0, 0.0], [0.0, 0.0, 0.0]],
            [[0.2, 0.2, 0.0], [0.8, 0.2, 0.0],
             [0.8, 0.8, 0.0], [0.2, 0.2, 0.0]],
        ]},
        {'type': 'MultiPoint',
         'coordinates': [[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]},
        {'type': 'MultiLineString', 'coordinates': [
            [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
            [[2.0, 2.0, 2.0], [3.0, 3.0, 3.0]],
        ]},
        {'type': 'MultiPolygon', 'coordinates': [
            [[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
              [1.0, 1.0, 0.0], [0.0, 0.0, 0.0]]],
        ]},
        {'type': 'GeometryCollection', 'geometries': [
            {'type': 'Point', 'coordinates': [9.0, 9.0, 9.0]},
            {'type': 'LineString',
             'coordinates': [[5.0, 5.0, 5.0], [6.0, 6.0, 6.0]]},
        ]},
    ],
}



class PointRoundingTestCase(unittest.TestCase):

    def setUp(self):
        self.point = {
            'coordinates': (0.7071067811865481, -0.707106781186547),
            'type': 'Point'}

    def test_round1(self):
        expectation = {
            'coordinates': (0.7, -0.7),
            'type': 'Point'}
        rounded = util.round_geom(self.point, 1)
        self.assertEqual(rounded, expectation)


class LineStringRoundingTestCase(unittest.TestCase):

    def setUp(self):
        # The testing LineString is the product of
        # shapely.geometry.Point(0, 0).buffer(1.0, 2).exterior.
        self.linestring = {
            'coordinates': (
                (1.0, 0.0),
                (0.7071067811865481, -0.707106781186547),
                (1.6155445744325867e-15, -1.0),
                (-0.7071067811865459, -0.7071067811865492),
                (-1.0, -3.2310891488651735e-15),
                (-0.7071067811865505, 0.7071067811865446),
                (-4.624589118372729e-15, 1.0),
                (0.7071067811865436, 0.7071067811865515),
                (1.0, 0.0)),
            'type': 'LineString'}

    def test_round1(self):
        expectation = {
            'coordinates': (
                (1.0, 0.0),
                (0.7, -0.7),
                (0.0, -1.0),
                (-0.7, -0.7),
                (-1.0, 0.0),
                (-0.7, 0.7),
                (0.0, 1.0),
                (0.7, 0.7),
                (1.0, 0.0)),
            'type': 'LineString'}
        rounded = util.round_geom(self.linestring, 1)
        self.assertEqual(rounded, expectation)

    def test_round2(self):
        expectation = {
            'coordinates': (
                (1.0, 0.0),
                (0.71, -0.71),
                (0.0, -1.0),
                (-0.71, -0.71),
                (-1.0, 0.0),
                (-0.71, 0.71),
                (0.0, 1.0),
                (0.71, 0.71),
                (1.0, 0.0)),
            'type': 'LineString'}
        rounded = util.round_geom(self.linestring, 2)
        self.assertEqual(rounded, expectation)


class PolygonRoundingTestCase(unittest.TestCase):

    def setUp(self):
        # The testing Polygon is the product of
        # shapely.geometry.Point(0, 0).buffer(1.0, 2).
        self.polygon = {
            'coordinates': [(
                (1.0, 0.0),
                (0.7071067811865481, -0.707106781186547),
                (1.6155445744325867e-15, -1.0),
                (-0.7071067811865459, -0.7071067811865492),
                (-1.0, -3.2310891488651735e-15),
                (-0.7071067811865505, 0.7071067811865446),
                (-4.624589118372729e-15, 1.0),
                (0.7071067811865436, 0.7071067811865515),
                (1.0, 0.0))],
            'type': 'Polygon'}

    def test_round1(self):
        expectation = {
            'coordinates': [(
                (1.0, 0.0),
                (0.7, -0.7),
                (0.0, -1.0),
                (-0.7, -0.7),
                (-1.0, 0.0),
                (-0.7, 0.7),
                (0.0, 1.0),
                (0.7, 0.7),
                (1.0, 0.0))],
            'type': 'Polygon'}
        rounded = util.round_geom(self.polygon, 1)
        self.assertEqual(rounded, expectation)

    def test_round2(self):
        expectation = {
            'coordinates': [(
                (1.0, 0.0),
                (0.71, -0.71),
                (0.0, -1.0),
                (-0.71, -0.71),
                (-1.0, 0.0),
                (-0.71, 0.71),
                (0.0, 1.0),
                (0.71, 0.71),
                (1.0, 0.0))],
            'type': 'Polygon'}
        rounded = util.round_geom(self.polygon, 2)
        self.assertEqual(rounded, expectation)


class MultiPolygonRoundingTestCase(unittest.TestCase):

    def setUp(self):
        # The testing MultiPolygon is the product of
        # shapely.geometry.Point(0, 0).buffer(1.0, 2).
        self.multipolygon = {
            'coordinates': [[(
                (1.0, 0.0),
                (0.7071067811865481, -0.707106781186547),
                (1.6155445744325867e-15, -1.0),
                (-0.7071067811865459, -0.7071067811865492),
                (-1.0, -3.2310891488651735e-15),
                (-0.7071067811865505, 0.7071067811865446),
                (-4.624589118372729e-15, 1.0),
                (0.7071067811865436, 0.7071067811865515),
                (1.0, 0.0))]],
            'type': 'MultiPolygon'}

    def test_round1(self):
        expectation = {
            'coordinates': [[(
                (1.0, 0.0),
                (0.7, -0.7),
                (0.0, -1.0),
                (-0.7, -0.7),
                (-1.0, 0.0),
                (-0.7, 0.7),
                (0.0, 1.0),
                (0.7, 0.7),
                (1.0, 0.0))]],
            'type': 'MultiPolygon'}
        rounded = util.round_geom(self.multipolygon, 1)
        self.assertEqual(rounded, expectation)


class FlattenMultiDimTestCase(unittest.TestCase):

    def test_1d(self):
        data = [1, 2, 3]
        self.assertEqual(data, list(util.flatten_multi_dim(data)))

    def test_2d(self):
        data = [[1, 2], [3, 4, 5]]
        expected = [1, 2, 3, 4, 5]
        self.assertEqual(expected, list(util.flatten_multi_dim(data)))

    def test_multid(self):
        data = [[[1], [2, 3]], [4, 5, [6]]]
        expected = [1, 2, 3, 4, 5, 6]
        self.assertEqual(expected, list(util.flatten_multi_dim(data)))


class ValidateGeometryTestCase(unittest.TestCase):

    def test_mixed_multipolygon_reports_polygon_ring_vertex(self):
        with self.assertRaises(MixedDimensionalityError) as ar:
            validate_geometry(MIXED_MULTIPOLYGON)
        error = ar.exception
        self.assertEqual(
            ['MultiPolygon', 'polygon[2]', 'ring[2]', 'vertex[2]'],
            error.path,
        )
        self.assertEqual(3, error.expected_dims)
        self.assertEqual(2, error.actual_dims)

    def test_mixed_collection_path_starts_at_second_member(self):
        with self.assertRaises(MixedDimensionalityError) as ar:
            validate_geometry(MIXED_COLLECTION)
        self.assertEqual(
            ['GeometryCollection', 'MultiPolygon[2]', 'polygon[2]',
             'ring[2]', 'vertex[2]'],
            ar.exception.path,
        )

    def test_legal_collection_passes(self):
        # Uniform dimensions across every member, including nested members.
        validate_geometry(LEGAL_COLLECTION)

    def test_empty_geometries_are_not_mixed_dimensionality(self):
        empty_geoms = [
            {'type': 'Point', 'coordinates': []},
            {'type': 'LineString', 'coordinates': []},
            {'type': 'Polygon', 'coordinates': []},
            {'type': 'Polygon', 'coordinates': [[]]},
            {'type': 'MultiPoint', 'coordinates': []},
            {'type': 'MultiLineString', 'coordinates': [[]]},
            {'type': 'MultiPolygon', 'coordinates': [[[[]]]]},
            {'type': 'GeometryCollection', 'geometries': []},
        ]
        for geom in empty_geoms:
            validate_geometry(geom)

    def test_sibling_collection_members_may_use_different_dimensions(self):
        # Members of a collection are independent geometries; differing
        # dimensionality between siblings is legal and must be left alone.
        collection = {
            'type': 'GeometryCollection',
            'geometries': [
                {'type': 'Point', 'coordinates': [0.0, 0.0]},
                {'type': 'Point', 'coordinates': [0.0, 0.0, 0.0]},
                {'type': 'LineString',
                 'coordinates': [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]},
            ],
        }
        validate_geometry(collection)

    def test_uniform_geometries_pass(self):
        geoms = [
            {'type': 'Point', 'coordinates': [0.0, 1.0, 2.0]},
            {'type': 'LineString',
             'coordinates': [[0.0, 0.0], [1.0, 1.0]]},
            {'type': 'Polygon', 'coordinates': [
                [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]],
                [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.2]],
            ]},
            {'type': 'MultiPoint',
             'coordinates': [[0.0, 0.0], [1.0, 1.0]]},
            {'type': 'MultiLineString', 'coordinates': [
                [[0.0, 0.0], [1.0, 1.0]],
                [[2.0, 2.0, 2.0], [3.0, 3.0, 3.0]],
            ]},
            {'type': 'MultiPolygon', 'coordinates': [
                [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                [[[2.0, 2.0, 2.0], [3.0, 2.0, 2.0],
                  [3.0, 3.0, 2.0], [2.0, 2.0, 2.0]]],
            ]},
        ]
        for geom in geoms:
            validate_geometry(geom)

    def test_mixed_linestring_path(self):
        geom = {
            'type': 'LineString',
            'coordinates': [[0.0, 0.0, 0.0], [1.0, 1.0], [2.0, 2.0, 2.0]],
        }
        with self.assertRaises(MixedDimensionalityError) as ar:
            validate_geometry(geom)
        self.assertEqual(['LineString', 'vertex[2]'], ar.exception.path)

    def test_mixed_multipoint_path(self):
        geom = {
            'type': 'MultiPoint',
            'coordinates': [[0.0, 0.0], [1.0, 1.0, 1.0]],
        }
        with self.assertRaises(MixedDimensionalityError) as ar:
            validate_geometry(geom)
        self.assertEqual(['MultiPoint', 'point[2]'], ar.exception.path)

    def test_mixed_polygon_ring_path(self):
        geom = {
            'type': 'Polygon',
            'coordinates': [
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0]],
            ],
        }
        with self.assertRaises(MixedDimensionalityError) as ar:
            validate_geometry(geom)
        self.assertEqual(
            ['Polygon', 'ring[1]', 'vertex[3]'], ar.exception.path
        )

    def test_mixed_multilinestring_is_scoped_per_linestring(self):
        # Each linestring is validated independently; the conflict is reported
        # within the offending linestring only.
        geom = {
            'type': 'MultiLineString',
            'coordinates': [
                [[0.0, 0.0], [1.0, 1.0]],
                [[2.0, 2.0, 2.0], [3.0, 3.0]],
            ],
        }
        with self.assertRaises(MixedDimensionalityError) as ar:
            validate_geometry(geom)
        self.assertEqual(
            ['MultiLineString', 'linestring[2]', 'vertex[2]'],
            ar.exception.path,
        )

    def test_unsupported_geom_type_raises_value_error(self):
        with self.assertRaises(ValueError):
            validate_geometry(
                {'type': 'Tetrahedron', 'coordinates': [[1.0, 2.0]]}
            )


class MixedDimensionalityErrorTestCase(unittest.TestCase):

    def test_is_value_error_and_carries_path(self):
        error = MixedDimensionalityError(
            ['MultiPolygon', 'polygon[2]', 'ring[2]', 'vertex[2]'], 3, 2
        )
        self.assertIsInstance(error, ValueError)
        self.assertEqual(
            ['MultiPolygon', 'polygon[2]', 'ring[2]', 'vertex[2]'],
            error.path,
        )
        message = str(error)
        self.assertIn('MultiPolygon -> polygon[2] -> ring[2] -> vertex[2]',
                      message)
        self.assertIn('expected 3', message)
        self.assertIn('found 2', message)

    def test_path_is_copied(self):
        path = ['Polygon', 'ring[1]']
        error = MixedDimensionalityError(path, 2, 3)
        path.append('vertex[9]')
        self.assertEqual(['Polygon', 'ring[1]'], error.path)

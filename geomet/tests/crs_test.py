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
import copy
import io
import unittest

from geomet import crs
from geomet import wkb
from geomet import wkt


def _merged_surveys():
    """
    Two land surveys merged into a single geometry collection.

    The root declares SRID 4326. The first sub-collection declares
    nothing, but some of its leaves repeat the same SRID via `crs`. The
    second sub-collection declares 4326 via `meta.srid`, but a deeper
    sub-collection declares 3857; the point inside it declares nothing,
    and its values happen to look like longitude/latitude.
    """
    return {
        'type': 'GeometryCollection',
        'meta': {'srid': 4326},
        'geometries': [
            {
                'type': 'GeometryCollection',
                'geometries': [
                    {'type': 'Point', 'coordinates': [116.0, 40.0],
                     'crs': {'type': 'name',
                             'properties': {'name': 'EPSG4326'}}},
                    {'type': 'Point', 'coordinates': [117.0, 41.0],
                     'crs': {'type': 'name',
                             'properties': {'name': 'EPSG4326'}}},
                ],
            },
            {
                'type': 'GeometryCollection',
                'meta': {'srid': 4326},
                'geometries': [
                    {
                        'type': 'GeometryCollection',
                        'meta': {'srid': 3857},
                        'geometries': [
                            {'type': 'Point',
                             'coordinates': [121.5, 31.2]},
                        ],
                    },
                ],
            },
        ],
    }


def _fixed_surveys():
    """
    The same merged collection, with the conflicting sub-collection's
    declaration corrected to 4326. Redundant (but equivalent)
    declarations at every level are kept.
    """
    geom = _merged_surveys()
    geom['geometries'][1]['geometries'][0]['meta']['srid'] = 4326
    return geom


class StrictWKTTestCase(unittest.TestCase):

    def test_conflicting_subset_is_rejected(self):
        geom = _merged_surveys()
        with self.assertRaises(crs.InvalidCRSException) as ctx:
            wkt.dumps(geom, strict_srid=True)
        message = str(ctx.exception)
        # The conflict points at the level declaring 3857, and mentions
        # the SRID it inherits from its ancestors.
        self.assertIn('root.geometries[1].geometries[0]', message)
        self.assertIn('3857', message)
        self.assertIn('4326', message)
        # The undeclared point inside the conflicting sub-collection is
        # not reported as a declaration error.
        self.assertNotIn('geometries[1].geometries[0].geometries[0]',
                         message)

    def test_dump_also_validates(self):
        geom = _merged_surveys()
        with self.assertRaises(crs.InvalidCRSException):
            wkt.dump(geom, io.StringIO(), strict_srid=True)

    def test_fixed_collection_encodes_root_srid_once(self):
        geom = _fixed_surveys()
        original = copy.deepcopy(geom)

        result = wkt.dumps(geom, strict_srid=True)

        self.assertTrue(result.startswith('SRID=4326;GEOMETRYCOLLECTION'))
        self.assertEqual(1, result.count('SRID='))
        # The input object is untouched: redundant declarations are kept.
        self.assertEqual(original, geom)

        # Round-trip: everything read back uses the same SRID.
        loaded = wkt.loads(result)
        self.assertEqual('4326', crs.validate_uniform_srid(loaded))
        self.assertEqual(
            [121.5, 31.2],
            loaded['geometries'][1]['geometries'][0]
                  ['geometries'][0]['coordinates'],
        )
        self.assertEqual(
            [116.0, 40.0],
            loaded['geometries'][0]['geometries'][0]['coordinates'],
        )

    def test_plain_collection_without_declarations_still_works(self):
        geom = {
            'type': 'GeometryCollection',
            'geometries': [
                {'type': 'Point', 'coordinates': [0.0, 1.0]},
                {'type': 'GeometryCollection', 'geometries': [
                    {'type': 'Point', 'coordinates': [2.0, 3.0]},
                ]},
            ],
        }
        result = wkt.dumps(geom, strict_srid=True)
        self.assertNotIn('SRID=', result)
        self.assertEqual(
            geom, wkt.loads(result))

    def test_member_with_contradictory_meta_and_crs_is_identified(self):
        geom = {
            'type': 'GeometryCollection',
            'meta': {'srid': 4326},
            'geometries': [
                {'type': 'Point', 'coordinates': [0.0, 1.0]},
                {'type': 'Point', 'coordinates': [2.0, 3.0],
                 'meta': {'srid': 4326},
                 'crs': {'type': 'name',
                         'properties': {'name': 'EPSG3857'}}},
            ],
        }
        with self.assertRaises(crs.InvalidCRSException) as ctx:
            wkt.dumps(geom, strict_srid=True)
        message = str(ctx.exception)
        self.assertIn('root.geometries[1]', message)
        self.assertIn('4326', message)
        self.assertIn('3857', message)

    def test_unconfirmed_mixed_declarations_are_rejected(self):
        # No root declaration; one member declares a SRID, the other is
        # unknown. The unknown member must not be silently assigned one.
        geom = {
            'type': 'GeometryCollection',
            'geometries': [
                {'type': 'Point', 'coordinates': [0.0, 1.0],
                 'meta': {'srid': 4326}},
                {'type': 'Point', 'coordinates': [2.0, 3.0]},
            ],
        }
        with self.assertRaises(crs.InvalidCRSException):
            wkt.dumps(geom, strict_srid=True)


class StrictWKBTestCase(unittest.TestCase):

    def test_conflicting_subset_is_rejected(self):
        geom = _merged_surveys()
        with self.assertRaises(crs.InvalidCRSException) as ctx:
            wkb.dumps(geom, strict_srid=True)
        message = str(ctx.exception)
        self.assertIn('root.geometries[1].geometries[0]', message)
        self.assertIn('3857', message)
        self.assertIn('4326', message)
        self.assertNotIn('geometries[1].geometries[0].geometries[0]',
                         message)

    def test_dump_also_validates(self):
        geom = _merged_surveys()
        with self.assertRaises(crs.InvalidCRSException):
            wkb.dump(geom, io.BytesIO(), strict_srid=True)

    def test_fixed_collection_encodes_root_srid_once(self):
        geom = _fixed_surveys()
        original = copy.deepcopy(geom)

        for big_endian in (True, False):
            data = wkb.dumps(geom, big_endian=big_endian, strict_srid=True)
            # The input object is untouched.
            self.assertEqual(original, geom)

            loaded = wkb.loads(data)
            # Only the root carries the SRID; nested members inherit it.
            self.assertEqual(4326, loaded['meta']['srid'])
            for member in loaded['geometries']:
                self.assertNotIn('meta', member)
                self.assertNotIn('crs', member)
            self.assertEqual('4326', crs.validate_uniform_srid(loaded))
            # Coordinates are unchanged.
            self.assertEqual(
                [121.5, 31.2],
                loaded['geometries'][1]['geometries'][0]
                      ['geometries'][0]['coordinates'],
            )
            self.assertEqual(
                [116.0, 40.0],
                loaded['geometries'][0]['geometries'][0]['coordinates'],
            )

    def test_plain_collection_without_declarations_still_works(self):
        geom = {
            'type': 'GeometryCollection',
            'geometries': [
                {'type': 'Point', 'coordinates': [0.0, 1.0]},
                {'type': 'Point', 'coordinates': [2.0, 3.0]},
            ],
        }
        loaded = wkb.loads(wkb.dumps(geom, strict_srid=True))
        self.assertNotIn('meta', loaded)
        self.assertEqual(geom, loaded)

    def test_member_with_contradictory_meta_and_crs_is_identified(self):
        geom = {
            'type': 'GeometryCollection',
            'meta': {'srid': 4326},
            'geometries': [
                {'type': 'Point', 'coordinates': [2.0, 3.0],
                 'meta': {'srid': 3857},
                 'crs': {'type': 'name',
                         'properties': {'name': 'EPSG4326'}}},
            ],
        }
        with self.assertRaises(crs.InvalidCRSException) as ctx:
            wkb.dumps(geom, strict_srid=True)
        self.assertIn('root.geometries[0]', str(ctx.exception))

    def test_unconfirmed_mixed_declarations_are_rejected(self):
        geom = {
            'type': 'GeometryCollection',
            'geometries': [
                {'type': 'Point', 'coordinates': [0.0, 1.0],
                 'meta': {'srid': 4326}},
                {'type': 'Point', 'coordinates': [2.0, 3.0]},
            ],
        }
        with self.assertRaises(crs.InvalidCRSException):
            wkb.dumps(geom, strict_srid=True)


class ValidateUniformSRIDTestCase(unittest.TestCase):

    def test_no_declarations_returns_none(self):
        geom = {'type': 'Point', 'coordinates': [0.0, 1.0]}
        self.assertIsNone(crs.validate_uniform_srid(geom))

    def test_redundant_equivalent_declarations_are_accepted(self):
        self.assertEqual(
            '4326', crs.validate_uniform_srid(_fixed_surveys()))

    def test_srid_declared_via_crs_only(self):
        geom = {
            'type': 'Point',
            'coordinates': [0.0, 1.0],
            'crs': {'type': 'name',
                    'properties': {'name': 'EPSG4326'}},
        }
        self.assertEqual('4326', crs.validate_uniform_srid(geom))
        self.assertEqual(
            'SRID=4326;POINT (0.0000000000000000 1.0000000000000000)',
            wkt.dumps(geom, strict_srid=True),
        )


if __name__ == '__main__':
    unittest.main()

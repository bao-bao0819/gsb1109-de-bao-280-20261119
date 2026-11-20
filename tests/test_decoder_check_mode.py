"""
Tests for the decoder check mode handling partially corrupted MVT tiles.
"""

import unittest

from google.protobuf.message import DecodeError

import mapbox_vector_tile
from mapbox_vector_tile.decoder import (
    GeometryCommandTruncatedError,
    TagIndexOutOfRangeError,
    UnexpectedTagCountError,
)
from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vector_tile


def zig_zag_encode(n):
    return (n << 1) ^ (n >> 31)


def line_geometry(points):
    geometry = []
    for index, (x, y) in enumerate(points):
        if index == 0:
            dx, dy = x, y
            geometry.append((1 << 3) | 1)
        else:
            dx, dy = x - points[index - 1][0], y - points[index - 1][1]
            geometry.append((1 << 3) | 2)
        geometry.extend([zig_zag_encode(dx), zig_zag_encode(dy)])
    return geometry


def point_geometry(x, y):
    return [(1 << 3) | 1, zig_zag_encode(x), zig_zag_encode(y)]


def build_corrupted_tile():
    tile = vector_tile.tile()

    roads = tile.layers.add()
    roads.version = 2
    roads.name = "roads"
    roads.extent = 4096
    roads.keys.append("name")
    roads.keys.append("kind")
    name_value = roads.values.add()
    name_value.string_value = "Main"
    kind_value = roads.values.add()
    kind_value.string_value = "highway"

    # feature 0: valid road, no explicit id
    first = roads.features.add()
    first.type = 2
    first.tags.extend([0, 0])
    first.geometry.extend(line_geometry([(10, 20), (30, 40)]))

    # feature 1: road without an id whose last LineTo command is missing one coordinate
    truncated = roads.features.add()
    truncated.type = 2
    truncated_geometry = line_geometry([(10, 20), (30, 40)])
    truncated.geometry.extend(truncated_geometry[:-1])
    assert not truncated.HasField("id")

    # feature 2: road with explicit id 0 and a tag pointing outside the values table
    bad_props = roads.features.add()
    bad_props.id = 0
    bad_props.type = 2
    bad_props.tags.extend([0, 99])
    bad_props.geometry.extend(line_geometry([(100, 100), (110, 110)]))

    # feature 3: valid road after the corrupted ones
    last = roads.features.add()
    last.id = 7
    last.type = 2
    last.tags.extend([1, 1])
    last.geometry.extend(line_geometry([(50, 60), (70, 80)]))

    places = tile.layers.add()
    places.version = 2
    places.name = "places"
    places.extent = 4096
    place = places.features.add()
    place.type = 1
    place.geometry.extend(point_geometry(5, 7))

    return tile.SerializeToString()


def build_valid_tile():
    tile = vector_tile.tile()
    roads = tile.layers.add()
    roads.version = 2
    roads.name = "roads"
    roads.extent = 4096
    first = roads.features.add()
    first.type = 2
    first.geometry.extend(line_geometry([(10, 20), (30, 40)]))
    last = roads.features.add()
    last.id = 7
    last.type = 2
    last.geometry.extend(line_geometry([(50, 60), (70, 80)]))
    return tile.SerializeToString()


class CheckModeTestCase(unittest.TestCase):
    def setUp(self):
        self.tile_data = build_corrupted_tile()

    def test_check_mode_keeps_valid_features_and_reports_bad_ones(self):
        def places_transformer(x, y):
            return x + 1000, y + 2000

        result = mapbox_vector_tile.decode(
            self.tile_data,
            check_mode=True,
            per_layer_options={"places": {"transformer": places_transformer}},
        )

        self.assertEqual(set(result.keys()), {"layers", "errors"})

        roads_features = result["layers"]["roads"]["features"]
        self.assertEqual(
            roads_features,
            [
                {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[10, 4096 - 20], [30, 4096 - 40]],
                    },
                    "properties": {"name": "Main"},
                    "id": 0,
                    "type": "Feature",
                },
                {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[50, 4096 - 60], [70, 4096 - 80]],
                    },
                    "properties": {"kind": "highway"},
                    "id": 7,
                    "type": "Feature",
                },
            ],
        )

        places_features = result["layers"]["places"]["features"]
        self.assertEqual(
            places_features,
            [
                {
                    "geometry": {"type": "Point", "coordinates": [1005, 2000 + (4096 - 7)]},
                    "properties": {},
                    "id": 0,
                    "type": "Feature",
                }
            ],
        )

        self.assertEqual(
            result["errors"],
            [
                {
                    "layer": "roads",
                    "index": 1,
                    "reason": "geometry_command_truncated",
                    "id_present": False,
                    "id": None,
                    "message": result["errors"][0]["message"],
                },
                {
                    "layer": "roads",
                    "index": 2,
                    "reason": "properties_invalid",
                    "id_present": True,
                    "id": 0,
                    "message": result["errors"][1]["message"],
                },
            ],
        )
        self.assertIn("Geometry command 2", result["errors"][0]["message"])
        self.assertIn("99", result["errors"][1]["message"])

        # malformed features must not leave any half-read geometry behind
        self.assertEqual(len(roads_features), 2)
        self.assertTrue(all(len(feature["geometry"]["coordinates"]) == 2 for feature in roads_features))

    def test_normal_read_still_raises_on_corrupted_feature(self):
        with self.assertRaises(GeometryCommandTruncatedError) as ex:
            mapbox_vector_tile.decode(self.tile_data)
        self.assertIsInstance(ex.exception, IndexError)

    def test_normal_read_raises_on_bad_tag_reference(self):
        tile = vector_tile.tile()
        layer = tile.layers.add()
        layer.version = 2
        layer.name = "roads"
        feature = layer.features.add()
        feature.type = 1
        feature.geometry.extend(point_geometry(1, 2))
        feature.tags.extend([42, 0])
        data = tile.SerializeToString()

        with self.assertRaises(TagIndexOutOfRangeError) as ex:
            mapbox_vector_tile.decode(data)
        self.assertIsInstance(ex.exception, IndexError)

        result = mapbox_vector_tile.decode(data, check_mode=True)
        self.assertEqual(result["layers"]["roads"]["features"], [])
        self.assertEqual(result["errors"][0]["reason"], "properties_invalid")

    def test_normal_read_raises_on_odd_tag_count(self):
        tile = vector_tile.tile()
        layer = tile.layers.add()
        layer.version = 2
        layer.name = "roads"
        layer.keys.append("name")
        value = layer.values.add()
        value.string_value = "Main"
        feature = layer.features.add()
        feature.type = 1
        feature.geometry.extend(point_geometry(1, 2))
        feature.tags.extend([0, 0, 0])
        data = tile.SerializeToString()

        with self.assertRaises(UnexpectedTagCountError) as ex:
            mapbox_vector_tile.decode(data)
        self.assertIsInstance(ex.exception, AssertionError)
        self.assertIn("Unexpected number of tags", str(ex.exception))

    def test_raising_transformer_is_not_reported_as_data_error(self):
        valid_data = build_valid_tile()

        def raising_transformer(x, y):
            raise RuntimeError("transformer boom")

        options = {"transformer": raising_transformer}
        with self.assertRaises(RuntimeError):
            mapbox_vector_tile.decode(valid_data, default_options=options)
        with self.assertRaises(RuntimeError):
            mapbox_vector_tile.decode(valid_data, default_options=options, check_mode=True)

    def test_check_mode_transformer_still_applies_to_valid_features(self):
        valid_data = build_valid_tile()

        def shift(x, y):
            return x + 1, y + 2

        result = mapbox_vector_tile.decode(
            valid_data,
            check_mode=True,
            default_options={"transformer": shift},
        )
        self.assertEqual(result["errors"], [])
        coordinates = result["layers"]["roads"]["features"][0]["geometry"]["coordinates"]
        self.assertEqual(coordinates, [[11, (4096 - 20) + 2], [31, (4096 - 40) + 2]])

    def test_undecodable_outer_bytes_raise_decode_error(self):
        for garbage in (b"\xff" * 10, b"not a tile"):
            with self.assertRaises(DecodeError):
                mapbox_vector_tile.decode(garbage)
            with self.assertRaises(DecodeError):
                mapbox_vector_tile.decode(garbage, check_mode=True)


if __name__ == "__main__":
    unittest.main()

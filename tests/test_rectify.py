import unittest

import mapbox_vector_tile
from mapbox_vector_tile.errors import (
    DuplicateFeatureIdError,
    FeatureNotFoundError,
    LayerNotFoundError,
    PropertyNotFoundError,
)
from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vector_tile
from mapbox_vector_tile.rectify import rectify_tile


def parse_tile(tile_bytes):
    tile = vector_tile.tile()
    tile.ParseFromString(tile_bytes)
    return tile


def feature_id_presence(layer):
    return [(feature.HasField("id"), feature.id if feature.HasField("id") else None) for feature in layer.features]


def geometry_commands(layer):
    return [list(feature.geometry) for feature in layer.features]


def value_entries(layer):
    entries = []
    for value in layer.values:
        for field in value.DESCRIPTOR.fields:
            if value.HasField(field.name):
                entries.append((field.name, getattr(value, field.name)))
    return entries


def build_published_tile():
    return mapbox_vector_tile.encode(
        [
            {
                "name": "road",
                "features": [
                    {"geometry": "LINESTRING (0 0, 1 1, 2 2)", "properties": {"name": "旧名", "note": "临时"}, "id": 7},
                    {"geometry": "LINESTRING (0 0, 3 3)", "properties": {"name": "旧名", "kind": 3}, "id": 9},
                    {"geometry": "POINT (5 5)", "properties": {"name": "零号路", "kind": 3}, "id": 0},
                    {
                        "geometry": "POINT (6 6)",
                        "properties": {"name": "无名路", "flag": True, "ratio": 1.0, "code": "1"},
                    },
                ],
            },
            {
                "name": "sign",
                "features": [
                    {"geometry": "POINT (10 10)", "properties": {"name": "旧路标", "code": 100}, "id": 7},
                ],
            },
        ]
    )


def build_duplicate_id_tile():
    return mapbox_vector_tile.encode(
        [
            {
                "name": "road",
                "features": [
                    {"geometry": "POINT (1 1)", "properties": {"name": "旧名"}, "id": 7},
                    {"geometry": "POINT (2 2)", "properties": {"name": "旧名"}, "id": 7},
                    {"geometry": "POINT (3 3)", "properties": {"name": "零号路"}, "id": 0},
                    {"geometry": "POINT (4 4)", "properties": {"name": "无名路"}},
                ],
            },
            {
                "name": "sign",
                "features": [
                    {"geometry": "POINT (10 10)", "properties": {"name": "旧路标"}, "id": 7},
                ],
            },
        ]
    )


class RectifyTileTestCase(unittest.TestCase):
    def test_batch_replace_and_delete_preserves_everything_else(self):
        tile_bytes = build_published_tile()
        original = parse_tile(tile_bytes)
        original_road = original.layers[0]
        original_sign_bytes = original.layers[1].SerializeToString()

        result = rectify_tile(
            tile_bytes,
            [
                {"layer": "road", "id": 7, "set_properties": {"name": "新名"}, "delete_properties": ["note"]},
                {"layer": "road", "id": 0, "set_properties": {"kind": 4}},
            ],
        )

        rectified = parse_tile(result)
        road = rectified.layers[0]
        sign = rectified.layers[1]

        decoded = mapbox_vector_tile.decode(result)
        road_features = decoded["road"]["features"]

        # Only the two targeted features changed.
        self.assertEqual(road_features[0]["properties"], {"name": "新名"})
        self.assertEqual(road_features[1]["properties"], {"name": "旧名", "kind": 3})
        self.assertEqual(road_features[2]["properties"], {"name": "零号路", "kind": 4})

        # The feature without an id is never matched as id 0 and keeps no written id.
        self.assertEqual(road_features[3]["properties"], {"name": "无名路", "flag": True, "ratio": 1.0, "code": "1"})
        self.assertEqual(
            feature_id_presence(road),
            [(True, 7), (True, 9), (True, 0), (False, None)],
        )

        # Geometry commands and feature ordering are byte-for-byte unchanged.
        self.assertEqual(geometry_commands(road), geometry_commands(original_road))
        self.assertEqual(feature_id_presence(road), feature_id_presence(original_road))
        rectified_types = [feature.type for feature in road.features]
        original_types = [feature.type for feature in original_road.features]
        self.assertEqual(rectified_types, original_types)

        # The untouched sign layer is serialized identically.
        self.assertEqual(sign.SerializeToString(), original_sign_bytes)

        # Deleting a property neither removes nor rewrites dictionary entries: the old name is still
        # referenced by another feature and orphaned entries are left in place as well.
        self.assertIn("旧名", [entry[1] for entry in value_entries(road) if entry[0] == "string_value"])
        self.assertIn("临时", [entry[1] for entry in value_entries(road) if entry[0] == "string_value"])
        self.assertIn("note", list(road.keys))

        # New entries are appended, keeping every pre-existing index stable.
        self.assertEqual(len(road.values), len(original_road.values) + 2)
        self.assertEqual(value_entries(road)[-2:], [("string_value", "新名"), ("int_value", 4)])

        # Unmodified properties keep their original value type representation.
        untouched = road_features[3]["properties"]
        self.assertIs(untouched["flag"], True)
        self.assertIsInstance(untouched["ratio"], float)
        self.assertIsInstance(untouched["code"], str)

    def test_duplicate_id_reports_layer_and_positions_and_outputs_nothing(self):
        tile_bytes = build_duplicate_id_tile()
        original = parse_tile(tile_bytes)
        original_sign_bytes = original.layers[1].SerializeToString()

        with self.assertRaises(DuplicateFeatureIdError) as context:
            rectify_tile(
                tile_bytes,
                [
                    {"layer": "road", "id": 7, "set_properties": {"name": "新名"}},
                    {"layer": "sign", "id": 7, "set_properties": {"name": "新路标"}},
                ],
            )

        error = context.exception
        self.assertEqual(error.layer_name, "road")
        self.assertEqual(error.feature_id, 7)
        self.assertEqual(error.positions, (0, 1))
        self.assertIn("road", str(error))
        self.assertIn("0, 1", str(error))

        # No half-product: the caller still holds the untouched tile, including the legal sign correction.
        reparsed = parse_tile(tile_bytes)
        self.assertEqual(reparsed.layers[1].SerializeToString(), original_sign_bytes)
        decoded = mapbox_vector_tile.decode(tile_bytes)
        self.assertEqual(decoded["sign"]["features"][0]["properties"], {"name": "旧路标"})
        self.assertEqual(
            [feature["properties"]["name"] for feature in decoded["road"]["features"]],
            ["旧名", "旧名", "零号路", "无名路"],
        )

    def test_missing_id_never_matches_feature_without_id(self):
        tile_bytes = build_published_tile()

        result = rectify_tile(tile_bytes, [{"layer": "road", "id": 0, "set_properties": {"kind": 4}}])

        road = parse_tile(result).layers[0]
        self.assertTrue(road.features[2].HasField("id"))
        self.assertEqual(road.features[2].id, 0)
        self.assertFalse(road.features[3].HasField("id"))

    def test_unknown_layer_and_unknown_id_are_rejected(self):
        tile_bytes = build_published_tile()

        with self.assertRaises(LayerNotFoundError):
            rectify_tile(tile_bytes, [{"layer": "river", "id": 7, "set_properties": {"name": "x"}}])

        with self.assertRaises(FeatureNotFoundError):
            rectify_tile(tile_bytes, [{"layer": "road", "id": 11, "set_properties": {"name": "x"}}])

    def test_missing_property_aborts_the_whole_batch(self):
        tile_bytes = build_published_tile()

        with self.assertRaises(PropertyNotFoundError):
            rectify_tile(
                tile_bytes,
                [
                    {"layer": "road", "id": 7, "delete_properties": ["absent"]},
                    {"layer": "road", "id": 0, "set_properties": {"kind": 4}},
                ],
            )

        decoded = mapbox_vector_tile.decode(tile_bytes)
        self.assertEqual(decoded["road"]["features"][2]["properties"]["kind"], 3)

    def test_existing_replacement_values_are_reused(self):
        tile_bytes = build_published_tile()
        original = parse_tile(tile_bytes)
        original_value_count = len(original.layers[0].values)

        # "零号路" already exists in the road value table, so no new value entry may be appended.
        result = rectify_tile(
            tile_bytes, [{"layer": "road", "id": 7, "set_properties": {"name": "零号路"}}]
        )

        road = parse_tile(result).layers[0]
        self.assertEqual(len(road.values), original_value_count)
        decoded = mapbox_vector_tile.decode(result)
        self.assertEqual(decoded["road"]["features"][0]["properties"]["name"], "零号路")

    def test_bool_and_int_values_remain_distinct(self):
        tile_bytes = build_published_tile()

        result = rectify_tile(tile_bytes, [{"layer": "road", "id": 7, "set_properties": {"flag": True, "kind": 1}}])

        road = parse_tile(result).layers[0]
        fields = value_entries(road)
        self.assertIn(("bool_value", True), fields)
        self.assertIn(("int_value", 1), fields)

    def test_new_property_is_added_and_several_changes_apply_in_one_rectification(self):
        tile_bytes = build_published_tile()

        result = rectify_tile(
            tile_bytes,
            [
                {
                    "layer": "sign",
                    "id": 7,
                    "set_properties": {"name": "新路标", "fresh": "v"},
                    "delete_properties": ["code"],
                }
            ],
        )

        properties = mapbox_vector_tile.decode(result)["sign"]["features"][0]["properties"]
        self.assertEqual(properties, {"name": "新路标", "fresh": "v"})

    def test_overlapping_set_and_delete_is_rejected(self):
        tile_bytes = build_published_tile()

        with self.assertRaises(ValueError):
            rectify_tile(
                tile_bytes,
                [{"layer": "road", "id": 7, "set_properties": {"note": "x"}, "delete_properties": ["note"]}],
            )

    def test_same_feature_targeted_twice_in_one_batch_is_rejected(self):
        tile_bytes = build_published_tile()

        with self.assertRaises(ValueError):
            rectify_tile(
                tile_bytes,
                [
                    {"layer": "road", "id": 7, "set_properties": {"name": "新名"}},
                    {"layer": "road", "id": 7, "delete_properties": ["note"]},
                ],
            )

        decoded = mapbox_vector_tile.decode(tile_bytes)
        self.assertEqual(decoded["road"]["features"][0]["properties"], {"name": "旧名", "note": "临时"})


if __name__ == "__main__":
    unittest.main()

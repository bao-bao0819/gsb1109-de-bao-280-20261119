"""
Tests for vector_tile/merge.py
"""

import unittest

import mapbox_vector_tile
from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vector_tile
from mapbox_vector_tile.merge import merge_tiles

VALUE_FIELD_NAMES = (
    "string_value",
    "float_value",
    "double_value",
    "int_value",
    "uint_value",
    "sint_value",
    "bool_value",
)

ROAD_A1 = [9, 50, 60, 10, 2, 2]
ROAD_A2 = [9, 4, 6, 10, 2, 8]
ROAD_B1 = [9, 8, 9, 10, 4, 4]
SQUARE_A = [9, 0, 0, 26, 20, 0, 0, 20, 19, 0, 15]
SQUARE_B = [9, 0, 0, 26, 10, 0, 0, 10, 9, 0, 15]


def value_field(value):
    fields = [field for field in VALUE_FIELD_NAMES if value.HasField(field)]
    assert len(fields) == 1
    return fields[0]


def add_layer(tile, name, extent=4096, version=2):
    layer = tile.layers.add()
    layer.name = name
    layer.version = version
    layer.extent = extent
    return layer


def first_tile():
    tile = vector_tile.tile()

    roads = add_layer(tile, "roads")
    roads.keys.extend(["kind", "name", "note"])
    roads.values.add().int_value = 9
    roads.values.add().string_value = "Main St"
    roads.values.add().string_value = "unused-a"
    feature = roads.features.add()
    feature.id = 10
    feature.type = tile.LineString
    feature.tags.extend([0, 0, 1, 1])
    feature.geometry.extend(ROAD_A1)
    feature = roads.features.add()
    feature.id = 11
    feature.type = tile.LineString
    feature.tags.extend([0, 0])
    feature.geometry.extend(ROAD_A2)

    only_a = add_layer(tile, "only_a")
    only_a.keys.append("k")
    only_a.values.add().bool_value = True
    feature = only_a.features.add()
    feature.id = 7
    feature.type = tile.Point
    feature.tags.extend([0, 0])
    feature.geometry.extend([9, 6, 10])

    water = add_layer(tile, "water")
    feature = water.features.add()
    feature.type = tile.Polygon
    feature.geometry.extend(SQUARE_A)

    return tile


def second_tile(extent=2048, version=2):
    tile = vector_tile.tile()

    roads = add_layer(tile, "roads")
    roads.keys.extend(["kind", "name"])
    roads.values.add().uint_value = 9
    roads.values.add().string_value = "Main St"
    roads.values.add().string_value = "unused-b"
    feature = roads.features.add()
    feature.id = 20
    feature.type = tile.LineString
    feature.tags.extend([0, 0, 1, 1])
    feature.geometry.extend(ROAD_B1)

    only_b = add_layer(tile, "only_b")
    only_b.keys.append("q")
    only_b.values.add().double_value = 1.5
    feature = only_b.features.add()
    feature.id = 99
    feature.type = tile.Point
    feature.tags.extend([0, 0])
    feature.geometry.extend([9, 2, 4])

    water = add_layer(tile, "water", extent=extent, version=version)
    feature = water.features.add()
    feature.type = tile.Polygon
    feature.geometry.extend(SQUARE_B)

    return tile


class BaseTestCase(unittest.TestCase):
    def build_output(self, tile_a=None, tile_b=None):
        tile_a = first_tile() if tile_a is None else tile_a
        tile_b = second_tile() if tile_b is None else tile_b
        merged_bytes, report = merge_tiles([tile_a.SerializeToString(), tile_b.SerializeToString()])
        output = vector_tile.tile()
        output.ParseFromString(merged_bytes)
        return tile_a, tile_b, output, report

    def test_merges_compatible_same_named_layer(self):
        tile_a, tile_b, output, report = self.build_output(tile_b=second_tile(extent=4096))

        self.assertIn("roads", report.merged_layers)
        self.assertEqual(report.conflicts, [])
        self.assertEqual(
            [layer.name for layer in output.layers],
            ["roads", "only_a", "water", "only_b"],
        )

        roads = next(layer for layer in output.layers if layer.name == "roads")
        self.assertEqual(roads.version, 2)
        self.assertEqual(roads.extent, 4096)

        # Feature order follows the input order: tile A features first, then tile B.
        self.assertEqual([feature.id for feature in roads.features], [10, 11, 20])
        # Geometry command streams are copied verbatim, without any decoding or quantization.
        self.assertEqual(list(roads.features[0].geometry), ROAD_A1)
        self.assertEqual(list(roads.features[1].geometry), ROAD_A2)
        self.assertEqual(list(roads.features[2].geometry), ROAD_B1)
        # Ids and types are preserved.
        self.assertEqual([feature.type for feature in roads.features], [tile_a.LineString] * 3)

    def test_remapped_tags_keep_each_value_representation(self):
        _, _, output, _ = self.build_output(tile_b=second_tile(extent=4096))
        roads = next(layer for layer in output.layers if layer.name == "roads")

        values = [(value_field(value), getattr(value, value_field(value))) for value in roads.values]
        # int_value 9 and uint_value 9 stay separate; the shared text is a single entry; unreferenced values drop.
        self.assertEqual(
            values,
            [("int_value", 9), ("string_value", "Main St"), ("uint_value", 9)],
        )

        kind_value_a = roads.values[roads.features[0].tags[1]]
        kind_value_b = roads.values[roads.features[2].tags[1]]
        self.assertTrue(kind_value_a.HasField("int_value"))
        self.assertFalse(kind_value_a.HasField("uint_value"))
        self.assertTrue(kind_value_b.HasField("uint_value"))
        self.assertFalse(kind_value_b.HasField("int_value"))

        # The identical, actually-used text value is shared by both tiles' features.
        self.assertEqual(roads.features[0].tags[3], roads.features[2].tags[3])
        name_value = roads.values[roads.features[0].tags[3]]
        self.assertEqual(name_value.string_value, "Main St")

        # Unreferenced values from both input tables are not carried over.
        self.assertNotIn("unused-a", [value.string_value for value in roads.values if value.HasField("string_value")])
        self.assertNotIn("unused-b", [value.string_value for value in roads.values if value.HasField("string_value")])

    def test_attributes_read_back_through_decode_entry_point(self):
        _, _, output, _ = self.build_output(tile_b=second_tile(extent=4096))
        decoded = mapbox_vector_tile.decode(output.SerializeToString())

        self.assertEqual(
            [feature["id"] for feature in decoded["roads"]["features"]],
            [10, 11, 20],
        )
        self.assertEqual(decoded["roads"]["features"][0]["properties"], {"kind": 9, "name": "Main St"})
        self.assertEqual(decoded["roads"]["features"][2]["properties"], {"kind": 9, "name": "Main St"})
        self.assertEqual(
            decoded["roads"]["features"][1]["geometry"]["coordinates"],
            [[2, 4093], [3, 4089]],
        )

    def test_unique_layers_are_preserved_verbatim(self):
        tile_a, tile_b, output, report = self.build_output(tile_b=second_tile(extent=4096))
        layers = {layer.name: layer for layer in output.layers}

        self.assertEqual(
            sorted(report.preserved_layers),
            [(0, "only_a"), (1, "only_b")],
        )
        self.assertEqual(layers["only_a"].SerializeToString(), tile_a.layers[1].SerializeToString())
        self.assertEqual(layers["only_b"].SerializeToString(), tile_b.layers[1].SerializeToString())

    def test_extent_conflict_is_refused_but_other_layers_still_delivered(self):
        tile_a, tile_b, output, report = self.build_output()

        self.assertEqual(len(report.conflicts), 1)
        conflict = report.conflicts[0]
        self.assertEqual(conflict.name, "water")
        self.assertEqual(conflict.extents, (2048, 4096))
        self.assertIn("extent", conflict.reason)

        layers = {layer.name: layer for layer in output.layers}
        # The first occurrence is delivered unchanged, the conflicting second one is not appended at all.
        self.assertEqual(layers["water"].extent, 4096)
        self.assertEqual(len(layers["water"].features), 1)
        self.assertEqual(list(layers["water"].features[0].geometry), SQUARE_A)
        self.assertEqual(layers["water"].SerializeToString(), tile_a.layers[2].SerializeToString())
        # The compatible same-named layer and the unique layers are still delivered.
        self.assertIn("roads", layers)
        self.assertEqual(len(layers["roads"].features), 3)
        self.assertIn("only_a", layers)
        self.assertIn("only_b", layers)

    def test_version_conflict_is_refused_with_reason(self):
        _, _, output, report = self.build_output(tile_b=second_tile(extent=4096, version=1))

        conflict = next(item for item in report.conflicts if item.name == "water")
        self.assertEqual(conflict.versions, (1, 2))
        self.assertIn("version", conflict.reason)
        layers = {layer.name: layer for layer in output.layers}
        self.assertEqual(layers["water"].version, 2)
        self.assertEqual(len(layers["water"].features), 1)

    def test_layer_with_unpreservable_referenced_value_is_refused(self):
        tile_a = first_tile()
        tile_b = second_tile(extent=4096)
        # An empty value message sets no specification field. It is actually referenced, so no representation can
        # be preserved and the layer must be refused rather than re-encoded.
        empty_index = len(tile_b.layers[0].values)
        tile_b.layers[0].values.add()
        tile_b.layers[0].features[0].tags.extend([1, empty_index])
        merged_bytes, report = merge_tiles([tile_a.SerializeToString(), tile_b.SerializeToString()])
        output = vector_tile.tile()
        output.ParseFromString(merged_bytes)

        conflict = next(item for item in report.conflicts if item.name == "roads")
        self.assertIn("representation", conflict.reason)
        layers = {layer.name: layer for layer in output.layers}
        # Refused layer keeps the first tile content only; no feature from the second tile is half-appended.
        self.assertEqual([feature.id for feature in layers["roads"].features], [10, 11])
        self.assertEqual(layers["roads"].SerializeToString(), tile_a.layers[0].SerializeToString())

    def test_unreferenced_unpreservable_value_is_dropped_not_refused(self):
        tile_a = first_tile()
        tile_b = second_tile(extent=4096)
        # An unreferenced empty value carries no information and is discarded; it must not block the merge.
        tile_b.layers[0].values.add()
        _, _, output, report = self.build_output(tile_a=tile_a, tile_b=tile_b)

        self.assertIn("roads", report.merged_layers)
        self.assertEqual([item.name for item in report.conflicts], [])
        roads = next(layer for layer in output.layers if layer.name == "roads")
        self.assertEqual([feature.id for feature in roads.features], [10, 11, 20])
        self.assertTrue(all(value_field(value) for value in roads.values))

    def test_same_named_layer_with_odd_tags_is_refused(self):
        tile_a = first_tile()
        tile_b = second_tile(extent=4096)
        del tile_b.layers[0].features[0].tags[:]
        tile_b.layers[0].features[0].tags.extend([0])
        merged_bytes, report = merge_tiles([tile_a.SerializeToString(), tile_b.SerializeToString()])
        output = vector_tile.tile()
        output.ParseFromString(merged_bytes)

        conflict = next(item for item in report.conflicts if item.name == "roads")
        self.assertIn("tags", conflict.reason)
        roads = next(layer for layer in output.layers if layer.name == "roads")
        self.assertEqual(roads.SerializeToString(), tile_a.layers[0].SerializeToString())

    def test_same_named_layer_with_out_of_range_tag_is_refused(self):
        tile_a = first_tile()
        tile_b = second_tile(extent=4096)
        tile_b.layers[0].features[0].tags.extend([0, 99])
        merged_bytes, report = merge_tiles([tile_a.SerializeToString(), tile_b.SerializeToString()])
        output = vector_tile.tile()
        output.ParseFromString(merged_bytes)

        conflict = next(item for item in report.conflicts if item.name == "roads")
        self.assertIn("out of range", conflict.reason)
        roads = next(layer for layer in output.layers if layer.name == "roads")
        self.assertEqual([feature.id for feature in roads.features], [10, 11])
        self.assertEqual(roads.SerializeToString(), tile_a.layers[0].SerializeToString())

    def test_three_inputs_share_used_text_and_keep_input_order(self):
        tile_a = first_tile()
        tile_b = second_tile(extent=4096)
        tile_c = vector_tile.tile()
        roads = add_layer(tile_c, "roads")
        roads.keys.append("name")
        roads.values.add().string_value = "Main St"
        feature = roads.features.add()
        feature.id = 30
        feature.type = tile_c.LineString
        feature.tags.extend([0, 0])
        feature.geometry.extend([9, 2, 2, 10, 6, 6])

        merged_bytes, report = merge_tiles(
            [tile_a.SerializeToString(), tile_b.SerializeToString(), tile_c.SerializeToString()]
        )
        output = vector_tile.tile()
        output.ParseFromString(merged_bytes)
        merged_roads = next(layer for layer in output.layers if layer.name == "roads")

        self.assertIn("roads", report.merged_layers)
        self.assertEqual([feature.id for feature in merged_roads.features], [10, 11, 20, 30])
        text_values = [
            value.string_value
            for value in merged_roads.values
            if value.HasField("string_value") and value.string_value == "Main St"
        ]
        self.assertEqual(text_values, ["Main St"])


if __name__ == "__main__":
    unittest.main()

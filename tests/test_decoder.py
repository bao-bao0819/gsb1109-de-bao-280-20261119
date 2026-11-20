"""
Tests for vector_tile/decoder.py
"""

import unittest

import mapbox_vector_tile
from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vector_tile
from mapbox_vector_tile.utils import DEFAULT_DECODE_OPTIONS, get_decode_options, zig_zag_encode


class BaseTestCase(unittest.TestCase):
    def test_decoder(self):
        vector_tile = (
            b'\x1aI\n\x05water\x12\x1a\x08\x01\x12\x06\x00\x00\x01\x01\x02\x02\x18\x03"\x0c\t\x00\x80@\x1a'
            b'\x00\x01\x02\x00\x00\x02\x0f\x1a\x03foo\x1a\x03baz\x1a\x03uid"\x05\n\x03bar"\x05\n\x03foo"\x02 {(\x80 '
            b"x\x02"
        )
        self.assertEqual(
            mapbox_vector_tile.decode(vector_tile),
            {
                "water": {
                    "version": 2,
                    "extent": 4096,
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
                            "properties": {"foo": "bar", "baz": "foo", "uid": 123},
                            "id": 1,
                            "type": "Feature",
                        }
                    ],
                },
            },
        )

    def test_decoder_geojson(self):
        vector_tile = (
            b'\x1aI\n\x05water\x12\x1a\x08\x01\x12\x06\x00\x00\x01\x01\x02\x02\x18\x03"\x0c\t\x00\x80@\x1a'
            b'\x00\x01\x02\x00\x00\x02\x0f\x1a\x03foo\x1a\x03baz\x1a\x03uid"\x05\n\x03bar"\x05\n\x03foo"\x02'
            b" {(\x80 x\x02"
        )
        self.assertEqual(
            mapbox_vector_tile.decode(vector_tile, default_options={"geojson": False}),
            {
                "water": {
                    "version": 2,
                    "extent": 4096,
                    "features": [
                        {
                            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
                            "properties": {"foo": "bar", "baz": "foo", "uid": 123},
                            "id": 1,
                            "type": 3,
                        }
                    ],
                },
            },
        )

    def test_decode_polygon_no_cmd_seg_end(self):
        # the binary here was generated without including the
        # CMD_SEG_END after the polygon parts
        # this tests that the decoder can detect that a new
        # CMD_MOVE_TO implicitly closes the previous polygon
        vector_tile = (
            b'\x1a+\n\x05water\x12\x1d\x18\x03"\x19\t\x00\x80@"\x08\x00\x00\x07\x07\x00\x00\x08\t\x02\x01"'
            b"\x00\x03\x04\x00\x00\x04\x03\x00(\x80 x\x02"
        )
        self.assertEqual(
            mapbox_vector_tile.decode(vector_tile),
            {
                "water": {
                    "version": 2,
                    "extent": 4096,
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
                                    [[1, 1], [1, 3], [3, 3], [3, 1], [1, 1]],
                                ],
                            },
                            "properties": {},
                            "id": 0,
                            "type": "Feature",
                        }
                    ],
                },
            },
        )

    def test_nondefault_extent(self):
        vector_tile = (
            b'\x1aK\n\x05water\x12\x1c\x08\x01\x12\x06\x00\x00\x01\x01\x02\x02\x18\x02"\x0e\t\x80}\xd0\x12'
            b'\x12\xbf>\xd86\xbf>\xd86\x1a\x03foo\x1a\x03baz\x1a\x03uid"\x05\n\x03bar"\x05\n\x03foo"\x02 '
            b"{(\x80@x\x02"
        )
        self.assertEqual(
            mapbox_vector_tile.decode(vector_tile),
            {
                "water": {
                    "version": 2,
                    "extent": 8192,
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "geometry": {"type": "LineString", "coordinates": [[8000, 7000], [4000, 3500], [0, 0]]},
                            "id": 1,
                            "properties": {"baz": "foo", "foo": "bar", "uid": 123},
                            "type": "Feature",
                        }
                    ],
                }
            },
        )

    def test_options(self):
        layer_options_1 = {"y_coord_down": True, "transformer": "my_function"}
        layer_options_2 = {"geojson": True}
        default_options = {"geojson": False}
        self.assertEqual(
            get_decode_options(layer_options=layer_options_1, default_options=default_options),
            {**layer_options_1, "geojson": False, "layers": None, "fields": None},
        )
        self.assertEqual(
            get_decode_options(layer_options=layer_options_2, default_options=default_options),
            {**layer_options_2, "y_coord_down": False, "transformer": None, "layers": None, "fields": None},
        )
        self.assertEqual(
            get_decode_options(layer_options=layer_options_2, default_options=None),
            {**layer_options_2, "y_coord_down": False, "transformer": None, "layers": None, "fields": None},
        )
        self.assertEqual(
            get_decode_options(layer_options=None, default_options=layer_options_1),
            {**layer_options_1, "geojson": True, "layers": None, "fields": None},
        )
        self.assertEqual(get_decode_options(layer_options=None, default_options=None), DEFAULT_DECODE_OPTIONS)

    def test_options_error(self):
        expected_result = "The following options are not allowed for decoding a tile: 'opt', 'unknown'."
        with self.assertRaises(ValueError) as ex:
            get_decode_options(layer_options={"geojson": False, "unknown": 23}, default_options={"opt": 42})
        self.assertEqual(str(ex.exception), expected_result)

    def test_selection_options_error(self):
        for option_name in ("layers", "fields"):
            with self.assertRaises(ValueError) as ex:
                get_decode_options(layer_options={option_name: "roads"}, default_options=None)
            self.assertEqual(
                str(ex.exception), f"The {option_name!r} option must be an iterable of strings when decoding a tile."
            )
            with self.assertRaises(ValueError) as ex:
                get_decode_options(layer_options={option_name: [1, 2]}, default_options=None)
            self.assertEqual(
                str(ex.exception), f"The {option_name!r} option must be an iterable of strings when decoding a tile."
            )


class SelectionTestCase(unittest.TestCase):
    """Layer and field selection while decoding a tile."""

    def setUp(self):
        self.roads_geometry = [
            {"type": "LineString", "coordinates": [[10, 4076], [30, 4056]]},
            {"type": "LineString", "coordinates": [[50, 4036], [70, 4016], [90, 3996]]},
        ]
        self.roads_properties = [{"name": "Main St", "level": 1}, {"name": "2nd Ave", "level": 2}]
        self.tile = self._build_tile(include_unreadable_value=True)
        self.clean_tile = self._build_tile(include_unreadable_value=False)

    @staticmethod
    def _line_commands(points):
        commands = []
        previous_x = previous_y = 0
        for index, (x, y) in enumerate(points):
            header = 9 if index == 0 else 10
            commands.extend([header, zig_zag_encode(x - previous_x), zig_zag_encode(y - previous_y)])
            previous_x, previous_y = x, y
        return commands

    @staticmethod
    def _add_value(layer, value):
        message = vector_tile.tile.value()
        if isinstance(value, str):
            message.string_value = value
        elif isinstance(value, bool):
            message.bool_value = value
        else:
            message.sint_value = value
        for index, existing in enumerate(layer.values):
            if existing == message:
                return index
        layer.values.append(message)
        return len(layer.values) - 1

    def _build_tile(self, include_unreadable_value):
        tile = vector_tile.tile()

        roads = tile.layers.add()
        roads.version = 2
        roads.name = "roads"
        roads.extent = 4096
        for feature_id, points, properties in (
            (1, [(10, 20), (30, 40)], {"name": "Main St", "level": 1}),
            (2, [(50, 60), (70, 80), (90, 100)], {"name": "2nd Ave", "level": 2}),
        ):
            feature = roads.features.add()
            feature.id = feature_id
            feature.type = 2
            feature.geometry.extend(self._line_commands(points))
            for key, value in properties.items():
                if key not in roads.keys:
                    roads.keys.append(key)
                key_index = list(roads.keys).index(key)
                feature.tags.extend([key_index, self._add_value(roads, value)])

        water = tile.layers.add()
        water.version = 2
        water.name = "water"
        water.extent = 4096
        water.keys.append("name")
        water_feature = water.features.add()
        water_feature.id = 3
        water_feature.type = 3
        water_feature.geometry.extend(
            [
                9,
                0,
                0,
                10,
                zig_zag_encode(4),
                0,
                10,
                0,
                zig_zag_encode(4),
                10,
                zig_zag_encode(-4),
                zig_zag_encode(-4),
                15,
            ]
        )
        water_feature.tags.extend([0, self._add_value(water, "pond")])

        if include_unreadable_value:
            pois = tile.layers.add()
            pois.version = 2
            pois.name = "pois"
            pois.extent = 4096
            pois.keys.append("name")
            pois.keys.append("mystery")
            name_value = vector_tile.tile.value()
            name_value.string_value = "shop"
            pois.values.append(name_value)
            pois.values.append(vector_tile.tile.value())  # value present in the protobuf but with no readable type
            pois_feature = pois.features.add()
            pois_feature.id = 9
            pois_feature.type = 1
            pois_feature.tags.extend([0, 0, 1, 1])
            pois_feature.geometry.extend([9, zig_zag_encode(5), zig_zag_encode(6)])

        return tile.SerializeToString()

    def test_default_selection_decodes_every_layer_and_field(self):
        result = mapbox_vector_tile.decode(self.clean_tile)
        self.assertEqual(set(result.keys()), {"roads", "water"})
        self.assertEqual([feature["properties"] for feature in result["roads"]["features"]], self.roads_properties)

    def test_full_decode_keeps_error_contract_for_unreadable_value(self):
        with self.assertRaises(ValueError) as ex:
            mapbox_vector_tile.decode(self.tile)
        self.assertTrue(str(ex.exception).endswith("is an unknown value"))

    def test_select_road_layer_and_its_name_field_only(self):
        result = mapbox_vector_tile.decode(self.tile, default_options={"layers": ["roads"], "fields": ["name"]})
        self.assertEqual(list(result.keys()), ["roads"])
        self.assertEqual(
            [feature["properties"] for feature in result["roads"]["features"]],
            [{"name": "Main St"}, {"name": "2nd Ave"}],
        )

    def test_explicit_empty_field_list_yields_empty_properties(self):
        result = mapbox_vector_tile.decode(self.tile, default_options={"layers": ["roads"], "fields": []})
        self.assertEqual([feature["properties"] for feature in result["roads"]["features"]], [{}, {}])

    def test_empty_layer_list_yields_empty_tile(self):
        self.assertEqual(mapbox_vector_tile.decode(self.tile, default_options={"layers": []}), {})

    def test_unknown_field_name_yields_empty_properties(self):
        result = mapbox_vector_tile.decode(self.tile, default_options={"layers": ["roads"], "fields": ["missing"]})
        self.assertEqual([feature["properties"] for feature in result["roads"]["features"]], [{}, {}])

    def test_road_features_geometry_order_and_direction_match_full_sample(self):
        full = mapbox_vector_tile.decode(self.clean_tile)
        filtered = mapbox_vector_tile.decode(self.tile, default_options={"layers": ["roads"], "fields": ["name"]})
        full_roads = full["roads"]["features"]
        filtered_roads = filtered["roads"]["features"]
        self.assertEqual([feature["id"] for feature in filtered_roads], [feature["id"] for feature in full_roads])
        self.assertEqual([feature["geometry"] for feature in filtered_roads], self.roads_geometry)
        self.assertEqual(
            [feature["geometry"] for feature in filtered_roads], [feature["geometry"] for feature in full_roads]
        )

    def test_layer_with_unreadable_value_decodes_when_property_excluded(self):
        result = mapbox_vector_tile.decode(self.tile, default_options={"layers": ["pois"], "fields": ["name"]})
        self.assertEqual(set(result.keys()), {"pois"})
        feature = result["pois"]["features"][0]
        self.assertEqual(feature["properties"], {"name": "shop"})
        self.assertEqual(feature["geometry"], {"type": "Point", "coordinates": [5, 4090]})

    def test_layer_with_unreadable_value_raises_when_property_selected(self):
        with self.assertRaises(ValueError) as ex:
            mapbox_vector_tile.decode(self.tile, default_options={"layers": ["pois"], "fields": ["name", "mystery"]})
        self.assertTrue(str(ex.exception).endswith("is an unknown value"))

    def test_unselected_layer_does_not_invoke_its_transformer(self):
        def failing_transformer(x, y):
            raise RuntimeError("the transformer of an unselected layer must not be called")

        result = mapbox_vector_tile.decode(
            self.tile,
            default_options={"layers": ["roads"]},
            per_layer_options={"water": {"transformer": failing_transformer}},
        )
        self.assertEqual(list(result.keys()), ["roads"])

    def test_selected_layers_keep_their_own_y_and_coordinate_transformations(self):
        def roads_transformer(x, y):
            return x + 1000, y + 2000

        def water_transformer(x, y):
            return -x, -y

        result = mapbox_vector_tile.decode(
            self.clean_tile,
            default_options={"layers": ["roads", "water"], "transformer": water_transformer, "y_coord_down": True},
            per_layer_options={"roads": {"transformer": roads_transformer, "y_coord_down": True}},
        )
        self.assertEqual(
            [feature["geometry"] for feature in result["roads"]["features"]],
            [
                {"type": "LineString", "coordinates": [[1010, 2020], [1030, 2040]]},
                {"type": "LineString", "coordinates": [[1050, 2060], [1070, 2080], [1090, 2100]]},
            ],
        )
        self.assertEqual(
            result["water"]["features"][0]["geometry"],
            {"type": "Polygon", "coordinates": [[[0, 0], [-4, 0], [-4, -4], [0, 0]]]},
        )

    def test_per_layer_field_selection(self):
        result = mapbox_vector_tile.decode(
            self.tile,
            default_options={"layers": ["roads", "pois"]},
            per_layer_options={"roads": {"fields": ["name"]}, "pois": {"fields": ["name"]}},
        )
        self.assertEqual(
            [feature["properties"] for feature in result["roads"]["features"]],
            [{"name": "Main St"}, {"name": "2nd Ave"}],
        )
        self.assertEqual(result["pois"]["features"][0]["properties"], {"name": "shop"})

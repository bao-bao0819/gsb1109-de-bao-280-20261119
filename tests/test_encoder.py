"""
Tests for vector_tile/encoder.py
"""

import math
import unittest

from shapely import wkt

import mapbox_vector_tile
from mapbox_vector_tile import decode, encode
from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vector_tile
from mapbox_vector_tile.utils import DEFAULT_ENCODE_OPTIONS, get_encode_options


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.layer_name = "water"
        self.feature_properties = {"uid": 123, "foo": "bar", "baz": "foo"}
        self.feature_geometry = "POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))"

    def assertRoundTrip(
        self,
        input_geometry,
        expected_geometry,
        name=None,
        properties=None,
        id=None,
        expected_len=1,
        expected_properties=None,
    ):
        if input_geometry is None:
            input_geometry = self.feature_geometry
        if name is None:
            name = self.layer_name
        if properties is None:
            properties = self.feature_properties
        if expected_properties is None:
            expected_properties = properties
        source = [{"name": name, "features": [{"geometry": input_geometry, "properties": properties}]}]
        if id:
            source[0]["features"][0]["id"] = id
        encoded = encode(source)
        decoded = decode(encoded)
        self.assertIn(name, decoded)
        layer = decoded[name]
        features = layer["features"]
        self.assertEqual(expected_len, len(features))
        self.assertEqual(features[0]["properties"], expected_properties)
        self.assertEqual(features[0]["geometry"], expected_geometry)
        if id:
            self.assertEqual(features[0]["id"], id)


class TestDifferentGeomFormats(BaseTestCase):
    def test_encoder(self):
        self.assertRoundTrip(
            input_geometry="POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))",
            expected_geometry={"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        )

    def test_encoder_point(self):
        self.assertRoundTrip(input_geometry="POINT (1 2)", expected_geometry={"type": "Point", "coordinates": [1, 2]})

    def test_encoder_multipoint(self):
        self.assertRoundTrip(
            input_geometry="MULTIPOINT (1 2, 3 4)",
            expected_geometry={"type": "MultiPoint", "coordinates": [[1, 2], [3, 4]]},
        )

    def test_encoder_linestring(self):
        self.assertRoundTrip(
            input_geometry="LINESTRING (30 10, 10 30, 40 40)",
            expected_geometry={"type": "LineString", "coordinates": [[30, 10], [10, 30], [40, 40]]},
        )

    def test_encoder_multilinestring(self):
        self.assertRoundTrip(
            input_geometry="MULTILINESTRING ((10 10, 20 20, 10 40), (40 40, 30 30, 40 20, 30 10))",
            expected_geometry={
                "type": "MultiLineString",
                "coordinates": [[[10, 10], [20, 20], [10, 40]], [[40, 40], [30, 30], [40, 20], [30, 10]]],
            },
        )

    def test_encoder_polygon(self):
        self.assertRoundTrip(
            input_geometry="POLYGON ((30 10, 40 40, 20 40, 10 20, 30 10))",
            expected_geometry={"type": "Polygon", "coordinates": [[[30, 10], [10, 20], [20, 40], [40, 40], [30, 10]]]},
        )

    def test_encoder_polygon_w_hole(self):
        self.assertRoundTrip(
            input_geometry="POLYGON ((35 10, 45 45, 15 40, 10 20, 35 10), (20 30, 35 35, 30 20, 20 30))",
            expected_geometry={
                "type": "Polygon",
                "coordinates": [
                    [[35, 10], [10, 20], [15, 40], [45, 45], [35, 10]],
                    [[20, 30], [30, 20], [35, 35], [20, 30]],
                ],
            },
        )

    def test_encoder_multipolygon(self):
        self.assertRoundTrip(
            input_geometry="MULTIPOLYGON (((30 20, 45 40, 10 40, 30 20)), ((15 5, 40 10, 10 20, 5 10, 15 5)))",
            expected_geometry={
                "type": "MultiPolygon",
                "coordinates": [
                    [[[30, 20], [10, 40], [45, 40], [30, 20]]],
                    [[[15, 5], [5, 10], [10, 20], [40, 10], [15, 5]]],
                ],
            },
        )

    def test_encoder_multipolygon_w_hole(self):
        self.assertRoundTrip(
            input_geometry="MULTIPOLYGON (((40 40, 20 45, 45 30, 40 40)), "
            "((20 35, 10 30, 10 10, 30 5, 45 20, 20 35), "
            "(30 20, 20 15, 20 25, 30 20)))",
            expected_geometry={
                "type": "MultiPolygon",
                "coordinates": [
                    [[[40, 40], [45, 30], [20, 45], [40, 40]]],
                    [
                        [[20, 35], [45, 20], [30, 5], [10, 10], [10, 30], [20, 35]],
                        [[30, 20], [20, 25], [20, 15], [30, 20]],
                    ],
                ],
            },
        )

    def test_encoder_quantize_before_orient(self):
        self.assertRoundTrip(
            input_geometry="POLYGON ((0 0, 4 0, 4 4, 0 4, 0 0), (1 1, 3 2, 2 2, 1 1))",
            expected_geometry={
                "type": "Polygon",
                "coordinates": [[[0, 0], [0, 4], [4, 4], [4, 0], [0, 0]], [[1, 1], [3, 2], [2, 2], [1, 1]]],
            },
        )

    def test_encoder_winding_order_polygon(self):
        # example from the spec
        # https://github.com/mapbox/vector-tile-spec/tree/master/2.1#4355-example-polygon   ()
        # the order given in the example is clockwise in a y-up coordinate
        # system, but the coordinate system given for the example is y-down!
        # therefore the y coordinate in this example is flipped negative.
        self.assertRoundTrip(
            input_geometry="POLYGON ((3 -6, 8 -12, 20 -34, 3 -6))",
            expected_geometry={"type": "Polygon", "coordinates": [[[3, -6], [8, -12], [20, -34], [3, -6]]]},
        )

    def test_encoder_winding_order_polygon_reverse(self):
        # tests that encode _corrects_ the winding order
        # example is the same as above - note the flipped coordinate system.
        self.assertRoundTrip(
            input_geometry="POLYGON ((3 -6, 20 -34, 8 -12, 3 -6))",
            expected_geometry={"type": "Polygon", "coordinates": [[[3, -6], [8, -12], [20, -34], [3, -6]]]},
        )

    def test_encoder_winding_order_multipolygon(self):
        # example from the spec
        # https://github.com/mapbox/vector-tile-spec/tree/master/2.1#4356-example-multi-polygon   ()
        # the order given in the example is clockwise in a y-up coordinate
        # system, but the coordinate system given for the example is y-down!
        self.assertRoundTrip(
            input_geometry=(
                "MULTIPOLYGON ("
                + "((0 0, 10 0, 10 -10, 0 -10, 0 0)),"
                + "((11 -11, 20 -11, 20 -20, 11 -20, 11 -11),"
                + " (13 -13, 13 -17, 17 -17, 17 -13, 13 -13)))"
            ),
            expected_geometry={
                "type": "MultiPolygon",
                "coordinates": [
                    [[[0, 0], [10, 0], [10, -10], [0, -10], [0, 0]]],
                    [
                        [[11, -11], [20, -11], [20, -20], [11, -20], [11, -11]],
                        [[13, -13], [13, -17], [17, -17], [17, -13], [13, -13]],
                    ],
                ],
            },
        )

    def test_encoder_ensure_winding_after_quantization(self):
        self.assertRoundTrip(
            input_geometry="POLYGON ((0 0, 4 0, 4 4, 0 4, 0 0), (1 1, 3 2.4, 2 1.6, 1 1))",
            # should be single polygon with hole
            expected_geometry={
                "type": "Polygon",
                "coordinates": [[[0, 0], [0, 4], [4, 4], [4, 0], [0, 0]], [[1, 1], [3, 2], [2, 2], [1, 1]]],
            },
        )
        # but becomes multi-polygon
        # expected_geometry=[[[[0, 0], [0, 4], [4, 4], [4, 0], [0, 0]]],
        #                   [[[1, 1], [2, 2], [3, 2], [1, 1]]]])

    def test_with_wkt(self):
        self.assertRoundTrip(
            input_geometry="LINESTRING(-71.160281 42.258729,-71.160837 43.259113,-71.161144 42.25932)",
            expected_geometry={"type": "LineString", "coordinates": [[-71, 42], [-71, 43], [-71, 42]]},
        )

    def test_with_wkb(self):
        self.assertRoundTrip(
            input_geometry=b"\001\003\000\000\000\001\000\000\000\005\000\000\000\000\000\000\000\000\000\000\000\000"
            b"\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\360?"
            b"\000\000\000\000\000\000\360?\000\000\000\000\000\000\360?\000\000\000\000\000\000\360?"
            b"\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000"
            b"\000\000\000",
            expected_geometry={"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        )

    def test_with_shapely(self):
        geometry = "LINESTRING(-71.160281 42.258729,-71.160837 43.259113,-71.161144 42.25932)"
        geometry = wkt.loads(geometry)
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={"type": "LineString", "coordinates": [[-71, 42], [-71, 43], [-71, 42]]},
        )

    def test_with_invalid_geometry(self):
        expected_result = "Can't do geometries that are not wkt, wkb, or shapely geometries"
        with self.assertRaises(NotImplementedError) as ex:
            mapbox_vector_tile.encode(
                [{"name": self.layer_name, "features": [{"geometry": "xyz", "properties": self.feature_properties}]}]
            )
        self.assertEqual(str(ex.exception), expected_result)

    def test_encode_unicode_property(self):
        geometry = "LINESTRING(-71.160281 42.258729,-71.160837 43.259113,-71.161144 42.25932)"
        properties = {
            "foo": str(self.feature_properties["foo"]),
            "baz": str(self.feature_properties["baz"]),
        }
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={"type": "LineString", "coordinates": [[-71, 42], [-71, 43], [-71, 42]]},
            properties=properties,
        )

    def test_encode_unicode_property_key(self):
        geometry = "LINESTRING(-71.160281 42.258729,-71.160837 43.259113,-71.161144 42.25932)"
        properties = {"☺": "☺"}
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={"type": "LineString", "coordinates": [[-71, 42], [-71, 43], [-71, 42]]},
            properties=properties,
        )

    def test_encode_float_little_endian(self):
        geometry = "LINESTRING(-71.160281 42.258729,-71.160837 43.259113,-71.161144 42.25932)"
        properties = {"floatval": 3.14159}
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={"type": "LineString", "coordinates": [[-71, 42], [-71, 43], [-71, 42]]},
            properties=properties,
        )

    def test_encode_feature_with_id(self):
        geometry = "POINT(1 1)"
        self.assertRoundTrip(input_geometry=geometry, expected_geometry={"type": "Point", "coordinates": [1, 1]}, id=42)

    def test_encode_point(self):
        geometry = "POINT(1 1)"
        self.assertRoundTrip(input_geometry=geometry, expected_geometry={"type": "Point", "coordinates": [1, 1]}, id=42)

    def test_encode_polygon_reverse_winding_order(self):
        geometry = "POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))"
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        )

    def test_encode_multipoint(self):
        geometry = "MULTIPOINT((10 10), (20 20), (10 40), (40 40), (30 30), (40 20), (30 10))"
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={
                "type": "MultiPoint",
                "coordinates": [[10, 10], [20, 20], [10, 40], [40, 40], [30, 30], [40, 20], [30, 10]],
            },
        )

    def test_encode_multilinestring(self):
        geometry = "MULTILINESTRING ((10 10, 20 20, 10 40), (40 40, 30 30, 40 20, 30 10))"
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={
                "type": "MultiLineString",
                "coordinates": [
                    [[10, 10], [20, 20], [10, 40]],
                    [[40, 40], [30, 30], [40, 20], [30, 10]],
                ],
            },
        )

    def test_encode_multipolygon_normal_winding_order(self):
        geometry = (
            "MULTIPOLYGON (((40 40, 20 45, 45 30, 40 40)), ((20 35, 10 30, 10 10, 30 5, 45 20, 20 35), "
            "(30 20, 20 15, 20 25, 30 20)))"
        )
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={
                "type": "MultiPolygon",
                "coordinates": [
                    [[[40, 40], [45, 30], [20, 45], [40, 40]]],
                    [
                        [[20, 35], [45, 20], [30, 5], [10, 10], [10, 30], [20, 35]],
                        [[30, 20], [20, 25], [20, 15], [30, 20]],
                    ],
                ],
            },
            expected_len=1,
        )

    def test_encode_multipolygon_normal_winding_order_zero_area(self):
        geometry = (
            "MULTIPOLYGON (((40 40, 40 20, 40 45, 40 40)), ((20 35, 10 30, 10 10, 30 5, 45 20, 20 35), "
            "(30 20, 20 15, 20 25, 30 20)))"
        )
        # NB there is only one resultant polygon here
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={
                "type": "Polygon",
                "coordinates": [
                    [[20, 35], [45, 20], [30, 5], [10, 10], [10, 30], [20, 35]],
                    [[30, 20], [20, 25], [20, 15], [30, 20]],
                ],
            },
            expected_len=1,
        )

    def test_encode_multipolygon_reverse_winding_order(self):
        geometry = "MULTIPOLYGON (((10 10, 10 0, 0 0, 0 10, 10 10), (8 8, 2 8, 2 0, 8 0, 8 8)))"
        # NB there is only one resultant polygon here
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={
                "type": "Polygon",
                "coordinates": [
                    [[10, 10], [10, 0], [0, 0], [0, 10], [10, 10]],
                    [[8, 8], [2, 8], [2, 0], [8, 0], [8, 8]],
                ],
            },
            expected_len=1,
        )

    def test_encode_property_bool(self):
        geometry = "POINT(0 0)"
        properties = {"test_bool_true": True, "test_bool_false": False}
        self.assertRoundTrip(
            input_geometry=geometry, expected_geometry={"type": "Point", "coordinates": [0, 0]}, properties=properties
        )

    def test_encode_property_int(self):
        geometry = "POINT(0 0)"
        properties = {
            "test_int": 1,
        }
        self.assertRoundTrip(
            input_geometry=geometry, expected_geometry={"type": "Point", "coordinates": [0, 0]}, properties=properties
        )

    def test_encode_property_null(self):
        geometry = "POINT(0 0)"
        properties = {"test_none": None, "test_empty": ""}
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={"type": "Point", "coordinates": [0, 0]},
            properties=properties,
            expected_properties={"test_empty": ""},
        )

    def test_encode_property_list(self):
        geometry = "POINT(0 0)"
        properties = {"test_list": [1, 2, 3], "test_empty": ""}
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry={"type": "Point", "coordinates": [0, 0]},
            properties=properties,
            expected_properties={"test_empty": ""},
        )

    def test_encode_multiple_values_test(self):
        geometry = "POINT(0 0)"
        properties1 = {"foo": "bar", "baz": "bar"}
        properties2 = {"quux": "morx", "baz": "bar"}
        name = "foo"
        feature1 = {"geometry": geometry, "properties": properties1}
        feature2 = {"geometry": geometry, "properties": properties2}
        source = [{"name": name, "features": [feature1, feature2]}]
        encoded = encode(source)
        decoded = decode(encoded)
        self.assertIn(name, decoded)
        layer = decoded[name]
        features = layer["features"]
        self.assertEqual(2, len(features))
        self.assertEqual(features[0]["properties"], properties1)
        self.assertEqual(features[1]["properties"], properties2)

    def test_encode_rounding_floats(self):
        geometry = "LINESTRING(1.1 1.1, 41.5 41.8)"
        exp_geoemtry = {"type": "LineString", "coordinates": [[1, 1], [42, 42]]}
        self.assertRoundTrip(
            input_geometry=geometry,
            expected_geometry=exp_geoemtry,
        )

    def test_too_small_linestring(self):
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        shape = shapely.wkt.loads("LINESTRING(-71.160281 42.258729,-71.160837 42.259113,-71.161144 42.25932)")
        features = [{"geometry": shape, "properties": {}}]
        pbf = encode(
            {"name": "foo", "features": features},
            default_options={"on_invalid_geometry": on_invalid_geometry_make_valid},
        )
        result = decode(pbf)
        features = result["foo"]["features"]
        self.assertEqual(0, len(features))

    def test_encode_1_true_values(self):
        geometry = "POINT(0 0)"
        properties = {
            "foo": True,
            "bar": 1,
        }
        source = [{"name": "layer", "features": [{"geometry": geometry, "properties": properties}]}]
        encoded = encode(source)
        decoded = decode(encoded)
        layer = decoded["layer"]
        features = layer["features"]
        act_props = features[0]["properties"]
        self.assertEqual(act_props["foo"], True)
        self.assertEqual(act_props["bar"], 1)
        self.assertTrue(isinstance(act_props["foo"], bool))
        self.assertFalse(isinstance(act_props["bar"], bool))


class ValueDictionaryTestCase(unittest.TestCase):
    """Values must be de-duplicated by their actual protobuf field, not by Python equality.

    Python considers ``True``, ``1`` and ``1.0`` equal, and ``0.0`` equal to ``-0.0``, so ordinary value comparisons
    cannot verify these properties. The assertions below inspect the raw protobuf values (including the float sign
    bit) instead of comparing Python values.
    """

    MIXED = {
        "bool_true": True,
        "int_one": 1,
        "float_one": 1.0,
        "bool_false": False,
        "int_zero": 0,
        "float_zero": 0.0,
        "float_negative_zero": -0.0,
    }

    EXTRA = {
        "repeat_float_one": 1.0,
        "repeat_int_one": 1,
        "repeat_bool_true": True,
        "string": "hello",
        "normal_float": 2.5,
        "normal_int": 7,
    }

    # Two layers with the same properties, so each layer repeats the values of the other one.
    LAYERS = {"first": {1: MIXED, 2: EXTRA}, "second": {1: MIXED, 2: EXTRA}}

    # Properties which are unsupported and must keep being ignored, regardless of the value dictionary handling.
    UNSUPPORTED = {"none": None, "list": [1, 2, 3], "object": {"a": 1}}
    # The empty string is a supported value and has to be preserved, as before.
    SUPPORTED_EXTRA = {"empty_string": ""}

    # Expected ``(protobuf field, native value, sign)`` for every accepted property.
    EXPECTED = {
        "bool_true": ("bool_value", True, None),
        "bool_false": ("bool_value", False, None),
        "repeat_bool_true": ("bool_value", True, None),
        "int_one": ("int_value", 1, None),
        "int_zero": ("int_value", 0, None),
        "repeat_int_one": ("int_value", 1, None),
        "normal_int": ("int_value", 7, None),
        "float_one": ("double_value", 1.0, 1.0),
        "float_zero": ("double_value", 0.0, 1.0),
        "float_negative_zero": ("double_value", -0.0, -1.0),
        "repeat_float_one": ("double_value", 1.0, 1.0),
        "normal_float": ("double_value", 2.5, 1.0),
        "string": ("string_value", "hello", None),
        "empty_string": ("string_value", "", None),
    }

    VALUE_FIELDS = (
        "string_value",
        "float_value",
        "double_value",
        "int_value",
        "uint_value",
        "sint_value",
        "bool_value",
    )

    @classmethod
    def _parse(cls, pbf):
        tile = vector_tile.tile()
        tile.ParseFromString(pbf)
        return tile

    @classmethod
    def _field(cls, value):
        for name in cls.VALUE_FIELDS:
            if value.HasField(name):
                return name
        raise AssertionError(f"{value} has no value field")

    @classmethod
    def _field_and_value(cls, value):
        """Return ``(field name, native value)``, keeping signed zero distinct via ``copysign``."""
        field = cls._field(value)
        native = getattr(value, field)
        if field in ("float_value", "double_value"):
            native = (native, math.copysign(1.0, native))
        return field, native

    @classmethod
    def _expected(cls, key):
        field, native, sign = cls.EXPECTED[key]
        return (field, (native, sign)) if sign is not None else (field, native)

    @classmethod
    def _expected_props(cls, props):
        return {key: cls._expected(key) for key in props}

    @classmethod
    def _properties_by_id(cls, layer):
        """Map each feature ID to the raw ``(field, value)`` tuple for every property."""
        result = {}
        for feature in layer.features:
            tags = list(feature.tags)
            assert len(tags) % 2 == 0, "Unexpected number of tags"
            props = {}
            for key_idx, val_idx in zip(tags[::2], tags[1::2]):
                props[layer.keys[key_idx]] = cls._field_and_value(layer.values[val_idx])
            result[feature.id] = props
        return result

    @classmethod
    def _type_counts(cls, props_by_id):
        """Count distinct value entries per actual protobuf field."""
        values = {cls._expected(key) for props in props_by_id.values() for key in props}
        counts = {}
        for field, _ in values:
            counts[field] = counts.get(field, 0) + 1
        return counts

    @classmethod
    def _layer_type_counts(cls, layer):
        counts = {}
        for value in layer.values:
            field = cls._field(value)
            counts[field] = counts.get(field, 0) + 1
        return counts

    @staticmethod
    def _features(props_by_id, reverse=False):
        items = list(props_by_id.items())
        if reverse:
            items.reverse()
        return [
            {"id": fid, "geometry": "POINT(1 1)", "properties": props if not reverse else dict(reversed(props.items()))}
            for fid, props in items
        ]

    def _encode(self, reverse=False):
        layers = [{"name": name, "features": self._features(props, reverse)} for name, props in self.LAYERS.items()]
        if reverse:
            layers.reverse()
        return encode(layers)

    def test_mixed_types_in_one_layer_do_not_merge(self):
        props = {**self.MIXED, **self.SUPPORTED_EXTRA, **self.UNSUPPORTED}
        pbf = encode([{"name": "layer", "features": [{"id": 1, "geometry": "POINT(0 0)", "properties": props}]}])
        layer = self._parse(pbf).layers[0]

        # Only the supported values are encoded, with one entry per actual type and per zero sign.
        self.assertEqual(8, len(layer.values))
        self.assertEqual(
            {"bool_value", "double_value", "int_value", "string_value"}, {self._field(v) for v in layer.values}
        )
        self.assertEqual(
            self._expected_props({**self.MIXED, **self.SUPPORTED_EXTRA}), self._properties_by_id(layer)[1]
        )

    def test_same_type_and_representation_shares_one_index_in_layer(self):
        pbf = encode(
            [
                {
                    "name": "layer",
                    "features": [
                        {"id": 1, "geometry": "POINT(0 0)", "properties": {"a": 1.5, "b": 1.5}},
                        {"id": 2, "geometry": "POINT(0 0)", "properties": {"a": 1.5}},
                        {"id": 3, "geometry": "POINT(0 0)", "properties": {"a": 1, "b": 1.0}},
                    ],
                }
            ]
        )
        layer = self._parse(pbf).layers[0]
        # The repeated double 1.5 is shared; int 1 and double 1.0 must not share.
        self.assertEqual(3, len(layer.values))
        by_id = self._properties_by_id(layer)
        self.assertEqual(("double_value", (1.5, 1.0)), by_id[1]["a"])
        self.assertEqual(by_id[1]["a"], by_id[2]["a"])
        self.assertEqual(("int_value", 1), by_id[3]["a"])
        self.assertEqual(("double_value", (1.0, 1.0)), by_id[3]["b"])

    def test_layers_keep_separate_value_dictionaries(self):
        tile = self._parse(self._encode())
        self.assertEqual(2, len(tile.layers))
        for layer in tile.layers:
            self.assertEqual(self._type_counts(self.LAYERS[layer.name]), self._layer_type_counts(layer))
            tags = [tag for feature in layer.features for tag in feature.tags[1::2]]
            self.assertTrue(all(0 <= tag < len(layer.values) for tag in tags))

    def test_types_and_zero_signs_survive_swapped_order_and_reencode(self):
        for reverse in (False, True):
            source = self._encode(reverse=reverse)
            self.assertEqual(self._expected_tile(), self._by_id(source))

            # Re-encode from the decoded properties, swapping the property order again.
            decoded = decode(source)
            reencoded = encode(
                [
                    {
                        "name": name,
                        "features": self._features(
                            {feature["id"]: feature["properties"] for feature in decoded[name]["features"]},
                            reverse=not reverse,
                        ),
                    }
                    for name in decoded
                ]
            )
            self.assertEqual(self._expected_tile(), self._by_id(reencoded))
            # Acceptance check: entries counted by their actual protobuf type after re-encoding.
            for layer in self._parse(reencoded).layers:
                self.assertEqual(self._type_counts(self.LAYERS[layer.name]), self._layer_type_counts(layer))

    @classmethod
    def _expected_tile(cls):
        return {
            name: {fid: cls._expected_props(props) for fid, props in props_by_id.items()}
            for name, props_by_id in cls.LAYERS.items()
        }

    @classmethod
    def _by_id(cls, pbf):
        return {layer.name: cls._properties_by_id(layer) for layer in cls._parse(pbf).layers}


class TestDictGeometries(BaseTestCase):

    def _test_encoder_dict(self, geometry):
        self.assertRoundTrip(input_geometry=geometry, expected_geometry=geometry)

    def test_encoder_point(self):
        self._test_encoder_dict({"type": "Point", "coordinates": [1, 2]})

    def test_encoder_multipoint(self):
        self._test_encoder_dict({"type": "MultiPoint", "coordinates": [[1, 2], [3, 4]]})

    def test_encoder_linestring(self):
        self._test_encoder_dict({"type": "LineString", "coordinates": [[30, 10], [10, 30], [40, 40]]})

    def test_encoder_multilinestring(self):
        self._test_encoder_dict(
            {
                "type": "MultiLineString",
                "coordinates": [[[10, 10], [20, 20], [10, 40]], [[40, 40], [30, 30], [40, 20], [30, 10]]],
            }
        )

    def test_encoder_polygon(self):
        self._test_encoder_dict(
            {"type": "Polygon", "coordinates": [[[30, 10], [10, 20], [20, 40], [40, 40], [30, 10]]]}
        )

    def test_encoder_polygon_w_hole(self):
        self._test_encoder_dict(
            {
                "type": "Polygon",
                "coordinates": [
                    [[35, 10], [10, 20], [15, 40], [45, 45], [35, 10]],
                    [[20, 30], [30, 20], [35, 35], [20, 30]],
                ],
            }
        )

    def test_encoder_multipolygon(self):
        self._test_encoder_dict(
            {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[30, 20], [10, 40], [45, 40], [30, 20]]],
                    [[[15, 5], [5, 10], [10, 20], [40, 10], [15, 5]]],
                ],
            }
        )

    def test_encoder_multipolygon_w_hole(self):
        self._test_encoder_dict(
            {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[40, 40], [45, 30], [20, 45], [40, 40]]],
                    [
                        [[20, 35], [45, 20], [30, 5], [10, 10], [10, 30], [20, 35]],
                        [[30, 20], [20, 25], [20, 15], [30, 20]],
                    ],
                ],
            }
        )


class QuantizeTest(unittest.TestCase):
    def test_quantize(self):
        from mapbox_vector_tile import decode, encode

        props = {"foo": "bar"}
        shape = "POINT(15 15)"
        feature = {"geometry": shape, "properties": props}
        features = [feature]
        source = {"name": "layername", "features": features}
        bounds = 10.0, 10.0, 20.0, 20.0
        pbf = encode(source, default_options={"quantize_bounds": bounds})
        result = decode(pbf)
        act_feature = result["layername"]["features"][0]
        act_geom = act_feature["geometry"]
        exp_geom = {"type": "Point", "coordinates": [2048, 2048]}
        self.assertEqual(exp_geom, act_geom)

    def test_y_coord_down(self):
        from mapbox_vector_tile import decode, encode

        props = {"foo": "bar"}
        shape = "POINT(10 10)"
        feature = {"geometry": shape, "properties": props}
        features = [feature]
        source = {"name": "layername", "features": features}
        pbf = encode(source, default_options={"y_coord_down": True})
        result = decode(pbf, default_options={"y_coord_down": True})
        act_feature = result["layername"]["features"][0]
        act_geom = act_feature["geometry"]
        exp_geom = {"type": "Point", "coordinates": [10, 10]}
        self.assertEqual(exp_geom, act_geom)

    def test_quantize_and_y_coord_down(self):
        from mapbox_vector_tile import decode, encode

        props = {"foo": "bar"}
        shape = "POINT(30 30)"
        feature = {"geometry": shape, "properties": props}
        features = [feature]
        source = {"name": "layername", "features": features}
        bounds = 0.0, 0.0, 50.0, 50.0
        pbf = encode(source, default_options={"quantize_bounds": bounds, "y_coord_down": True})

        result_decode_no_flip = decode(pbf, default_options={"y_coord_down": True})
        act_feature = result_decode_no_flip["layername"]["features"][0]
        act_geom = act_feature["geometry"]
        exp_geom = {"type": "Point", "coordinates": [2458, 2458]}
        self.assertEqual(exp_geom, act_geom)

        result_decode_flip = decode(pbf)
        act_feature = result_decode_flip["layername"]["features"][0]
        act_geom = act_feature["geometry"]
        exp_geom = {"type": "Point", "coordinates": [2458, 1638]}
        self.assertEqual(exp_geom, act_geom)


class ExtentTest(unittest.TestCase):
    def test_custom_extent(self):
        from mapbox_vector_tile import decode, encode

        props = {"foo": "bar"}
        shape = "POINT(10 10)"
        feature = {"geometry": shape, "properties": props}
        features = [feature]
        source = {"name": "layername", "features": features}
        bounds = 0.0, 0.0, 10.0, 10.0
        pbf = encode(source, default_options={"quantize_bounds": bounds, "extents": 50})
        result = decode(pbf)
        act_feature = result["layername"]["features"][0]
        act_geom = act_feature["geometry"]
        exp_geom = {"type": "Point", "coordinates": [50, 50]}
        self.assertEqual(exp_geom, act_geom)


class InvalidGeometryTest(unittest.TestCase):
    def test_invalid_geometry_ignore(self):
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_ignore

        geometry = "POLYGON ((10 10, 20 10, 20 20, 15 15, 15 5, 10 10))"
        shape = shapely.wkt.loads(geometry)
        self.assertFalse(shape.is_valid)
        feature = {"geometry": shape, "properties": {}}
        source = {"name": "layername", "features": [feature]}
        pbf = encode(source, default_options={"on_invalid_geometry": on_invalid_geometry_ignore})
        result = decode(pbf)
        self.assertEqual(0, len(result["layername"]["features"]))

    def test_invalid_geometry_raise(self):
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_raise

        geometry = "POLYGON ((10 10, 20 10, 20 20, 15 15, 15 5, 10 10))"
        shape = shapely.wkt.loads(geometry)
        self.assertFalse(shape.is_valid)
        feature = {"geometry": shape, "properties": {}}
        source = {"name": "layername", "features": [feature]}
        with self.assertRaises(ValueError):
            encode(source, default_options={"on_invalid_geometry": on_invalid_geometry_raise})

    def test_invalid_geometry_make_valid(self):
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        geometry = "POLYGON ((10 10, 20 10, 20 20, 15 15, 15 5, 10 10))"
        shape = shapely.wkt.loads(geometry)
        self.assertFalse(shape.is_valid)
        feature = {"geometry": shape, "properties": {}}
        source = {"name": "layername", "features": [feature]}
        pbf = encode(source, default_options={"on_invalid_geometry": on_invalid_geometry_make_valid})
        result = decode(pbf)
        self.assertEqual(1, len(result["layername"]["features"]))
        valid_geometry = result["layername"]["features"][0]["geometry"]
        self.assertTrue(valid_geometry["type"], "MultiPolygon")
        multipolygon = shapely.geometry.shape(valid_geometry)
        self.assertTrue(multipolygon.geom_type, "MultiPolygon")
        self.assertTrue(multipolygon.is_valid)

        for poly in multipolygon.geoms:
            self.assertTrue(poly.is_valid)

    def test_bowtie_self_touching(self):
        import shapely.geometry
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        bowtie = "POLYGON ((0 0, 0 2, 1 1, 2 2, 2 0, 1 1, 0 0))"
        shape = shapely.wkt.loads(bowtie)
        self.assertFalse(shape.is_valid)
        feature = {"geometry": shape, "properties": {}}
        source = {"name": "layername", "features": [feature]}
        pbf = encode(source, default_options={"on_invalid_geometry": on_invalid_geometry_make_valid})
        result = decode(pbf)
        self.assertEqual(1, len(result["layername"]["features"]))
        valid_geometries = result["layername"]["features"][0]["geometry"]
        multipolygon = shapely.geometry.shape(valid_geometries)
        self.assertEqual(2, len(multipolygon.geoms))
        shape1, shape2 = multipolygon.geoms
        self.assertTrue(shape1.is_valid)
        self.assertTrue(shape2.is_valid)
        self.assertGreater(shape1.area, 0)
        self.assertGreater(shape2.area, 0)

    def test_bowtie_self_crossing(self):
        import shapely.geometry
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        bowtie = "POLYGON ((0 0, 2 2, 2 0, 0 2, 0 0))"
        shape = shapely.wkt.loads(bowtie)
        self.assertFalse(shape.is_valid)
        feature = {"geometry": shape, "properties": {}}
        source = {"name": "layername", "features": [feature]}
        pbf = encode(source, default_options={"on_invalid_geometry": on_invalid_geometry_make_valid})
        result = decode(pbf)
        self.assertEqual(1, len(result["layername"]["features"]))
        valid_geometries = result["layername"]["features"][0]["geometry"]
        multipolygon = shapely.geometry.shape(valid_geometries)
        self.assertEqual(multipolygon.geom_type, "MultiPolygon")
        self.assertTrue(multipolygon.is_valid)

        total_area = 0
        for p in multipolygon.geoms:
            self.assertEqual(p.geom_type, "Polygon")
            self.assertTrue(p.is_valid)
            self.assertGreater(p.area, 0)
            total_area += p.area
        self.assertEqual(2, total_area)

    def test_make_valid_self_crossing(self):
        import shapely.geometry
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        geometry = "POLYGON ((10 10, 20 10, 20 20, 15 15, 15 5, 10 10))"
        shape = shapely.wkt.loads(geometry)
        self.assertFalse(shape.is_valid)
        feature = {"geometry": shape, "properties": {}}
        source = {"name": "layername", "features": [feature]}
        pbf = encode(source, default_options={"on_invalid_geometry": on_invalid_geometry_make_valid})
        result = decode(pbf)
        self.assertEqual(1, len(result["layername"]["features"]))
        valid_geometries = result["layername"]["features"][0]["geometry"]
        self.assertEqual(valid_geometries["type"], "MultiPolygon")
        multipolygon = shapely.geometry.shape(valid_geometries)
        self.assertTrue(multipolygon.is_valid)

        total_area = 0
        for p in multipolygon.geoms:
            self.assertTrue(p.is_valid)
            self.assertGreater(p.area, 0)
            total_area += p.area

        self.assertEqual(50, total_area)
        self.assertEqual(50, multipolygon.area)

    def test_validate_generates_rounding_error(self):
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        bowtie = "POLYGON((0 0, 1 1, 0 1, 1 0, 0 0))"
        shape = shapely.wkt.loads(bowtie)
        self.assertFalse(shape.is_valid)
        feature = {"geometry": shape, "properties": {}}
        source = {"name": "layername", "features": [feature]}
        pbf = encode(source, default_options={"on_invalid_geometry": on_invalid_geometry_make_valid})
        result = decode(pbf)
        features = result["layername"]["features"]
        self.assertEqual(1, len(features))
        self.assertEqual(features[0]["geometry"]["type"], "Polygon")
        shape = shapely.geometry.shape(features[0]["geometry"])
        self.assertEqual(shape.geom_type, "Polygon")
        self.assertTrue(shape.is_valid)
        self.assertGreater(shape.area, 0)

    def test_geometry_collection_raises(self):
        import shapely.wkt

        from mapbox_vector_tile import encode

        collection = shapely.wkt.loads(
            """GEOMETRYCOLLECTION (GEOMETRYCOLLECTION (POINT (4095 3664), LINESTRING (2889 0, 2889 0)),
POINT (4095 3664), LINESTRING (2889 0, 2912 158, 3757 1700, 3732 1999, 4095 3277))"""
        )
        with self.assertRaises(ValueError):
            encode({"name": "streets", "features": [{"geometry": collection}]})

    def test_quantize_makes_mutlipolygon_invalid(self):
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        shape = shapely.wkt.loads(
            """MULTIPOLYGON (((656510.8206577231 5674684.979891453, 656511.16 5674685.9, """
            """656514.1758819892 5674684.979891453, 656510.8206577231 5674684.979891453)), """
            """((657115.9120547654 5674684.979891453, 657118.85 5674690, 657118.0689111941 5674684.979891453, """
            """657115.9120547654 5674684.979891453)))"""
        )
        quantize_bounds = (645740.0149532147, 5674684.979891453, 665307.8941942193, 5694252.8591324575)
        features = [{"geometry": shape, "properties": {}}]
        pbf = encode(
            {"name": "foo", "features": features},
            default_options={
                "quantize_bounds": quantize_bounds,
                "on_invalid_geometry": on_invalid_geometry_make_valid,
            },
        )
        result = decode(pbf)
        features = result["foo"]["features"]
        self.assertEqual(1, len(features))

    def test_flipped_geometry_produces_multipolygon(self):
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        shape = shapely.wkt.loads(
            """POLYGON ((3449 1939, 3476 1967, 3473 1996, 3483 2027, 3542 2119, 3538 2160, 3563 2233, 3602 2255, """
            """3639 2326, 3629 2388, 3573 2455, 3594 2493, 3558 2533, 3573 2549, 3518 2572, 3502 2592, 3505 2607, """
            """3513 2614, 3535 2616, 3537 2610, 3535 2602, 3537 2599, 3548 2607, 3551 2636, 3528 2634, 3537 2668, """
            """3549 2670, 3528 2711, 3550 2667, 3532 2635, 3550 2641, 3553 2613, 3549 2602, 3540 2596, 3512 2610, """
            """3506 2589, 3576 2552, 3576 2543, 3563 2535, 3596 2506, 3597 2494, 3587 2469, 3589 2451, 3636 2385, """
            """3644 2326, 3605 2251, 3566 2230, 3547 2122, 3482 2014, 3479 1966, 3455 1944, 3458 1910, 3449 1902, """
            """3449 1939))"""
        )
        features = [{"geometry": shape, "properties": {}}]
        pbf = encode(
            {"name": "foo", "features": features},
            default_options={"on_invalid_geometry": on_invalid_geometry_make_valid},
        )
        result = decode(pbf)
        features = result["foo"]["features"]
        self.assertEqual(1, len(features))
        geom = shapely.geometry.shape(features[0]["geometry"])
        self.assertEqual(features[0]["geometry"]["type"], "MultiPolygon")
        self.assertEqual(geom.geom_type, "MultiPolygon")
        self.assertTrue(geom.is_valid)
        for poly in geom.geoms:
            self.assertTrue(poly.is_valid)

    def test_make_valid_can_return_multipolygon(self):
        from pathlib import Path

        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        test_dir = Path(__file__).resolve().parent
        file_name = "error_nested_multipolygon.wkt"

        with (test_dir / file_name).open() as fh:
            shape = wkt.loads(fh.read())

        features = [{"geometry": shape, "properties": {}}]
        pbf = encode(
            {"name": "foo", "features": features},
            default_options={
                "quantize_bounds": (-10018754.1713946, 11271098.44281893, -8766409.899970269, 12523442.714243261),
                "on_invalid_geometry": on_invalid_geometry_make_valid,
            },
        )
        result = decode(pbf)
        features = result["foo"]["features"]
        self.assertEqual(1, len(features))
        geom = features[0]["geometry"]
        self.assertEqual(geom["type"], "MultiPolygon")
        multipolygon = shapely.geometry.shape(geom)
        self.assertTrue(multipolygon.is_valid)

        area = 0
        for p in multipolygon.geoms:
            self.assertTrue(p.is_valid)
            area += p.area
        self.assertEqual(4339852.5, area)

    def test_too_small_geometry(self):
        import shapely.wkt

        from mapbox_vector_tile import encode
        from mapbox_vector_tile.encoder import on_invalid_geometry_make_valid

        shape = shapely.wkt.loads(
            "LINESTRING (3065.656210384849 3629.831662879646, 3066.458953567231 3629.725941289478)"
        )
        features = [{"geometry": shape, "properties": {}}]
        pbf = encode(
            {"name": "foo", "features": features},
            default_options={"on_invalid_geometry": on_invalid_geometry_make_valid},
        )
        result = decode(pbf)
        features = result["foo"]["features"]
        self.assertEqual(0, len(features))


class LowLevelEncodingTestCase(unittest.TestCase):
    def test_example_multi_polygon(self):
        from mapbox_vector_tile.encoder import VectorTile

        # example from spec:
        # https://github.com/mapbox/vector-tile-spec/tree/master/2.1#4356-example-multi-polygon  ()
        # note that examples are in **tile local coordinates** which are
        # y-down.
        input_geometry = (
            "MULTIPOLYGON ("
            + "((0 0, 10 0, 10 10, 0 10, 0 0)),"
            + "((11 11, 20 11, 20 20, 11 20, 11 11),"
            + " (13 13, 13 17, 17 17, 17 13, 13 13)))"
        )
        expected_commands = [
            9,  # 1 x move to
            0,
            0,  # ........... +0,+0
            26,  # 3 x line to
            20,
            0,  # ........... +10,+0
            0,
            20,  # ........... +0,+10
            19,
            0,  # ........... -10,+0
            15,  # 1 x close path
            9,  # 1 x move to
            22,
            2,  # ........... +11,+1
            26,  # 3 x line to
            18,
            0,  # ........... +9,+0
            0,
            18,  # ........... +0,+9
            17,
            0,  # ........... -9,+0
            15,  # 1 x close path
            9,  # 1 x move to
            4,
            13,  # ........... +2,-7
            26,  # 3 x line to
            0,
            8,  # ........... +0,+4
            8,
            0,  # ........... +4,+0
            0,
            7,  # ........... +0,-4
            15,  # 1 x close path
        ]

        tile = VectorTile(default_options={"extents": 4096, "quantize_bounds": None, "y_coord_down": True})
        tile.add_layer(name="example_layer", features=[{"geometry": input_geometry}])
        self.assertEqual(1, len(tile.layer.features))
        f = tile.layer.features[0]
        self.assertEqual(expected_commands, list(f.geometry))

    def test_example_multi_polygon_y_up(self):
        from mapbox_vector_tile.encoder import VectorTile

        # example from spec:
        # https://github.com/mapbox/vector-tile-spec/tree/master/2.1#4356-example-multi-polygon
        # in this example, we transform the coordinates to their equivalents
        # in a y-up coordinate system.
        input_geometry = (
            "MULTIPOLYGON ("
            + "((0 20, 10 20, 10 10, 0 10, 0 20)),"
            + "((11 9, 20 9, 20 0, 11 0, 11 9),"
            + " (13 7, 13 3, 17 3, 17 7, 13 7)))"
        )
        expected_commands = [
            9,  # 1 x move to
            0,
            0,  # ........... +0,+0
            26,  # 3 x line to
            20,
            0,  # ........... +10,+0
            0,
            20,  # ........... +0,+10
            19,
            0,  # ........... -10,+0
            15,  # 1 x close path
            9,  # 1 x move to
            22,
            2,  # ........... +11,+1
            26,  # 3 x line to
            18,
            0,  # ........... +9,+0
            0,
            18,  # ........... +0,+9
            17,
            0,  # ........... -9,+0
            15,  # 1 x close path
            9,  # 1 x move to
            4,
            13,  # ........... +2,-7
            26,  # 3 x line to
            0,
            8,  # ........... +0,+4
            8,
            0,  # ........... +4,+0
            0,
            7,  # ........... +0,-4
            15,  # 1 x close path
        ]

        tile = VectorTile(default_options={"extents": 20, "quantize_bounds": None, "y_coord_down": False})
        tile.add_layer(name="example_layer", features=[{"geometry": input_geometry}])
        self.assertEqual(1, len(tile.layer.features))
        f = tile.layer.features[0]
        self.assertEqual(expected_commands, list(f.geometry))

    def test_issue_57(self):
        from mapbox_vector_tile.encoder import VectorTile

        # example from issue:
        # https://github.com/tilezen/mapbox-vector-tile/issues/57
        input_geometry = "POLYGON ((2 2, 5 4, 2 6, 2 2))"
        expected_commands = [
            9,  # 1 x move to
            4,
            4,  # ........... +2,+2
            18,  # 2 x line to
            6,
            4,  # ........... +3,+2
            5,
            4,  # ........... -3,+2
            15,  # 1 x close path
        ]

        tile = VectorTile(default_options={"extents": 4096, "quantize_bounds": None, "y_coord_down": True})
        tile.add_layer(name="example_layer", features=[{"geometry": input_geometry}])
        self.assertEqual(1, len(tile.layer.features))
        f = tile.layer.features[0]
        self.assertEqual(expected_commands, list(f.geometry))


class InvalidVectorTileTest(unittest.TestCase):
    def test_duplicate_layer_name(self):
        from mapbox_vector_tile import encode

        props = {"foo": "bar"}
        shape = "POINT(10 10)"
        feature = {"geometry": shape, "properties": props}
        features = [feature]
        source = [{"name": "layername", "features": features}, {"name": "layername", "features": features}]
        with self.assertRaises(ValueError) as ex:
            encode(source, default_options={"extents": 4096})
        self.assertEqual(str(ex.exception), "The layer name 'layername' already exists in the vector tile.")

    def test_empty_layer_name(self):
        from mapbox_vector_tile import encode

        props = {"foo": "bar"}
        shape = "POINT(10 10)"
        feature = {"geometry": shape, "properties": props}
        features = [feature]
        source = [{"name": "", "features": features}]
        with self.assertRaises(ValueError) as ex:
            encode(source, default_options={"extents": 4096})
        self.assertEqual(str(ex.exception), "A layer name can not be empty. '' was provided.")

        source = [{"name": None, "features": features}]
        with self.assertRaises(ValueError) as ex:
            encode(source, default_options={"extents": 4096})
        self.assertEqual(str(ex.exception), "A layer name can not be empty. None was provided.")

    def test_empty_layer(self):
        from mapbox_vector_tile import encode

        # No content
        res = encode([], default_options={"extents": 4096})
        self.assertEqual(res, b"")

        # Layer without feature
        res = encode({"name": "layer", "features": []}, default_options={"extents": 4096})
        self.assertEqual(res, b"\x1a\x0c\n\x05layer(\x80 x\x02")

    def test_invalid_extent(self):
        from mapbox_vector_tile import encode

        props = {"foo": "bar"}
        shape = "POINT(10 10)"
        feature = {"geometry": shape, "properties": props}
        features = [feature]
        source = [{"name": "layername", "features": features}]
        with self.assertRaises(ValueError) as ex:
            encode(source, default_options={"extents": 0})
        self.assertEqual(str(ex.exception), "The extents must be positive. 0 provided.")

        with self.assertRaises(ValueError) as ex:
            encode(source, default_options={"extents": -2.3})
        self.assertEqual(str(ex.exception), "The extents must be positive. -2.3 provided.")


class TransformerTestCase(unittest.TestCase):
    def test_transformer(self):
        from shapely.geometry import shape
        from shapely.wkt import loads

        def forward_transformer(x, y):
            return x + 5, y + 2

        def backward_transformer(x, y):
            return x - 5, y - 2

        source = {
            "name": "water",
            "features": [
                {
                    "geometry": """LINESTRING (76097 5861479, 418535 6066942, 526159 5685368, 878380 5910399,"""
                    """1514336 5900615, 1426281 5558177)""",
                    "properties": {"uid": 123, "foo": "bar", "cat": "flew"},
                }
            ],
        }

        encoded = encode(source, default_options={"transformer": forward_transformer, "extents": 4096})
        decoded = decode(encoded, default_options={"transformer": backward_transformer})

        # Source data
        layer_name = source["name"]
        nb_features = len(source["features"])

        # Output data
        layer = decoded[layer_name]
        features = layer["features"]
        self.assertEqual(nb_features, len(features))
        for source_feature, destination_feature in zip(source["features"], features):
            self.assertEqual(source_feature["properties"], destination_feature["properties"])
            source_geometry = loads(source_feature["geometry"])
            destination_geometry = shape(destination_feature["geometry"])
            self.assertTrue(source_geometry.equals_exact(destination_geometry, tolerance=1e-6))


class OptionsTestCase(unittest.TestCase):
    def test_options(self):
        layer_options_1 = {"y_coord_down": True, "transformer": "my_function"}
        layer_options_2 = {"quantize_bounds": (10.0, 10.0, 20.0, 20.0)}
        default_options = {"extents": 42}
        self.assertEqual(
            get_encode_options(layer_options=layer_options_1, default_options=default_options),
            {
                **layer_options_1,
                "check_winding_order": True,
                "extents": 42,
                "max_geometry_validate_tries": 5,
                "on_invalid_geometry": None,
                "quantize_bounds": None,
            },
        )
        self.assertEqual(
            get_encode_options(layer_options=layer_options_2, default_options=default_options),
            {
                **layer_options_2,
                "check_winding_order": True,
                "extents": 42,
                "max_geometry_validate_tries": 5,
                "on_invalid_geometry": None,
                "transformer": None,
                "y_coord_down": False,
            },
        )
        self.assertEqual(
            get_encode_options(layer_options=layer_options_2, default_options=None),
            {
                **layer_options_2,
                "check_winding_order": True,
                "extents": 4096,
                "max_geometry_validate_tries": 5,
                "on_invalid_geometry": None,
                "transformer": None,
                "y_coord_down": False,
            },
        )
        self.assertEqual(
            get_encode_options(layer_options=None, default_options=layer_options_1),
            {
                **layer_options_1,
                "check_winding_order": True,
                "extents": 4096,
                "max_geometry_validate_tries": 5,
                "on_invalid_geometry": None,
                "quantize_bounds": None,
            },
        )
        self.assertEqual(get_encode_options(layer_options=None, default_options=None), DEFAULT_ENCODE_OPTIONS)

    def test_options_error(self):
        expected_result = "The following options are not allowed for encoding a tile: 'opt', 'unknown'."
        with self.assertRaises(ValueError) as ex:
            get_encode_options(layer_options={"y_coord_down": False, "unknown": 23}, default_options={"opt": 42})
        self.assertEqual(str(ex.exception), expected_result)

        expected_result = "The max_geometry_validate_tries must be positive. -3 provided."
        with self.assertRaises(ValueError) as ex:
            get_encode_options(
                layer_options={"y_coord_down": False, "max_geometry_validate_tries": -3},
                default_options={"extents": 42},
            )
        self.assertEqual(str(ex.exception), expected_result)

        expected_result = "The extents must be positive. 0 provided."
        with self.assertRaises(ValueError) as ex:
            get_encode_options(
                layer_options={"y_coord_down": False, "max_geometry_validate_tries": 25}, default_options={"extents": 0}
            )
        self.assertEqual(str(ex.exception), expected_result)

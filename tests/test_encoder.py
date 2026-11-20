"""
Tests for vector_tile/encoder.py
"""

import unittest

from shapely import wkt

import mapbox_vector_tile
from mapbox_vector_tile import decode, encode
from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vector_tile
from mapbox_vector_tile.utils import DEFAULT_ENCODE_OPTIONS, get_encode_options, zig_zag_decode


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


class QuantizationOrderTest(unittest.TestCase):
    @staticmethod
    def _features():
        return [
            # x = y = 3 lands exactly on the half-grid in both layers: 1.5 with the odd
            # layer's bounds and 0.5 with the even layer's bounds.
            {"id": 1, "geometry": "POINT (3 3)", "properties": {}},
            # Just below and just above the half-grid: 1.49 / 1.51 after the odd layer's
            # scaling and 0.49 / 0.51 after the even layer's scaling.
            {"id": 2, "geometry": "POINT (2.98 3.02)", "properties": {}},
            # Just above and just below the half-grid with the same per-layer scaling.
            {"id": 3, "geometry": "POINT (3.02 2.98)", "properties": {}},
            # The middle two vertices land in the same quantized cell in both layers.
            {"id": 4, "geometry": "LINESTRING (2.2 2.2, 4.4 3.2, 4.8 3.4, 7 7)", "properties": {}},
        ]

    @staticmethod
    def _encode():
        return encode(
            [
                {"name": "odd", "features": QuantizationOrderTest._features()},
                {"name": "even", "features": QuantizationOrderTest._features()},
            ],
            per_layer_options={
                # extent 7 is odd: 1.5 rounds to 2, and 7 - 2 = 5 differs from round(7 - 1.5) = 6.
                "odd": {
                    "extents": 7,
                    "quantize_bounds": (0.0, 0.0, 14.0, 14.0),
                    "y_coord_down": False,
                },
                # extent 8 is even: the same coordinates map to different grid positions.
                "even": {
                    "extents": 8,
                    "quantize_bounds": (2.0, 2.0, 18.0, 18.0),
                    "y_coord_down": True,
                },
            },
            # Each layer's own Y option and extent must override this default.
            default_options={"y_coord_down": True, "extents": 4096},
        )

    @staticmethod
    def _absolute_coords(geometry):
        coords = []
        x = y = 0
        i = 0
        while i < len(geometry):
            command = geometry[i]
            i += 1
            cmd = command & 0x7
            count = command >> 3
            if cmd == 7:  # SEG_END
                continue
            for _ in range(count):
                x += zig_zag_decode(geometry[i])
                y += zig_zag_decode(geometry[i + 1])
                i += 2
                coords.append((x, y))
        return coords

    def test_decoded_geometry_matches_quantized_grid(self):
        decoded = decode(self._encode())

        def geometry_of(layer_name, feature_id):
            return next(f["geometry"] for f in decoded[layer_name]["features"] if f["id"] == feature_id)

        # Odd layer, bounds (0, 0, 14, 14) and extent 7: round first, then reflect y as 7 - y.
        # The default decode flips it back, exposing the integer grid positions.
        self.assertEqual({"type": "Point", "coordinates": [2, 2]}, geometry_of("odd", 1))
        self.assertEqual({"type": "Point", "coordinates": [1, 2]}, geometry_of("odd", 2))
        self.assertEqual({"type": "Point", "coordinates": [2, 1]}, geometry_of("odd", 3))
        # The duplicate vertex is dropped by its quantized position, not its source position.
        self.assertEqual(
            {"type": "LineString", "coordinates": [[1, 1], [2, 2], [4, 4]]},
            geometry_of("odd", 4),
        )

        # Even layer, bounds (2, 2, 18, 18) and extent 8. The layer asks for y_coord_down,
        # so the encoder does not reflect y; decoding with the default flips it once.
        # The half-grid y = 0.5 rounds to 0 (banker's rounding), hence the decoded y of 8.
        self.assertEqual({"type": "Point", "coordinates": [0, 8]}, geometry_of("even", 1))
        self.assertEqual({"type": "Point", "coordinates": [0, 7]}, geometry_of("even", 2))
        self.assertEqual({"type": "Point", "coordinates": [1, 8]}, geometry_of("even", 3))
        self.assertEqual(
            {"type": "LineString", "coordinates": [[0, 8], [1, 7], [2, 6]]},
            geometry_of("even", 4),
        )

    def test_protobuf_commands_show_round_then_y_flip(self):
        pbf = self._encode()
        tile = vector_tile.tile()
        tile.ParseFromString(pbf)

        layers = {layer.name: layer for layer in tile.layers}
        self.assertEqual({"odd", "even"}, set(layers))

        # Odd layer: extent 7, factor 1/2, y reflected after rounding.
        # Quantized (x, y): (2, 2), (1, 2), (2, 1), then (1, 1), (2, 2), (4, 4).
        # Stored y values are 7 - y: 5, 5, 6, then 6, 5, 3.
        odd = layers["odd"]
        self.assertEqual(7, odd.extent)
        odd_commands = {feature.id: list(feature.geometry) for feature in odd.features}
        self.assertEqual([9, 4, 10], odd_commands[1])
        self.assertEqual([9, 2, 10], odd_commands[2])
        self.assertEqual([9, 4, 12], odd_commands[3])
        self.assertEqual([9, 2, 12, 18, 2, 1, 4, 3], odd_commands[4])
        # Reflecting before rounding would turn y = 1.5 into 7 - 1.5 = 5.5 and store 6
        # for the half-grid point, while rounding first stores 7 - 2 = 5.
        self.assertNotEqual([9, 4, 12], odd_commands[1])

        # Even layer: extent 8, factor 1/2, no reflection during encoding.
        # Quantized (x, y): (0, 0), (0, 1), (1, 0), then (0, 0), (1, 1), (2, 2).
        even = layers["even"]
        self.assertEqual(8, even.extent)
        even_commands = {feature.id: list(feature.geometry) for feature in even.features}
        self.assertEqual([9, 0, 0], even_commands[1])
        self.assertEqual([9, 0, 2], even_commands[2])
        self.assertEqual([9, 2, 0], even_commands[3])
        self.assertEqual([9, 0, 0, 18, 2, 2, 2, 2], even_commands[4])

        # Decode the commands independently of the decoder to inspect the stored integers.
        self.assertEqual([(2, 5)], self._absolute_coords(odd_commands[1]))
        self.assertEqual([(1, 6), (2, 5), (4, 3)], self._absolute_coords(odd_commands[4]))
        self.assertEqual([(0, 0)], self._absolute_coords(even_commands[1]))
        self.assertEqual([(0, 0), (1, 1), (2, 2)], self._absolute_coords(even_commands[4]))

    def test_read_back_layers_with_explicit_options(self):
        pbf = self._encode()
        decoded = decode(
            pbf,
            per_layer_options={
                "odd": {"y_coord_down": False},
                "even": {"y_coord_down": True},
            },
        )

        self.assertEqual(7, decoded["odd"]["extent"])
        self.assertEqual(8, decoded["even"]["extent"])

        def geometry_of(layer_name, feature_id):
            return next(f["geometry"] for f in decoded[layer_name]["features"] if f["id"] == feature_id)

        # Explicit per-layer decode options reproduce each layer's encoding orientation.
        self.assertEqual({"type": "Point", "coordinates": [2, 2]}, geometry_of("odd", 1))
        self.assertEqual({"type": "Point", "coordinates": [0, 0]}, geometry_of("even", 1))
        self.assertEqual(
            {"type": "LineString", "coordinates": [[1, 1], [2, 2], [4, 4]]},
            geometry_of("odd", 4),
        )
        self.assertEqual(
            {"type": "LineString", "coordinates": [[0, 0], [1, 1], [2, 2]]},
            geometry_of("even", 4),
        )

        # Decoding both layers with an explicit default (True) instead exposes what the
        # encoder stored: the odd layer's reflected y and the even layer's unreflected y.
        decoded_default = decode(pbf, default_options={"y_coord_down": True})
        self.assertEqual(
            {"type": "Point", "coordinates": [2, 5]},
            next(f["geometry"] for f in decoded_default["odd"]["features"] if f["id"] == 1),
        )
        self.assertEqual(
            {"type": "Point", "coordinates": [0, 0]},
            next(f["geometry"] for f in decoded_default["even"]["features"] if f["id"] == 1),
        )


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

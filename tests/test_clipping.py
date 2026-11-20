"""
Tests for optional geometry clipping at shared tile edges.
"""

import unittest

from mapbox_vector_tile import decode, encode
from mapbox_vector_tile.clipping import ClipError, normalize_bounds

LEFT = (0.0, 0.0, 10.0, 10.0)
RIGHT = (10.0, 0.0, 20.0, 10.0)
SHARED = {"axis": "x", "position": 10.0, "ticks": 1.0}

CROSSING = "LINESTRING (8 2, 10 2, 10 8, 12 8)"
TOUCHES_AND_RETURNS = "LINESTRING (8 3, 10 5, 8 7)"
STRAIGHT_CROSSING = "LINESTRING (8 6, 12 6)"


def encode_tile(bounds, extent, owner, geometries, ticks=1.0, layer="roads", extra_options=None):
    shared = {"axis": "x", "position": 10.0, "owner": owner, "ticks": ticks}
    options = {
        "clip_bounds": bounds,
        "quantize_bounds": bounds,
        "extents": extent,
        "shared_edge": shared,
    }
    options.update(extra_options or {})
    source = {"name": layer, "features": [{"geometry": geometry} for geometry in geometries]}
    return encode(source, default_options=options)


def to_physical(geometry, bounds, extent):
    minx, miny, maxx, maxy = bounds
    scale_x = (maxx - minx) / extent
    scale_y = (maxy - miny) / extent

    def convert(point):
        return (minx + point[0] * scale_x, miny + point[1] * scale_y)

    kind = geometry["type"]
    if kind == "Point":
        return convert(geometry["coordinates"])
    if kind == "MultiPoint":
        return [convert(point) for point in geometry["coordinates"]]
    if kind == "LineString":
        return [convert(point) for point in geometry["coordinates"]]
    if kind == "MultiLineString":
        return [[convert(point) for point in part] for part in geometry["coordinates"]]
    raise AssertionError(f"Unexpected geometry type {kind}")


def decode_physical(tile, bounds, extent, layer="roads"):
    decoded = decode(tile)[layer]
    assert decoded["extent"] == extent
    return [to_physical(feature["geometry"], bounds, extent) for feature in decoded["features"]]


class SharedEdgeOwnershipTest(unittest.TestCase):
    def test_crossing_line_meets_at_the_agreed_junction(self):
        left = decode_physical(encode_tile(LEFT, 100, "low", [CROSSING]), LEFT, 100)
        right = decode_physical(encode_tile(RIGHT, 200, "low", [CROSSING]), RIGHT, 200)

        self.assertEqual([[(8.0, 2.0), (10.0, 2.0), (10.0, 8.0)]], left)
        self.assertEqual([[(10.0, 8.0), (12.0, 8.0)]], right)
        self.assertEqual(left[0][-1], right[0][0])

    def test_line_along_the_edge_is_written_by_the_owner_only(self):
        for owner, expected_left, expected_right in (
            (
                "low",
                [[(8.0, 2.0), (10.0, 2.0), (10.0, 8.0)]],
                [[(10.0, 8.0), (12.0, 8.0)]],
            ),
            (
                "high",
                [[(8.0, 2.0), (10.0, 2.0)]],
                [[(10.0, 2.0), (10.0, 8.0), (12.0, 8.0)]],
            ),
        ):
            with self.subTest(owner=owner):
                left = decode_physical(encode_tile(LEFT, 100, owner, [CROSSING]), LEFT, 100)
                right = decode_physical(encode_tile(RIGHT, 200, owner, [CROSSING]), RIGHT, 200)
                self.assertEqual(expected_left, left)
                self.assertEqual(expected_right, right)

    def test_named_owner_sides_are_accepted(self):
        left = decode_physical(encode_tile(LEFT, 100, "left", [CROSSING]), LEFT, 100)
        right = decode_physical(encode_tile(RIGHT, 200, "left", [CROSSING]), RIGHT, 200)
        self.assertEqual([[(8.0, 2.0), (10.0, 2.0), (10.0, 8.0)]], left)
        self.assertEqual([[(10.0, 8.0), (12.0, 8.0)]], right)

    def test_line_that_only_touches_the_edge_is_not_a_point_on_the_other_side(self):
        for owner in ("low", "high"):
            with self.subTest(owner=owner):
                left = decode_physical(encode_tile(LEFT, 100, owner, [TOUCHES_AND_RETURNS]), LEFT, 100)
                right = decode_physical(encode_tile(RIGHT, 200, owner, [TOUCHES_AND_RETURNS]), RIGHT, 200)
                self.assertEqual([[(8.0, 3.0), (10.0, 5.0), (8.0, 7.0)]], left)
                self.assertEqual([], right)

    def test_touched_point_is_not_duplicated_by_the_point_side(self):
        # The non-owner must not contain a one-vertex line.
        tile = encode_tile(RIGHT, 200, "low", [TOUCHES_AND_RETURNS])
        self.assertEqual([], decode(tile)["roads"]["features"])


class SharedEdgeTickTest(unittest.TestCase):
    def test_physical_ticks_are_not_local_integer_ticks(self):
        # The left tile has extent 100, so physical y=6 is local 60.  The right
        # tile has extent 200, so the same physical junction is local 120.  A
        # local grid spacing of 1 would put the junction at 60 and 120, which
        # do not match in physical space.
        left = decode_physical(encode_tile(LEFT, 100, "low", [STRAIGHT_CROSSING]), LEFT, 100)
        right = decode_physical(encode_tile(RIGHT, 200, "low", [STRAIGHT_CROSSING]), RIGHT, 200)
        self.assertEqual(left[0][-1], (10.0, 6.0))
        self.assertEqual(right[0][0], (10.0, 6.0))

    def test_physical_ticks_snap_both_tiles_to_the_same_junction(self):
        # A road hits the shared edge at y=5.4.  With a physical 1.0 tick grid
        # both tiles choose y=5.0.
        road = "LINESTRING (8 5.4, 12 5.4)"
        left = decode_physical(encode_tile(LEFT, 100, "low", [road]), LEFT, 100)
        right = decode_physical(encode_tile(RIGHT, 200, "low", [road]), RIGHT, 200)
        self.assertEqual(left[0][-1], (10.0, 5.0))
        self.assertEqual(right[0][0], (10.0, 5.0))
        self.assertEqual(left[0][-1], right[0][0])

    def test_local_tick_spacing_is_not_reinterpreted_as_physical(self):
        with self.assertRaises(ClipError):
            encode_tile(LEFT, 100, "low", [CROSSING], ticks={"spacing": 1, "units": "local"})

    def test_local_tick_spacing_is_not_reinterpreted_as_physical_for_both_sides(self):
        # A caller who supplied a 50.0 tick expecting local integers would get
        # a physical grid of 50 units here.  The tick option is explicitly
        # physical, so 50.0 means 50 physical units.
        with self.assertRaises(ClipError):
            encode_tile((0.0, 0.0, 10.0, 10.0), 100, "low", [CROSSING], ticks=0.0)

    def test_explicit_tick_values_are_used(self):
        road = "LINESTRING (8 5.4, 12 5.4)"
        ticks = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
        left = decode_physical(encode_tile(LEFT, 100, "low", [road], ticks=ticks), LEFT, 100)
        right = decode_physical(encode_tile(RIGHT, 200, "low", [road], ticks=ticks), RIGHT, 200)
        self.assertEqual(left[0][-1], (10.0, 6.0))
        self.assertEqual(right[0][0], (10.0, 6.0))

    def test_diagonal_crossing_is_snapped_on_both_sides(self):
        road = "LINESTRING (8 4.8, 12 5.6)"
        left = decode_physical(encode_tile(LEFT, 100, "low", [road]), LEFT, 100)
        right = decode_physical(encode_tile(RIGHT, 200, "low", [road]), RIGHT, 200)
        self.assertEqual(left[0][-1], (10.0, 5.0))
        self.assertEqual(right[0][0], (10.0, 5.0))

    def test_off_grid_exit_point_matches_after_snapping(self):
        road = "LINESTRING (8 2, 10 2, 10 8.4, 12 8.4)"
        left = decode_physical(encode_tile(LEFT, 100, "low", [road]), LEFT, 100)
        right = decode_physical(encode_tile(RIGHT, 200, "low", [road]), RIGHT, 200)
        self.assertEqual(left[0][-1], (10.0, 8.0))
        self.assertEqual(right[0][0], (10.0, 8.0))
        self.assertEqual(left[0][-1], right[0][0])

    def test_touch_point_keeps_its_source_position(self):
        # The touch point is not shared with the other tile, so it must not
        # be snapped to the physical tick grid.
        road = "LINESTRING (8 3, 10 5.4, 8 7)"
        left = decode_physical(encode_tile(LEFT, 100, "low", [road]), LEFT, 100)
        self.assertEqual([[(8.0, 3.0), (10.0, 5.4), (8.0, 7.0)]], left)

    def test_direction_is_preserved(self):
        forward = "LINESTRING (8 2, 10 2, 10 8, 12 8)"
        backward = "LINESTRING (12 8, 10 8, 10 2, 8 2)"
        forward_left = decode_physical(encode_tile(LEFT, 100, "low", [forward]), LEFT, 100)
        backward_left = decode_physical(encode_tile(LEFT, 100, "low", [backward]), LEFT, 100)
        self.assertEqual([[(8.0, 2.0), (10.0, 2.0), (10.0, 8.0)]], forward_left)
        self.assertEqual([[(10.0, 8.0), (10.0, 2.0), (8.0, 2.0)]], backward_left)

    def test_whole_line_along_the_edge_is_written_once(self):
        road = "LINESTRING (10 2, 10 8)"
        for owner, expected_left, expected_right in (
            ("low", [[(10.0, 2.0), (10.0, 8.0)]], []),
            ("high", [], [[(10.0, 2.0), (10.0, 8.0)]]),
        ):
            with self.subTest(owner=owner):
                left = decode_physical(encode_tile(LEFT, 100, owner, [road]), LEFT, 100)
                right = decode_physical(encode_tile(RIGHT, 200, owner, [road]), RIGHT, 200)
                self.assertEqual(expected_left, left)
                self.assertEqual(expected_right, right)

    def test_horizontal_shared_edge(self):
        bottom = (0.0, 0.0, 10.0, 10.0)
        top = (0.0, 10.0, 10.0, 20.0)
        road = "LINESTRING (2 8, 2 10, 8 10, 8 12)"
        shared = {"axis": "y", "position": 10.0, "owner": "low", "ticks": 1.0}

        def encode_horizontal(bounds, extent):
            options = {
                "clip_bounds": bounds,
                "quantize_bounds": bounds,
                "extents": extent,
                "shared_edge": shared,
            }
            return encode(
                {"name": "roads", "features": [{"geometry": road}]},
                default_options=options,
            )

        lower = decode_physical(encode_horizontal(bottom, 100), bottom, 100)
        upper = decode_physical(encode_horizontal(top, 200), top, 200)
        self.assertEqual([[(2.0, 8.0), (2.0, 10.0), (8.0, 10.0)]], lower)
        self.assertEqual([[(8.0, 10.0), (8.0, 12.0)]], upper)
        self.assertEqual(lower[0][-1], upper[0][0])

    def test_shared_edge_must_be_a_tile_boundary(self):
        # An edge declared inside the clip rectangle is not shared with a
        # neighbour.  It must not be treated as an edge, and the line must
        # therefore be clipped normally.
        options = {
            "clip_bounds": LEFT,
            "quantize_bounds": LEFT,
            "extents": 100,
            "shared_edge": {"axis": "x", "position": 5.0, "owner": "low", "ticks": 1.0},
        }
        source = {"name": "roads", "features": [{"geometry": "LINESTRING (2 5, 8 5)"}]}
        decoded = decode(encode(source, default_options=options))["roads"]
        self.assertEqual([[[20, 50], [80, 50]]], [f["geometry"]["coordinates"] for f in decoded["features"]])

    def test_alternative_option_spellings(self):
        nested = {
            "clip": {
                "bounds": LEFT,
                "shared_edge": {"axis": "x", "position": 10.0, "owner": "low", "ticks": 1.0},
            },
            "quantize_bounds": LEFT,
            "extents": 100,
        }
        flat = {
            "clip_bounds": RIGHT,
            "quantize_bounds": RIGHT,
            "extents": 200,
            "edge": "left",
            "edge_owner": "low",
            "edge_ticks": 1.0,
        }
        left = decode_physical(
            encode({"name": "roads", "features": [{"geometry": CROSSING}]}, default_options=nested),
            LEFT,
            100,
        )
        right = decode_physical(
            encode({"name": "roads", "features": [{"geometry": CROSSING}]}, default_options=flat),
            RIGHT,
            200,
        )
        self.assertEqual([[(8.0, 2.0), (10.0, 2.0), (10.0, 8.0)]], left)
        self.assertEqual([[(10.0, 8.0), (12.0, 8.0)]], right)


class ClippingStabilityTest(unittest.TestCase):
    def test_inserting_a_collinear_vertex_does_not_change_the_seam(self):
        road = "LINESTRING (8 2, 9 2, 10 2, 10 5, 10 8, 11 8, 12 8)"
        left = decode_physical(encode_tile(LEFT, 100, "low", [road]), LEFT, 100)
        right = decode_physical(encode_tile(RIGHT, 200, "low", [road]), RIGHT, 200)
        self.assertEqual([[(8.0, 2.0), (10.0, 2.0), (10.0, 8.0)]], left)
        self.assertEqual([[(10.0, 8.0), (12.0, 8.0)]], right)

    def test_inserting_a_vertex_on_a_diagonal_crossing_does_not_change_the_seam(self):
        road = "LINESTRING (8 4.8, 9 5.0, 10 5.2, 12 5.6)"
        left = decode_physical(encode_tile(LEFT, 100, "low", [road]), LEFT, 100)
        right = decode_physical(encode_tile(RIGHT, 200, "low", [road]), RIGHT, 200)
        plain = "LINESTRING (8 4.8, 12 5.6)"
        plain_left = decode_physical(encode_tile(LEFT, 100, "low", [plain]), LEFT, 100)
        plain_right = decode_physical(encode_tile(RIGHT, 200, "low", [plain]), RIGHT, 200)
        self.assertEqual(plain_left, left)
        self.assertEqual(plain_right, right)
        self.assertEqual(left[0][-1], right[0][0])

    def test_order_of_processing_does_not_change_the_result(self):
        features = [CROSSING, TOUCHES_AND_RETURNS, STRAIGHT_CROSSING]
        forward_left = decode_physical(encode_tile(LEFT, 100, "low", features), LEFT, 100)
        forward_right = decode_physical(encode_tile(RIGHT, 200, "low", features), RIGHT, 200)
        reverse_left = decode_physical(encode_tile(LEFT, 100, "low", features[::-1]), LEFT, 100)
        reverse_right = decode_physical(encode_tile(RIGHT, 200, "low", features[::-1]), RIGHT, 200)
        self.assertEqual(forward_left, reverse_left[::-1])
        self.assertEqual(forward_right, reverse_right[::-1])

    def test_annotation_layer_uses_its_own_bounds(self):
        # The roads layer declares a shared edge in its own physical range.  An
        # annotation layer is encoded with a different range and extent and
        # must be quantized with that range, not with the roads range.
        roads_options = {
            "quantize_bounds": LEFT,
            "extents": 100,
            "clip_bounds": LEFT,
            "shared_edge": {"axis": "x", "position": 10.0, "owner": "low", "ticks": 1.0},
        }
        labels_options = {
            "quantize_bounds": (0.0, 0.0, 200.0, 200.0),
            "extents": 200,
        }
        source = [
            {"name": "roads", "features": [{"geometry": CROSSING}]},
            {
                "name": "labels",
                "features": [{"geometry": "POINT (40 40)"}, {"geometry": "POINT (160 160)"}],
            },
        ]
        encoded = encode(
            source,
            per_layer_options={"roads": roads_options, "labels": labels_options},
        )
        decoded = decode(encoded)
        self.assertEqual(100, decoded["roads"]["extent"])
        self.assertEqual(200, decoded["labels"]["extent"])
        points = [
            to_physical(feature["geometry"], (0.0, 0.0, 200.0, 200.0), 200) for feature in decoded["labels"]["features"]
        ]
        self.assertEqual([(40.0, 40.0), (160.0, 160.0)], points)
        roads = [to_physical(feature["geometry"], LEFT, 100) for feature in decoded["roads"]["features"]]
        self.assertEqual([[(8.0, 2.0), (10.0, 2.0), (10.0, 8.0)]], roads)


class ClippingOptionsTest(unittest.TestCase):
    def test_clipping_is_opt_in(self):
        # Without clip options the existing quantization and direction
        # behaviour must be unchanged.
        source = {"name": "roads", "features": [{"geometry": "LINESTRING (8 2, 12 2)"}]}
        plain = decode(encode(source, default_options={"quantize_bounds": LEFT, "extents": 100}))["roads"]
        self.assertEqual(
            [[80, 20], [120, 20]],
            plain["features"][0]["geometry"]["coordinates"],
        )

    def test_clipping_without_bounds_raises(self):
        source = {"name": "roads", "features": [{"geometry": CROSSING}]}
        with self.assertRaises(ClipError):
            encode(source, default_options={"shared_edge": SHARED})

    def test_true_clip_uses_quantize_bounds(self):
        source = {"name": "roads", "features": [{"geometry": CROSSING}]}
        tile = encode(source, default_options={"clip": True, "quantize_bounds": LEFT, "extents": 100})
        decoded = decode(tile)["roads"]
        self.assertEqual(
            [[[80, 20], [100, 20], [100, 80]]],
            [f["geometry"]["coordinates"] for f in decoded["features"]],
        )

    def test_invalid_tick_spacing_raises(self):
        with self.assertRaises(ClipError):
            encode_tile(LEFT, 100, "low", [CROSSING], ticks=-1.0)

    def test_normalize_bounds_accepts_mapping(self):
        self.assertEqual(
            LEFT,
            normalize_bounds({"minx": 0, "miny": 0, "maxx": 10, "maxy": 10}),
        )


if __name__ == "__main__":
    unittest.main()

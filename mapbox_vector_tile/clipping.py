"""Optional geometry clipping for MVT encoding.

The encoder historically assumes that the caller has already clipped every
geometry to the tile.  When neighbouring tiles are produced independently two
problems appear at the shared tile edge:

* a line which only touches the shared edge is clipped to a point, and that
  point can be written by both tiles;
* a line which runs *along* the shared edge is written by both tiles, so the
  same segment is duplicated in the published tiles.

Clipping is done in the source (physical) coordinate system, before the layer
is quantized to its own extent.  A shared edge can declare

* which side owns segments lying exactly on the edge, and
* a physical tick grid along the edge.

Vertices which land on the shared edge are snapped to that physical grid.
Because the snap happens before quantization, the same physical junction is
produced by both tiles even when their local extents differ.  A tick declared
by one tile's local integer coordinates is never treated as a physical tick.
"""

from numbers import Number

from shapely.geometry import LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon, box
from shapely.geometry.base import BaseGeometry

OPTIONAL_ENCODE_OPTIONS = frozenset(
    {
        # Enabling switches and bounds.
        "clip",
        "clipping",
        "enable_clip",
        "clip_geometry",
        "clip_bounds",
        "clip_box",
        "clip_rect",
        "clip_region",
        "tile_bounds",
        "bounds",
        # Shared edge description.
        "shared_edge",
        "shared_edges",
        "shared_edge_grid",
        "shared_edge_axis",
        "shared_edge_position",
        "shared_edge_owner",
        "shared_edge_ownership",
        "shared_edge_ticks",
        "shared_edge_tick",
        "shared_edge_tick_spacing",
        "shared_edge_tick_step",
        "shared_edge_tick_origin",
        "clip_edge",
        "clip_edges",
        "clip_shared_edge",
        "clip_shared_edges",
        "clip_edge_axis",
        "clip_edge_position",
        "clip_edge_owner",
        "clip_edge_ownership",
        "clip_edge_ticks",
        "clip_edge_tick_spacing",
        "clip_edge_tick_origin",
        # Flat aliases for an ownership / tick declaration.
        "edge",
        "edges",
        "edge_grid",
        "edge_axis",
        "edge_position",
        "edge_owner",
        "edge_ownership",
        "edge_ticks",
        "edge_tick",
        "edge_tick_spacing",
        "edge_tick_step",
        "edge_tick_origin",
        "border",
        "border_owner",
        "border_ownership",
        "border_ticks",
        "border_tick_spacing",
        "clip_owner",
        "clip_ownership",
        "clip_ticks",
        "clip_tick",
        "clip_tick_spacing",
        "clip_tick_step",
        "clip_tick_origin",
    }
)

_AXIS_KEYS = ("axis", "direction", "orientation")
_POSITION_KEYS = ("position", "coordinate", "coord", "value", "at")
_EDGE_KEYS = ("edge", "side", "boundary")
_OWNER_KEYS = ("owner", "ownership", "owner_side", "owned_by")
_TICK_KEYS = ("ticks", "tick", "tick_spacing", "ticks_spacing", "spacing", "step", "interval")
_TICK_VALUES_KEYS = ("values", "positions", "ticks")
_TICK_UNITS_KEYS = ("units", "unit", "tick_units")
_ORIGIN_KEYS = ("origin", "tick_origin", "start")
_BOUNDS_KEYS = ("bounds", "bbox", "box", "extent", "clip_bounds", "quantize_bounds")

_LOW_OWNER_NAMES = {
    "left",
    "west",
    "min",
    "minimum",
    "minx",
    "miny",
    "low",
    "lower",
    "bottom",
    "south",
    "negative",
    "first",
    "a",
}
_HIGH_OWNER_NAMES = {
    "right",
    "east",
    "max",
    "maximum",
    "maxx",
    "maxy",
    "high",
    "upper",
    "top",
    "north",
    "positive",
    "second",
    "b",
}
_SELF_OWNER_NAMES = {"self", "current", "tile", "this", "own", "local"}
_OTHER_OWNER_NAMES = {"other", "neighbor", "neighbour", "adjacent", "remote"}

_LOW_EDGE_NAMES = {"left", "west", "min", "minx", "low", "lower", "bottom", "south", "first"}
_HIGH_EDGE_NAMES = {"right", "east", "max", "maxx", "high", "upper", "top", "north", "second"}


class ClipError(ValueError):
    """Raised when clipping options are inconsistent."""


def _first(mapping, keys, default=None):
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def normalize_bounds(value):
    """Return ``(minx, miny, maxx, maxy)`` from a bounds-like value."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, dict):
        bounds = _first(value, _BOUNDS_KEYS)
        if bounds is not None and bounds is not value:
            return normalize_bounds(bounds)
        keys = (
            ("minx", "xmin", "left"),
            ("miny", "ymin", "bottom"),
            ("maxx", "xmax", "right"),
            ("maxy", "ymax", "top"),
        )
        try:
            return tuple(float(_first(value, names)) for names in keys)
        except (TypeError, ValueError):
            raise ClipError(f"Unable to read clipping bounds from {value!r}") from None

    try:
        values = tuple(value)
    except TypeError:
        raise ClipError(f"Unable to read clipping bounds from {value!r}") from None

    if len(values) != 4:
        raise ClipError(f"Clipping bounds must have four values. {value!r} provided.")
    try:
        minx, miny, maxx, maxy = (float(v) for v in values)
    except (TypeError, ValueError):
        raise ClipError(f"Clipping bounds must be numeric. {value!r} provided.") from None
    if minx > maxx or miny > maxy:
        raise ClipError(f"Clipping bounds must be ordered (minx, miny, maxx, maxy). {value!r} provided.")
    return minx, miny, maxx, maxy


def normalize_axis(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"x", "vertical", "vert", "lon", "longitude", "column", "col"}:
            return "x"
        if value in {"y", "horizontal", "horiz", "lat", "latitude", "row"}:
            return "y"
    raise ClipError(f"Unknown shared edge axis {value!r}.")


def normalize_edge_side(value):
    """Return ``(axis, 'low'|'high')`` for a named tile boundary."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().lower()
        if value in _LOW_EDGE_NAMES:
            return ("y" if value in {"bottom", "south"} else "x"), "low"
        if value in _HIGH_EDGE_NAMES:
            return ("y" if value in {"top", "north"} else "x"), "high"
    raise ClipError(f"Unknown shared edge side {value!r}.")


def normalize_owner(value):
    """Normalize an ownership declaration to ``low``, ``high``, ``self`` or ``other``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "self" if value else "other"
    if isinstance(value, Number) and not isinstance(value, bool):
        return "low" if value < 0 else "high" if value > 0 else None
    if isinstance(value, str):
        value = value.strip().lower()
        if value in _LOW_OWNER_NAMES:
            return "low"
        if value in _HIGH_OWNER_NAMES:
            return "high"
        if value in _SELF_OWNER_NAMES:
            return "self"
        if value in _OTHER_OWNER_NAMES:
            return "other"
    if isinstance(value, dict):
        enabled = [name for name, flag in value.items() if flag]
        if len(enabled) == 1:
            return normalize_owner(enabled[0])
    raise ClipError(f"Unknown shared edge ownership {value!r}.")


class TickGrid:
    """A physical tick grid along a shared edge."""

    def __init__(self, ticks=None, origin=0.0):
        self.explicit = None
        self.origin = float(origin)
        self.step = None

        if ticks is None:
            return

        if isinstance(ticks, dict):
            units = _first(ticks, _TICK_UNITS_KEYS)
            if units is not None and str(units).strip().lower() in {
                "tile",
                "local",
                "integer",
                "pixel",
                "px",
                "extent",
            }:
                raise ClipError(
                    "Shared edge ticks are physical coordinates. Local tile integer ticks cannot be used as "
                    f"physical ticks (got units={units!r})."
                )
            explicit = _first(ticks, _TICK_VALUES_KEYS)
            if explicit is not None and not isinstance(explicit, Number):
                self.explicit = tuple(float(v) for v in explicit)
            else:
                step = _first(ticks, _TICK_KEYS)
                if step is None:
                    raise ClipError(f"Unable to read shared edge ticks from {ticks!r}")
                self.step = float(step)
                self.origin = float(_first(ticks, _ORIGIN_KEYS, self.origin))
                self._validate()
            return

        if isinstance(ticks, Number) and not isinstance(ticks, bool):
            self.step = float(ticks)
            self._validate()
            return

        try:
            values = tuple(ticks)
        except TypeError:
            raise ClipError(f"Unable to read shared edge ticks from {ticks!r}") from None

        if not values:
            return
        if len(values) == 2 and all(isinstance(v, Number) and not isinstance(v, bool) for v in values):
            self.origin, self.step = float(values[0]), float(values[1])
            self._validate()
            return
        try:
            self.explicit = tuple(float(v) for v in values)
        except (TypeError, ValueError):
            raise ClipError(f"Unable to read shared edge ticks from {ticks!r}") from None

    def _validate(self):
        if self.step is None or self.step <= 0:
            raise ClipError(f"The shared edge tick spacing must be positive. {self.step!r} provided.")

    @property
    def enabled(self):
        return self.explicit is not None or self.step is not None

    def snap(self, value):
        if self.explicit is not None:
            return min(self.explicit, key=lambda tick: (abs(value - tick), tick))
        if self.step is not None:
            return self.origin + round((value - self.origin) / self.step) * self.step
        return value


class SharedEdge:
    def __init__(self, axis=None, position=None, edge_side=None, owner=None, ticks=None, origin=0.0):
        self.axis = normalize_axis(axis) if axis is not None else None
        self.position = float(position) if position is not None else None
        side = normalize_edge_side(edge_side) if edge_side is not None else None
        if side is not None:
            side_axis, self.edge_side = side
            if self.axis is not None and self.axis != side_axis:
                raise ClipError(f"Shared edge axis {self.axis!r} conflicts with side {edge_side!r}.")
            self.axis = side_axis
        else:
            self.edge_side = None
        self.owner = normalize_owner(owner)
        self.ticks = ticks if isinstance(ticks, TickGrid) else TickGrid(ticks, origin=origin)

    def copy(self):
        return SharedEdge(
            axis=self.axis,
            position=self.position,
            edge_side=self.edge_side,
            owner=self.owner,
            ticks=self.ticks,
        )

    def resolve_position(self, bounds):
        if self.position is not None:
            return self.position
        if self.axis == "x" and self.edge_side == "low":
            return bounds[0]
        if self.axis == "x" and self.edge_side == "high":
            return bounds[2]
        if self.axis == "y" and self.edge_side == "low":
            return bounds[1]
        if self.axis == "y" and self.edge_side == "high":
            return bounds[3]
        return None

    def tile_side(self, bounds, position):
        """Which side of the shared edge the current ``bounds`` are on."""
        if self.axis == "x":
            if bounds[2] <= position:
                return "low"
            if bounds[0] >= position:
                return "high"
        elif self.axis == "y":
            if bounds[3] <= position:
                return "low"
            if bounds[1] >= position:
                return "high"
        return None

    def owns_edge(self, bounds, position):
        tile_side = self.tile_side(bounds, position)
        if tile_side is None:
            return True
        if self.owner is None:
            # Deterministic default: the lower / left / bottom tile owns the edge.
            return tile_side == "low"
        if self.owner == "low":
            return tile_side == "low"
        if self.owner == "high":
            return tile_side == "high"
        if self.owner == "self":
            return True
        if self.owner == "other":
            return False
        return tile_side == "low"


def _shared_edge_from_mapping(value):
    # ``{"x": 10}`` / ``{"y": 10}`` is a compact physical position declaration.
    if len(value) == 1:
        name, position = next(iter(value.items()))
        if (
            isinstance(name, str)
            and name.strip().lower() in {"x", "y", "vertical", "horizontal"}
            and isinstance(position, Number)
            and not isinstance(position, bool)
        ):
            return SharedEdge(axis=name, position=position)
    return SharedEdge(
        axis=_first(value, _AXIS_KEYS),
        position=_first(value, _POSITION_KEYS),
        edge_side=_first(value, _EDGE_KEYS),
        owner=_first(value, _OWNER_KEYS),
        ticks=_first(value, _TICK_KEYS),
        origin=_first(value, _ORIGIN_KEYS, 0.0),
    )


def _normalize_shared_edges(value):
    if value is None:
        return []
    if isinstance(value, SharedEdge):
        return [value]
    if isinstance(value, str):
        # ``"left"`` / ``"top"`` names the tile boundary which is shared.
        return [SharedEdge(edge_side=value)]
    if isinstance(value, dict):
        nested = _first(value, ("edges", "shared_edges"))
        if nested is not None:
            return _normalize_shared_edges(nested)
        return [_shared_edge_from_mapping(value)]
    if isinstance(value, (list, tuple)):
        if not value:
            return []
        if all(isinstance(item, SharedEdge) for item in value):
            return list(value)
        if all(isinstance(item, dict) for item in value):
            return [_shared_edge_from_mapping(item) for item in value]
        if all(isinstance(item, str) for item in value):
            return [SharedEdge(edge_side=item) for item in value]
        raise ClipError(f"Unable to read shared edges from {value!r}; use a mapping.")
    raise ClipError(f"Unable to read shared edges from {value!r}")


def _flat_shared_edge(options):
    axis = _first(options, ("shared_edge_axis", "edge_axis", "clip_edge_axis"))
    position = _first(options, ("shared_edge_position", "edge_position", "clip_edge_position"))
    edge_side = _first(options, ("edge", "clip_edge", "border"))
    if not isinstance(edge_side, str):
        edge_side = None
    owner = _first(
        options,
        (
            "shared_edge_owner",
            "shared_edge_ownership",
            "edge_owner",
            "edge_ownership",
            "clip_edge_owner",
            "clip_edge_ownership",
            "border_owner",
            "border_ownership",
            "clip_owner",
            "clip_ownership",
        ),
    )
    ticks = _first(
        options,
        (
            "shared_edge_ticks",
            "shared_edge_tick",
            "shared_edge_tick_spacing",
            "shared_edge_tick_step",
            "edge_ticks",
            "edge_tick",
            "edge_tick_spacing",
            "edge_tick_step",
            "clip_edge_ticks",
            "clip_edge_tick_spacing",
            "border_ticks",
            "border_tick_spacing",
            "clip_ticks",
            "clip_tick",
            "clip_tick_spacing",
            "clip_tick_step",
        ),
    )
    origin = _first(
        options,
        ("shared_edge_tick_origin", "edge_tick_origin", "clip_edge_tick_origin", "clip_tick_origin"),
        0.0,
    )

    if all(v is None for v in (axis, position, edge_side, owner, ticks)):
        return None
    return SharedEdge(axis=axis, position=position, edge_side=edge_side, owner=owner, ticks=ticks, origin=origin)


class Clipper:
    """Geometry clipper configured from a layer's encode options."""

    def __init__(self, bounds, shared_edges=()):
        self.bounds = bounds
        self.shared_edges = tuple(shared_edges)
        self._minx, self._miny, self._maxx, self._maxy = bounds
        self._box = box(*bounds)

    def _edges_with_positions(self):
        result = []
        for edge in self.shared_edges:
            position = edge.resolve_position(self.bounds)
            if position is None:
                continue
            # A shared edge only describes a boundary of this tile.  An edge
            # which lies strictly inside (or outside) the clip rectangle is
            # not shared with a neighbour and must not change the geometry.
            on_boundary = (
                edge.axis == "x" and (self._near(position, self._minx) or self._near(position, self._maxx))
            ) or (edge.axis == "y" and (self._near(position, self._miny) or self._near(position, self._maxy)))
            if on_boundary:
                result.append((edge, position))
        return result

    def _owned(self, edge, position):
        return edge.owns_edge(self.bounds, position)

    @staticmethod
    def _near(value, target):
        # Vertices produced by the segment clipper lie exactly on the bounds.
        # The tolerance also catches input vertices intended to be on the edge
        # but carrying floating point noise.
        return abs(value - target) <= 1e-9

    def _snap_point(self, x, y):
        for edge, position in self._edges_with_positions():
            if edge.axis == "x" and self._near(x, position):
                x = position
                y = edge.ticks.snap(y)
            elif edge.axis == "y" and self._near(y, position):
                y = position
                x = edge.ticks.snap(x)
        return min(max(x, self._minx), self._maxx), min(max(y, self._miny), self._maxy)

    def _points_on_owned_edge(self, q, p):
        """Return whether the segment ``q``-``p`` lies along an owned shared edge."""
        qx, qy = q
        px, py = p
        for edge, position in self._edges_with_positions():
            if not self._owned(edge, position):
                continue
            if edge.axis == "x" and self._near(qx, position) and self._near(px, position):
                return True
            if edge.axis == "y" and self._near(qy, position) and self._near(py, position):
                return True
        return False

    def _on_shared_edge(self, point):
        x, y = point
        for edge, position in self._edges_with_positions():
            if (edge.axis == "x" and self._near(x, position)) or (edge.axis == "y" and self._near(y, position)):
                return True
        return False

    def _snap_junctions(self, coords):
        """Snap only the points where a line meets or leaves a shared edge.

        Snapping every vertex which happens to lie on the edge would deform a
        line which merely touches the edge and turns back.  Those touch points
        are not shared with the neighbouring tile, so they keep their source
        position.  Junctions - run endpoints and the ends of an on-edge run -
        are the points both tiles have to agree on.
        """
        snapped = list(coords)
        last = len(coords) - 1
        for index, point in enumerate(coords):
            if not self._on_shared_edge(point):
                continue
            previous_on_edge = index > 0 and self._points_on_owned_edge(coords[index - 1], point)
            next_on_edge = index < last and self._points_on_owned_edge(point, coords[index + 1])
            if index in (0, last) or previous_on_edge or next_on_edge:
                snapped[index] = self._snap_point(*point)
        return snapped

    def _segment_on_unowned_edge(self, q, p):
        qx, qy = q
        px, py = p
        for edge, position in self._edges_with_positions():
            if self._owned(edge, position):
                continue
            if edge.axis == "x" and self._near(qx, position) and self._near(px, position):
                return True
            if edge.axis == "y" and self._near(qy, position) and self._near(py, position):
                return True
        return False

    def _point_on_unowned_edge(self, x, y):
        for edge, position in self._edges_with_positions():
            if self._owned(edge, position):
                continue
            if (edge.axis == "x" and self._near(x, position)) or (edge.axis == "y" and self._near(y, position)):
                return True
        return False

    def _clip_segment(self, x0, y0, x1, y1):
        """Liang-Barsky clip of one segment to the tile rectangle."""
        dx = x1 - x0
        dy = y1 - y0
        t0, t1 = 0.0, 1.0
        for p, q in (
            (-dx, x0 - self._minx),
            (dx, self._maxx - x0),
            (-dy, y0 - self._miny),
            (dy, self._maxy - y0),
        ):
            if p == 0:
                if q < 0:
                    return None
                continue
            ratio = q / p
            if p < 0:
                if ratio > t1:
                    return None
                if ratio > t0:
                    t0 = ratio
            else:
                if ratio < t0:
                    return None
                if ratio < t1:
                    t1 = ratio
        if t0 > t1:
            return None
        return (x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy)

    @staticmethod
    def _dedupe(coords):
        result = []
        for point in coords:
            if not result or point != result[-1]:
                result.append(point)
        return result

    @staticmethod
    def _clean_line(coords):
        """Drop duplicate and exactly collinear intermediate vertices."""
        coords = Clipper._dedupe(coords)
        if len(coords) < 3:
            return coords
        result = [coords[0]]
        for index in range(1, len(coords) - 1):
            ax, ay = result[-1]
            bx, by = coords[index]
            cx, cy = coords[index + 1]
            cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            scale = max(1.0, abs(bx - ax) + abs(by - ay) + abs(cx - ax) + abs(cy - ay))
            if abs(cross) > 1e-12 * scale:
                result.append((bx, by))
        result.append(coords[-1])
        return result

    def _strip_edge_segments(self, coords):
        runs = []
        current = []
        for index, point in enumerate(coords):
            if index == 0:
                current = [point]
                continue
            previous = coords[index - 1]
            if self._segment_on_unowned_edge(previous, point):
                if len(current) >= 2:
                    runs.append(current)
                current = []
            else:
                if not current:
                    current = [previous]
                current.append(point)
        if len(current) >= 2:
            runs.append(current)
        return runs

    def _clip_line(self, shape):
        parts = []
        current = []
        for arc in _iter_lines(shape):
            coords = list(arc.coords)
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                clipped = self._clip_segment(x0, y0, x1, y1)
                if clipped is None:
                    if len(current) >= 2:
                        parts.append(current)
                    current = []
                    continue
                start, end = clipped
                if start == end:
                    continue
                if current and current[-1] == start:
                    current.append(end)
                else:
                    if len(current) >= 2:
                        parts.append(current)
                    current = [start, end]
            if len(current) >= 2:
                parts.append(current)
                current = []

        result = []
        for part in parts:
            part = self._clean_line(part)
            for run in self._strip_edge_segments(part):
                run = self._clean_line(self._snap_junctions(run))
                if len(run) >= 2:
                    result.append(run)
        return _lines_from_coords(result)

    def _clip_point(self, shape):
        points = []
        for point in _iter_points(shape):
            if not (self._minx <= point.x <= self._maxx and self._miny <= point.y <= self._maxy):
                continue
            x, y = self._snap_point(point.x, point.y)
            if self._point_on_unowned_edge(x, y):
                continue
            points.append((x, y))
        if not points:
            return None
        if len(points) == 1:
            return Point(points[0])
        return MultiPoint(points)

    def _clip_polygon(self, shape):
        clipped = shape.intersection(self._box)
        polygons = []
        for polygon in _iter_polygons(clipped):
            exterior = [self._snap_point(x, y) for x, y in polygon.exterior.coords]
            holes = [[self._snap_point(x, y) for x, y in ring.coords] for ring in polygon.interiors]
            candidate = Polygon(exterior, holes)
            if not candidate.is_empty and candidate.area > 0:
                polygons.append(candidate)
        if not polygons:
            return None
        if len(polygons) == 1:
            return polygons[0]
        return MultiPolygon(polygons)

    def clip(self, shape):
        if shape is None or shape.is_empty:
            return shape
        if not isinstance(shape, BaseGeometry):
            shape = shape.geometry

        if shape.geom_type in {"LineString", "MultiLineString"}:
            return self._clip_line(shape)
        if shape.geom_type in {"Point", "MultiPoint"}:
            return self._clip_point(shape)
        if shape.geom_type in {"Polygon", "MultiPolygon"}:
            return self._clip_polygon(shape)
        if shape.geom_type == "GeometryCollection":
            # Geometry collections are not supported by the encoder.
            return None
        return shape


def _iter_lines(geometry):
    if geometry.geom_type == "LineString":
        yield geometry
    elif geometry.geom_type == "MultiLineString":
        yield from geometry.geoms


def _iter_points(geometry):
    if geometry.geom_type == "Point":
        yield geometry
    elif geometry.geom_type == "MultiPoint":
        yield from geometry.geoms


def _iter_polygons(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
    elif geometry.geom_type == "MultiPolygon":
        yield from geometry.geoms
    elif geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            if part.geom_type == "Polygon":
                yield part
            elif part.geom_type == "MultiPolygon":
                yield from part.geoms


def _lines_from_coords(parts):
    lines = [LineString(part) for part in parts if len(part) >= 2]
    lines = [line for line in lines if not line.is_empty]
    if not lines:
        return None
    if len(lines) == 1:
        return lines[0]
    return MultiLineString(lines)


def _resolve_bounds(options, raw_clip, nested_shared):
    if isinstance(raw_clip, dict):
        bounds = normalize_bounds(_first(raw_clip, _BOUNDS_KEYS))
        if bounds is None and nested_shared is None:
            bounds = normalize_bounds(raw_clip)
        return bounds
    if raw_clip is True:
        bounds = normalize_bounds(options.get("quantize_bounds"))
        if bounds is None:
            raise ClipError("Clipping was enabled but no clip bounds were provided.")
        return bounds
    return normalize_bounds(raw_clip)


def _merge_flat_edge(shared_edges, flat_edge):
    if flat_edge is None:
        return shared_edges
    if not shared_edges:
        return [flat_edge]
    for edge in shared_edges:
        if edge.owner is None and flat_edge.owner is not None:
            edge.owner = flat_edge.owner
        if not edge.ticks.enabled and flat_edge.ticks.enabled:
            edge.ticks = flat_edge.ticks
        if edge.axis is None:
            edge.axis = flat_edge.axis
        if edge.position is None:
            edge.position = flat_edge.position
    return shared_edges


def resolve_clipper(options):
    """Build a :class:`Clipper` from encode ``options``, or return ``None``."""
    if not options:
        return None

    raw_clip = _first(
        options,
        (
            "clip",
            "clipping",
            "enable_clip",
            "clip_geometry",
            "clip_bounds",
            "clip_box",
            "clip_rect",
            "clip_region",
            "tile_bounds",
            "bounds",
        ),
    )
    if isinstance(raw_clip, dict) and not raw_clip:
        raw_clip = None
    nested_shared = None
    if isinstance(raw_clip, dict):
        nested_shared = _first(raw_clip, ("shared_edge", "shared_edges", "edges", "clip_edges", "edge"))
        if nested_shared is None and any(
            key in raw_clip for key in ("owner", "ownership", "ticks", "tick", "tick_spacing")
        ):
            # The ownership/tick declaration may live directly next to the
            # bounds: {"bounds": ..., "edge": "left", "owner": "low"}.
            nested_shared = raw_clip

    bounds = _resolve_bounds(options, raw_clip, nested_shared)
    shared_value = _first(
        options,
        ("shared_edge", "shared_edges", "shared_edge_grid", "clip_shared_edge", "clip_shared_edges", "edges"),
    )
    if shared_value is None:
        edge_value = options.get("edge")
        if isinstance(edge_value, (dict, SharedEdge, list, tuple)):
            shared_value = edge_value
    border_value = options.get("border")
    if shared_value is None and isinstance(border_value, (dict, SharedEdge, list, tuple)):
        shared_value = border_value
    if shared_value is None:
        shared_value = nested_shared
    flat_edge = _flat_shared_edge(options)

    requested = (raw_clip is not None and raw_clip is not False) or shared_value is not None or flat_edge is not None
    if not requested:
        return None

    if bounds is None:
        bounds = normalize_bounds(options.get("quantize_bounds"))
    if bounds is None:
        raise ClipError("Clipping requires clip_bounds or quantize_bounds.")

    shared_edges = _merge_flat_edge(_normalize_shared_edges(shared_value), flat_edge)
    for edge in shared_edges:
        if edge.axis is None:
            edge.axis = "x"

    return Clipper(bounds, shared_edges)


def clip_geometry(shape, bounds, shared_edges=None):
    """Convenience wrapper around :class:`Clipper`."""
    return Clipper(normalize_bounds(bounds), _normalize_shared_edges(shared_edges)).clip(shape)

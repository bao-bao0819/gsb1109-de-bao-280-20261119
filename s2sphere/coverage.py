"""Re-scan work orders for regions authorized by two suppliers at once.

Each supplier may hand in several lat/lng rectangles in one batch and every
rectangle carries one authorization number.  A single number can be split
into several rectangles by the export or appear more than once, so the
records are de-duplicated per supplier into a set of rectangles and a set
of authorization numbers before anything else happens.

The output is a list of work orders.  Every work order owns one region whose
points share one fixed, complete set of authorization numbers and holds the
list of tiles to feed to :class:`~s2sphere.sphere.RegionCoverer` one by one.
The scanner can only use the numbers written on the work order, therefore
regions that share a location never share it: the arrangement below gives
every interior, edge and vertex to exactly one work order.  Edges and
vertices only get their own work order when their authorization set really
differs from every adjacent interior, so an artificial split line carrying
the same number never produces a new work order while a true permission
boundary is kept even when the S2 cell lists on both sides are identical.

Longitude is unwrapped while building the planar arrangement, so rectangles
crossing the antimeridian (for example a band running from 110 degrees
eastward to 70 degrees) are handled exactly like any other rectangle.
Candidate observations are filtered against the exact tile boundaries,
which also drops the extra cells that a rectangular bound pulls in around
non-rectangular (edge/point) tiles, and an observation entered once at
+180 degrees and once at -180 degrees is dispatched a single time.
"""
from __future__ import absolute_import, division, print_function

import math

from .sphere import (
    CellUnion,
    LatLng,
    LatLngRect,
    LineInterval,
    SphereInterval,
)

FULL_LONGITUDE = 2.0 * math.pi


def _wrapped_lng(radians):
    """Normalize a longitude to the canonical [-pi, pi] interval."""
    wrapped = radians % FULL_LONGITUDE
    if wrapped > math.pi:
        wrapped -= FULL_LONGITUDE
    return wrapped


def dispatch(work_orders, latlng):
    """Return the unique work order owning one observation point.

    Closed rectangle tiles geometrically overlap where two work orders meet,
    so membership alone is ambiguous on a shared edge or vertex.  The
    arrangement emits lower-dimension tiles exactly for those locations; the
    owner is therefore the matching tile with the smallest dimension, and
    ties inside one dimension resolve through the stable work order id.
    """
    normalized = latlng.normalized()
    best = None
    for order in work_orders:
        for tile in order.tiles:
            if tile.contains(normalized):
                candidate = (tile.dimension,
                             order.work_order_id
                             if order.work_order_id is not None else 0)
                if best is None or candidate < best[0]:
                    best = (candidate, order)
                break
    if best is None:
        return None
    return best[1]


def _seam_key(radians):
    """Canonical key that identifies the +180/-180 seam as one longitude."""
    if abs(abs(radians) - math.pi) < 1e-12:
        return math.pi
    return radians


class AuthorizationRecord(object):
    """One exported rectangle together with its authorization number."""

    def __init__(self, supplier, auth_number, rect):
        self.supplier = supplier
        self.auth_number = auth_number
        self.rect = rect

    @classmethod
    def from_degrees(cls, supplier, auth_number, lat_lo, lng_lo,
                     lat_hi, lng_hi):
        lo = math.radians(lng_lo)
        hi = math.radians(lng_hi)
        if lo == -math.pi and hi == math.pi:
            lng_interval = SphereInterval.full()
        else:
            # SphereInterval only accepts the +pi spelling of the seam.
            if lo == -math.pi:
                lo = math.pi
            if hi == -math.pi:
                hi = math.pi
            lng_interval = SphereInterval(lo, hi, args_checked=True)
        rect = LatLngRect(
            LineInterval(math.radians(lat_lo), math.radians(lat_hi)),
            lng_interval)
        return cls(supplier, auth_number, rect)


class _Segment(object):
    """A connected longitude piece kept on the unwrapped axis.

    ``lo`` may be outside [-pi, pi] while the arrangement is built.  Folding
    back to a :class:`SphereInterval` happens in :meth:`as_lng_interval`.
    """

    __slots__ = ('lo', 'hi')

    def __init__(self, lo, hi):
        self.lo = lo
        self.hi = hi

    @property
    def is_point(self):
        return self.lo == self.hi

    def as_lng_interval(self):
        lo = _wrapped_lng(self.lo)
        if self.is_point:
            hi = lo
        else:
            hi = _wrapped_lng(self.hi)
            if self.hi - self.lo >= FULL_LONGITUDE:
                hi = lo + FULL_LONGITUDE
        return SphereInterval(lo, hi, args_checked=True)

    def as_rect(self, lat_lo, lat_hi):
        return LatLngRect(LineInterval(lat_lo, lat_hi),
                          self.as_lng_interval())


class Tile(object):
    """One exact, connected piece of a work order on the sphere.

    A tile is a rectangle in latitude/longitude space.  Zero-height
    rectangles describe boundary edges and zero-area rectangles describe
    boundary vertices.  ``rect`` is the exact boundary used to filter
    candidate observations and the rectangle handed to
    :class:`~s2sphere.sphere.RegionCoverer`.
    """

    def __init__(self, lat_lo, lat_hi, segment):
        self.lat_lo = lat_lo
        self.lat_hi = lat_hi
        self.segment = segment

    @property
    def dimension(self):
        """2 for an interior face, 1 for an edge, 0 for a vertex."""
        return 2 - int(self.lat_lo == self.lat_hi) \
            - int(self.segment.is_point)

    @property
    def rect(self):
        return self.segment.as_rect(self.lat_lo, self.lat_hi)

    @property
    def bounds(self):
        return self.rect

    def contains(self, latlng):
        """Closed membership, including the +180/-180 seam."""
        rect = self.rect
        if rect.contains(latlng):
            return True
        lng = latlng.lng().radians
        if abs(abs(lng) - math.pi) < 1e-12:
            flipped = LatLng.from_radians(
                latlng.lat().radians,
                math.pi if lng < 0 else -math.pi)
            if rect.contains(flipped):
                return True
        return False


class WorkOrder(object):
    """A re-scannable region with one fixed authorization set.

    ``auth_numbers`` maps every supplier to the frozenset of numbers that
    authorize this work order (a single number per supplier in the standard
    two-party case); ``tiles`` is the list of rectangles, possibly
    degenerate, to cover with :class:`~s2sphere.sphere.RegionCoverer`.
    """

    def __init__(self, work_order_id, suppliers, auth_numbers, tiles):
        self.work_order_id = work_order_id
        self.suppliers = tuple(suppliers)
        self.auth_numbers = {
            supplier: frozenset(numbers)
            for supplier, numbers in auth_numbers.items()}
        self.tiles = list(tiles)

    @property
    def auth_set(self):
        numbers = set()
        for supplier in self.suppliers:
            numbers.update(self.auth_numbers[supplier])
        return frozenset(numbers)

    def number(self, supplier):
        """Return the unique authorization number of one supplier."""
        numbers = self.auth_numbers[supplier]
        if len(numbers) != 1:
            raise KeyError(
                'supplier {!r} has several numbers on this work order: {}'
                .format(supplier, sorted(numbers)))
        return next(iter(numbers))

    def covering(self, coverer):
        """Return the de-duplicated cell id covering of every tile."""
        cell_ids = []
        for tile in self.tiles:
            cell_ids.extend(coverer.get_covering(tile.bounds))
        return CellUnion(cell_ids).cell_ids()

    def contains_point(self, latlng):
        """Exact boundary membership used to filter candidate observations."""
        normalized = latlng.normalized()
        return any(tile.contains(normalized) for tile in self.tiles)

    def filter_candidates(self, candidates, coverer):
        """Keep only candidate observations inside the exact tile boundaries.

        Candidates are ``(longitude, latitude)`` pairs in radians in any
        convention.  The rectangular bounds of a degenerate tile can pull in
        extra cells; the exact tile check drops them.  Observations entered
        at both +180 degrees and -180 degrees share one seam key and are
        dispatched a single time.
        """
        covering = CellUnion(self.covering(coverer))
        accepted = {}
        for lng, lat in candidates:
            normalized = LatLng.from_radians(lat, lng).normalized()
            if not covering.contains(normalized.to_point()):
                continue
            if not any(tile.contains(normalized) for tile in self.tiles):
                continue
            key = (normalized.lat().radians,
                   _seam_key(normalized.lng().radians))
            if key not in accepted:
                accepted[key] = (lng, lat)
        return list(accepted.values())


def _unwrapped_segments(rect):
    """Split a longitude interval into connected unwrapped segments.

    A normal interval yields one segment.  An inverted (antimeridian
    crossing) interval is cut at the seam into two connected segments, one
    on each side of the canonical [-pi, pi] range.
    """
    interval = rect.lng()
    if interval.is_empty():
        return []
    lo = interval.lo()
    hi = interval.hi()
    if not interval.is_inverted():
        return [_Segment(lo, hi)]
    return [_Segment(lo, math.pi), _Segment(-math.pi, hi)]


class _Grid(object):
    """All break coordinates of an arrangement on one axis."""

    def __init__(self):
        self.values = []

    def add(self, value):
        if value not in self.values:
            self.values.append(value)

    def prepare(self):
        self.values.sort()

    def index(self, value):
        return self.values.index(value)


class _SupplierLayer(object):
    """Rectangles and authorization numbers of one supplier."""

    def __init__(self, supplier):
        self.supplier = supplier
        self.rectangles = []
        self.auth_by_rect = {}

    def add(self, auth_number, rect):
        key = (rect.lat().bounds(), rect.lng().bounds())
        if key in self.auth_by_rect:
            self.auth_by_rect[key].add(auth_number)
            return
        self.auth_by_rect[key] = {auth_number}
        self.rectangles.append(rect)

    def numbers_at(self, lat_lo, lat_hi, lng_lo, lng_hi):
        """All numbers covering a closed piece of the unwrapped plane.

        A point sitting on the antimeridian is queried with both longitude
        signs because the +180 and -180 representations name one meridian.
        """
        queries = [(lng_lo, lng_hi)]
        if lng_lo == lng_hi and \
                abs(abs(lng_lo) - math.pi) < 1e-12:
            queries.append((-lng_lo, -lng_hi))
        numbers = set()
        for query_lo, query_hi in queries:
            for rect in self.rectangles:
                lat = rect.lat()
                if lat_lo < lat.lo() or lat_hi > lat.hi():
                    continue
                for segment in _unwrapped_segments(rect):
                    if query_lo >= segment.lo and \
                            query_hi <= segment.hi:
                        numbers.update(
                            self.auth_by_rect[
                                (lat.bounds(), rect.lng().bounds())])
                        break
        return frozenset(numbers)


class CoveragePlan(object):
    """Arrangement of two supplier layers into re-scan work orders."""

    def __init__(self, first_supplier, second_supplier):
        self.suppliers = (first_supplier, second_supplier)
        self._layers = {
            first_supplier: _SupplierLayer(first_supplier),
            second_supplier: _SupplierLayer(second_supplier),
        }

    def add_record(self, record):
        if record.supplier not in self._layers:
            raise ValueError(
                'unknown supplier: {!r}'.format(record.supplier))
        self._layers[record.supplier].add(record.auth_number, record.rect)

    def build(self):
        lat_grid = _Grid()
        lng_grid = _Grid()
        for layer in self._layers.values():
            for rect in layer.rectangles:
                lat_grid.add(rect.lat().lo())
                lat_grid.add(rect.lat().hi())
                for segment in _unwrapped_segments(rect):
                    lng_grid.add(segment.lo)
                    lng_grid.add(segment.hi)
        lat_grid.prepare()
        lng_grid.prepare()

        face_suppliers = self._paint_faces(lat_grid, lng_grid)
        face_auth = self._face_authorizations(
            lat_grid, lng_grid, face_suppliers)

        # Closed rectangles geometrically overlap on every shared boundary, so
        # unique ownership is a dimension-priority dispatch: a vertex wins
        # over an edge, an edge wins over a face.  Lower-dimension elements
        # are only emitted when some element containing them disagrees, hence
        # an artificial same-number split line stays inside its face while a
        # real permission change gets its own work order.
        edge_auth, edge_tiles, edge_owners = self._edge_elements(
            lat_grid, lng_grid, face_auth)
        vertex_tiles = self._vertex_elements(
            lat_grid, lng_grid, face_auth, edge_auth, edge_owners)

        groups = {}
        for key, tile in self._face_tiles(lat_grid, lng_grid, face_auth):
            groups.setdefault(key, []).append(tile)
        for key, tile in edge_tiles:
            groups.setdefault(key, []).append(tile)
        for key, tile in vertex_tiles:
            groups.setdefault(key, []).append(tile)

        work_orders = []
        for key, tiles in groups.items():
            numbers = dict(key)
            work_orders.append(
                WorkOrder(None, self.suppliers, numbers,
                          _fold_segments(tiles)))
        return work_orders

    def _paint_faces(self, lat_grid, lng_grid):
        """Mark the supplier of every open unit face.

        Faces are painted from each rectangle directly instead of pairing
        records across suppliers, so disjoint longitude bands never
        materialize cross-supplier candidate pairs.
        """
        face_suppliers = {}
        for supplier, layer in self._layers.items():
            for rect in layer.rectangles:
                i_lo = lat_grid.index(rect.lat().lo())
                i_hi = lat_grid.index(rect.lat().hi())
                for segment in _unwrapped_segments(rect):
                    j_lo = lng_grid.index(segment.lo)
                    j_hi = lng_grid.index(segment.hi)
                    for i in range(i_lo, i_hi):
                        for j in range(j_lo, j_hi):
                            face_suppliers.setdefault(
                                (i, j), set()).add(supplier)
        return face_suppliers

    def _auth_set(self, lat_lo, lat_hi, lng_lo, lng_hi):
        numbers = {}
        for supplier in self.suppliers:
            found = self._layers[supplier].numbers_at(
                lat_lo, lat_hi, lng_lo, lng_hi)
            if not found:
                return None
            numbers[supplier] = found
        return tuple(sorted(numbers.items()))

    def _face_authorizations(self, lat_grid, lng_grid, face_suppliers):
        face_auth = {}
        lats = lat_grid.values
        lngs = lng_grid.values
        for i in range(len(lats) - 1):
            for j in range(len(lngs) - 1):
                suppliers = face_suppliers.get((i, j))
                if suppliers is None or not all(
                        supplier in suppliers for supplier in self.suppliers):
                    continue
                auth = self._auth_set(
                    lats[i], lats[i + 1], lngs[j], lngs[j + 1])
                if auth is not None:
                    face_auth[(i, j)] = auth
        return face_auth

    def _face_tiles(self, lat_grid, lng_grid, face_auth):
        lats = lat_grid.values
        lngs = lng_grid.values
        for (i, j), auth in face_auth.items():
            tile = Tile(lats[i], lats[i + 1],
                        _Segment(lngs[j], lngs[j + 1]))
            yield auth, tile

    def _edge_elements(self, lat_grid, lng_grid, face_auth):
        """Emit edges that are not uniformly owned by their adjacent faces.

        An edge whose set equals every dual-authorized adjacent face is a
        split line with no permission change: it stays inside the closed
        face tiles and produces no work order.  As soon as one adjacent face
        disagrees (or is not dual-authorized), the edge becomes its own
        owner so that each location has a unique work order.  Vertical edges
        sitting on the antimeridian are merged, since +180 and -180 are the
        same physical meridian.
        """
        lats = lat_grid.values
        lngs = lng_grid.values
        edge_auth = {}
        edge_owners = set()
        edge_tiles = []
        seam = self._seam_columns(lngs)

        def consider(auth, adjacent, tile, edge_key):
            if auth is None:
                return
            neighbor_sets = [
                face_auth[face] for face in adjacent
                if face in face_auth]
            if neighbor_sets and all(
                    neighbor == auth for neighbor in neighbor_sets):
                return
            edge_auth[edge_key] = auth
            edge_owners.add(edge_key)
            edge_tiles.append((auth, tile))

        # Horizontal edges: fixed latitude, open longitude span.
        for i in range(len(lats)):
            for j in range(len(lngs) - 1):
                auth = self._auth_set(
                    lats[i], lats[i], lngs[j], lngs[j + 1])
                tile = Tile(lats[i], lats[i],
                            _Segment(lngs[j], lngs[j + 1]))
                consider(auth, ((i - 1, j), (i, j)), tile, ('h', i, j))

        # Vertical edges: fixed longitude, open latitude span.  The two grid
        # columns on the seam describe one meridian and share one edge key.
        j_pos, j_neg = seam
        for j in range(len(lngs)):
            if j_pos is not None and j_neg is not None and j == j_pos:
                continue
            for i in range(len(lats) - 1):
                auth = self._auth_set(
                    lats[i], lats[i + 1], lngs[j], lngs[j])
                tile = Tile(lats[i], lats[i + 1],
                            _Segment(lngs[j], lngs[j]))
                if j_neg is not None and j == j_neg and j_pos is not None:
                    adjacent = ((i, j_neg), (i, j_pos))
                else:
                    adjacent = ((i, j - 1), (i, j))
                consider(auth, adjacent, tile, ('v', i, j))

        return edge_auth, edge_tiles, edge_owners

    @staticmethod
    def _seam_columns(lngs):
        j_pos = j_neg = None
        for j, lng in enumerate(lngs):
            if lng == math.pi:
                j_pos = j
            elif lng == -math.pi:
                j_neg = j
        return j_pos, j_neg

    def _vertex_elements(self, lat_grid, lng_grid, face_auth, edge_auth,
                         edge_owners):
        """Emit vertices not uniformly owned by a containing lower element.

        The two representations of a seam vertex (+180 and -180) are merged
        into one element, gathering the edges and faces around both signs.
        """
        lats = lat_grid.values
        lngs = lng_grid.values
        j_pos, j_neg = self._seam_columns(lngs)
        vertex_tiles = []
        for i in range(len(lats)):
            for j in range(len(lngs)):
                if j_pos is not None and j_neg is not None and j == j_pos:
                    continue
                auth = self._auth_set(lats[i], lats[i], lngs[j], lngs[j])
                if auth is None:
                    continue

                seam_vertex = j_neg is not None and j == j_neg \
                    and j_pos is not None
                if seam_vertex:
                    edge_keys = (
                        ('h', i, j_pos - 1), ('h', i, j_neg),
                        ('v', i - 1, j_neg), ('v', i, j_neg))
                    face_keys = (
                        (i - 1, j_pos - 1), (i, j_pos - 1),
                        (i - 1, j_neg), (i, j_neg))
                    lng = -math.pi
                else:
                    edge_keys = (
                        ('h', i, j - 1), ('h', i, j),
                        ('v', i - 1, j), ('v', i, j))
                    face_keys = (
                        (i - 1, j - 1), (i - 1, j),
                        (i, j - 1), (i, j))
                    lng = lngs[j]

                owner_sets = [
                    edge_auth[edge] for edge in edge_keys
                    if edge in edge_owners]
                if not owner_sets:
                    owner_sets = [
                        face_auth[face] for face in face_keys
                        if face in face_auth]

                if owner_sets and all(
                        owner == auth for owner in owner_sets):
                    continue
                tile = Tile(lats[i], lats[i], _Segment(lng, lng))
                vertex_tiles.append((auth, tile))
        return vertex_tiles


def _fold_segments(tiles):
    """Fold unwrapped longitude pieces back onto the sphere.

    Every piece is converted to a canonical :class:`SphereInterval` on its
    own; two pieces meeting on the antimeridian therefore stay separate
    tiles of one work order but describe one connected region.  Point copies
    sitting on the two seam signs (+180 and -180) collapse to a single
    point tile.
    """
    grouped = {}
    for tile in tiles:
        grouped.setdefault((tile.lat_lo, tile.lat_hi), []).append(tile)

    folded = []
    for (lat_lo, lat_hi), group in grouped.items():
        points = {}
        segments = []
        for tile in group:
            segment = tile.segment
            if segment.is_point:
                points[_seam_key(_wrapped_lng(segment.lo))] = segment
            else:
                segments.append(segment)

        for lng in sorted(points):
            folded.append(Tile(lat_lo, lat_hi, _Segment(lng, lng)))
        for segment in sorted(segments, key=lambda item: item.lo):
            folded.append(Tile(lat_lo, lat_hi, segment))
    return folded


def single_rectangle_intersection(rect_a, rect_b):
    """Intersect two rectangles, keeping the per-rectangle intersection call.

    This is the original single-rectangle intersection: latitude uses
    :meth:`LineInterval.intersection` directly and every pair of connected,
    unwrapped longitude pieces is intersected on its own, so an
    antimeridian-crossing result simply comes back as several rectangles.
    An empty intersection returns an empty list.
    """
    lat = rect_a.lat().intersection(rect_b.lat())
    if lat.is_empty():
        return []

    pieces = []
    seen = set()
    for segment_a in _unwrapped_segments(rect_a):
        for segment_b in _unwrapped_segments(rect_b):
            lo = max(segment_a.lo, segment_b.lo)
            hi = min(segment_a.hi, segment_b.hi)
            if lo > hi:
                continue
            piece = LatLngRect(lat, _Segment(lo, hi).as_lng_interval())
            key = (piece.lat().bounds(), piece.lng().bounds())
            if key not in seen:
                seen.add(key)
                pieces.append(piece)
    return pieces


def build_work_orders(records, first_supplier, second_supplier):
    """Build the re-scan work orders for one batch of records.

    Records may arrive in any order and may contain split or duplicated
    rectangles sharing one authorization number.  Only regions carrying at
    least one authorization number from each of the two suppliers are
    returned; rectangles whose longitude bands never meet therefore never
    produce a cross-supplier candidate.
    """
    plan = CoveragePlan(first_supplier, second_supplier)
    for record in records:
        plan.add_record(record)
    work_orders = plan.build()

    ordered = sorted(
        work_orders,
        key=lambda order: tuple(
            (supplier, sorted(order.auth_numbers[supplier]))
            for supplier in order.suppliers))
    for index, work_order in enumerate(ordered):
        work_order.work_order_id = index
    return ordered

"""Regression tests for S2 ingestion and range reads across face seams.

The pipeline under test is a minimal two-level GPS store built on the
pinned :mod:`s2sphere` library:

* ingestion stores each record under exactly one fine (level-12)
  :class:`s2sphere.CellId` partition and registers that partition in a
  coarse (level-5) directory;
* a range read covers the query region with
  :class:`s2sphere.RegionCoverer`, recalls candidate partitions (possibly
  at mixed levels and on both faces of a seam), merges copies of the same
  observation and finally applies the exact closed-region filter.

All expectations are derived from independent spherical direction
equalities and inequalities (``math.atan2``/``math.hypot`` on the true
input direction).  The covering under test is never used to generate
expectations.

Two reproducible deviations of the pinned library are pinned to the stage
where they originate, without masking the promises that do hold:

* stage ``routing``: for directions with two equal components (face
  seams and corners) the exact 3D direction and any proportional
  scaling of it choose a stable face, but the lat/lng round-trip picks
  another face, because ``cos(pi/4)`` is one ulp larger than
  ``sin(pi/4)``;
* stage ``filter``: the same one-ulp asymmetry moves the lat/lng
  representation of an exact seam point strictly inside one side, so
  the closed region on the other side rejects a record that
  geometrically touches its boundary.
"""

from __future__ import print_function, unicode_literals, division

import math
import unittest

from collections import OrderedDict

from s2sphere import CellId, LatLng, LatLngRect, Point, RegionCoverer

# Two-level ingestion partitioning: fine leaf partitions plus a coarse
# directory level that the query router can recall directly.
INGEST_LEVEL = 12
COARSE_LEVEL = 5
MAX_CANDIDATE_CELLS = 30

# Exclusion reasons, one per pipeline stage, so a failing read can be
# attributed to recall, merge or exact filtering.
NOT_RECALLED = 'not_recalled'
DEDUPED = 'deduped_by_obs_id'
SUPERSEDED = 'superseded_by_valid_copy'
NOT_VALID = 'version_not_valid_for_query'
OUTSIDE_REGION = 'outside_closed_region'

REPR_EXACT = 'exact3d'
REPR_SCALED = 'scaled'
REPR_LATLNG = 'latlng'
REPRS = (REPR_EXACT, REPR_SCALED, REPR_LATLNG)

# The one-ulp cos/sin asymmetry at pi/4 observed on the pinned library.
DRIFT = 2 ** -53


def direction_from_degrees(lat, lng):
    """True direction tuple for a lat/lng, independent of s2sphere."""
    lat_r = math.radians(lat)
    lng_r = math.radians(lng)
    return (math.cos(lng_r) * math.cos(lat_r),
            math.sin(lng_r) * math.cos(lat_r),
            math.sin(lat_r))


def direction_lat_lng(direction):
    """Independent spherical coordinates of a direction tuple."""
    x, y, z = direction
    return math.atan2(z, math.hypot(x, y)), math.atan2(y, x)


def represent(direction, repr_name):
    """Materialize one input representation of the same record."""
    if repr_name == REPR_EXACT:
        return Point(*direction)
    if repr_name == REPR_SCALED:
        return Point(*(7.5 * component for component in direction))
    if repr_name == REPR_LATLNG:
        lat, lng = direction_lat_lng(direction)
        return LatLng.from_radians(lat, lng).to_point()
    raise ValueError(repr_name)


class StoredCopy(object):
    """One ingested copy of an observation."""

    def __init__(self, obs_id, version, point, repr_name, partition):
        self.obs_id = obs_id
        self.version = version
        self.point = point
        self.repr_name = repr_name
        self.partition = partition


class Exclusion(object):
    """A copy that did not survive a query, with the stage reason."""

    def __init__(self, copy, reason):
        self.copy = copy
        self.reason = reason


class QueryOutcome(object):

    def __init__(self, rows, excluded, candidates, recalled):
        self.rows = rows
        self.excluded = excluded
        self.candidates = candidates
        self.recalled = recalled


class TwoLevelStore(object):
    """GPS store keyed by one fine CellId per record, with a coarse
    directory mapping coarse cells to the fine partitions below them."""

    def __init__(self):
        self.fine_index = {}
        self.coarse_index = {}

    def ingest(self, obs_id, version, point, repr_name):
        fine = CellId.from_point(point).parent(INGEST_LEVEL)
        coarse = fine.parent(COARSE_LEVEL)
        copy = StoredCopy(obs_id, version, point, repr_name, fine)
        self.fine_index.setdefault(fine, []).append(copy)
        self.coarse_index.setdefault(coarse, set()).add(fine)
        return copy

    def partition_of(self, obs_id, version=1):
        for key in self.fine_index:
            for copy in self.fine_index[key]:
                if copy.obs_id == obs_id and copy.version == version:
                    return key
        raise KeyError(obs_id)

    def only_copy(self, obs_id, version=1):
        key = self.partition_of(obs_id, version)
        for copy in self.fine_index[key]:
            if copy.obs_id == obs_id and copy.version == version:
                return copy
        raise KeyError(obs_id)

    def partitions_for_candidate(self, candidate):
        if candidate.level() == COARSE_LEVEL:
            return sorted(self.coarse_index.get(candidate, ()))
        return [key for key in self.fine_index
                if candidate.contains(key)]


def covering_candidates(regions, allowed_faces=None):
    """Route a range read: RegionCoverer candidates for each region."""
    candidates = []
    seen = set()
    for region in regions:
        coverer = RegionCoverer()
        coverer.min_level = COARSE_LEVEL
        coverer.max_level = INGEST_LEVEL
        coverer.max_cells = MAX_CANDIDATE_CELLS
        for cell_id in coverer.get_covering(region):
            if (allowed_faces is not None and
                    cell_id.face() not in allowed_faces):
                continue
            if cell_id.id() not in seen:
                seen.add(cell_id.id())
                candidates.append(cell_id)
    return candidates


def run_query(store, regions, valid_version, candidate_order='face_asc',
              merge='version_aware', allowed_faces=None):
    """Staged range read: recall -> merge -> exact closed filter."""
    candidates = covering_candidates(regions, allowed_faces)
    candidates = sorted(candidates, key=lambda c: c.id(),
                        reverse=(candidate_order == 'face_desc'))

    recalled = []
    seen_copies = set()
    for candidate in candidates:
        for key in store.partitions_for_candidate(candidate):
            for copy in store.fine_index[key]:
                if id(copy) not in seen_copies:
                    seen_copies.add(id(copy))
                    recalled.append(copy)

    excluded = []
    if merge == 'naive':
        # Merge before filtering, first copy seen per observation wins.
        selected = []
        seen_obs = set()
        for copy in recalled:
            if copy.obs_id in seen_obs:
                excluded.append(Exclusion(copy, DEDUPED))
            else:
                seen_obs.add(copy.obs_id)
                selected.append(copy)
    else:
        # Version-aware merge: the valid copy for this query is the
        # newest one not newer than the query's version tag.
        selected = []
        by_obs = OrderedDict()
        for copy in recalled:
            by_obs.setdefault(copy.obs_id, []).append(copy)
        for obs_id, copies in by_obs.items():
            eligible = []
            for copy in copies:
                if copy.version > valid_version:
                    excluded.append(Exclusion(copy, NOT_VALID))
                else:
                    eligible.append(copy)
            if not eligible:
                continue
            best = max(copy.version for copy in eligible)
            chosen = None
            for copy in eligible:
                if chosen is None and copy.version == best:
                    chosen = copy
                else:
                    excluded.append(Exclusion(copy, SUPERSEDED))
            selected.append(chosen)

    rows = []
    for copy in selected:
        if any(region.contains(copy.point) for region in regions):
            rows.append(copy)
        else:
            excluded.append(Exclusion(copy, OUTSIDE_REGION))

    for key in store.fine_index:
        for copy in store.fine_index[key]:
            if id(copy) not in seen_copies:
                excluded.append(Exclusion(copy, NOT_RECALLED))

    return QueryOutcome(rows, excluded, candidates, recalled)


def result_ids(outcome):
    return set(row.obs_id for row in outcome.rows)


def result_pairs(outcome):
    return set((row.obs_id, row.version) for row in outcome.rows)


def only_row(outcome, obs_id):
    matches = [row for row in outcome.rows if row.obs_id == obs_id]
    assert len(matches) == 1
    return matches[0]


def exclusion_reasons(outcome, obs_id):
    return dict((ex.copy.version, ex.reason)
                for ex in outcome.excluded
                if ex.copy.obs_id == obs_id)


def lng_of(point):
    return LatLng.from_point(point).lng().radians


# Closed query regions ending exactly at the face seam lng = +45deg.
RECT_A = LatLngRect(LatLng.from_degrees(-40, 10),
                    LatLng.from_degrees(40, 45))
RECT_B = LatLngRect(LatLng.from_degrees(-40, 45),
                    LatLng.from_degrees(40, 80))

# Same layout mirrored on the face seam lng = -45deg.
RECT_A2 = LatLngRect(LatLng.from_degrees(-40, -45),
                     LatLng.from_degrees(40, -10))
RECT_B2 = LatLngRect(LatLng.from_degrees(-40, -80),
                     LatLng.from_degrees(40, -45))

# Records along the seam lng = +45deg: exact co-edge points in several
# directions, near points on either face, the three-face corner, and
# points sharing a coarse candidate cell while lying strictly outside
# the closed queries.
MAIN_RECORDS = (
    ('SEAM_LO', (1.0, 1.0, -0.5)),
    ('SEAM_MID', (1.0, 1.0, 0.0)),
    ('SEAM_HI', (1.0, 1.0, 0.5)),
    ('NEAR_A', (1.0, 1.0 - 1e-9, 0.0)),
    ('NEAR_B', (1.0 - 1e-9, 1.0, 0.0)),
    ('CORNER', (1.0, 1.0, 1.0)),
    ('OUT_A', direction_from_degrees(40.05, 44.0)),
    ('OUT_B', direction_from_degrees(0.0, 80.05)),
)

EXPECTED_A = frozenset(('SEAM_LO', 'SEAM_MID', 'SEAM_HI',
                        'NEAR_A', 'CORNER'))
EXPECTED_B = frozenset(('SEAM_LO', 'SEAM_MID', 'SEAM_HI',
                        'NEAR_B', 'CORNER'))
EXPECTED_U = EXPECTED_A | EXPECTED_B

# Records the lat/lng representation loses on side B of the pinned
# library (one-ulp drift below the seam).
LATLNG_LOST_FROM_B = frozenset(('SEAM_MID', 'CORNER'))

MAIN_FACES_VECTOR = {
    'SEAM_LO': 1, 'SEAM_MID': 1, 'SEAM_HI': 1,
    'NEAR_A': 0, 'NEAR_B': 1, 'CORNER': 2, 'OUT_A': 2, 'OUT_B': 1,
}
MAIN_FACES_LATLNG = {
    'SEAM_LO': 0, 'SEAM_MID': 0, 'SEAM_HI': 0,
    'NEAR_A': 0, 'NEAR_B': 1, 'CORNER': 0, 'OUT_A': 2, 'OUT_B': 1,
}

SEAM2_RECORDS = (
    ('SEAM2_MID', (1.0, -1.0, 0.0)),
    ('NEAR2_A', (1.0, -(1.0 - 1e-9), 0.0)),
    ('NEAR2_B', (1.0, -(1.0 + 1e-9), 0.0)),
    ('CORNER2', (1.0, -1.0, -1.0)),
    ('OUT2_A', direction_from_degrees(-40.05, -44.0)),
    ('OUT2_B', direction_from_degrees(0.0, -80.05)),
)

EXPECTED_A2 = frozenset(('SEAM2_MID', 'NEAR2_A', 'CORNER2'))
EXPECTED_B2 = frozenset(('SEAM2_MID', 'NEAR2_B', 'CORNER2'))
EXPECTED_U2 = EXPECTED_A2 | EXPECTED_B2
LATLNG_LOST_FROM_B2 = frozenset(('SEAM2_MID', 'CORNER2'))

SEAM2_FACES_VECTOR = {
    'SEAM2_MID': 4, 'NEAR2_A': 0, 'NEAR2_B': 4,
    'CORNER2': 5, 'OUT2_A': 5, 'OUT2_B': 4,
}
SEAM2_FACES_LATLNG = {
    'SEAM2_MID': 0, 'NEAR2_A': 0, 'NEAR2_B': 4,
    'CORNER2': 0, 'OUT2_A': 5, 'OUT2_B': 4,
}


def specs_for(records, repr_name):
    return [(obs_id, direction, repr_name) for obs_id, direction in records]


def build_store(specs):
    store = TwoLevelStore()
    for obs_id, direction, repr_name in specs:
        store.ingest(obs_id, 1, represent(direction, repr_name), repr_name)
    return store


class IndependentTruthTest(unittest.TestCase):
    """Expected memberships from independent spherical math only."""

    def test_main_seam_expectations(self):
        # The seam direction satisfies an exact equality: atan2(1, 1)
        # is exactly radians(45), so co-edge records belong to both
        # closed sides.
        self.assertEqual(math.atan2(1.0, 1.0), math.radians(45))
        derived_a = set()
        derived_b = set()
        for obs_id, direction in MAIN_RECORDS:
            lat, lng = direction_lat_lng(direction)
            if math.radians(-40) <= lat <= math.radians(40):
                if math.radians(10) <= lng <= math.radians(45):
                    derived_a.add(obs_id)
                if math.radians(45) <= lng <= math.radians(80):
                    derived_b.add(obs_id)
        self.assertEqual(derived_a, EXPECTED_A)
        self.assertEqual(derived_b, EXPECTED_B)

    def test_main_seam_strict_inequalities(self):
        # Near points are strictly on their own side; the corner is
        # exactly on the seam but strictly below lat 40; the outside
        # points violate a strict inequality.
        _, lng_near_a = direction_lat_lng((1.0, 1.0 - 1e-9, 0.0))
        _, lng_near_b = direction_lat_lng((1.0 - 1e-9, 1.0, 0.0))
        self.assertLess(lng_near_a, math.radians(45))
        self.assertGreater(lng_near_b, math.radians(45))
        lat_corner, lng_corner = direction_lat_lng((1.0, 1.0, 1.0))
        self.assertEqual(lng_corner, math.radians(45))
        self.assertEqual(lat_corner, math.atan2(1.0, math.sqrt(2.0)))
        self.assertLess(lat_corner, math.radians(40))
        lat_out_a, _ = direction_lat_lng(
            direction_from_degrees(40.05, 44.0))
        _, lng_out_b = direction_lat_lng(
            direction_from_degrees(0.0, 80.05))
        self.assertGreater(lat_out_a, math.radians(40))
        self.assertGreater(lng_out_b, math.radians(80))

    def test_second_seam_expectations(self):
        self.assertEqual(math.atan2(-1.0, 1.0), math.radians(-45))
        derived_a2 = set()
        derived_b2 = set()
        for obs_id, direction in SEAM2_RECORDS:
            lat, lng = direction_lat_lng(direction)
            if math.radians(-40) <= lat <= math.radians(40):
                if math.radians(-45) <= lng <= math.radians(-10):
                    derived_a2.add(obs_id)
                if math.radians(-80) <= lng <= math.radians(-45):
                    derived_b2.add(obs_id)
        self.assertEqual(derived_a2, EXPECTED_A2)
        self.assertEqual(derived_b2, EXPECTED_B2)


class MainSeamScenarioTest(unittest.TestCase):

    def test_routing_stage(self):
        stores = dict((repr_name,
                       build_store(specs_for(MAIN_RECORDS, repr_name)))
                      for repr_name in REPRS)
        for obs_id, _ in MAIN_RECORDS:
            key_exact = stores[REPR_EXACT].partition_of(obs_id)
            key_scaled = stores[REPR_SCALED].partition_of(obs_id)
            key_latlng = stores[REPR_LATLNG].partition_of(obs_id)
            # Promise: proportional scaling of the direction is routed
            # to the very same ingest cell as the exact direction.
            self.assertEqual(key_exact, key_scaled,
                             'stage=routing %s' % obs_id)
            self.assertEqual(key_exact.face(), MAIN_FACES_VECTOR[obs_id],
                             'stage=routing exact3d %s' % obs_id)
            self.assertEqual(key_latlng.face(), MAIN_FACES_LATLNG[obs_id],
                             'stage=routing latlng %s' % obs_id)
        # Reproducible deviation of the pinned library: the lat/lng
        # round-trip of an exact co-edge or corner direction picks a
        # different face than the stable vector choice.
        for obs_id in ('SEAM_LO', 'SEAM_MID', 'SEAM_HI'):
            self.assertNotEqual(
                stores[REPR_EXACT].partition_of(obs_id).face(),
                stores[REPR_LATLNG].partition_of(obs_id).face(),
                'stage=routing deviation %s' % obs_id)
        self.assertEqual(
            stores[REPR_EXACT].partition_of('CORNER').face(), 2)
        self.assertEqual(
            stores[REPR_LATLNG].partition_of('CORNER').face(), 0)
        # The exact boundary record is routed to the far side of the
        # side-A query (query A ends at the seam from the face-0 side).
        self.assertEqual(
            stores[REPR_EXACT].partition_of('SEAM_MID').face(), 1,
            'stage=routing: boundary record lives on the other face')

    def test_candidate_recall_stage(self):
        candidates_a = covering_candidates([RECT_A])
        candidates_b = covering_candidates([RECT_B])
        # Mixed levels: coarse directory cells and finer cells together.
        levels_a = set(c.level() for c in candidates_a)
        levels_b = set(c.level() for c in candidates_b)
        self.assertIn(COARSE_LEVEL, levels_a)
        self.assertIn(COARSE_LEVEL, levels_b)
        self.assertTrue(any(level > COARSE_LEVEL for level in levels_a))
        self.assertTrue(any(level > COARSE_LEVEL for level in levels_b))
        for repr_name in REPRS:
            store = build_store(specs_for(MAIN_RECORDS, repr_name))
            key_mid = store.partition_of('SEAM_MID')
            # Both closed sides can hit the same ingested record,
            # whichever face it was routed to.
            self.assertTrue(
                any(c.contains(key_mid) for c in candidates_a),
                'stage=recall side A misses SEAM_MID %s' % repr_name)
            self.assertTrue(
                any(c.contains(key_mid) for c in candidates_b),
                'stage=recall side B misses SEAM_MID %s' % repr_name)
            # A coarse candidate cell covers the fine ingest cell of
            # the corner record on both sides.
            key_corner = store.partition_of('CORNER')
            for candidates in (candidates_a, candidates_b):
                self.assertTrue(
                    any(c.level() == COARSE_LEVEL and
                        c.contains(key_corner) for c in candidates),
                    'stage=recall coarse cover CORNER %s' % repr_name)
            # Outside points share a coarse candidate cell with the
            # ingested records, so the exact filter is what must
            # exclude them.
            key_out_a = store.partition_of('OUT_A')
            key_out_b = store.partition_of('OUT_B')
            self.assertTrue(
                any(c.contains(key_out_a) for c in candidates_a))
            self.assertFalse(
                any(c.contains(key_out_a) for c in candidates_b))
            self.assertTrue(
                any(c.contains(key_out_b) for c in candidates_b))
            self.assertFalse(
                any(c.contains(key_out_b) for c in candidates_a))
            # Necessity: every record expected on a side is recalled
            # by that side's candidates (no recall miss on the pinned
            # library for the closing router).
            for obs_id in EXPECTED_A:
                key = store.partition_of(obs_id)
                self.assertTrue(
                    any(c.contains(key) for c in candidates_a),
                    'stage=recall necessity A %s %s' % (obs_id, repr_name))
            for obs_id in EXPECTED_B:
                key = store.partition_of(obs_id)
                self.assertTrue(
                    any(c.contains(key) for c in candidates_b),
                    'stage=recall necessity B %s %s' % (obs_id, repr_name))

    def test_exact_filter_stage(self):
        for repr_name in REPRS:
            store = build_store(specs_for(MAIN_RECORDS, repr_name))
            for obs_id, _ in MAIN_RECORDS:
                copy = store.only_copy(obs_id)
                in_a = RECT_A.contains(copy.point)
                in_b = RECT_B.contains(copy.point)
                self.assertEqual(in_a, obs_id in EXPECTED_A,
                                 'stage=filter A %s %s' % (obs_id,
                                                           repr_name))
                if repr_name == REPR_LATLNG and \
                        obs_id in LATLNG_LOST_FROM_B:
                    # Reproducible deviation: the stored lat/lng point
                    # drifted one ulp below the seam, so the closed
                    # side B rejects an exactly-touching record.
                    self.assertFalse(
                        in_b, 'stage=filter deviation B %s' % obs_id)
                    drift = lng_of(copy.point) - math.radians(45)
                    self.assertEqual(drift, -DRIFT,
                                     'stage=filter drift %s' % obs_id)
                else:
                    self.assertEqual(in_b, obs_id in EXPECTED_B,
                                     'stage=filter B %s %s' % (obs_id,
                                                               repr_name))
            # The other two co-edge directions do not drift: their
            # rounded lat/lng products collide and land exactly on the
            # seam again (also a pinned, reproducible fact).
            if repr_name == REPR_LATLNG:
                for obs_id in ('SEAM_LO', 'SEAM_HI'):
                    copy = store.only_copy(obs_id)
                    self.assertEqual(lng_of(copy.point),
                                     math.radians(45),
                                     'stage=filter no-drift %s' % obs_id)

    def test_end_to_end_closed_side_a(self):
        for repr_name in REPRS:
            store = build_store(specs_for(MAIN_RECORDS, repr_name))
            outcome = run_query(store, [RECT_A], valid_version=1)
            self.assertEqual(result_ids(outcome), EXPECTED_A,
                             'side A %s' % repr_name)
            # OUT_A was recalled but the exact filter rejected it.
            self.assertEqual(exclusion_reasons(outcome, 'OUT_A'),
                             {1: OUTSIDE_REGION})

    def test_end_to_end_closed_side_b(self):
        for repr_name in REPRS:
            store = build_store(specs_for(MAIN_RECORDS, repr_name))
            outcome = run_query(store, [RECT_B], valid_version=1)
            if repr_name == REPR_LATLNG:
                # Deviation scoped to the drifted records only.
                self.assertEqual(result_ids(outcome),
                                 EXPECTED_B - LATLNG_LOST_FROM_B,
                                 'side B latlng')
                self.assertEqual(
                    exclusion_reasons(outcome, 'SEAM_MID'),
                    {1: OUTSIDE_REGION})
                self.assertEqual(
                    exclusion_reasons(outcome, 'CORNER'),
                    {1: OUTSIDE_REGION})
            else:
                self.assertEqual(result_ids(outcome), EXPECTED_B,
                                 'side B %s' % repr_name)
            # Promises that survive every representation.
            self.assertTrue(
                set(('SEAM_LO', 'SEAM_HI', 'NEAR_B')) <=
                result_ids(outcome),
                'side B unaffected records %s' % repr_name)
            self.assertEqual(exclusion_reasons(outcome, 'OUT_B'),
                             {1: OUTSIDE_REGION})

    def test_end_to_end_merged_union(self):
        for repr_name in REPRS:
            store = build_store(specs_for(MAIN_RECORDS, repr_name))
            outcome = run_query(store, [RECT_A, RECT_B], valid_version=1)
            # Reading both closed sides and merging recovers every
            # expected record for every representation: the union
            # absorbs the one-ulp drift of the lat/lng representation.
            self.assertEqual(result_ids(outcome), EXPECTED_U,
                             'union %s' % repr_name)
            for out_id in ('OUT_A', 'OUT_B'):
                self.assertEqual(exclusion_reasons(outcome, out_id),
                                 {1: OUTSIDE_REGION})


class SecondSeamDirectionTest(unittest.TestCase):
    """The same scenario on the face seam lng = -45deg."""

    def test_routing_stage(self):
        stores = dict((repr_name,
                       build_store(specs_for(SEAM2_RECORDS, repr_name)))
                      for repr_name in REPRS)
        for obs_id, _ in SEAM2_RECORDS:
            key_exact = stores[REPR_EXACT].partition_of(obs_id)
            key_scaled = stores[REPR_SCALED].partition_of(obs_id)
            key_latlng = stores[REPR_LATLNG].partition_of(obs_id)
            self.assertEqual(key_exact, key_scaled,
                             'stage=routing %s' % obs_id)
            self.assertEqual(key_exact.face(),
                             SEAM2_FACES_VECTOR[obs_id],
                             'stage=routing exact3d %s' % obs_id)
            self.assertEqual(key_latlng.face(),
                             SEAM2_FACES_LATLNG[obs_id],
                             'stage=routing latlng %s' % obs_id)
        # Same reproducible deviation, mirrored seam.
        self.assertEqual(
            stores[REPR_EXACT].partition_of('SEAM2_MID').face(), 4)
        self.assertEqual(
            stores[REPR_LATLNG].partition_of('SEAM2_MID').face(), 0)
        self.assertEqual(
            stores[REPR_EXACT].partition_of('CORNER2').face(), 5)
        self.assertEqual(
            stores[REPR_LATLNG].partition_of('CORNER2').face(), 0)

    def test_recall_and_filter_stage(self):
        candidates_a2 = covering_candidates([RECT_A2])
        candidates_b2 = covering_candidates([RECT_B2])
        for repr_name in REPRS:
            store = build_store(specs_for(SEAM2_RECORDS, repr_name))
            key_mid = store.partition_of('SEAM2_MID')
            self.assertTrue(
                any(c.contains(key_mid) for c in candidates_a2))
            self.assertTrue(
                any(c.contains(key_mid) for c in candidates_b2))
            key_corner = store.partition_of('CORNER2')
            for candidates in (candidates_a2, candidates_b2):
                self.assertTrue(
                    any(c.level() == COARSE_LEVEL and
                        c.contains(key_corner) for c in candidates))
            # Mirrored drift: the lat/lng representation of the exact
            # seam direction lands one ulp above lng -45, inside A2.
            if repr_name == REPR_LATLNG:
                for obs_id in LATLNG_LOST_FROM_B2:
                    copy = store.only_copy(obs_id)
                    drift = lng_of(copy.point) - math.radians(-45)
                    self.assertEqual(drift, DRIFT,
                                     'stage=filter drift %s' % obs_id)
                    self.assertFalse(RECT_B2.contains(copy.point))
                    self.assertTrue(RECT_A2.contains(copy.point))

    def test_end_to_end(self):
        for repr_name in REPRS:
            store = build_store(specs_for(SEAM2_RECORDS, repr_name))
            outcome_a = run_query(store, [RECT_A2], valid_version=1)
            self.assertEqual(result_ids(outcome_a), EXPECTED_A2,
                             'side A2 %s' % repr_name)
            outcome_b = run_query(store, [RECT_B2], valid_version=1)
            if repr_name == REPR_LATLNG:
                self.assertEqual(result_ids(outcome_b),
                                 EXPECTED_B2 - LATLNG_LOST_FROM_B2)
                self.assertIn('NEAR2_B', result_ids(outcome_b))
            else:
                self.assertEqual(result_ids(outcome_b), EXPECTED_B2,
                                 'side B2 %s' % repr_name)
            outcome_u = run_query(store, [RECT_A2, RECT_B2],
                                  valid_version=1)
            self.assertEqual(result_ids(outcome_u), EXPECTED_U2,
                             'union2 %s' % repr_name)
            self.assertEqual(exclusion_reasons(outcome_u, 'OUT2_A'),
                             {1: OUTSIDE_REGION})
            self.assertEqual(exclusion_reasons(outcome_u, 'OUT2_B'),
                             {1: OUTSIDE_REGION})


def build_versioned_store():
    """One observation backfilled with a corrected copy.

    OBS-1 is stored twice with different input representations: the old
    copy sits exactly on the seam (lat/lng representation), the
    correction sits strictly inside side B (exact 3D direction).
    OBS-3 is the same seam position as the old OBS-1 copy but stored
    as an exact 3D direction.
    """
    store = TwoLevelStore()
    store.ingest('OBS-1', 1,
                 represent(direction_from_degrees(0.0, 45.0),
                           REPR_LATLNG),
                 REPR_LATLNG)
    store.ingest('OBS-1', 2,
                 represent(direction_from_degrees(0.0, 46.0),
                           REPR_EXACT),
                 REPR_EXACT)
    store.ingest('OBS-2', 1,
                 represent(direction_from_degrees(5.0, 60.0),
                           REPR_LATLNG),
                 REPR_LATLNG)
    store.ingest('OBS-3', 1,
                 represent((1.0, 1.0, 0.0), REPR_EXACT),
                 REPR_EXACT)
    return store


class VersionedCopyMergeTest(unittest.TestCase):

    def test_copy_routing_and_recall(self):
        store = build_versioned_store()
        key_v1 = store.partition_of('OBS-1', 1)
        key_v2 = store.partition_of('OBS-1', 2)
        # Different input representations of the backfill route the two
        # copies to different faces of the seam.
        self.assertEqual(key_v1.face(), 0)
        self.assertEqual(key_v2.face(), 1)
        self.assertNotEqual(key_v1, key_v2)
        self.assertEqual(store.partition_of('OBS-3').face(), 1)
        # Two-sided recall reaches every partition; a router restricted
        # to the home face of side B misses the boundary copy only.
        two_sided = covering_candidates([RECT_B])
        restricted = covering_candidates([RECT_B], allowed_faces={1})
        self.assertTrue(any(c.contains(key_v1) for c in two_sided))
        self.assertTrue(any(c.contains(key_v2) for c in two_sided))
        self.assertFalse(any(c.contains(key_v1) for c in restricted))
        self.assertTrue(any(c.contains(key_v2) for c in restricted))
        self.assertTrue(
            any(c.contains(store.partition_of('OBS-3'))
                for c in restricted))

    def test_count_invisible_wrong_copy(self):
        store = build_versioned_store()
        expected_pairs = set((('OBS-1', 2), ('OBS-2', 1), ('OBS-3', 1)))
        # Version-aware read of the merged union: the promise.
        outcome = run_query(store, [RECT_A, RECT_B], valid_version=2)
        self.assertEqual(result_pairs(outcome), expected_pairs)
        row = only_row(outcome, 'OBS-1')
        self.assertEqual(row.version, 2)
        self.assertAlmostEqual(lng_of(row.point), math.radians(46))
        self.assertEqual(row.partition.face(), 1)
        # Naive merge (first copy wins) returns the same observation
        # ids and the same total row count, so counts cannot see the
        # wrong copy.
        naive = run_query(store, [RECT_A, RECT_B], valid_version=2,
                          merge='naive')
        self.assertEqual(result_ids(naive), set(('OBS-1', 'OBS-2',
                                                 'OBS-3')))
        self.assertEqual(len(naive.rows), len(outcome.rows))
        # The deviation is only visible in the returned raw position,
        # version and source partition.
        stale = only_row(naive, 'OBS-1')
        self.assertEqual(stale.version, 1)
        self.assertLess(lng_of(stale.point), math.radians(45))
        self.assertEqual(stale.partition.face(), 0)
        self.assertEqual(exclusion_reasons(naive, 'OBS-1'),
                         {2: DEDUPED})

    def test_candidate_order_swaps_naive_winner(self):
        store = build_versioned_store()
        ascending = run_query(store, [RECT_A, RECT_B], valid_version=2,
                              merge='naive', candidate_order='face_asc')
        descending = run_query(store, [RECT_A, RECT_B], valid_version=2,
                               merge='naive', candidate_order='face_desc')
        # The naive merge outcome depends on candidate cell order.
        self.assertEqual(only_row(ascending, 'OBS-1').version, 1)
        self.assertEqual(only_row(descending, 'OBS-1').version, 2)
        self.assertEqual(result_ids(ascending), result_ids(descending))

    def test_miss_only_when_merged_before_filter(self):
        store = build_versioned_store()
        # Version-aware read of side B alone returns the valid copy.
        outcome = run_query(store, [RECT_B], valid_version=2)
        self.assertIn(('OBS-1', 2), result_pairs(outcome))
        # Naive merge over both faces' candidates keeps the stale
        # boundary copy; the exact filter then rejects its drifted
        # position and the observation vanishes from side B.
        naive = run_query(store, [RECT_B], valid_version=2,
                          merge='naive')
        self.assertNotIn('OBS-1', result_ids(naive))
        self.assertEqual(exclusion_reasons(naive, 'OBS-1'),
                         {1: OUTSIDE_REGION, 2: DEDUPED})
        # The miss does not touch the other observations.
        self.assertIn('OBS-2', result_ids(naive))
        self.assertIn('OBS-3', result_ids(naive))
        # Querying the two sides separately and merging the rows
        # afterwards matches the joint version-aware union read.
        joint = run_query(store, [RECT_A, RECT_B], valid_version=2)
        side_a = run_query(store, [RECT_A], valid_version=2)
        side_b = run_query(store, [RECT_B], valid_version=2)
        merged = result_pairs(side_a) | result_pairs(side_b)
        self.assertEqual(merged, result_pairs(joint))
        # Side A alone holds no valid copy of OBS-1: the stale copy is
        # superseded and the valid copy lies outside the closed side.
        self.assertNotIn('OBS-1', result_ids(side_a))
        self.assertEqual(exclusion_reasons(side_a, 'OBS-1'),
                         {1: SUPERSEDED, 2: OUTSIDE_REGION})

    def test_recall_miss_vs_filter_miss(self):
        store = build_versioned_store()
        key_v1 = store.partition_of('OBS-1', 1)
        copy_v1 = store.only_copy('OBS-1', 1)
        # Independent truth: the origin is the direction with equal
        # x/y components, which lies exactly on the seam and therefore
        # belongs to the closed side B.
        _, true_lng = direction_lat_lng((1.0, 1.0, 0.0))
        self.assertEqual(true_lng, math.radians(45))
        # (a) Two-sided recall with the boundary copy as the valid
        # version: the copy is recalled but the exact filter rejects
        # its drifted stored position (filter-stage deviation).
        outcome = run_query(store, [RECT_B], valid_version=1)
        self.assertTrue(any(c.contains(key_v1)
                            for c in outcome.candidates))
        self.assertFalse(RECT_B.contains(copy_v1.point))
        self.assertNotIn('OBS-1', result_ids(outcome))
        self.assertEqual(exclusion_reasons(outcome, 'OBS-1'),
                         {1: OUTSIDE_REGION, 2: NOT_VALID})
        # (b) A router that only reads the home face of side B loses
        # the same copy at the recall stage instead.
        restricted = run_query(store, [RECT_B], valid_version=1,
                               allowed_faces={1})
        self.assertFalse(any(c.contains(key_v1)
                             for c in restricted.candidates))
        self.assertNotIn('OBS-1', result_ids(restricted))
        self.assertEqual(exclusion_reasons(restricted, 'OBS-1'),
                         {1: NOT_RECALLED, 2: NOT_VALID})
        # Control: the exact-direction copy of the very same seam
        # position survives both routers on side B.
        self.assertIn('OBS-3', result_ids(outcome))
        self.assertIn('OBS-3', result_ids(restricted))
        # (c) Side A reads the boundary copy without any deviation.
        side_a = run_query(store, [RECT_A], valid_version=1)
        self.assertIn(('OBS-1', 1), result_pairs(side_a))
        self.assertLess(lng_of(only_row(side_a, 'OBS-1').point),
                        math.radians(45))


if __name__ == '__main__':
    unittest.main()

"""Tests for read-plan comparison, cache re-confirmation, and survey bounds."""

from __future__ import absolute_import, division, print_function
from __future__ import unicode_literals

import math
import unittest

import s2sphere
from s2sphere import Cell, CellId, CellUnion, LatLng, LatLngRect
from s2sphere import RegionCoverer
from s2sphere import (AreaInterval, CacheReconfirmation, ReadPlan,
                      ReadPlanComparison, RegionEvidence, SurveyConstraint,
                      SurveyPiece, compare_read_plans,
                      reconfirm_cached_cells, solve_survey_bounds,
                      survey_from_comparison, tighten_xyz_read_difference)


DATELINE_RECT = LatLngRect.from_point_pair(
    LatLng.from_degrees(-0.02, 179.95),
    LatLng.from_degrees(0.02, -179.95),
)


def covering(region, max_cells, max_level):
    coverer = RegionCoverer()
    coverer.max_cells = max_cells
    coverer.max_level = max_level
    return coverer.get_covering(region)


def interior_covering(region, max_cells, max_level):
    coverer = RegionCoverer()
    coverer.max_cells = max_cells
    coverer.max_level = max_level
    return coverer.get_interior_covering(region)


def cell_area(cell_ids):
    return math.fsum(Cell(cell_id).exact_area() for cell_id in cell_ids)


class TestAreaUnits(unittest.TestCase):

    def testRectAreaMatchesAnalyticValue(self):
        # A rectangle well away from a pole: area = d_lng * (sin hi - sin lo)
        rect = LatLngRect.from_point_pair(
            LatLng.from_degrees(-10, 20),
            LatLng.from_degrees(30, 50),
        )
        delta_lng = math.radians(30)
        expected = delta_lng * (
            math.sin(math.radians(30)) - math.sin(math.radians(-10))
        )
        self.assertAlmostEqual(expected, rect.area(), delta=1e-15)
        # Steradians to square degrees.
        self.assertAlmostEqual(
            rect.area() * (180.0 / math.pi) ** 2,
            math.degrees(delta_lng) * (
                math.sin(math.radians(30)) - math.sin(math.radians(-10))
            ) * (180.0 / math.pi),
            delta=1e-12,
        )

    def testCellUnionAreaIsSumOfCells(self):
        parent = CellId.from_lat_lng(LatLng.from_degrees(0, 0)).parent(6)
        children = list(parent.children())
        union = CellUnion(children)
        self.assertAlmostEqual(
            union.exact_area(),
            cell_area(children),
            delta=1e-15,
        )
        self.assertAlmostEqual(
            union.exact_area(), Cell(parent).exact_area(), delta=1e-15)
        self.assertGreater(union.approx_area(), 0)
        self.assertGreater(union.average_area(), 0)

    def testFaceAreasSumToSphere(self):
        total = math.fsum(
            Cell(CellId.from_face_pos_level(face, 0, 0)).exact_area()
            for face in range(6)
        )
        self.assertAlmostEqual(total, 4 * math.pi, delta=1e-12)


class TestDatelineOverCovering(unittest.TestCase):

    def setUp(self):
        self.coarse = covering(DATELINE_RECT, 16, 14)
        self.coarse_interior = interior_covering(DATELINE_RECT, 16, 14)
        self.evidence = RegionEvidence(
            DATELINE_RECT,
            interior_covering=self.coarse_interior,
        )

    def testOuterCoverIsLargerThanInterior(self):
        coarse = self.evidence.composition(self.coarse)
        interior = self.evidence.composition(self.coarse_interior)
        self.assertGreater(coarse.total_area, DATELINE_RECT.area())
        self.assertLessEqual(interior.total_area, DATELINE_RECT.area())
        self.assertLessEqual(coarse.inside_area, DATELINE_RECT.area())
        self.assertLessEqual(
            coarse.inside_area + coarse.unknown_area,
            coarse.total_area + 1e-15,
        )

    def testEveryCoveringCellIsCovered(self):
        union = CellUnion(self.coarse)
        for cell_id in self.coarse_interior:
            self.assertTrue(union.contains(cell_id))

    def testCoarseBudgetHasNoInteriorCellsButFineBudgetDoes(self):
        coarse_interior = interior_covering(DATELINE_RECT, 8, 12)
        fine_interior = interior_covering(DATELINE_RECT, 128, 20)
        coarse_outer = covering(DATELINE_RECT, 8, 12)
        fine_outer = covering(DATELINE_RECT, 128, 20)

        coarse_evidence = RegionEvidence(
            DATELINE_RECT, interior_covering=coarse_interior)
        fine_evidence = RegionEvidence(
            DATELINE_RECT, interior_covering=fine_interior)

        self.assertEqual(
            0.0, coarse_evidence.composition(coarse_outer).inside_area)
        self.assertEqual(
            0, coarse_evidence.composition(coarse_outer).inside.num_cells())
        self.assertGreater(
            fine_evidence.composition(fine_outer).inside_area, 0)
        self.assertGreater(
            fine_evidence.composition(fine_outer).inside.num_cells(), 0)


class TestReadPlanComparison(unittest.TestCase):

    def setUp(self):
        self.region = DATELINE_RECT
        self.interior = interior_covering(self.region, 128, 20)
        self.evidence = RegionEvidence(self.region,
                                       interior_covering=self.interior)
        self.old_covering = covering(self.region, 16, 14)
        self.new_covering = covering(self.region, 128, 20)

    def testPlanAreasAndInsideLowerBounds(self):
        old = ReadPlan(self.old_covering, evidence=self.evidence)
        new = ReadPlan(self.new_covering, evidence=self.evidence)
        self.assertAlmostEqual(old.read_area,
                               cell_area(self.old_covering), delta=1e-15)
        self.assertAlmostEqual(new.read_area,
                               cell_area(self.new_covering), delta=1e-15)

        comparison = ReadPlanComparison(old, new, self.evidence)
        self.assertLessEqual(comparison.old_inside_lower_bound,
                             old.read_area + 1e-15)
        self.assertLessEqual(comparison.new_inside_lower_bound,
                             new.read_area + 1e-15)
        self.assertTrue(comparison.check())

    def testDifferenceUsesOnlyExclusiveBlocks(self):
        old = ReadPlan(self.old_covering, evidence=self.evidence)
        new = ReadPlan(self.new_covering, evidence=self.evidence)
        comparison = ReadPlanComparison(old, new, self.evidence)
        old_block = comparison.old_only_composition
        new_block = comparison.new_only_composition

        expected = AreaInterval(
            new_block.outside_area - old_block.outside_area -
            old_block.unknown_area,
            new_block.outside_area - old_block.outside_area +
            new_block.unknown_area,
        )
        self.assertEqual(expected, comparison.difference_interval)
        self.assertEqual(expected, comparison.difference_from_components())

    def testCommonUnknownCancelsAndNaiveIsWider(self):
        old = ReadPlan(self.old_covering, evidence=self.evidence)
        new = ReadPlan(self.new_covering, evidence=self.evidence)
        comparison = ReadPlanComparison(old, new, self.evidence)
        # Independent subtraction counts the shared uncertainty twice, so it
        # contains the true interval and is at least as wide.
        self.assertLessEqual(
            comparison.naive_difference_interval.lo,
            comparison.difference_interval.lo + 1e-12,
        )
        self.assertGreaterEqual(
            comparison.naive_difference_interval.hi,
            comparison.difference_interval.hi - 1e-12,
        )
        self.assertGreaterEqual(
            comparison.naive_difference_interval.width,
            comparison.difference_interval.width - 1e-12,
        )

    def testSwapNegatesInterval(self):
        old = ReadPlan(self.old_covering, evidence=self.evidence)
        new = ReadPlan(self.new_covering, evidence=self.evidence)
        comparison = ReadPlanComparison(old, new, self.evidence)
        swapped = comparison.swapped()
        self.assertEqual(comparison.difference_interval,
                         -swapped.difference_interval)
        self.assertEqual(comparison.difference_interval,
                         swapped.reduction_interval)
        self.assertTrue(swapped.check())

    def testMixedParentAndChildRepresentations(self):
        # Give the old plan a parent cell and the new plan its children for
        # the same shared block.  Normalization makes the two CellUnions
        # identical, so the comparison must be unchanged.
        old = ReadPlan(self.old_covering, evidence=self.evidence)
        new = ReadPlan(self.new_covering, evidence=self.evidence)
        base = ReadPlanComparison(old, new, self.evidence)

        parent = CellId.from_lat_lng(
            LatLng.from_degrees(0.0, 179.999)).parent(8)
        children = list(parent.children())
        mixed = ReadPlanComparison(
            ReadPlan(CellUnion([parent]), evidence=self.evidence),
            ReadPlan(CellUnion(children), evidence=self.evidence),
            self.evidence,
        )
        self.assertEqual(0, mixed.old_only.num_cells())
        self.assertEqual(0, mixed.new_only.num_cells())
        self.assertEqual(1, mixed.common.num_cells())
        self.assertEqual(AreaInterval(0.0, 0.0),
                         mixed.difference_interval)
        self.assertTrue(mixed.check())
        # The mixed representation has the same area as the common block.
        self.assertAlmostEqual(
            mixed.common.exact_area(),
            cell_area([parent]),
            delta=1e-15,
        )
        # The original comparison is unaffected by building a second one.
        repeated = ReadPlanComparison(old, new, self.evidence)
        self.assertEqual(base.difference_interval,
                         repeated.difference_interval)
        self.assertEqual(base.old_only, repeated.old_only)
        self.assertEqual(base.new_only, repeated.new_only)

    def testCacheHitsRemoveReadsButNotRegionEvidence(self):
        cached = list(self.old_covering)[:2]
        old = ReadPlan(self.old_covering, cache=cached,
                       evidence=self.evidence)
        new = ReadPlan(self.new_covering, evidence=self.evidence)
        self.assertAlmostEqual(
            old.read_area,
            cell_area(self.old_covering) - cell_area(cached),
            delta=1e-15,
        )
        self.assertAlmostEqual(old.cache_hit_area, cell_area(cached),
                               delta=1e-15)
        # The region model is still the same object.
        self.assertIs(old.evidence, new.evidence)

    def testPlansBuiltWithDifferentEvidenceAreRejected(self):
        other = RegionEvidence(self.region)
        old = ReadPlan(self.old_covering, evidence=other)
        new = ReadPlan(self.new_covering, evidence=self.evidence)
        with self.assertRaises(ValueError):
            ReadPlanComparison(old, new, self.evidence)

    def testCompareHelperAcceptsRawCoverings(self):
        comparison = compare_read_plans(
            self.old_covering, self.new_covering,
            region=self.region,
        )
        self.assertTrue(comparison.check())
        self.assertIs(comparison.old_plan.evidence,
                      comparison.new_plan.evidence)


class TestCacheReconfirmation(unittest.TestCase):

    def setUp(self):
        self.region = LatLngRect.from_point_pair(
            LatLng.from_degrees(-0.02, 179.95),
            LatLng.from_degrees(0.02, -179.95),
        )
        self.previous = covering(self.region, 32, 14)

    def testOnlyReconfirmedCellsCountAsDetermined(self):
        result = reconfirm_cached_cells(
            self.region,
            previous_cache=self.previous,
        )
        previous_area = cell_area(self.previous)
        self.assertLess(result.determined_area, previous_area)
        self.assertGreater(result.partial_area, 0)
        self.assertAlmostEqual(
            result.determined_area + result.partial_area +
            result.outside_area,
            previous_area,
            delta=1e-15,
        )
        self.assertTrue(result.check())

    def testCacheHitSavesReadWithoutProvingInRange(self):
        result = reconfirm_cached_cells(
            self.region,
            previous_cache=self.previous[:1],
            previous_read=self.previous[1:],
        )
        self.assertGreater(result.read_saved_area, result.determined_area)
        for cell_id in result.partial.cell_ids():
            self.assertFalse(self.region.contains(Cell(cell_id)))
        for cell_id in result.confirmed.cell_ids():
            self.assertTrue(self.region.contains(Cell(cell_id)))

    def testExcludedCellsAreOutside(self):
        outside_cell = CellId.from_lat_lng(
            LatLng.from_degrees(30, 30)).parent(12)
        result = CacheReconfirmation(
            self.region, previous_cache=[outside_cell])
        self.assertEqual(1, result.outside.num_cells())
        self.assertEqual(0.0, result.determined_area)
        self.assertAlmostEqual(
            result.outside_area, Cell(outside_cell).exact_area(),
            delta=1e-18)


class TestThreeBlockContract(unittest.TestCase):
    """A large shared block plus one small exclusive block per plan.

    The cells come from either side of the date line, so the region is the
    narrow dateline rectangle above.  Both exclusive blocks contain an
    undecided (unknown) cell.
    """

    def setUp(self):
        self.region = DATELINE_RECT
        interior = interior_covering(self.region, 128, 20)
        self.evidence = RegionEvidence(self.region,
                                       interior_covering=interior)
        self.common_cell = CellId.from_lat_lng(
            LatLng.from_degrees(0.0, 179.99)).parent(11)
        dateline_cell = CellId.from_lat_lng(
            LatLng.from_degrees(0.0, -179.99)).parent(11)
        self.old_only_cell = dateline_cell.child(1)
        self.new_only_cell = dateline_cell.child(2)
        self.old_covering = CellUnion(
            [self.common_cell, self.old_only_cell])
        self.new_covering = CellUnion(
            [self.common_cell, self.new_only_cell])

    def comparison(self, old_covering=None, new_covering=None):
        old_covering = old_covering or self.old_covering
        new_covering = new_covering or self.new_covering
        return ReadPlanComparison(
            ReadPlan(old_covering, evidence=self.evidence),
            ReadPlan(new_covering, evidence=self.evidence),
            self.evidence,
        )

    def testThreeBlocksAreNonEmpty(self):
        comparison = self.comparison()
        self.assertGreater(comparison.common.exact_area(), 0)
        self.assertGreater(comparison.old_only.exact_area(), 0)
        self.assertGreater(comparison.new_only.exact_area(), 0)
        for block in (comparison.common_composition,
                      comparison.old_only_composition,
                      comparison.new_only_composition):
            self.assertGreater(block.unknown_area, 0)
            self.assertEqual(0.0, block.outside_area)
        self.assertTrue(comparison.check())

    def testDifferenceIsReadableFromExclusiveBlocks(self):
        comparison = self.comparison()
        data = comparison.as_dict()
        expected_lo = (data['new_only_outside'] -
                       data['old_only_outside'] -
                       data['old_only_unknown'])
        expected_hi = (data['new_only_outside'] -
                       data['old_only_outside'] +
                       data['new_only_unknown'])
        self.assertAlmostEqual(expected_lo,
                               comparison.difference_interval.lo, delta=1e-18)
        self.assertAlmostEqual(expected_hi,
                               comparison.difference_interval.hi, delta=1e-18)

    def testCommonBlockParentVersusChildrenAndSwap(self):
        base = self.comparison()
        children = list(self.common_cell.children())
        # Old plan uses the parent cell, new plan uses its children.
        swapped_repr = self.comparison(
            old_covering=CellUnion(
                [self.common_cell, self.old_only_cell]),
            new_covering=CellUnion(children + [self.new_only_cell]),
        )
        self.assertEqual(base.common, swapped_repr.common)
        self.assertEqual(base.difference_interval,
                         swapped_repr.difference_interval)
        self.assertTrue(swapped_repr.check())

        swapped = base.swapped()
        self.assertEqual(base.difference_interval,
                         -swapped.difference_interval)
        self.assertEqual(base.difference_interval,
                         swapped.reduction_interval)
        # The representation change is direction-neutral, and swapping it
        # afterwards still negates the original interval.
        self.assertEqual(base.difference_interval,
                         swapped_repr.difference_interval)
        swapped_again = swapped_repr.swapped()
        self.assertEqual(swapped.difference_interval,
                         swapped_again.difference_interval)
        self.assertEqual(base.difference_interval,
                         -swapped_again.difference_interval)

    def testCommonUncertaintyIsNotDoubleCounted(self):
        comparison = self.comparison()
        common_unknown = comparison.common_composition.unknown_area
        # The naive independent subtraction contains the true interval and
        # is wider by at least the shared unknown area on each side.
        self.assertLessEqual(
            comparison.naive_difference_interval.lo,
            comparison.difference_interval.lo - common_unknown + 1e-18)
        self.assertGreaterEqual(
            comparison.naive_difference_interval.hi,
            comparison.difference_interval.hi + common_unknown - 1e-18)


class TestSurveyBounds(unittest.TestCase):

    def testTightestIntervalAndWitnesses(self):
        result = tighten_xyz_read_difference(
            1.0, 2.0, 3.0, (1.5, 2.5), (3.0, 4.5))
        self.assertTrue(result.feasible)
        # new - old = Z - X, with X+Y in [1.5,2.5], Y+Z in [3,4.5].
        self.assertAlmostEqual(result.difference_interval.lo, 0.5)
        self.assertAlmostEqual(result.difference_interval.hi, 3.0)
        self.assertAlmostEqual(result.reduction_interval.lo, -3.0)
        self.assertAlmostEqual(result.reduction_interval.hi, -0.5)
        self.assertTrue(result.check_witness(result.lo_witness, 'lo'))
        self.assertTrue(result.check_witness(result.hi_witness, 'hi'))
        self.assertFalse(result.check_witness(result.lo_witness, 'hi'))
        self.assertAlmostEqual(
            result.witness_difference(result.lo_witness), 0.5)
        self.assertAlmostEqual(
            result.witness_difference(result.hi_witness), 3.0)

        for allocation in (result.lo_witness, result.hi_witness):
            x = allocation['X']
            y = allocation['Y']
            z = allocation['Z']
            self.assertGreaterEqual(x, -1e-12)
            self.assertLessEqual(x, 1.0 + 1e-12)
            self.assertGreaterEqual(y, -1e-12)
            self.assertLessEqual(y, 2.0 + 1e-12)
            self.assertGreaterEqual(z, -1e-12)
            self.assertLessEqual(z, 3.0 + 1e-12)
            self.assertGreaterEqual(x + y, 1.5 - 1e-12)
            self.assertLessEqual(x + y, 2.5 + 1e-12)
            self.assertGreaterEqual(y + z, 3.0 - 1e-12)
            self.assertLessEqual(y + z, 4.5 + 1e-12)

    def testEachBoundaryButJointlyImpossible(self):
        # X <= 1 and Y <= 2 allow X+Y <= 3, and Y <= 2 with Z <= 3 allows
        # Y+Z <= 5.  Both readings are individually satisfiable, but
        # X+Y >= 2.5 and Y+Z <= 0.5 force X >= 2, which is impossible.
        result = tighten_xyz_read_difference(
            1.0, 2.0, 3.0, (2.5, 3.0), (0.0, 0.5))
        self.assertFalse(result.feasible)
        self.assertIn('jointly', result.reason)
        self.assertIsNotNone(result.bounds_only_difference_interval)

    def testSingleReadingsAreFeasibleSeparately(self):
        x_area, y_area, z_area = 3.0, 2.0, 3.0
        xy_interval = (2.5, 3.0)
        yz_interval = (1.5, 2.0)
        for interval, coefficients in (
                (xy_interval, {'X': 1.0, 'Y': 1.0}),
                (yz_interval, {'Y': 1.0, 'Z': 1.0})):
            pieces = [
                SurveyPiece('X', x_area, s2sphere.OLD_ONLY),
                SurveyPiece('Y', y_area, s2sphere.COMMON),
                SurveyPiece('Z', z_area, s2sphere.NEW_ONLY),
            ]
            single = solve_survey_bounds(
                pieces,
                [SurveyConstraint(coefficients, interval[0], interval[1])],
            )
            self.assertTrue(single.feasible)

    def testTogetherReadingsTightenTheDifference(self):
        x_area, y_area, z_area = 2.0, 2.0, 2.0
        xy_interval = (1.5, 2.0)
        yz_interval = (1.5, 2.0)
        result = tighten_xyz_read_difference(
            x_area, y_area, z_area, xy_interval, yz_interval)
        self.assertTrue(result.feasible)
        bounds_only = result.bounds_only_difference_interval
        individual = result.individual_difference_intervals
        # Neither reading alone narrows the unconstrained interval...
        self.assertEqual(bounds_only, individual[0])
        self.assertEqual(bounds_only, individual[1])
        # ...but together they halve it.
        self.assertEqual(AreaInterval(-0.5, 0.5),
                         result.difference_interval)
        self.assertTrue(result.check_witness(result.lo_witness))
        self.assertTrue(result.check_witness(result.hi_witness))

    def testBoundaryValuesIndividuallyFeasibleButJointlyNot(self):
        # Each reading keeps every piece within [0, piece.area], yet
        # X + Y >= 2.5 together with Y + Z <= 0.5 would need X >= 2 > 1.
        result = tighten_xyz_read_difference(
            1.0, 2.0, 3.0, (2.5, 3.0), (0.0, 0.5))
        self.assertFalse(result.feasible)
        for lo, hi, cap in ((2.5, 3.0, 3.0), (0.0, 0.5, 5.0)):
            self.assertLessEqual(lo, hi)
            self.assertLessEqual(hi, cap)

    def testNeighbouringOverlapIntervalsMeetAtABoundary(self):
        result = tighten_xyz_read_difference(
            1.0, 2.0, 3.0, (1.8, 2.5), (2.6, 3.0))
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.difference_interval.lo, 0.1, delta=1e-12)
        self.assertAlmostEqual(result.difference_interval.hi, 1.2, delta=1e-12)
        self.assertTrue(result.check_witness(result.lo_witness, 'lo'))
        self.assertTrue(result.check_witness(result.hi_witness, 'hi'))

    def testFourSurveysOverDisjointPieces(self):
        pieces = [
            SurveyPiece('A', 2.0, s2sphere.OLD_ONLY),
            SurveyPiece('B', 1.0, s2sphere.COMMON),
            SurveyPiece('C', 2.0, s2sphere.NEW_ONLY),
            SurveyPiece('D', 1.0, s2sphere.COMMON),
        ]
        constraints = [
            SurveyConstraint({'A': 1.0, 'B': 1.0}, 0.5, 2.0),
            SurveyConstraint({'B': 1.0, 'C': 1.0}, 0.0, 2.5),
            SurveyConstraint({'C': 1.0, 'D': 1.0}, 1.0, 2.5),
            SurveyConstraint({'A': 1.0, 'D': 1.0}, 0.0, 2.0),
        ]
        result = solve_survey_bounds(pieces, constraints)
        self.assertTrue(result.feasible)
        self.assertTrue(result.check_witness(result.lo_witness, 'lo'))
        self.assertTrue(result.check_witness(result.hi_witness, 'hi'))

    def testSurveyFromComparisonUsesSameEvidence(self):
        region = DATELINE_RECT
        interior = interior_covering(region, 128, 20)
        evidence = RegionEvidence(region, interior_covering=interior)
        old = ReadPlan(covering(region, 16, 14), evidence=evidence)
        new = ReadPlan(covering(region, 128, 20), evidence=evidence)
        comparison = ReadPlanComparison(old, new, evidence)
        result = survey_from_comparison(
            comparison, (0.0, 1e-6), (0.0, 1e-6))
        self.assertIs(comparison.evidence, evidence)
        self.assertIs(comparison.old_plan.evidence, evidence)
        self.assertIs(comparison.new_plan.evidence, evidence)
        self.assertTrue(result.feasible or not result.feasible)


if __name__ == '__main__':
    unittest.main()

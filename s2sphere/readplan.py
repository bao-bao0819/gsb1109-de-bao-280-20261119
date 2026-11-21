"""Read-plan comparison helpers for S2 cell coverings.

This module adds the bookkeeping needed to compare two disk read plans over
the same region.  A read plan is an outer covering of a region minus the cells
that are already available in a cache.  All areas returned by this module are
spherical areas in **steradians** on the unit sphere.  One steradian equals
``(180 / pi) ** 2`` square degrees.  Intervals are closed, are measured in
steradians, and are compared with an explicit absolute tolerance
(:data:`DEFAULT_TOLERANCE`, ``1e-12`` steradians by default).

Three geometric states are distinguished for every cell:

* :data:`INSIDE`: the cell is contained in the region (or in an interior
  covering supplied as part of the shared region evidence),
* :data:`OUTSIDE`: the region cannot intersect the cell,
* :data:`UNKNOWN`: the cell straddles the boundary and its in-region area is
  not decided.

Only :data:`UNKNOWN` cells carry uncertainty.  When two plans share a
physical block, that block's uncertainty is the same in both plans and
cancels from a difference.  It must not be counted once per plan; the
``naive_difference_interval`` in :class:`ReadPlanComparison` is provided only
to show the extra width that independent subtraction creates.
"""

from __future__ import absolute_import, division, print_function
from __future__ import unicode_literals

import itertools
import math

from .sphere import Cell, CellUnion


DEFAULT_TOLERANCE = 1e-12

INSIDE = 'inside'
OUTSIDE = 'outside'
UNKNOWN = 'unknown'

OLD_ONLY = 'old_only'
COMMON = 'common'
NEW_ONLY = 'new_only'

ROLES = (OLD_ONLY, COMMON, NEW_ONLY)


class AreaInterval(object):
    """A closed interval of spherical area, measured in steradians.

    ``lo`` and ``hi`` are inclusive.  Two intervals are equal when both
    endpoints agree to within :attr:`tolerance`; the tolerance is absolute
    and has units of steradians.
    """

    def __init__(self, lo, hi, tolerance=DEFAULT_TOLERANCE):
        if lo > hi + tolerance:
            raise ValueError(
                'interval lower bound {} exceeds upper bound {}'.format(lo, hi)
            )
        self._lo = min(lo, hi)
        self._hi = max(lo, hi)
        self._tolerance = tolerance

    @property
    def lo(self):
        return self._lo

    @property
    def hi(self):
        return self._hi

    @property
    def tolerance(self):
        return self._tolerance

    @property
    def width(self):
        return self._hi - self._lo

    def __neg__(self):
        return AreaInterval(-self._hi, -self._lo, self._tolerance)

    def __add__(self, other):
        tolerance = max(self._tolerance, other._tolerance)
        return AreaInterval(self._lo + other._lo,
                            self._hi + other._hi,
                            tolerance)

    def __sub__(self, other):
        return self + (-other)

    def contains(self, value):
        return (self._lo - self._tolerance <= value <=
                self._hi + self._tolerance)

    def __eq__(self, other):
        return (isinstance(other, AreaInterval) and
                abs(self._lo - other._lo) <= self._tolerance and
                abs(self._hi - other._hi) <= self._tolerance)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return 'AreaInterval({!r}, {!r})'.format(self._lo, self._hi)


def _cell_area(cell_id):
    return Cell(cell_id).exact_area()


def _union_area(cell_ids):
    return math.fsum(_cell_area(cell_id) for cell_id in cell_ids)


def _describe_union(union):
    return {
        'cell_ids': [cell_id.id() for cell_id in union.cell_ids()],
        'tokens': [cell_id.to_token() for cell_id in union.cell_ids()],
        'num_cells': union.num_cells(),
        'area': _union_area(union.cell_ids()),
    }


class RegionComposition(object):
    """Disjoint cells split by their geometric relation to a region.

    :param inside: cells proved to lie inside the region
    :param outside: cells proved to lie outside the region
    :param unknown: cells that may intersect the region boundary

    ``inside_area`` is a lower bound on the part of this composition that is
    in the region; ``inside_area + unknown_area`` is the corresponding upper
    bound.  Likewise ``outside_area`` is a lower bound on the part outside
    the region.
    """

    def __init__(self, inside, outside, unknown, tolerance=DEFAULT_TOLERANCE):
        self.inside = CellUnion(inside)
        self.outside = CellUnion(outside)
        self.unknown = CellUnion(unknown)
        self.tolerance = tolerance
        self.inside_area = _union_area(self.inside.cell_ids())
        self.outside_area = _union_area(self.outside.cell_ids())
        self.unknown_area = _union_area(self.unknown.cell_ids())

    @property
    def total_area(self):
        return self.inside_area + self.outside_area + self.unknown_area

    @property
    def inside_bounds(self):
        """Bounds on the part of this composition lying in the region."""
        return AreaInterval(self.inside_area,
                            self.inside_area + self.unknown_area,
                            self.tolerance)

    @property
    def outside_bounds(self):
        """Bounds on the part of this composition lying outside the region."""
        return AreaInterval(self.outside_area,
                            self.outside_area + self.unknown_area,
                            self.tolerance)

    @property
    def inside_lower_bound(self):
        return self.inside_area

    @property
    def outside_lower_bound(self):
        return self.outside_area

    def as_dict(self):
        return {
            'inside': _describe_union(self.inside),
            'outside': _describe_union(self.outside),
            'unknown': _describe_union(self.unknown),
            'inside_area': self.inside_area,
            'outside_area': self.outside_area,
            'unknown_area': self.unknown_area,
            'total_area': self.total_area,
            'inside_bounds': self.inside_bounds,
            'outside_bounds': self.outside_bounds,
            'unit': 'steradians',
            'tolerance': self.tolerance,
        }

    def check(self):
        """True when the three groups are disjoint and exhaust the union."""
        pieces = (self.inside, self.outside, self.unknown)
        for index, first in enumerate(pieces):
            for second in pieces[index + 1:]:
                if first.intersects(second):
                    return False
        return True


class RegionEvidence(object):
    """The one geometric truth shared by every plan in a comparison.

    Create one :class:`RegionEvidence` for a region and pass that same object
    to all plans and comparisons.  Cache hits are bookkeeping facts about
    what was read; they do not change what is inside the region, so they must
    not create a second, plan-specific region model.
    """

    def __init__(self, region, interior_covering=None,
                 tolerance=DEFAULT_TOLERANCE):
        self.region = region
        self.tolerance = tolerance
        if interior_covering is None:
            interior_covering = []
        self.interior_covering = _as_cell_union(interior_covering)
        self._classification = {}

    def classify_cell(self, cell_id):
        key = cell_id.id()
        cached = self._classification.get(key)
        if cached is not None:
            return cached

        if self.interior_covering.contains(cell_id):
            result = INSIDE
        else:
            cell = Cell(cell_id)
            if not self.region.may_intersect(cell):
                result = OUTSIDE
            elif self.region.contains(cell):
                result = INSIDE
            else:
                result = UNKNOWN
        self._classification[key] = result
        return result

    def composition(self, cell_union):
        """Split a disjoint cell union into inside/outside/unknown cells."""
        cell_union = _as_cell_union(cell_union)

        inside = []
        outside = []
        unknown = []
        for cell_id in cell_union.cell_ids():
            classification = self.classify_cell(cell_id)
            if classification == INSIDE:
                inside.append(cell_id)
            elif classification == OUTSIDE:
                outside.append(cell_id)
            else:
                unknown.append(cell_id)

        return RegionComposition(inside, outside, unknown,
                                 tolerance=self.tolerance)


def _as_cell_union(cell_ids):
    if isinstance(cell_ids, CellUnion):
        return CellUnion(cell_ids.cell_ids())
    return CellUnion(cell_ids)


class ReadPlan(object):
    """An outer covering after cache hits have removed cells from the read.

    :param covering: the plan's outer covering of the region
    :param cache: cells already available from cache
    :param evidence: shared :class:`RegionEvidence`, if known
    :param label: optional human-readable plan name
    """

    def __init__(self, covering, cache=None, evidence=None, label=''):
        self.covering = _as_cell_union(covering)
        self.cache = _as_cell_union([] if cache is None else cache)
        self.read = CellUnion.get_difference(self.covering, self.cache)
        self.cache_hit = CellUnion.get_intersection(self.covering,
                                                    self.cache)
        self.evidence = evidence
        self.label = label

    @property
    def read_area(self):
        """Exact spherical area actually read from disk, in steradians."""
        return self.read.exact_area()

    @property
    def cache_hit_area(self):
        """Covering area avoided because of cache hits, in steradians."""
        return self.cache_hit.exact_area()

    @property
    def covering_area(self):
        return self.covering.exact_area()

    def composition(self, evidence=None):
        evidence = evidence if evidence is not None else self.evidence
        if evidence is None:
            raise ValueError('RegionEvidence is required for classification')
        return evidence.composition(self.read)

    def as_dict(self):
        return {
            'label': self.label,
            'covering_area': self.covering_area,
            'cache_hit_area': self.cache_hit_area,
            'read_area': self.read_area,
            'unit': 'steradians',
        }


class ReadPlanComparison(object):
    """Comparison of two :class:`ReadPlan` objects over one region.

    The exclusive blocks ``old_only`` and ``new_only`` are the only blocks
    that can change the read difference.  The shared block ``common`` adds an
    identical physical uncertainty to both plans, so it cancels exactly and
    is deliberately excluded from :attr:`difference_interval`.

    With ``D = new_irrelevant - old_irrelevant``::

        D_lo = new_only_outside - old_only_outside - old_only_unknown
        D_hi = new_only_outside - old_only_outside + new_only_unknown

    All four terms are read directly from the exclusive-block compositions.
    """

    def __init__(self, old_plan, new_plan, evidence,
                 tolerance=DEFAULT_TOLERANCE):
        if not isinstance(old_plan, ReadPlan):
            old_plan = ReadPlan(old_plan)
        if not isinstance(new_plan, ReadPlan):
            new_plan = ReadPlan(new_plan)
        if old_plan.evidence is not None and \
                old_plan.evidence is not evidence:
            raise ValueError('old plan was built with different evidence')
        if new_plan.evidence is not None and \
                new_plan.evidence is not evidence:
            raise ValueError('new plan was built with different evidence')

        old_plan.evidence = evidence
        new_plan.evidence = evidence

        self.old_plan = old_plan
        self.new_plan = new_plan
        self.evidence = evidence
        self.tolerance = tolerance

        self.old_read = old_plan.read
        self.new_read = new_plan.read
        self.common = CellUnion.get_intersection(self.old_read,
                                                 self.new_read)
        self.old_only = CellUnion.get_difference(self.old_read,
                                                 self.new_read)
        self.new_only = CellUnion.get_difference(self.new_read,
                                                 self.old_read)

        self.common_composition = evidence.composition(self.common)
        self.old_only_composition = evidence.composition(self.old_only)
        self.new_only_composition = evidence.composition(self.new_only)

        self.difference_interval = self.difference_from_components()
        # old - new: how much less irrelevant area the new plan reads.
        self.reduction_interval = -self.difference_interval

        # Independent interval subtraction, diagnostics only.  It counts the
        # shared block's uncertainty once in each plan instead of cancelling
        # it, so its interval is at least as wide as the true one.  Because
        # the two plans may represent the shared block at different cell
        # levels, the extra width is not in general exactly the shared
        # unknown area.
        self.naive_difference_interval = (
            self.new_read_composition.outside_bounds -
            self.old_read_composition.outside_bounds
        )

    @property
    def old_read_composition(self):
        return self.evidence.composition(self.old_read)

    @property
    def new_read_composition(self):
        return self.evidence.composition(self.new_read)

    @property
    def old_irrelevant_bounds(self):
        return self.old_read_composition.outside_bounds

    @property
    def new_irrelevant_bounds(self):
        return self.new_read_composition.outside_bounds

    @property
    def old_inside_lower_bound(self):
        """Area of old read that is certainly inside the region."""
        return self.old_read_composition.inside_lower_bound

    @property
    def new_inside_lower_bound(self):
        """Area of new read that is certainly inside the region."""
        return self.new_read_composition.inside_lower_bound

    def difference_from_components(self):
        """Recompute the difference interval from exclusive blocks only."""
        old = self.old_only_composition
        new = self.new_only_composition
        return AreaInterval(
            new.outside_area - old.outside_area - old.unknown_area,
            new.outside_area - old.outside_area + new.unknown_area,
            self.tolerance,
        )

    def components(self):
        """Verifiable breakdown of the exclusive and common blocks."""
        return {
            'old_only': self.old_only_composition.as_dict(),
            'new_only': self.new_only_composition.as_dict(),
            'common': self.common_composition.as_dict(),
            'difference_interval': self.difference_interval,
            'reduction_interval': self.reduction_interval,
        }

    def check(self):
        """Verify the interval against the exclusive blocks."""
        if not (self.common_composition.check() and
                self.old_only_composition.check() and
                self.new_only_composition.check()):
            return False
        if self.difference_interval != self.difference_from_components():
            return False
        if self.difference_interval != -self.reduction_interval:
            return False
        reconstructed_old = CellUnion.get_union(self.old_only, self.common)
        reconstructed_new = CellUnion.get_union(self.new_only, self.common)
        return (reconstructed_old == self.old_read and
                reconstructed_new == self.new_read)

    def swapped(self):
        """Compare the plans in the opposite order."""
        return ReadPlanComparison(self.new_plan, self.old_plan,
                                  self.evidence, self.tolerance)

    def as_dict(self):
        return {
            'old_read_area': self.old_read.exact_area(),
            'new_read_area': self.new_read.exact_area(),
            'old_inside_lower_bound': self.old_inside_lower_bound,
            'new_inside_lower_bound': self.new_inside_lower_bound,
            'old_irrelevant_bounds': self.old_irrelevant_bounds,
            'new_irrelevant_bounds': self.new_irrelevant_bounds,
            'common_area': self.common.exact_area(),
            'old_only_area': self.old_only.exact_area(),
            'new_only_area': self.new_only.exact_area(),
            'old_only_inside': self.old_only_composition.inside_area,
            'old_only_outside': self.old_only_composition.outside_area,
            'old_only_unknown': self.old_only_composition.unknown_area,
            'new_only_inside': self.new_only_composition.inside_area,
            'new_only_outside': self.new_only_composition.outside_area,
            'new_only_unknown': self.new_only_composition.unknown_area,
            'common_inside': self.common_composition.inside_area,
            'common_outside': self.common_composition.outside_area,
            'common_unknown': self.common_composition.unknown_area,
            'difference_interval': self.difference_interval,
            'reduction_interval': self.reduction_interval,
            'naive_difference_interval': self.naive_difference_interval,
            'unit': 'steradians',
            'tolerance': self.tolerance,
        }


def compare_read_plans(old, new, evidence=None, region=None,
                       old_cache=None, new_cache=None,
                       tolerance=DEFAULT_TOLERANCE, labels=('old', 'new')):
    """Compare two read plans.

    ``old`` and ``new`` may be :class:`ReadPlan` instances or coverings.  If
    they are coverings, ``old_cache`` and ``new_cache`` describe each plan's
    cache cells.  ``evidence`` is the one shared geometric model; if omitted,
    ``region`` is required and a fresh evidence object is created and used by
    both plans.
    """
    if evidence is None:
        if region is None:
            raise ValueError('evidence or region is required')
        evidence = RegionEvidence(region, tolerance=tolerance)

    if not isinstance(old, ReadPlan):
        old = ReadPlan(old, cache=old_cache, evidence=evidence,
                       label=labels[0])
    if not isinstance(new, ReadPlan):
        new = ReadPlan(new, cache=new_cache, evidence=evidence,
                       label=labels[1])

    return ReadPlanComparison(old, new, evidence, tolerance=tolerance)


class CacheReconfirmation(object):
    """Re-confirm cached/read cells against the current region.

    A cache hit only means "no disk read is needed".  It does not prove that
    the cached cell is inside the current measurement range.  Only a cell
    wholly contained in the current region contributes to
    :attr:`determined_area`; a cell that straddles the current boundary is
    reported as partial and contributes only to the upper bounds.
    """

    def __init__(self, region, previous_cache=(), previous_read=(),
                 tolerance=DEFAULT_TOLERANCE):
        self.region = region
        self.tolerance = tolerance
        previous = _as_cell_union(
            list(previous_cache) + list(previous_read))
        self.previous = previous

        confirmed = []
        partial = []
        outside = []
        for cell_id in previous.cell_ids():
            cell = Cell(cell_id)
            if region.contains(cell):
                confirmed.append(cell_id)
            elif not region.may_intersect(cell):
                outside.append(cell_id)
            else:
                partial.append(cell_id)

        self.confirmed = CellUnion(confirmed)
        self.partial = CellUnion(partial)
        self.outside = CellUnion(outside)

        self.determined_area = _union_area(self.confirmed.cell_ids())
        self.partial_area = _union_area(self.partial.cell_ids())
        self.outside_area = _union_area(self.outside.cell_ids())

        # A cache hit avoids a read for every previous cell that reaches the
        # current range.  The whole cell area is saved, even when only part
        # of it is in the current measurement range.
        self.read_saved_area = self.determined_area + self.partial_area

    @property
    def partial_upper_bound(self):
        """The most partial cells could contribute if fully in range."""
        return self.partial_area

    @property
    def determined_upper_bound(self):
        """The most the current cache evidence could determine in range."""
        return self.determined_area + self.partial_area

    def composition(self):
        return {
            'confirmed': _describe_union(self.confirmed),
            'partial': _describe_union(self.partial),
            'outside': _describe_union(self.outside),
            'determined_area': self.determined_area,
            'partial_area': self.partial_area,
            'outside_area': self.outside_area,
            'determined_upper_bound': self.determined_upper_bound,
            'read_saved_area': self.read_saved_area,
            'unit': 'steradians',
            'tolerance': self.tolerance,
        }

    def check(self):
        """Verify disjointness, in-range proof, and area conservation."""
        if self.confirmed.intersects(self.partial) or \
                self.confirmed.intersects(self.outside) or \
                self.partial.intersects(self.outside):
            return False
        for cell_id in self.confirmed.cell_ids():
            if not self.region.contains(Cell(cell_id)):
                return False
        total = (self.determined_area + self.partial_area +
                 self.outside_area)
        return abs(total - self.previous.exact_area()) <= self.tolerance


def reconfirm_cached_cells(region, previous_cache=(), previous_read=(),
                           tolerance=DEFAULT_TOLERANCE):
    return CacheReconfirmation(region, previous_cache, previous_read,
                               tolerance=tolerance)


class SurveyPiece(object):
    """A disjoint piece of undetermined read area.

    ``area`` is the total spherical area of the piece.  ``role`` says where
    the piece sits: only in the old read, only in the new read, or in the
    common read.  The allocation variable for a piece is its confirmed-inside
    area and lies in ``[0, area]``.
    """

    def __init__(self, name, area, role=COMMON):
        if role not in ROLES:
            raise ValueError('unknown survey role: {}'.format(role))
        if area < 0:
            raise ValueError('piece area must be non-negative')
        self.name = name
        self.area = area
        self.role = role


class SurveyConstraint(object):
    """A surveyed interval on a linear combination of piece areas."""

    def __init__(self, coefficients, lo, hi, tolerance=DEFAULT_TOLERANCE):
        if lo > hi + tolerance:
            raise ValueError('survey lower bound exceeds upper bound')
        self.coefficients = dict(coefficients)
        self.lo = lo
        self.hi = hi
        self.tolerance = tolerance


class SurveyResult(object):
    """Tight bounds on a read difference explained by survey observations.

    ``difference_interval`` bounds ``new_irrelevant - old_irrelevant``;
    ``reduction_interval`` bounds ``old_irrelevant - new_irrelevant``.
    ``lo_witness`` and ``hi_witness`` are allocations of confirmed-inside
    area to every piece that attain the two endpoints.
    """

    def __init__(self, feasible, difference_interval=None,
                 lo_witness=None, hi_witness=None,
                 bounds_only_difference_interval=None,
                 individual_difference_intervals=None,
                 reason='', tolerance=DEFAULT_TOLERANCE,
                 constant=0.0, objective=None, pieces=None,
                 constraints=None):
        self.feasible = feasible
        self.difference_interval = difference_interval
        self.reduction_interval = (None if difference_interval is None
                                   else -difference_interval)
        self.lo_witness = lo_witness
        self.hi_witness = hi_witness
        self.bounds_only_difference_interval = \
            bounds_only_difference_interval
        self.individual_difference_intervals = \
            list(individual_difference_intervals or [])
        self.reason = reason
        self.tolerance = tolerance
        self.constant = constant
        self.objective = dict(objective or {})
        self.pieces = dict(pieces or {})
        self.constraints = list(constraints or [])

    def objective_value(self, allocation):
        """Evaluate the new-minus-old read difference for an allocation."""
        return self.constant + math.fsum(
            coefficient * allocation[name]
            for name, coefficient in self.objective.items()
        )

    def allocation_is_feasible(self, allocation):
        """Check piece bounds and every survey constraint for an allocation."""
        scale = max([1.0] + [piece.area for piece in self.pieces.values()])
        check_tolerance = max(self.tolerance, 1e-10 * scale)
        for name, piece in self.pieces.items():
            value = allocation.get(name)
            if value is None:
                return False
            if value < -check_tolerance or \
                    value > piece.area + check_tolerance:
                return False
        for coefficients, lo, hi in self.constraints:
            value = math.fsum(
                coefficient * allocation[name]
                for name, coefficient in coefficients.items()
            )
            if value < lo - check_tolerance or value > hi + check_tolerance:
                return False
        return True

    def check_witness(self, allocation, expected=None):
        """Verify an allocation is feasible and matches an endpoint.

        ``expected`` may be ``'lo'`` or ``'hi'`` to require that the
        allocation attains that endpoint of :attr:`difference_interval`.
        """
        if expected is not None and expected not in ('lo', 'hi'):
            raise ValueError('expected must be None, "lo", or "hi"')
        if not self.feasible:
            return False
        if not self.allocation_is_feasible(allocation):
            return False
        value = self.objective_value(allocation)
        if expected is None:
            return (self.difference_interval.contains(value) or
                    abs(value - self.difference_interval.lo) <=
                    self.tolerance or
                    abs(value - self.difference_interval.hi) <=
                    self.tolerance)
        if expected == 'lo':
            return abs(value - self.difference_interval.lo) <= self.tolerance
        return abs(value - self.difference_interval.hi) <= self.tolerance

    def witness_difference(self, allocation):
        return self.objective_value(allocation)

    def as_dict(self):
        return {
            'feasible': self.feasible,
            'difference_interval': self.difference_interval,
            'reduction_interval': self.reduction_interval,
            'lo_witness': self.lo_witness,
            'hi_witness': self.hi_witness,
            'bounds_only_difference_interval':
                self.bounds_only_difference_interval,
            'individual_difference_intervals':
                self.individual_difference_intervals,
            'reason': self.reason,
            'unit': 'steradians',
            'tolerance': self.tolerance,
        }


def _solve_linear_system(matrix, rhs, tolerance):
    """Solve a square system with partial pivoting; None if singular."""
    size = len(matrix)
    augmented = [list(matrix[row]) + [rhs[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size),
                    key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= tolerance:
            return None
        if pivot != column:
            augmented[column], augmented[pivot] = \
                augmented[pivot], augmented[column]
        scale = augmented[column][column]
        for row in range(column + 1, size):
            factor = augmented[row][column] / scale
            if factor == 0:
                continue
            for k in range(column, size + 1):
                augmented[row][k] -= factor * augmented[column][k]

    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        total = augmented[row][size]
        for column in range(row + 1, size):
            total -= augmented[row][column] * solution[column]
        solution[row] = total / augmented[row][row]
    return solution


def _polytope_vertices(names, constraints, caps, tolerance):
    """Enumerate the vertices of a bounded allocation polytope."""
    inequalities = []

    for name in names:
        row = dict((other, 1.0 if other == name else 0.0)
                   for other in names)
        inequalities.append((row, caps[name]))
        negative = dict((other, -1.0 if other == name else 0.0)
                        for other in names)
        inequalities.append((negative, 0.0))

    for coefficients, lo, hi in constraints:
        row = dict((name, float(coefficients.get(name, 0.0)))
                   for name in names)
        inequalities.append((row, hi))
        inequalities.append((dict((name, -value)
                                  for name, value in row.items()), -lo))

    scale = max([1.0] + [abs(cap) for cap in caps.values()] +
                [abs(bound) for _, bound in inequalities])
    check_tolerance = max(tolerance, 1e-10 * scale)
    epsilon = 1e-9 * scale

    vertices = []
    for combination in itertools.combinations(range(len(inequalities)),
                                               len(names)):
        matrix = [inequalities[index][0] for index in combination]
        rhs = [inequalities[index][1] for index in combination]
        solution = _solve_linear_system(
            [[row[name] for name in names] for row in matrix],
            rhs, check_tolerance)
        if solution is None:
            continue
        feasible = True
        for row, bound in inequalities:
            value = math.fsum(row[name] * solution[position]
                              for position, name in enumerate(names))
            if value > bound + check_tolerance:
                feasible = False
                break
        if feasible:
            solution = [
                0.0 if abs(value) <= epsilon else value
                for value in solution
            ]
            if not any(max(abs(a - b) for a, b in zip(solution, other)) <=
                       check_tolerance for other in vertices):
                vertices.append(solution)
    return vertices, check_tolerance


def _range_over_vertices(names, constraints, caps, objective, tolerance):
    vertices, check_tolerance = _polytope_vertices(
        names, constraints, caps, tolerance)
    if not vertices:
        return None

    values = [
        math.fsum(objective.get(name, 0.0) * solution[position]
                  for position, name in enumerate(names))
        for solution in vertices
    ]
    low_index = min(range(len(values)), key=values.__getitem__)
    high_index = max(range(len(values)), key=values.__getitem__)
    return {
        'low_allocation': dict(zip(names, vertices[low_index])),
        'low_value': values[low_index],
        'high_allocation': dict(zip(names, vertices[high_index])),
        'high_value': values[high_index],
        'check_tolerance': check_tolerance,
    }


def solve_survey_bounds(pieces, constraints, outside_baseline=0.0,
                        tolerance=DEFAULT_TOLERANCE):
    """Find tight read-difference bounds over all feasible allocations.

    ``pieces`` is a sequence of :class:`SurveyPiece`.  Confirmed-inside area
    in an old-only piece adds to the old irrelevant read, confirmed-inside
    area in a new-only piece adds to the new irrelevant read, and the common
    piece contributes to neither difference (it cancels).  Hence::

        new - old = (new_outside - old_outside)
                    + (new-only confirmed-inside)
                    - (old-only confirmed-inside)

    Every allocation gives each piece a confirmed-inside area in
    ``[0, piece.area]`` and must satisfy every survey constraint.  The
    feasible set is a bounded polytope and the objective is linear, so the
    extrema occur at vertices; vertex enumeration therefore yields the
    tightest possible bounds, and the endpoint vertices are returned as
    verifiable allocations.
    """
    names = [piece.name for piece in pieces]
    if len(set(names)) != len(names):
        raise ValueError('piece names must be unique')
    piece_map = dict((piece.name, piece) for piece in pieces)
    caps = dict((piece.name, piece.area) for piece in pieces)

    rows = []
    for constraint in constraints:
        for name in constraint.coefficients:
            if name not in piece_map:
                raise ValueError(
                    'constraint names unknown piece: {}'.format(name))
        rows.append((dict(constraint.coefficients),
                     constraint.lo, constraint.hi))

    objective = {}
    constant = outside_baseline
    for piece in pieces:
        if piece.role == OLD_ONLY:
            objective[piece.name] = -1.0
        elif piece.role == NEW_ONLY:
            objective[piece.name] = 1.0
        else:
            objective[piece.name] = 0.0

    bounds_only = _range_over_vertices(
        names, [], caps, objective, tolerance)
    if bounds_only is None:
        return SurveyResult(
            False, reason='allocation bounds are empty',
            tolerance=tolerance, constant=constant, objective=objective,
            pieces=piece_map, constraints=rows)

    bounds_only_interval = AreaInterval(
        constant + bounds_only['low_value'],
        constant + bounds_only['high_value'],
        tolerance)

    joint = _range_over_vertices(names, rows, caps, objective, tolerance)
    if joint is None:
        return SurveyResult(
            False,
            bounds_only_difference_interval=bounds_only_interval,
            reason='no allocation satisfies all surveys jointly',
            tolerance=tolerance, constant=constant, objective=objective,
            pieces=piece_map, constraints=rows)

    difference_interval = AreaInterval(
        constant + joint['low_value'],
        constant + joint['high_value'],
        tolerance)

    individual = []
    for coefficients, lo, hi in rows:
        single = _range_over_vertices(
            names, [(coefficients, lo, hi)], caps, objective, tolerance)
        if single is None:
            individual.append(None)
        else:
            individual.append(AreaInterval(
                constant + single['low_value'],
                constant + single['high_value'],
                tolerance))

    return SurveyResult(
        True,
        difference_interval=difference_interval,
        lo_witness=joint['low_allocation'],
        hi_witness=joint['high_allocation'],
        bounds_only_difference_interval=bounds_only_interval,
        individual_difference_intervals=individual,
        tolerance=tolerance,
        constant=constant,
        objective=objective,
        pieces=piece_map,
        constraints=rows,
    )


def tighten_xyz_read_difference(x_area, y_area, z_area, xy_interval,
                                yz_interval, old_only_outside=0.0,
                                new_only_outside=0.0,
                                tolerance=DEFAULT_TOLERANCE):
    """Tighten a read difference using X+Y and Y+Z survey intervals.

    X is the undetermined piece of the old-only block, Y is the undetermined
    piece of the common block, and Z is the undetermined piece of the
    new-only block.  ``xy_interval`` and ``yz_interval`` are ``(lo, hi)``
    confirmed-area readings for the two overlapping surveys.  The returned
    difference is new-minus-old irrelevant read area, and
    ``reduction_interval`` is old-minus-new.
    """
    pieces = [
        SurveyPiece('X', x_area, OLD_ONLY),
        SurveyPiece('Y', y_area, COMMON),
        SurveyPiece('Z', z_area, NEW_ONLY),
    ]
    constraints = [
        SurveyConstraint({'X': 1.0, 'Y': 1.0}, xy_interval[0],
                         xy_interval[1], tolerance),
        SurveyConstraint({'Y': 1.0, 'Z': 1.0}, yz_interval[0],
                         yz_interval[1], tolerance),
    ]
    return solve_survey_bounds(
        pieces, constraints,
        outside_baseline=new_only_outside - old_only_outside,
        tolerance=tolerance,
    )


def survey_from_comparison(comparison, xy_interval, yz_interval,
                           tolerance=None):
    """Use a comparison's exclusive/common unknown pieces as X, Y, Z.

    The same :class:`RegionEvidence` object that produced the comparison
    defines the piece areas, so cached no-read decisions do not get a second,
    independent region model.
    """
    if tolerance is None:
        tolerance = comparison.tolerance
    old = comparison.old_only_composition
    common = comparison.common_composition
    new = comparison.new_only_composition
    return tighten_xyz_read_difference(
        old.unknown_area,
        common.unknown_area,
        new.unknown_area,
        xy_interval,
        yz_interval,
        old_only_outside=old.outside_area,
        new_only_outside=new.outside_area,
        tolerance=tolerance,
    )

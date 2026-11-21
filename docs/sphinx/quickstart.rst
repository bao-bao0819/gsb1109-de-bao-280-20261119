.. _quickstart:


Examples
========

One of the standard applications is to get a set of S2 cells at various levels
covering a rectangle in :math:`(lat, lng)` coordinates:

.. code-block:: python

    import s2sphere

    r = s2sphere.RegionCoverer()
    p1 = s2sphere.LatLng.from_degrees(33, -122)
    p2 = s2sphere.LatLng.from_degrees(33.1, -122.1)
    cell_ids = r.get_covering(s2sphere.LatLngRect.from_point_pair(p1, p2))
    print(cell_ids)

which prints this list:

.. code-block:: sh

    [9291041754864156672, 9291043953887412224, 9291044503643226112, 9291045878032760832, 9291047252422295552, 9291047802178109440, 9291051650468806656, 9291052200224620544]



Comparing disk read plans
=========================

Two plans over the same region may use different S2 cells.  A plan is an
outer covering minus the cells that are already cached; only the difference
is actually read.  Build one :class:`s2sphere.RegionEvidence` for the region
and share it, so cached no-read decisions do not create a second region
model:

.. code-block:: python

    import s2sphere

    region = s2sphere.LatLngRect.from_point_pair(
        s2sphere.LatLng.from_degrees(-0.02, 179.95),
        s2sphere.LatLng.from_degrees(0.02, -179.95),
    )
    evidence = s2sphere.RegionEvidence(
        region,
        interior_covering=interior,
    )
    old = s2sphere.ReadPlan(old_covering, old_cache, evidence=evidence)
    new = s2sphere.ReadPlan(new_covering, new_cache, evidence=evidence)
    comparison = s2sphere.ReadPlanComparison(old, new, evidence)

All areas are in steradians on the unit sphere, and intervals are closed
with an absolute tolerance (``1e-12`` steradians by default).  For example,
this rectangle near the date line has analytic area
``radians(0.04) * (sin(0.02 deg) - sin(-0.02 deg))``.  An outer covering is
larger than that, while an interior covering is a lower bound on the region
area.

``comparison.difference_interval`` bounds ``new_irrelevant - old_irrelevant``.
It uses only the ``old_only`` and ``new_only`` blocks::

    D_lo = new_only_outside - old_only_outside - old_only_unknown
    D_hi = new_only_outside - old_only_outside + new_only_unknown

The shared ``common`` block carries the same physical uncertainty in both
plans and cancels.  ``comparison.reduction_interval`` is its negation, and
``comparison.swapped().difference_interval`` is the same interval with the
opposite sign.  ``comparison.naive_difference_interval`` shows the wider
result of subtracting two independently computed read intervals.

``comparison.old_inside_lower_bound`` and
``comparison.new_inside_lower_bound`` are guaranteed lower bounds on the part
of each read that lies in the region.  A cache hit only avoids a read; it
does not prove that the cached cell is inside the measurement region.  Use
:func:`s2sphere.reconfirm_cached_cells` to separate confirmed cells from
boundary-straddling and excluded cells.

When two overlapping surveys give confirmed areas for ``X + Y`` and
``Y + Z`` but not for ``X``, ``Y``, or ``Z`` individually, pass the two
readings together:

.. code-block:: python

    result = s2sphere.tighten_xyz_read_difference(
        x_area, y_area, z_area, xy_interval, yz_interval)
    result.difference_interval
    result.lo_witness, result.hi_witness

Each endpoint comes with an allocation of confirmed area to every piece;
``result.check_witness(allocation, 'lo')`` verifies the piece bounds and
the survey constraints.  If no allocation explains both readings,
``result.feasible`` is ``False`` and the reason is reported.

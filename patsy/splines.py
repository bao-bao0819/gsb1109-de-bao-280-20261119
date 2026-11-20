# This file is part of Patsy
# Copyright (C) 2012-2013 Nathaniel Smith <njs@pobox.com>
# See file LICENSE.txt for license information.

# R-compatible spline basis functions

# These are made available in the patsy.* namespace
__all__ = ["bs"]

import numpy as np

from patsy.util import have_pandas, no_pickling, assert_no_pickling
from patsy.state import stateful_transform

if have_pandas:
    import pandas


def _eval_bspline_basis(x, knots, degree):
    try:
        from scipy.interpolate import splev
    except ImportError:  # pragma: no cover
        raise ImportError("spline functionality requires scipy")
    # 'knots' are assumed to be already pre-processed. E.g. usually you
    # want to include duplicate copies of boundary knots; you should do
    # that *before* calling this constructor.
    knots = np.atleast_1d(np.asarray(knots, dtype=float))
    assert knots.ndim == 1
    knots.sort()
    degree = int(degree)
    x = np.atleast_1d(x)
    if x.ndim == 2 and x.shape[1] == 1:
        x = x[:, 0]
    assert x.ndim == 1
    # XX FIXME: when points fall outside of the boundaries, splev and R seem
    # to handle them differently. I don't know why yet. So until we understand
    # this and decide what to do with it, I'm going to play it safe and
    # disallow such points. (NaNs are ignored here; they're missing values,
    # and are handled by the usual missing-data machinery instead.)
    if np.nanmin(x) < np.min(knots) or np.nanmax(x) > np.max(knots):
        raise NotImplementedError(
            "some data points fall outside the "
            "outermost knots, and I'm not sure how "
            "to handle them. (Patches accepted!)"
        )
    # Thanks to Charles Harris for explaining splev. It's not well
    # documented, but basically it computes an arbitrary b-spline basis
    # given knots and degree on some specified points (or derivatives
    # thereof, but we don't use that functionality), and then returns some
    # linear combination of these basis functions. To get out the basis
    # functions themselves, we use linear combinations like [1, 0, 0], [0,
    # 1, 0], [0, 0, 1].
    # NB: This probably makes it rather inefficient (though I haven't checked
    # to be sure -- maybe the fortran code actually skips computing the basis
    # function for coefficients that are zero).
    # Note: the order of a spline is the same as its degree + 1.
    # Note: there are (len(knots) - order) basis functions.
    n_bases = len(knots) - (degree + 1)
    basis = np.empty((x.shape[0], n_bases), dtype=float)
    for i in range(n_bases):
        coefs = np.zeros((n_bases,))
        coefs[i] = 1
        basis[:, i] = splev(x, (knots, coefs, degree))
    return basis


def _R_compat_quantile(x, probs):
    # return np.percentile(x, 100 * np.asarray(probs))
    probs = np.asarray(probs)
    quantiles = np.asarray(
        [np.percentile(x, 100 * prob) for prob in probs.ravel(order="C")]
    )
    return quantiles.reshape(probs.shape, order="C")


def test__R_compat_quantile():
    def t(x, prob, expected):
        assert np.allclose(_R_compat_quantile(x, prob), expected)

    t([10, 20], 0.5, 15)
    t([10, 20], 0.3, 13)
    t([10, 20], [0.3, 0.7], [13, 17])
    t(list(range(10)), [0.3, 0.7], [2.7, 6.3])


class BS(object):
    """bs(x, df=None, knots=None, degree=3, include_intercept=False, lower_bound=None, upper_bound=None, clamp_boundaries=False)

    Generates a B-spline basis for ``x``, allowing non-linear fits. The usual
    usage is something like::

      y ~ 1 + bs(x, 4)

    to fit ``y`` as a smooth function of ``x``, with 4 degrees of freedom
    given to the smooth.

    :arg df: The number of degrees of freedom to use for this spline. The
      return value will have this many columns. You must specify at least one
      of ``df`` and ``knots``.
    :arg knots: The interior knots to use for the spline. If unspecified, then
      equally spaced quantiles of the input data are used. You must specify at
      least one of ``df`` and ``knots``.
    :arg degree: The degree of the spline to use.
    :arg include_intercept: If ``True``, then the resulting
      spline basis will span the intercept term (i.e., the constant
      function). If ``False`` (the default) then this will not be the case,
      which is useful for avoiding overspecification in models that include
      multiple spline terms and/or an intercept term.
    :arg lower_bound: The lower exterior knot location.
    :arg upper_bound: The upper exterior knot location.
    :arg clamp_boundaries: If ``True``, then points that fall outside the
      lower/upper bounds are clamped to the nearest boundary before the
      spline basis is evaluated, instead of raising an error. This only
      affects the spline basis itself; any other terms in the formula (e.g.
      a plain ``x`` term) still see the original, unclamped values. Missing
      values are left as missing and handled by the usual missing-data
      machinery. The default is ``False``.

    A spline with ``degree=0`` is piecewise constant with breakpoints at each
    knot, and the default knot positions are quantiles of the input. So if you
    find yourself in the situation of wanting to quantize a continuous
    variable into ``num_bins`` equal-sized bins with a constant effect across
    each bin, you can use ``bs(x, num_bins - 1, degree=0)``. (The ``- 1`` is
    because one degree of freedom will be taken by the intercept;
    alternatively, you could leave the intercept term out of your model and
    use ``bs(x, num_bins, degree=0, include_intercept=True)``.

    A spline with ``degree=1`` is piecewise linear with breakpoints at each
    knot.

    The default is ``degree=3``, which gives a cubic b-spline.

    This is a stateful transform (for details see
    :ref:`stateful-transforms`). If ``knots``, ``lower_bound``, or
    ``upper_bound`` are not specified, they will be calculated from the data
    and then the chosen values will be remembered and re-used for prediction
    from the fitted model.

    Using this function requires scipy be installed.

    .. note:: This function is very similar to the R function of the same
      name. In cases where both return output at all (e.g., R's ``bs`` will
      raise an error if ``degree=0``, while patsy's will not), they should
      produce identical output given identical input and parameter settings.

    .. note:: By default, attempting to evaluate a spline basis at points
      outside the lower/upper bounds produces an error. Pass
      ``clamp_boundaries=True`` to instead evaluate such points at the
      nearest boundary.

    .. versionadded:: 0.2.0
    """

    def __init__(self):
        self._tmp = {}
        self._degree = None
        self._all_knots = None

    def memorize_chunk(
        self,
        x,
        df=None,
        knots=None,
        degree=3,
        include_intercept=False,
        lower_bound=None,
        upper_bound=None,
        clamp_boundaries=False,
    ):
        args = {
            "df": df,
            "knots": knots,
            "degree": degree,
            "include_intercept": include_intercept,
            "lower_bound": lower_bound,
            "upper_bound": upper_bound,
            "clamp_boundaries": clamp_boundaries,
        }
        self._tmp["args"] = args
        # XX: check whether we need x values before saving them
        x = np.atleast_1d(x)
        if x.ndim == 2 and x.shape[1] == 1:
            x = x[:, 0]
        if x.ndim > 1:
            raise ValueError("input to 'bs' must be 1-d, or a 2-d column vector")
        # There's no better way to compute exact quantiles than memorizing
        # all data.
        self._tmp.setdefault("xs", []).append(x)

    def memorize_finish(self):
        tmp = self._tmp
        args = tmp["args"]
        del self._tmp

        if args["degree"] < 0:
            raise ValueError(
                "degree must be greater than 0 (not %r)" % (args["degree"],)
            )
        if int(args["degree"]) != args["degree"]:
            raise ValueError("degree must be an integer (not %r)" % (self._degree,))

        # These are guaranteed to all be 1d vectors by the code above
        x = np.concatenate(tmp["xs"])
        if args["df"] is None and args["knots"] is None:
            raise ValueError("must specify either df or knots")
        order = args["degree"] + 1
        if args["df"] is not None:
            n_inner_knots = args["df"] - order
            if not args["include_intercept"]:
                n_inner_knots += 1
            if n_inner_knots < 0:
                raise ValueError(
                    "df=%r is too small for degree=%r and "
                    "include_intercept=%r; must be >= %s"
                    % (
                        args["df"],
                        args["degree"],
                        args["include_intercept"],
                        # We know that n_inner_knots is negative;
                        # if df were that much larger, it would
                        # have been zero, and things would work.
                        args["df"] - n_inner_knots,
                    )
                )
            if args["knots"] is not None:
                if len(args["knots"]) != n_inner_knots:
                    raise ValueError(
                        "df=%s with degree=%r implies %s knots, "
                        "but %s knots were provided"
                        % (
                            args["df"],
                            args["degree"],
                            n_inner_knots,
                            len(args["knots"]),
                        )
                    )
            else:
                # Need to compute inner knots
                knot_quantiles = np.linspace(0, 1, n_inner_knots + 2)[1:-1]
                inner_knots = _R_compat_quantile(x, knot_quantiles)
        if args["knots"] is not None:
            inner_knots = args["knots"]
        if args["lower_bound"] is not None:
            lower_bound = args["lower_bound"]
        else:
            lower_bound = np.min(x)
        if args["upper_bound"] is not None:
            upper_bound = args["upper_bound"]
        else:
            upper_bound = np.max(x)
        if lower_bound > upper_bound:
            raise ValueError(
                "lower_bound > upper_bound (%r > %r)" % (lower_bound, upper_bound)
            )
        inner_knots = np.asarray(inner_knots)
        if inner_knots.ndim > 1:
            raise ValueError("knots must be 1 dimensional")
        if np.any(inner_knots < lower_bound):
            raise ValueError(
                "some knot values (%s) fall below lower bound "
                "(%r)" % (inner_knots[inner_knots < lower_bound], lower_bound)
            )
        if np.any(inner_knots > upper_bound):
            raise ValueError(
                "some knot values (%s) fall above upper bound "
                "(%r)" % (inner_knots[inner_knots > upper_bound], upper_bound)
            )
        all_knots = np.concatenate(([lower_bound, upper_bound] * order, inner_knots))
        all_knots.sort()

        self._degree = args["degree"]
        self._all_knots = all_knots

    def transform(
        self,
        x,
        df=None,
        knots=None,
        degree=3,
        include_intercept=False,
        lower_bound=None,
        upper_bound=None,
        clamp_boundaries=False,
    ):
        basis_x = x
        if clamp_boundaries:
            # The memorized outermost knots are the lower/upper bounds.
            # np.clip leaves NaNs alone, so missing values stay missing and
            # are handled by the usual missing-data machinery.
            lower_bound = self._all_knots[0]
            upper_bound = self._all_knots[-1]
            basis_x = np.clip(np.asarray(x, dtype=float), lower_bound, upper_bound)
        basis = _eval_bspline_basis(basis_x, self._all_knots, self._degree)
        if not include_intercept:
            basis = basis[:, 1:]
        if have_pandas:
            if isinstance(x, (pandas.Series, pandas.DataFrame)):
                basis = pandas.DataFrame(basis)
                basis.index = x.index
        return basis

    __getstate__ = no_pickling


bs = stateful_transform(BS)


def test_bs_compat():
    from patsy.test_state import check_stateful
    from patsy.test_splines_bs_data import R_bs_test_x, R_bs_test_data, R_bs_num_tests

    lines = R_bs_test_data.split("\n")
    tests_ran = 0
    start_idx = lines.index("--BEGIN TEST CASE--")
    while True:
        if not lines[start_idx] == "--BEGIN TEST CASE--":
            break
        start_idx += 1
        stop_idx = lines.index("--END TEST CASE--", start_idx)
        block = lines[start_idx:stop_idx]
        test_data = {}
        for line in block:
            key, value = line.split("=", 1)
            test_data[key] = value
        # Translate the R output into Python calling conventions
        kwargs = {
            "degree": int(test_data["degree"]),
            # integer, or None
            "df": eval(test_data["df"]),
            # np.array() call, or None
            "knots": eval(test_data["knots"]),
        }
        if test_data["Boundary.knots"] != "None":
            lower, upper = eval(test_data["Boundary.knots"])
            kwargs["lower_bound"] = lower
            kwargs["upper_bound"] = upper
        kwargs["include_intercept"] = test_data["intercept"] == "TRUE"
        # Special case: in R, setting intercept=TRUE increases the effective
        # dof by 1. Adjust our arguments to match.
        # if kwargs["df"] is not None and kwargs["include_intercept"]:
        #     kwargs["df"] += 1
        output = np.asarray(eval(test_data["output"]))
        if kwargs["df"] is not None:
            assert output.shape[1] == kwargs["df"]
        # Do the actual test
        check_stateful(BS, False, R_bs_test_x, output, **kwargs)
        tests_ran += 1
        # Set up for the next one
        start_idx = stop_idx + 1
    assert tests_ran == R_bs_num_tests


test_bs_compat.slow = 1


# This isn't checked by the above, because R doesn't have zero degree
# b-splines.
def test_bs_0degree():
    x = np.logspace(-1, 1, 10)
    result = bs(x, knots=[1, 4], degree=0, include_intercept=True)
    assert result.shape[1] == 3
    expected_0 = np.zeros(10)
    expected_0[x < 1] = 1
    assert np.array_equal(result[:, 0], expected_0)
    expected_1 = np.zeros(10)
    expected_1[(x >= 1) & (x < 4)] = 1
    assert np.array_equal(result[:, 1], expected_1)
    expected_2 = np.zeros(10)
    expected_2[x >= 4] = 1
    assert np.array_equal(result[:, 2], expected_2)
    # Check handling of points that exactly fall on knots. They arbitrarily
    # get included into the larger region, not the smaller. This is consistent
    # with Python's half-open interval convention -- each basis function is
    # constant on [knot[i], knot[i + 1]).
    assert np.array_equal(
        bs([0, 1, 2], degree=0, knots=[1], include_intercept=True),
        [[1, 0], [0, 1], [0, 1]],
    )

    result_int = bs(x, knots=[1, 4], degree=0, include_intercept=True)
    result_no_int = bs(x, knots=[1, 4], degree=0, include_intercept=False)
    assert np.array_equal(result_int[:, 1:], result_no_int)


def test_bs_errors():
    import pytest

    x = np.linspace(-10, 10, 20)
    # error checks:
    # out of bounds
    pytest.raises(NotImplementedError, bs, x, 3, lower_bound=0)
    pytest.raises(NotImplementedError, bs, x, 3, upper_bound=0)
    # must specify df or knots
    pytest.raises(ValueError, bs, x)
    # df/knots match/mismatch (with and without intercept)
    #   match:
    bs(x, df=10, include_intercept=False, knots=[0] * 7)
    bs(x, df=10, include_intercept=True, knots=[0] * 6)
    bs(x, df=10, include_intercept=False, knots=[0] * 9, degree=1)
    bs(x, df=10, include_intercept=True, knots=[0] * 8, degree=1)
    #   too many knots:
    pytest.raises(ValueError, bs, x, df=10, include_intercept=False, knots=[0] * 8)
    pytest.raises(ValueError, bs, x, df=10, include_intercept=True, knots=[0] * 7)
    pytest.raises(
        ValueError, bs, x, df=10, include_intercept=False, knots=[0] * 10, degree=1
    )
    pytest.raises(
        ValueError, bs, x, df=10, include_intercept=True, knots=[0] * 9, degree=1
    )
    #   too few knots:
    pytest.raises(ValueError, bs, x, df=10, include_intercept=False, knots=[0] * 6)
    pytest.raises(ValueError, bs, x, df=10, include_intercept=True, knots=[0] * 5)
    pytest.raises(
        ValueError, bs, x, df=10, include_intercept=False, knots=[0] * 8, degree=1
    )
    pytest.raises(
        ValueError, bs, x, df=10, include_intercept=True, knots=[0] * 7, degree=1
    )
    # df too small
    pytest.raises(ValueError, bs, x, df=1, degree=3)
    pytest.raises(ValueError, bs, x, df=3, degree=5)
    # bad degree
    pytest.raises(ValueError, bs, x, df=10, degree=-1)
    pytest.raises(ValueError, bs, x, df=10, degree=1.5)
    # upper_bound < lower_bound
    pytest.raises(ValueError, bs, x, 3, lower_bound=1, upper_bound=-1)
    # multidimensional input
    pytest.raises(ValueError, bs, np.column_stack((x, x)), 3)
    # unsorted knots are okay, and get sorted
    assert np.array_equal(bs(x, knots=[1, 4]), bs(x, knots=[4, 1]))
    # 2d knots
    pytest.raises(ValueError, bs, x, knots=[[0], [20]])
    # knots > upper_bound
    pytest.raises(ValueError, bs, x, knots=[0, 20])
    pytest.raises(ValueError, bs, x, knots=[0, 4], upper_bound=3)
    # knots < lower_bound
    pytest.raises(ValueError, bs, x, knots=[-20, 0])
    pytest.raises(ValueError, bs, x, knots=[-4, 0], lower_bound=-3)


def test_bs_clamp_boundaries():
    import pytest
    from patsy.highlevel import dmatrix
    from patsy.build import build_design_matrices
    from patsy import PatsyError

    def bs_state(design_info):
        for factor_info in design_info.factor_infos.values():
            if factor_info.factor.name().startswith("bs("):
                (transform,) = factor_info.state["transforms"].values()
                return transform
        assert False, "no bs factor found"

    # Two designs built from the same formula text, but fit on training data
    # covering different ranges, so they memorize different boundaries.
    formula = "bs(x, df=5, clamp_boundaries=True) + x"
    train1 = {"x": np.linspace(0, 10, 21)}
    train2 = {"x": np.linspace(100, 200, 21)}
    design1 = dmatrix(formula, train1)
    design2 = dmatrix(formula, train2)
    info1 = design1.design_info
    info2 = design2.design_info
    knots1 = bs_state(info1)._all_knots.copy()
    knots2 = bs_state(info2)._all_knots.copy()
    lo1, hi1 = knots1[0], knots1[-1]
    lo2, hi2 = knots2[0], knots2[-1]
    assert (lo1, hi1) == (0, 10)
    assert (lo2, hi2) == (100, 200)
    # Enabling the mode does not change the number or order of columns.
    plain1 = dmatrix("bs(x, df=5) + x", train1)
    plain_info1 = plain1.design_info
    assert len(info1.column_names) == len(plain_info1.column_names)
    assert info1.column_names[0] == "Intercept"
    assert info1.column_names[-1] == "x"
    assert all(name.startswith("bs(") for name in info1.column_names[1:-1])

    # A scoring batch with in-boundary, on-boundary, out-of-boundary (both
    # sides, for both designs), and missing values.
    batch_x = np.array(
        [5.0, 0.0, 10.0, -3.0, 15.0, 150.0, 100.0, 200.0, 90.0, 210.0, np.nan]
    )
    batch = {"x": batch_x}
    kept_x = batch_x[:-1]

    # Score both designs jointly, then swap the order.
    mat1, mat2 = build_design_matrices([info1, info2], batch)
    mat2_swapped, mat1_swapped = build_design_matrices([info2, info1], batch)
    mat1 = np.asarray(mat1)
    mat2 = np.asarray(mat2)
    assert np.array_equal(mat1, np.asarray(mat1_swapped))
    assert np.array_equal(mat2, np.asarray(mat2_swapped))
    # The missing row is dropped from the joint matrices.
    assert mat1.shape[0] == len(kept_x)
    assert mat2.shape[0] == len(kept_x)
    # The plain x term keeps the original, unclamped values.
    assert np.array_equal(mat1[:, -1], kept_x)
    assert np.array_equal(mat2[:, -1], kept_x)

    # Each design clamps to its own memorized boundaries: every out-of-bound
    # row is identical to the row scored at the corresponding endpoint.
    def endpoint_row(info, value):
        (mat,) = build_design_matrices([info], {"x": [value]})
        return np.asarray(mat)[0]

    lo1_row = endpoint_row(info1, lo1)
    hi1_row = endpoint_row(info1, hi1)
    lo2_row = endpoint_row(info2, lo2)
    hi2_row = endpoint_row(info2, hi2)
    for value, row1, row2 in zip(kept_x, mat1, mat2):
        # Compare everything except the unclamped plain x column.
        if value < lo1:
            assert np.array_equal(row1[:-1], lo1_row[:-1])
        elif value > hi1:
            assert np.array_equal(row1[:-1], hi1_row[:-1])
        if value < lo2:
            assert np.array_equal(row2[:-1], lo2_row[:-1])
        elif value > hi2:
            assert np.array_equal(row2[:-1], hi2_row[:-1])

    # In-boundary values are not modified: clamped and unclamped designs
    # agree exactly inside the training range (boundaries included).
    in_bound = {"x": np.array([5.0, 0.0, 10.0])}
    (clamped_in,) = build_design_matrices([info1], in_bound)
    (plain_in,) = build_design_matrices([plain_info1], in_bound)
    assert np.allclose(np.asarray(clamped_in), np.asarray(plain_in))

    # Clamping does not update the memorized knots.
    assert np.array_equal(bs_state(info1)._all_knots, knots1)
    assert np.array_equal(bs_state(info2)._all_knots, knots2)

    # Clamping does not fill in missing values: they are still detected and
    # handled by the NA machinery.
    pytest.raises(
        PatsyError,
        build_design_matrices,
        [info1],
        {"x": [1.0, np.nan]},
        NA_action="raise",
    )

    # With the mode disabled, out-of-bound points are still rejected, even
    # when scored jointly with a clamp-enabled design. (The factor evaluation
    # machinery wraps the original NotImplementedError in a PatsyError.)
    for infos in ([plain_info1], [info1, plain_info1], [plain_info1, info2]):
        with pytest.raises(PatsyError) as excinfo:
            build_design_matrices(infos, batch)
        assert isinstance(excinfo.value.__cause__, NotImplementedError)

    # Also works with include_intercept=True, preserving column order.
    design_ii = dmatrix(
        "bs(x, df=4, include_intercept=True, clamp_boundaries=True) + x", train1
    )
    info_ii = design_ii.design_info
    assert info_ii.column_names[-1] == "x"
    assert len(info_ii.column_names) == 1 + 4 + 1
    (mat_ii,) = build_design_matrices([info_ii], {"x": [-3.0, 5.0, 15.0]})
    mat_ii = np.asarray(mat_ii)
    lo_ii_row = endpoint_row(info_ii, 0.0)
    hi_ii_row = endpoint_row(info_ii, 10.0)
    assert np.array_equal(mat_ii[0][:-1], lo_ii_row[:-1])
    assert np.array_equal(mat_ii[2][:-1], hi_ii_row[:-1])
    # The plain x column is still unclamped.
    assert np.array_equal(mat_ii[:, -1], [-3.0, 5.0, 15.0])

    # The stateful protocol itself honors the flag: same learned knots, and
    # clamping happens only when requested at transform time.
    t = BS()
    t.memorize_chunk(np.linspace(0, 10, 21), df=5)
    t.memorize_finish()
    pytest.raises(NotImplementedError, t.transform, [-3.0], df=5)
    clamped = t.transform([-3.0], df=5, clamp_boundaries=True)
    at_bound = t.transform([0.0], df=5)
    assert np.allclose(clamped, at_bound)


# differences between bs and ns (since the R code is a pile of copy-paste):
# - degree is always 3
# - different number of interior knots given df (b/c fewer dof used at edges I
#   guess)
# - boundary knots always repeated exactly 4 times (same as bs with degree=3)
# - complications at the end to handle boundary conditions
# the 'rcs' function uses slightly different conventions -- in particular it
# picks boundary knots that are not quite at the edges of the data, which
# makes sense for a natural spline.

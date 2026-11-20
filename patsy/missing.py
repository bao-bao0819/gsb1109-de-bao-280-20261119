# This file is part of Patsy
# Copyright (C) 2013 Nathaniel Smith <njs@pobox.com>
# See file LICENSE.txt for license information.

# Missing data detection/handling

# First, how do we represent missing data? (i.e., which values count as
# "missing"?) In the long run, we want to use numpy's NA support... but that
# doesn't exist yet. Until then, people use various sorts of ad-hoc
# things. Some things that might be considered NA:
#   NA (eventually)
#   NaN  (in float or object arrays)
#   None (in object arrays)
#   np.ma.masked (in numpy.ma masked arrays)
# Pandas compatibility considerations:
#   For numeric arrays, None is unconditionally converted to NaN.
#   For object arrays (including string arrays!), None and NaN are preserved,
#     but pandas.isnull() returns True for both.
# np.ma compatibility considerations:
#   Preserving array subtypes is a huge pain, because it means that we can't
#   just call 'asarray' and be done... we already jump through tons of hoops
#   to write code that can handle both ndarray's and pandas objects, and
#   just thinking about adding another item to this list makes me tired. So
#   for now we don't support np.ma missing values. Use pandas!

# Next, what should be done once we find missing data? R's options:
#   -- throw away those rows (from all aligned matrices)
#      -- with or without preserving information on which rows were discarded
#   -- error out
#   -- carry on
# The 'carry on' option requires that we have some way to represent NA in our
# output array. To avoid further solidifying the use of NaN for this purpose,
# we'll leave this option out for now, until real NA support is
# available. Also, we always preserve information on which rows were
# discarded, using the pandas index functionality (currently this is only
# returned to the original caller if they used return_type="dataframe",
# though).

import numpy as np
from patsy import PatsyError
from patsy.util import safe_isnan, safe_scalar_isnan, no_pickling, assert_no_pickling

# These are made available in the patsy.* namespace
__all__ = ["NAAction", "DropInfo"]

_valid_NA_types = ["None", "NaN"]
_valid_NA_responses = ["raise", "drop"]


def _desc_options(options):
    return ", ".join([repr(opt) for opt in options])


class NAAction(object):
    """An :class:`NAAction` object defines a strategy for handling missing
    data.

    "NA" is short for "Not Available", and is used to refer to any value which
    is somehow unmeasured or unavailable. In the long run, it is devoutly
    hoped that numpy will gain first-class missing value support. Until then,
    we work around this lack as best we're able.

    There are two parts to this: First, we have to determine what counts as
    missing data. For numerical data, the default is to treat NaN values
    (e.g., ``numpy.nan``) as missing. For categorical data, the default is to
    treat NaN values, and also the Python object None, as missing. (This is
    consistent with how pandas does things, so if you're already using
    None/NaN to mark missing data in your pandas DataFrames, you're good to
    go.)

    Second, we have to decide what to do with any missing data when we
    encounter it. One option is to simply discard any rows which contain
    missing data from our design matrices (``drop``). Another option is to
    raise an error (``raise``). A third option would be to simply let the
    missing values pass through into the returned design matrices. However,
    this last option is not yet implemented, because of the lack of any
    standard way to represent missing values in arbitrary numpy matrices;
    we're hoping numpy will get this sorted out before we standardize on
    anything ourselves.

    You can control how patsy handles missing data through the ``NA_action=``
    argument to functions like :func:`build_design_matrices` and
    :func:`dmatrix`. If all you want to do is to choose between ``drop`` and
    ``raise`` behaviour, you can pass one of those strings as the
    ``NA_action=`` argument directly. If you want more fine-grained control
    over how missing values are detected and handled, then you can create an
    instance of this class, or your own object that implements the same
    interface, and pass that as the ``NA_action=`` argument instead.
    """

    def __init__(self, on_NA="drop", NA_types=["None", "NaN"]):
        """The :class:`NAAction` constructor takes the following arguments:

        :arg on_NA: How to handle missing values. The default is ``"drop"``,
          which removes all rows from all matrices which contain any missing
          values. Also available is ``"raise"``, which raises an exception
          when any missing values are encountered.
        :arg NA_types: Which rules are used to identify missing values, as a
          list of strings. Allowed values are:

          * ``"None"``: treat the ``None`` object as missing in categorical
            data.
          * ``"NaN"``: treat floating point NaN values as missing in
            categorical and numerical data.

        .. versionadded:: 0.2.0
        """
        self.on_NA = on_NA
        if self.on_NA not in _valid_NA_responses:
            raise ValueError(
                "invalid on_NA action %r "
                "(should be one of %s)" % (on_NA, _desc_options(_valid_NA_responses))
            )
        if isinstance(NA_types, str):
            raise ValueError("NA_types should be a list of strings")
        self.NA_types = tuple(NA_types)
        for NA_type in self.NA_types:
            if NA_type not in _valid_NA_types:
                raise ValueError(
                    "invalid NA_type %r "
                    "(should be one of %s)" % (NA_type, _desc_options(_valid_NA_types))
                )

    def is_categorical_NA(self, obj):
        """Return True if `obj` is a categorical NA value.

        Note that here `obj` is a single scalar value."""
        if "NaN" in self.NA_types and safe_scalar_isnan(obj):
            return True
        if "None" in self.NA_types and obj is None:
            return True
        return False

    def is_numerical_NA(self, arr):
        """Returns a 1-d mask array indicating which rows in an array of
        numerical values contain at least one NA value.

        Note that here `arr` is a numpy array or pandas DataFrame."""
        mask = np.zeros(arr.shape, dtype=bool)
        if "NaN" in self.NA_types:
            mask |= np.isnan(arr)
        if mask.ndim > 1:
            mask = np.any(mask, axis=1)
        return mask

    def handle_NA(self, values, is_NAs, origins):
        """Takes a set of factor values that may have NAs, and handles them
        appropriately.

        :arg values: A list of `ndarray` objects representing the data.
          These may be 1- or 2-dimensional, and may be of varying dtype. All
          will have the same number of rows (or entries, for 1-d arrays).
        :arg is_NAs: A list with the same number of entries as `values`,
          containing boolean `ndarray` objects that indicate which rows
          contain NAs in the corresponding entry in `values`.
        :arg origins: A list with the same number of entries as
          `values`, containing information on the origin of each
          value. If we encounter a problem with some particular value, we use
          the corresponding entry in `origins` as the origin argument when
          raising a :class:`PatsyError`.
        :returns: A list of new values (which may have a differing number of
          rows.)
        """
        assert len(values) == len(is_NAs) == len(origins)
        if len(values) == 0:
            return values
        if self.on_NA == "raise":
            return self._handle_NA_raise(values, is_NAs, origins)
        elif self.on_NA == "drop":
            return self._handle_NA_drop(values, is_NAs, origins)
        else:  # pragma: no cover
            assert False

    def _handle_NA_raise(self, values, is_NAs, origins):
        for is_NA, origin in zip(is_NAs, origins):
            if np.any(is_NA):
                raise PatsyError("factor contains missing values", origin)
        return values

    def _handle_NA_drop(self, values, is_NAs, origins):
        total_mask = np.zeros(is_NAs[0].shape[0], dtype=bool)
        for is_NA in is_NAs:
            total_mask |= is_NA
        good_mask = ~total_mask
        # "..." to handle 1- versus 2-dim indexing
        return [v[good_mask] if v.ndim == 1 else v[good_mask, ...] for v in values]

    __getstate__ = no_pickling


class DropInfo(object):
    """Records how the ``drop`` NA action mapped the rows of the original
    data onto the rows of the returned design matrices.

    A :class:`DropInfo` is returned (alongside the design matrices) when
    calling :func:`build_design_matrices`, :func:`dmatrix`, or
    :func:`dmatrices` with ``return_drop_info=True``. It allows the rows of
    the returned matrices to be joined back onto the original data, and
    records exactly which factors were missing for each dropped row.

    All row references are *positional*: they are integer offsets into the
    original data (i.e. what you would pass to ``numpy.take`` or
    ``pandas.DataFrame.iloc``), counted before any rows were dropped. They
    are computed while the matrices are being built -- they are never
    reconstructed from pandas index labels, so they remain unambiguous even
    when the original data has a duplicate index. If the original data did
    have a pandas index, it is available as :attr:`data_index` purely as an
    annotation of the original data.

    .. versionadded:: 1.1.0

    Attributes:

    .. attribute:: num_rows_in

       The number of rows in the original data, before dropping.

    .. attribute:: kept_positions

       A 1-d integer ndarray. ``kept_positions[i]`` is the position in the
       original data of the row that became row ``i`` of the returned
       matrices. All matrices returned from the same build share this
       mapping, including intercept-only matrices.

    .. attribute:: dropped_positions

       A 1-d integer ndarray, in ascending order, giving the positions in
       the original data of every row that was dropped.

    .. attribute:: drop_reasons

       A tuple parallel to :attr:`dropped_positions`. Each entry is a tuple
       of :class:`FactorInfo` objects identifying every factor whose value
       was missing at that position. If a single row had several missing
       factors, all of them are listed. The entries are
       :class:`FactorInfo` objects (not names), so two factors that display
       identically but memorized different state (e.g. from different
       training batches) are still reported as distinct entries.

    .. attribute:: data_index

       The pandas :class:`~pandas.Index` of the original data, if one was
       involved in the build, otherwise ``None``. This is only an
       annotation of the original data: index labels may be duplicated, so
       always use the positional attributes above to join results back onto
       your data.
    """

    def __init__(
        self, num_rows_in, kept_positions, dropped_positions, drop_reasons, data_index=None
    ):
        self.num_rows_in = num_rows_in
        self.kept_positions = np.asarray(kept_positions, dtype=int)
        self.dropped_positions = np.asarray(dropped_positions, dtype=int)
        self.drop_reasons = tuple(drop_reasons)
        self.data_index = data_index
        if self.kept_positions.ndim != 1 or self.dropped_positions.ndim != 1:
            raise ValueError("kept_positions and dropped_positions must be 1-d")
        if len(self.drop_reasons) != self.dropped_positions.shape[0]:
            raise ValueError(
                "drop_reasons must have one entry per dropped position"
            )
        if num_rows_in is not None:
            if (
                self.kept_positions.shape[0] + self.dropped_positions.shape[0]
                != num_rows_in
            ):
                raise ValueError(
                    "kept and dropped positions do not add up to num_rows_in"
                )

    @property
    def num_rows_out(self):
        """The number of rows in each returned design matrix."""
        return self.kept_positions.shape[0]

    @property
    def kept_labels(self):
        """The index labels of the kept rows, or None if the original data
        had no pandas index.

        These are only an annotation of the original data; labels may be
        duplicated. Use :attr:`kept_positions` to join back reliably."""
        if self.data_index is None:
            return None
        return self.data_index.take(self.kept_positions)

    @property
    def dropped_labels(self):
        """The index labels of the dropped rows, or None if the original
        data had no pandas index. See :attr:`kept_labels`."""
        if self.data_index is None:
            return None
        return self.data_index.take(self.dropped_positions)

    @property
    def drop_reason_names(self):
        """Like :attr:`drop_reasons`, but with each :class:`FactorInfo`
        replaced by its display name.

        Convenient for reporting, but note that distinct factors (e.g. the
        same expression memorized in different training batches) may share
        a single name; use :attr:`drop_reasons` itself to tell them apart.
        """
        return tuple(
            tuple(factor_info.factor.name() for factor_info in reasons)
            for reasons in self.drop_reasons
        )

    def row_for_position(self, position):
        """Returns the row index in the returned matrices corresponding to
        the given original data position, or None if that row was dropped.
        """
        matches = np.flatnonzero(self.kept_positions == position)
        if matches.shape[0] == 0:
            return None
        return int(matches[0])

    def reasons_for_position(self, position):
        """Returns the tuple of :class:`FactorInfo` objects whose missing
        values caused the given original data position to be dropped, or
        None if that row was kept."""
        matches = np.flatnonzero(self.dropped_positions == position)
        if matches.shape[0] == 0:
            return None
        return self.drop_reasons[int(matches[0])]

    def __repr__(self):
        return (
            "DropInfo(num_rows_in=%r, num_rows_out=%r, "
            "dropped_positions=%r, drop_reason_names=%r)"
            % (
                self.num_rows_in,
                self.num_rows_out,
                self.dropped_positions,
                self.drop_reason_names,
            )
        )


def test_NAAction_basic():
    import pytest

    pytest.raises(ValueError, NAAction, on_NA="pord")
    pytest.raises(ValueError, NAAction, NA_types=("NaN", "asdf"))
    pytest.raises(ValueError, NAAction, NA_types="NaN")

    assert_no_pickling(NAAction())


def test_NAAction_NA_types_numerical():
    for NA_types in [[], ["NaN"], ["None"], ["NaN", "None"]]:
        action = NAAction(NA_types=NA_types)
        for extra_shape in [(), (1,), (2,)]:
            arr = np.ones((4,) + extra_shape, dtype=float)
            nan_rows = [0, 2]
            if arr.ndim > 1 and arr.shape[1] > 1:
                arr[nan_rows, [0, 1]] = np.nan
            else:
                arr[nan_rows] = np.nan
            exp_NA_mask = np.zeros(4, dtype=bool)
            if "NaN" in NA_types:
                exp_NA_mask[nan_rows] = True
            got_NA_mask = action.is_numerical_NA(arr)
            assert np.array_equal(got_NA_mask, exp_NA_mask)


def test_NAAction_NA_types_categorical():
    for NA_types in [[], ["NaN"], ["None"], ["NaN", "None"]]:
        action = NAAction(NA_types=NA_types)
        assert not action.is_categorical_NA("a")
        assert not action.is_categorical_NA(1)
        assert action.is_categorical_NA(None) == ("None" in NA_types)
        assert action.is_categorical_NA(np.nan) == ("NaN" in NA_types)


def test_NAAction_drop():
    action = NAAction("drop")
    in_values = [
        np.asarray([-1, 2, -1, 4, 5]),
        np.asarray([10.0, 20.0, 30.0, 40.0, 50.0]),
        np.asarray([[1.0, np.nan], [3.0, 4.0], [10.0, 5.0], [6.0, 7.0], [8.0, np.nan]]),
    ]
    is_NAs = [
        np.asarray([True, False, True, False, False]),
        np.zeros(5, dtype=bool),
        np.asarray([True, False, False, False, True]),
    ]
    out_values = action.handle_NA(in_values, is_NAs, [None] * 3)
    assert len(out_values) == 3
    assert np.array_equal(out_values[0], [2, 4])
    assert np.array_equal(out_values[1], [20.0, 40.0])
    assert np.array_equal(out_values[2], [[3.0, 4.0], [6.0, 7.0]])


def test_NAAction_raise():
    action = NAAction(on_NA="raise")

    # no-NA just passes through:
    in_arrs = [np.asarray([1.1, 1.2]), np.asarray([1, 2])]
    is_NAs = [np.asarray([False, False])] * 2
    got_arrs = action.handle_NA(in_arrs, is_NAs, [None, None])
    assert np.array_equal(got_arrs[0], in_arrs[0])
    assert np.array_equal(got_arrs[1], in_arrs[1])

    from patsy.origin import Origin

    o1 = Origin("asdf", 0, 1)
    o2 = Origin("asdf", 2, 3)

    # NA raises an error with a correct origin
    in_idx = np.arange(2)
    in_arrs = [np.asarray([1.1, 1.2]), np.asarray([1.0, np.nan])]
    is_NAs = [np.asarray([False, False]), np.asarray([False, True])]
    try:
        action.handle_NA(in_arrs, is_NAs, [o1, o2])
        assert False
    except PatsyError as e:
        assert e.origin is o2

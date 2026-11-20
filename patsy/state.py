# This file is part of Patsy
# Copyright (C) 2011 Nathaniel Smith <njs@pobox.com>
# See file LICENSE.txt for license information.

# Stateful transform protocol:
#   def __init__(self):
#       pass
#   def memorize_chunk(self, input_data):
#       return None
#   def memorize_finish(self):
#       return None
#   def transform(self, input_data):
#       return output_data

# BETTER WAY: always run the first row of data through the builder alone, and
# check that it gives the same output row as when running the whole block of
# data through at once. This gives us the same information, but it's robust
# against people writing their own centering functions.

# QUESTION: right now we refuse to even fit a model that contains a
# my_transform(x)-style function. Maybe we should allow it to be fit (with a
# warning), and only disallow making predictions with it? Need to revisit this
# question once it's clearer what exactly our public API will look like,
# because right now I'm not sure how to tell whether we are being called for
# fitting versus being called for prediction.

from functools import wraps
import numpy as np
from patsy.util import (
    atleast_2d_column_default,
    asarray_or_pandas,
    pandas_friendly_reshape,
    wide_dtype_for,
    safe_issubdtype,
    no_pickling,
    assert_no_pickling,
)

# These are made available in the patsy.* namespace
__all__ = [
    "stateful_transform",
    "center",
    "standardize",
    "scale",
]


def stateful_transform(class_):
    """Create a stateful transform callable object from a class that fulfills
    the :ref:`stateful transform protocol <stateful-transform-protocol>`.
    """

    @wraps(class_)
    def stateful_transform_wrapper(*args, **kwargs):
        transform = class_()
        transform.memorize_chunk(*args, **kwargs)
        transform.memorize_finish()
        # 'weights' is a fitting-only argument that tunes the learned
        # statistics. Transforms always apply the fixed, learned statistics,
        # so it must never be forwarded to transform() (e.g. prediction data
        # has no weight column at all).
        transform_kwargs = dict(kwargs)
        transform_kwargs.pop("weights", None)
        return transform.transform(*args, **transform_kwargs)

    stateful_transform_wrapper.__patsy_stateful_transform__ = class_
    return stateful_transform_wrapper


# class NonIncrementalStatefulTransform(object):
#     def __init__(self):
#         self._data = []
#
#     def memorize_chunk(self, input_data, *args, **kwargs):
#         self._data.append(input_data)
#         self._args = _args
#         self._kwargs = kwargs
#
#     def memorize_finish(self):
#         all_data = np.vstack(self._data)
#         args = self._args
#         kwargs = self._kwargs
#         del self._data
#         del self._args
#         del self._kwargs
#         self.memorize_all(all_data, *args, **kwargs)
#
#     def memorize_all(self, input_data, *args, **kwargs):
#         raise NotImplementedError
#
#     def transform(self, input_data, *args, **kwargs):
#         raise NotImplementedError
#
# class QuantileEstimatingTransform(NonIncrementalStatefulTransform):
#     def memorize_all(self, input_data, *args, **kwargs):


def _check_weights(weights, n_rows):
    """Validate a column of integer frequency (case-count) weights.

    Each weight counts how many times its row is repeated, so it must be a
    single non-negative integer per row. The weights are never expanded into
    repeated rows -- this only checks them and returns a plain 2-D integer
    array. Zero-weight rows are allowed (they are calibration rows that do
    not contribute to the statistics).
    """
    weights = atleast_2d_column_default(weights)
    if weights.shape[0] != n_rows:
        raise ValueError(
            "weights must have one entry for each row of data "
            "(got %s weights for %s rows)" % (weights.shape[0], n_rows)
        )
    if weights.shape[1] != 1:
        raise ValueError(
            "weights must be a single column of integer frequencies, "
            "not an array with shape %r" % (weights.shape,)
        )
    # bool is deliberately not an integer dtype here: True/False are not
    # meaningful repetition counts. Likewise integer-valued floats (e.g.
    # 2.0) are rejected rather than silently truncated.
    if not safe_issubdtype(weights.dtype, np.integer):
        raise ValueError(
            "weights must be integers (each weight is the number of repeated "
            "observations for that row); got dtype %r instead" % (weights.dtype,)
        )
    if np.any(weights < 0):
        raise ValueError("weights must be non-negative integers")
    return weights


def test__check_weights():
    import pytest

    # Accepted shapes and types
    assert _check_weights([1, 0, 3], 3).shape == (3, 1)
    assert _check_weights(np.array([1, 0, 3], dtype=np.int64), 3).dtype == np.int64
    assert _check_weights(np.array([[2], [0]]), 2).shape == (2, 1)

    # Wrong number of rows
    pytest.raises(ValueError, _check_weights, [1, 2], 3)
    pytest.raises(ValueError, _check_weights, [1, 2, 3], 2)
    # More than one column
    pytest.raises(ValueError, _check_weights, np.ones((3, 2), dtype=int), 3)
    # Negative weights
    pytest.raises(ValueError, _check_weights, [1, -1, 0], 3)
    # Non-integer weights
    pytest.raises(ValueError, _check_weights, [1.0, 2.0], 2)
    pytest.raises(ValueError, _check_weights, [1.5], 1)
    # Booleans are not accepted as frequencies
    pytest.raises(ValueError, _check_weights, np.array([True, False]), 2)
    # Strings / objects are not numeric at all
    pytest.raises(ValueError, _check_weights, ["1", "2"], 2)


class Center(object):
    """center(x, weights=None)

    A stateful transform that centers input data, i.e., subtracts the mean.

    If input has multiple columns, centers each column separately.

    If `weights` is given, it must be a column of non-negative integer
    frequencies, with one weight per row. Each weight is the number of times
    that row's observation is repeated, so the mean is computed as a
    frequency-weighted mean without ever expanding the data in memory. Rows
    with weight zero do not contribute. Weights are used while fitting only;
    when the fitted transform is later applied to new data (e.g. for
    prediction) no weight column is needed and the learned mean is reused.

    Equivalent to ``standardize(x, rescale=False)``
    """

    def __init__(self):
        # Default (unweighted) accumulator, kept exactly for backward
        # compatible behavior.
        self._sum = None
        self._count = 0
        # Weighted frequency accumulator. Statistics are measured relative to
        # a fixed 'origin' (the first positively weighted observation) so that
        # values sitting on a huge common baseline do not lose precision to
        # catastrophic cancellation. The result is independent of how the rows
        # are split into chunks or ordered.
        self._weighted = False
        self._origin = None
        self._weighted_T = None

    def memorize_chunk(self, x, weights=None):
        x = atleast_2d_column_default(x)
        if weights is None:
            self._count += x.shape[0]
            this_total = np.sum(x, 0, dtype=wide_dtype_for(x))
            # This is to handle potentially multi-column x's:
            if self._sum is None:
                self._sum = this_total
            else:
                self._sum += this_total
        else:
            self._weighted = True
            w = _check_weights(weights, x.shape[0])
            self._memorize_chunk_weighted(x, w)

    def _memorize_chunk_weighted(self, x, w):
        wide = wide_dtype_for(x)
        x = np.asarray(x, dtype=wide)
        wf = w.astype(wide)
        if self._origin is None:
            nonzero = np.nonzero(w[:, 0])[0]
            # A chunk made entirely of zero-weight rows contributes nothing
            # and must not pin the origin to an observation that is ignored.
            if nonzero.size == 0:
                return
            self._origin = x[nonzero[0], :].copy()
            self._weighted_T = np.zeros(x.shape[1], dtype=wide)
        d = x - self._origin
        self._weighted_T += np.sum(wf * d, axis=0)
        self._count += int(np.sum(w, dtype=np.int64))

    def memorize_finish(self):
        if self._weighted and self._count == 0:
            raise ValueError(
                "cannot center data: every weight was zero, so there are no "
                "observations with positive frequency"
            )

    def _weighted_mean(self, dtype):
        mean = self._origin + self._weighted_T / self._count
        return np.asarray(mean, dtype=dtype)

    def transform(self, x, weights=None):
        if weights is not None:
            raise ValueError(
                "weights are used only while fitting; do not pass weights "
                "when applying a fitted transform to new data"
            )
        x = asarray_or_pandas(x)
        # This doesn't copy data unless our input is a DataFrame that has
        # heterogeneous types. And in that case we're going to be munging the
        # types anyway, so copying isn't a big deal.
        x_arr = np.asarray(x)
        if safe_issubdtype(x_arr.dtype, np.integer):
            dt = float
        else:
            dt = x_arr.dtype
        if self._weighted:
            mean_val = self._weighted_mean(dt)
        else:
            mean_val = np.asarray(self._sum / self._count, dtype=dt)
        centered = atleast_2d_column_default(x, preserve_pandas=True) - mean_val
        return pandas_friendly_reshape(centered, x.shape)

    __getstate__ = no_pickling


center = stateful_transform(Center)


# Weighted statistics are accumulated as
#   T = sum_i w_i (x_i - origin)
#   S = sum_i w_i (x_i - origin)^2
# over all positively weighted rows. Then
#   mean = origin + T / n
#   M2   = S - T^2 / n
# where n = sum_i w_i is the total effective frequency. Working relative to a
# fixed origin keeps the arithmetic on the (small) deviations from the first
# observation instead of the (potentially huge) raw values, so the statistics
# are accurate even when every value shares a very large baseline and differ
# only slightly. Only O(number of columns) state is retained -- rows are never
# repeated in memory.
class Standardize(object):
    """standardize(x, center=True, rescale=True, ddof=0, weights=None)

    A stateful transform that standardizes input data, i.e. it subtracts the
    mean and divides by the sample standard deviation.

    Either centering or rescaling or both can be disabled by use of keyword
    arguments. The `ddof` argument controls the delta degrees of freedom when
    computing the standard deviation (cf. :func:`numpy.std`). The default of
    ``ddof=0`` produces the maximum likelihood estimate; use ``ddof=1`` if you
    prefer the square root of the unbiased estimate of the variance.

    If input has multiple columns, standardizes each column separately.

    If `weights` is given, it must be a column of non-negative integer
    frequencies with one weight per row. Each weight is the number of times
    that row's observation is repeated. The mean and the sum of squared
    deviations are then computed over the full set of repeated observations,
    so the learned statistics are exactly those of the frequency-expanded
    data -- but the expansion is never materialized in memory. Rows whose
    weight is zero are calibration rows that contribute nothing. The computed
    statistics do not depend on how the rows are chunked or ordered, and
    chunks containing only zero-weight rows are tolerated.

    Weights are a fitting-only concept: they tune the statistics learned from
    the training data. When the fitted transform is later applied to new data
    (for prediction) it reuses those fixed statistics and does not read any
    weight column. If the total effective frequency is not greater than
    `ddof` while rescaling, the scale is not defined and fitting raises a
    :class:`ValueError`.

    .. note:: This function computes the mean and standard deviation using a
       memory-efficient online algorithm, making it suitable for use with
       large incrementally processed data-sets.
    """

    def __init__(self):
        # Default (unweighted) Welford accumulator, preserved exactly so that
        # behavior without weights is unchanged.
        self.current_n = 0
        self.current_mean = None
        self.current_M2 = None
        # Weighted frequency accumulator (shifted-origin; see note above).
        self._weighted = False
        self._weighted_n = 0
        self._origin = None
        self._weighted_T = None
        self._weighted_S = None
        self._rescale = True
        self._ddof = 0

    def memorize_chunk(self, x, center=True, rescale=True, ddof=0, weights=None):
        x = atleast_2d_column_default(x)
        self._rescale = rescale
        self._ddof = ddof
        if weights is None:
            self._memorize_chunk_unweighted(x)
        else:
            self._weighted = True
            w = _check_weights(weights, x.shape[0])
            self._memorize_chunk_weighted(x, w)

    def _memorize_chunk_unweighted(self, x):
        if self.current_mean is None:
            self.current_mean = np.zeros(x.shape[1], dtype=wide_dtype_for(x))
            self.current_M2 = np.zeros(x.shape[1], dtype=wide_dtype_for(x))
        # XX this can surely be vectorized but I am feeling lazy:
        for i in range(x.shape[0]):
            self.current_n += 1
            delta = x[i, :] - self.current_mean
            self.current_mean += delta / self.current_n
            self.current_M2 += delta * (x[i, :] - self.current_mean)

    def _memorize_chunk_weighted(self, x, w):
        wide = wide_dtype_for(x)
        x = np.asarray(x, dtype=wide)
        wf = w.astype(wide)
        if self._origin is None:
            nonzero = np.nonzero(w[:, 0])[0]
            # An all-zero-weight chunk carries no information and must not fix
            # the origin to an ignored observation.
            if nonzero.size == 0:
                return
            self._origin = x[nonzero[0], :].copy()
            self._weighted_T = np.zeros(x.shape[1], dtype=wide)
            self._weighted_S = np.zeros(x.shape[1], dtype=wide)
        d = x - self._origin
        self._weighted_T += np.sum(wf * d, axis=0)
        self._weighted_S += np.sum(wf * d * d, axis=0)
        self._weighted_n += int(np.sum(w, dtype=np.int64))

    def memorize_finish(self):
        if not self._weighted:
            return
        if self._weighted_n == 0:
            raise ValueError(
                "cannot standardize data: every weight was zero, so there "
                "are no observations with positive frequency"
            )
        if self._rescale and self._weighted_n <= self._ddof:
            raise ValueError(
                "cannot define a scale with ddof=%s from a total effective "
                "frequency of %s; the effective frequency must be greater "
                "than ddof" % (self._ddof, self._weighted_n)
            )

    def _weighted_stats(self):
        n = self._weighted_n
        mean = self._origin + self._weighted_T / n
        M2 = self._weighted_S - self._weighted_T**2 / n
        return mean, M2, n

    def transform(self, x, center=True, rescale=True, ddof=0, weights=None):
        if weights is not None:
            raise ValueError(
                "weights are used only while fitting; do not pass weights "
                "when applying a fitted transform to new data"
            )
        # XX: this forces all inputs to double-precision real, even if the
        # input is single- or extended-precision or complex. But I got all
        # tangled up in knots trying to do that without breaking something
        # else (e.g. by requiring an extra copy).
        x = asarray_or_pandas(x, copy=True, dtype=float)
        x_2d = atleast_2d_column_default(x, preserve_pandas=True)
        if self._weighted:
            mean, M2, n = self._weighted_stats()
            if center:
                x_2d -= mean
            if rescale:
                # Defensive guard; memorize_finish() already rejects fits
                # where the effective frequency does not exceed ddof.
                if n - ddof <= 0:
                    raise ValueError(
                        "cannot compute a scale with ddof=%s from a total "
                        "effective frequency of %s" % (ddof, n)
                    )
                x_2d /= np.sqrt(M2 / (n - ddof))
        else:
            if center:
                x_2d -= self.current_mean
            if rescale:
                x_2d /= np.sqrt(self.current_M2 / (self.current_n - ddof))
        return pandas_friendly_reshape(x_2d, x.shape)

    __getstate__ = no_pickling


standardize = stateful_transform(Standardize)
# R compatibility:
scale = standardize

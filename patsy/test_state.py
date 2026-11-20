# This file is part of Patsy
# Copyright (C) 2012-2013 Nathaniel Smith <njs@pobox.com>
# See file LICENSE.txt for license information.

import numpy as np
import pytest

from patsy.state import Center, Standardize, center, standardize
from patsy.util import atleast_2d_column_default


def check_stateful(cls, accepts_multicolumn, input, output, *args, **kwargs):
    input = np.asarray(input)
    output = np.asarray(output)
    test_cases = [
        # List input, one chunk
        ([input], output),
        # Scalar input, many chunks
        (input, output),
        # List input, many chunks:
        ([[n] for n in input], output),
        # 0-d array input, many chunks:
        ([np.array(n) for n in input], output),
        # 1-d array input, one chunk:
        ([np.array(input)], output),
        # 1-d array input, many chunks:
        ([np.array([n]) for n in input], output),
        # 2-d but 1 column input, one chunk:
        ([np.array(input)[:, None]], atleast_2d_column_default(output)),
        # 2-d but 1 column input, many chunks:
        ([np.array([[n]]) for n in input], atleast_2d_column_default(output)),
    ]
    if accepts_multicolumn:
        # 2-d array input, one chunk:
        test_cases += [
            (
                [np.column_stack((input, input[::-1]))],
                np.column_stack((output, output[::-1])),
            ),
            # 2-d array input, many chunks:
            (
                [np.array([[input[i], input[-i - 1]]]) for i in range(len(input))],
                np.column_stack((output, output[::-1])),
            ),
        ]
    from patsy.util import have_pandas

    if have_pandas:
        import pandas

        pandas_type = (pandas.Series, pandas.DataFrame)
        pandas_index = np.linspace(0, 1, num=len(input))
        # 1d and 2d here refer to the dimensionality of the input
        if output.ndim == 1:
            output_1d = pandas.Series(output, index=pandas_index)
        else:
            output_1d = pandas.DataFrame(output, index=pandas_index)
        test_cases += [
            # Series input, one chunk
            ([pandas.Series(input, index=pandas_index)], output_1d),
            # Series input, many chunks
            (
                [
                    pandas.Series([x], index=[idx])
                    for (x, idx) in zip(input, pandas_index)
                ],
                output_1d,
            ),
        ]
        if accepts_multicolumn:
            input_2d_2col = np.column_stack((input, input[::-1]))
            output_2d_2col = np.column_stack((output, output[::-1]))
            output_2col_dataframe = pandas.DataFrame(output_2d_2col, index=pandas_index)
            test_cases += [
                # DataFrame input, one chunk
                (
                    [pandas.DataFrame(input_2d_2col, index=pandas_index)],
                    output_2col_dataframe,
                ),
                # DataFrame input, many chunks
                (
                    [
                        pandas.DataFrame([input_2d_2col[i, :]], index=[pandas_index[i]])
                        for i in range(len(input))
                    ],
                    output_2col_dataframe,
                ),
            ]
    for input_obj, output_obj in test_cases:
        print(input_obj)
        t = cls()
        for input_chunk in input_obj:
            t.memorize_chunk(input_chunk, *args, **kwargs)
        t.memorize_finish()
        all_outputs = []
        for input_chunk in input_obj:
            output_chunk = t.transform(input_chunk, *args, **kwargs)
            if input.ndim == output.ndim:
                assert output_chunk.ndim == np.asarray(input_chunk).ndim
            all_outputs.append(output_chunk)
        if have_pandas and isinstance(all_outputs[0], pandas_type):
            all_output1 = pandas.concat(all_outputs)
            assert np.array_equal(all_output1.index, pandas_index)
        elif all_outputs[0].ndim == 0:
            all_output1 = np.array(all_outputs)
        elif all_outputs[0].ndim == 1:
            all_output1 = np.concatenate(all_outputs)
        else:
            all_output1 = np.vstack(all_outputs)
        assert all_output1.shape[0] == len(input)
        # output_obj_reshaped = np.asarray(output_obj).reshape(all_output1.shape)
        # assert np.allclose(all_output1, output_obj_reshaped)
        assert np.allclose(all_output1, output_obj)
        if np.asarray(input_obj[0]).ndim == 0:
            all_input = np.array(input_obj)
        elif have_pandas and isinstance(input_obj[0], pandas_type):
            # handles both Series and DataFrames
            all_input = pandas.concat(input_obj)
        elif np.asarray(input_obj[0]).ndim == 1:
            # Don't use vstack, because that would turn this into a 1xn
            # matrix:
            all_input = np.concatenate(input_obj)
        else:
            all_input = np.vstack(input_obj)
        all_output2 = t.transform(all_input, *args, **kwargs)
        if have_pandas and isinstance(input_obj[0], pandas_type):
            assert np.array_equal(all_output2.index, pandas_index)
        if input.ndim == output.ndim:
            assert all_output2.ndim == all_input.ndim
        assert np.allclose(all_output2, output_obj)


def test_Center():
    check_stateful(Center, True, [1, 2, 3], [-1, 0, 1])
    check_stateful(Center, True, [1, 2, 1, 2], [-0.5, 0.5, -0.5, 0.5])
    check_stateful(Center, True, [1.3, -10.1, 7.0, 12.0], [-1.25, -12.65, 4.45, 9.45])


def test_stateful_transform_wrapper():
    assert np.allclose(center([1, 2, 3]), [-1, 0, 1])
    assert np.allclose(center([1, 2, 1, 2]), [-0.5, 0.5, -0.5, 0.5])
    assert center([1.0, 2.0, 3.0]).dtype == np.dtype(float)
    assert center(np.array([1.0, 2.0, 3.0], dtype=np.float32)).dtype == np.dtype(
        np.float32
    )
    assert center([1, 2, 3]).dtype == np.dtype(float)

    from patsy.util import have_pandas

    if have_pandas:
        import pandas

        s = pandas.Series([1, 2, 3], index=["a", "b", "c"])
        df = pandas.DataFrame(
            [[1, 2], [2, 4], [3, 6]], columns=["x1", "x2"], index=[10, 20, 30]
        )
        s_c = center(s)
        assert isinstance(s_c, pandas.Series)
        assert np.array_equal(s_c.index, ["a", "b", "c"])
        assert np.allclose(s_c, [-1, 0, 1])
        df_c = center(df)
        assert isinstance(df_c, pandas.DataFrame)
        assert np.array_equal(df_c.index, [10, 20, 30])
        assert np.array_equal(df_c.columns, ["x1", "x2"])
        assert np.allclose(df_c, [[-1, -2], [0, 0], [1, 2]])


def test_Standardize():
    check_stateful(Standardize, True, [1, -1], [1, -1])
    check_stateful(Standardize, True, [12, 10], [1, -1])
    check_stateful(
        Standardize, True, [12, 11, 10], [np.sqrt(3.0 / 2), 0, -np.sqrt(3.0 / 2)]
    )

    check_stateful(
        Standardize, True, [12.0, 11.0, 10.0], [np.sqrt(3.0 / 2), 0, -np.sqrt(3.0 / 2)]
    )

    # XX: see the comment in Standardize.transform about why this doesn't
    # work:
    # check_stateful(Standardize,
    #               [12.0+0j, 11.0+0j, 10.0],
    #               [np.sqrt(3./2)+0j, 0, -np.sqrt(3./2)])

    r20 = list(range(20))

    check_stateful(
        Standardize, True, [1, -1], [np.sqrt(2) / 2, -np.sqrt(2) / 2], ddof=1
    )

    check_stateful(
        Standardize, True, r20, list((np.arange(20) - 9.5) / 5.7662812973353983), ddof=0
    )
    check_stateful(
        Standardize, True, r20, list((np.arange(20) - 9.5) / 5.9160797830996161), ddof=1
    )
    check_stateful(
        Standardize, True, r20, list((np.arange(20) - 9.5)), rescale=False, ddof=1
    )
    check_stateful(
        Standardize,
        True,
        r20,
        list(np.arange(20) / 5.9160797830996161),
        center=False,
        ddof=1,
    )
    check_stateful(Standardize, True, r20, r20, center=False, rescale=False, ddof=1)


def _expanded(x, weights):
    # The frequency-expanded reference data set.
    x = np.asarray(x, dtype=float)
    if x.ndim == 1:
        return np.repeat(x, np.asarray(weights))
    return np.repeat(x, np.asarray(weights), axis=0)


def test_Center_weights_match_expansion():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(8, 2))
    weights = np.array([1, 100000, 3, 0, 7, 2, 50000, 0], dtype=np.int64)
    expanded = _expanded(x, weights)

    t = Center()
    t.memorize_chunk(x, weights=weights)
    t.memorize_finish()
    assert np.allclose(t._weighted_mean(float), expanded.mean(0))

    # The transform applies the fixed mean and takes no weights
    centered = t.transform(x)
    assert np.allclose(centered, x - expanded.mean(0))
    pytest.raises(ValueError, t.transform, x, weights=weights)


def test_Center_weights_all_zero_rejected():
    t = Center()
    t.memorize_chunk(np.arange(6.0).reshape(3, 2), weights=np.zeros(3, dtype=int))
    pytest.raises(ValueError, t.memorize_finish)


def test_Standardize_weights_match_expansion():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(10, 3))
    weights = np.array([1, 100000, 3, 0, 7, 2, 50000, 0, 1, 9], dtype=np.int64)
    expanded = _expanded(x, weights)

    t = Standardize()
    t.memorize_chunk(x, ddof=1, weights=weights)
    t.memorize_finish()
    mean, M2, n = t._weighted_stats()
    assert n == weights.sum()
    assert np.allclose(mean, expanded.mean(0))
    assert np.allclose(M2 / (n - 1), expanded.var(0, ddof=1))

    expected = (x - expanded.mean(0)) / expanded.std(0, ddof=1)
    assert np.allclose(t.transform(x, ddof=1), expected)
    # weights are not accepted when applying the fitted transform
    pytest.raises(ValueError, t.transform, x, ddof=1, weights=weights)


def test_Standardize_weights_chunk_order_invariant():
    # Same rows fit through different legal chunk orderings must give the
    # same learned statistics, including chunks that contain only zero-weight
    # rows and weights differing by several orders of magnitude.
    rng = np.random.default_rng(2)
    x = rng.normal(size=(12, 2))
    weights = np.array([1, 100000, 3, 0, 7, 2, 50000, 0, 1, 4, 2, 9], dtype=np.int64)

    def fit(slices):
        t = Standardize()
        for sl in slices:
            t.memorize_chunk(x[sl], ddof=1, weights=weights[sl])
        t.memorize_finish()
        return t._weighted_stats()

    order_a = [slice(0, 4), slice(4, 8), slice(8, 12)]
    order_b = [slice(8, 12), slice(0, 4), slice(4, 8)]
    order_c = [slice(0, 12)]
    stats = [fit(order) for order in (order_a, order_b, order_c)]
    for mean, M2, n in stats[1:]:
        assert n == stats[0][2]
        # Different chunk orderings pick a different shifted origin, so only
        # floating-point roundoff may separate the results (well within a
        # single ULP relative to the magnitude of the accumulators).
        assert np.allclose(mean, stats[0][0], rtol=1e-10, atol=1e-12)
        assert np.allclose(M2, stats[0][1], rtol=1e-10)

    # An all-zero-weight chunk (the zero-weight row at index 3) appearing
    # between real chunks is a no-op and loses no observation.
    t = Standardize()
    t.memorize_chunk(x[:3], ddof=1, weights=weights[:3])
    t.memorize_chunk(x[3:4], ddof=1, weights=weights[3:4])
    t.memorize_chunk(x[4:], ddof=1, weights=weights[4:])
    t.memorize_finish()
    assert t._weighted_stats()[2] == weights.sum()


def test_Standardize_weights_huge_baseline_tiny_differences():
    # Values clustered tightly around a very large common baseline. The
    # shifted-origin accumulator must stay accurate where a naive two-pass
    # subtraction of two ~1e12 numbers would lose precision.
    rng = np.random.default_rng(3)
    baseline = 1e12
    x = baseline + rng.normal(scale=1e-4, size=(15, 2))
    weights = rng.integers(0, 10, size=15).astype(np.int64)
    weights[0] = 100000
    weights[4] = 0

    t = Standardize()
    t.memorize_chunk(x, ddof=1, weights=weights)
    t.memorize_finish()
    mean, M2, n = t._weighted_stats()

    # Stable frequency-weighted reference computed in the same shifted space.
    origin = x[np.nonzero(weights)[0][0]]
    d = x - origin
    total_n = weights.sum()
    T = (weights[:, None] * d).sum(0)
    S = (weights[:, None] * d * d).sum(0)
    ref_mean = origin + T / total_n
    ref_var = (S - T**2 / total_n) / (total_n - 1)
    assert np.allclose(mean, ref_mean, rtol=1e-12, atol=1e-9)
    assert np.allclose(M2 / (n - 1), ref_var, rtol=1e-10)


def test_Standardize_weights_scale_undefined_rejected():
    x = np.arange(6.0).reshape(3, 2)
    # Total effective frequency equal to ddof -> scale denominator is zero.
    t = Standardize()
    t.memorize_chunk(x, ddof=1, weights=np.array([1, 0, 0]))
    pytest.raises(ValueError, t.memorize_finish)
    # Every weight zero -> no observations at all.
    t2 = Standardize()
    t2.memorize_chunk(x, ddof=1, weights=np.zeros(3, dtype=int))
    pytest.raises(ValueError, t2.memorize_finish)
    # Rescaling disabled means a single observation is fine for centering.
    t3 = Standardize()
    t3.memorize_chunk(x, ddof=1, rescale=False, weights=np.array([1, 0, 0]))
    t3.memorize_finish()
    assert np.allclose(t3.transform(x[:1], ddof=1, rescale=False), 0.0)


def test_standardize_wrapper_weights():
    x = np.array([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
    weights = np.array([1, 2, 3], dtype=np.int64)
    expanded = _expanded(x, weights)
    out = standardize(x, ddof=1, weights=weights)
    assert np.allclose(
        out, (x - expanded.mean(0)) / expanded.std(0, ddof=1), atol=1e-10
    )
    # Default unweighted behavior is unchanged.
    plain = standardize(x, ddof=1)
    assert np.allclose(plain, (x - x.mean(0)) / x.std(0, ddof=1), atol=1e-10)


def test_invalid_weights_rejected():
    x = np.arange(6.0).reshape(3, 2)
    bad = [
        np.array([1, -1, 0]),  # negative
        np.array([1.0, 2.0, 0.0]),  # non-integer float
        np.array([True, True, False]),  # booleans are not frequencies
        np.array([1, 2]),  # wrong number of rows
        np.ones((3, 2), dtype=int),  # multiple columns
        np.array(["1", "2", "3"]),  # non-numeric
    ]
    for weights in bad:
        t = Standardize()
        pytest.raises(ValueError, t.memorize_chunk, x, weights=weights)
        c = Center()
        pytest.raises(ValueError, c.memorize_chunk, x, weights=weights)

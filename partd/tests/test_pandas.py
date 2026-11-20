import pytest
pytest.importorskip('pandas')  # noqa

import numpy as np
import pandas as pd
import pandas.testing as tm
import os

try:
    import pyarrow as pa
except ImportError:
    pa = None

import partd
from partd.pandas import PandasColumns, PandasBlocks, serialize, deserialize
from partd.pandas import dumps, pnp


df1 = pd.DataFrame({'a': [1, 2, 3],
                    'b': [1., 2., 3.],
                    'c': ['x', 'y', 'x']}, columns=['a', 'b', 'c'],
                    index=pd.Index([1, 2, 3], name='myindex'))

df2 = pd.DataFrame({'a': [10, 20, 30],
                    'b': [10., 20., 30.],
                    'c': ['X', 'Y', 'X']}, columns=['a', 'b', 'c'],
                    index=pd.Index([10, 20, 30], name='myindex'))


def test_PandasColumns():
    with PandasColumns() as p:
        assert os.path.exists(p.partd.partd.path)

        p.append({'x': df1, 'y': df2})
        p.append({'x': df2, 'y': df1})
        assert os.path.exists(p.partd.partd.filename('x'))
        assert os.path.exists(p.partd.partd.filename(('x', 'a')))
        assert os.path.exists(p.partd.partd.filename(('x', '.index')))
        assert os.path.exists(p.partd.partd.filename('y'))

        result = p.get(['y', 'x'])
        tm.assert_frame_equal(result[0], pd.concat([df2, df1]))
        tm.assert_frame_equal(result[1], pd.concat([df1, df2]))

        with p.lock:  # uh oh, possible deadlock
            result = p.get(['x'], lock=False)

    assert not os.path.exists(p.partd.partd.path)


def test_column_selection():
    with PandasColumns('foo') as p:
        p.append({'x': df1, 'y': df2})
        p.append({'x': df2, 'y': df1})
        result = p.get('x', columns=['c', 'b'])
        tm.assert_frame_equal(result, pd.concat([df1, df2])[['c', 'b']])


def test_PandasBlocks():
    with PandasBlocks() as p:
        assert os.path.exists(p.partd.path)

        p.append({'x': df1, 'y': df2})
        p.append({'x': df2, 'y': df1})
        assert os.path.exists(p.partd.filename('x'))
        assert os.path.exists(p.partd.filename('y'))

        result = p.get(['y', 'x'])
        tm.assert_frame_equal(result[0], pd.concat([df2, df1]))
        tm.assert_frame_equal(result[1], pd.concat([df1, df2]))

        with p.lock:  # uh oh, possible deadlock
            result = p.get(['x'], lock=False)

    assert not os.path.exists(p.partd.path)


@pytest.mark.parametrize('ordered', [False, True])
def test_serialize_categoricals(ordered):
    frame = pd.DataFrame({'x': [1, 2, 3, 4],
                          'y': pd.Categorical(['c', 'a', 'b', 'a'],
                                              ordered=ordered)},
                          index=pd.Categorical(['x', 'y', 'z', 'x'],
                                                ordered=ordered))
    frame.index.name = 'foo'
    frame.columns.name = 'bar'

    for ind, df in [(0, frame), (1, frame.T)]:
        df2 = deserialize(serialize(df))
        tm.assert_frame_equal(df, df2)


def test_serialize_multi_index():
    df = pd.DataFrame({'x': ['a', 'b', 'c', 'a', 'b', 'c'],
                       'y': [1, 2, 3, 4, 5, 6],
                       'z': [7., 8, 9, 10, 11, 12]})
    df = df.groupby([df.x, df.y]).sum()
    df.index.name = 'foo'
    df.columns.name = 'bar'

    df2 = deserialize(serialize(df))
    tm.assert_frame_equal(df, df2)


@pytest.mark.parametrize('base', [
    pd.Timestamp('1987-03-3T01:01:01+0001'),
    pd.Timestamp('1987-03-03 01:01:01-0600', tz='US/Central'),
])
def test_serialize(base):
    df = pd.DataFrame({'x': [
        base + pd.Timedelta(seconds=i)
        for i in np.random.randint(0, 1000, size=10)],
                       'y': list(range(10)),
                       'z': pd.date_range('2017', periods=10)})
    df2 = deserialize(serialize(df))
    tm.assert_frame_equal(df, df2)


def test_other_extension_types():
    pytest.importorskip("pandas", minversion="0.25.0")
    a = pd.array([pd.Period("2000"), pd.Period("2001")])
    df = pd.DataFrame({"A": a})
    df2 = deserialize(serialize(df))
    tm.assert_frame_equal(df, df2)

@pytest.mark.parametrize("dtype", ["Int64", "Int32", "Float64", "Float32"])
def test_index_numeric_extension_types(dtype):
    pytest.importorskip("pandas", minversion="1.4.0")

    df = pd.DataFrame({"x": [1, 2, 3]}, index=[4, 5, 6])
    df.index = df.index.astype(dtype)
    df2 = deserialize(serialize(df))
    tm.assert_frame_equal(df, df2)
    
@pytest.mark.parametrize(
    "dtype",
    [
        "string[python]",
        pytest.param(
            "string[pyarrow]",
            marks=pytest.mark.skipif(pa is None, reason="Requires pyarrow"),
        ),
    ],
)
def test_index_non_numeric_extension_types(dtype):
    pytest.importorskip("pandas", minversion="1.4.0")
    df = pd.DataFrame({"x": [1, 2, 3]}, index=["a", "b", "c"])
    df.index = df.index.astype(dtype)
    df2 = deserialize(serialize(df))
    tm.assert_frame_equal(df, df2)


def test_categorical_concat():
    pytest.importorskip("pandas", minversion="2")

    df1 = pd.DataFrame({"a": ["x", "y"]}, dtype="category")
    df2 = pd.DataFrame({"a": ["y", "z"]}, dtype="category")

    with PandasBlocks() as p:
        p.append({'x': df1})
        p.append({'x': df2})

        result = p.get(["x"])
        pd.testing.assert_frame_equal(result[0], pd.concat([df1, df2]).astype("category"))


# ---------------------------------------------------------------------------
# Positional range reads / column projection over appended blocks
# ---------------------------------------------------------------------------

def _range_frame(rows, seed):
    """A block with a duplicated, non-monotonic business index."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            'a': rng.integers(0, 1000, size=rows),
            'b': rng.random(size=rows),
            'c': rng.integers(0, 5, size=rows),
            # big column that must never be read when not projected
            'big': np.arange(rows, dtype='int64') + seed * 1000,
        },
        index=pd.Index(rng.integers(0, 5, size=rows).tolist(),
                       name='dupe-index'),
    )


def _three_blocks():
    first = _range_frame(40, 1)
    second = _range_frame(7, 2)
    third = _range_frame(11, 3)
    return first, second, third


def test_range_crosses_block_boundary_and_projects_columns():
    first, second, third = _three_blocks()
    with PandasColumns() as p:
        for df in (first, second, third):
            p.append({'x': df})

        full = pd.concat([first, second, third])
        # window starts inside the second block and ends inside the third
        start, stop = len(first) - 2, len(first) + len(second) + 3
        result = p.get('x', columns=['c', 'a'],
                       range=slice(start, stop))

        expected = full.iloc[start:stop][['c', 'a']]
        tm.assert_frame_equal(result, expected)
        assert result.index.name == 'dupe-index'
        # every duplicate index occurrence stays at its own position
        assert list(result.index) == list(expected.index)

        # positional, not label based: a value slice that also happens to be
        # a business index value must not be interpreted as labels.
        assert p.get('x', columns=['a'], start=5, stop=9).equals(
            full.iloc[5:9][['a']])


def test_range_does_not_read_non_overlapping_or_unselected_data(monkeypatch):
    first, second, third = _three_blocks()
    with PandasColumns() as p:
        for df in (first, second, third):
            p.append({'x': df})

        first_span = p._read_manifest('x')['blocks'][0]['fields'][('x', 'a')]
        first_end = first_span[0] + first_span[1]

        reads = []
        original = p.partd.partd._get_range

        def witness(key, offset, nbytes, **kwargs):
            reads.append((key, offset, nbytes))
            return original(key, offset, nbytes, **kwargs)

        monkeypatch.setattr(p.partd.partd, '_get_range', witness)

        start, stop = len(first) + len(second) - 2, len(first) + len(second) + 3
        result = p.get('x', columns=['a'], range=slice(start, stop))

        touched = {key for key, _, _ in reads}
        assert ('x', 'big') not in touched   # unselected column
        assert ('x', 'c') not in touched
        assert ('x', 'b') not in touched
        # the first (non-overlapping) block's fields are never read
        for key, offset, nbytes in reads:
            if key == ('x', 'a'):
                assert offset >= first_end
        assert not p.last_read_fallback
        expected = pd.concat([first, second, third]).iloc[start:stop][['a']]
        tm.assert_frame_equal(result, expected)


def test_range_does_not_read_unselected_column_metadata(monkeypatch):
    first, second, third = _three_blocks()
    with PandasColumns() as p:
        for df in (first, second, third):
            p.append({'x': df})

        names = []
        original = p.partd.partd._get

        def witness(keys, **kwargs):
            names.extend(keys)
            return original(keys, **kwargs)

        monkeypatch.setattr(p.partd.partd, '_get', witness)
        p.get('x', columns=['a'], range=slice(0, 3))
        assert partd.utils.suffix(('x', 'big'), '.dtype') not in names
        assert partd.utils.suffix(('x', 'c'), '.dtype') not in names


@pytest.mark.parametrize('boundary', ['a', 'b', 'partial'])
def test_partial_append_is_invisible_until_committed(monkeypatch, boundary):
    first, second, third = _three_blocks()
    with PandasColumns() as p:
        p.append({'x': first})
        p.append({'x': second})

        committed = pd.concat([first, second])

        # Simulate the third block's bytes hitting disk while the append is
        # still in flight: never publish a manifest for it.
        arrays = {('x', col): third[col].values for col in third.columns}
        arrays[('x', '.index')] = third.index.values
        serialized = {}
        for fkey, values in arrays.items():
            p.partd.partd.iset(
                partd.utils.suffix(fkey, '.dtype'),
                pnp.serialize_dtype(values.dtype), lock=False)
            serialized[fkey] = pnp.serialize(values)
        if boundary == 'partial':
            # A torn write: only a prefix of one field reached the disk while
            # every other field is entirely absent.
            torn = serialized[('x', 'a')]
            to_write = {('x', 'a'): torn[:len(torn) // 2]}
        elif boundary == 'index':
            to_write = {('x', '.index'): serialized[('x', '.index')]}
        else:
            # One field landed, the rest did not (fields are separate files).
            to_write = {('x', boundary): serialized[('x', boundary)]}
        p.partd.partd.append(to_write, lock=False)

        # A range covering the uncommitted block only sees committed rows.
        result = p.get('x', columns=['a', 'c'], range=slice(0, None))
        tm.assert_frame_equal(result, committed[['a', 'c']])

        # Nor may one field's new block be stitched onto another field's old
        # positions.  A plain get sees the same committed rows only.
        assert len(result) == len(committed)
        tm.assert_frame_equal(p.get('x'), committed)

        # A fresh append commits and is then visible.
        p.append({'x': third})
        result = p.get('x', columns=['a', 'c'], range=slice(0, None))
        tm.assert_frame_equal(result, pd.concat([first, second, third])[['a', 'c']])
        window = p.get('x', columns=['a', 'c'],
                       range=slice(len(committed) - 1, len(committed) + 2))
        tm.assert_frame_equal(
            window,
            pd.concat([first, second, third]).iloc[
                len(committed) - 1: len(committed) + 2][['a', 'c']])
        tm.assert_frame_equal(p.get('x'), pd.concat([first, second, third]))


def test_reopened_directory_reads_ranges(tmp_path):
    first, second, third = _three_blocks()
    path = str(tmp_path / 'store')
    p = PandasColumns(path)
    for df in (first, second, third):
        p.append({'x': df})

    # A brand new instance (as a later process would create) sees the same
    # positional layout.
    p2 = PandasColumns(path)
    try:
        full = pd.concat([first, second, third])
        result = p2.get('x', columns=['b'], range=slice(38, 46))
        tm.assert_frame_equal(result, full.iloc[38:46][['b']])
        # plain gets keep working
        tm.assert_frame_equal(p2.get('x'), full)
    finally:
        p2.drop()


def test_legacy_directory_falls_back_explicitly():
    first, second = _three_blocks()[:2]
    with PandasColumns() as p:
        # Write data without a manifest, as an older partd would have.
        p.partd.append({('x', col): first[col].values for col in first.columns})
        p.partd.append({('x', '.index'): first.index.values})
        p.iset(('x', '.columns'), dumps(['a', 'b', 'c', 'big']))
        p.iset(('x', '.index-name'), dumps('dupe-index'))
        assert p._read_manifest('x') is None

        result = p.get('x', columns=['a'], range=slice(1, 3))
        assert p.last_read_fallback
        tm.assert_frame_equal(result, first.iloc[1:3][['a']])

        # Appending to a legacy directory keeps the old data readable and
        # does not pretend it can be located cheaply.
        p.append({'x': second})
        assert p._read_manifest('x')['legacy']
        result = p.get('x', columns=['a'], range=slice(0, None))
        assert p.last_read_fallback
        tm.assert_frame_equal(result,
                              pd.concat([first, second])[['a']])
        tm.assert_frame_equal(p.get('x'), pd.concat([first, second]))


def test_multi_key_ranges_are_independent():
    first, second, third = _three_blocks()
    with PandasColumns() as p:
        p.append({'x': first})
        p.append({'x': second})
        p.append({'y': third})

        got = p.get(['x', 'y'], columns=['a', 'c'],
                    ranges={'x': slice(len(first) - 1, len(first) + 2),
                            'y': slice(1, 4)})
        tm.assert_frame_equal(
            got[0],
            pd.concat([first, second]).iloc[len(first) - 1:len(first) + 2][
                ['a', 'c']])
        tm.assert_frame_equal(got[1], third.iloc[1:4][['a', 'c']])

        # A key without an explicit range is read in full.
        got = p.get(['x', 'y'], columns=['a'], ranges={'x': slice(0, 2)})
        assert len(got[0]) == 2
        assert len(got[1]) == len(third)


def test_range_in_last_block_skips_earlier_blocks(monkeypatch):
    first, second, third = _three_blocks()
    with PandasColumns() as p:
        for df in (first, second, third):
            p.append({'x': df})

        first_two_end = len(first) + len(second)
        reads = []
        original = p.partd.partd._get_range

        def witness(key, offset, nbytes, **kwargs):
            reads.append((key, offset, nbytes))
            return original(key, offset, nbytes, **kwargs)

        monkeypatch.setattr(p.partd.partd, '_get_range', witness)
        result = p.get('x', columns=['a'], range=slice(first_two_end, None))
        tm.assert_frame_equal(
            result, pd.concat([first, second, third]).iloc[first_two_end:][['a']])

        for key, offset, nbytes in reads:
            if key == ('x', 'a'):
                span = p._read_manifest('x')['blocks'][2]['fields'][key]
                assert offset == span[0]


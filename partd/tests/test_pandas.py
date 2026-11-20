import pytest
pytest.importorskip('pandas')  # noqa

import numpy as np
import pandas as pd
import pandas.testing as tm
import os
import shutil
import tempfile

try:
    import pyarrow as pa
except ImportError:
    pa = None

from partd.pandas import PandasColumns, PandasBlocks, AmbiguousColumnError, serialize, deserialize


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


def _duplicate_amount_frame(amounts, notes, originals, index_values):
    frame = pd.DataFrame(
        {'amount_local': amounts, 'note': notes, 'amount_original': originals})
    frame.columns = ['amount', 'note', 'amount']
    frame.index = pd.Index(index_values, name='idx')
    return frame


block1 = _duplicate_amount_frame(
    [1, 2, 3], ['a', 'b', 'c'], [1.5, 2.5, 3.5], [10, 10, 30])
block2 = _duplicate_amount_frame([4], ['d'], [4.5], [40])


def _snapshot(path):
    files = {}
    for root, _, names in os.walk(path):
        for name in names:
            if name == '.lock':
                continue
            full = os.path.join(root, name)
            with open(full, 'rb') as f:
                files[os.path.relpath(full, path)] = f.read()
    return files


def test_duplicate_column_names_stored_and_read_by_position():
    path = tempfile.mkdtemp()
    try:
        p = PandasColumns(path)
        p.append({'r': block1})
        p.append({'r': block2})
        # positional storage, never a shared file keyed by the label
        assert os.path.exists(
            p.partd.partd.filename(('r', '.layout')))
        assert os.path.exists(
            p.partd.partd.filename(('r', '.col', 0)))
        assert os.path.exists(
            p.partd.partd.filename(('r', '.col', 2)))
        del p  # close the instance; an explicit path must persist

        expected = pd.concat([block1, block2])

        # reopen the store from disk and read the whole table back
        p = PandasColumns(path)
        full = p.get(['r'])[0]
        tm.assert_frame_equal(full, expected)
        assert list(full.columns) == ['amount', 'note', 'amount']
        assert full.iloc[:, 0].dtype == np.dtype('int64')
        assert full.iloc[:, 1].dtype == np.dtype('object')
        assert full.iloc[:, 2].dtype == np.dtype('float64')
        assert full.index.name == 'idx'
        tm.assert_index_equal(full.index, expected.index)
        assert full.index.tolist() == [10, 10, 30, 40]

        # read only the third column
        third = p.get('r', columns=[2])
        tm.assert_frame_equal(third, expected.iloc[:, [2]])
        assert third.iloc[:, 0].dtype == np.dtype('float64')
        tm.assert_index_equal(third.index, expected.index)

        # third, first, third: repeated positions stay separate columns
        projected = p.get('r', columns=[2, 0, 2])
        tm.assert_frame_equal(projected, expected.iloc[:, [2, 0, 2]])
        assert projected.shape == (4, 3)
        assert list(projected.columns) == ['amount', 'amount', 'amount']
        assert [projected.iloc[:, i].dtype
                for i in range(3)] == [np.dtype('float64'),
                                        np.dtype('int64'),
                                        np.dtype('float64')]
        tm.assert_series_equal(projected.iloc[:, 0], projected.iloc[:, 2],
                               check_names=False)
        del p
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_type_swapped_append_is_rejected_without_writing():
    path = tempfile.mkdtemp()
    try:
        p = PandasColumns(path)
        p.append({'r': block1})
        p.append({'r': block2})
        expected = pd.concat([block1, block2])
        before = _snapshot(path)

        swapped = _duplicate_amount_frame(
            [9.5], ['z'], [9], [99])  # int/float positions exchanged

        with pytest.raises(ValueError):
            p.append({'r': swapped})

        # no column or index byte was touched
        assert _snapshot(path) == before
        tm.assert_frame_equal(p.get(['r'])[0], expected)

        # a block with the wrong number of columns is rejected as well
        narrow = pd.DataFrame({'a': [1], 'b': ['x']})
        narrow.columns = ['amount', 'note']
        narrow.index = pd.Index([99], name='idx')
        with pytest.raises(ValueError):
            p.append({'r': narrow})
        assert _snapshot(path) == before
        del p
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_legacy_unique_named_partition_keeps_name_lookups():
    path = tempfile.mkdtemp()
    try:
        p = PandasColumns(path)
        p.append({'old': df1})
        p.append({'old': df2})
        # unique names keep the historical per-label file layout
        assert not os.path.exists(
            p.partd.partd.filename(('old', '.layout')))
        assert os.path.exists(p.partd.partd.filename(('old', 'a')))
        del p

        p = PandasColumns(path)
        tm.assert_frame_equal(p.get(['old'])[0],
                              pd.concat([df1, df2]))
        tm.assert_frame_equal(
            p.get('old', columns=['c', 'b']),
            pd.concat([df1, df2])[['c', 'b']])
        del p
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_duplicate_name_lookup_is_explicitly_ambiguous():
    path = tempfile.mkdtemp()
    try:
        p = PandasColumns(path)
        p.append({'r': block1})
        p.append({'r': block2})

        with pytest.raises(AmbiguousColumnError):
            p.get('r', columns=['amount'])

        # an unambiguous label still resolves inside the new partition
        note = p.get('r', columns=['note'])
        tm.assert_frame_equal(
            note, pd.concat([block1, block2]).iloc[:, [1]])

        # explicit positional access remains the way to disambiguate
        local = p.get('r', columns=[0])
        original = p.get('r', columns=[2])
        assert local.iloc[:, 0].dtype == np.dtype('int64')
        assert original.iloc[:, 0].dtype == np.dtype('float64')
        del p
    finally:
        shutil.rmtree(path, ignore_errors=True)

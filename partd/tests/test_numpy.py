import pytest
np = pytest.importorskip('numpy')  # noqa

import pickle

import partd
from partd import File
from partd.numpy import Numpy, DtypeMismatchError


def test_numpy():
    dt = np.dtype([('a', 'i4'), ('b', 'i2'), ('c', 'f8')])
    with Numpy() as p:
        p.append({'a': np.array([10, 20, 30], dtype=dt['a']),
                  'b': np.array([ 1,  2,  3], dtype=dt['b']),
                  'c': np.array([.1, .2, .3], dtype=dt['c'])})
        p.append({'a': np.array([70, 80, 90], dtype=dt['a']),
                  'b': np.array([ 7,  8,  9], dtype=dt['b']),
                  'c': np.array([.7, .8, .9], dtype=dt['c'])})

        result = p.get(['a', 'c'])
        assert (result[0] == np.array([10, 20, 30, 70, 80, 90],dtype=dt['a'])).all()
        assert (result[1] == np.array([.1, .2, .3, .7, .8, .9],dtype=dt['c'])).all()

        with p.lock:  # uh oh, possible deadlock
            result = p.get(['a'], lock=False)


def test_nested():
    with Numpy() as p:
        p.append({'x': np.array([1, 2, 3]),
                 ('y', 1): np.array([4, 5, 6]),
                 ('z', 'a', 3): np.array([.1, .2, .3])})
        assert (p.get(('z', 'a', 3)) == np.array([.1, .2, .3])).all()


def test_serialization():
    with Numpy() as p:
        p.append({'x': np.array([1, 2, 3])})
        q = pickle.loads(pickle.dumps(p))
        assert (q.get('x') == [1, 2, 3]).all()


array_of_lists = np.empty(3, dtype='O')
array_of_lists[:] = [[1, 2], [3, 4], [5, 6]]


@pytest.mark.parametrize('x', [np.array(['Alice', 'Bob', 'Charlie'], dtype='O'),
                               array_of_lists])
def test_object_dtype(x):
    with Numpy() as p:
        p.append({'x': x})
        p.append({'x': x})
        assert isinstance(p.get('x'), np.ndarray)
        assert (p.get('x') == np.concatenate([x, x])).all()


def test_datetime_types():
    x = np.array(['2014-01-01T12:00:00'], dtype='M8[us]')
    y = np.array(['2014-01-01T12:00:00'], dtype='M8[s]')
    with Numpy() as p:
        p.append({'x': x, 'y': y})
        assert p.get('x').dtype == x.dtype
        assert p.get('y').dtype == y.dtype


def test_non_utf8_bytes():
    a = np.array([b'\xc3\x28', b'\xa0\xa1', b'\xe2\x28\xa1', b'\xe2\x82\x28',
                  b'\xf0\x28\x8c\xbc'], dtype='O')
    s = partd.numpy.serialize(a)
    assert (partd.numpy.deserialize(s, 'O') == a).all()


# A padded structured layout: big-endian integer, a gap, then a little-endian
# 2-D subarray, with trailing padding up to an explicit itemsize.
LAYOUT = {'names': ['stamp', 'samples'],
          'formats': ['>i8', ('<f8', (2, 3))],
          'offsets': [0, 32],
          'itemsize': 80}


def layout_dtype(**overrides):
    spec = dict(LAYOUT)
    spec.update(overrides)
    return np.dtype(spec)


def sample_block(start):
    x = np.zeros(3, dtype=layout_dtype())
    x['stamp'] = np.array([start, start + 1, start + 2], dtype='>i8')
    x['samples'] = np.arange(start * 100, start * 100 + 18,
                             dtype='<f8').reshape(3, 2, 3)
    return x


def test_structured_layout_roundtrip(tmp_path):
    dt = layout_dtype()

    p = Numpy(str(tmp_path))
    p.append({'x': sample_block(0)})
    p.append({'x': sample_block(10)})
    # Re-open the directory as a fresh reader.
    p = Numpy(str(tmp_path))
    result = p.get('x')
    assert result.dtype == dt
    assert result.dtype.itemsize == 80
    assert result.strides == (80,)
    assert result.dtype.fields['stamp'][1] == 0
    assert result.dtype.fields['samples'][1] == 32
    assert result.dtype.fields['stamp'][0] == np.dtype('>i8')
    samples = result.dtype.fields['samples'][0]
    assert samples.subdtype == (np.dtype('<f8'), (2, 3))
    assert result.shape == (6,)
    assert (result['stamp'] == np.array([0, 1, 2, 10, 11, 12],
                                        dtype='>i8')).all()
    assert result['samples'].shape == (6, 2, 3)
    assert (result['samples'][:, 0, 0] ==
            np.array([0, 6, 12, 1000, 1006, 1012])).all()


def test_padding_bytes_are_ignored(tmp_path):
    dt = layout_dtype()

    p = Numpy(str(tmp_path))
    p.append({'x': sample_block(0)})

    # Same layout (same stride/offsets), different padding contents.
    raw = bytearray(sample_block(20).tobytes())
    for record in range(3):
        base = record * dt.itemsize
        raw[base + 8:base + 32] = b'\xaa' * 24
    block = np.frombuffer(bytes(raw), dtype=dt)
    p.append({'x': block})

    p = Numpy(str(tmp_path))
    result = p.get('x')
    assert (result['stamp'] == np.array([0, 1, 2, 20, 21, 22],
                                        dtype='>i8')).all()
    assert (result['samples'][3:] ==
            sample_block(20)['samples']).all()


def test_incompatible_layout_rejected_before_write(tmp_path):
    p = Numpy(str(tmp_path))
    p.append({'x': sample_block(0)})
    good = p.get('x')

    # Same names, values and total length, but the fields moved.
    shifted = layout_dtype(offsets=[8, 0])
    shifted_block = np.zeros(3, dtype=shifted)
    shifted_block['stamp'] = np.array([30, 31, 32], dtype='>i8')
    shifted_block['samples'] = np.arange(3000, 3018,
                                         dtype='<f8').reshape(3, 2, 3)

    p = Numpy(str(tmp_path))
    with pytest.raises(DtypeMismatchError) as info:
        p.append({'x': shifted_block})
    message = str(info.value)
    assert "stamp" in message
    assert "offset differs" in message
    assert "itemsize" in message

    # Nothing was written; the readable block is untouched.
    p = Numpy(str(tmp_path))
    assert (p.get('x') == good).all()


def test_incompatible_layout_does_not_touch_other_keys(tmp_path):
    p = Numpy(str(tmp_path))
    p.append({'x': sample_block(0), 'y': sample_block(100)})

    shifted = layout_dtype(offsets=[16, 0], itemsize=88)
    incompatible = np.zeros(3, dtype=shifted)
    incompatible['stamp'] = np.array([1, 2, 3], dtype='>i8')

    p = Numpy(str(tmp_path))
    with pytest.raises(DtypeMismatchError):
        p.append({'x': incompatible})
    assert (p.get('y') == sample_block(100)).all()
    assert (p.get('x') == sample_block(0)).all()


def test_plain_old_directory_still_readable(tmp_path):
    # An old partition with no structured fields: plain scalar dtype stored
    # as the bare dtype string, exactly as older versions wrote it.
    store = File(str(tmp_path))
    store.append({'plain': np.arange(4, dtype='<i4').tobytes()})
    store.iset('plain.dtype', b'<i4')

    p = Numpy(str(tmp_path))
    result = p.get('plain')
    assert result.dtype == np.dtype('<i4')
    assert (result == np.arange(4)).all()

    # Appending a compatible plain block still works without rewriting.
    p.append({'plain': np.arange(4, 8, dtype='<i4')})
    assert (p.get('plain') == np.arange(8)).all()


def test_legacy_structured_dtype_file_still_readable(tmp_path):
    dt = np.dtype([('a', 'i4'), ('b', '<f8')])
    x = np.zeros(2, dtype=dt)
    x['a'] = [1, 2]
    x['b'] = [0.5, 1.5]

    store = File(str(tmp_path))
    store.append({'x': x.tobytes()})
    store.iset('x.dtype', repr(dt.descr).encode())

    p = Numpy(str(tmp_path))
    result = p.get('x')
    assert result.dtype == dt
    assert (result == x).all()

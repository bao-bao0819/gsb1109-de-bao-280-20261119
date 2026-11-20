from partd.file import File, encode_key

import pickle
import pytest
import shutil
import tempfile
import os


def test_partd():
    with File() as p:
        p.append({'x': b'Hello', 'y': b'abc'})
        p.append({'x': b'World!', 'y': b'def'})
        assert os.path.exists(p.filename('x'))
        assert os.path.exists(p.filename('y'))

        result = p.get(['y', 'x'])
        assert result == [b'abcdef', b'HelloWorld!']

        assert p.get('z') == b''

        with p.lock:  # uh oh, possible deadlock
            result = p.get(['x'], lock=False)

    assert not os.path.exists(p.path)


def test_key_tuple():
    with File() as p:
        p.append({('a', 'b'): b'123'})
        assert os.path.exists(p.filename(('a', 'b')))


def test_iset():
    with File() as p:
        p.iset('x', b'123')
        assert 'x' in p._iset_seen
        assert 'y' not in p._iset_seen
        p.iset('x', b'123')
        p.iset('x', b'123')
        assert p.get('x') == b'123'


def test_nested_get():
    with File() as p:
        p.append({'x': b'1', 'y': b'2', 'z': b'3'})
        assert p.get(['x', ['y', 'z']]) == [b'1', [b'2', b'3']]


def test_drop():
    with File() as p:
        p.append({'x': b'123'})
        p.iset('y', b'abc')
        assert p.get('x') == b'123'
        assert p.get('y') == b'abc'

        p.drop()
        assert p.get('x') == b''
        assert p.get('y') == b''

        p.append({'x': b'123'})
        p.iset('y', b'def')
        assert p.get('x') == b'123'
        assert p.get('y') == b'def'


def test_del():
    f = File()

    assert f.path
    assert os.path.exists(f.path)

    f.__del__()
    assert not os.path.exists(f.path)

    with File('Foo') as p:
        p.__del__()
        assert os.path.exists(p.path)


def test_specify_dirname():
    with File(dir=os.getcwd()) as f:
        assert os.getcwd() in f.path


def test_encode_key_injective():
    keys = ['NW1', 'NW#1', 7, '7', -7, '', ('NW', '1'), 'NW/1',
            ('a', ('b', 'c')), ('a', ('b',), 'c'), (('a', 'b'), 'c'),
            ('a', 'b'), ('a', 'b', 'c'), ()]
    assert len(keys) == len({encode_key(key) for key in keys})


def test_safe_keys_reject_unsupported_types():
    with File(legacy_keys=()) as f:
        for bad_key in [1.5, b'x', None, True, ['a'], ('a', 1.5), (b'x',)]:
            with pytest.raises(TypeError):
                f.filename(bad_key)


def test_safe_keys_pickle_roundtrip():
    path = tempfile.mkdtemp(suffix='.partd')
    try:
        f = File(path, legacy_keys={'NW1'})
        f.append({'NW#1': b'123'})
        g = pickle.loads(pickle.dumps(f))
        assert g._legacy_keys == frozenset({'NW1'})
        assert g.get('NW#1') == b'123'
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_safe_keys_coexist_with_legacy_batch():
    path = tempfile.mkdtemp(suffix='.partd')
    try:
        # Old batch written through the old interface.  'NW1' is later
        # confirmed to belong to the string key 'NW1'; 'mystery' stays
        # unattributed.
        old = File(path)
        old.append({'NW1': b'old-nw1', 'mystery': b'unknown-owner'})
        del old

        # New batch in the same directory, keeping key types/characters.
        f = File(path, legacy_keys={'NW1'})
        f.append({'NW#1': b'hash', 7: b'int-seven', '7': b'str-seven',
                  ('NW', '1'): b'tuple', 'NW/1': b'path-string'})
        del f

        # Reopen: append to the known old batch, delete two new keys.
        f = File(path, legacy_keys={'NW1'})
        f.append({'NW1': b'+more'})
        f.delete(['NW#1', 7])
        assert f.get('NW#1') == b''
        assert f.get(7) == b''
        assert f.get('7') == b'str-seven'
        assert f.get(('NW', '1')) == b'tuple'
        assert f.get('NW/1') == b'path-string'
        assert f.get('NW1') == b'old-nw1+more'
        del f

        # Reopen again: everything survives restarts.
        f = File(path, legacy_keys={'NW1'})
        assert f.get('7') == b'str-seven'
        assert f.get(('NW', '1')) == b'tuple'
        assert f.get('NW/1') == b'path-string'
        assert f.get('NW1') == b'old-nw1+more'
        # Without attribution the look-alike old file is not a hit.
        assert f.get('mystery') == b''
        del f

        # Deleting the new keys did not delete or hide their look-alikes.
        with open(os.path.join(path, 'NW1'), 'rb') as fh:
            assert fh.read() == b'old-nw1+more'
        with open(os.path.join(path, 'mystery'), 'rb') as fh:
            assert fh.read() == b'unknown-owner'

        # The old interface still reads the old directory as before.
        g = File(path)
        assert g.get('NW1') == b'old-nw1+more'
        assert g.get('mystery') == b'unknown-owner'
        del g
    finally:
        shutil.rmtree(path, ignore_errors=True)

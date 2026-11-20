from partd.file import File

import shutil
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


def test_window_append_is_isolated_from_open_window():
    with File() as p:
        p.append({'x': b'0123456789'})
        window = p.open_window('x', blocksize=4)
        first = window.read()
        assert first.data == b'0123'
        assert first.position == 4
        assert first.status == 'data'

        p.append({'x': b'abcdefghij'})

        data, position = window.read(4, first.position)
        assert (data, position) == (b'4567', 8)
        assert window.read(-1, 8) == (b'89', 10)
        end = window.read(4, 10)
        assert end == (b'', 10)
        assert end.status == 'window_end'

        new = p.open_window('x', blocksize=4)
        assert new.size == 20
        assert b''.join(d for d, pos in new.iter_chunks()) == (
            b'0123456789abcdefghij')
        window.close()
        new.close()


def test_window_delete_recreate_is_isolated():
    with File() as p:
        p.append({'x': b'old-version'})
        old = p.open_window('x')
        assert old.read(3).data == b'old'

        p.delete('x')
        p.append({'x': b'new-version'})

        assert old.read(-1, 0).data == b'old-version'
        assert old.read(1, 11).status == 'window_end'

        new = p.open_window('x')
        assert new.read(-1).data == b'new-version'
        old.close()
        new.close()


def test_window_distinguishes_empty_and_missing():
    with File() as p:
        missing = p.open_window('missing')
        assert missing.read() == (b'', 0)
        assert missing.read().status == 'missing'

        p.iset('empty', b'')
        empty = p.open_window('empty')
        assert empty.read() == (b'', 0)
        assert empty.read().status == 'empty'

        assert p.get('missing') == b''
        assert p.get('empty') == b''
        missing.close()
        empty.close()

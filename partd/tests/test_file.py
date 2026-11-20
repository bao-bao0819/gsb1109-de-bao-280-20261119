from partd.file import File, CREATED, SAME, CONFLICT

import shutil
import os
import threading
from unittest import mock

import pytest


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


def test_create_created_same_conflict():
    with File() as p:
        assert p.create('format', b'v1') == CREATED
        assert p.get('format') == b'v1'

        assert p.create('format', b'v1') == SAME
        assert p.create('format', b'v2') == CONFLICT
        assert p.get('format') == b'v1'


def test_create_empty_existing_differs_from_missing():
    with File() as p:
        p.append({'empty': b''})
        assert p.create('empty', b'') == SAME
        assert p.create('empty', b'x') == CONFLICT

        assert p.create('missing', b'') == CREATED
        assert p.get('missing') == b''


def test_create_race_and_observer():
    contents = [b'format-version-1' * 1000, b'format-version-2' * 1000]
    with File() as p1:
        p2 = File(p1.path)
        observer = File(p1.path)
        barrier = threading.Barrier(2)
        results = {}
        seen = []
        done = []

        def writer(name, instance, content):
            barrier.wait()
            results[name] = instance.create('format', content)

        def reader():
            while not done:
                seen.append(observer.get('format'))

        threads = [threading.Thread(target=writer, args=(name, inst, content))
                   for name, inst, content in
                   [('a', p1, contents[0]), ('b', p2, contents[1])]]
        observer_thread = threading.Thread(target=reader)
        observer_thread.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        done.append(True)
        observer_thread.join()

        assert sorted(results.values()) == [CONFLICT, CREATED]
        winner = contents[0] if results['a'] == CREATED else contents[1]
        loser = contents[1] if results['a'] == CREATED else contents[0]

        assert set(seen) <= {b'', winner}
        assert p1.get('format') == winner

        assert p1.create('format', winner) == SAME
        assert p2.create('format', loser) == CONFLICT
        assert p1.get('format') == winner


def test_create_failure_before_publish_leaves_nothing():
    with File() as p:
        with mock.patch('partd.file.os.link',
                        side_effect=OSError('publish failed')):
            with pytest.raises(OSError):
                p.create('format', b'v1')

        assert not os.path.exists(p.filename('format'))
        assert p.get('format') == b''
        assert not [fn for fn in os.listdir(p.path) if fn != '.lock']

        assert p.create('format', b'v1') == CREATED
        assert p.get('format') == b'v1'


def test_create_compares_disk_bytes_after_reopen():
    with File() as p1:
        assert p1.create('format', b'v1') == CREATED

        p2 = File(p1.path)
        assert p2.create('format', b'v1') == SAME
        assert p2.create('format', b'v2') == CONFLICT
        assert p2.get('format') == b'v1'


def test_create_does_not_affect_append_get():
    with File() as p:
        p.create('format', b'v1')
        p.append({'x': b'Hello'})
        p.append({'x': b'World!'})
        assert p.get('x') == b'HelloWorld!'
        p.append({'format': b'-appended'})
        assert p.get('format') == b'v1-appended'

from partd.dict import Dict
from partd.file import File
from partd.buffer import Buffer, keys_to_flush
import pickle

import pytest

import shutil
import os


class FailingSlow(Dict):
    """In-memory slow layer that fails selected writes up front.

    The failure is raised before any bytes are handed to the underlying
    store, so a failed write is never partially accepted.
    """
    def __init__(self, fail_on=()):
        Dict.__init__(self)
        self.fail_on = list(fail_on)
        self.attempts = []

    def append(self, data, lock=True, **kwargs):
        for key in data:
            self.attempts.append(key)
            if self.fail_on and key == self.fail_on[0]:
                self.fail_on.pop(0)
                raise RuntimeError('simulated write failure for %r' % key)
        Dict.append(self, data, lock=lock, **kwargs)


def test_partd():
    a = Dict()
    b = Dict()
    with Buffer(a, b, available_memory=10) as p:
        p.append({'x': b'Hello', 'y': b'abc'})
        assert a.get(['x', 'y']) == [b'Hello', b'abc']

        p.append({'x': b'World!', 'y': b'def'})
        assert a.get(['x', 'y']) == [b'', b'abcdef']
        assert b.get(['x', 'y']) == [b'HelloWorld!', b'']

        result = p.get(['y', 'x'])
        assert result == [b'abcdef', b'HelloWorld!']

        assert p.get('z') == b''

        with p.lock:  # uh oh, possible deadlock
            result = p.get(['x'], lock=False)


def test_keys_to_flush():
    lengths = {'a': 20, 'b': 10, 'c': 15, 'd': 15, 'e': 10, 'f': 25, 'g': 5}
    assert keys_to_flush(lengths, 0.5) == ['f', 'a']


def test_pickle():
    with Dict() as a:
        with File() as b:
            c = Buffer(a, b)

            c.append({'x': b'123'})

            d = pickle.loads(pickle.dumps(c))

            assert d.get('x') == c.get('x')

            pickled_attrs = ('memory_usage', 'lengths', 'available_memory')
            for attr in pickled_attrs:
                assert hasattr(d, attr)
                assert getattr(d, attr) == getattr(c, attr)
            # special case Dict and File -- some attrs do not pickle
            assert hasattr(d, 'fast')
            assert d.fast.data == c.fast.data
            assert hasattr(d, 'slow')
            assert d.slow.path == c.slow.path


def test_get_preserves_append_order_across_layers():
    with Dict() as fast, Dict() as slow:
        with Buffer(fast, slow) as p:
            p.append({'x': b'A'})
            p.flush(['x'])            # A now lives only on the slow layer
            p.append({'x': b'B'})     # B stays in the fast layer

            assert fast.get(['x']) == [b'B']
            assert slow.get(['x']) == [b'A']
            # bytes must come back in append order even across layers
            assert p.get('x') == b'AB'
            assert p.get(['x']) == [b'AB']


def test_flush_failure_retry_keeps_parts_intact():
    slow = FailingSlow()
    single = Dict()

    with Dict() as fast:
        with Buffer(fast, slow) as p:
            # x: A flushed to disk, then B appended and held in memory
            p.append({'x': b'A'})
            p.flush(['x'])
            # y: an old part already on disk plus a new part in memory
            p.append({'y': b'Yold'})
            p.flush(['y'])
            p.append({'x': b'B', 'y': b'Ynew'})
            # an empty part and a key that is never created
            p.append({'e': b''})

            single.append({'x': b'A', 'y': b'Yold'})
            single.append({'x': b'B', 'y': b'Ynew'})

            # batch read interleaves slow (older) and fast (newer) parts
            assert p.get(['x', 'y']) == [b'AB', b'YoldYnew']
            assert p.get(['x', 'y']) == single.get(['x', 'y'])

            # arm the failure so the y write fails before accepting bytes;
            # x is still flushed first and must complete cleanly
            slow.fail_on = ['y']
            start = len(slow.attempts)
            with pytest.raises(RuntimeError):
                p.flush(['x', 'y'])

            # x completed once and is not sent again on retry
            assert slow.attempts[start:] == ['x', 'y']
            assert 'x' not in fast.data
            assert slow.get(['x']) == [b'AB']
            assert p.get('x') == b'AB'

            # y was never accepted by the slow layer and stays fully in fast
            assert slow.get(['y']) == [b'Yold']
            assert fast.get(['y']) == [b'Ynew']
            assert p.get('y') == b'YoldYnew'
            assert p.lengths == {'y': 4, 'e': 0}
            assert p.memory_usage == 4

            # retry moves y without duplicating the old on-disk part
            p.flush(['y'])
            assert slow.attempts[start:] == ['x', 'y', 'y']
            assert fast.get(['y']) == [b'']
            assert slow.get(['y']) == [b'YoldYnew']
            assert p.get('y') == b'YoldYnew'

            # a further append to x still reads back in order
            p.append({'x': b'C'})
            assert p.get('x') == b'ABC'

            single.append({'x': b'C'})
            assert p.get(['x', 'y']) == single.get(['x', 'y'])

            # empty part and missing query keep the existing semantics
            assert p.get('e') == b''
            assert p.get('z') == b''


def test_auto_flush_failure_retry_keeps_parts_intact():
    slow = FailingSlow(fail_on=['y'])

    with Dict() as fast:
        with Buffer(fast, slow, available_memory=5) as p:
            p.append({'x': b'AA'})          # 2 bytes, under the budget
            with pytest.raises(RuntimeError):
                # 6 bytes total triggers an automatic flush of the largest key
                p.append({'y': b'BBBB'})

            # nothing was accepted by the slow layer; both keys remain readable
            assert slow.attempts == ['y']
            assert slow.get(['x', 'y']) == [b'', b'']
            assert fast.get(['x', 'y']) == [b'AA', b'BBBB']
            assert p.get(['x', 'y']) == [b'AA', b'BBBB']
            assert p.lengths == {'x': 2, 'y': 4}
            assert p.memory_usage == 6

            # retrying through another append flushes y exactly once
            p.append({'z': b'z'})
            assert slow.attempts == ['y', 'y']
            assert slow.get(['y']) == [b'BBBB']
            assert p.get(['x', 'y', 'z']) == [b'AA', b'BBBB', b'z']
            assert p.lengths == {'x': 2, 'z': 1}
            assert p.memory_usage == 3

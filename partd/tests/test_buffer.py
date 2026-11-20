from partd.dict import Dict
from partd.file import File
from partd.buffer import Buffer, keys_to_flush
import pickle
import pytest

import shutil
import os


class FlakySlow(Dict):
    """ Slow layer that refuses the very first delete, then accepts them.

    The refusal happens before anything is removed, mirroring storage that
    rejects an operation whose outcome cannot be guaranteed.
    """

    def __init__(self):
        Dict.__init__(self)
        self.delete_calls = 0
        self.calls = []
        self.rejected_keys = None

    def _delete(self, keys, lock=True):
        self.delete_calls += 1
        self.calls.append(list(keys))
        if self.delete_calls == 1:
            self.rejected_keys = list(keys)
            raise RuntimeError("simulated slow-layer failure")
        return Dict._delete(self, keys, lock=lock)


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


def test_delete_refunds_memory_once_and_ignores_missing_keys():
    with Buffer(Dict(), Dict(), available_memory=10) as p:
        p.append({'x': b'12345'})
        p.flush(['x'])                        # x now lives only on the slow layer
        p.append({'x': b'67', 'y': b'ab'})   # 4 bytes held in memory

        p.delete(['x', 'y', 'z', 'x'])       # z never existed, x repeated

        assert p.lengths == {}
        assert p.memory_usage == 0            # 4 real bytes; x charged only once
        assert p.fast.data == {}
        assert p.slow.data == {}
        assert p.get(['x', 'y', 'z']) == [b'', b'', b'']

        # The refunded quota is reusable: a batch within the limit must not flush.
        p.append({'q': b'1234567890'})        # exactly at the 10-byte limit
        assert p.memory_usage == 10
        assert 'q' in p.fast.data
        assert p.slow.get(['q']) == [b'']


def test_delete_disk_only_key_does_not_touch_memory_accounting():
    with Buffer(Dict(), Dict(), available_memory=10) as p:
        p.append({'x': b'12345'})
        p.flush(['x'])                        # x is disk-only now
        p.append({'y': b'ab'})
        assert p.memory_usage == 2

        p.delete(['x', 'z'])                  # z never existed

        assert p.memory_usage == 2            # no phantom charge, no negative count
        assert p.lengths == {'y': 2}
        assert p.slow.data == {}
        assert p.get(['x', 'y', 'z']) == [b'', b'ab', b'']


def test_delete_failure_is_retryable_and_accounting_is_exact():
    slow = FlakySlow()
    with Buffer(Dict(), slow, available_memory=10) as p:
        # x: one on-disk piece plus a fresh in-memory piece; y: memory only.
        p.append({'x': b'12345'})
        p.flush(['x'])
        p.append({'x': b'67', 'y': b'ab'})

        assert p.memory_usage == 4
        assert p.lengths == {'x': 2, 'y': 2}
        assert p.slow.get(['x']) == [b'12345']
        assert p.get('x') == b'6712345'

        # First attempt: the slow layer rejects while nothing is removed yet.
        with pytest.raises(RuntimeError):
            p.delete(['x'])

        # A failed delete must keep the in-memory piece and its quota intact.
        assert slow.delete_calls == 1
        assert slow.rejected_keys == ['x']
        assert p.fast.get(['x', 'y']) == [b'67', b'ab']
        assert p.slow.get(['x']) == [b'12345']
        assert p.lengths == {'x': 2, 'y': 2}
        assert p.memory_usage == 4
        assert p.get('x') == b'6712345'

        # Retry with a repeated x, a memory-only y, and a missing z.
        p.delete(['x', 'y', 'z', 'x'])

        # Each key reaches each layer exactly once; missing z is a no-op.
        assert slow.delete_calls == 2
        assert slow.calls[1] == ['x', 'y', 'z']
        assert p.fast.data == {}
        assert p.slow.data == {}
        assert p.lengths == {}
        assert p.memory_usage == 0            # only the 4 real memory bytes, once
        assert p.get(['x', 'y', 'z']) == [b'', b'', b'']

        # A re-created x must not see any data from the old x.
        p.append({'x': b'89'})
        assert p.get('x') == b'89'
        assert p.lengths == {'x': 2}
        assert p.memory_usage == 2

        # Two bytes fit the remaining quota, so nothing is flushed.
        assert p.fast.get(['x']) == [b'89']
        assert p.slow.get(['x']) == [b'']

        # Only genuinely exceeding the remaining quota triggers a flush.
        p.append({'x': b'0123456789'})        # 12 bytes total, over the 10 limit
        assert p.memory_usage <= p.available_memory
        assert p.get('x') == b'890123456789'
        assert p.slow.get(['x']) == [b'890123456789']
        assert 'x' not in p.lengths

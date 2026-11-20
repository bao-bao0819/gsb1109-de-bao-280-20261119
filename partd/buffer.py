from .core import Interface
from threading import Lock
from toolz import topk, accumulate, pluck
from operator import add
from bisect import bisect
from collections import defaultdict


def zero():
    return 0

class Buffer(Interface):
    def __init__(self, fast, slow, available_memory=1e9):
        self.lock = Lock()
        self.fast = fast
        self.slow = slow
        self.available_memory = available_memory
        self.lengths = defaultdict(zero)
        self.memory_usage = 0
        Interface.__init__(self)

    def __getstate__(self):
        return {'fast': self.fast,
                'slow': self.slow,
                'memory_usage': self.memory_usage,
                'lengths': self.lengths,
                'available_memory': self.available_memory}

    def __setstate__(self, state):
        Interface.__setstate__(self, state)
        self.lock = Lock()
        self.__dict__.update(state)

    def append(self, data, lock=True, **kwargs):
        if lock: self.lock.acquire()
        try:
            for k, v in data.items():
                self.lengths[k] += len(v)
                self.memory_usage += len(v)
            self.fast.append(data, lock=False, **kwargs)

            while self.memory_usage > self.available_memory:
                keys = keys_to_flush(self.lengths, 0.1, maxcount=20)
                self.flush(keys)

        finally:
            if lock: self.lock.release()

    def _get(self, keys, lock=True, **kwargs):
        if lock: self.lock.acquire()
        try:
            # On-disk (slow) parts were appended before anything still held
            # in memory (fast), so slow bytes come first in append order.
            result = list(map(add, self.slow.get(keys, lock=False),
                                   self.fast.get(keys, lock=False)))
        finally:
            if lock: self.lock.release()
        return result

    def _iset(self, key, value, lock=True):
        """ Idempotent set """
        if lock: self.lock.acquire()
        try:
            self.fast.iset(key, value, lock=False)
        finally:
            if lock: self.lock.release()

    def _delete(self, keys, lock=True):
        if lock: self.lock.acquire()
        try:
            self.fast.delete(keys, lock=False)
            self.slow.delete(keys, lock=False)
        finally:
            if lock: self.lock.release()

    def drop(self):
        self._iset_seen.clear()
        self.fast.drop()
        self.slow.drop()

    def __exit__(self, *args):
        self.drop()

    def flush(self, keys=None, block=None):
        """ Flush keys to disk

        Parameters
        ----------

        keys: list or None
            list of keys to flush
        block: bool (defaults to None)
            Whether or not to block until all writing is complete

        If no keys are given then flush all keys

        Each key is fully moved to the slow layer (appended there, removed
        here) before the next one is touched.  A failure while flushing a key
        therefore leaves that key entirely in the fast layer, while keys that
        were already flushed stay complete on the slow layer.  The flush can
        be retried without re-appending the completed keys.
        """
        if keys is None:
            keys = list(self.lengths)

        for key in keys:
            values = self.fast.get([key])
            # Move one key at a time so a failure is confined to this key:
            # completed keys are not re-sent, the failed key is still fully
            # readable from the fast layer and can be flushed again.
            self.slow.append({key: values[0]})
            self.fast.delete([key])

            self.memory_usage -= self.lengths[key]
            del self.lengths[key]


def keys_to_flush(lengths, fraction=0.1, maxcount=100000):
    """ Which keys to remove

    >>> lengths = {'a': 20, 'b': 10, 'c': 15, 'd': 15,
    ...            'e': 10, 'f': 25, 'g': 5}
    >>> keys_to_flush(lengths, 0.5)
    ['f', 'a']
    """
    top = topk(max(len(lengths) // 2, 1),
               lengths.items(),
               key=1)
    total = sum(lengths.values())
    cutoff = min(maxcount, max(1,
                   bisect(list(accumulate(add, pluck(1, top))),
                          total * fraction)))
    result = [k for k, v in top[:cutoff]]
    assert result
    return result

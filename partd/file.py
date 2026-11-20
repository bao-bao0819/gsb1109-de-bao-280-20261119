import atexit
from contextlib import suppress
import os
import shutil
import string
import tempfile
from urllib.parse import quote

from .core import Interface
import locket


class File(Interface):
    """ Appendable byte storage on disk, one file per key

    By default keys map to physical files through the historical
    ``token``/``escape_filename`` scheme, which is lossy: ``'NW#1'`` and
    ``'NW1'``, ``7`` and ``'7'``, or ``('a', 'b')`` and ``'a/b'`` all
    collide on the same file.

    Passing ``legacy_keys`` (an iterable of keys, possibly empty) enables
    the collision-free encoding for every key *not* listed there.  Keys in
    ``legacy_keys`` are confirmed to live at their historical filenames
    (e.g. batches written by an older version) and keep using them, so old
    directories remain readable and appendable without migration.  All
    other keys are stored under an encoding that preserves their type and
    characters, and never fall back to historical filenames: without the
    caller-supplied evidence of ``legacy_keys``, an old physical file is
    never attributed to a look-alike key.

    The collision-free encoding supports strings, integers, and tuples
    composed (recursively) of those types; other key types raise
    ``TypeError``.
    """
    def __init__(self, path=None, dir=None, legacy_keys=None):
        if not path:
            path = tempfile.mkdtemp(suffix='.partd', dir=dir)
            cleanup_files.append(path)
            self._explicitly_given_path = False
        else:
            self._explicitly_given_path = True
        self.path = path
        if legacy_keys is not None:
            legacy_keys = frozenset(legacy_keys)
        self._legacy_keys = legacy_keys
        if not os.path.exists(path):
            with suppress(OSError):
                os.makedirs(path)
        self.lock = locket.lock_file(filename(self.path, '.lock'))
        Interface.__init__(self)

    def __getstate__(self):
        return {'path': self.path, 'legacy_keys': self._legacy_keys}

    def __setstate__(self, state):
        Interface.__setstate__(self, state)
        File.__init__(self, state['path'],
                      legacy_keys=state.get('legacy_keys'))

    def append(self, data, lock=True, fsync=False, **kwargs):
        if lock: self.lock.acquire()
        try:
            for k, v in data.items():
                fn = self.filename(k)
                if not os.path.exists(os.path.dirname(fn)):
                    os.makedirs(os.path.dirname(fn))
                with open(fn, 'ab') as f:
                    f.write(v)
                    if fsync:
                        os.fsync(f)
        finally:
            if lock: self.lock.release()

    def _get(self, keys, lock=True, **kwargs):
        assert isinstance(keys, (list, tuple, set))
        if lock:
            self.lock.acquire()
        try:
            result = []
            for key in keys:
                try:
                    with open(self.filename(key), 'rb') as f:
                        result.append(f.read())
                except OSError:
                    result.append(b'')
        finally:
            if lock:
                self.lock.release()
        return result

    def _iset(self, key, value, lock=True):
        """ Idempotent set """
        fn = self.filename(key)
        if not os.path.exists(os.path.dirname(fn)):
            os.makedirs(os.path.dirname(fn))
        if lock:
            self.lock.acquire()
        try:
            with open(self.filename(key), 'wb') as f:
                f.write(value)
        finally:
            if lock:
                self.lock.release()

    def _delete(self, keys, lock=True):
        if lock:
            self.lock.acquire()
        try:
            for key in keys:
                path = self.filename(key)
                if os.path.exists(path):
                    os.remove(path)
        finally:
            if lock:
                self.lock.release()

    def drop(self):
        if os.path.exists(self.path):
            shutil.rmtree(self.path)
        self._iset_seen.clear()
        os.mkdir(self.path)

    def filename(self, key):
        return filename(self.path, key, legacy_keys=self._legacy_keys)

    def __exit__(self, *args):
        self.drop()
        os.rmdir(self.path)

    def __del__(self):
        if not self._explicitly_given_path:
            self.drop()
            os.rmdir(self.path)


def filename(path, key, legacy_keys=None):
    if legacy_keys is None or key in legacy_keys:
        return os.path.join(path, escape_filename(token(key)))
    return os.path.join(path, SAFE_PREFIX + encode_key(key))


# The historical escape_filename below strips every character outside
# valid_chars, so no legacy physical file can ever contain this prefix.
SAFE_PREFIX = '#'


def encode_key(key):
    """ Lossless, type-preserving encoding of a key to a filename token

    Only strings, integers, and tuples composed (recursively) of those
    types are supported; anything else raises TypeError.

    >>> encode_key('NW#1')
    's,NW%231'
    >>> encode_key(7)
    'i,7'
    >>> encode_key(('a', 7))
    't2,s,a,i,7'
    """
    if isinstance(key, str):
        # quote escapes os.sep, ',', and every other non-unreserved
        # character, so distinct strings never collide with the
        # structural markers below.
        return 's,' + quote(key, safe='')
    elif isinstance(key, bool):
        raise TypeError('partd.File keys must be str, int, or tuples '
                        'thereof; got bool %r' % (key,))
    elif isinstance(key, int):
        return 'i,' + str(key)
    elif isinstance(key, tuple):
        return 't%d,' % len(key) + ','.join(map(encode_key, key))
    raise TypeError('partd.File keys must be str, int, or tuples thereof; '
                    'got %s' % type(key).__name__)


# http://stackoverflow.com/questions/295135/turn-a-string-into-a-valid-filename-in-python
valid_chars = "-_.() " + string.ascii_letters + string.digits + os.path.sep


def escape_filename(fn):
    """ Escape text so that it is a valid filename

    >>> escape_filename('Foo!bar?')
    'Foobar'

    """
    return ''.join(filter(valid_chars.__contains__, fn))



def token(key):
    """

    >>> token('hello')
    'hello'
    >>> token(('hello', 'world'))  # doctest: +SKIP
    'hello/world'
    """
    if isinstance(key, str):
        return key
    elif isinstance(key, tuple):
        return os.path.join(*map(token, key))
    else:
        return str(key)


cleanup_files = list()

@atexit.register
def cleanup():
    for fn in cleanup_files:
        if os.path.exists(fn):
            shutil.rmtree(fn)

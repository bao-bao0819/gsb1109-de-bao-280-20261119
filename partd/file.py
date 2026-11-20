import atexit
from contextlib import suppress
import hashlib
import json
import os
import shutil
import stat
import string
import tempfile
import threading
import uuid

from .core import Interface
import locket


# The '@' character is not in ``valid_chars``, so an escaped key can never
# collide with this metadata directory.
EXPORT_DIRNAME = '.partd-export@meta'
EXPORT_MANIFEST = 'manifest.json'
EXPORT_SUCCESS = 'SUCCESS'
EXPORT_PARTIAL = 'PARTIAL'
EXPORT_FORMAT = 'partd-file-export'
EXPORT_VERSION = 1
COPY_CHUNK_SIZE = 1024 * 1024


class ExportError(Exception):
    pass


class ExportNotPublished(ExportError, OSError):
    pass


class ExportExists(ExportError, FileExistsError):
    pass


class ExportCorrupt(ExportError, OSError):
    pass


class ReadOnlyError(PermissionError, ExportError):
    pass


def _pread(fd, size, offset):
    if hasattr(os, 'pread'):
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


class _SnapshotReader:
    """Read exactly the bytes pinned by a snapshot, never later appends."""

    def __init__(self, entry, digest=None):
        self.entry = entry
        self.digest = digest
        self.offset = 0
        self.size = entry['size']

    def read(self, n=-1):
        remaining = self.size - self.offset
        if remaining <= 0:
            return b''
        if n is None or n < 0:
            n = remaining
        n = min(n, remaining)
        data = _pread(self.entry['fd'], n, self.offset)
        if not data:
            raise ExportCorrupt(
                'source for %r shortened before export'
                % (self.entry['key'],))
        self.offset += len(data)
        if self.digest is not None:
            self.digest.update(data)
        return data


def _copy_snapshot_entry(entry, target, fsync=False):
    digest = hashlib.sha256()
    _remove_path(target)
    if entry['size'] == 0:
        with open(target, 'wb') as f:
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        return digest.hexdigest()

    with open(target, 'wb') as f:
        shutil.copyfileobj(_SnapshotReader(entry, digest), f,
                           COPY_CHUNK_SIZE)
        if fsync:
            f.flush()
            os.fsync(f.fileno())
    return digest.hexdigest()


MISSING = None


def _key_to_json(key):
    if isinstance(key, str):
        return key
    elif isinstance(key, tuple):
        return {'tuple': [_key_to_json(k) for k in key]}
    else:
        return {'key': str(key)}


def _key_from_json(value):
    if isinstance(value, str):
        return value
    elif isinstance(value, dict) and 'tuple' in value:
        return tuple(_key_from_json(k) for k in value['tuple'])
    elif isinstance(value, dict) and 'key' in value:
        return value['key']
    else:
        return str(value)


def _export_dir(path):
    return os.path.join(path, EXPORT_DIRNAME)


def _manifest_path(path):
    return os.path.join(_export_dir(path), EXPORT_MANIFEST)


def _success_path(path):
    return os.path.join(_export_dir(path), EXPORT_SUCCESS)


def _partial_path(path):
    return os.path.join(_export_dir(path), EXPORT_PARTIAL)


def _json_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def _atomic_write(path, data):
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
    tmp = path + '.tmp-' + uuid.uuid4().hex
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _entry_status(entry):
    return entry.get('status', entry.get('kind', 'missing'))


class ExportStatus:
    def __init__(self, path, published, reason=None, manifest=None,
                 errors=None):
        self.path = path
        self.published = published
        self.reason = reason
        self.manifest = manifest
        self.errors = list(errors or [])

    @property
    def is_published(self):
        return self.published

    @property
    def success(self):
        return self.published

    @property
    def ok(self):
        return self.published

    @property
    def complete(self):
        return self.published

    @property
    def entries(self):
        if self.manifest:
            return list(self.manifest.get('entries', []))
        return []

    def __bool__(self):
        return self.published

    def __repr__(self):
        return ('ExportStatus(path=%r, published=%r, reason=%r)'
                % (self.path, self.published, self.reason))


class FileSnapshot:
    """A point-in-time view of selected keys in a :class:`File`.

    The source lock is held only while the key sizes and file descriptors are
    captured.  Subsequent copies read at most those recorded sizes from those
    descriptors, so appends and even a full ``drop``/rebuild of the source
    partition cannot change this snapshot.
    """

    def __init__(self, path, entries):
        self.path = path
        self.entries = entries
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        for entry in self.entries:
            fd = entry.get('fd')
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)
                entry['fd'] = None

    def _check_open(self):
        if self._closed:
            raise ValueError('snapshot is closed')

    def keys(self):
        return [entry['key'] for entry in self.entries]

    def _entry(self, key):
        token = filename('', key)
        for entry in self.entries:
            if entry['token'] == token:
                return entry
        raise KeyError(key)

    def status(self, key):
        return self._entry(key)['status']

    def exists(self, key):
        return self.status(key) != 'missing'

    def get(self, keys, missing=MISSING):
        if not isinstance(keys, list):
            return self.get([keys], missing=missing)[0]
        result = []
        for key in keys:
            entry = self._entry(key)
            if entry['status'] == 'missing':
                result.append(missing)
            else:
                result.append(self._read(entry))
        return result

    def _read(self, entry):
        self._check_open()
        size = entry['size']
        chunks = []
        offset = 0
        while offset < size:
            chunk = _pread(entry['fd'],
                           min(COPY_CHUNK_SIZE, size - offset),
                           offset)
            if not chunk:
                raise ExportCorrupt(
                    'source for %r shortened before export' % (entry['key'],))
            chunks.append(chunk)
            offset += len(chunk)
        return b''.join(chunks)

    def export(self, path, overwrite=False, fsync=False, before_copy=None):
        self._check_open()
        return _publish_snapshot(self, path, overwrite=overwrite,
                                 fsync=fsync, before_copy=before_copy)

    def readonly_view(self, path=None):
        """Convert this snapshot into a published read-only export."""
        if path is None:
            path = tempfile.mkdtemp(suffix='.partd-export')
            cleanup_files.append(path)
        self.export(path)
        return open_export(path)

    def __del__(self):
        with suppress(Exception):
            self.close()


class FileView(Interface):
    """Read-only view over an export produced by :meth:`File.export`."""

    def __init__(self, path, manifest=None):
        if manifest is None:
            status = export_status(path)
            if not status.published:
                raise ExportNotPublished(
                    'export at %s is not published: %s'
                    % (path, status.reason or 'incomplete'))
            path = status.path
            manifest = status.manifest
        self.path = path
        self._manifest = manifest
        self._entries = {}
        for entry in manifest.get('entries', []):
            self._entries[entry['token']] = entry
        self.lock = threading.Lock()
        self.readonly = True
        Interface.__init__(self)

    def __getstate__(self):
        return {'path': self.path}

    def __setstate__(self, state):
        view = open_export(state['path'])
        Interface.__setstate__(self, state)
        self.__dict__.update(view.__dict__)

    @property
    def published(self):
        return True

    @property
    def is_published(self):
        return True

    @property
    def complete(self):
        return True

    @property
    def success(self):
        return True

    @property
    def ok(self):
        return True

    @property
    def manifest(self):
        return self._manifest

    @property
    def entries(self):
        return list(self._manifest.get('entries', []))

    @property
    def missing_keys(self):
        return [_key_from_json(entry['key']) for entry in self._entries.values()
                if entry['status'] == 'missing']

    @property
    def missing(self):
        return self.missing_keys

    @property
    def empty_keys(self):
        return [_key_from_json(entry['key']) for entry in self._entries.values()
                if entry['status'] == 'empty']

    @property
    def empty(self):
        return self.empty_keys

    @property
    def present_keys(self):
        return [_key_from_json(entry['key']) for entry in self._entries.values()
                if entry['status'] == 'present']

    def __fspath__(self):
        return self.path

    def close(self):
        pass

    def selected_keys(self):
        return [_key_from_json(entry['key']) for entry in self._entries.values()]

    def keys(self):
        return self.selected_keys()

    def _entry(self, key):
        token = filename('', key)
        try:
            return self._entries[token]
        except KeyError:
            raise KeyError(key)

    def _require(self, key):
        """Look up a selected key, treating unselected keys as missing."""
        try:
            return self._entry(key)
        except KeyError:
            return None

    def status(self, key):
        entry = self._require(key)
        return 'missing' if entry is None else entry['status']

    def exists(self, key):
        return self.status(key) != 'missing'

    def is_missing(self, key):
        return not self.exists(key)

    def is_empty(self, key):
        return self.status(key) == 'empty'

    def size(self, key):
        entry = self._require(key)
        return 0 if entry is None else entry['size']

    def filename(self, key):
        entry = self._entry(key)
        return os.path.join(self.path, entry['path'])

    def _get(self, keys, lock=False, missing=MISSING, **kwargs):
        result = []
        for key in keys:
            entry = self._require(key)
            if entry is None or entry['status'] == 'missing':
                result.append(missing)
            else:
                with open(self.filename(key), 'rb') as f:
                    result.append(f.read())
        return result

    def __getitem__(self, key):
        value = self.get(key)
        if value is MISSING:
            raise KeyError(key)
        return value

    def __contains__(self, key):
        try:
            return self.exists(key)
        except KeyError:
            return False

    def verify(self):
        errors = []
        for entry in self._entries.values():
            key = _key_from_json(entry['key'])
            if entry['status'] == 'missing':
                continue
            if entry['status'] == 'empty':
                if entry['size'] != 0:
                    errors.append((key, 'empty key has non-zero size'))
                continue
            try:
                with open(self.filename(key), 'rb') as f:
                    data = f.read()
            except OSError as e:
                errors.append((key, str(e)))
                continue
            if len(data) != entry['size']:
                errors.append((key, 'size mismatch'))
            elif (entry.get('sha256') and
                  hashlib.sha256(data).hexdigest() != entry['sha256']):
                errors.append((key, 'checksum mismatch'))
        return errors

    def _readonly(self, *args, **kwargs):
        raise ReadOnlyError('exported File views are read-only')

    append = _readonly
    _iset = _readonly
    _delete = _readonly
    drop = _readonly
    pop = _readonly

    def __exit__(self, *args):
        pass


class File(Interface):
    def __new__(cls, path=None, dir=None, readonly=False):
        if readonly:
            if path is None:
                raise TypeError('readonly File requires an export path')
            return open_export(path)
        return super().__new__(cls)

    def __init__(self, path=None, dir=None, readonly=False):
        if not path:
            path = tempfile.mkdtemp(suffix='.partd', dir=dir)
            cleanup_files.append(path)
            self._explicitly_given_path = False
        else:
            self._explicitly_given_path = True
        self.path = path
        if not os.path.exists(path):
            with suppress(OSError):
                os.makedirs(path)
        self.lock = locket.lock_file(self.filename('.lock'))
        Interface.__init__(self)

    def __getstate__(self):
        return {'path': self.path}

    def __setstate__(self, state):
        Interface.__setstate__(self, state)
        File.__init__(self, state['path'])

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
                path = filename(self.path, key)
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
        return filename(self.path, key)

    def snapshot(self, keys, lock=True):
        """Capture a stable point-in-time view of ``keys``.

        Only the selected key sizes and open file descriptors are captured.
        The source lock is released before any bytes are copied, allowing
        writers to append or rebuild the source while an export is in
        progress.
        """
        if not isinstance(keys, (list, tuple, set)):
            keys = [keys]
        seen = set()
        selected = []
        for key in keys:
            token = filename('', key)
            if token in seen:
                continue
            seen.add(token)
            selected.append(key)
        keys = selected
        if lock:
            self.lock.acquire()
        try:
            entries = []
            for key in keys:
                entry = {
                    'key': key,
                    'token': filename('', key),
                    'path': filename('', key),
                    'fd': None,
                    'size': 0,
                    'status': 'missing',
                }
                try:
                    fd = os.open(self.filename(key), os.O_RDONLY)
                except OSError:
                    fd = None
                if fd is None:
                    entries.append(entry)
                    continue
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        os.close(fd)
                        entries.append(entry)
                        continue
                    entry['fd'] = fd
                    entry['size'] = info.st_size
                    entry['status'] = 'empty' if info.st_size == 0 else 'present'
                except BaseException:
                    os.close(fd)
                    raise
                entries.append(entry)
        finally:
            if lock:
                self.lock.release()
        return FileSnapshot(self.path, entries)

    def export(self, path=None, keys=None, overwrite=False, fsync=False,
               lock=True, before_copy=None):
        """Write selected keys to an independently openable read-only export.

        ``path`` is the destination directory and ``keys`` are the selected
        keys.  The positionally-first argument may be either the destination
        or the key collection.  The destination becomes published atomically:
        until a success marker is written, :func:`open_export` will refuse to
        treat it as a complete export.
        """
        path, keys = _normalise_export_args(path, keys)
        if keys is None:
            raise TypeError('keys are required')
        if path is None:
            path = tempfile.mkdtemp(suffix='.partd-export')
            cleanup_files.append(path)
        _check_export_destination(self.path, path)
        snapshot = self.snapshot(keys, lock=lock)
        try:
            return snapshot.export(path, overwrite=overwrite, fsync=fsync,
                                   before_copy=before_copy)
        finally:
            snapshot.close()

    @classmethod
    def readonly(cls, path):
        return open_export(path)

    @classmethod
    def open_readonly(cls, path):
        return open_export(path)

    @classmethod
    def open_export(cls, path, verify=False):
        return open_export(path, verify=verify)

    @classmethod
    def is_published(cls, path):
        return is_published(path)

    @classmethod
    def export_status(cls, path):
        return export_status(path)

    def export_readonly(self, path=None, keys=None, **kwargs):
        return self.export(path, keys=keys, **kwargs)

    def export_snapshot(self, path=None, keys=None, **kwargs):
        return self.export(path, keys=keys, **kwargs)

    def export_view(self, path=None, keys=None, **kwargs):
        return self.export(path, keys=keys, **kwargs)

    def __exit__(self, *args):
        self.drop()
        with suppress(OSError):
            os.rmdir(self.path)

    def __del__(self):
        if not self._explicitly_given_path:
            self.drop()
            with suppress(OSError):
                os.rmdir(self.path)


def filename(path, key):
    return os.path.join(path, escape_filename(token(key)))


def _check_export_destination(source, destination):
    source = os.path.realpath(source)
    destination = os.path.realpath(destination)
    if destination == source or destination.startswith(source + os.sep):
        raise ExportExists(
            'export destination must be outside the source partition: %s'
            % destination)


def _normalise_export_args(path, keys):
    if (isinstance(path, (list, tuple, set)) and keys is None):
        return None, path
    if path is None:
        return None, keys
    if (isinstance(path, (list, tuple, set)) and
            isinstance(keys, (str, bytes, os.PathLike))):
        path, keys = keys, path
    return path, keys


def _remove_path(path):
    with suppress(OSError):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def _is_export_artifact(name):
    return name == EXPORT_DIRNAME


def _prepare_destination(path, overwrite):
    if not os.path.exists(path):
        return
    if not os.path.isdir(path):
        raise ExportExists('export destination is not a directory: %s' % path)
    if is_published(path):
        if not overwrite:
            raise ExportExists('export destination is already published: %s'
                               % path)
        return
    if overwrite:
        return
    entries = os.listdir(path)
    if not entries:
        return
    # A previous attempt that never published may be retried in place.
    if all(_is_export_artifact(entry) for entry in entries):
        return
    if os.path.exists(_partial_path(path)):
        return
    raise ExportExists('export destination is not empty: %s' % path)


def _clear_previous_export(path):
    manifest_path = _manifest_path(path)
    with suppress(OSError, ValueError):
        with open(manifest_path, 'rb') as f:
            previous = json.loads(f.read().decode())
        for entry in previous.get('entries', []):
            if entry.get('path'):
                _remove_path(os.path.join(path, entry['path']))
    _remove_path(_manifest_path(path))
    _remove_path(_success_path(path))


def _clear_stale_contents(path):
    """Remove files left by an earlier unpublished attempt."""
    for entry in os.listdir(path):
        if entry == EXPORT_DIRNAME:
            continue
        _remove_path(os.path.join(path, entry))


def _target_path(path, entry_path):
    target = os.path.abspath(os.path.join(path, entry_path))
    root = os.path.abspath(path)
    if os.path.commonpath((root, target)) != root or target == root:
        raise ExportError('key maps outside the export directory: %r'
                          % (entry_path,))
    return target


def _publish_snapshot(snapshot, path, overwrite=False, fsync=False,
                      before_copy=None):

    path = os.path.abspath(path)
    _prepare_destination(path, overwrite)

    os.makedirs(path, exist_ok=True)
    metadata = _export_dir(path)
    unpublished = os.path.exists(metadata) and not is_published(path)
    os.makedirs(metadata, exist_ok=True)
    if overwrite:
        _clear_previous_export(path)
        _clear_stale_contents(path)
    elif unpublished:
        _clear_stale_contents(path)
    _atomic_write(_partial_path(path), b'incomplete\n')
    _remove_path(_success_path(path))

    if before_copy is not None:
        before_copy(snapshot)

    entries = []
    try:
        for entry in snapshot.entries:
            key = entry['key']
            target = _target_path(path, entry['path'])
            parent = os.path.dirname(target)
            if parent and not os.path.exists(parent):
                os.makedirs(parent)

            if entry['status'] == 'missing':
                _remove_path(target)
                entries.append({
                    'key': _key_to_json(key),
                    'token': entry['token'],
                    'path': entry['path'],
                    'status': 'missing',
                    'size': 0,
                    'sha256': None,
                })
                continue

            digest = _copy_snapshot_entry(entry, target, fsync=fsync)
            entries.append({
                'key': _key_to_json(key),
                'token': entry['token'],
                'path': entry['path'],
                'status': entry['status'],
                'size': entry['size'],
                'sha256': digest,
            })

        manifest = {
            'format': EXPORT_FORMAT,
            'version': EXPORT_VERSION,
            'entries': entries,
        }
        manifest_bytes = _json_bytes(manifest)
        _atomic_write(_manifest_path(path), manifest_bytes)
        if fsync:
            _fsync_dir(metadata)
        _remove_path(_partial_path(path))
        success = hashlib.sha256(manifest_bytes).hexdigest().encode() + b'\n'
        _atomic_write(_success_path(path), success)
        if fsync:
            _fsync_dir(metadata)
            _fsync_dir(path)
    finally:
        if not is_published(path):
            _atomic_write(_partial_path(path), b'incomplete\n')

    return open_export(path)


def _fsync_dir(path):
    with suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def is_published(path):
    return os.path.exists(_success_path(path))


def export_status(path):
    path = os.path.abspath(path)
    success = _success_path(path)
    manifest_path = _manifest_path(path)
    errors = []

    if not os.path.isdir(path):
        return ExportStatus(path, False, 'destination does not exist',
                            errors=['destination does not exist'])
    if not os.path.exists(success):
        return ExportStatus(path, False, 'export was not published',
                            errors=['missing success marker'])
    if not os.path.exists(manifest_path):
        return ExportStatus(path, False, 'export manifest is missing',
                            errors=['missing manifest'])
    try:
        with open(manifest_path, 'rb') as f:
            manifest_bytes = f.read()
        manifest = json.loads(manifest_bytes.decode())
    except (OSError, ValueError) as e:
        return ExportStatus(path, False, 'export manifest is unreadable',
                            errors=[str(e)])

    if manifest.get('format') != EXPORT_FORMAT:
        return ExportStatus(path, False, 'unexpected export format',
                            manifest=manifest)
    if manifest.get('version') != EXPORT_VERSION:
        return ExportStatus(path, False, 'unsupported export version',
                            manifest=manifest)

    running = hashlib.sha256(manifest_bytes)
    expected = running.hexdigest().encode() + b'\n'
    try:
        with open(success, 'rb') as f:
            actual = f.read()
    except OSError as e:
        return ExportStatus(path, False, 'cannot read success marker',
                            manifest=manifest, errors=[str(e)])
    if actual != expected:
        return ExportStatus(path, False, 'success marker mismatch',
                            manifest=manifest,
                            errors=['success marker does not match manifest'])

    for entry in manifest.get('entries', []):
        key = _key_from_json(entry.get('key'))
        if _entry_status(entry) == 'missing':
            continue
        try:
            fn = _target_path(path, entry['path'])
        except (ExportError, KeyError) as e:
            errors.append((key, str(e)))
            continue
        try:
            size = os.path.getsize(fn)
        except OSError as e:
            errors.append((key, str(e)))
            continue
        if size != entry.get('size'):
            errors.append((key, 'size mismatch'))
    if errors:
        return ExportStatus(path, False, 'export contents are incomplete',
                            manifest=manifest, errors=errors)
    return ExportStatus(path, True, manifest=manifest)


def open_export(path, verify=False):
    """Open a published export as an independent read-only store."""
    path = os.path.abspath(path)
    status = export_status(path)
    if not status.published:
        raise ExportNotPublished(
            'export at %s is not published: %s'
            % (path, status.reason or 'incomplete'))
    view = FileView(path, status.manifest)
    if verify:
        errors = view.verify()
        if errors:
            raise ExportCorrupt('export at %s failed verification: %r'
                                % (path, errors))
    return view


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


ReadOnlyFile = FileView

@atexit.register
def cleanup():
    for fn in cleanup_files:
        if os.path.exists(fn):
            shutil.rmtree(fn)

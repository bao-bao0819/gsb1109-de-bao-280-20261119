""" Store arrays

We put arrays on disk as raw bytes, extending along the first dimension.
Alongside each array x we ensure the value x.dtype which stores the string
description of the array's dtype.

For structured dtypes the full physical layout (field names and titles,
per-field dtypes, subarray shapes, offsets and the record itemsize) is
preserved, including any padding.  Appending a block whose layout differs is
rejected before any bytes are written; padding *contents* may differ freely.
"""
from contextlib import suppress
import ast
import pickle

import numpy as np
from toolz import valmap, identity, partial
from .core import Interface
from .file import File
from .utils import frame, framesplit, suffix


class DtypeMismatchError(ValueError):
    """Raised when appended data does not match an existing dtype layout."""


_DTYPE_PREFIX = b'partd-dtype-v1:'


def _dtype_spec(dt):
    dt = np.dtype(dt)
    if dt.subdtype is not None:
        base, shape = dt.subdtype
        return ('subarray', _dtype_spec(base),
                tuple(int(n) for n in shape))
    if dt.names is None:
        return ('scalar', dt.str)
    titles = []
    for name in dt.names:
        field = dt.fields[name]
        titles.append(field[2] if len(field) > 2 else None)
    return ('structured',
            tuple(dt.names),
            tuple(_dtype_spec(dt.fields[name][0]) for name in dt.names),
            tuple(int(dt.fields[name][1]) for name in dt.names),
            int(dt.itemsize),
            bool(dt.isalignedstruct),
            tuple(titles))


def _dtype_from_spec(spec):
    kind = spec[0]
    if kind == 'scalar':
        return np.dtype(spec[1])
    if kind == 'subarray':
        return np.dtype((_dtype_from_spec(spec[1]), tuple(spec[2])))
    if kind == 'structured':
        _, names, formats, offsets, itemsize, aligned, titles = spec
        fields = []
        for fmt in formats:
            field_dtype = _dtype_from_spec(fmt)
            if field_dtype.subdtype is not None:
                base, shape = field_dtype.subdtype
                fields.append((base, shape))
            else:
                fields.append(field_dtype)
        spec = {'names': list(names),
                'formats': fields,
                'offsets': list(offsets),
                'itemsize': int(itemsize),
                'aligned': bool(aligned)}
        if any(title is not None for title in titles):
            spec['titles'] = list(titles)
        return np.dtype(spec)
    raise ValueError("Unknown dtype specification: %r" % (spec,))


def _dtype_layout(dt):
    """Return the physical layout of a dtype, ignoring padding contents."""
    dt = np.dtype(dt)
    if dt.subdtype is not None:
        base, shape = dt.subdtype
        return ('subarray', _dtype_layout(base),
                tuple(int(n) for n in shape))
    if dt.names is None:
        return ('scalar', dt.str)
    fields = tuple(sorted(
        (name, int(dt.fields[name][1]), _dtype_layout(dt.fields[name][0]))
        for name in dt.names))
    return ('structured', fields, int(dt.itemsize))


def _describe_layout_mismatch(old, new):
    details = []
    if old.itemsize != new.itemsize:
        details.append("record itemsize (stride) differs: existing %d, "
                       "incoming %d" % (old.itemsize, new.itemsize))

    old_structured = old.names is not None
    new_structured = new.names is not None
    if old_structured and new_structured:
        old_names = set(old.names)
        new_names = set(new.names)
        if old_names != new_names:
            details.append("field names differ: existing %r, incoming %r"
                           % (old.names, new.names))
        for name in old.names:
            if name not in new.fields:
                continue
            old_field, old_offset = old.fields[name][:2]
            new_field, new_offset = new.fields[name][:2]
            if old_offset != new_offset:
                details.append("field %r offset differs: existing %d, "
                               "incoming %d" % (name, old_offset, new_offset))
            if _dtype_layout(old_field) != _dtype_layout(new_field):
                details.append("field %r dtype differs: existing %r, "
                               "incoming %r" % (name, old_field, new_field))
    elif old_structured != new_structured:
        details.append("one dtype is structured and the other is not")

    if not details:
        details.append("dtype layouts differ")
    return '; '.join(details)


def _ensure_compatible_dtype(key, old, new):
    if _dtype_layout(old) == _dtype_layout(new):
        return
    raise DtypeMismatchError(
        "Cannot append to key %r: incoming dtype layout is incompatible "
        "with the existing data; nothing was written.\n"
        "existing dtype: %r\nincoming dtype: %r\n%s"
        % (key, old, new, _describe_layout_mismatch(old, new)))


def _check_chunk_layout(key, chunk, dtype):
    """Make sure stored bytes can be reinterpreted with the stored dtype."""
    dtype = np.dtype(dtype)
    if dtype == 'O' or not chunk or not dtype.itemsize:
        return
    if len(chunk) % dtype.itemsize:
        raise DtypeMismatchError(
            "Stored data for key %r has %d bytes, which is not a multiple of "
            "its record itemsize %d (dtype %r). The partition was probably "
            "written with an incompatible dtype layout."
            % (key, len(chunk), dtype.itemsize, dtype))


def serialize_dtype(dt):
    """ Serialize dtype to bytes

    >>> serialize_dtype(np.dtype('i4'))
    b'<i4'
    >>> serialize_dtype(np.dtype('M8[us]'))
    b'<M8[us]'
    """
    dt = np.dtype(dt)
    if dt.names is None and dt.subdtype is None:
        return dt.str.encode()
    return _DTYPE_PREFIX + repr(_dtype_spec(dt)).encode('utf-8')


def parse_dtype(s):
    """ Parse text as numpy dtype

    >>> parse_dtype(b'i4')
    dtype('int32')

    >>> parse_dtype(b"[('a', 'i4')]")
    dtype([('a', '<i4')])
    """
    if isinstance(s, str):
        s = s.encode()
    if s.startswith(_DTYPE_PREFIX):
        return _dtype_from_spec(ast.literal_eval(
            s[len(_DTYPE_PREFIX):].decode('utf-8')))
    if s.startswith(b'['):
        return np.dtype(eval(s))  # Dangerous!
    else:
        return np.dtype(s)


class Numpy(Interface):
    def __init__(self, partd=None):
        if not partd or isinstance(partd, str):
            partd = File(partd)
        self.partd = partd
        Interface.__init__(self)

    def __getstate__(self):
        return {'partd': self.partd}

    def append(self, data, **kwargs):
        # Validate every key before writing anything for any key.
        self._check_dtypes(data, lock=kwargs.get('lock', True))
        for k, v in data.items():
            self.partd.iset(suffix(k, '.dtype'), serialize_dtype(v.dtype))
        self.partd.append(valmap(serialize, data), **kwargs)

    def _check_dtypes(self, data, lock=True):
        """Reject incompatible layouts before any bytes are written.

        Padding bytes may differ between appends, but the record stride and
        each field's offset, dtype and subarray shape must stay the same.
        """
        keys = list(data)
        dtype_keys = [suffix(k, '.dtype') for k in keys]
        existing = self.partd._get(dtype_keys, lock=lock)
        for key, raw in zip(keys, existing):
            if not raw:
                continue
            old = parse_dtype(raw)
            new = np.dtype(data[key].dtype)
            if old == 'O' and new == 'O':
                continue
            _ensure_compatible_dtype(key, old, new)

    def _get(self, keys, **kwargs):
        bytes = self.partd._get(keys, **kwargs)
        dtypes = self.partd._get([suffix(key, '.dtype') for key in keys],
                                 lock=False)
        dtypes = list(map(parse_dtype, dtypes))
        for key, chunk, dtype in zip(keys, bytes, dtypes):
            _check_chunk_layout(key, chunk, dtype)
        return list(map(deserialize, bytes, dtypes))

    def delete(self, keys, **kwargs):
        keys2 = [suffix(key, '.dtype') for key in keys]
        self.partd.delete(keys2, **kwargs)

    def _iset(self, key, value):
        return self.partd._iset(key, value)

    def drop(self):
        return self.partd.drop()

    def __del__(self):
        self.partd.__del__()

    @property
    def lock(self):
        return self.partd.lock

    def __exit__(self, *args):
        self.drop()
        self.partd.__exit__(self, *args)

try:
    from pandas import msgpack
except ImportError:
    try:
        import msgpack
    except ImportError:
        msgpack = False


def serialize(x):
    if x.dtype == 'O':
        l = x.flatten().tolist()
        with suppress(Exception):  # Try msgpack (faster on strings)
            return frame(msgpack.packb(l, use_bin_type=True))
        return frame(pickle.dumps(l, protocol=pickle.HIGHEST_PROTOCOL))
    else:
        return x.tobytes()


def deserialize(bytes, dtype, copy=False):
    if dtype == 'O':
        try:
            if msgpack.version >= (0, 5, 2):
                unpack_kwargs = {'raw': False}
            else:
                unpack_kwargs = {'encoding': 'utf-8'}

            blocks = [msgpack.unpackb(f, **unpack_kwargs)
                      for f in framesplit(bytes)]
        except Exception:
            blocks = [pickle.loads(f) for f in framesplit(bytes)]

        result = np.empty(sum(map(len, blocks)), dtype='O')
        i = 0
        for block in blocks:
            result[i:i + len(block)] = block
            i += len(block)
        return result
    else:
        result = np.frombuffer(bytes, dtype)
        if copy:
            result = result.copy()
        return result


compress_text = identity
decompress_text = identity
compress_bytes = lambda bytes, itemsize: bytes
decompress_bytes = identity

with suppress(ImportError):
    import blosc
    blosc.set_nthreads(1)

    compress_bytes = blosc.compress
    decompress_bytes = blosc.decompress

    compress_text = partial(blosc.compress, typesize=1)
    decompress_text = blosc.decompress

with suppress(ImportError):
    from snappy import compress as compress_text
    from snappy import decompress as decompress_text


def compress(bytes, dtype):
    if dtype == 'O':
        return compress_text(bytes)
    else:
        return compress_bytes(bytes, dtype.itemsize)


def decompress(bytes, dtype):
    if dtype == 'O':
        return decompress_text(bytes)
    else:
        return decompress_bytes(bytes)

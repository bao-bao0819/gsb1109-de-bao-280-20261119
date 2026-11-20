from functools import partial
from contextlib import suppress
import os
import pickle
import tempfile

import pandas as pd
from packaging.version import Version

PANDAS_GE_210 = Version(pd.__version__).release >= (2, 1, 0)
PANDAS_GE_300 =  Version(pd.__version__).major >= 3

if PANDAS_GE_300:
    from pandas.api.internals import create_dataframe_from_blocks
    create_block_manager_from_blocks = None
    make_block = None
else:
    create_dataframe_from_blocks = None
    try:
        from pandas.core.internals.managers import create_block_manager_from_blocks
    except ImportError:
        from pandas.core.internals import create_block_manager_from_blocks

    from pandas.core.internals import make_block

from . import numpy as pnp
from .core import Interface
from .encode import Encode
from .utils import extend, framesplit, frame, suffix
from pandas.api.types import is_extension_array_dtype
from pandas.api.extensions import ExtensionArray

def is_extension_array(x):
    return isinstance(x, ExtensionArray)


dumps = partial(pickle.dumps, protocol=pickle.HIGHEST_PROTOCOL)



MANIFEST_VERSION = 1
BLOCKS_SUFFIX = '.blocks'
INDEX_FIELD = '.index'


def _normalize_range(spec):
    """ Turn a user supplied row range into a ``slice(start, stop, step)``

    Ranges are interpreted as *row positions* (like ``DataFrame.iloc``) and
    never as labels from the (possibly duplicated / unsorted) index.
    """
    if spec is None:
        return None
    if isinstance(spec, slice):
        return spec
    if isinstance(spec, (tuple, list)):
        if len(spec) == 2:
            return slice(spec[0], spec[1])
        if len(spec) == 3:
            return slice(spec[0], spec[1], spec[2])
    raise TypeError("Row ranges must be a slice or a (start, stop[, step]) "
                    "sequence, got %r" % (spec,))


class PandasColumns(Interface):
    def __init__(self, partd=None):
        self.partd = pnp.Numpy(partd)
        # Set by range reads that could not use the block manifest and had to
        # fall back to reading the complete, append-only arrays.
        self.last_read_fallback = False
        Interface.__init__(self)

    # ------------------------------------------------------------------
    # Block manifest
    #
    # Each committed append stores, per key, the absolute byte range of every
    # field (the index and each selected column) inside its append-only file.
    # The manifest itself is replaced atomically *after* all of a block's
    # bytes have been written, so a reader either sees a complete block or
    # none of it.  Partially written, uncommitted bytes are simply ignored.
    # ------------------------------------------------------------------
    def _manifest_filename(self, key):
        return self.partd.partd.filename(extend(key, BLOCKS_SUFFIX))

    def _file_backed(self):
        return hasattr(self.partd.partd, 'filename')

    def _read_manifest(self, key):
        if not self._file_backed():
            return None
        try:
            with open(self._manifest_filename(key), 'rb') as f:
                raw = f.read()
        except OSError:
            return None
        try:
            manifest = pickle.loads(raw)
        except Exception:
            return None
        if not isinstance(manifest, dict):
            return None
        if manifest.get('version') != MANIFEST_VERSION:
            return None
        if not isinstance(manifest.get('blocks'), list):
            return None
        return manifest

    def _range_from_manifest_supported(self, manifest):
        blocks = manifest.get('blocks')
        if not isinstance(blocks, list):
            return False
        for block in blocks:
            if not isinstance(block, dict):
                return False
            nrows = block.get('nrows')
            fields = block.get('fields')
            if not isinstance(nrows, int) or nrows < 0:
                return False
            if not isinstance(fields, dict):
                return False
            for span in fields.values():
                if not isinstance(span, (tuple, list)) or len(span) != 3:
                    return False
                offset, nbytes, dtype = span
                if not isinstance(offset, int) or offset < 0:
                    return False
                if not isinstance(nbytes, int) or nbytes < 0:
                    return False
                if not isinstance(dtype, bytes):
                    return False
        return True

    def _write_manifest(self, key, manifest):
        fn = self._manifest_filename(key)
        directory = os.path.dirname(fn) or '.'
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix='.blocks-',
                                   suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(dumps(manifest))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, fn)
        except BaseException:
            with suppress(OSError):
                os.remove(tmp)
            raise

    @staticmethod
    def _committed_ends(manifest):
        ends = {}
        for block in manifest.get('blocks', []):
            for field_key, span in block.get('fields', {}).items():
                offset, nbytes = span[0], span[1]
                ends[field_key] = max(ends.get(field_key, 0), offset + nbytes)
        return ends

    def _append_one(self, key, df, **kwargs):
        file_partd = self.partd.partd
        file_backed = self._file_backed()

        cols = list(df.columns)
        # Kept for backwards compatibility with the whole-array read path.
        self.iset(extend(key, '.columns'), dumps(cols), lock=False)
        self.iset(extend(key, '.index-name'), dumps(df.index.name),
                  lock=False)

        # TODO: don't use values, it does some work.  Look at _blocks instead
        #       pframe/cframe do this well
        arrays = {extend(key, col): df[col].values for col in cols}
        arrays[extend(key, INDEX_FIELD)] = df.index.values

        # TODO: handle categoricals
        serialized = {}
        for fkey, values in arrays.items():
            file_partd.iset(suffix(fkey, '.dtype'),
                            pnp.serialize_dtype(values.dtype), lock=False)
            serialized[fkey] = pnp.serialize(values)

        if not file_backed:
            # In-memory / buffered backends have no byte-addressable files;
            # keep the original append path and use the full-read fallback.
            file_partd.append(serialized, lock=False, **kwargs)
            return

        manifest = self._read_manifest(key)

        if manifest is None:
            # A directory written before block manifests existed cannot be
            # located cheaply.  Mark it legacy so range reads use the
            # explicit fallback instead of guessing block boundaries.
            try:
                legacy = any(os.path.getsize(file_partd.filename(fkey)) > 0
                             for fkey in serialized)
            except OSError:
                legacy = False
            manifest = {
                'version': MANIFEST_VERSION,
                'legacy': legacy,
                'columns': cols,
                'index_name': df.index.name,
                'total_rows': 0,
                'blocks': [],
            }
            # Publish the empty manifest first so that a crash midway through
            # the very first append cannot expose a partial block.
            self._write_manifest(key, manifest)

        legacy = manifest.get('legacy', False)
        committed = self._committed_ends(manifest)
        offsets = {}
        for fkey in serialized:
            fn = file_partd.filename(fkey)
            try:
                size = os.path.getsize(fn)
            except OSError:
                size = 0
            if legacy:
                # Never discard data we cannot describe; fall back instead.
                offsets[fkey] = size
            else:
                offsets[fkey] = committed.get(fkey, 0)
                if size > offsets[fkey]:
                    # Drop bytes left by an append that never committed so a
                    # whole-file read cannot see half of a block.
                    os.truncate(fn, offsets[fkey])
                elif size < offsets[fkey]:
                    # Committed bytes went missing; do not silently pad with
                    # zeros.  Surface it so the caller can investigate.
                    raise ValueError(
                        "File for %r is shorter than its committed blocks"
                        % (fkey,))

        block = {
            'nrows': len(df),
            'columns': cols,
            'fields': {fkey: (offsets[fkey], len(data),
                              pnp.serialize_dtype(arrays[fkey].dtype))
                       for fkey, data in serialized.items()},
        }

        file_partd.append(serialized, lock=False, **kwargs)

        manifest = dict(manifest)
        manifest['blocks'] = list(manifest['blocks']) + [block]
        manifest['total_rows'] = manifest.get('total_rows', 0) + len(df)
        # ``.columns`` / ``.index-name`` are written once (first append wins);
        # mirror that for the manifest's schema description.
        self._write_manifest(key, manifest)

    def append(self, data, **kwargs):
        lock = kwargs.pop('lock', True)
        if lock:
            self.lock.acquire()
        try:
            for k, df in data.items():
                self._append_one(k, df, **kwargs)
        finally:
            if lock:
                self.lock.release()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def _get_full(self, keys, columns=None, **kwargs):
        if columns is None:
            columns = self.partd.partd.get([extend(k, '.columns') for k in keys],
                                           **kwargs)
            columns = list(map(pickle.loads, columns))
        else:
            columns = [columns] * len(keys)
        index_names = self.partd.partd.get([extend(k, '.index-name')
                                            for k in keys], **kwargs)
        index_names = map(pickle.loads, index_names)

        arrays = []
        for key, cols in zip(keys, columns):
            manifest = self._read_manifest(key)
            if (manifest is None or manifest.get('legacy')
                    or not manifest.get('blocks')
                    or not self._range_from_manifest_supported(manifest)
                    or not self._covers(manifest, key, cols)):
                arrays.append(self.partd.get(
                    [[extend(key, '.index'),
                      [extend(key, col) for col in cols]]], **kwargs)[0])
            else:
                # Read only the committed spans so bytes from an unfinished
                # append can never leak into a whole-frame read either.
                index = self._read_all_committed(key, manifest,
                                                 extend(key, INDEX_FIELD))
                arrs = [self._read_all_committed(key, manifest,
                                                 extend(key, col))
                        for col in cols]
                arrays.append((index, arrs))

        return [pd.DataFrame(dict(zip(cols, arrs)), columns=cols,
                             index=pd.Index(index, name=iname))
            for iname, (index, arrs), cols in zip(index_names, arrays, columns)]

    @staticmethod
    def _covers(manifest, key, cols):
        fields = [extend(key, INDEX_FIELD)] + [extend(key, col)
                                               for col in cols]
        blocks = manifest.get('blocks', [])
        for block in blocks:
            if any(field not in block['fields'] for field in fields):
                return False
        # Our writer lays each field out contiguously from byte 0.  Anything
        # else is evidence we cannot use; let the caller fall back.
        for field in fields:
            expected = 0
            for block in blocks:
                offset, nbytes = block['fields'][field][:2]
                if offset != expected:
                    return False
                expected += nbytes
        return True

    def _read_all_committed(self, key, manifest, field_key):
        # Committed spans are always a prefix of the append-only file, so a
        # single prefix read yields exactly the committed bytes even if an
        # unfinished append left a tail behind.
        nbytes = self._committed_ends(manifest).get(field_key, 0)
        dtype = None
        for block in manifest.get('blocks', []):
            span = block.get('fields', {}).get(field_key)
            if span is not None:
                dtype = span[2]
        if dtype is None:
            raise ValueError("No committed data for %r" % (field_key,))
        return pnp.deserialize(self._read_field(field_key, 0, nbytes),
                               pnp.parse_dtype(dtype))

    @staticmethod
    def _range_for_key(key, ranges, default):
        if isinstance(ranges, dict):
            return ranges.get(key)
        return default

    def _get(self, keys, columns=None, **kwargs):
        # Accept a handful of spellings for the row range.  ``range`` is the
        # canonical name; ``slice``/``rows``/``iloc`` are aliases and
        # ``start``/``stop``/``step`` work as separate arguments.
        default = kwargs.pop('range', None)
        for alias in ('slice', 'rows', 'iloc'):
            alias_value = kwargs.pop(alias, None)
            if default is None:
                default = alias_value
        start = kwargs.pop('start', None)
        stop = kwargs.pop('stop', None)
        step = kwargs.pop('step', None)
        ranges = kwargs.pop('ranges', None)
        if (start is not None or stop is not None or step is not None) \
                and default is None:
            default = (start, stop, step)
        if default is not None:
            default = _normalize_range(default)

        per_key_range = []
        any_range = False
        for key in keys:
            spec = self._range_for_key(key, ranges, default)
            spec = _normalize_range(spec) if spec is not None else None
            per_key_range.append(spec)
            any_range = any_range or spec is not None

        if not any_range:
            return self._get_full(keys, columns=columns, **kwargs)

        result = []
        for key, spec in zip(keys, per_key_range):
            if spec is None:
                result.append(self._get_full([key], columns=columns,
                                             **kwargs)[0])
            else:
                result.append(self._get_range(key, spec, columns=columns,
                                              **kwargs))
        return result

    def get_range(self, keys, start=None, stop=None, step=None,
                  columns=None, **kwargs):
        """Read a positional row range, optionally projecting columns.

        ``start``/``stop``/``step`` follow ``DataFrame.iloc`` semantics and
        therefore refer to row positions, not to index labels.
        """
        return self.get(keys, range=slice(start, stop, step),
                        columns=columns, **kwargs)

    def _get_range(self, key, spec, columns=None, **kwargs):
        self.last_read_fallback = False
        manifest = self._read_manifest(key)
        if manifest is None or manifest.get('legacy'):
            return self._get_range_fallback(key, spec, columns, **kwargs)
        if not self._range_from_manifest_supported(manifest):
            return self._get_range_fallback(key, spec, columns, **kwargs)
        cols = list(manifest.get('columns') or []) if columns is None \
            else list(columns)
        if not self._covers(manifest, key, cols):
            # Some block lacks one of the requested fields; do not silently
            # drop its rows.
            return self._get_range_fallback(key, spec, columns, **kwargs)
        try:
            return self._get_range_from_manifest(key, spec, columns, manifest)
        except (KeyError, IndexError, TypeError, ValueError, OSError,
                EOFError, pickle.UnpicklingError):
            # Evidence is incomplete/unusable (for example an old directory
            # whose manifest did not cover its data).  Be explicit about
            # falling back to a full read rather than returning bad rows.
            return self._get_range_fallback(key, spec, columns, **kwargs)

    def _get_range_fallback(self, key, spec, columns, **kwargs):
        self.last_read_fallback = True
        df = self._get_full([key], columns=columns, **kwargs)[0]
        return df.iloc[spec]

    def _read_field(self, field_key, offset, nbytes):
        # Byte offsets/sizes come from the manifest, which is fetched before
        # any data bytes.  Each field is read independently, so a reader can
        # never mix one field's newer block with another field's older span.
        data = self.partd.partd._get_range(field_key, offset, nbytes,
                                           lock=False)
        if len(data) != nbytes:
            raise ValueError("Incomplete block for %r" % (field_key,))
        return data

    def _empty_range_frame(self, key, cols, manifest):
        """An empty frame that keeps dtypes and the index dtype."""
        index_name = manifest.get('index_name')
        index_dtype = None
        for block in manifest.get('blocks', []):
            span = block.get('fields', {}).get(extend(key, INDEX_FIELD))
            if span is not None:
                index_dtype = pnp.parse_dtype(span[2])
        index = pd.Index([], name=index_name,
                         dtype=index_dtype if index_dtype is not None
                         else None)

        arrays = {}
        for col in cols:
            dtype = None
            for block in manifest.get('blocks', []):
                span = block.get('fields', {}).get(extend(key, col))
                if span is not None:
                    dtype = pnp.parse_dtype(span[2])
            arrays[col] = pd.Series([], dtype=dtype)
        return pd.DataFrame(arrays, columns=cols, index=index)

    def _get_range_from_manifest(self, key, spec, columns, manifest):
        blocks = manifest.get('blocks', [])
        total_rows = sum(block['nrows'] for block in blocks)

        positions = range(*spec.indices(total_rows))

        if columns is None:
            cols = list(manifest.get('columns') or [])
        else:
            cols = list(columns)
        index_name = manifest.get('index_name')

        if len(positions) == 0:
            return self._empty_range_frame(key, cols, manifest)

        # Inclusive positional bounds that every block must overlap.
        lo = min(positions[0], positions[-1])
        hi = max(positions[0], positions[-1]) + 1

        index_field = extend(key, INDEX_FIELD)
        frames = []
        window_start = None
        window_stop = None
        position = 0
        for block in blocks:
            nrows = block['nrows']
            block_start = position
            block_stop = position + nrows
            position = block_stop

            if nrows == 0 or block_stop <= lo or block_start >= hi:
                continue  # non-overlapping: never touch this block's bytes

            index_offset, index_nbytes, index_dtype = block['fields'][
                index_field]
            index = pnp.deserialize(
                self._read_field(index_field, index_offset, index_nbytes),
                pnp.parse_dtype(index_dtype))
            if len(index) != nrows:
                raise ValueError("Index length disagrees with manifest")

            arrays = {}
            for col in cols:
                field_key = extend(key, col)
                offset, nbytes, dtype = block['fields'][field_key]
                values = pnp.deserialize(self._read_field(field_key, offset,
                                                          nbytes),
                                         pnp.parse_dtype(dtype))
                if len(values) != nrows:
                    raise ValueError("Column length disagrees with manifest")
                arrays[col] = values

            frames.append(pd.DataFrame(arrays, columns=cols,
                                       index=pd.Index(index,
                                                      name=index_name)))
            if window_start is None:
                window_start = block_start
            window_stop = block_stop

        if not frames:
            return self._empty_range_frame(key, cols, manifest)

        result = pd.concat(frames) if len(frames) > 1 else frames[0]
        # The concatenated frames cover global positions
        # ``window_start:window_stop``.  Translate the requested positional
        # slice into that window so every duplicate index occurrence keeps its
        # own position.
        local = [i - window_start for i in positions
                 if window_start <= i < window_stop]
        return result.iloc[local]

    def __getstate__(self):
        return {'partd': self.partd}

    def _iset(self, key, value, **kwargs):
        return self.partd._iset(key, value, **kwargs)

    def drop(self):
        return self.partd.drop()

    @property
    def lock(self):
        return self.partd.partd.lock

    def __exit__(self, *args):
        self.drop()
        self.partd.__exit__(self, *args)

    def __del__(self):
        self.partd.__del__()


def index_to_header_bytes(ind):
    # These have special `__reduce__` methods, just use pickle
    if isinstance(ind, (pd.DatetimeIndex,
                        pd.MultiIndex,
                        pd.RangeIndex)):
        return None, dumps(ind)

    if isinstance(ind, pd.CategoricalIndex):
        cat = (ind.ordered, ind.categories)
        values = ind.codes
    else:
        cat = None
        values = ind.values

    if is_extension_array_dtype(ind):
        return None, dumps(ind)

    header = (type(ind), {k: getattr(ind, k, None) for k in ind._attributes}, values.dtype, cat)
    bytes = pnp.compress(pnp.serialize(values), values.dtype)
    return header, bytes


def index_from_header_bytes(header, bytes):
    if header is None:
        return pickle.loads(bytes)

    typ, attr, dtype, cat = header
    data = pnp.deserialize(pnp.decompress(bytes, dtype), dtype, copy=True)
    if cat:
        data = pd.Categorical.from_codes(data, cat[1], ordered=cat[0])
    return typ.__new__(typ, data=data, **attr)


def block_to_header_bytes(block):
    values = block.values
    if isinstance(values, pd.Categorical):
        extension = ('categorical_type', (values.ordered, values.categories))
        values = values.codes
    elif isinstance(block, pd.DatetimeTZDtype):
        extension = ('datetime64_tz_type', (block.values.tzinfo,))
        values = values.view('i8')
    elif is_extension_array_dtype(block.dtype) or is_extension_array(values):
        extension = ("other", ())
    else:
        extension = ('numpy_type', ())

    header = (block.mgr_locs.as_array, values.dtype, values.shape, extension)
    if extension == ("other", ()):
        bytes = pickle.dumps(values)
    else:
        bytes = pnp.compress(pnp.serialize(values), values.dtype)
    return header, bytes


def block_from_header_bytes(header, bytes, create_block: bool):
    placement, dtype, shape, (extension_type, extension_values) = header

    if extension_type == "other":
        values = pickle.loads(bytes)
    else:
        values = pnp.deserialize(pnp.decompress(bytes, dtype), dtype,
                                 copy=True).reshape(shape)
    if extension_type == 'categorical_type':
        values = pd.Categorical.from_codes(values,
                                           extension_values[1],
                                           ordered=extension_values[0])
    elif extension_type == 'datetime64_tz_type':
        tz_info = extension_values[0]
        values = pd.DatetimeIndex(values).tz_localize('utc').tz_convert(
            tz_info)
    if create_block:
        return make_block(values, placement=placement)
    return values, placement


def serialize(df):
    """ Serialize and compress a Pandas DataFrame

    Uses Pandas blocks, snappy, and blosc to deconstruct an array into bytes
    """
    col_header, col_bytes = index_to_header_bytes(df.columns)
    ind_header, ind_bytes = index_to_header_bytes(df.index)
    headers = [col_header, ind_header]
    bytes = [col_bytes, ind_bytes]

    for block in df._mgr.blocks:
        h, b = block_to_header_bytes(block)
        headers.append(h)
        bytes.append(b)

    frames = [dumps(headers)] + bytes
    return b''.join(map(frame, frames))


def deserialize(bytes):
    """ Deserialize and decompress bytes back to a pandas DataFrame """
    frames = list(framesplit(bytes))
    headers = pickle.loads(frames[0])
    bytes = frames[1:]
    axes = [index_from_header_bytes(headers[0], bytes[0]),
            index_from_header_bytes(headers[1], bytes[1])]
    blocks = [block_from_header_bytes(h, b, create_block=not PANDAS_GE_300)
              for (h, b) in zip(headers[2:], bytes[2:])]
    if PANDAS_GE_300:
        return pd.api.internals.create_dataframe_from_blocks(blocks, axes[1], axes[0])
    elif PANDAS_GE_210:
        return pd.DataFrame._from_mgr(create_block_manager_from_blocks(blocks, axes), axes=axes)
    else:
        return pd.DataFrame(create_block_manager_from_blocks(blocks, axes))


def join(dfs):
    if not dfs:
        return pd.DataFrame()
    else:
        result = pd.concat(dfs)
        dtypes = {
            col: "category"
            for col in result.columns
            if (
                isinstance(dfs[0][col].dtype, pd.CategoricalDtype)
                and not isinstance(result[col].dtype, pd.CategoricalDtype)
            )
        }
        if dtypes:
            result = result.astype(dtypes)
        return result

PandasBlocks = partial(Encode, serialize, deserialize, join)

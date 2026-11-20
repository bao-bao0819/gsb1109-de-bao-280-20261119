from functools import partial
import pickle

import numpy as np
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


class AmbiguousColumnError(ValueError):
    """A column name matches more than one positional column."""


def _column_label_key(key, label):
    """ Storage key of a column in the legacy unique-name layout """
    return extend(key, label)


def _column_position_key(key, position):
    """ Storage key of a column in the positional layout """
    return extend(key, ('.col', position))


class PandasColumns(Interface):
    # Legacy partitions (written by older partd) store one file per unique
    # column name and cannot represent duplicate column names.  New partitions
    # that contain duplicate names store every column at its position under
    # ``.col/<position>`` and carry a ``.layout`` marker.
    def __init__(self, partd=None):
        self.partd = pnp.Numpy(partd)
        Interface.__init__(self)

    def _read_raw(self, keys, **kwargs):
        return self.partd.partd.get(keys, **kwargs)

    def _partition_layout(self, key, **kwargs):
        """ Return (mode, stored_columns) for one partition key.

        mode is ``'positional'`` or ``'legacy'``; stored_columns is the
        pickled column-label list or ``None`` for an empty partition.
        """
        layout_raw, columns_raw = self._read_raw(
            [extend(key, '.layout'), extend(key, '.columns')], **kwargs)
        if layout_raw:
            layout = pickle.loads(layout_raw)
            if layout[0] != 'positional':
                raise ValueError("Unknown partition layout %r" % (layout,))
            columns = pickle.loads(columns_raw) if columns_raw else None
            return 'positional', columns
        return 'legacy', pickle.loads(columns_raw) if columns_raw else None

    def _existing_dtypes(self, key, ncolumns, **kwargs):
        dtype_keys = [suffix(_column_position_key(key, i), '.dtype')
                      for i in range(ncolumns)]
        return [pnp.parse_dtype(raw) for raw in self._read_raw(dtype_keys, **kwargs)]

    def _validate_append(self, data):
        """ Validate every frame before anything is written to disk.

        Returns a list of (key, df, mode, column_keys) describing how each
        frame must be stored.
        """
        plan = []
        for key, df in data.items():
            mode, stored_columns = self._partition_layout(key, lock=False)
            labels = list(df.columns)
            duplicate_names = len(set(labels)) != len(labels)

            if mode == 'legacy':
                if stored_columns is not None and duplicate_names:
                    raise ValueError(
                        "Partition %r uses the legacy unique-column-name "
                        "layout and cannot store duplicate column names %r"
                        % (key, labels))
                if duplicate_names:
                    mode = 'positional'
                else:
                    column_keys = [_column_label_key(key, label)
                                   for label in labels]
                    plan.append((key, df, 'legacy', column_keys))
                    continue

            if stored_columns is not None:
                if len(stored_columns) != len(labels):
                    raise ValueError(
                        "Partition %r holds %d columns but the new block has "
                        "%d; refusing to append a block that does not match "
                        "the positional layout"
                        % (key, len(stored_columns), len(labels)))
                existing_dtypes = self._existing_dtypes(key, len(labels),
                                                         lock=False)
                for position, (existing, current) in enumerate(zip(
                        existing_dtypes,
                        [df.iloc[:, i].values.dtype for i in range(len(labels))])):
                    if existing != current:
                        raise ValueError(
                            "Column at position %d of partition %r has dtype "
                            "%s in storage but %s in the new block; the "
                            "types do not match the positional layout"
                            % (position, key, existing, current))

            column_keys = [_column_position_key(key, i)
                           for i in range(len(labels))]
            plan.append((key, df, 'positional', column_keys))
        return plan

    def append(self, data, **kwargs):
        plan = self._validate_append(data)

        arrays = {}
        for key, df, mode, column_keys in plan:
            self.iset(extend(key, '.columns'), dumps(list(df.columns)))
            self.iset(extend(key, '.index-name'), dumps(df.index.name))
            if mode == 'positional':
                self.iset(extend(key, '.layout'),
                          dumps(('positional', len(df.columns))))
            arrays.update({storage_key: df.iloc[:, i].values
                           for i, storage_key in enumerate(column_keys)})
            arrays[extend(key, '.index')] = df.index.values

        # Validation above ran before any write, so a block rejected for a
        # layout/dtype mismatch never reaches this point.
        self.partd.append(arrays, **kwargs)

    def _resolve_columns(self, key, mode, stored_columns, columns):
        """ Map requested columns to storage positions and labels.

        Integer selectors always address a position.  Label selectors address
        every position carrying that label; in a positional partition a label
        shared by several columns raises rather than silently picking one.
        """
        ncolumns = len(stored_columns)
        if columns is None:
            positions = list(range(ncolumns))
        else:
            positions = []
            for selector in columns:
                is_position = (isinstance(selector, (int, np.integer))
                               and not isinstance(selector, bool))
                if mode == 'positional' and is_position:
                    position = int(selector)
                    if position < 0 or position >= ncolumns:
                        raise IndexError(
                            "Column position %d out of range for partition "
                            "%r with %d columns"
                            % (position, key, ncolumns))
                    positions.append(position)
                    continue

                matches = [i for i, label in enumerate(stored_columns)
                           if label == selector]
                if not matches:
                    raise KeyError(
                        "Column %r not found in partition %r"
                        % (selector, key))
                if mode == 'positional' and len(matches) > 1:
                    raise AmbiguousColumnError(
                        "Column name %r is ambiguous in partition %r: it "
                        "matches positions %s. Select a position explicitly "
                        "instead." % (selector, key, matches))
                positions.append(matches[0])

        selected_labels = [stored_columns[i] for i in positions]
        if mode == 'positional':
            storage_keys = [_column_position_key(key, i) for i in positions]
        else:
            storage_keys = [_column_label_key(key, stored_columns[i])
                            for i in positions]
        return positions, selected_labels, storage_keys

    def _get(self, keys, columns=None, **kwargs):
        layouts = [self._partition_layout(key, **kwargs) for key in keys]
        if columns is not None:
            requested = [columns] * len(keys)
        else:
            requested = [None] * len(keys)

        resolved = []
        for key, (mode, stored_columns), selection in zip(keys, layouts,
                                                          requested):
            if stored_columns is None:
                raise KeyError("Partition %r does not exist" % (key,))
            _, labels, storage_keys = self._resolve_columns(
                key, mode, stored_columns, selection)
            resolved.append((key, mode, labels, storage_keys))

        index_names = map(pickle.loads, self._read_raw(
            [extend(key, '.index-name') for key, _, _, _ in resolved],
            **kwargs))

        nested_keys = [[extend(key, '.index'), storage_keys]
                       for key, _, _, storage_keys in resolved]
        arrays = self.partd.get(nested_keys, **kwargs)

        frames = []
        for iname, (index, column_arrays), (_, _, labels, _) in zip(
                index_names, arrays, resolved):
            result = pd.DataFrame(
                {i: values for i, values in enumerate(column_arrays)})
            result.columns = labels
            result.index = pd.Index(index, name=iname)
            frames.append(result)
        return frames

    def __getstate__(self):
        return {'partd': self.partd}

    def _iset(self, key, value):
        return self.partd._iset(key, value)

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

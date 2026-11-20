"""Upstream schema gate for incremental patsy feeds.

The fix lives here, *outside* patsy: every chunk leaving the data factory is
normalized before ``incr_dbuilder`` ever sees it, so the first chunk cannot
freeze a string factor as numeric and every chunk advertises the same level
order. No patsy source file is modified.
"""

import pandas as pd

# Frozen training schema for the demo factor `grp`: non-lexicographic order,
# deliberately different from pandas' default sorting.
CANON_LEVELS = ("c", "a", "b")
# Original full-table training column order, enforced on every chunk.
TRAIN_COLUMNS = ["x", "grp"]
SCHEMA = TRAIN_COLUMNS


class SchemaError(ValueError):
    """Raised when a block carries labels outside the frozen schema."""


def normalize_block(raw, levels=CANON_LEVELS, column_order=TRAIN_COLUMNS):
    """Return a chunk aligned to the frozen training schema.

    * reindexes to ``column_order`` so patsy sees a stable column order;
    * rejects unknown labels explicitly instead of letting pandas coerce
      them silently to NaN inside ``Categorical(...)``;
    * always emits the categorical column as a pandas Categorical whose
      declared categories equal ``levels``, even when the block is all empty.
    """
    frame = raw.reindex(columns=list(column_order))
    values = frame["grp"].tolist()
    unknown = [value for value in values if not pd.isna(value) and value not in levels]
    if unknown:
        raise SchemaError(
            "labels %r are outside the frozen schema %r" % (unknown, tuple(levels))
        )
    frame["grp"] = pd.Categorical(values, categories=list(levels))
    return frame

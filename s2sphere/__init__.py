"""Pure Python implementation of s2-geometry library."""
# flake8: noqa  (don't lint this file)

from __future__ import absolute_import
from .sphere import *
from .partitioning import (
    DEFAULT_LEVEL,
    DeviceHeader,
    DeviceSample,
    FastSample,
    PartitionError,
    PartitionResolver,
    PartitionResult,
    RouteSource,
    partition_all,
    partition_row,
    partition_rows,
)

__version__ = '0.2.5'

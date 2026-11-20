#  Copyright 2026 Lars Butler & individual contributors
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Record-oriented WKB loading with explicit coordinate and depth budgets.

A spatial store receives WKB records one at a time, and the length of every
record is known up front. This module frames those records and decodes them
through :func:`geomet.wkb.loads`, making sure that:

- a record which declares too many vertices is reported as
  :data:`STATUS_COORDINATE_BUDGET_EXCEEDED`;
- a record whose payload ends before its declared structure is fully read is
  reported as :data:`STATUS_TRUNCATED`;
- a record nested beyond the depth limit is reported as
  :data:`STATUS_DEPTH_EXCEEDED`;
- a failing record is contained by its own frame, so the next record is
  decoded independently.

The status strings are stable so callers can route records to different
dead-letter handling without inspecting exception classes.
"""

from geomet.util import take
from geomet import wkb

#: The record decoded successfully; the payload is a GeoJSON `dict`.
STATUS_OK = 'ok'
#: The record declared more vertices than the coordinate budget allows.
STATUS_COORDINATE_BUDGET_EXCEEDED = 'coordinate_budget_exceeded'
#: The record's geometry collections are nested beyond the depth limit.
STATUS_DEPTH_EXCEEDED = 'depth_exceeded'
#: The record ended before its declared payload could be read in full.
STATUS_TRUNCATED = 'truncated'
#: The record was malformed in some other way (bad endian byte, unknown
#: geometry type, ...).
STATUS_INVALID = 'invalid'

_STATUS_FOR_ERROR = {
    wkb.WKBCoordinateBudgetExceeded: STATUS_COORDINATE_BUDGET_EXCEEDED,
    wkb.WKBDepthExceeded: STATUS_DEPTH_EXCEEDED,
    wkb.WKBTruncated: STATUS_TRUNCATED,
}


def classify(error):
    """Map a WKB decoding error to one of the ``STATUS_*`` constants.

    :param error:
        The exception raised while decoding a record. Budget and truncation
        errors map to their dedicated statuses; any other `ValueError` maps
        to :data:`STATUS_INVALID`.
    """
    status = _STATUS_FOR_ERROR.get(type(error))
    if status is None:
        return STATUS_INVALID
    return status


def frame_records(stream, record_lengths):
    """Split a byte stream into records using caller-provided lengths.

    The record boundary is an explicit caller contract: ``record_lengths``
    gives the exact byte length of each record in stream order. The stream
    may arrive in arbitrary chunks (for example segmented network reads),
    because framing only relies on byte iteration.

    :param stream:
        Iterable of bytes containing concatenated WKB records.
    :param record_lengths:
        Iterable of non-negative integers, one explicit length per record.
    :returns:
        Generator of ``(index, record)`` tuples where ``record`` is a byte
        string of exactly the requested length.
    :raises geomet.wkb.WKBTruncated:
        If the stream ends before a full record can be framed.
    """
    stream = iter(stream)
    for index, length in enumerate(record_lengths):
        record = bytes(take(length, stream))
        if len(record) != length:
            raise wkb.WKBTruncated(
                ['record[%d]' % index], length, len(record)
            )
        yield index, record


def load_records(records, coordinate_budget=None, max_depth=None):
    """Decode framed WKB records without letting one bad record stop the run.

    :param records:
        Iterable of ``(index, record)`` tuples as produced by
        :func:`frame_records`, where each ``record`` is the exact byte
        string of one WKB record.
    :param int coordinate_budget:
        Optional per-record vertex budget, forwarded to
        :func:`geomet.wkb.loads`.
    :param int max_depth:
        Optional per-record geometry nesting limit, forwarded to
        :func:`geomet.wkb.loads`.
    :returns:
        Generator of ``(index, status, payload)`` tuples. On success
        ``status`` is :data:`STATUS_OK` and ``payload`` is the decoded
        GeoJSON `dict`; otherwise ``status`` is one of the error constants
        and ``payload`` is the original exception, which carries the layer
        path and the counted or over-limit metrics for the failure.
    """
    for item in records:
        index, record = item
        try:
            geometry = wkb.loads(
                record,
                coordinate_budget=coordinate_budget,
                max_depth=max_depth,
            )
        except ValueError as error:
            yield index, classify(error), error
        else:
            yield index, STATUS_OK, geometry

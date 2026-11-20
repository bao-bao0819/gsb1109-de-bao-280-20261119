#  Copyright 2013 Lars Butler & individual contributors
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
"""Structural inspection of WKB without decoding coordinate objects.

The importer functions in :mod:`geomet.wkb` build GeoJSON coordinate lists.
A structural profile instead reports only what it actually verified: each
member's header, declared child counts, and byte ranges.  Vertex values are
skipped, never materialised, so profiling memory is proportional to the
number of rings and nested members rather than to the number of vertices in
the first polygon.

The profiler is bounded by the record end supplied by the caller.  A
truncated record therefore cannot borrow bytes from the record which follows
it, and a member whose payload ends early is reported as incomplete rather
than as a complete structure.
"""

import struct

#: The member has no child geometry members.  This covers Point, LineString
#: and Polygon (including an empty polygon), and a collection which declares
#: zero children.
STATUS_NO_MEMBERS = 'no_members'
#: A container whose declared child members were all verified.
STATUS_COMPLETE = 'complete'
#: The current member's payload or one of its children ended before the
#: declared structure was verified.
STATUS_INCOMPLETE = 'incomplete'
#: The type declaration does not name a WKB geometry type which can be read.
STATUS_UNREADABLE_TYPE = 'unreadable_type'

#: Aliases for callers which prefer the wording of the public contract.
STATUS_EMPTY = STATUS_NO_MEMBERS
STATUS_TRUNCATED = STATUS_INCOMPLETE
STATUS_UNKNOWN_TYPE = STATUS_UNREADABLE_TYPE

STATUSES = (
    STATUS_NO_MEMBERS,
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    STATUS_UNREADABLE_TYPE,
)

_BASE_GEOMETRY_TYPES = {
    1: 'Point',
    2: 'LineString',
    3: 'Polygon',
    4: 'MultiPoint',
    5: 'MultiLineString',
    6: 'MultiPolygon',
    7: 'GeometryCollection',
}

#: ISO WKB encodes dimensionality as a multiple of 1000 added to the base
#: type number.
_ISO_DIMENSIONS = {
    0: (2, '2D'),
    1: (3, 'Z'),
    2: (3, 'M'),
    3: (4, 'ZM'),
}

_EWKB_Z = 0x80000000
_EWKB_M = 0x40000000
_EWKB_SRID = 0x20000000
_EWKB_FLAGS = _EWKB_Z | _EWKB_M | _EWKB_SRID

_BYTES_PER_VALUE = 8
_COUNT_SIZE = 4
_HEADER_SIZE = 5
_SRID_SIZE = 4

DEFAULT_CHUNK_SIZE = 65536
DEFAULT_MAX_DEPTH = 512


class WKBProfileError(ValueError):
    """Raised when the profiler cannot begin reading its input at all."""


class ProfileDict(dict):
    """A dict whose keys are also readable as attributes.

    A small set of synonymous keys is accepted so callers can use either
    ``member.geometry_type``/``member['type']`` or
    ``member.byte_range``/``member['span']``.  The canonical key is always
    present in the mapping; aliases are resolved on access.
    """

    _ALIASES = {
        'type': 'geometry_type',
        'geom_type': 'geometry_type',
        'geometry': 'geometry_type',
        'range': 'byte_range',
        'span': 'byte_range',
        'bytes': 'byte_range',
        'begin': 'start',
        'offset': 'start',
        'stop': 'end',
        'limit': 'end',
        'children': 'members',
        'declared': 'declared_count',
        'actual': 'parsed_count',
        'read_count': 'parsed_count',
        'found': 'parsed_count',
        'level': 'depth',
        'number': 'index',
        'header_bytes': 'header_range',
        'payload_bytes': 'payload_range',
        'srid_value': 'srid',
    }

    def _resolve(self, key):
        if dict.__contains__(self, key):
            return key
        alias = self._ALIASES.get(key)
        if alias is not None and dict.__contains__(self, alias):
            return alias
        return key

    def __getitem__(self, key):
        return dict.__getitem__(self, self._resolve(key))

    def __contains__(self, key):
        return dict.__contains__(self, self._resolve(key))

    def get(self, key, default=None):
        key = self._resolve(key)
        return dict.get(self, key, default)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value

    def as_dict(self):
        """Return a plain dict recursively."""
        return _plain(self)

    def to_dict(self):
        return self.as_dict()

    def walk(self, include_self=True):
        """Yield this record/member and its verified descendants."""
        if include_self:
            yield self
        stack = list(reversed(self.get('members', ())))
        while stack:
            member = stack.pop()
            yield member
            stack.extend(reversed(member.get('members', ())))

    def find_incomplete(self, include_self=True):
        """Return the first unverified member as a path, or ``None``.

        ``unreadable_type`` counts as unverified; ``no_members`` and
        ``complete`` do not.  The record root is searched last, so a caller
        learns about the leaf that actually failed to verify.
        """
        candidates = list(self.walk(include_self=False))
        if include_self:
            candidates.append(self)
        for member in candidates:
            if member.get('status') in (
                STATUS_INCOMPLETE, STATUS_UNREADABLE_TYPE,
            ):
                return member
        return None


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _decode_type(raw_type):
    """Return ``(geometry_type, base_code, dimensions, dimension_label)``.

    ``raw_type`` is the unsigned value of the declared WKB type field.  Both
    ISO type numbers (1001, 2001, 3001, ...) and the EWKB high-bit flags are
    understood.  An unknown type yields ``None`` for the type and
    dimensions, which the caller reports as ``unreadable_type``.
    """
    has_z = bool(raw_type & _EWKB_Z)
    has_m = bool(raw_type & _EWKB_M)
    base = raw_type & ~_EWKB_FLAGS

    iso_offset, base_code = divmod(base, 1000)
    geometry_type = _BASE_GEOMETRY_TYPES.get(base_code)
    if geometry_type is None:
        return None, base_code, None, None

    if has_z or has_m:
        if has_z and has_m:
            dimensions, label = 4, 'ZM'
        elif has_z:
            dimensions, label = 3, 'Z'
        else:
            dimensions, label = 3, 'M'
    else:
        dimensions, label = _ISO_DIMENSIONS.get(iso_offset, (None, None))

    if dimensions is None:
        return None, base_code, None, None

    return geometry_type, base_code, dimensions, label


def _token(byte_order):
    return '>' if byte_order == 'big' else '<'


def _issue(reason, path, start, end, expected_end=None, available=None,
           declared=None, message='', **extra):
    problem = ProfileDict({
        'reason': reason,
        'path': list(path),
        'start': start,
        'end': end,
        'byte_range': (start, end),
        'expected_end': expected_end,
        'available': available,
        'declared': declared,
        'missing': (
            None if expected_end is None else max(0, expected_end - end)
        ),
        'message': message,
    })
    problem.update(extra)
    return problem


class _InputReader(object):
    """Bounded reader over bytes, a readable stream, or chunk iterables.

    The reader never obtains bytes at or beyond the record limit; a request
    which would cross that position stops at it.  This is what keeps a
    truncated record from borrowing bytes from the following record.

    ``end`` is absolute for bytes-like and seekable sources.  For a
    non-seekable source, including an iterable of chunks, it is the record
    length measured from ``start``.
    """

    def __init__(self, source, start=None, end=None,
                 chunk_size=DEFAULT_CHUNK_SIZE):
        self._chunk_size = max(1, int(chunk_size))
        self._kind = None
        self._view = None
        self._stream = None
        self._seekable = False
        self._chunks = None
        self._pending_view = None
        self._pending_pos = 0
        self._eof = False
        #: True when ``end`` came from the caller's record boundary, so a
        #: profile may report unread bytes after a verified geometry.
        self.has_declared_end = end is not None

        if isinstance(source, (bytes, bytearray, memoryview)):
            self._kind = 'bytes'
            self._view = memoryview(source)
            size = len(self._view)
            if start is None:
                start = 0
            if start < 0 or start > size:
                raise WKBProfileError(
                    'record start %r is outside the input length %d'
                    % (start, size)
                )
            if end is None:
                end = size
            elif end < start:
                raise WKBProfileError(
                    'record range [%r, %r) is invalid' % (start, end)
                )
            self._limit = end
            self._available_end = size
            if end > size:
                # The caller described a longer record than this buffer
                # holds.  Keep their end for reporting, but only the bytes
                # which exist can be verified.
                self._available_end = size
        elif callable(getattr(source, 'read', None)):
            self._kind = 'stream'
            self._stream = source
            tell = getattr(source, 'tell', None)
            seek = getattr(source, 'seek', None)
            try:
                current = tell() if callable(tell) else None
            except (OSError, ValueError):
                current = None
            self._seekable = callable(seek) and current is not None

            if start is None:
                start = current if current is not None else 0
            elif start < 0:
                raise WKBProfileError('start must be non-negative')

            if self._seekable and start != current:
                source.seek(start)
            elif not self._seekable and current is not None \
                    and start > current:
                self._discard(start - current)

            if end is None:
                if self._seekable:
                    source.seek(0, 2)
                    end = source.tell()
                    source.seek(start)
                self._limit = end
                self._available_end = end
            else:
                if not self._seekable:
                    if end < 0:
                        raise WKBProfileError('end must be non-negative')
                    self._limit = start + end
                    self._available_end = None
                else:
                    if end < start:
                        raise WKBProfileError(
                            'record range [%r, %r) is invalid'
                            % (start, end)
                        )
                    if end == start and start:
                        end = start + end
                    self._limit = end
                    source.seek(0, 2)
                    self._available_end = source.tell()
                    source.seek(start)
        else:
            self._chunks = iter(source)
            self._kind = 'iter'
            if start is None:
                start = 0
            if start < 0:
                raise WKBProfileError('start must be non-negative')
            if start:
                self._discard(start)
            if end is None:
                self._limit = None
            else:
                if end < 0:
                    raise WKBProfileError('end must be non-negative')
                self._limit = start + end
            self._available_end = None

        self._start = start
        self.pos = start

    @property
    def start(self):
        return self._start

    @property
    def limit(self):
        return self._limit

    @property
    def bound(self):
        """The furthest absolute position this reader may obtain."""
        if self._limit is None:
            return self._available_end
        if self._available_end is None:
            return self._limit
        return min(self._limit, self._available_end)

    @property
    def available_end(self):
        """The furthest absolute position known to hold bytes."""
        return self._available_end

    def relative(self, absolute):
        return absolute - self._start

    def _pending_bytes(self, size):
        """Take up to ``size`` bytes from the pending chunk, zero-copy."""
        view = self._pending_view
        if view is None:
            return b''
        available = len(view) - self._pending_pos
        take = min(size, available)
        data = view[self._pending_pos:self._pending_pos + take]
        self._pending_pos += take
        if self._pending_pos >= len(view):
            self._pending_view = None
            self._pending_pos = 0
        self.pos += take
        return bytes(data)

    def _store_pending(self, data):
        self._pending_view = memoryview(data)
        self._pending_pos = 0

    def _read_raw(self, size):
        """Return up to ``size`` bytes; fewer only at end of input."""
        if size <= 0:
            return b''

        if self._kind == 'bytes':
            data = bytes(self._view[self.pos:self.pos + size])
            self.pos += len(data)
            return data

        if self._kind == 'stream':
            if self._pending_view is not None:
                return self._pending_bytes(size)
            data = self._stream.read(size)
            if data is None:
                return b''
            data = memoryview(data)
            if len(data) > size:
                # A cooperative stream may return more than requested; keep
                # the surplus without advancing over the record bound.
                data, surplus = data[:size], data[size:]
                self._pending_view = surplus
                self._pending_pos = 0
            data = bytes(data)
            self.pos += len(data)
            if not data:
                self._eof = True
            return data

        # Iterator of chunks: retain only a view of the unconsumed tail of
        # one chunk, so a large chunk is never copied or split up front.
        if self._pending_view is not None:
            return self._pending_bytes(size)
        if self._eof:
            return b''
        while True:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                self._eof = True
                return b''
            if isinstance(chunk, int):
                chunk = bytes((chunk,))
            elif not isinstance(chunk, (bytes, bytearray, memoryview)):
                chunk = bytes(chunk)
            view = memoryview(chunk)
            if not len(view):
                continue
            if len(view) <= size:
                data = bytes(view)
                self.pos += len(data)
                return data
            data = bytes(view[:size])
            self._pending_view = view[size:]
            self._pending_pos = 0
            self.pos += size
            return data

    def _discard(self, size):
        """Advance ``size`` bytes, returning how many were actually skipped."""
        if size <= 0:
            return 0
        if self._kind == 'bytes':
            skipped = min(size, len(self._view) - self.pos)
            self.pos += skipped
            return skipped
        skipped = 0
        while skipped < size:
            data = self._read_raw(min(size - skipped, self._chunk_size))
            if not data:
                break
            skipped += len(data)
        return skipped

    def _allowed(self, size):
        bound = self.bound
        if bound is not None:
            available = bound - self.pos
            if size > available:
                return max(0, available)
        return size

    def read_exact(self, size):
        """Return exactly ``size`` bytes, or ``None`` if the bound ends first.

        Whatever remains available is consumed before ``None`` is returned,
        so ``pos`` reflects the bytes actually inspected.
        """
        if size < 0:
            raise ValueError('size must be non-negative')
        if size == 0:
            return b''
        wanted = self._allowed(size)
        chunks = []
        remaining = wanted
        while remaining:
            data = self._read_raw(remaining)
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
        if wanted != size or remaining:
            return None
        return b''.join(chunks)

    def skip(self, size):
        """Advance past ``size`` bytes without retaining them.

        Returns ``False`` if the bound or end of input came first; the reader
        is then positioned at that bound.
        """
        if size < 0:
            raise ValueError('size must be non-negative')
        if size == 0:
            return True

        wanted = self._allowed(size)
        if wanted != size:
            self._discard(wanted)
            return False

        if self._kind == 'bytes':
            self.pos += size
            return True

        if self._kind == 'stream' and self._seekable:
            try:
                self._stream.seek(size, 1)
                self.pos += size
                return True
            except (OSError, ValueError):
                self._seekable = False

        return self._discard(size) == size


def _read_header(reader, path):
    """Read one member header.

    Returns ``(header, issue)``.  ``header`` is ``None`` when no full header
    could be read; ``issue`` then describes the verified prefix.
    """
    start_abs = reader.pos
    start = reader.relative(start_abs)

    endian_byte = reader.read_exact(1)
    if endian_byte is None:
        return None, _issue(
            'truncated_header', path, start, reader.relative(reader.pos),
            expected_end=start + 1, available=0,
            message='member header is missing',
        )

    if endian_byte == b'\x00':
        byte_order = 'big'
    elif endian_byte == b'\x01':
        byte_order = 'little'
    else:
        return None, _issue(
            'invalid_endian', path, start, reader.relative(reader.pos),
            declared=endian_byte[0], available=1,
            message='invalid endian byte 0x%s' % endian_byte.hex(),
        )

    token = _token(byte_order)
    type_start_abs = reader.pos
    type_bytes = reader.read_exact(_COUNT_SIZE)
    if type_bytes is None:
        return None, _issue(
            'truncated_type', path, start, reader.relative(reader.pos),
            expected_end=reader.relative(type_start_abs) + _COUNT_SIZE,
            available=reader.pos - type_start_abs,
            message='member type field is incomplete',
        )

    [raw_type] = struct.unpack(token + 'I', type_bytes)
    geometry_type, base_code, dimensions, dimension_label = (
        _decode_type(raw_type)
    )
    has_srid = bool(raw_type & _EWKB_SRID)
    srid = None
    srid_range = None
    if has_srid:
        srid_start_abs = reader.pos
        srid_bytes = reader.read_exact(_SRID_SIZE)
        if srid_bytes is None:
            return None, _issue(
                'truncated_srid', path, start, reader.relative(reader.pos),
                expected_end=reader.relative(srid_start_abs) + _SRID_SIZE,
                available=reader.pos - srid_start_abs,
                message='SRID field is incomplete',
            )
        [srid] = struct.unpack(token + 'i', srid_bytes)
        srid_range = (
            reader.relative(srid_start_abs), reader.relative(reader.pos)
        )

    header_end = reader.relative(reader.pos)
    header = ProfileDict({
        'start': start,
        'end': header_end,
        'byte_range': (start, header_end),
        'endian_range': (start, start + 1),
        'type_range': (start + 1, start + _HEADER_SIZE),
        'srid_range': srid_range,
        'byte_order': byte_order,
        'type_code': raw_type,
        'base_type_code': base_code,
        'geometry_type': geometry_type,
        'dimensions': dimensions,
        'dimension_label': dimension_label,
        'has_srid': has_srid,
        'srid': srid,
    })
    return header, None


def _read_count(reader, token, path, what):
    """Return ``(count, start, end, issue)``.

    ``count`` is ``None`` when the count could not be read or is negative;
    ``issue`` then explains why.  ``start``/``end`` always give the verified
    span of the count field itself.
    """
    start_abs = reader.pos
    start = reader.relative(start_abs)
    raw = reader.read_exact(_COUNT_SIZE)
    if raw is None:
        return None, start, reader.relative(reader.pos), _issue(
            'truncated_count', path, start, reader.relative(reader.pos),
            expected_end=start + _COUNT_SIZE,
            available=reader.pos - start_abs,
            message='%s count is incomplete' % what,
        )
    [count] = struct.unpack(token + 'i', raw)
    end = reader.relative(reader.pos)
    if count < 0:
        return None, start, end, _issue(
            'negative_count', path, start, end,
            declared=count, available=_COUNT_SIZE,
            message='negative %s count' % what,
        )
    return count, start, end, None


def _new_member(index, path, depth, reader, start_abs):
    start = reader.relative(start_abs)
    return ProfileDict({
        'index': index,
        'path': list(path),
        'depth': depth,
        'start': start,
        'end': None,
        'byte_range': (start, None),
        'absolute_start': start_abs,
        'absolute_end': None,
        'status': STATUS_INCOMPLETE,
        'state': STATUS_INCOMPLETE,
        'kind': STATUS_INCOMPLETE,
        'complete': False,
        'truncated': False,
        'incomplete': False,
        'unreadable': False,
        'type_readable': True,
        'has_members': False,
        'geometry_type': None,
        'type_code': None,
        'base_type_code': None,
        'byte_order': None,
        'dimensions': None,
        'dimension_label': None,
        'has_srid': False,
        'srid': None,
        'declaration': None,
        'header': None,
        'header_range': None,
        'endian_range': None,
        'type_range': None,
        'srid_range': None,
        'payload_range': None,
        'declared_count': None,
        'parsed_count': 0,
        'member_count': 0,
        'members': [],
        'rings': [],
        'ring_count': None,
        'vertex_count': None,
        'parsed_vertex_count': None,
        'vertex_range': None,
        'expected_end': None,
        'available': None,
        'missing': None,
        'failure_path': None,
        'issue': None,
        'error': None,
        'reason': None,
    })


def _set_result(reader, node, status, complete, issue=None):
    node['end'] = reader.relative(reader.pos)
    node['byte_range'] = (node['start'], node['end'])
    node['absolute_end'] = reader.pos
    node['status'] = status
    node['state'] = status
    node['kind'] = status
    node['complete'] = complete
    node['truncated'] = status == STATUS_INCOMPLETE
    node['incomplete'] = status == STATUS_INCOMPLETE
    node['unreadable'] = status == STATUS_UNREADABLE_TYPE
    node['type_readable'] = status != STATUS_UNREADABLE_TYPE
    node['issue'] = issue

    if issue is not None:
        node['failure_path'] = list(issue.get('path') or ())
        # Only adopt the issue's accounting when it describes this member
        # itself; a container's issue normally describes a child, whose
        # expected end says nothing about where the container should end.
        if list(issue.get('path') or ()) == node['path']:
            node['expected_end'] = issue.get('expected_end')
            node['available'] = issue.get('available')
            node['missing'] = issue.get('missing')

    payload = node.get('payload_range')
    if payload is not None and payload[0] is not None:
        payload_start = payload[0]
        node['payload_range'] = (payload_start, node['end'])
        node['available'] = max(0, node['end'] - payload_start)
        if node['expected_end'] is not None:
            node['missing'] = max(0, node['expected_end'] - node['end'])
    return node


def _leaf_result(reader, node, status=STATUS_NO_MEMBERS, complete=True):
    node['payload_range'] = (
        node['payload_range'][0] if node['payload_range'] else None,
        reader.relative(reader.pos),
    )
    return _set_result(reader, node, status, complete)


def _skip_payload(reader, node, size, path):
    """Skip a leaf payload without retaining its bytes."""
    start_abs = reader.pos
    start = reader.relative(start_abs)
    expected_end = start + size
    node['expected_end'] = expected_end
    node['payload_range'] = (start, None)
    node['available'] = 0
    node['missing'] = size

    if reader.skip(size):
        end = reader.relative(reader.pos)
        node['payload_range'] = (start, end)
        node['available'] = size
        node['missing'] = 0
        return True

    end = reader.relative(reader.pos)
    node['payload_range'] = (start, end)
    node['available'] = max(0, end - start)
    node['missing'] = size - node['available']
    _set_result(reader, node, STATUS_INCOMPLETE, False, _issue(
        'truncated_payload', path, start, end,
        expected_end=expected_end, available=node['available'],
        declared=size,
        message=(
            'coordinate payload is incomplete: %d of %d bytes available'
            % (node['available'], size)
        ),
    ))
    # _set_result replaces expected_end with the issue's value, which is the
    # same payload end; retain the issue's fuller accounting explicitly.
    return False


def _parse_rings(reader, node, ring_count, value_size, token):
    path = node['path']
    payload_start = reader.relative(reader.pos)
    node['payload_range'] = (payload_start, None)
    node['ring_count'] = ring_count
    total_vertices = 0

    for ring_index in range(ring_count):
        ring_start_abs = reader.pos
        ring_start = reader.relative(ring_start_abs)
        count, count_start, count_end, count_issue = _read_count(
            reader, token, path, 'ring vertex'
        )
        if count is None:
            node['rings'].append(ProfileDict({
                'index': ring_index,
                'start': ring_start,
                'end': reader.relative(reader.pos),
                'byte_range': (ring_start, reader.relative(reader.pos)),
                'count_range': (count_start, count_end),
                'vertex_count': None,
                'declared_vertex_count': None,
                'parsed_vertex_count': 0,
                'complete': False,
                'vertex_range': None,
                'expected_end': count_issue.get('expected_end'),
                'available': reader.pos - ring_start_abs,
                'missing': None,
            }))
            if count_issue is not None:
                count_issue['path'] = list(
                    path + ['ring', ring_index]
                )
            _set_result(reader, node, STATUS_INCOMPLETE, False,
                        count_issue)
            node['payload_range'] = (
                payload_start, reader.relative(reader.pos)
            )
            return False

        payload_start_ring = reader.relative(reader.pos)
        expected_end = payload_start_ring + count * value_size
        ring = ProfileDict({
            'index': ring_index,
            'start': ring_start,
            'end': None,
            'byte_range': (ring_start, None),
            'count_range': (count_start, count_end),
            'vertex_count': count,
            'declared_vertex_count': count,
            'parsed_vertex_count': 0,
            'complete': False,
            'vertex_range': (payload_start_ring, None),
            'expected_end': expected_end,
            'available': 0,
            'missing': count * value_size,
        })
        node['rings'].append(ring)

        if not reader.skip(count * value_size):
            end = reader.relative(reader.pos)
            ring['end'] = end
            ring['byte_range'] = (ring_start, end)
            ring['vertex_range'] = (payload_start_ring, end)
            ring['available'] = max(0, end - payload_start_ring)
            ring['missing'] = (
                count * value_size - ring['available']
            )
            ring['parsed_vertex_count'] = (
                ring['available'] // value_size
                if value_size else 0
            )
            total_vertices += ring['parsed_vertex_count']
            _set_result(reader, node, STATUS_INCOMPLETE, False, _issue(
                'truncated_payload',
                path + ['ring', ring_index],
                payload_start_ring, end,
                expected_end=expected_end,
                available=ring['available'],
                declared=count * value_size,
                message=(
                    'ring %d coordinate payload is incomplete: %d of %d '
                    'bytes available'
                    % (ring_index, ring['available'],
                       count * value_size)
                ),
                ring=ring_index,
                vertex_count=count,
            ))
            node['vertex_count'] = total_vertices
            node['parsed_vertex_count'] = total_vertices
            node['payload_range'] = (
                payload_start, reader.relative(reader.pos)
            )
            return False

        end = reader.relative(reader.pos)
        ring['end'] = end
        ring['byte_range'] = (ring_start, end)
        ring['vertex_range'] = (payload_start_ring, end)
        ring['complete'] = True
        ring['available'] = count * value_size
        ring['missing'] = 0
        ring['parsed_vertex_count'] = count
        total_vertices += count

    node['vertex_count'] = total_vertices
    node['parsed_vertex_count'] = total_vertices
    node['payload_range'] = (payload_start, reader.relative(reader.pos))
    return True


def _parse_member(reader, index, path, depth, max_depth):
    node = _new_member(index, path, depth, reader, reader.pos)

    if depth > max_depth:
        return _set_result(reader, node, STATUS_INCOMPLETE, False, _issue(
            'max_depth_exceeded', path, node['start'], node['start'],
            message='maximum member nesting depth exceeded',
        ))

    header, issue = _read_header(reader, path)
    if header is None:
        status = (
            STATUS_UNREADABLE_TYPE
            if issue['reason'] == 'invalid_endian'
            else STATUS_INCOMPLETE
        )
        return _set_result(reader, node, status, False, issue)

    node['header'] = header
    node['header_range'] = header['byte_range']
    node['endian_range'] = header['endian_range']
    node['type_range'] = header['type_range']
    node['srid_range'] = header['srid_range']
    node['geometry_type'] = header['geometry_type']
    node['type_code'] = header['type_code']
    node['base_type_code'] = header['base_type_code']
    node['byte_order'] = header['byte_order']
    node['dimensions'] = header['dimensions']
    node['dimension_label'] = header['dimension_label']
    node['has_srid'] = header['has_srid']
    node['srid'] = header['srid']
    node['payload_range'] = (header['end'], None)
    node['declaration'] = ProfileDict({
        'geometry_type': header['geometry_type'],
        'type_code': header['type_code'],
        'base_type_code': header['base_type_code'],
        'dimensions': header['dimensions'],
        'dimension_label': header['dimension_label'],
        'byte_order': header['byte_order'],
        'has_srid': header['has_srid'],
        'srid': header['srid'],
        'count': None,
    })

    if header['geometry_type'] is None:
        return _set_result(
            reader, node, STATUS_UNREADABLE_TYPE, False, _issue(
                'unreadable_type', path, header['start'], header['end'],
                declared=header['type_code'],
                available=header['end'] - header['start'],
                message='unreadable geometry type %d'
                % header['type_code'],
            )
        )

    geometry_type = header['geometry_type']
    value_size = header['dimensions'] * _BYTES_PER_VALUE
    token = _token(header['byte_order'])

    if geometry_type == 'Point':
        node['vertex_count'] = 1
        node['parsed_vertex_count'] = 0
        if _skip_payload(reader, node, value_size, path):
            node['parsed_vertex_count'] = 1
            node['vertex_range'] = node['payload_range']
            node['declaration']['count'] = 1
            return _leaf_result(reader, node)
        node['vertex_range'] = node['payload_range']
        node['declaration']['count'] = 1
        return node

    if geometry_type == 'LineString':
        count, count_start, count_end, count_issue = _read_count(
            reader, token, path, 'vertex'
        )
        if count is None:
            return _set_result(reader, node, STATUS_INCOMPLETE, False,
                               count_issue)
        node['declared_count'] = count
        node['vertex_count'] = count
        node['declaration']['count'] = count
        payload_start = reader.relative(reader.pos)
        node['vertex_range'] = (payload_start, None)
        if _skip_payload(reader, node, count * value_size, path):
            node['vertex_range'] = node['payload_range']
            node['parsed_vertex_count'] = count
            return _leaf_result(reader, node)
        node['vertex_range'] = node['payload_range']
        node['parsed_vertex_count'] = (
            node['available'] // value_size if value_size else 0
        )
        return node

    if geometry_type == 'Polygon':
        count, count_start, count_end, count_issue = _read_count(
            reader, token, path, 'ring'
        )
        if count is None:
            return _set_result(reader, node, STATUS_INCOMPLETE, False,
                               count_issue)
        node['declared_count'] = count
        node['declaration']['count'] = count
        polygon_payload_start = reader.relative(reader.pos)
        if _parse_rings(reader, node, count, value_size, token):
            node['payload_range'] = (
                polygon_payload_start, reader.relative(reader.pos)
            )
            return _leaf_result(reader, node)
        # Report the payload span consistently for complete and truncated
        # polygons: it starts after the ring count and ends where reading
        # stopped.  Individual ring records retain their own finer spans.
        node['payload_range'] = (
            polygon_payload_start, reader.relative(reader.pos)
        )
        node['available'] = max(
            0, node['end'] - polygon_payload_start
        )
        for ring in reversed(node['rings']):
            if not ring['complete'] and ring['expected_end'] is not None:
                node['expected_end'] = ring['expected_end']
                node['missing'] = max(
                    0, node['expected_end'] - node['end']
                )
                break
        return node

    # Multi* and GeometryCollection members all hold full geometry members.
    count, count_start, count_end, count_issue = _read_count(
        reader, token, path, 'member'
    )
    if count is None:
        return _set_result(reader, node, STATUS_INCOMPLETE, False,
                           count_issue)

    node['declared_count'] = count
    node['declaration']['count'] = count
    node['payload_range'] = (reader.relative(reader.pos), None)

    if count == 0:
        node['payload_range'] = (
            node['payload_range'][0], reader.relative(reader.pos)
        )
        return _set_result(reader, node, STATUS_NO_MEMBERS, True)

    node['has_members'] = True
    for child_index in range(count):
        child_path = tuple(path) + (child_index,)
        child = _parse_member(
            reader, child_index, child_path, depth + 1, max_depth
        )
        node['members'].append(child)
        node['parsed_count'] += 1
        node['member_count'] = len(node['members'])
        if child['complete']:
            continue

        # The child's end is unknown or unverified, so the remaining
        # declared members cannot be trusted either.
        node['payload_range'] = (
            node['payload_range'][0], reader.relative(reader.pos)
        )
        status = (
            STATUS_UNREADABLE_TYPE
            if child['status'] == STATUS_UNREADABLE_TYPE
            else STATUS_INCOMPLETE
        )
        issue = child.get('issue')
        if issue is not None:
            node['expected_end'] = issue.get('expected_end')
            node['available'] = issue.get('available')
            node['missing'] = issue.get('missing')
            node['failure_path'] = list(issue.get('path') or ())
        return _set_result(reader, node, status, False, issue)

    node['payload_range'] = (
        node['payload_range'][0], reader.relative(reader.pos)
    )
    return _set_result(reader, node, STATUS_COMPLETE, True)


def _collect_issues(profile):
    issues = []
    seen = set()

    def visit(node):
        issue = node.get('issue')
        if issue is not None and id(issue) not in seen:
            seen.add(id(issue))
            issues.append(issue)
        for member in node['members']:
            visit(member)

    visit(profile)
    return issues


def _flatten(profile):
    """Return the record's verified members as a flat list."""
    members = [profile]
    stack = list(reversed(profile['members']))
    while stack:
        member = stack.pop()
        members.append(member)
        stack.extend(reversed(member['members']))
    return members


def _profile_reader(reader, max_depth):
    record_start = reader.start
    root = _parse_member(reader, index=None, path=(), depth=0,
                         max_depth=max_depth)
    profile = ProfileDict(root)
    profile['record_start'] = record_start
    profile['record_end'] = None
    profile['record_length'] = None
    profile['bytes_verified'] = max(0, reader.pos - record_start)
    profile['bytes_remaining'] = None
    profile['trailing_range'] = None
    profile['complete_record'] = False

    if reader.limit is not None:
        record_end = reader.limit
        profile['record_end'] = record_end
        profile['record_length'] = record_end - record_start
        remaining = max(0, record_end - reader.pos)
        profile['bytes_remaining'] = remaining
        if remaining:
            profile['trailing_range'] = (
                reader.relative(reader.pos),
                record_end - record_start,
            )
        profile['complete_record'] = bool(
            root['complete'] and remaining == 0
        )
    elif root['complete']:
        # Without an explicit bound the record ends where its root geometry
        # ends.  This keeps profiles over chunked input identical to profiles
        # over an equally long complete bytes object.
        record_end = reader.pos
        profile['record_end'] = record_end
        profile['record_length'] = record_end - record_start
        profile['bytes_remaining'] = 0
        profile['trailing_range'] = None
        profile['complete_record'] = True

    profile['members'] = root['members']
    profile['member_count'] = len(root['members'])
    profile['flat_members'] = [
        member for member in _flatten(profile) if member is not profile
    ]
    profile['layers'] = profile['flat_members']
    profile['record_range'] = (record_start, profile['record_end'])

    by_depth = {}
    for member in profile['flat_members']:
        by_depth.setdefault(member['depth'], []).append(member)
    profile['members_by_depth'] = {
        depth: by_depth[depth] for depth in sorted(by_depth)
    }

    issues = _collect_issues(profile)
    profile['issues'] = issues

    # A verified root geometry does not by itself mean the whole record was
    # verified.  If the caller's record extends past the verified geometry,
    # say so explicitly instead of claiming a complete structure.
    profile['error'] = None
    profile['reason'] = None
    if profile['complete'] and profile['bytes_remaining']:
        supplied_end = reader.available_end
        if supplied_end is not None and reader.pos >= supplied_end:
            reason = 'payload_beyond_supplied_bytes'
        else:
            reason = 'trailing_bytes_not_verified'
        issue = _issue(
            reason, (), reader.relative(reader.pos),
            profile['record_end'] - record_start,
            expected_end=profile['record_end'] - record_start,
            available=max(
                0, (supplied_end or reader.pos) - reader.pos
            ),
            message=(
                '%d record bytes were not verified'
                % profile['bytes_remaining']
            ),
        )
        issue['missing'] = profile['bytes_remaining']
        profile['error'] = STATUS_INCOMPLETE
        profile['reason'] = reason
        profile['complete_record'] = False
        profile['issue'] = issue
        issues.append(issue)
    elif not profile['complete']:
        profile['error'] = profile['status']
        profile['reason'] = (
            profile['issue'].get('reason')
            if profile.get('issue') is not None else None
        )

    return profile


def profile_wkb(source, end=None, start=None, chunk_size=DEFAULT_CHUNK_SIZE,
                length=None, max_depth=DEFAULT_MAX_DEPTH):
    """Profile the structure of one WKB record.

    :param source:
        A bytes-like object, a readable binary stream, or an iterable of
        byte chunks.  Vertex payloads are skipped, never materialised.
    :param end:
        Exclusive end position of the record.  For a bytes-like object or a
        seekable stream this is an absolute offset.  For a non-seekable
        stream or an iterable it is a byte length measured from ``start``.
        Supplying it is what keeps a truncated record from reading into a
        following record.
    :param start:
        Position where the record starts.  Defaults to 0 for bytes-like
        objects and to the current position for a stream.
    :param chunk_size:
        Size of the chunks discarded while skipping vertex payloads on a
        non-seekable input.
    :param length:
        Alternative to ``end``: the number of bytes in the record.
    :param max_depth:
        Maximum nesting depth accepted before the profile is marked
        incomplete.

    :returns:
        A :class:`ProfileDict`.  Data problems appear in ``status`` and
        ``issues`` instead of being raised; only an unusable input range
        raises :class:`WKBProfileError`.

    A member's ``byte_range`` is ``(start, end)`` relative to the record
    start.  ``start`` points at the member's real endian byte, which is the
    beginning of its own sub-header for nested members.
    """
    if length is not None:
        if end is not None:
            raise WKBProfileError('pass either end or length, not both')
        if start is not None:
            end = start + length
        elif isinstance(source, (bytes, bytearray, memoryview)):
            end = length
        else:
            tell = getattr(source, 'tell', None)
            try:
                end = (tell() if callable(tell) else 0) + length
            except (OSError, ValueError):
                end = length

    reader = _InputReader(
        source, start=start, end=end, chunk_size=chunk_size
    )
    return _profile_reader(reader, max_depth)


class WKBProfile(ProfileDict):
    """A profile which can also be built like a dict.

    ``WKBProfile(data, end=len(data))`` is equivalent to
    ``profile_wkb(data, end=len(data))``.
    """

    def __init__(self, source=None, end=None, start=None,
                 chunk_size=DEFAULT_CHUNK_SIZE, length=None,
                 max_depth=DEFAULT_MAX_DEPTH):
        if source is None:
            ProfileDict.__init__(self)
            return
        result = profile_wkb(
            source, end=end, start=start, chunk_size=chunk_size,
            length=length, max_depth=max_depth,
        )
        ProfileDict.__init__(self, result)


#: Named entry points for the structural profiler.  ``profile_wkb`` is the
#: canonical name; the aliases make the intent discoverable from callers.
profile = profile_wkb
inspect = profile_wkb
inspect_wkb = profile_wkb
scan_wkb = profile_wkb
structural_profile = profile_wkb
profile_structure = profile_wkb
describe = profile_wkb
structure = profile_wkb


__all__ = [
    'DEFAULT_CHUNK_SIZE',
    'DEFAULT_MAX_DEPTH',
    'ProfileDict',
    'STATUS_COMPLETE',
    'STATUS_EMPTY',
    'STATUS_INCOMPLETE',
    'STATUS_NO_MEMBERS',
    'STATUS_TRUNCATED',
    'STATUS_UNKNOWN_TYPE',
    'STATUS_UNREADABLE_TYPE',
    'STATUSES',
    'WKBProfile',
    'WKBProfileError',
    'describe',
    'inspect',
    'inspect_wkb',
    'profile',
    'profile_structure',
    'profile_wkb',
    'scan_wkb',
    'structural_profile',
    'structure',
]

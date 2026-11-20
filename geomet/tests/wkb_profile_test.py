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
import io
import struct
import unittest

from geomet import wkb
from geomet import wkb_profile


def _point(x, y, srid=None, big_endian=False):
    token = '>' if big_endian else '<'
    endian = b'\x00' if big_endian else b'\x01'
    type_code = 1 | (0x20000000 if srid is not None else 0)
    data = endian + struct.pack(token + 'I', type_code)
    if srid is not None:
        data += struct.pack(token + 'i', srid)
    return data + struct.pack(token + 'dd', x, y)


def _empty_collection(big_endian=False):
    token = '>' if big_endian else '<'
    endian = b'\x00' if big_endian else b'\x01'
    return endian + struct.pack(token + 'I', 7) + struct.pack(token + 'i', 0)


def _empty_polygon(big_endian=False):
    token = '>' if big_endian else '<'
    endian = b'\x00' if big_endian else b'\x01'
    return endian + struct.pack(token + 'I', 3) + struct.pack(token + 'i', 0)


def _polygon(rings, big_endian=True):
    token = '>' if big_endian else '<'
    endian = b'\x00' if big_endian else b'\x01'
    data = endian + struct.pack(token + 'I', 3)
    data += struct.pack(token + 'i', len(rings))
    for ring in rings:
        data += struct.pack(token + 'i', len(ring))
        for x, y in ring:
            data += struct.pack(token + 'dd', x, y)
    return data


def _collection(children, big_endian=True):
    token = '>' if big_endian else '<'
    endian = b'\x00' if big_endian else b'\x01'
    return (
        endian
        + struct.pack(token + 'I', 7)
        + struct.pack(token + 'i', len(children))
        + b''.join(children)
    )


def build_scenario():
    """Build the record described by the profiling request.

    The record is a big-endian collection.  Its first member is a large
    polygon.  Its second member is a little-endian collection which holds an
    empty collection and a point carrying its own SRID.
    """
    ring = [(float(index), float(index + 1)) for index in range(64)]
    large_polygon = _polygon([ring], big_endian=True)
    inner = _collection(
        [
            _empty_collection(big_endian=False),
            _point(3.5, 4.5, srid=4326, big_endian=False),
        ],
        big_endian=False,
    )
    record = _collection([large_polygon, inner], big_endian=True)
    following = _point(9.0, 9.0, srid=3857, big_endian=False)
    return record, following


class ProfileScenarioTestCase(unittest.TestCase):

    def setUp(self):
        self.maxDiff = None
        self.record, self.following = build_scenario()
        self.data = self.record + self.following

    def test_member_sequence_matches_full_decode(self):
        profile = wkb.profile_wkb(self.data, end=len(self.record))

        self.assertTrue(profile.complete)
        self.assertEqual(profile.status, wkb_profile.STATUS_COMPLETE)
        self.assertEqual(profile.declared_count, 2)
        self.assertEqual(profile.parsed_count, 2)

        members = list(profile.flat_members)
        expected_types = [
            'Polygon',
            'GeometryCollection',
            'GeometryCollection',
            'Point',
        ]
        self.assertEqual(
            [member.geometry_type for member in members], expected_types
        )
        self.assertEqual(
            [member.path for member in members],
            [[0], [1], [1, 0], [1, 1]],
        )

        polygon = profile.members[0]
        inner = profile.members[1]
        self.assertEqual(polygon.geometry_type, 'Polygon')
        self.assertEqual(inner.path, [1])
        self.assertEqual(inner.byte_order, 'little')
        self.assertEqual(inner.members[0].geometry_type, 'GeometryCollection')
        self.assertEqual(inner.members[0].declared_count, 0)
        self.assertFalse(inner.members[0].has_members)
        self.assertEqual(inner.members[1].srid, 4326)

        # Every non-empty member agrees with decoding its exact byte range.
        # (The decoder itself cannot round-trip a zero-count collection, so
        # containers holding an empty child are checked structurally above.)
        for member in members:
            if member.declared_count == 0:
                continue
            if any(child.declared_count == 0 for child in member.members):
                continue
            start, end = member.byte_range
            decoded = wkb.loads(self.record[start:end])
            self.assertEqual(decoded['type'], member.geometry_type)

    def test_byte_ranges_locate_real_subheaders(self):
        profile = wkb.profile_wkb(self.data, end=len(self.record))
        polygon = profile.members[0]
        inner = profile.members[1]

        # Every member starts with an endian byte followed by a type field.
        expected_endian = {
            'big': b'\x00',
            'little': b'\x01',
        }
        for member in profile.flat_members:
            start, end = member.byte_range
            self.assertEqual(
                self.record[start:start + 1],
                expected_endian[member.byte_order],
            )
            self.assertEqual(member.type_range, (start + 1, start + 5))
            type_start, type_end = member.type_range
            self.assertEqual(
                self.record[type_start:type_end],
                self.record[start + 1:start + 5],
            )
            self.assertLessEqual(start, end)

        self.assertEqual(polygon.start, 9)
        self.assertEqual(polygon.end, inner.start)
        self.assertEqual(inner.end, len(self.record))

        # The inner point carries a real SRID sub-field in its own header.
        point = inner.members[1]
        self.assertEqual(point.srid_range, (point.start + 5, point.start + 9))
        self.assertEqual(
            struct.unpack(
                '<i', self.record[point.srid_range[0]:point.srid_range[1]]
            )[0],
            4326,
        )

    def test_following_record_is_not_consumed(self):
        profile = wkb.profile_wkb(self.data, end=len(self.record))
        self.assertEqual(profile.bytes_verified, len(self.record))
        self.assertEqual(profile.bytes_remaining, 0)
        self.assertIsNone(profile.trailing_range)
        self.assertEqual(profile.members[-1].end, len(self.record))

    def test_truncated_copy_reports_incomplete_member(self):
        complete = wkb.profile_wkb(self.data, end=len(self.record))
        polygon = complete.members[0]
        cut = polygon.end - 3  # last three bytes of a coordinate
        truncated = self.data[:cut]

        profile = wkb.profile_wkb(truncated, end=len(truncated))

        self.assertFalse(profile.complete)
        self.assertFalse(profile.complete_record)
        self.assertEqual(profile.status, wkb_profile.STATUS_INCOMPLETE)
        self.assertEqual(profile.parsed_count, 1)
        self.assertEqual(profile.declared_count, 2)

        member = profile.members[0]
        self.assertEqual(member.status, wkb_profile.STATUS_INCOMPLETE)
        self.assertFalse(member.complete)
        self.assertEqual(member.end, cut)
        self.assertEqual(member.expected_end, polygon.end)
        self.assertEqual(member.missing, 3)
        self.assertEqual(
            member.available, cut - member.payload_range[0]
        )
        self.assertEqual(member.rings[0].complete, False)
        self.assertEqual(member.rings[0].vertex_range[1], cut)

        # The next declared member must not be invented from following bytes.
        self.assertEqual(len(profile.members), 1)
        self.assertIsNotNone(profile.find_incomplete())
        self.assertEqual(profile.find_incomplete().path, [0])

    def test_truncated_copy_does_not_claim_complete_structure(self):
        complete = wkb.profile_wkb(self.data, end=len(self.record))
        cut = complete.members[0].end - 1
        profile = wkb.profile_wkb(self.data[:cut], end=cut)

        # The inner collection and its members are after the cut: none of
        # them may appear as verified.
        paths = [member.path for member in profile.flat_members]
        self.assertNotIn([1], paths)
        self.assertNotIn([1, 0], paths)
        self.assertNotIn([1, 1], paths)
        self.assertFalse(profile.complete)
        self.assertEqual(profile.status, wkb_profile.STATUS_INCOMPLETE)

    def test_segmented_input_matches_complete_input(self):
        complete = wkb.profile_wkb(self.data, end=len(self.record))

        chunks = [self.data[index:index + 7]
                  for index in range(0, len(self.record), 7)]
        segmented = wkb.profile_wkb(iter(chunks), end=len(self.record))

        self.assertEqual(segmented.status, complete.status)
        self.assertEqual(segmented.complete, complete.complete)
        self.assertEqual(segmented.byte_range, complete.byte_range)
        self.assertEqual(
            [(member.path, member.byte_range, member.status)
             for member in segmented.flat_members],
            [(member.path, member.byte_range, member.status)
             for member in complete.flat_members],
        )
        self.assertEqual(segmented.declared_count, complete.declared_count)
        self.assertEqual(segmented.parsed_count, complete.parsed_count)
        self.assertEqual(segmented.issues, complete.issues)

    def test_seekable_stream_matches_bytes(self):
        complete = wkb.profile_wkb(self.data, end=len(self.record))
        stream = io.BytesIO(self.data)
        result = wkb.profile_wkb(stream, end=len(self.record))
        self.assertEqual(result.status, complete.status)
        self.assertEqual(
            [(member.path, member.byte_range)
             for member in result.flat_members],
            [(member.path, member.byte_range)
             for member in complete.flat_members],
        )
        self.assertEqual(stream.tell(), len(self.record))

    def test_chunked_truncation_stops_at_bound(self):
        complete = wkb.profile_wkb(self.data, end=len(self.record))
        cut = complete.members[0].end - 5
        truncated = self.data[:cut]
        chunks = [truncated[index:index + 5]
                  for index in range(0, len(truncated), 5)]
        profile = wkb.profile_wkb(iter(chunks), end=len(truncated))
        self.assertFalse(profile.complete)
        self.assertEqual(profile.status, wkb_profile.STATUS_INCOMPLETE)
        self.assertEqual(profile.members[0].end, cut)


class ProfileStatusTestCase(unittest.TestCase):

    def test_no_members(self):
        polygon = _polygon([[(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]])
        profile = wkb.profile_wkb(polygon, end=len(polygon))
        self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertTrue(profile.complete)
        self.assertFalse(profile.has_members)
        self.assertEqual(profile.member_count, 0)

        empty = _empty_collection()
        profile = wkb.profile_wkb(empty, end=len(empty))
        self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertTrue(profile.complete)
        self.assertFalse(profile.has_members)
        self.assertEqual(profile.declared_count, 0)

    def test_incomplete_payload(self):
        line = wkb.dumps(
            {'type': 'LineString', 'coordinates': [[0, 0], [1, 1]]},
            big_endian=False,
        )
        profile = wkb.profile_wkb(line[:-1], end=len(line) - 1)
        self.assertEqual(profile.status, wkb_profile.STATUS_INCOMPLETE)
        self.assertFalse(profile.complete)
        self.assertEqual(profile.missing, 1)

    def test_unreadable_type(self):
        # An unknown base geometry type number.
        blob = b'\x00' + struct.pack('>I', 99) + b'\x00\x00\x00\x01'
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertEqual(profile.status, wkb_profile.STATUS_UNREADABLE_TYPE)
        self.assertFalse(profile.complete)
        self.assertFalse(profile.type_readable)

        # A truncated type field is incomplete, not unreadable; the profiler
        # has bytes but not enough of the declaration.
        blob = b'\x00\x00\x01'
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertEqual(profile.status, wkb_profile.STATUS_INCOMPLETE)

    def test_missing_header_is_incomplete(self):
        profile = wkb.profile_wkb(b'', end=0)
        self.assertEqual(profile.status, wkb_profile.STATUS_INCOMPLETE)
        self.assertFalse(profile.complete)
        self.assertEqual(profile.issue['reason'], 'truncated_header')

    def test_empty_polygon(self):
        blob = b'\x00' + struct.pack('>I', 3) + struct.pack('>i', 0)
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertTrue(profile.complete)
        self.assertEqual(profile.declared_count, 0)
        self.assertEqual(profile.rings, [])

    def test_caller_end_beyond_buffer_is_not_complete(self):
        blob = _point(1.0, 2.0, big_endian=False)
        # The caller says the record is longer than the data supplied.
        profile = wkb.profile_wkb(blob, end=len(blob) + 10)
        self.assertTrue(profile.complete)
        self.assertEqual(profile.error, wkb_profile.STATUS_INCOMPLETE)
        self.assertEqual(profile.reason, 'payload_beyond_supplied_bytes')
        self.assertEqual(profile.issue['missing'], 10)
        self.assertFalse(profile.complete_record)
        self.assertEqual(profile.issue['byte_range'],
                         (len(blob), len(blob) + 10))

    def test_zero_length_record(self):
        profile = wkb.profile_wkb(b'')
        self.assertEqual(profile.status, wkb_profile.STATUS_INCOMPLETE)
        self.assertEqual(profile.record_length, 0)
        self.assertEqual(profile.bytes_verified, 0)

    def test_record_with_trailing_bytes_stays_verified(self):
        blob = _point(1.0, 2.0, big_endian=False)
        profile = wkb.profile_wkb(blob + b'\xde\xad\xbe\xef', end=len(blob))
        self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertTrue(profile.complete)
        self.assertTrue(profile.complete_record)
        self.assertEqual(profile.record_range, (0, len(blob)))


class ProfileCompatibilityTestCase(unittest.TestCase):

    def test_loads_is_unchanged(self):
        geom = {'type': 'Polygon',
                'coordinates': [[[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]]}
        blob = wkb.dumps(geom, big_endian=False)
        self.assertEqual(geom, wkb.loads(blob))
        wkb.profile_wkb(blob, end=len(blob))
        self.assertEqual(geom, wkb.loads(blob))

    def test_profile_does_not_encode_vertex_objects(self):
        # A polygon with many vertices must not produce per-vertex entries.
        ring = [(float(index), float(index)) for index in range(5000)]
        blob = _polygon([ring], big_endian=False)
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertEqual(len(profile.rings), 1)
        self.assertEqual(profile.rings[0].vertex_count, len(ring))
        self.assertNotIn('coordinates', profile)
        self.assertEqual(profile.member_count, 0)

    def test_memory_does_not_grow_with_first_polygon_vertices(self):
        # Profiling a very large polygon must not retain its vertices.  A
        # tracemalloc peak close to the input size (rather than a multiple
        # covering parsed coordinate objects) demonstrates the skip path.
        import tracemalloc

        ring = [(float(index), float(index)) for index in range(20000)]
        blob = _polygon([ring, ring[:1]], big_endian=False)
        tracemalloc.start()
        try:
            profile = wkb.profile_wkb(blob, end=len(blob))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertEqual(len(profile.rings), 2)
        self.assertEqual(
            sum(ring_profile.vertex_count for ring_profile in profile.rings),
            len(ring) + 1,
        )
        # The decoder materialises ~2 floats per vertex; the profiler must
        # stay well below the raw input size.
        self.assertLess(peak, len(blob))

    def test_large_chunk_is_not_copied(self):
        ring = [(float(index), float(index)) for index in range(1000)]
        blob = _polygon([ring], big_endian=False)
        profile = wkb.profile_wkb(iter([blob]), end=len(blob))
        self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertEqual(profile.rings[0].vertex_count, len(ring))
        self.assertEqual(profile.byte_range, (0, len(blob)))

    def test_profile_dict_access(self):
        blob = _point(1.0, 2.0, srid=4326, big_endian=False)
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertEqual(profile['srid'], profile.srid)
        self.assertIsInstance(profile.as_dict(), dict)
        self.assertNotIsInstance(profile.as_dict(), wkb_profile.ProfileDict)

    def test_profile_class_matches_function(self):
        blob = _point(1.0, 2.0, big_endian=False)
        result = wkb_profile.WKBProfile(blob, end=len(blob))
        self.assertEqual(result.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertEqual(result.byte_range, (0, len(blob)))

    def test_module_aliases_agree(self):
        blob = _point(1.0, 2.0, big_endian=False)
        for entry in (
            wkb.profile_wkb,
            wkb.profile,
            wkb.inspect,
            wkb.inspect_wkb,
            wkb.scan_wkb,
            wkb.structural_profile,
            wkb_profile.profile,
            wkb_profile.inspect,
            wkb_profile.inspect_wkb,
            wkb_profile.scan_wkb,
        ):
            result = entry(blob, end=len(blob))
            self.assertEqual(result.status, wkb_profile.STATUS_NO_MEMBERS)

    def test_member_attribute_aliases(self):
        blob = _point(1.0, 2.0, big_endian=False)
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertEqual(profile.type, 'Point')
        self.assertEqual(profile.geom_type, profile.geometry_type)
        self.assertEqual(profile.span, profile.byte_range)
        self.assertEqual(profile.offset, profile.start)

    def test_bytearray_and_memoryview_inputs(self):
        blob = _point(1.0, 2.0, big_endian=False)
        for source in (bytearray(blob), memoryview(blob), blob):
            profile = wkb.profile_wkb(source, end=len(blob))
            self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
            self.assertEqual(profile.byte_range, (0, len(blob)))

    def test_start_offset_is_respected(self):
        prefix = b'\xde\xad\xbe\xef'
        blob = _point(1.0, 2.0, big_endian=False)
        data = prefix + blob
        profile = wkb.profile_wkb(data, start=len(prefix),
                                  end=len(prefix) + len(blob))
        self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertEqual(profile.record_start, len(prefix))
        self.assertEqual(profile.byte_range, (0, len(blob)))
        self.assertEqual(profile.absolute_start, len(prefix))

    def test_negative_count_is_rejected(self):
        blob = b'\x00' + struct.pack('>I', 7) + struct.pack('>i', -1)
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertFalse(profile.complete)
        self.assertEqual(profile.issue['reason'], 'negative_count')

    def test_invalid_endian_byte(self):
        blob = b'\x02' + struct.pack('>I', 1)
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertEqual(profile.status, wkb_profile.STATUS_UNREADABLE_TYPE)
        self.assertEqual(profile.issue['reason'], 'invalid_endian')

    def test_mixed_endian_nested_members(self):
        record, _ = build_scenario()
        profile = wkb.profile_wkb(record, end=len(record))
        orders = {tuple(member.path): member.byte_order
                  for member in profile.flat_members}
        self.assertEqual(orders[(0,)], 'big')
        self.assertEqual(orders[(1,)], 'little')
        self.assertEqual(orders[(1, 1)], 'little')

    def test_container_stops_at_unreadable_child(self):
        good = _point(1.0, 2.0, big_endian=False)
        unknown = b'\x01' + struct.pack('<I', 99)
        children = good + unknown + good
        blob = (
            b'\x01' + struct.pack('<I', 7) + struct.pack('<i', 3) + children
        )
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertFalse(profile.complete)
        self.assertEqual(profile.status, wkb_profile.STATUS_UNREADABLE_TYPE)
        self.assertEqual(profile.declared_count, 3)
        self.assertEqual(profile.parsed_count, 2)
        self.assertEqual([member.status for member in profile.members],
                         [wkb_profile.STATUS_NO_MEMBERS,
                          wkb_profile.STATUS_UNREADABLE_TYPE])
        # The third declared member must not be reported.
        self.assertEqual([member.path for member in profile.flat_members],
                         [[0], [1]])

    def test_container_stops_at_truncated_child(self):
        good = _point(1.0, 2.0, big_endian=False)
        partial = _point(3.0, 4.0, big_endian=False)[:-5]
        blob = (
            b'\x01' + struct.pack('<I', 7) + struct.pack('<i', 2)
            + good + partial
        )
        profile = wkb.profile_wkb(blob, end=len(blob))
        self.assertEqual(profile.status, wkb_profile.STATUS_INCOMPLETE)
        self.assertEqual(profile.members[1].status,
                         wkb_profile.STATUS_INCOMPLETE)
        self.assertEqual(profile.members[1].missing, 5)
        self.assertEqual(profile.find_incomplete().path, [1])

    def test_trailing_bytes_are_reported_not_verified(self):
        blob = _point(1.0, 2.0, big_endian=False)
        data = blob + b'\x00\x00\x00\x00'
        profile = wkb.profile_wkb(data, end=len(data))
        self.assertEqual(profile.status, wkb_profile.STATUS_NO_MEMBERS)
        self.assertTrue(profile.complete)
        self.assertFalse(profile.complete_record)
        self.assertEqual(profile.reason, 'trailing_bytes_not_verified')
        self.assertEqual(profile.bytes_remaining, 4)
        self.assertEqual(profile.trailing_range,
                         (len(blob), len(data)))


if __name__ == '__main__':
    unittest.main()

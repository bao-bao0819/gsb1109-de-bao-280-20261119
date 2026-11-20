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
import itertools
import unittest

from geomet import loader, wkb
from geomet.tests.wkb_budget_test import (
    chunked,
    collection_wkb,
    polygon_wkb,
)


class LoaderReplayTestCase(unittest.TestCase):

    def setUp(self):
        self.outer = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 0.0)]
        self.inner_three = [(2.0, 2.0), (8.0, 2.0), (2.0, 8.0)]
        self.inner_four = self.inner_three + [(5.0, 5.0)]

        # Record one: exactly seven vertices, equal to the budget.
        self.exact = polygon_wkb([self.outer, self.inner_three])
        # Record two: one extra vertex only in the last inner ring.
        self.over_budget = polygon_wkb([self.outer, self.inner_four])
        # Record three: counts within budget, payload ends inside the ring.
        # header (5) + ring count (4) + ring0 count (4) + ring0 payload (64)
        # + ring1 count (4), leaving exactly one of ring1's three vertices.
        inner_start = 5 + 4 + 4 + len(self.outer) * 16 + 4
        self.truncated = self.exact[:inner_start + 16]
        # Record three wrapped in collections beyond the depth limit.
        self.too_deep = collection_wkb(
            [collection_wkb([collection_wkb([self.truncated])])]
        )
        # A legal record following every bad one.
        self.legal = self.exact

    def _framed_stream(self, records):
        lengths = [len(record) for record in records]
        stream = itertools.chain.from_iterable(records)
        return loader.frame_records(stream, lengths), lengths

    def test_replay_classifies_each_record_and_keeps_going(self):
        records = [
            self.exact,
            self.over_budget,
            self.truncated,
            self.too_deep,
            self.legal,
        ]
        framed, _ = self._framed_stream(records)
        results = list(
            loader.load_records(framed, coordinate_budget=7, max_depth=2)
        )

        statuses = [status for _, status, _ in results]
        self.assertEqual(
            [
                loader.STATUS_OK,
                loader.STATUS_COORDINATE_BUDGET_EXCEEDED,
                loader.STATUS_TRUNCATED,
                loader.STATUS_DEPTH_EXCEEDED,
                loader.STATUS_OK,
            ],
            statuses,
        )

        indices = [index for index, _, _ in results]
        self.assertEqual([0, 1, 2, 3, 4], indices)

        # The exact-budget record and the final legal record decode fully.
        self.assertEqual('Polygon', results[0][2]['type'])
        self.assertEqual('Polygon', results[4][2]['type'])

        # The over-budget record is pinpointed at its last inner ring before
        # that ring's coordinates are consumed.
        budget_error = results[1][2]
        self.assertEqual(
            ['Polygon', 'ring[1]'], budget_error.layer
        )
        self.assertEqual(4, budget_error.vertices_counted)
        self.assertEqual(4, budget_error.vertices_required)

        # The truncated record fails on payload shortage, not on budget.
        truncated_error = results[2][2]
        self.assertEqual(
            ['Polygon', 'ring[1]'], truncated_error.layer
        )
        self.assertEqual(48, truncated_error.expected_bytes)
        self.assertEqual(16, truncated_error.available_bytes)

        # The depth error names the first over-deep collection.
        depth_error = results[3][2]
        self.assertEqual(3, depth_error.depth)
        self.assertEqual(
            [
                'GeometryCollection', 'geometry[0]',
                'GeometryCollection', 'geometry[0]',
                'GeometryCollection',
            ],
            depth_error.layer,
        )

    def test_record_boundaries_are_driven_by_explicit_lengths(self):
        records = [self.over_budget, self.too_deep, self.legal]
        framed, lengths = self._framed_stream(records)
        framed = list(framed)

        self.assertEqual(lengths, [len(record) for _, record in framed])
        for (_, record), original in zip(framed, records):
            self.assertEqual(original, record)

    def test_bad_record_never_borrows_the_next_records_bytes(self):
        records = [self.too_deep, self.legal]
        framed, _ = self._framed_stream(records)
        results = list(
            loader.load_records(framed, coordinate_budget=7, max_depth=2)
        )
        self.assertEqual(
            [loader.STATUS_DEPTH_EXCEEDED, loader.STATUS_OK],
            [status for _, status, _ in results],
        )
        self.assertEqual('Polygon', results[1][2]['type'])

    def test_budget_accumulates_across_segmented_input(self):
        records = [self.exact, self.over_budget]
        lengths = [len(record) for record in records]
        # Every byte arrives separately, yet framing and the vertex budget
        # still accumulate across the segment boundaries.
        stream = chunked(b''.join(records), 1)
        results = list(
            loader.load_records(
                loader.frame_records(stream, lengths),
                coordinate_budget=7,
            )
        )
        self.assertEqual(
            [loader.STATUS_OK, loader.STATUS_COORDINATE_BUDGET_EXCEEDED],
            [status for _, status, _ in results],
        )

    def test_without_budgets_bad_records_only_fail_on_truncation(self):
        records = [self.over_budget, self.truncated]
        framed, _ = self._framed_stream(records)
        results = list(loader.load_records(framed))
        self.assertEqual(
            [loader.STATUS_OK, loader.STATUS_TRUNCATED],
            [status for _, status, _ in results],
        )

    def test_framing_short_stream_is_reported_as_truncated(self):
        stream = chunked(self.exact, 3)
        with self.assertRaises(wkb.WKBTruncated) as ctx:
            list(loader.frame_records(stream, [len(self.exact) + 10]))
        self.assertEqual(['record[0]'], ctx.exception.layer)
        self.assertEqual(len(self.exact) + 10,
                         ctx.exception.expected_bytes)
        self.assertEqual(len(self.exact),
                         ctx.exception.available_bytes)


class ClassifyTestCase(unittest.TestCase):

    def test_budget_and_structure_errors_map_to_distinct_statuses(self):
        budget_error = wkb.WKBCoordinateBudgetExceeded(7, 4, 4,
                                                       ['Polygon'])
        depth_error = wkb.WKBDepthExceeded(2, 3, ['GeometryCollection'])
        truncated_error = wkb.WKBTruncated(['Polygon'], 16, 0)

        self.assertEqual(
            loader.STATUS_COORDINATE_BUDGET_EXCEEDED,
            loader.classify(budget_error),
        )
        self.assertEqual(
            loader.STATUS_DEPTH_EXCEEDED, loader.classify(depth_error)
        )
        self.assertEqual(
            loader.STATUS_TRUNCATED, loader.classify(truncated_error)
        )

    def test_other_value_errors_are_invalid(self):
        self.assertEqual(
            loader.STATUS_INVALID,
            loader.classify(ValueError("Unsupported geometry type 'X'")),
        )


if __name__ == '__main__':
    unittest.main()

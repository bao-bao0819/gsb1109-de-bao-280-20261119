"""Round-trip regression tests for DeepDiff + Delta on observation-matrix batches.

These tests pin down behavior needed before migrating the observation-matrix
structure comparison tool onto DeepDiff/Delta. No new API is introduced.

Data model under test:

- The outer batch is an unordered list of records; the two batches being
  compared hold the same device set with the outer records swapped.
- Each record carries an observation matrix whose two axes are devices
  (rows) and time points (columns). The device axis is semantic: row i
  holds the measurements of devices[i]. That meaning must not be erased
  by treating the matrix as a plain rearrangeable list.

Batch v1 -> v2 changes (outer record order is also swapped):

- alpha: non-square 3x2 matrix transposed to 2x3 (axis change, same values).
- beta: 1x3 matrix elements change int -> float (type change, same shape).
- gamma: 2x2 matrix reshaped into a single row (shape change, same values).

A control batch only reorders the outer records and changes no matrix;
per the existing DeepDiff convention it must produce an empty diff.
"""
import logging

import pytest

from deepdiff import DeepDiff, Delta
from deepdiff.serialization import DELTA_IGNORE_ORDER_NEEDS_REPETITION_REPORT


def batch_v1():
    return [
        {'rec': 'alpha', 'devices': ['d1', 'd2', 'd3'], 'matrix': [[1, 2], [3, 4], [5, 6]]},
        {'rec': 'beta', 'devices': ['d4'], 'matrix': [[7, 8, 9]]},
        {'rec': 'gamma', 'devices': ['d5', 'd6'], 'matrix': [[1, 2], [3, 4]]},
    ]


def batch_v2():
    return [
        {'rec': 'gamma', 'devices': ['d5', 'd6'], 'matrix': [[1, 2, 3, 4]]},
        {'rec': 'alpha', 'devices': ['d1', 'd2', 'd3'], 'matrix': [[1, 3, 5], [2, 4, 6]]},
        {'rec': 'beta', 'devices': ['d4'], 'matrix': [[7.0, 8.0, 9.0]]},
    ]


def batch_control():
    """Only the outer records are reordered; no matrix is touched."""
    t1 = batch_v1()
    return [t1[2], t1[0], t1[1]]


# Setting (a): ignore only the outer batch order. Outer records are paired
# by their 'rec' key; every inner list (matrix rows, matrix elements, the
# device axis itself) keeps positional meaning.
IGNORE_OUTER_ORDER_ONLY = {'iterable_match_keys': {'root': 'rec'}}

# Setting (b): additionally exclude plain-list positions, a common recipe
# for "ignore list ordering" snapshots.
EXCLUDE_PLAIN_LIST_POSITIONS = dict(IGNORE_OUTER_ORDER_ONLY, exclude_regex_paths=[r'\[\d+\]'])


def assert_batch_replayed(result, expected):
    """Verify devices, shape, element types and values of a replayed batch.

    Flattened element equality is explicitly not sufficient: a transposed
    or reshaped matrix keeps the same element multiset, and 1 == 1.0 hides
    the int -> float type change, so each axis and type is checked directly.
    """
    assert [r['rec'] for r in result] == [r['rec'] for r in expected]
    for got, want in zip(result, expected):
        assert got['devices'] == want['devices'], 'device axis must survive the round trip'
        got_matrix, want_matrix = got['matrix'], want['matrix']
        assert len(got_matrix) == len(want_matrix), 'device axis length (row count) changed'
        for got_row, want_row in zip(got_matrix, want_matrix):
            assert len(got_row) == len(want_row), 'time axis length (column count) changed'
            for got_val, want_val in zip(got_row, want_row):
                assert type(got_val) is type(want_val)
                assert got_val == want_val


class TestIgnoreOuterBatchOrderOnly:
    """Setting (a): the replayable branch."""

    def test_diff_distinguishes_axis_type_and_order_changes(self):
        diff = DeepDiff(batch_v1(), batch_v2(), **IGNORE_OUTER_ORDER_ONLY)
        assert diff == {
            # beta: original element type changed int -> float, shape untouched.
            'type_changes': {
                "root[2]['matrix'][0][0]": {
                    'old_type': int, 'new_type': float, 'old_value': 7, 'new_value': 7.0},
                "root[2]['matrix'][0][1]": {
                    'old_type': int, 'new_type': float, 'old_value': 8, 'new_value': 8.0},
                "root[2]['matrix'][0][2]": {
                    'old_type': int, 'new_type': float, 'old_value': 9, 'new_value': 9.0},
            },
            # alpha: transposed; overlapping cells report new values.
            'values_changed': {
                "root[1]['matrix'][0][1]": {'new_value': 3, 'old_value': 2},
                "root[1]['matrix'][1][0]": {'new_value': 2, 'old_value': 3},
            },
            # alpha gained a time column; gamma collapsed to a single row.
            'iterable_item_added': {
                "root[1]['matrix'][0][2]": 5,
                "root[1]['matrix'][1][2]": 6,
                "root[0]['matrix'][0][2]": 3,
                "root[0]['matrix'][0][3]": 4,
            },
            # alpha lost a device row; gamma lost its second device row.
            'iterable_item_removed': {
                "root[1]['matrix'][2]": [5, 6],
                "root[0]['matrix'][1]": [3, 4],
            },
        }
        # The device labels themselves are untouched by every change kind.
        for report_type in diff.values():
            for path in report_type:
                assert 'devices' not in path

    def test_delta_round_trip_restores_devices_shape_types_and_values(self):
        t1, t2 = batch_v1(), batch_v2()
        diff = DeepDiff(t1, t2, **IGNORE_OUTER_ORDER_ONLY)
        assert_batch_replayed(t1 + Delta(diff), t2)

    def test_delta_round_trip_after_serialization(self):
        t1, t2 = batch_v1(), batch_v2()
        diff = DeepDiff(t1, t2, **IGNORE_OUTER_ORDER_ONLY)
        assert_batch_replayed(t1 + Delta(Delta(diff).dumps()), t2)

    def test_control_reorder_only_is_no_actual_change(self):
        t1 = batch_v1()
        control = batch_control()
        diff = DeepDiff(t1, control, **IGNORE_OUTER_ORDER_ONLY)
        # Existing convention: a pure reorder is no actual change.
        assert diff == {}
        # The delta still carries the moves, so the replay reproduces the
        # control batch exactly; every matrix is verified untouched.
        assert_batch_replayed(t1 + Delta(diff), control)

    def test_flattened_equality_would_not_detect_axis_or_type_changes(self):
        """Why the replay assertions above do not use flattened equality."""
        alpha_v1 = batch_v1()[0]['matrix']
        alpha_v2 = batch_v2()[1]['matrix']
        flat_v1 = sorted(v for row in alpha_v1 for v in row)
        flat_v2 = sorted(v for row in alpha_v2 for v in row)
        assert flat_v1 == flat_v2  # transpose keeps the element multiset
        assert alpha_v1 != alpha_v2  # but the device x time structure changed
        beta_v1 = batch_v1()[1]['matrix']
        beta_v2 = batch_v2()[2]['matrix']
        assert beta_v1 == beta_v2  # 7 == 7.0: value equality hides the type change
        assert [type(v) for v in beta_v1[0]] != [type(v) for v in beta_v2[0]]


class TestExcludePlainListPositions:
    """Setting (b): excluding plain-list positions cannot support matrices.

    The snapshot keeps no paths below any list index, so every matrix
    change (axis, shape, type) falls outside the snapshot. The diff is
    empty and the Delta replays to the old batch: a real round-trip
    failure, recorded here with the exact records that diverge.
    """

    def test_excluding_list_positions_hides_all_matrix_changes(self):
        diff = DeepDiff(batch_v1(), batch_v2(), **EXCLUDE_PLAIN_LIST_POSITIONS)
        assert diff == {}

    def test_control_is_also_empty_under_excluded_positions(self):
        diff = DeepDiff(batch_v1(), batch_control(), **EXCLUDE_PLAIN_LIST_POSITIONS)
        assert diff == {}

    def test_replay_of_excluded_snapshot_fails_to_reproduce_batch(self):
        t1, t2 = batch_v1(), batch_v2()
        diff = DeepDiff(t1, t2, **EXCLUDE_PLAIN_LIST_POSITIONS)
        replayed = t1 + Delta(diff)
        assert_batch_replayed(replayed, t1)  # nothing was applied
        assert replayed != t2
        # Locatable evidence: the excluded paths hide each real change.
        by_rec = {r['rec']: r for r in replayed}
        assert by_rec['alpha']['matrix'] == [[1, 2], [3, 4], [5, 6]]  # still 3x2, not transposed
        assert all(type(v) is int for v in by_rec['beta']['matrix'][0])  # still int, not float
        assert by_rec['gamma']['matrix'] == [[1, 2], [3, 4]]  # still 2 rows, not one


class TestRecursiveIgnoreOrderCounterexamples:
    """Recursive ignore_order treats matrices as rearrangeable plain lists.

    Both counterexamples keep the same element multiset while changing the
    observation meaning. The axis whose meaning must not be erased is the
    device axis: matrix row i belongs to devices[i], and the transpose
    additionally swaps the device axis with the time axis.
    """

    def test_transpose_misread_as_plain_list_rearrangement(self):
        # [[1, 2], [3, 1]] transposed is [[1, 3], [2, 1]]: the columns are
        # exactly the rows as multisets, so ignore_order sees no change.
        t1 = [{'rec': 'rho', 'devices': ['dx', 'dy'], 'matrix': [[1, 2], [3, 1]]}]
        t2 = [{'rec': 'rho', 'devices': ['dx', 'dy'], 'matrix': [[1, 3], [2, 1]]}]
        assert DeepDiff(t1, t2, ignore_order=True) == {}
        # The ordered comparison keeps the device axis: device dx's second
        # reading and device dy's first reading changed.
        assert DeepDiff(t1, t2, **IGNORE_OUTER_ORDER_ONLY) == {
            'values_changed': {
                "root[0]['matrix'][0][1]": {'new_value': 3, 'old_value': 2},
                "root[0]['matrix'][1][0]": {'new_value': 2, 'old_value': 3},
            },
        }
        # The empty snapshot cannot replay the axis change.
        replayed = t1 + Delta(DeepDiff(t1, t2, ignore_order=True, report_repetition=True))
        assert replayed == t1 != t2

    def test_row_swap_with_same_element_set_is_erased(self):
        # Only positions change: the two device rows are swapped, so d5 and
        # d6 trade measurements while the element set is untouched.
        t1 = [{'rec': 'sig', 'devices': ['d5', 'd6'], 'matrix': [[5, 6], [7, 8]]}]
        t2 = [{'rec': 'sig', 'devices': ['d5', 'd6'], 'matrix': [[7, 8], [5, 6]]}]
        assert DeepDiff(t1, t2, ignore_order=True) == {}
        # The ordered comparison attributes every value to its device row.
        assert DeepDiff(t1, t2, **IGNORE_OUTER_ORDER_ONLY) == {
            'values_changed': {
                "root[0]['matrix'][0][0]": {'new_value': 7, 'old_value': 5},
                "root[0]['matrix'][0][1]": {'new_value': 8, 'old_value': 6},
                "root[0]['matrix'][1][0]": {'new_value': 5, 'old_value': 7},
                "root[0]['matrix'][1][1]": {'new_value': 6, 'old_value': 8},
            },
        }

    def test_delta_requires_repetition_report_under_ignore_order(self):
        diff = DeepDiff(batch_v1(), batch_v2(), ignore_order=True)
        with pytest.raises(ValueError) as exc_info:
            Delta(diff)
        assert str(exc_info.value) == DELTA_IGNORE_ORDER_NEEDS_REPETITION_REPORT

    def test_ignore_order_delta_replays_but_cannot_distinguish_change_kinds(self):
        t1, t2 = batch_v1(), batch_v2()
        diff = DeepDiff(t1, t2, ignore_order=True, report_repetition=True)
        # Every record collapsed into a whole-record remove/add pair; the
        # axis change and the type change are indistinguishable in the diff.
        assert set(diff) == {'iterable_item_added', 'iterable_item_removed'}
        assert_batch_replayed(t1 + Delta(diff), t2)


class TestIgnoreOrderFuncAtRoot:
    """The other common recipe for "ignore only the outer batch order".

    ignore_order_func at root alone replays correctly but pairs records by
    content similarity, so the diff cannot attribute the axis change vs the
    type change to their records. Combined with iterable_match_keys it
    suppresses move reporting and the Delta silently corrupts the batch.
    Both are recorded here as real failures with locatable evidence.
    """

    @staticmethod
    def ignore_outer_level(level):
        return level.path() == 'root'

    def test_control_reorder_only_is_no_actual_change(self):
        t1 = batch_v1()
        diff = DeepDiff(t1, batch_control(), ignore_order_func=self.ignore_outer_level)
        assert diff == {}
        assert_batch_replayed(t1 + Delta(diff), t1)

    def test_diff_replays_but_cannot_attribute_changes_to_records(self):
        t1, t2 = batch_v1(), batch_v2()
        diff = DeepDiff(t1, t2, ignore_order_func=self.ignore_outer_level)
        # All three changes collapse into whole-record replacements...
        assert set(diff) == {'values_changed'}
        assert set(diff['values_changed']) == {'root[0]', 'root[1]', 'root[2]'}
        # ...and the similarity pairing mismatches records: the old 'alpha'
        # record is paired with the new 'gamma' record, so neither the axis
        # change nor the type change can be attributed from the diff alone.
        paired = {(v['old_value']['rec'], v['new_value']['rec']) for v in diff['values_changed'].values()}
        assert paired != {('alpha', 'alpha'), ('beta', 'beta'), ('gamma', 'gamma')}
        # Replay still works because whole records are overwritten.
        assert_batch_replayed(t1 + Delta(diff), t2)

    def test_combined_with_match_keys_corrupts_replay(self, caplog):
        t1, t2 = batch_v1(), batch_v2()
        diff = DeepDiff(t1, t2, iterable_match_keys={'root': 'rec'},
                        ignore_order_func=self.ignore_outer_level)
        with caplog.at_level(logging.WARNING, logger='deepdiff.delta'):
            replayed = t1 + Delta(diff)
        assert replayed != t2
        # Locatable evidence: Delta reports the matrix paths it cannot get.
        assert 'Unable to get the item at' in caplog.text
        # The outer reorder itself is never replayed...
        assert [r['rec'] for r in replayed] == ['alpha', 'beta', 'gamma']
        # ...and the type change is lost while partial matrix edits land.
        assert all(type(v) is int for v in replayed[1]['matrix'][0])

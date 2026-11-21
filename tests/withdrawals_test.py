from __future__ import absolute_import, division, print_function

import random
import unittest

from s2sphere import CellId, CellUnion
from s2sphere.withdrawals import CoverageWithdrawals


def cell(face, path):
    """Descend from a face cell along child positions (0..3)."""
    cell_id = CellId.from_face_pos_level(face, 0, 0)
    for position in path:
        cell_id = cell_id.child(position)
    return cell_id


def descendants_at_level(cell_id, level):
    if cell_id.level() == level:
        return [cell_id]
    return list(cell_id.children(level))


class BruteForceReference(object):
    """Independent model: every coverage tracked as level-E leaf sets."""

    def __init__(self, base_cells, level):
        self.level = level
        self.base = set()
        for cell_id in base_cells:
            self.base.update(descendants_at_level(cell_id, level))
        self.orders = {}

    def submit(self, ticket_id, requests):
        withdrawn = set()
        for cell_id in requests:
            withdrawn.update(descendants_at_level(cell_id, self.level))
        self.orders[ticket_id] = withdrawn

    def cancel(self, ticket_id):
        self.orders.pop(ticket_id, None)

    def active_set(self):
        withdrawn = set()
        for covered in self.orders.values():
            withdrawn.update(covered)
        return self.base - withdrawn

    def newly_withdrawn(self, requests):
        before = self.active_set()
        withdrawn = set()
        for cell_id in requests:
            withdrawn.update(descendants_at_level(cell_id, self.level))
        return before & withdrawn


def union_as_level_set(cell_union, level):
    result = set()
    for cell_id in cell_union.cell_ids():
        result.update(descendants_at_level(cell_id, level))
    return result


def cw_current(coverage_withdrawals):
    return coverage_withdrawals.current_coverage().cell_ids()


class WithdrawalScenarioTest(unittest.TestCase):

    def setUp(self):
        # P: a coarse cell on face 0.  Q: the whole neighbouring face.
        self.p = cell(0, [0])
        self.q = CellId.from_face_pos_level(1, 0, 0)
        self.base = CellUnion([self.p, self.q])
        self.level = 5
        self.reference = BruteForceReference(self.base.cell_ids(),
                                             self.level)
        self.cw = CoverageWithdrawals(self.base)

        # Deep cell under P and its sibling neighbours.
        self.deep = cell(0, [0, 1, 2])
        self.sibling_one = cell(0, [0, 1, 0])
        self.sibling_two = cell(0, [0, 1, 1])
        self.child_of_sibling = cell(0, [0, 1, 0, 3])
        # A fresh P cell that no early order touches.
        self.fresh = cell(0, [0, 0, 3])
        # A cell completely outside the original coverage.
        self.outside = CellId.from_face_pos_level(2, 0, 0)

    def assert_matches_reference(self):
        self.assertEqual(
            union_as_level_set(self.cw.current_coverage(), self.level),
            self.reference.active_set())

    def assert_q_stays_compressed(self):
        faces = [cell_id for cell_id
                 in self.cw.current_coverage().cell_ids()
                 if cell_id.face() == 1]
        self.assertEqual(faces, [self.q])

    def test_overlapping_orders_and_reverse_cancellation(self):
        # Order A: deep cell, sibling neighbours, a parent/child pair and
        # an exact duplicate.
        request_a = [self.deep, self.deep, self.sibling_one,
                     self.sibling_two, self.child_of_sibling]
        expected_new_a = self.reference.newly_withdrawn(request_a)
        receipt_a = self.cw.submit('A', request_a)
        self.reference.submit('A', request_a)

        self.assertTrue(receipt_a.applied)
        self.assertEqual(
            union_as_level_set(receipt_a.change, self.level),
            expected_new_a)
        self.assertEqual(receipt_a.current, self.cw.current_coverage())
        self.assertEqual(receipt_a.outside_requests, ())
        self.assert_matches_reference()
        self.assert_q_stays_compressed()

        # Replaying A returns the stored receipt and withdraws nothing.
        replay_a = self.cw.submit('A', request_a)
        self.assertIs(replay_a, receipt_a)
        self.assert_matches_reference()

        # Order B: overlaps A on deep, adds a fresh P cell and an
        # out-of-coverage cell, plus a duplicate.
        request_b = [self.deep, self.fresh, self.fresh, self.outside]
        expected_new_b = self.reference.newly_withdrawn(request_b)
        receipt_b = self.cw.submit('B', request_b)
        self.reference.submit('B', request_b)

        self.assertTrue(receipt_b.applied)
        self.assertEqual(
            union_as_level_set(receipt_b.change, self.level),
            expected_new_b)
        # The receipt only lists the freshly withdrawn in-scope cell:
        # neither the A/B overlap nor the outside request appear.
        self.assertEqual(receipt_b.change.cell_ids(), [self.fresh])
        self.assertEqual(receipt_b.outside_requests, (self.outside,))
        self.assert_matches_reference()
        self.assert_q_stays_compressed()

        # The full request of B is remembered even where A got there
        # first, and the outside request can never enter the coverage.
        self.assertIn(self.deep, self.cw.order_requests('B'))
        self.assertFalse(self.cw.current_coverage().intersects(self.outside))

        # Cancel A: A-exclusive coverage comes back, the overlap stays
        # withdrawn because B still holds it.
        cancel_a = self.cw.cancel('A')
        self.reference.cancel('A')
        self.assertEqual(
            union_as_level_set(cancel_a.change, self.level),
            self.reference.base
            & (set(descendants_at_level(self.sibling_one, self.level))
               | set(descendants_at_level(self.sibling_two, self.level))))
        self.assert_matches_reference()
        self.assert_q_stays_compressed()

        # Replaying B's old submission does not run it a second time.
        replay_b = self.cw.submit('B', request_b)
        self.assertIs(replay_b, receipt_b)
        self.assert_matches_reference()

        # Historical receipts remain available and unchanged.
        self.assertIs(self.cw.receipt('A', CoverageWithdrawals.SUBMIT),
                      receipt_a)
        self.assertIs(self.cw.receipt('A', CoverageWithdrawals.CANCEL),
                      cancel_a)
        # The snapshot inside an old receipt keeps its original value
        # after later submissions and cancellations.
        snapshot_after_a = CellUnion(receipt_a.current.cell_ids())
        self.assertEqual(receipt_a.current, snapshot_after_a)
        self.assertNotEqual(receipt_a.current,
                            self.cw.current_coverage())

        # Cancel B: only the still-withdrawn B-held coverage returns.
        cancel_b = self.cw.cancel('B')
        self.reference.cancel('B')
        self.assertEqual(
            union_as_level_set(cancel_b.change, self.level),
            self.reference.base
            & (set(descendants_at_level(self.deep, self.level))
               | set(descendants_at_level(self.fresh, self.level))))
        self.assert_matches_reference()
        self.assertEqual(self.cw.current_coverage(), self.base)
        self.assertEqual(self.cw.active_ticket_ids(), ())

        # Cancelling again is a stored no-op; resubmitting the old ticket
        # id returns the original submission receipt and stays inactive.
        cancel_b_again = self.cw.cancel('B')
        self.assertIs(cancel_b_again, cancel_b)
        self.assertIs(self.cw.submit('B', request_b), receipt_b)
        self.assertEqual(self.cw.active_ticket_ids(), ())
        self.assertEqual(self.cw.current_coverage(), self.base)

    def test_reverse_cancellation_order(self):
        request_a = [self.deep, self.sibling_one, self.sibling_two]
        request_b = [self.deep, self.fresh, self.outside]
        self.cw.submit('A', request_a)
        self.cw.submit('B', request_b)
        self.reference.submit('A', request_a)
        self.reference.submit('B', request_b)

        # Cancel B first: only the B-exclusive cell returns.
        cancel_b = self.cw.cancel('B')
        self.reference.cancel('B')
        self.assertEqual(cancel_b.change.cell_ids(), [self.fresh])
        self.assert_matches_reference()

        # The overlap remains withdrawn under A until A goes as well.
        self.assertFalse(self.cw.current_coverage().contains(self.deep))
        cancel_a = self.cw.cancel('A')
        self.reference.cancel('A')
        self.assertEqual(
            union_as_level_set(cancel_a.change, self.level),
            self.reference.base
            & (set(descendants_at_level(self.deep, self.level))
               | set(descendants_at_level(self.sibling_one, self.level))
               | set(descendants_at_level(self.sibling_two, self.level))))
        self.assertEqual(self.cw.current_coverage(), self.base)

    def test_order_fully_covered_by_earlier_order(self):
        self.cw.submit('A', [self.sibling_one])
        receipt_c = self.cw.submit('C', [self.sibling_one,
                                         self.child_of_sibling])
        self.assertFalse(receipt_c.applied)
        self.assertEqual(receipt_c.change.num_cells(), 0)
        self.assertEqual(self.cw.active_ticket_ids(), ('A', 'C'))

        # Cancelling the redundant order restores nothing on its own.
        cancel_c = self.cw.cancel('C')
        self.assertFalse(cancel_c.applied)
        self.assertEqual(cancel_c.change.num_cells(), 0)
        self.assertFalse(self.cw.current_coverage().contains(
            self.sibling_one))

    def test_unknown_cancel_is_idempotent_noop(self):
        receipt = self.cw.cancel('ghost')
        self.assertFalse(receipt.applied)
        self.assertEqual(receipt.change.num_cells(), 0)
        self.assertEqual(receipt.current, self.base)
        self.assertIs(self.cw.cancel('ghost'), receipt)


class NoLayerExpansionTest(unittest.TestCase):

    def test_deep_withdrawal_does_not_materialise_a_full_layer(self):
        coarse = cell(0, [0])
        untouched_face = CellId.from_face_pos_level(1, 0, 0)
        cw = CoverageWithdrawals(CellUnion([coarse, untouched_face]))

        deep_level = 28
        fine = coarse
        for position in [1, 2, 0, 3] * 7:
            if fine.level() == deep_level:
                break
            fine = fine.child(position)
        self.assertEqual(fine.level(), deep_level)

        receipt = cw.submit('X', [fine])
        self.assertEqual(receipt.change, CellUnion([fine]))

        inside_p = [cell_id for cell_id
                    in cw.current_coverage().cell_ids()
                    if cell_id.face() == 0]
        # The hole is represented by sibling cells along one path, never
        # by enumerating leaves at or below the requested depth.
        self.assertTrue(all(cell_id.level() <= deep_level
                            for cell_id in inside_p))
        self.assertFalse(any(cell_id.level() > deep_level
                             for cell_id in inside_p))
        self.assertLess(len(inside_p), 3 * deep_level + 1)
        # Q remains one compressed face cell.
        self.assertEqual(
            [cell_id for cell_id in cw.current_coverage().cell_ids()
             if cell_id.face() == 1],
            [untouched_face])

        cancel = cw.cancel('X')
        self.assertEqual(cancel.change, CellUnion([fine]))
        self.assertEqual(cw.current_coverage(),
                         CellUnion([coarse, untouched_face]))


class AccessIsolationTest(unittest.TestCase):

    def setUp(self):
        self.p = cell(0, [0])
        self.q = CellId.from_face_pos_level(1, 0, 0)
        self.cw = CoverageWithdrawals(CellUnion([self.p, self.q]))

        # Several overlapping active withdrawals accumulate on Q.
        self.q_orders = [
            ('Q1', [cell(1, [0]), cell(1, [1])]),
            ('Q2', [cell(1, [0]), cell(1, [2])]),
            ('Q3', [cell(1, [3]), cell(1, [0, 0])]),
        ]
        for ticket_id, requests in self.q_orders:
            self.cw.submit(ticket_id, requests)

    def test_p_operations_never_touch_q_orders(self):
        q_snapshot = [cell_id for cell_id
                      in self.cw.current_coverage().cell_ids()
                      if cell_id.face() == 1]

        p_piece = cell(0, [0, 2, 1])
        p_other = cell(0, [0, 2, 2])

        self.cw.reset_access_records()
        submit_receipt = self.cw.submit('P1', [p_piece, p_other])
        for record in self.cw.access_records():
            self.assertEqual(record.ticket_id, 'P1')
            self.assertEqual(set(record.faces), {0})

        # Q's representation is byte-for-byte unchanged.
        q_after_submit = [cell_id for cell_id
                          in self.cw.current_coverage().cell_ids()
                          if cell_id.face() == 1]
        self.assertEqual(q_after_submit, q_snapshot)

        self.cw.reset_access_records()
        cancel_receipt = self.cw.cancel('P1')
        for record in self.cw.access_records():
            self.assertEqual(record.ticket_id, 'P1')
            self.assertEqual(set(record.faces), {0})
        self.assertEqual(
            [cell_id for cell_id in cw_current(self.cw)
             if cell_id.face() == 1],
            q_snapshot)

        # The Q orders are still live and their receipts are preserved.
        self.assertEqual(
            self.cw.active_ticket_ids(), ('Q1', 'Q2', 'Q3'))
        for ticket_id, _ in self.q_orders:
            self.assertIsNotNone(
                self.cw.receipt(ticket_id, CoverageWithdrawals.SUBMIT))
        self.assertTrue(submit_receipt.change.num_cells() > 0)
        self.assertTrue(cancel_receipt.change.num_cells() > 0)

    def test_same_ticket_retries_recorded_once(self):
        piece = cell(0, [0, 1, 1])
        first = self.cw.submit('P2', [piece])
        self.cw.reset_access_records()
        second = self.cw.submit('P2', [piece])
        self.assertIs(second, first)
        self.assertEqual(self.cw.access_records(), [])

        cancel_first = self.cw.cancel('P2')
        self.cw.reset_access_records()
        cancel_second = self.cw.cancel('P2')
        self.assertIs(cancel_second, cancel_first)
        self.assertEqual(self.cw.access_records(), [])


class AncestorAndFineCellTest(unittest.TestCase):

    def test_cancelling_ancestor_keeps_fine_withdrawal(self):
        coarse = cell(0, [0])
        neighbour = cell(0, [1])
        untouched_face = CellId.from_face_pos_level(1, 0, 0)
        cw = CoverageWithdrawals(
            CellUnion([coarse, neighbour, untouched_face]))
        level = 6
        reference = BruteForceReference(
            [coarse, neighbour, untouched_face], level)

        ancestor = cell(0, [0, 1])
        fine = cell(0, [0, 1, 2, 0, 3])
        self.assertTrue(ancestor.contains(fine))

        cw.submit('ancestor', [ancestor])
        reference.submit('ancestor', [ancestor])
        cw.submit('fine', [fine])
        reference.submit('fine', [fine])
        self.assertEqual(
            union_as_level_set(cw.current_coverage(), level),
            reference.active_set())

        cancel_ancestor = cw.cancel('ancestor')
        reference.cancel('ancestor')

        # The fine cell stays withdrawn; the receipt lists only coverage
        # genuinely restored, kept as a compressed sibling tiling.
        self.assertFalse(cw.current_coverage().contains(fine))
        self.assertEqual(
            union_as_level_set(cancel_ancestor.change, level),
            set(descendants_at_level(ancestor, level))
            - set(descendants_at_level(fine, level)))
        # Compressed sibling tiling: no cell deeper than the fine hole,
        # and far fewer cells than enumerating a full layer under ancestor.
        self.assertTrue(all(cell_id.level() <= fine.level()
                            for cell_id in cancel_ancestor.change.cell_ids()))
        self.assertLess(
            len(cancel_ancestor.change.cell_ids()),
            4 * (fine.level() - ancestor.level()))
        self.assertEqual(
            union_as_level_set(cw.current_coverage(), level),
            reference.active_set())

        cancel_fine = cw.cancel('fine')
        reference.cancel('fine')
        self.assertEqual(
            union_as_level_set(cancel_fine.change, level),
            set(descendants_at_level(fine, level)))
        self.assertEqual(cw.current_coverage(),
                         CellUnion([coarse, neighbour, untouched_face]))

    def test_fine_submitted_before_its_ancestor(self):
        coarse = cell(0, [0])
        cw = CoverageWithdrawals(CellUnion([coarse]))
        level = 6
        reference = BruteForceReference([coarse], level)

        ancestor = cell(0, [0, 1])
        fine = cell(0, [0, 1, 2, 0, 3])

        cw.submit('fine', [fine])
        reference.submit('fine', [fine])
        # The ancestor order adds nothing on the fine cell itself.
        ancestor_receipt = cw.submit('ancestor', [ancestor])
        reference.submit('ancestor', [ancestor])
        self.assertEqual(
            union_as_level_set(ancestor_receipt.change, level),
            set(descendants_at_level(ancestor, level))
            - set(descendants_at_level(fine, level)))

        cancel_ancestor = cw.cancel('ancestor')
        reference.cancel('ancestor')
        self.assertEqual(
            union_as_level_set(cancel_ancestor.change, level),
            set(descendants_at_level(ancestor, level))
            - set(descendants_at_level(fine, level)))
        self.assertFalse(cw.current_coverage().contains(fine))
        self.assertEqual(
            union_as_level_set(cw.current_coverage(), level),
            reference.active_set())

        cw.cancel('fine')
        reference.cancel('fine')
        self.assertEqual(cw.current_coverage(), CellUnion([coarse]))


class RandomDifferentialTest(unittest.TestCase):

    ENUM_LEVEL = 5

    def setUp(self):
        self.base = CellUnion([
            CellId.from_face_pos_level(0, 0, 0),
            CellId.from_face_pos_level(1, 0, 0),
        ])
        self.rng = random.Random(20261121)
        self.pool = self._build_request_pool()

    def _random_face0_cell(self):
        depth = self.rng.randint(1, 4)
        path = [self.rng.randint(0, 3) for _ in range(depth)]
        return cell(0, path)

    def _build_request_pool(self):
        pool = [self._random_face0_cell() for _ in range(24)]
        # Parent/child pairs and exact duplicates.
        for _ in range(4):
            parent = self._random_face0_cell()
            pool.append(parent)
            pool.append(parent.child(self.rng.randint(0, 3)))
        # Cells outside the base coverage on other faces.
        pool.extend([
            CellId.from_face_pos_level(2, 0, 0),
            cell(3, [0, 2]),
            cell(4, [3, 1, 0]),
        ])
        return pool

    def _random_requests(self):
        count = self.rng.randint(1, 4)
        requests = [self.rng.choice(self.pool) for _ in range(count)]
        if self.rng.random() < 0.5:
            requests.append(requests[0])
        return requests

    def test_random_sequence_matches_brute_force(self):
        cw = CoverageWithdrawals(self.base)
        reference = BruteForceReference(self.base.cell_ids(),
                                        self.ENUM_LEVEL)
        ticket_ids = ['T{:02d}'.format(i) for i in range(10)]
        submitted = set()
        cancelled = set()
        stored_receipts = {}

        for _ in range(400):
            ticket_id = self.rng.choice(ticket_ids)
            action = self.rng.choice(
                ['submit', 'submit', 'cancel', 'cancel', 'replay'])
            cw.reset_access_records()

            if action == 'submit' and ticket_id not in submitted:
                requests = self._random_requests()
                before = reference.active_set()
                receipt = cw.submit(ticket_id, requests)
                reference.submit(ticket_id, requests)
                submitted.add(ticket_id)
                stored_receipts[(ticket_id, 'submit')] = receipt

                expected_change = before - reference.active_set()
                self.assertEqual(
                    union_as_level_set(receipt.change, self.ENUM_LEVEL),
                    expected_change)
                self.assertEqual(receipt.current, cw.current_coverage())
                self._assert_access_scoped(cw, requests)

            elif action == 'cancel' and ticket_id in submitted \
                    and ticket_id not in cancelled:
                requests = cw.order_requests(ticket_id)
                before = reference.active_set()
                receipt = cw.cancel(ticket_id)
                reference.cancel(ticket_id)
                cancelled.add(ticket_id)
                stored_receipts[(ticket_id, 'cancel')] = receipt

                expected_change = reference.active_set() - before
                self.assertEqual(
                    union_as_level_set(receipt.change, self.ENUM_LEVEL),
                    expected_change)
                self.assertEqual(receipt.current, cw.current_coverage())
                self._assert_access_scoped(cw, requests)

            else:
                # Replays of either kind must be identity no-ops.
                if ticket_id in submitted and ticket_id not in cancelled:
                    replay = cw.submit(ticket_id, self._random_requests())
                    original = stored_receipts[(ticket_id, 'submit')]
                    self.assertIs(replay, original)
                if ticket_id in cancelled:
                    replay = cw.cancel(ticket_id)
                    original = stored_receipts[(ticket_id, 'cancel')]
                    self.assertIs(replay, original)
                self.assertEqual(cw.access_records(), [])

            self.assertEqual(
                union_as_level_set(cw.current_coverage(),
                                   self.ENUM_LEVEL),
                reference.active_set())

        # Every historical receipt is still available and immutable.
        for key, receipt in stored_receipts.items():
            self.assertIs(cw.receipt(key[0], key[1]), receipt)
        # Nothing outside the base coverage ever appears in the coverage.
        for outside in self.pool:
            if not self.base.intersects(outside):
                self.assertFalse(cw.current_coverage().intersects(outside))

    def _assert_access_scoped(self, cw, requests):
        allowed_faces = {cell_id.face() for cell_id in requests}
        for record in cw.access_records():
            self.assertTrue(set(record.faces).issubset(allowed_faces))


if __name__ == '__main__':
    unittest.main()

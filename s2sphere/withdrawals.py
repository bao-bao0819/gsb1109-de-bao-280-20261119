"""Idempotent withdrawal orders over an S2 coverage.

A :class:`CoverageWithdrawals` keeps a base coverage (a
:class:`CellUnion`) and a set of numbered withdrawal orders.  Each order
asks for some cells to stop being scanned; the active coverage is the
base coverage with every active withdrawal subtracted.

Withdrawals are kept in a trie of disjoint leaf-index spans instead of
being folded into the coverage.  A leaf cell id is odd, so mapping it to
``leaf_index = cell_id.id() >> 1`` turns every cell into a contiguous
half-open interval ``[a, b)`` of leaf indices and makes adjacent cells
tile exactly.  Each trie leaf remembers the set of orders that currently
own it, which gives three properties:

* an order overlapping an earlier one still records every cell it asked
  for, so cancelling the earlier order restores exactly the coverage
  that order alone was responsible for while overlapping cells stay
  withdrawn under the other order;
* submitting or cancelling an order only walks trie nodes on the paths
  of its own cells - orders stored on a disjoint face are never read or
  replayed, which is checked through
  :meth:`CoverageWithdrawals.access_records`;
* requests outside the base coverage are remembered on their order but
  can never add anything to the active coverage.

Every response carries the active coverage as a normalised
:class:`CellUnion` together with the coverage that this particular call
actually changed.  Receipts are kept and returned verbatim when an order
id is replayed, so retries never withdraw or restore anything twice.
"""

from __future__ import absolute_import, division, print_function

from collections import namedtuple

from .sphere import CellId, CellUnion


_CellSpan = namedtuple('_CellSpan', ['lo', 'hi', 'cell_id'])


def _leaf_span(cell_id):
    """Half-open leaf-index interval ``[lo, hi)`` covered by a cell."""
    lo = cell_id.range_min().id() >> 1
    hi = (cell_id.range_max().id() + 1) >> 1
    return lo, hi


def _face_at_index(index):
    return CellId((index << 1) | 1).face()


def _ranges_to_cell_ids(ranges):
    """Greedily decompose half-open leaf-index ranges into CellIds.

    Taking the largest cell that starts at the cursor and still fits
    inside ``hi`` yields a normalised, non-overlapping tiling of every
    range.
    """
    cell_ids = []
    for lo, hi in ranges:
        cursor = lo
        while cursor < hi:
            cell_id = CellId((cursor << 1) | 1)
            while cell_id.level() > 0:
                parent = cell_id.parent()
                parent_lo = parent.range_min().id() >> 1
                parent_hi = (parent.range_max().id() + 1) >> 1
                if parent_lo != cursor or parent_hi > hi:
                    break
                cell_id = parent
            cell_ids.append(cell_id)
            cursor = (cell_id.range_max().id() + 1) >> 1
    return cell_ids


class _SpanTree(object):
    """Trie of half-open leaf-index spans with per-leaf owner sets.

    A leaf spans ``[lo, hi)`` and is owned by a non-empty set of order
    ids.  Internal nodes only group children and own nothing themselves;
    the root has no span of its own and its children may be separated by
    unowned gaps.  An internal node created by splitting a leaf is
    always tiled contiguously by its children.
    """

    def __init__(self, lo=None, hi=None):
        self.lo = lo
        self.hi = hi
        self.owners = set()
        self.children = []

    def is_leaf(self):
        return not self.children

    def is_empty(self):
        return not self.children and not self.owners

    def _log(self, lo, recorder, ticket_id):
        if recorder is not None and ticket_id is not None:
            recorder.record(ticket_id, _face_at_index(lo))

    def _new_leaf(self, lo, hi, owners):
        leaf = _SpanTree(lo, hi)
        leaf.owners = set(owners)
        return leaf

    def _locate(self, lo):
        low = 0
        high = len(self.children)
        while low < high:
            middle = (low + high) // 2
            if self.children[middle].lo < lo:
                low = middle + 1
            else:
                high = middle
        return low

    def insert(self, lo, hi, ticket_id, recorder=None):
        if self.lo is not None and self.is_leaf():
            self._log(lo, recorder, ticket_id)
            if hi <= self.lo or lo >= self.hi:
                return
            if lo <= self.lo and hi >= self.hi:
                self.owners.add(ticket_id)
                return
            self._split_leaf(lo, hi, ticket_id)
            return
        self._insert_container(lo, hi, ticket_id, recorder)

    def _split_leaf(self, lo, hi, ticket_id):
        """Split this leaf around the incoming span, keeping ownership."""
        boundaries = []
        if self.lo < lo:
            boundaries.append((self.lo, lo, set(self.owners)))
        middle_owners = set(self.owners)
        middle_owners.add(ticket_id)
        boundaries.append((max(self.lo, lo), min(self.hi, hi),
                           middle_owners))
        if self.hi > hi:
            boundaries.append((hi, self.hi, set(self.owners)))
        self.owners = set()
        self.children = [self._new_leaf(piece_lo, piece_hi, piece_owners)
                         for piece_lo, piece_hi, piece_owners in boundaries]

    def _insert_container(self, lo, hi, ticket_id, recorder):
        index = self._locate(lo)
        if index > 0 and self.children[index - 1].hi > lo:
            index -= 1

        cursor = lo
        while index < len(self.children) and self.children[index].lo < hi:
            child = self.children[index]
            if cursor < child.lo:
                self.children.insert(
                    index, self._new_leaf(cursor, child.lo,
                                          set([ticket_id])))
                self._log(cursor, recorder, ticket_id)
                index += 1
            child.insert(max(lo, child.lo), min(hi, child.hi),
                         ticket_id, recorder)
            cursor = child.hi
            index += 1

        if cursor < hi:
            self.children.insert(
                index, self._new_leaf(cursor, hi, set([ticket_id])))
            self._log(cursor, recorder, ticket_id)

    def remove(self, lo, hi, ticket_id, recorder=None):
        """Remove an owner; return ranges that became completely unowned."""
        restored = []
        if self.lo is not None and self.is_leaf():
            if hi <= self.lo or lo >= self.hi:
                return restored
            self._log(lo, recorder, ticket_id)
            if lo <= self.lo and hi >= self.hi:
                self.owners.discard(ticket_id)
                if not self.owners:
                    restored.append((self.lo, self.hi))
                    self.lo = None
                    self.hi = None
            else:
                self._remove_partial(lo, hi, ticket_id, restored)
            return restored

        index = self._locate(lo)
        if index > 0 and self.children[index - 1].hi > lo:
            index -= 1
        start = index
        surviving = []
        while index < len(self.children) and self.children[index].lo < hi:
            child = self.children[index]
            restored.extend(child.remove(
                max(lo, child.lo), min(hi, child.hi),
                ticket_id, recorder))
            if not child.is_empty():
                surviving.append(child)
            index += 1
        self.children[start:index] = surviving
        self._try_merge()
        return restored

    def _remove_partial(self, lo, hi, ticket_id, restored):
        owners = set(self.owners)
        middle_owners = set(owners)
        middle_owners.discard(ticket_id)
        pieces = []
        if self.lo < lo:
            pieces.append(self._new_leaf(self.lo, lo, owners))
        if middle_owners:
            pieces.append(self._new_leaf(max(self.lo, lo),
                                         min(self.hi, hi), middle_owners))
        else:
            restored.append((max(self.lo, lo), min(self.hi, hi)))
        if self.hi > hi:
            pieces.append(self._new_leaf(hi, self.hi, owners))
        self.owners = set()
        self.children = pieces

    def _try_merge(self):
        if self.lo is None or len(self.children) < 2:
            return
        if any(not child.is_leaf() for child in self.children):
            return
        first_owners = self.children[0].owners
        if any(child.owners != first_owners for child in self.children):
            return
        if self.children[0].lo != self.lo \
                or self.children[-1].hi != self.hi:
            return
        for index in range(len(self.children) - 1):
            if self.children[index].hi != self.children[index + 1].lo:
                return
        self.owners = set(first_owners)
        self.children = []

    def uncovered_ranges(self, lo, hi, recorder=None, ticket_id=None):
        """Return the parts of ``[lo, hi)`` not owned by any order."""
        ranges = []
        if self.lo is not None and self.is_leaf():
            self._log(lo, recorder, ticket_id)
            if hi <= self.lo or lo >= self.hi:
                ranges.append((lo, hi))
            else:
                if lo < self.lo:
                    ranges.append((lo, self.lo))
                if hi > self.hi:
                    ranges.append((self.hi, hi))
            return ranges

        cursor = lo
        index = self._locate(lo)
        if index > 0 and self.children[index - 1].hi > lo:
            index -= 1
        while index < len(self.children) and self.children[index].lo < hi:
            child = self.children[index]
            if cursor < child.lo:
                ranges.append((cursor, child.lo))
            ranges.extend(child.uncovered_ranges(
                max(lo, child.lo), min(hi, child.hi),
                recorder, ticket_id))
            cursor = child.hi
            index += 1
        if cursor < hi:
            ranges.append((cursor, hi))
        return ranges


def _normalize_requests(cell_ids):
    """Drop duplicate requests and requests contained by a coarser one."""
    ordered = sorted(cell_ids,
                     key=lambda item: (item.range_min().id(),
                                       item.level()))
    result = []
    for cell_id in ordered:
        if result and result[-1].contains(cell_id):
            continue
        result.append(cell_id)
    return result


def _spans_from_cell_ids(cell_ids):
    normalized = _normalize_requests(list(cell_ids))
    spans = []
    for cell_id in normalized:
        lo, hi = _leaf_span(cell_id)
        spans.append(_CellSpan(lo, hi, cell_id))
    spans.sort()
    return normalized, spans


class WithdrawalReceipt(object):
    """Result of submitting or cancelling one withdrawal order.

    ``ticket_id`` is the order number, ``applied`` is False when the call
    was a replay of an order already in that state, ``change`` is the
    :class:`CellUnion` of coverage that this call actually withdrew or
    restored, and ``current`` is the full active coverage after the call.
    ``outside_requests`` lists requested cells that never intersected the
    base coverage.
    """

    def __init__(self, ticket_id, action, applied, change, current,
                 outside_requests):
        self.ticket_id = ticket_id
        self.action = action
        self.applied = applied
        self.change = change
        self.current = current
        self.outside_requests = tuple(outside_requests)

    def __repr__(self):
        return ('WithdrawalReceipt(ticket_id={!r}, action={!r}, '
                'applied={!r}, change={!r})').format(
                    self.ticket_id, self.action, self.applied, self.change)

    def __eq__(self, other):
        return (isinstance(other, self.__class__)
                and self.ticket_id == other.ticket_id
                and self.action == other.action
                and self.applied == other.applied
                and self.change == other.change
                and self.current == other.current
                and self.outside_requests == other.outside_requests)

    def __ne__(self, other):
        return not self.__eq__(other)


class AccessRecord(namedtuple('AccessRecord', ['ticket_id', 'faces'])):
    """Faces touched while serving one order-level operation."""


class _AccessRecorder(object):

    def __init__(self):
        self._records = []

    def record(self, ticket_id, face):
        if self._records and self._records[-1][0] == ticket_id:
            self._records[-1][1].add(face)
        else:
            self._records.append((ticket_id, set([face])))

    def records(self):
        return [AccessRecord(ticket_id, frozenset(faces))
                for ticket_id, faces in self._records]

    def reset(self):
        self._records = []


class CoverageWithdrawals(object):
    """Numbered, idempotent withdrawal orders over a base coverage."""

    SUBMIT = 'submit'
    CANCEL = 'cancel'

    def __init__(self, base_coverage):
        self._base = CellUnion(base_coverage.cell_ids())
        self._active = CellUnion(base_coverage.cell_ids())
        self._tree = _SpanTree()
        self._orders = {}
        self._receipts = {}
        self._recorder = _AccessRecorder()

    def base_coverage(self):
        return CellUnion(self._base.cell_ids())

    def current_coverage(self):
        """Active coverage: the base coverage minus active withdrawals."""
        return CellUnion(self._active.cell_ids())

    def active_ticket_ids(self):
        return tuple(sorted(self._orders))

    def order_requests(self, ticket_id):
        """The full request recorded for an order, even if already gone."""
        entry = self._orders.get(ticket_id)
        if entry is None:
            return ()
        return tuple(entry['requests'])

    def receipt(self, ticket_id, action):
        """The historical receipt, or None if that call never happened."""
        return self._receipts.get((ticket_id, action))

    def access_records(self):
        return self._recorder.records()

    def reset_access_records(self):
        self._recorder.reset()

    def submit(self, ticket_id, cell_ids):
        """Submit (or replay) a withdrawal order.

        The receipt's ``change`` only lists coverage that this call newly
        withdraws.  Replaying a live order changes nothing and returns
        the original receipt.
        """
        previous = self._receipts.get((ticket_id, self.SUBMIT))
        if previous is not None:
            return previous

        requests, spans = _spans_from_cell_ids(cell_ids)

        uncovered = []
        for span in spans:
            uncovered.extend(_ranges_to_cell_ids(
                self._tree.uncovered_ranges(
                    span.lo, span.hi, self._recorder, ticket_id)))
        change = CellUnion.get_intersection(
            CellUnion(uncovered), self._base)

        for span in spans:
            self._tree.insert(span.lo, span.hi, ticket_id,
                              self._recorder)

        if change.num_cells():
            self._active = CellUnion.get_difference(self._active, change)

        outside = [cell_id for cell_id in requests
                   if not self._base.intersects(cell_id)]
        receipt = WithdrawalReceipt(
            ticket_id, self.SUBMIT, bool(change.num_cells()),
            change, self.current_coverage(), outside)
        self._orders[ticket_id] = {'requests': requests}
        self._receipts[(ticket_id, self.SUBMIT)] = receipt
        return receipt

    def cancel(self, ticket_id):
        """Cancel (or replay the cancellation of) an order.

        The receipt's ``change`` lists only the coverage that actually
        comes back: cells still withdrawn by another order stay off.
        """
        previous = self._receipts.get((ticket_id, self.CANCEL))
        if previous is not None:
            return previous

        entry = self._orders.pop(ticket_id, None)
        if entry is None:
            receipt = WithdrawalReceipt(
                ticket_id, self.CANCEL, False, CellUnion(),
                self.current_coverage(), [])
            self._receipts[(ticket_id, self.CANCEL)] = receipt
            return receipt

        restored_ranges = []
        for cell_id in entry['requests']:
            lo, hi = _leaf_span(cell_id)
            restored_ranges.extend(self._tree.remove(
                lo, hi, ticket_id, self._recorder))
        restored = CellUnion.get_intersection(
            CellUnion(_ranges_to_cell_ids(restored_ranges)), self._base)

        if restored.num_cells():
            self._active = CellUnion.get_union(self._active, restored)

        receipt = WithdrawalReceipt(
            ticket_id, self.CANCEL, bool(restored.num_cells()),
            restored, self.current_coverage(), [])
        self._receipts[(ticket_id, self.CANCEL)] = receipt
        return receipt

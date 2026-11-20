"""
Audit download helpers.

The warehouse exports the history of a single tracked object in shards.  A
report must fold consecutive historical records that carry the same price and
status into one segment while remaining independent of where an export shard
happens to split that history.

The helpers in this module are read-only and opt-in: enabling a segmented
download never changes the historical table, and callers that keep using the
plain history manager (the legacy download) need no changes at all.

Segmentation rules:

* Records are ordered by ``history_date`` and then ``history_id``, so the
  version order of records sharing a timestamp is preserved.
* Two adjacent records belong to the same segment when their price and status
  are equal.  A remark (``history_change_reason``) change alone never ends a
  segment; the first and last remark are kept as endpoint evidence instead.
* A deletion always ends the running segment and always stands alone, so a
  delete followed by a rebuild cannot rejoin the earlier segment even when the
  rebuilt row has the very same price and status.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional, Sequence

from simple_history.utils import (
    get_app_model_primary_key_name,
    get_history_manager_for_model,
)

DEFAULT_PRICE_FIELD = "price"
DEFAULT_STATUS_FIELD = "status"


@dataclass(eq=False)
class AuditSegment:
    """
    A maximal run of historical records sharing price and status.

    ``eq`` is disabled because segment identity is the covered history rows
    (``first_history_id``/``last_history_id``), not a structural value match:
    two separated runs may hold the same price and status without being the
    same segment.
    """

    price: Any
    status: Any
    history_type: str
    start_date: datetime
    end_date: datetime
    first_history_id: Any
    last_history_id: Any
    history_count: int
    first_change_reason: Optional[str] = None
    last_change_reason: Optional[str] = None

    def span(self, other: "AuditSegment") -> "AuditSegment":
        """
        Return a segment covering ``self`` followed by ``other``.

        The two segments are expected to be adjacent and to share their price
        and status without crossing a deletion, as guaranteed by
        :func:`merge_audit_segments` and :func:`build_audit_segments`.
        """
        return AuditSegment(
            price=self.price,
            status=self.status,
            history_type=self.history_type,
            start_date=self.start_date,
            end_date=other.end_date,
            first_history_id=self.first_history_id,
            last_history_id=other.last_history_id,
            history_count=self.history_count + other.history_count,
            first_change_reason=self.first_change_reason,
            last_change_reason=other.last_change_reason,
        )

    def endpoint_evidence(self) -> dict:
        """
        Boundary evidence identifying where the segment starts and ends.

        Two reports describe the same endpoints when their evidence matches,
        independently of how the underlying rows were split into shards.
        """
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "first_history_id": self.first_history_id,
            "last_history_id": self.last_history_id,
            "first_change_reason": self.first_change_reason,
            "last_change_reason": self.last_change_reason,
        }

    def as_dict(self) -> dict:
        """Return a plain-data representation of the segment."""
        return {
            "price": self.price,
            "status": self.status,
            "history_type": self.history_type,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "first_history_id": self.first_history_id,
            "last_history_id": self.last_history_id,
            "history_count": self.history_count,
            "first_change_reason": self.first_change_reason,
            "last_change_reason": self.last_change_reason,
        }


@dataclass(eq=False)
class AuditShard:
    """
    One export shard covering an ordered slice of one object's history.

    ``identifier`` is stable for a given object, shard position and revision.
    A corrected re-export uses a new revision (and therefore a new identifier)
    and records the identifier it replaces in ``supersedes``; a plain
    redelivery reuses the same identifier.
    """

    identifier: str
    index: int
    revision: int
    segments: List[AuditSegment] = field(default_factory=list)
    supersedes: Optional[str] = None

    @property
    def history_count(self) -> int:
        return sum(segment.history_count for segment in self.segments)


class _SegmentBuilder:
    """Accumulates the rows of one segment while the history is scanned."""

    def __init__(self, record, price: Any, status: Any):
        self.price = price
        self.status = status
        self.history_type = record.history_type
        self.start_date = record.history_date
        self.end_date = record.history_date
        self.first_history_id = record.history_id
        self.last_history_id = record.history_id
        self.history_count = 1
        self.first_change_reason = record.history_change_reason
        self.last_change_reason = record.history_change_reason

    @property
    def key(self):
        return self.price, self.status

    def can_absorb(self, record, price: Any, status: Any) -> bool:
        """
        Whether ``record`` continues this segment.

        A deletion never continues a segment and never lets the following row
        continue, so delete/rebuild always splits even on identical values.
        """
        if self.history_type == "-" or record.history_type == "-":
            return False
        return self.key == (price, status)

    def add(self, record) -> None:
        self.end_date = record.history_date
        self.last_history_id = record.history_id
        self.last_change_reason = record.history_change_reason
        self.history_count += 1

    def finish(self) -> AuditSegment:
        return AuditSegment(
            price=self.price,
            status=self.status,
            history_type=self.history_type,
            start_date=self.start_date,
            end_date=self.end_date,
            first_history_id=self.first_history_id,
            last_history_id=self.last_history_id,
            history_count=self.history_count,
            first_change_reason=self.first_change_reason,
            last_change_reason=self.last_change_reason,
        )


def build_audit_segments(
    records,
    *,
    price_field: str = DEFAULT_PRICE_FIELD,
    status_field: str = DEFAULT_STATUS_FIELD,
) -> List[AuditSegment]:
    """
    Fold an ordered iterable of historical records into audit segments.

    Consecutive records are folded together while their price and status stay
    equal and neither record is a deletion; a remark change alone does not end
    a segment.  Records must already be ordered by ``history_date`` and
    ``history_id``; :func:`audit_history_rows` returns them in that order.
    """
    builders: List[_SegmentBuilder] = []
    current: Optional[_SegmentBuilder] = None
    for record in records:
        price = getattr(record, price_field)
        status = getattr(record, status_field)
        if current is not None and current.can_absorb(record, price, status):
            current.add(record)
        else:
            current = _SegmentBuilder(record, price, status)
            builders.append(current)
    return [builder.finish() for builder in builders]


def merge_audit_segments(
    existing: Sequence[AuditSegment], incoming: Sequence[AuditSegment]
) -> List[AuditSegment]:
    """
    Concatenate two ordered segment lists, folding across the seam.

    This is what makes the report independent of shard boundaries: the last
    segment of one shard and the first segment of the next shard merge exactly
    when two raw rows would have merged in a one-shot export.  The shard
    boundary itself is never treated as an event.  Segments that merely share
    their price and status but are separated by another segment stay apart.
    """
    merged: List[AuditSegment] = list(existing)
    for segment in incoming:
        if merged and _is_continuous(merged[-1], segment):
            merged[-1] = merged[-1].span(segment)
        else:
            merged.append(segment)
    return merged


def _is_continuous(left: AuditSegment, right: AuditSegment) -> bool:
    if left.history_type == "-" or right.history_type == "-":
        return False
    return left.price == right.price and left.status == right.status


def audit_history_rows(model, object_id, *, using: Optional[str] = None):
    """
    Return the raw historical rows of one object in canonical export order.

    Ordering is ascending ``history_date`` followed by ascending
    ``history_id``, so records with identical timestamps keep their version
    order.  The rows are returned unmodified; the original history remains
    fully queryable through the regular history manager.
    """
    manager = get_history_manager_for_model(model)
    queryset = manager.using(using) if using is not None else manager
    pk_name = get_app_model_primary_key_name(model)
    return list(
        queryset.filter(**{pk_name: object_id}).order_by(
            "history_date", "history_id"
        )
    )


def audit_segments_for_object(
    model,
    object_id,
    *,
    price_field: str = DEFAULT_PRICE_FIELD,
    status_field: str = DEFAULT_STATUS_FIELD,
    using: Optional[str] = None,
) -> List[AuditSegment]:
    """
    One-shot (legacy-style) audit download for one object.

    This reads the same historical table as the regular manager and requires no
    opt-in, so existing downloads keep working without segmented export.
    """
    records = audit_history_rows(model, object_id, using=using)
    return build_audit_segments(
        records, price_field=price_field, status_field=status_field
    )


def build_audit_shards(
    records,
    *,
    shard_sizes: Sequence[int],
    identifier_prefix: str,
    revision: int = 1,
    start_index: int = 1,
    price_field: str = DEFAULT_PRICE_FIELD,
    status_field: str = DEFAULT_STATUS_FIELD,
) -> List[AuditShard]:
    """
    Split ordered historical records into export shards.

    ``shard_sizes`` gives the number of raw rows each shard contains in order;
    its values must sum to the number of records.  Segments are folded inside
    each shard independently, and :func:`merge_audit_segments` reconstructs the
    one-shot report on the receiving side.  ``revision`` starts at ``1`` and a
    corrected re-export passes a higher revision, which marks the previous
    revision's shard identifier as superseded.  ``start_index`` allows a single
    affected shard to be rebuilt on its own while keeping its original shard
    position.
    """
    records = list(records)
    if sum(shard_sizes) != len(records):
        raise ValueError(
            "shard_sizes sum to "
            f"{sum(shard_sizes)} but {len(records)} records were provided"
        )

    shards: List[AuditShard] = []
    offset = 0
    for position, size in enumerate(shard_sizes):
        index = start_index + position
        slice_ = records[offset : offset + size]
        offset += size
        identifier = f"{identifier_prefix}:shard-{index}:rev-{revision}"
        supersedes = (
            f"{identifier_prefix}:shard-{index}:rev-{revision - 1}"
            if revision > 1
            else None
        )
        shards.append(
            AuditShard(
                identifier=identifier,
                index=index,
                revision=revision,
                segments=build_audit_segments(
                    slice_, price_field=price_field, status_field=status_field
                ),
                supersedes=supersedes,
            )
        )
    return shards


class AuditReportReceiver:
    """
    Reference receiver that assembles shards into one segment report.

    Guarantees:

    * applying the same shard identifier twice is a no-op, so redelivery never
      adds history rows;
    * a corrected shard replaces the revision it supersedes while every other
      shard (and therefore every unaffected segment) is retained untouched;
    * the assembled report equals the one-shot export because shard seams are
      folded with :func:`merge_audit_segments`.
    """

    def __init__(self):
        self._active_shards: dict = {}
        self._applied_identifiers: set = set()

    def apply_shard(self, shard: AuditShard) -> bool:
        """
        Apply one shard.

        Return ``True`` when the shard was applied and ``False`` when the
        identifier had already been consumed (a redelivery).
        """
        if shard.identifier in self._applied_identifiers:
            return False
        self._applied_identifiers.add(shard.identifier)
        if shard.supersedes is not None:
            self._active_shards.pop(shard.supersedes, None)
        self._active_shards[shard.identifier] = shard
        return True

    def active_identifiers(self) -> List[str]:
        """Identifiers of the shards currently contributing to the report."""
        return [
            identifier
            for identifier, shard in sorted(
                self._active_shards.items(), key=lambda item: item[1].index
            )
        ]

    def segments(self) -> List[AuditSegment]:
        ordered = sorted(
            self._active_shards.values(), key=lambda shard: shard.index
        )
        segments: List[AuditSegment] = []
        for shard in ordered:
            segments = merge_audit_segments(segments, shard.segments)
        return segments

    def history_row_count(self) -> int:
        """Number of raw historical rows represented by the active shards."""
        return sum(segment.history_count for segment in self.segments())

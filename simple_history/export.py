"""
Export of customer status changes from django-simple-history tables.

The exporter walks the historical rows of a model in the deterministic
``(history_date, history_id)`` order and turns them into *logical* status
changes for a given calendar month.  Only changes of the tracked status field
are emitted: rows where only other fields changed produce no event.

Exporting is resumable.  A cursor captures the ``(history_date, history_id)``
position of the last delivered historical row and therefore distinguishes
rows that share the same ``history_date`` (for example rows written by one
batch import).  Re-reading with the same cursor replays the same events and
never creates additional logical events.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterator, Optional

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.utils import timezone

from simple_history.utils import get_history_model_for_model

_CURSOR_VERSION = 1


@dataclass(frozen=True)
class CustomerStatusChange:
    """One logical status change derived from historical rows."""

    object_id: object
    history_id: object
    history_date: datetime
    history_type: str
    old_status: Optional[str]
    new_status: Optional[str]
    history_user_id: Optional[object]
    history_user: object
    history_change_reason: Optional[str]


@dataclass(frozen=True)
class CustomerStatusExportCursor:
    """
    Resumable position in the ordered history stream.

    ``history_id`` is part of the position so that rows sharing one
    ``history_date`` (a common timestamp from a batch import) can be resumed
    row by row instead of restarting at the whole timestamp.
    """

    history_date: datetime
    history_id: object

    def encode(self) -> str:
        payload = {
            "version": _CURSOR_VERSION,
            "history_date": self.history_date.isoformat(),
            "history_id": self.history_id,
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @classmethod
    def decode(cls, token: str) -> "CustomerStatusExportCursor":
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid export cursor token.") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _CURSOR_VERSION
        ):
            raise ValueError("Invalid export cursor token.")
        try:
            history_date = datetime.fromisoformat(payload["history_date"])
            history_id = payload["history_id"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid export cursor token.") from exc
        if history_id is None:
            raise ValueError("Invalid export cursor token.")
        return cls(history_date=history_date, history_id=history_id)


def _as_supported_datetime(value: datetime) -> datetime:
    """Make the month boundary timezone-aware when the project uses TZ."""
    if settings.USE_TZ and timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value


def month_start(month: date) -> datetime:
    """Return the inclusive lower bound of the given calendar month."""
    return _as_supported_datetime(
        datetime.combine(date(month.year, month.month, 1), time.min)
    )


def month_end(month: date) -> datetime:
    """Return the exclusive upper bound of the given calendar month."""
    if month.month == 12:
        boundary = datetime(month.year + 1, 1, 1)
    else:
        boundary = datetime(month.year, month.month + 1, 1)
    return _as_supported_datetime(boundary)


class CustomerStatusExporter:
    """
    Stream logical status changes of one history-tracked model per month.

    Historical rows are read globally in ``(history_date, history_id)`` order,
    so rows written at the same timestamp are delivered in historical-row
    order.  Rows before the month only establish the baseline status and are
    never reported as changes of the month.
    """

    def __init__(self, model, status_field: str = "status"):
        self.history_model = get_history_model_for_model(model)
        self.status_field = status_field
        self._pk_attname = self.history_model.instance_type._meta.pk.attname

    def read_batch(
        self,
        month: date,
        *,
        limit: Optional[int] = None,
        after: Optional[CustomerStatusExportCursor] = None,
    ) -> tuple[list[CustomerStatusChange], Optional[CustomerStatusExportCursor]]:
        """
        Materialize at most ``limit`` status changes and the next cursor.

        ``next_cursor`` is ``None`` when the month has been fully consumed.
        Passing the same cursor again replays the same batch without creating
        any additional logical event.
        """
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None.")
        events: list[CustomerStatusChange] = []
        next_cursor = None
        for event in self.stream_status_changes(month, after=after):
            events.append(event)
            next_cursor = CustomerStatusExportCursor(
                history_date=event.history_date, history_id=event.history_id
            )
            if limit is not None and len(events) >= limit:
                return events, next_cursor
        return events, None

    def stream_status_changes(
        self,
        month: date,
        *,
        after: Optional[CustomerStatusExportCursor] = None,
    ) -> Iterator[CustomerStatusChange]:
        """Yield the logical status changes of ``month`` in delivery order."""
        start = month_start(month)
        end = month_end(month)
        if after is not None and not (start <= after.history_date < end):
            raise ValueError("Cursor does not belong to the given month.")

        queryset = self.history_model.objects.filter(
            history_date__gte=start, history_date__lt=end
        )
        if after is not None:
            queryset = queryset.filter(
                Q(history_date__gt=after.history_date)
                | Q(
                    history_date=after.history_date,
                    history_id__gt=after.history_id,
                )
            )
        queryset = queryset.order_by("history_date", "history_id")

        # Latest status known for each object while scanning.  ``None`` means
        # the object has no live state yet (month start, or after a deletion),
        # which is distinct from a status that genuinely stores ``None``.
        known_status: dict[object, Optional[str]] = {}
        # ``history_type`` of the previous row of each object, so that a
        # creation following a deletion is recognized even when rows of other
        # objects sit between them.
        previous_history_type: dict[object, str] = {}

        for row in queryset:
            object_id = getattr(row, self._pk_attname)

            if object_id not in known_status:
                # On resume the running state of this object is rebuilt from
                # the rows already consumed in earlier batches.
                known_status[object_id], previous_history_type[object_id] = (
                    self._state_before_scan(object_id, start, after)
                )
            elif previous_history_type[object_id] == "-":
                # The object was deleted and this row belongs to a new
                # existence period: it must not inherit the old period's
                # status.
                known_status[object_id] = None

            previous_status = known_status[object_id]
            new_status = getattr(row, self.status_field)
            if new_status != previous_status:
                yield self._build_change(row, previous_status, new_status)

            known_status[object_id] = new_status
            previous_history_type[object_id] = row.history_type

    def _state_before_scan(
        self,
        object_id: object,
        start: datetime,
        after: Optional[CustomerStatusExportCursor],
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Return ``(status, history_type)`` of the object's latest row that is
        already part of the consumed stream: before the month on a fresh run,
        or at or before the cursor when resuming.
        """
        filter_kwargs = {self._pk_attname: object_id}
        if after is None:
            position_filter = Q(history_date__lt=start)
        else:
            position_filter = Q(history_date__lt=after.history_date) | Q(
                history_date=after.history_date,
                history_id__lte=after.history_id,
            )
        latest = (
            self.history_model.objects.filter(position_filter, **filter_kwargs)
            .order_by("-history_date", "-history_id")
            .first()
        )
        if latest is None or latest.history_type == "-":
            # No live object at this position: the next creation has no
            # previous status.
            return None, getattr(latest, "history_type", None)
        return getattr(latest, self.status_field), latest.history_type

    def _build_change(
        self,
        row,
        previous_status: Optional[str],
        new_status: Optional[str],
    ) -> CustomerStatusChange:
        try:
            history_user = row.history_user
        except ObjectDoesNotExist:
            history_user = None
        return CustomerStatusChange(
            object_id=getattr(row, self._pk_attname),
            history_id=row.history_id,
            history_date=row.history_date,
            history_type=row.history_type,
            old_status=previous_status,
            new_status=new_status,
            history_user_id=row.history_user_id,
            history_user=history_user,
            history_change_reason=row.history_change_reason,
        )

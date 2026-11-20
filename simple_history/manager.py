from django.conf import settings
from django.db import models
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from simple_history.exceptions import (
    MissingBatchIdFieldError,
    NotEffectiveDatedModelError,
)
from simple_history.utils import (
    get_app_model_primary_key_name,
    get_change_reason_from_object,
)

# when converting a historical record to an instance, this attribute is added
# to the instance so that code can reverse the instance to its historical record
SIMPLE_HISTORY_REVERSE_ATTR_NAME = "_history"


class HistoricalQuerySet(QuerySet):
    """
    Enables additional functionality when working with historical records.

    For additional history on this topic, see:
        - https://github.com/django-commons/django-simple-history/pull/229
        - https://github.com/django-commons/django-simple-history/issues/354
        - https://github.com/django-commons/django-simple-history/issues/397
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._as_instances = False
        self._as_of = None
        self._effective_dated = False
        self._pk_attr = self.model.instance_type._meta.pk.attname

    def as_instances(self) -> "HistoricalQuerySet":
        """
        Return a queryset that generates instances instead of historical records.
        Queries against the resulting queryset will translate `pk` into the
        primary key field of the original type.
        """
        if not self._as_instances:
            result = self.exclude(history_type="-")
            result._as_instances = True
        else:
            result = self._clone()
        return result

    def filter(self, *args, **kwargs) -> "HistoricalQuerySet":
        """
        If a `pk` filter arrives and the queryset is returning instances
        then the caller actually wants to filter based on the original
        type's primary key, and not the history_id (historical record's
        primary key); this happens frequently with DRF.
        """
        if self._as_instances and "pk" in kwargs:
            kwargs[self._pk_attr] = kwargs.pop("pk")
        return super().filter(*args, **kwargs)

    def effective_dated(self) -> "HistoricalQuerySet":
        """
        Switch point-in-time methods (e.g. ``latest_of_each()``) onto the
        two-timeline semantics: records are ordered and selected by business
        effective date rather than by the system ``history_date``.

        The rows themselves are not filtered - every record, including ones
        with a null business effective date, stays in the queryset. Records
        with a null business effective date keep their legacy behavior: their
        business effective date is taken to be their entry date
        (``history_date``), so old rows stay queryable through the new API
        without any backfill.
        """
        if not getattr(self.model, "_has_effective_dates", False):
            raise NotEffectiveDatedModelError(
                f"{self.model.__name__} has no business effective date field; "
                "enable effective dating with "
                "HistoricalRecords(history_effective_date_field=...)."
            )
        result = self._clone()
        result._effective_dated = True
        return result

    @property
    def _effective_date_name(self):
        return getattr(self.model, "_history_effective_date_field", None)

    def _effective_date_expression(self):
        """
        Coalesce the (nullable) business effective date with the entry date so
        that records without a business effective date behave exactly as they
        did before effective dating existed.
        """
        from django.db.models.functions import Coalesce

        return Coalesce(self._effective_date_name, "history_date")

    def latest_of_each(self) -> "HistoricalQuerySet":
        """
        Ensures results in the queryset are the latest historical record for each
        primary key. This includes deletion records.

        On an :meth:`effective_dated` queryset the "latest" record is the one
        with the greatest business effective date (falling back to
        ``history_date`` for legacy rows that have no business effective date);
        ties on the same business effective date are resolved by entry date and
        then ``history_id``, so a correction entered later always supersedes the
        record it amends.
        """
        if self._effective_dated:
            return self._latest_effective_of_each()
        return self._latest_recorded_of_each()

    def _latest_recorded_of_each(self) -> "HistoricalQuerySet":
        # Subquery for finding the records that belong to the same history-tracked
        # object as the record from the outer query (identified by `_pk_attr`),
        # and that have a later `history_date` than the outer record.
        # The very latest record of a history-tracked object should be excluded from
        # this query - which will make it included in the `~Exists` query below.
        later_records = self.filter(
            Q(**{self._pk_attr: OuterRef(self._pk_attr)}),
            Q(history_date__gt=OuterRef("history_date")),
        )

        # Filter the records to only include those for which the `later_records`
        # subquery does not return any results.
        return self.filter(~Exists(later_records))

    def _latest_effective_of_each(self) -> "HistoricalQuerySet":
        # A record is "later" on the business timeline when its effective date
        # is greater; records that share an effective date are ordered by when
        # they entered the warehouse (history_date, then history_id), which is
        # what makes same-day corrections deterministic.
        effective_date = self._effective_date_expression()
        if "_effective_at" not in self.query.annotations:
            base = self.annotate(_effective_at=effective_date)
        else:
            base = self
        later_records = base.filter(
            Q(**{self._pk_attr: OuterRef(self._pk_attr)}),
            Q(
                Q(_effective_at__gt=OuterRef("_effective_at"))
                | Q(
                    _effective_at=OuterRef("_effective_at"),
                    history_date__gt=OuterRef("history_date"),
                )
                | Q(
                    _effective_at=OuterRef("_effective_at"),
                    history_date=OuterRef("history_date"),
                    history_id__gt=OuterRef("history_id"),
                )
            ),
        )
        return base.filter(~Exists(later_records))

    def _select_related_history_tracked_objs(self) -> "HistoricalQuerySet":
        """
        A convenience method that calls ``select_related()`` with all the names of
        the model's history-tracked ``ForeignKey`` fields.
        """
        field_names = [
            field.name
            for field in self.model.tracked_fields
            if isinstance(field, models.ForeignKey)
        ]
        return self.select_related(*field_names)

    def _clone(self) -> "HistoricalQuerySet":
        c = super()._clone()
        c._as_instances = self._as_instances
        c._as_of = self._as_of
        c._effective_dated = self._effective_dated
        c._pk_attr = self._pk_attr
        return c

    def _fetch_all(self) -> None:
        super()._fetch_all()
        self._instanceize()

    def _instanceize(self) -> None:
        """
        Convert the result cache to instances if possible and it has not already been
        done.  If a query extracts `.values(...)` then the result cache will not contain
        historical objects to be converted.
        """
        if (
            self._result_cache
            and self._as_instances
            and isinstance(self._result_cache[0], self.model)
        ):
            self._result_cache = [item.instance for item in self._result_cache]
            for item in self._result_cache:
                historic = getattr(item, SIMPLE_HISTORY_REVERSE_ATTR_NAME)
                setattr(historic, "_as_of", self._as_of)


class HistoryManager(models.Manager):
    def __init__(self, model, instance=None):
        super().__init__()
        self.model = model
        self.instance = instance

    def get_super_queryset(self):
        return super().get_queryset()

    def get_queryset(self):
        qs = self.get_super_queryset()
        if self.instance is None:
            return qs

        key_name = get_app_model_primary_key_name(self.instance)
        return self.get_super_queryset().filter(**{key_name: self.instance.pk})

    def most_recent(self):
        """
        Returns the most recent copy of the instance available in the history.
        """
        if not self.instance:
            raise TypeError(
                "Can't use most_recent() without a {} instance.".format(
                    self.model._meta.object_name
                )
            )
        tmp = []

        for field in self.model.tracked_fields:
            if isinstance(field, models.ForeignKey):
                tmp.append(field.name + "_id")
            else:
                tmp.append(field.name)
        fields = tuple(tmp)
        try:
            values = self.get_queryset().values(*fields)[0]
        except IndexError:
            raise self.instance.DoesNotExist(
                "%s has no historical record." % self.instance._meta.object_name
            )
        return self.instance.__class__(**values)

    @property
    def _effective_date_field(self):
        return getattr(self.model, "_history_effective_date_field", None)

    @property
    def _batch_id_field(self):
        return getattr(self.model, "_history_batch_id_field", None)

    @property
    def _correction_field(self):
        return getattr(self.model, "_history_correction_field", None)

    def _require_effective_dates(self):
        if not self._effective_date_field:
            raise NotEffectiveDatedModelError(
                f"{self.model.__name__} has no business effective date field; "
                "enable effective dating with "
                "HistoricalRecords(history_effective_date_field=...)."
            )

    def as_of(self, date, *, business_date=None):
        """
        Get a snapshot as of a specific date.

        When this is used on an instance, it will return the instance based
        on the specific date.  If the instance did not exist yet, or had been
        deleted, then a DoesNotExist error is railed.

        When this is used on a model's history manager, the resulting queryset
        will locate the most recent historical record before the specified date
        for each primary key, generating instances.  If the most recent historical
        record is a deletion, that instance is dropped from the result.

        A common usage pattern for querying is to accept an optional time
        point `date` and then use:

            `qs = <Model>.history.as_of(date) if date else <Model>.objects`

        after which point one can add filters, values - anything a normal
        queryset would support.

        To retrieve historical records, query the model's history directly;
        for example:
            `qs = <Model>.history.filter(history_date__lte=date, pk=...)`

        To retrieve the most recent historical record, including deletions,
        you could then use:
            `qs = qs.latest_of_each()`

        Two-timeline (bitemporal) snapshots
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

        When the historical model carries a business effective date (enabled
        through ``history_effective_date_field``) pass ``business_date`` to
        reproduce any past export exactly: only records that had actually been
        entered by the system cutoff ``date`` (``history_date__lte=date``) and
        whose business effective date is no later than ``business_date`` are
        considered, and the winning record per object is the one with the
        greatest business effective date.  Records corrected or withdrawn after
        the cutoff simply do not exist for that export.

        Calling ``as_of(date)`` without ``business_date`` keeps the original,
        single-timeline behavior (selection by ``history_date``), including for
        legacy rows whose business effective date is null.
        """
        if business_date is not None:
            return self.effective_as_of(system_date=date, business_date=business_date)

        queryset = self.get_queryset().filter(history_date__lte=date)
        if not self.instance:
            if isinstance(queryset, HistoricalQuerySet):
                queryset._as_of = date
            queryset = queryset.latest_of_each().as_instances()
            return queryset

        try:
            # historical records are sorted in reverse chronological order
            history_obj = queryset[0]
        except IndexError:
            raise self.instance.DoesNotExist(
                "%s had not yet been created." % self.instance._meta.object_name
            )
        if history_obj.history_type == "-":
            raise self.instance.DoesNotExist(
                "%s had already been deleted." % self.instance._meta.object_name
            )
        result = history_obj.instance
        historic = getattr(result, SIMPLE_HISTORY_REVERSE_ATTR_NAME)
        setattr(historic, "_as_of", date)
        return result

    def effective_as_of(self, business_date, system_date=None):
        """
        Reproduce a warehouse export for business day ``business_date`` as the
        data stood at system cutoff ``system_date``.

        Every historical record whose entry date (``history_date``) is no later
        than ``system_date`` and whose business effective date (defaulting to
        the entry date for legacy rows) is no later than ``business_date`` is a
        candidate; the record with the greatest business effective date per
        primary key wins, ties resolved by entry date / ``history_id``.
        Deletion records drop the object from the snapshot.

        ``system_date`` defaults to "now", i.e. everything known so far applied
        back onto the original business day.
        """
        self._require_effective_dates()
        if system_date is None:
            system_date = timezone.now()

        queryset = (
            self.get_queryset()
            .filter(history_date__lte=system_date)
            .effective_dated()
        )
        effective_date = queryset._effective_date_expression()
        queryset = queryset.annotate(_effective_at=effective_date).filter(
            _effective_at__lte=business_date
        )
        if not self.instance:
            queryset._as_of = system_date
            return queryset.latest_of_each().as_instances()

        winner = queryset.latest_of_each().first()
        if winner is None:
            raise self.instance.DoesNotExist(
                "%s had no effective record as of %s."
                % (self.instance._meta.object_name, business_date)
            )
        if winner.history_type == "-":
            raise self.instance.DoesNotExist(
                "%s had already been deleted." % self.instance._meta.object_name
            )
        result = winner.instance
        historic = getattr(result, SIMPLE_HISTORY_REVERSE_ATTR_NAME)
        setattr(historic, "_as_of", system_date)
        return result

    def state_events(self, business_date, system_date=None):
        """
        Return the status/state events in business-timeline order for records
        effective on ``business_date`` and known at system cutoff
        ``system_date``.

        Each event is a dict containing the tracked object's primary key
        (``pk``), the winning historical record (``record``) and the derived
        instance (``instance``; ``None`` when the winning record is a deletion).
        Records that a later correction withdrew never surface because the
        correction replaces them on the business timeline.

        This is a thin, ordered convenience wrapper around
        :meth:`effective_as_of` that also exposes the historical record each
        event was derived from.
        """
        self._require_effective_dates()
        if system_date is None:
            system_date = timezone.now()

        queryset = (
            self.get_queryset()
            .filter(history_date__lte=system_date)
            .effective_dated()
        )
        effective_date = queryset._effective_date_expression()
        winners = (
            queryset.annotate(_effective_at=effective_date)
            .filter(_effective_at__lte=business_date)
            .latest_of_each()
            .order_by("_effective_at", "history_date", "history_id")
        )
        pk_attr = self.model.instance_type._meta.pk.attname
        events = []
        for record in winners:
            events.append(
                {
                    "pk": getattr(record, pk_attr),
                    "record": record,
                    "instance": None
                    if record.history_type == "-"
                    else record.instance,
                }
            )
        return events

    def replay_batch(self, records, batch_id=None, *, using=None):
        """
        Re-enter a backfill batch of historical records idempotently.

        ``records`` are unsaved historical model instances (for example built by
        replaying an export file through the same code path that created them).
        Every record is stamped with ``batch_id`` (when the model has a batch
        identifier field) and any record whose natural key - batch identifier
        plus all tracked field values and history metadata - already exists is
        skipped instead of being inserted again. Replaying the same batch
        therefore changes nothing and produces no duplicate state events.

        Returns ``(created, skipped)`` counts. The original records and any
        correction to them are always preserved: replay only inserts rows that
        are genuinely missing.
        """
        if not records:
            return 0, 0

        batch_field = self._batch_id_field
        self._stamp_batch_id(records, batch_id, batch_field)

        natural_key_fields = self._natural_key_fields()
        existing_keys = self._existing_natural_keys(
            batch_id, batch_field, natural_key_fields
        )

        to_create = []
        skipped = 0
        for record in records:
            key = self._natural_key_of(record, natural_key_fields)
            if key in existing_keys:
                skipped += 1
                continue
            existing_keys.add(key)
            to_create.append(record)

        created = len(
            self.model.objects.using(using).bulk_create(to_create)
            if to_create
            else []
        )
        return created, skipped

    def _stamp_batch_id(self, records, batch_id, batch_field):
        if batch_id is None:
            return
        if not batch_field:
            raise MissingBatchIdFieldError(
                f"{self.model.__name__} has no batch identifier field; "
                "enable it with HistoricalRecords(history_batch_id_field=...)."
            )
        for record in records:
            setattr(record, batch_field, batch_id)

    def _existing_natural_keys(self, batch_id, batch_field, natural_key_fields):
        existing = self.get_queryset()
        if batch_field and batch_id is not None:
            existing = existing.filter(**{batch_field: batch_id})
        if len(natural_key_fields) == 1:
            return set(existing.values_list(*natural_key_fields, flat=True))
        return set(existing.values_list(*natural_key_fields))

    @staticmethod
    def _natural_key_of(record, natural_key_fields):
        key = tuple(getattr(record, field) for field in natural_key_fields)
        return key[0] if len(key) == 1 else key

    def _natural_key_fields(self):
        """
        Fields that uniquely identify a record within a replayed batch. The
        batch identifier (when present) plus every column the warehouse stores,
        which makes replay byte-for-byte idempotent without relying on the
        surrogate ``history_id``.
        """
        fields = []
        if self._batch_id_field:
            fields.append(self._batch_id_field)
        fields.extend(
            field.attname
            for field in self.model._meta.fields
            if not field.primary_key and field.attname != self._batch_id_field
        )
        return fields

    def bulk_history_create(
        self,
        objs,
        batch_size=None,
        update=False,
        default_user=None,
        default_change_reason="",
        default_date=None,
        custom_historical_attrs=None,
    ):
        """
        Bulk create the history for the objects specified by objs.
        If called by bulk_update_with_history, use the update boolean and
        save the history_type accordingly.
        """
        if not getattr(settings, "SIMPLE_HISTORY_ENABLED", True):
            return

        history_type = "+"
        if update:
            history_type = "~"

        historical_instances = []
        for instance in objs:
            history_user = getattr(
                instance,
                "_history_user",
                default_user or self.model.get_default_history_user(instance),
            )
            row = self.model(
                history_date=getattr(
                    instance, "_history_date", default_date or timezone.now()
                ),
                history_user=history_user,
                history_change_reason=get_change_reason_from_object(instance)
                or default_change_reason,
                history_type=history_type,
                **{
                    field.attname: getattr(instance, field.attname)
                    for field in self.model.tracked_fields
                },
                **(custom_historical_attrs or {}),
            )
            if hasattr(self.model, "history_relation"):
                row.history_relation_id = instance.pk
            historical_instances.append(row)

        return self.model.objects.bulk_create(
            historical_instances, batch_size=batch_size
        )


class HistoryDescriptor:
    def __init__(self, model, manager=HistoryManager, queryset=HistoricalQuerySet):
        self.model = model
        self.queryset_class = queryset
        self.manager_class = manager

    def __get__(self, instance, owner):
        return self.manager_class.from_queryset(self.queryset_class)(
            self.model, instance
        )

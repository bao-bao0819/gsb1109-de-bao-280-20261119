"""
Previewed, transactional recovery of a history-tracked object together with
its history-tracked related objects.

The flow supported by this module is:

1. Build a :class:`RecoveryPreview` from a historical record.  The preview
   describes which scalar values of the main object would change, and for
   every history-tracked related association that existed at the time of the
   historical record, whether that association can still be recovered.
2. Persist the preview (e.g. in the session) and let a user explicitly choose
   between a full recovery and a partial recovery.
3. Re-validate the preview against the current database state and apply the
   chosen recovery inside a single atomic block, so that a failure never
   leaves partially restored scalars or quotas behind.

Related objects are identified by their primary key, but their identity is
additionally verified through a stable business field (see
:func:`get_identity_field`): a different row that merely reused the same
database primary key must not inherit the historical association.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from simple_history.utils import get_history_manager_for_model

RECOVERY_MODE_FULL = "full"
RECOVERY_MODE_PARTIAL = "partial"

RECOVERY_PREVIEW_SESSION_KEY = "_simple_history_recovery_previews"
RECOVERY_PREVIEW_TTL = timedelta(minutes=30)


class RecoveryItemStatus:
    # The association is still present and its related objects are the same
    # business entities as in the historical record.
    RECOVERABLE = "recoverable"
    # The association already matches the historical state.
    CURRENT = "current"
    # The association (or one of its related objects) can no longer be
    # identified, so the historical state cannot be restored for it.
    UNRECOVERABLE = "unrecoverable"


class RecoveryError(Exception):
    """Base class for recovery failures."""


class StaleRecoveryPreview(RecoveryError):
    """Raised when a preview no longer matches the current database state."""


class UnrecoverableRelationsError(RecoveryError):
    """Raised when a full recovery is requested but items are unrecoverable."""

    def __init__(self, items):
        self.items = items
        super().__init__(
            "The following historical associations can no longer be recovered: "
            + ", ".join(str(item) for item in items)
        )


@dataclass
class ScalarChange:
    field: str
    current: Any
    historical: Any

    def to_session_data(self):
        return {
            "field": self.field,
            "current": self.current,
            "historical": self.historical,
        }

    @classmethod
    def from_session_data(cls, data):
        return cls(
            field=data["field"],
            current=data["current"],
            historical=data["historical"],
        )


@dataclass
class RecoveryItem:
    related_model_label: str
    related_name: str
    historical_pk: Any
    current_pk: Any | None
    status: str
    reason: str = ""
    field_changes: list[ScalarChange] = field(default_factory=list)
    identity_field: str | None = None
    historical_identity: Any = None
    current_identity: Any = None
    historical_label: str = ""
    current_label: str = ""
    # Transient (not serialized) live model instances used while applying a
    # recovery in the same request.
    historical: Any = None
    current: Any = None

    @property
    def is_recoverable(self):
        return self.status in (
            RecoveryItemStatus.RECOVERABLE,
            RecoveryItemStatus.CURRENT,
        )

    @property
    def has_changes(self):
        return bool(self.field_changes)

    def to_session_data(self):
        return {
            "related_model_label": self.related_model_label,
            "related_name": self.related_name,
            "historical_pk": self.historical_pk,
            "current_pk": self.current_pk,
            "status": self.status,
            "reason": self.reason,
            "field_changes": [
                change.to_session_data() for change in self.field_changes
            ],
            "identity_field": self.identity_field,
            "historical_identity": self.historical_identity,
            "current_identity": self.current_identity,
            "historical_label": self.historical_label,
            "current_label": self.current_label,
        }

    @classmethod
    def from_session_data(cls, data):
        return cls(
            related_model_label=data["related_model_label"],
            related_name=data["related_name"],
            historical_pk=data["historical_pk"],
            current_pk=data["current_pk"],
            status=data["status"],
            reason=data["reason"],
            field_changes=[
                ScalarChange.from_session_data(change)
                for change in data["field_changes"]
            ],
            identity_field=data["identity_field"],
            historical_identity=data["historical_identity"],
            current_identity=data["current_identity"],
            historical_label=data["historical_label"],
            current_label=data["current_label"],
        )

    def __str__(self):
        return f"{self.related_model_label}(pk={self.historical_pk})"

    @property
    def model(self):
        app_label, model_name = self.related_model_label.split(".")
        return apps.get_model(app_label, model_name)


@dataclass
class RecoveryPreview:
    app_label: str
    model_name: str
    object_id: Any
    history_id: Any
    history_date: datetime
    scalar_changes: list[ScalarChange]
    items: list[RecoveryItem]
    fingerprint: str
    token: str | None = None
    created_at: datetime | None = None

    @property
    def model(self):
        return apps.get_model(self.app_label, self.model_name)

    @property
    def unrecoverable_items(self):
        return [
            item
            for item in self.items
            if item.status == RecoveryItemStatus.UNRECOVERABLE
        ]

    @property
    def recoverable_items(self):
        return [item for item in self.items if item.is_recoverable]

    @property
    def changed_items(self):
        return [item for item in self.recoverable_items if item.has_changes]

    def to_session_data(self):
        return {
            "app_label": self.app_label,
            "model_name": self.model_name,
            "object_id": self.object_id,
            "history_id": self.history_id,
            "history_date": self.history_date.isoformat(),
            "scalar_changes": [
                change.to_session_data() for change in self.scalar_changes
            ],
            "items": [item.to_session_data() for item in self.items],
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_session_data(cls, token, data, created_at):
        return cls(
            app_label=data["app_label"],
            model_name=data["model_name"],
            object_id=data["object_id"],
            history_id=data["history_id"],
            history_date=datetime.fromisoformat(data["history_date"]),
            scalar_changes=[
                ScalarChange.from_session_data(change)
                for change in data["scalar_changes"]
            ],
            items=[
                RecoveryItem.from_session_data(item) for item in data["items"]
            ],
            fingerprint=data["fingerprint"],
            token=token,
            created_at=created_at,
        )


@dataclass
class RecoveryResult:
    mode: str
    scalar_changes: list[ScalarChange]
    applied_items: list[RecoveryItem]
    skipped_items: list[RecoveryItem]
    config_history_id: Any
    item_history_ids: dict


def is_history_tracked(model) -> bool:
    manager_attribute = getattr(
        model._meta, "simple_history_manager_attribute", None
    )
    return bool(manager_attribute) and hasattr(model, manager_attribute)


def get_identity_field(model, identity_fields=None) -> str | None:
    """
    Return the name of the stable business field used to verify the identity
    of ``model``.

    Explicit mappings take precedence.  Otherwise the first unique non-primary
    field is used.  When the model has no such field, ``None`` is returned and
    identity can only be verified through the primary key.
    """
    identity_fields = identity_fields or {}
    label = model._meta.label_lower
    if label in identity_fields:
        return identity_fields[label]
    for field_instance in model._meta.get_fields():
        if (
            getattr(field_instance, "unique", False)
            and not field_instance.primary_key
            and not field_instance.is_relation
        ):
            return field_instance.name
    return None


def _concrete_fields(model):
    return [
        field_instance
        for field_instance in model._meta.concrete_fields
        if not field_instance.primary_key
    ]


def _scalar_fields(model):
    return [
        field_instance
        for field_instance in _concrete_fields(model)
        if not field_instance.is_relation
    ]


def _foreign_key_fields(model):
    return [
        field_instance
        for field_instance in _concrete_fields(model)
        if field_instance.is_relation
        and field_instance.many_to_one
    ]


def _related_specs(model):
    """Return reverse (FK / one-to-one) relations to history-tracked models."""
    specs = []
    for relation in model._meta.related_objects:
        if not (relation.one_to_many or relation.one_to_one):
            continue
        related_model = relation.related_model
        if not is_history_tracked(related_model):
            continue
        specs.append(
            RecoveryRelatedSpec(
                related_model=related_model,
                related_name=relation.get_accessor_name(),
                parent_field_attname=relation.field.attname,
            )
        )
    return specs


@dataclass
class RecoveryRelatedSpec:
    related_model: Any
    related_name: str
    parent_field_attname: str


def _value_or_none(instance, attname):
    if instance is None:
        return None
    return getattr(instance, attname)


def build_recovery_preview(
    history_record, identity_fields=None
) -> RecoveryPreview:
    """Build a :class:`RecoveryPreview` for ``history_record``.

    Related associations are evaluated with ``as_of()`` at the historical
    record's capture time, i.e. the recovery restores the related state exactly
    as it existed when this version was recorded.
    """
    model = history_record.instance_type
    opts = model._meta
    object_id = getattr(history_record, opts.pk.attname)
    as_of = history_record.history_date
    historical_object = history_record.instance

    try:
        current_object = model._default_manager.get(pk=object_id)
    except model.DoesNotExist:
        current_object = None

    scalar_changes = [
        ScalarChange(
            field=field_instance.name,
            current=_value_or_none(current_object, field_instance.attname),
            historical=getattr(historical_object, field_instance.attname),
        )
        for field_instance in _concrete_fields(model)
        if _value_or_none(current_object, field_instance.attname)
        != getattr(historical_object, field_instance.attname)
    ]

    items = []
    for spec in _related_specs(model):
        historical_rows = (
            get_history_manager_for_model(spec.related_model)
            .as_of(as_of)
            .filter(**{spec.parent_field_attname: object_id})
        )
        current_rows = {
            row.pk: row
            for row in spec.related_model._default_manager.filter(
                **{spec.parent_field_attname: object_id}
            )
        }
        for historical_row in historical_rows:
            items.append(
                _build_recovery_item(
                    spec,
                    historical_row,
                    current_rows.get(historical_row.pk),
                    as_of,
                    identity_fields,
                )
            )

    fingerprint = _compute_fingerprint(model, object_id)
    return RecoveryPreview(
        app_label=opts.app_label,
        model_name=opts.model_name,
        object_id=object_id,
        history_id=history_record.history_id,
        history_date=history_record.history_date,
        scalar_changes=scalar_changes,
        items=items,
        fingerprint=fingerprint,
    )


def _build_recovery_item(
    spec, historical_row, current_row, as_of, identity_fields
):
    related_model = spec.related_model
    item = RecoveryItem(
        related_model_label=related_model._meta.label_lower,
        related_name=spec.related_name,
        historical_pk=historical_row.pk,
        current_pk=current_row.pk if current_row is not None else None,
        status=RecoveryItemStatus.RECOVERABLE,
        historical=historical_row,
        current=current_row,
        historical_label=str(historical_row),
        current_label=str(current_row) if current_row is not None else "",
    )

    if current_row is None:
        item.status = RecoveryItemStatus.UNRECOVERABLE
        item.reason = "The historical association no longer exists."
        return item

    for fk_field in _foreign_key_fields(related_model):
        if fk_field.attname == spec.parent_field_attname:
            continue
        reason = _verify_foreign_key(
            item, fk_field, historical_row, current_row, as_of, identity_fields
        )
        if reason is not None:
            item.status = RecoveryItemStatus.UNRECOVERABLE
            item.reason = reason
            return item

    item.field_changes = [
        ScalarChange(
            field=scalar_field.name,
            current=getattr(current_row, scalar_field.attname),
            historical=getattr(historical_row, scalar_field.attname),
        )
        for scalar_field in _scalar_fields(related_model)
        if getattr(current_row, scalar_field.attname)
        != getattr(historical_row, scalar_field.attname)
    ]
    if not item.field_changes:
        item.status = RecoveryItemStatus.CURRENT
    return item


def _verify_foreign_key(
    item, fk_field, historical_row, current_row, as_of, identity_fields
):
    target_model = fk_field.related_model
    historical_id = getattr(historical_row, fk_field.attname)
    current_id = getattr(current_row, fk_field.attname)

    if historical_id is None:
        return None
    if current_id is None:
        return (
            f"The related {target_model._meta.verbose_name} "
            f"(pk={historical_id}) was deleted."
        )
    if current_id != historical_id:
        return (
            f"The {fk_field.name} now points to a different "
            f"{target_model._meta.verbose_name} (pk={current_id} instead of "
            f"pk={historical_id})."
        )

    if not is_history_tracked(target_model):
        return None

    historical_target = (
        get_history_manager_for_model(target_model)
        .as_of(as_of)
        .filter(pk=historical_id)
        .first()
    )
    if historical_target is None:
        return (
            f"The related {target_model._meta.verbose_name} "
            f"(pk={historical_id}) did not exist at the time of the historical "
            "record."
        )

    identity_field = get_identity_field(target_model, identity_fields)
    item.identity_field = identity_field
    if identity_field is None:
        item.historical_identity = historical_target.pk
        item.current_identity = current_id
        return None

    historical_identity = getattr(historical_target, identity_field)
    current_target = getattr(current_row, fk_field.name)
    current_identity = getattr(current_target, identity_field)
    item.historical_identity = historical_identity
    item.current_identity = current_identity
    if historical_identity != current_identity:
        return (
            f"The {target_model._meta.verbose_name} with pk={historical_id} is a "
            f"different object now: its {identity_field} changed from "
            f"{historical_identity!r} to {current_identity!r}."
        )
    return None


def _compute_fingerprint(model, object_id) -> str:
    """Hash the current state of the object and its tracked associations."""
    hasher = hashlib.sha256()
    try:
        current_object = model._default_manager.get(pk=object_id)
    except model.DoesNotExist:
        hasher.update(b"deleted")
        current_object = None
    else:
        for field_instance in _concrete_fields(model):
            hasher.update(
                repr(
                    (
                        field_instance.attname,
                        getattr(current_object, field_instance.attname),
                    )
                ).encode()
            )

    for spec in _related_specs(model):
        rows = spec.related_model._default_manager.filter(
            **{spec.parent_field_attname: object_id}
        ).order_by("pk")
        for row in rows:
            values = tuple(
                (field_instance.attname, getattr(row, field_instance.attname))
                for field_instance in _concrete_fields(spec.related_model)
            )
            hasher.update(repr((spec.related_name, row.pk, values)).encode())
    return hasher.hexdigest()


def apply_recovery(
    preview,
    mode,
    *,
    history_user=None,
    change_reason=None,
    identity_fields=None,
) -> RecoveryResult:
    """
    Apply ``preview`` in a single atomic block.

    The preview is rebuilt from the historical record so that every decision
    reflects the current database state.

    A full recovery requires that nothing changed since the preview was taken
    (fingerprint check) and that every historical association is still
    recoverable; otherwise nothing is written.  A partial recovery tolerates
    later changes: it restores the main object's scalars and only those
    associations that are still identifiable, and reports the associations
    that could not be recovered.
    """
    if mode not in (RECOVERY_MODE_FULL, RECOVERY_MODE_PARTIAL):
        raise ValueError(f"Unknown recovery mode: {mode!r}")

    model = preview.model
    history_manager = get_history_manager_for_model(model)
    history_record = history_manager.get(pk=preview.history_id)
    live_preview = build_recovery_preview(history_record, identity_fields)

    if mode == RECOVERY_MODE_FULL:
        _validate_full_recovery(preview, live_preview)

    historical_object = history_record.instance
    skipped_items = list(live_preview.unrecoverable_items)
    applied_items = []
    applied_scalar_changes = []

    with transaction.atomic():
        current_object = model._default_manager.get(pk=preview.object_id)
        applied_scalar_changes = _restore_scalars(
            current_object, historical_object, model, history_user, change_reason
        )
        item_history_ids = {}
        for item in live_preview.items:
            if not item.is_recoverable or not item.has_changes:
                continue
            item_history_ids[item.historical_pk] = _restore_item(
                item, history_user, change_reason
            )
            applied_items.append(item)

    # Re-query after the transaction has committed, so that the returned
    # versions describe the final state and never an intermediate write (e.g.
    # a restored quota before the remaining writes finished).
    config_history_id = _latest_history_id(model, preview.object_id)
    return RecoveryResult(
        mode=mode,
        scalar_changes=applied_scalar_changes,
        applied_items=applied_items,
        skipped_items=skipped_items,
        config_history_id=config_history_id,
        item_history_ids=item_history_ids,
    )


def _validate_full_recovery(stored_preview, live_preview):
    if live_preview.fingerprint != stored_preview.fingerprint:
        raise StaleRecoveryPreview(
            "The recovery preview is outdated: the current data changed "
            "after the preview was created."
        )
    if live_preview.unrecoverable_items:
        raise UnrecoverableRelationsError(live_preview.unrecoverable_items)


def _restore_scalars(
    current_object, historical_object, model, history_user, change_reason
):
    _prepare_history_metadata(current_object, history_user, change_reason)
    changes = []
    updated_fields = []
    for field_instance in _concrete_fields(model):
        historical_value = getattr(historical_object, field_instance.attname)
        current_value = getattr(current_object, field_instance.attname)
        if current_value != historical_value:
            setattr(current_object, field_instance.attname, historical_value)
            updated_fields.append(field_instance.attname)
            changes.append(
                ScalarChange(
                    field=field_instance.name,
                    current=current_value,
                    historical=historical_value,
                )
            )
    if updated_fields:
        current_object.save(update_fields=updated_fields)
    return changes


def _restore_item(item, history_user, change_reason):
    related_model = item.model
    current_row = related_model._default_manager.get(pk=item.historical_pk)
    _prepare_history_metadata(current_row, history_user, change_reason)
    updated_fields = []
    for change in item.field_changes:
        field_instance = related_model._meta.get_field(change.field)
        setattr(
            current_row,
            field_instance.attname,
            getattr(item.historical, field_instance.attname),
        )
        updated_fields.append(field_instance.attname)
    current_row.save(update_fields=updated_fields)
    return _latest_history_id(related_model, item.historical_pk)


def _latest_history_id(model, object_id):
    return (
        get_history_manager_for_model(model)
        .filter(**{model._meta.pk.attname: object_id})
        .first()
        .history_id
    )


def _prepare_history_metadata(instance, history_user, change_reason):
    if history_user is not None:
        instance._history_user = history_user
    if change_reason is not None:
        instance._change_reason = change_reason


class RecoveryPreviewStore:
    """Stores recovery previews in the Django session, keyed by opaque tokens."""

    def __init__(self, request):
        self.request = request

    def store(self, preview: RecoveryPreview) -> str:
        token = secrets.token_hex(16)
        previews = self.request.session.get(RECOVERY_PREVIEW_SESSION_KEY, {})
        self._prune(previews)
        previews[token] = {
            "data": preview.to_session_data(),
            "created_at": timezone.now().isoformat(),
        }
        self.request.session[RECOVERY_PREVIEW_SESSION_KEY] = previews
        return token

    def _prune(self, previews):
        now = timezone.now()
        for token in list(previews):
            created_at = datetime.fromisoformat(previews[token]["created_at"])
            if now - created_at > RECOVERY_PREVIEW_TTL:
                previews.pop(token)

    def get(self, token: str) -> RecoveryPreview | None:
        if not token:
            return None
        previews = self.request.session.get(RECOVERY_PREVIEW_SESSION_KEY, {})
        entry = previews.get(token)
        if entry is None:
            return None
        created_at = datetime.fromisoformat(entry["created_at"])
        if timezone.now() - created_at > RECOVERY_PREVIEW_TTL:
            self.delete(token)
            return None
        return RecoveryPreview.from_session_data(token, entry["data"], created_at)

    def delete(self, token: str) -> None:
        previews = self.request.session.get(RECOVERY_PREVIEW_SESSION_KEY, {})
        previews.pop(token, None)
        self.request.session[RECOVERY_PREVIEW_SESSION_KEY] = previews

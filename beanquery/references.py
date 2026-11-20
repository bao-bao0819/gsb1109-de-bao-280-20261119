"""Stable references for postings, derived from ledger metadata.

A reference identifies a posting by the source-provided institution,
transaction number, and leg number.  It intentionally does not use the
account, the amount, the posting order, or the file location, so that
editing an amount, reordering the posting lines, or moving a transaction
to another ledger file does not change it.

The institution is part of the reference namespace: the same external
transaction number may legitimately appear at two institutions.

A reference is only produced when the transaction key and the leg key
are unique.  Ambiguity is reported at the level at which it occurs
(``transaction`` or ``leg``) rather than resolved by picking a record by
line number, amount, or iteration order.
"""

from __future__ import annotations

import collections
import dataclasses
import urllib.parse

from typing import Iterable, Optional

from beancount.core import data


TRANSACTION_CONFLICT = 'transaction'
LEG_CONFLICT = 'leg'

STATUS_OK = 'ok'
STATUS_MISSING = 'missing'
STATUS_TRANSACTION_CONFLICT = 'transaction-conflict'
STATUS_LEG_CONFLICT = 'leg-conflict'


def status(info):
    """Describe a reference as usable, missing, or conflicting at a level."""
    if info.reference is not None:
        return STATUS_OK
    if info.conflict == TRANSACTION_CONFLICT:
        return STATUS_TRANSACTION_CONFLICT
    if info.conflict == LEG_CONFLICT:
        return STATUS_LEG_CONFLICT
    return STATUS_MISSING


@dataclasses.dataclass(frozen=True)
class ReferenceFields:
    """Names of the ledger metadata fields holding the source identifiers.

    The institution and transaction fields are looked up on the posting
    first and on the transaction as a fallback, covering sources that
    attach the identifiers at either level.  The leg field is looked up
    on the posting only: a leg number shared by every posting of a
    transaction would not identify a leg.
    """

    institution: Optional[str] = None
    transaction: Optional[str] = None
    leg: Optional[str] = None

    @property
    def complete(self):
        return bool(self.institution and self.transaction and self.leg)


@dataclasses.dataclass(frozen=True)
class ReferenceInfo:
    """The reference of one posting and the reason it may be unavailable."""

    reference: Optional[str]
    institution: Optional[str]
    transaction: Optional[str]
    leg: Optional[str]
    conflict: Optional[str]


def _component(value):
    """Turn a metadata value into a reference component.

    Empty strings and missing values are absent identifiers.  Other
    values are converted without interpreting them as line numbers or
    amounts.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    return str(value)


def _meta_value(meta, name):
    """Return a component for ``name`` in ``meta``, or None if absent/empty."""
    if not name or not meta or name not in meta:
        return None
    return _component(meta[name])


def _field(entry, posting, name):
    """Look up a declared field on the posting, then on the transaction."""
    if not name:
        return None
    value = _meta_value(getattr(posting, 'meta', None), name)
    if value is not None:
        return value
    return _meta_value(getattr(entry, 'meta', None), name)


def _posting_field(posting, name):
    """Look up a per-posting field without falling back to the transaction.

    The leg number identifies one posting; inheriting a single value from
    the transaction would manufacture identical leg numbers rather than
    report that the legs are unidentified.
    """
    if not name:
        return None
    return _meta_value(getattr(posting, 'meta', None), name)


def _quote(value):
    return urllib.parse.quote(value, safe='')


def make_reference(institution, transaction, leg):
    """Encode the three identifier components unambiguously."""
    return ':'.join(_quote(component) for component in (institution, transaction, leg))


class PostingReferenceIndex:
    """Count transaction and leg keys to detect genuine ambiguity."""

    def __init__(self, entries: Optional[Iterable[data.Directive]], fields: ReferenceFields):
        self.fields = fields
        self._transactions = collections.Counter()
        self._legs = collections.Counter()
        if entries is not None and fields.complete:
            self._build(entries)

    def _keys(self, entry, posting):
        fields = self.fields
        institution = _field(entry, posting, fields.institution)
        transaction = _field(entry, posting, fields.transaction)
        leg = _posting_field(posting, fields.leg)
        return institution, transaction, leg

    def _build(self, entries):
        for entry in entries:
            if not isinstance(entry, data.Transaction):
                continue
            transaction_keys = set()
            for posting in entry.postings:
                institution, transaction, leg = self._keys(entry, posting)
                if institution is None or transaction is None:
                    continue
                key = (institution, transaction)
                transaction_keys.add(key)
                if leg is not None:
                    self._legs[(institution, transaction, leg)] += 1
            # Count every transaction record once per key it carries.
            for key in transaction_keys:
                self._transactions[key] += 1

    def info(self, entry, posting) -> ReferenceInfo:
        institution, transaction, leg = self._keys(entry, posting)

        if not self.fields.complete or institution is None or transaction is None:
            return ReferenceInfo(None, institution, transaction, leg, None)

        if self._transactions[(institution, transaction)] > 1:
            # The same institution and external transaction number name
            # more than one transaction record.  No leg-level choice can
            # repair that ambiguity.
            return ReferenceInfo(None, institution, transaction, leg, TRANSACTION_CONFLICT)

        if leg is None:
            return ReferenceInfo(None, institution, transaction, None, None)

        if self._legs[(institution, transaction, leg)] > 1:
            # The transaction is unique, but this leg number names more
            # than one posting in it.  Other legs of the transaction
            # remain referenceable.
            return ReferenceInfo(None, institution, transaction, leg, LEG_CONFLICT)

        return ReferenceInfo(make_reference(institution, transaction, leg),
                             institution, transaction, leg, None)


def fields_from_params(params, *, institution_field=None, transaction_field=None, leg_field=None):
    """Build a ReferenceFields from explicit arguments and DSN parameters."""
    return ReferenceFields(
        institution=institution_field or params.get('institution_field') or params.get('institution'),
        transaction=transaction_field or params.get('transaction_field') or params.get('transaction'),
        leg=leg_field or params.get('leg_field') or params.get('leg'),
    )

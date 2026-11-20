Audit download
==============

When history is exported for audit, consecutive historical records that only
differ by their remark (``history_change_reason``) are usually noise.  The
helpers in :mod:`simple_history.audit` fold such records into *segments*:
maximal runs of records that share the same price and status.

The helpers are read-only and opt-in.  They never modify the historical table,
so the raw history stays fully queryable through the regular history manager
and existing downloads that do not use segmented export keep working unchanged.

Ordering
--------

Records are always read in ascending ``history_date`` and then ascending
``history_id`` order.  The secondary ``history_id`` key preserves the version
order of records that share an identical timestamp.

.. code-block:: python

    from simple_history.audit import (
        audit_history_rows,
        audit_segments_for_object,
    )

    rows = audit_history_rows(Product, product_id)
    segments = audit_segments_for_object(Product, product_id)

The field names used for folding default to ``price`` and ``status`` and can be
overridden with the ``price_field`` and ``status_field`` keyword arguments.

Segment rules
-------------

* Adjacent records with equal price and status are folded into one segment; a
  remark change only updates the segment's first/last remark evidence.
* A deletion (``history_type == "-"``) always ends the running segment and
  stands alone, so a delete followed by a rebuild never rejoins the earlier
  segment even when the rebuilt row has the identical price and status.
* Two runs that share price and status but are separated by another segment are
  never merged.

Each :class:`~simple_history.audit.AuditSegment` carries its start/end dates,
the first/last ``history_id``, the number of folded raw rows
(``history_count``) and the first/last remarks.  ``endpoint_evidence()``
returns the boundary evidence used to compare two reports.

Sharded export
--------------

A warehouse may export one object's history in several shards.  Folding is
applied inside each shard with :func:`~simple_history.audit.build_audit_shards`,
and :class:`~simple_history.audit.AuditReportReceiver` reconstructs the report
by folding across shard seams.  Because the seam is folded with exactly the same
rule as raw rows, a shard boundary cannot change the resulting segments:

.. code-block:: python

    from simple_history.audit import AuditReportReceiver, build_audit_shards

    shards = build_audit_shards(
        rows,
        shard_sizes=[4, 2],
        identifier_prefix=f"product-{product_id}",
    )

    receiver = AuditReportReceiver()
    for shard in shards:
        receiver.apply_shard(shard)
    segments = receiver.segments()

Corrections and redelivery
--------------------------

Each shard has a stable ``identifier`` built from the prefix, shard position and
revision.

* Re-applying the same identifier is a no-op, so redelivering a shard never
  adds history rows (``apply_shard`` returns ``False``).
* A corrected shard is exported with a higher ``revision``; its ``supersedes``
  points at the previous revision, which the receiver discards.  Every other
  shard and its unaffected segments are retained without being re-sent.
* A late back-filled record therefore only requires replaying the affected
  shard; the reconstructed segments still match a one-shot export of the same
  rows.

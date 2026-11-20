Exporting status changes
========================

``simple_history.export`` turns the historical rows of a model into *logical*
status changes for one calendar month and supports resumable, batched reads.

Given a history-tracked model with a status field (for example a
``Customer`` model with ``status`` and ``HistoricalRecords()``), create an
exporter for that model:

.. code-block:: python

    from datetime import date

    from simple_history.export import CustomerStatusExporter

    exporter = CustomerStatusExporter(Customer)
    events, next_cursor = exporter.read_batch(date(2021, 2, 1), limit=100)

Each event is a ``CustomerStatusChange`` carrying the object id, the old and
new status, the historical row id and date, the history type, the history
user (id and object) and the change reason.

Only rows where the status actually differs from the previous known status
produce an event. A row that only changes another field (for example a
remark or note) is skipped, and rows before the month only establish the
baseline status; they are never reported as changes of the month.

Rows are read globally in ``(history_date, history_id)`` order, so rows that
share one timestamp - such as the records written by a single batch import -
are delivered in historical-row order.

Resuming and replaying
----------------------

The cursor returned with every batch points at the last delivered
historical row and can be serialized for later use:

.. code-block:: python

    from simple_history.export import CustomerStatusExportCursor

    token = next_cursor.encode()
    cursor = CustomerStatusExportCursor.decode(token)
    more_events, next_cursor = exporter.read_batch(
        date(2021, 2, 1), after=cursor
    )

Because the cursor stores both ``history_date`` and ``history_id``, it can
resume inside a group of rows that share the same timestamp instead of
restarting at the whole timestamp. A cursor may only be used with the month
it belongs to.

Reading again with the same cursor replays the same events and never creates
an additional logical event. When a month is fully consumed,
``read_batch()`` returns ``next_cursor=None``.

For streaming use cases, ``stream_status_changes()`` yields the same events
as an iterator without materializing a batch.

Deletions and re-created objects
--------------------------------

A deletion followed by a creation of an object with the same primary key is
treated as two separate existence periods. The re-created object starts with
``old_status=None`` and does not inherit the status the object had before it
was deleted.

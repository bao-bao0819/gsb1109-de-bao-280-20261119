Version 0.1 (unreleased)
------------------------

- Added the ``DISPLAY`` (or ``FILTER``) clause to ``SELECT`` queries. The
  clause takes a predicate that selects the rows to output without
  affecting the rows that contribute to the ``balance`` column or to the
  aggregations. This allows a report to show only a subset of the postings
  of a period while the running balances keep following the ledger. The
  predicate is evaluated after the ``WHERE`` clause and before ``ORDER
  BY``. In an aggregate query it must be an aggregate expression, like
  the ``HAVING`` clause::

    SELECT date, narration, balance
    FROM OPEN ON 2025-05-01 CLOSE ON 2025-06-01
    WHERE account = 'Assets:A'
    DISPLAY flag = '*'

- Added the ``OPENING`` statement to retrieve the balances at the opening
  date of the ``FROM`` clause. It is equivalent to ``BALANCES`` restricted
  to the opening balances synthesized by the ``OPEN`` clause, which makes
  the opening balance available even when the report over the period has
  no visible rows::

    OPENING AT cost FROM OPEN ON 2025-05-01 CLOSE ON 2025-06-01

- The ``HAVING`` clause for aggregate queries is now supported.

- The ``empty()`` BQL function to determine whether an Inventory
  object as returned by the ``sum()`` aggregate function is empty has
  been added.

- Added the ``round()`` BQL function.

- ``NULL`` values in ``SORT BY`` clause are now always considered to
  be smaller than any other values.  This may results in rows to be
  returned in a slightly different order.

- It is now possible to specify the direction of the ordering for each
  column in the ``SORT BY`` clause.  This brings BQL closer to SQL
  specification but queries written with the old behaviour in mind
  will return rows in a different order.  The query::

    SELECT date, narration ORDER BY date, narration DESC

  used to return rows in descending order by both ``date`` and
  ``narration`` while now it would order the rows ascending by
  ``date`` and descending by ``narration``.  To recover the old
  behavior, the query should be written::

    SELECT date, narration ORDER BY date DESC, narration DESC

- Type casting functions ``int()``, ``decimal()``, ``str()``,
  ``date()`` have been added.  These are mostly useful to convert the
  generic ``object`` type returned by the metadata retrieval functions
  but can also be used to convert between types.  If the conversion
  fails, ``NULL`` is returned.

- The ``str()`` BQL function used to return a string representation of
  its argument using the Python :py:func:`repr()` function.  This
  clashes with the use of ``str()`` as a type casting function.  The
  function is renamed ``repr()``.

- The ``date()`` BQL function used to extract a date from string
  arguments with a very relaxed parser.  This clashes with the use of
  ``date()`` as a type casting function.  The function is renamed
  ``parse_date()``.  Another form of ``parse_date()`` that accepts the
  date format as second argument has been added.

- The ``getitem()`` BQL function return type has been changed from a
  string to a generic ``object`` to match the return type of function
  retrieving entries from metadata dictionaries.  The old behavior can
  be obtained with ``str(getitem(x, key))``.

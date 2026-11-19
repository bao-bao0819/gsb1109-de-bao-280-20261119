beanquery: Customizable lightweight SQL query tool
==================================================

beanquery is a customizable and extensible lightweight SQL query tool
that works on tabular data, including `Beancount`__ ledger data.

__ https://beancount.github.io/

Output formats
--------------

The output format is selected with the ``--format`` command line
option or the ``.set`` shell command (``.set format jsonl``). The
available formats are ``text`` (the default), ``csv``, ``beancount``
and ``jsonl``.

The ``jsonl`` format is meant for programs rather than humans. It
writes one JSON object per result row, using the original query column
names as field names. Each line is a single, self-contained JSON
document and can be consumed independently.

It removes the ambiguities of the text and CSV formats:

* Amounts are split into two fields, ``<column>.number`` and
  ``<column>.currency``, instead of being joined into a single string.
* Numbers, including high precision decimals, are emitted verbatim as
  strings using their canonical representation. Parsing them back with
  ``decimal.Decimal`` reconstructs the exact value, without any
  display-context quantization.
* A missing value (``NULL``), the empty string and an empty inventory
  are represented distinctly as JSON ``null``, ``""`` and ``[]``.
* Positions are objects with ``units`` and ``cost`` members and
  inventories are arrays of such objects, so holdings are never
  squeezed into a single display cell.

For example, the query::

    SELECT narration, position, balance FROM postings

produces one record per row (shown wrapped below for readability; in
the output each record occupies a single line)::

    {"narration": "Invest",
     "position": {"units": {"number": "2", "currency": "RGAGX"},
                  "cost": {"number": "172.55", "currency": "USD",
                           "date": "2013-04-05", "label": null}},
     "balance": [{"units": {"number": "2", "currency": "RGAGX"},
                  "cost": {"number": "172.55", "currency": "USD",
                           "date": "2013-04-05", "label": null}}]}

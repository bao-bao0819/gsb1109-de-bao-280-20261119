`csvw.dsv`
==========

.. automodule:: csvw.dsv
   :members:

Line numbers
------------

Rows in a DSV file need not correspond to single lines: quoted fields may contain line breaks.
:class:`csvw.dsv.UnicodeReaderWithLineNumber` therefore reports the physical line range occupied
by each logical record as a :class:`csvw.dsv.LineNumber` with ``start`` and ``end`` attributes.
These are 1-based line numbers in the source file, unaffected by skipping blank lines, comments or
rows - so a record can be located directly in the text editor.

.. code-block:: python

    >>> from csvw.dsv import UnicodeReaderWithLineNumber, Dialect
    >>> import io
    >>> text = 'ID,note\n1,"first\nsecond"\n\n2,other\n'
    >>> with UnicodeReaderWithLineNumber(
    ...         io.StringIO(text, newline=''),
    ...         dialect=Dialect(skipBlankRows=True)) as reader:
    ...     for lineno, row in reader:
    ...         print(lineno.start, lineno.end)
    1 1
    2 3
    5 5

:class:`csvw.metadata.Table` reports the same ranges when reading rows with
``iterdicts(with_metadata=True)`` and also uses them in error messages for invalid cells.

.. automodule:: csvw.dsv_dialects
   :members:

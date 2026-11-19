beangulp: Importers Framework for Beancount
-------------------------------------------

``beangulp`` provides a framework for importing transactions into a
Beancount ledger from account statements and other documents and for
managing documents.

``beangulp`` is compatible with both Beancount 2.3 and the
Beancount 3.0. ``beangulp`` is the evolution of ``beancount.ingest`` in
Beancount 2.3 and replaces it in the Beancount 3.0 release.

Identification conflicts
------------------------

When more than one importer identifies a document, ``beangulp`` reports
all matching importers, each with the explanation returned by
``Importer.explain_identification()``.  For example, two banks can share
a CSV header and both match the same statement::

    Document identified by more than one importer.
      bank.a: header Date,Description,Amount matches bank A
      bank.b: header Date,Description,Amount matches bank B

Use ``beangulp.identify.resolve()`` to obtain the auditable
``Identification`` result (also available through
``identify.identify(..., return_result=True)``), including the matching
importers, their explanations, and the conflict.

Conflicts can be resolved with selection rules configured on ``Ingest``
or passed to ``identify()``::

    import beangulp

    ingest = beangulp.Ingest(
        importers,
        rules={"statements/bank-a/*.csv": "bank.a"},
    )

A rule maps a source to the preferred importer name, or to a list of
importer names in order of preference.  The source is matched against
the document path, its basename and stem, the importer name, and the
source returned by ``Importer.identify_source()``.  Rules can also use
compiled regular expressions or callables.  Selection depends only on
importer names and rule configuration, never on the order of the
``importers`` list, so reordering importers does not change the conflict
report or the selected importer.

Documentation is available in form of code docstrings and
`examples`__.  The `documentation`__ for ``beancount.ingest`` provided
a good introduction and is still partially relevant to ``beangulp``.
The rest of the Beancount documentation can be found `here`__.

__ https://github.com/beancount/beangulp/tree/master/examples/
__ https://beancount.github.io/docs/importing_external_data/
__ https://beancount.github.io/docs/

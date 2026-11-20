import datetime
import os
import shutil
import tempfile
import unittest

from decimal import Decimal
from os import path

from beancount.core import data

from beangulp import cache
from beangulp import exceptions
from beangulp import extract
from beangulp import identify
from beangulp import importer
from beangulp import tests


class TestIdentify(unittest.TestCase):
    def test_identify(self):
        importers = [
            tests.utils.Importer("A", "Assets:Tests", "application/pdf"),
            tests.utils.Importer("B", "Assets:Tests", "text/csv"),
        ]

        # Pass an absolute path to identify() to make the cache code
        # used internally by the importers happy. This can go away
        # once FileMemo is removed from the importers interface.
        importer = identify.identify(importers, path.abspath("test.txt"))
        self.assertIsNone(importer)

        importer = identify.identify(importers, path.abspath("test.pdf"))
        self.assertEqual(importer.name, "A")

        importer = identify.identify(importers, path.abspath("test.csv"))
        self.assertEqual(importer.name, "B")

    def test_identify_collision(self):
        importers = [
            tests.utils.Importer("A", "Assets:Tests", "text/csv"),
            tests.utils.Importer("B", "Assets:Tests", "text/csv"),
        ]

        importer = identify.identify(importers, path.abspath("test.txt"))
        self.assertIsNone(importer)

        with self.assertRaises(exceptions.Error):
            importer = identify.identify(importers, path.abspath("test.csv"))


class StatementImporter(importer.Importer):
    """Importer matching a marker in the file head and parsing the amount."""

    def __init__(self, name, marker):
        self._name = name
        self._marker = marker

    @property
    def name(self):
        return self._name

    def identify(self, filepath):
        return self._marker in cache.get_file(filepath).head()

    def account(self, filepath):
        return "Assets:Tests"

    def extract(self, filepath, existing):
        contents = cache.get_file(filepath).contents()
        amount = Decimal(contents.strip().splitlines()[-1].split(",")[1])
        meta = data.new_metadata(filepath, 1)
        return [
            data.Transaction(
                meta,
                datetime.date(2026, 11, 19),
                "*",
                None,
                "Statement",
                frozenset(),
                frozenset(),
                [
                    data.Posting(
                        "Assets:Tests",
                        data.Amount(amount, "USD"),
                        None,
                        None,
                        None,
                        None,
                    )
                ],
            )
        ]


class TestIdentifyVersioning(unittest.TestCase):
    """Identification and extraction operate on the same file version."""

    CONTENTS_V1 = "Date,Amount A\n2026-11-19,1.00\n"
    CONTENTS_V2 = "Date,Amount B\n2026-11-19,2.00\n"

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tempdir)
        self.filename = path.join(self.tempdir, "statement.csv")
        self.importers = [
            StatementImporter("A", "Amount A"),
            StatementImporter("B", "Amount B"),
        ]

    def _replace(self, contents, preserve=None):
        """Replace the test file, optionally preserving its timestamps."""
        with open(self.filename, "w") as f:
            f.write(contents)
        if preserve is not None:
            os.utime(self.filename, ns=(preserve.st_atime_ns, preserve.st_mtime_ns))

    def test_identify_extract_round_version_consistency(self):
        self.assertEqual(len(self.CONTENTS_V1), len(self.CONTENTS_V2))
        self._replace(self.CONTENTS_V1)

        # Identify the first version of the file.
        found = identify.identify(self.importers, self.filename)
        self.assertEqual(found.name, "A")

        # Replace the file with a new version preserving the file
        # modification time and size before the extract phase runs.
        stat = os.stat(self.filename)
        self._replace(self.CONTENTS_V2, preserve=stat)

        # The processing round that spanned the replacement ends
        # explicitly instead of reading the new version of the file
        # with the importer identified for the old version.
        with self.assertRaises(exceptions.Error):
            extract.extract_from_file(found, self.filename, [])

        # A new processing round identifies the new version of the
        # file and extracts it completely.
        found = identify.identify(self.importers, self.filename)
        self.assertEqual(found.name, "B")
        entries = extract.extract_from_file(found, self.filename, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].postings[0].units, data.Amount(Decimal("2.00"), "USD"))

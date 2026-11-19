import io
import textwrap
import unittest

from datetime import timedelta
from os import path
from tempfile import TemporaryDirectory
from unittest import mock

from beancount.parser import parser
from beangulp import extract
from beangulp import importer
from beangulp import similar
from beangulp import tests
from beangulp.exceptions import EntryParseError
from beangulp.exceptions import FileOpenError
from beangulp.exceptions import NoImporterError


class _ReadErrorImporter(importer.Importer):
    """An importer that cannot open or read a document."""

    def __init__(self, suffix=".bin", exc=None):
        self.suffix = suffix
        self.exc = exc if exc is not None else OSError("cannot open document")

    @property
    def name(self):
        return "tests.ReadError"

    def identify(self, filepath):
        return filepath.endswith(self.suffix)

    def account(self, filepath):
        return "Assets:Tests"

    def extract(self, filepath, existing):
        raise self.exc


class _EntryErrorImporter(importer.Importer):
    """An importer that reads a document but cannot parse a record."""

    @property
    def name(self):
        return "tests.EntryError"

    def identify(self, filepath):
        return filepath.endswith(".bad")

    def account(self, filepath):
        return "Assets:Tests"

    def extract(self, filepath, existing):
        raise EntryParseError(f"Error processing {filepath} line 3")


class _IdentifyReadErrorImporter(importer.Importer):
    """An importer that cannot read a document even to identify it."""

    @property
    def name(self):
        return "tests.IdentifyReadError"

    def identify(self, filepath):
        if filepath.endswith(".unreadable"):
            raise OSError("permission denied")
        return False

    def account(self, filepath):
        return "Assets:Tests"


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.importer = tests.utils.Importer(None, "Assets:Tests", None)

    def test_extract_from_file_no_entries(self):
        entries = extract.extract_from_file(self.importer, path.abspath("test.csv"), [])
        self.assertEqual(entries, [])

    def test_extract_from_file(self):
        entries, errors, options = parser.parse_string(
            textwrap.dedent("""
            1970-01-03 * "Test"
              Assets:Tests  1.00 USD

            1970-01-01 * "Test"
              Assets:Tests  1.00 USD

            1970-01-02 * "Test"
              Assets:Tests  1.00 USD
            """)
        )

        importer_ = mock.MagicMock(wraps=self.importer)
        importer_.extract.return_value = entries
        entries = extract.extract_from_file(importer_, path.abspath("test.csv"), [])
        dates = [entry.date for entry in entries]
        self.assertSequenceEqual(dates, sorted(dates))

    def test_extract_from_file_ensure_sanity(self):
        entries, errors, options = parser.parse_string("""
            1970-01-01 * "Test"
              Assets:Tests  1.00 USD
            """)

        # Break something.
        entries[-1] = entries[-1]._replace(narration=42)
        importer_ = mock.MagicMock(wraps=self.importer)
        importer_.extract.return_value = entries
        with self.assertRaises(AssertionError):
            extract.extract_from_file(importer_, path.abspath("test.csv"), [])

    def test_extract_from_file_open_error(self):
        # OSError subclasses and decoding errors are classified as
        # document read errors.
        for exc in (
            OSError("boom"),
            FileNotFoundError("missing"),
            PermissionError("denied"),
            UnicodeDecodeError("utf-8", b"", 0, 1, "bad byte"),
        ):
            with self.subTest(exc=exc):
                importer_ = _ReadErrorImporter(exc=exc)
                with self.assertRaises(FileOpenError) as ctx:
                    extract.extract_from_file(importer_, path.abspath("test.bin"), [])
                self.assertIs(ctx.exception.__cause__, exc)

    def test_extract_from_file_entry_error_passthrough(self):
        importer_ = _EntryErrorImporter()
        with self.assertRaises(EntryParseError):
            extract.extract_from_file(importer_, path.abspath("test.bad"), [])


class TestProcessDocuments(unittest.TestCase):
    def _good_importer(self):
        importer_ = tests.utils.Importer("tests.Good", "Assets:Tests", "text/csv")
        entries, _, _ = parser.parse_string(
            '1970-01-01 * "Test"\n  Assets:Tests  1.00 USD\n'
        )
        importer_ = mock.MagicMock(wraps=importer_)
        importer_.extract.return_value = entries
        return importer_

    def test_classification(self):
        importers = [
            self._good_importer(),
            _ReadErrorImporter(),
            _EntryErrorImporter(),
        ]

        seen_pending = []
        seen_error = []
        seen_success = []

        with TemporaryDirectory() as tmp:
            documents = []
            for name in ("notes.txt", "good.csv", "data.bin", "rows.bad"):
                filepath = path.join(tmp, name)
                with open(filepath, "w") as fd:
                    fd.write("data\n")
                documents.append(filepath)

            extracted, pending, errors = extract.process_documents(
                importers,
                documents,
                [],
                on_error=lambda f, e: seen_error.append((f, type(e))),
                on_pending=lambda f, e: seen_pending.append((f, type(e))),
                on_success=lambda f: seen_success.append(f),
            )

        # Only the readable document contributes entries.
        self.assertEqual([path.basename(f) for f, _, _, _ in extracted], ["good.csv"])
        self.assertTrue(extracted[0][1])

        # No matching importer: the document is pending, not an error.
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0][0].endswith("notes.txt"))
        self.assertIsInstance(pending[0][1], NoImporterError)

        # Read and record errors are kept distinct and classified.
        self.assertEqual(
            {path.basename(f): type(e) for f, e in errors},
            {"data.bin": FileOpenError, "rows.bad": EntryParseError},
        )

        # Callbacks mirror the classification.
        self.assertEqual([path.basename(f) for f, _ in seen_pending], ["notes.txt"])
        self.assertEqual(
            {path.basename(f): cls for f, cls in seen_error},
            {"data.bin": FileOpenError, "rows.bad": EntryParseError},
        )
        self.assertEqual([path.basename(f) for f in seen_success], ["good.csv"])

    def test_continues_after_error(self):
        importers = [self._good_importer(), _ReadErrorImporter()]

        with TemporaryDirectory() as tmp:
            bad = path.join(tmp, "data.bin")
            good = path.join(tmp, "good.csv")
            for filepath in (bad, good):
                with open(filepath, "w") as fd:
                    fd.write("data\n")

            # The unreadable document is processed first but does not
            # stop processing of the readable one.
            extracted, pending, errors = extract.process_documents(
                importers, [bad, good], [], on_error=lambda f, e: None
            )

        self.assertEqual(len(extracted), 1)
        self.assertTrue(extracted[0][0].endswith("good.csv"))
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0][0].endswith("data.bin"))

    def test_failfast(self):
        importers = [self._good_importer(), _ReadErrorImporter()]

        with TemporaryDirectory() as tmp:
            bad = path.join(tmp, "data.bin")
            good = path.join(tmp, "good.csv")
            for filepath in (bad, good):
                with open(filepath, "w") as fd:
                    fd.write("data\n")

            extracted, pending, errors = extract.process_documents(
                importers,
                [bad, good],
                [],
                on_error=lambda f, e: None,
                failfast=True,
            )

        self.assertEqual(extracted, [])
        self.assertEqual(len(errors), 1)

    def test_read_error_while_identifying(self):
        importers = [_IdentifyReadErrorImporter()]

        seen_error = []
        with TemporaryDirectory() as tmp:
            filepath = path.join(tmp, "doc.unreadable")
            with open(filepath, "w") as fd:
                fd.write("data\n")

            extracted, pending, errors = extract.process_documents(
                importers,
                [filepath],
                [],
                on_error=lambda f, e: seen_error.append(type(e)),
            )

        self.assertEqual(extracted, [])
        self.assertEqual(pending, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0][1], FileOpenError)
        self.assertIsInstance(errors[0][1].__cause__, OSError)
        self.assertEqual(seen_error, [FileOpenError])



class TestDuplicates(unittest.TestCase):
    def test_mark_duplicate_entries(self):
        entries, error, options = parser.parse_string(
            textwrap.dedent("""
          1970-01-01 * "Test"
            Assets:Tests  10.00 USD

          1970-01-02 * "Test"
            Assets:Tests  20.00 USD
        """)
        )
        compare = similar.heuristic_comparator()
        extract.mark_duplicate_entries(entries, entries[:1], timedelta(days=2), compare)
        self.assertTrue(entries[0].meta[extract.DUPLICATE])
        self.assertNotIn(extract.DUPLICATE, entries[1].meta)


class TestPrint(unittest.TestCase):
    def test_print_extracted_entries(self):
        entries, error, options = parser.parse_string(
            textwrap.dedent("""
            1970-01-01 * "Test"
              Assets:Tests  10.00 USD""")
        )

        extracted = [
            ("/path/to/test.csv", entries, None, None),
            ("/path/to/empty.pdf", [], None, None),
        ]

        output = io.StringIO()
        extract.print_extracted_entries(extracted, output)

        self.assertEqual(
            output.getvalue(),
            textwrap.dedent("""\
            ;; -*- mode: beancount -*-

            **** /path/to/test.csv

            1970-01-01 * "Test"
              Assets:Tests  10.00 USD


            **** /path/to/empty.pdf


            """),
        )

    def test_print_extracted_entries_duplictes(self):
        entries, error, options = parser.parse_string(
            textwrap.dedent("""
            1970-01-01 * "Test"
              Assets:Tests  10.00 USD

            1970-01-01 * "Test"
              Assets:Tests  10.00 USD """)
        )

        # Mark the second entry as duplicate
        entries[1].meta[extract.DUPLICATE] = True

        extracted = [
            ("/path/to/test.csv", entries, None, None),
        ]

        output = io.StringIO()
        extract.print_extracted_entries(extracted, output)

        self.assertEqual(
            output.getvalue(),
            textwrap.dedent("""\
            ;; -*- mode: beancount -*-

            **** /path/to/test.csv

            1970-01-01 * "Test"
              Assets:Tests  10.00 USD

            ; 1970-01-01 * "Test"
            ;   Assets:Tests  10.00 USD


            """),
        )

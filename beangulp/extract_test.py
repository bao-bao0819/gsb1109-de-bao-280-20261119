import io
import textwrap
import unittest

from datetime import timedelta
from os import path
from unittest import mock

from beancount.parser import parser
from beangulp import extract
from beangulp import similar
from beangulp import tests


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

        importer = mock.MagicMock(wraps=self.importer)
        importer.extract.return_value = entries
        entries = extract.extract_from_file(importer, path.abspath("test.csv"), [])
        dates = [entry.date for entry in entries]
        self.assertSequenceEqual(dates, sorted(dates))

    def test_extract_from_file_ensure_sanity(self):
        entries, errors, options = parser.parse_string("""
            1970-01-01 * "Test"
              Assets:Tests  1.00 USD
            """)

        # Break something.
        entries[-1] = entries[-1]._replace(narration=42)
        importer = mock.MagicMock(wraps=self.importer)
        importer.extract.return_value = entries
        with self.assertRaises(AssertionError):
            extract.extract_from_file(importer, path.abspath("test.csv"), [])


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

    def test_mark_duplicate_entries_one_replay_one_existing(self):
        # An existing entry can only cancel its own replay. Of two
        # identical extracted entries, only one can be a duplicate of
        # the existing entry. The other one may be a legitimate
        # transaction, for example a second purchase of the same
        # amount at the same store, and is flagged for review instead
        # of being silently discarded.
        existing, errors, options = parser.parse_string(
            textwrap.dedent("""
          1970-01-01 * "Test"
            Assets:Tests  10.00 USD
        """)
        )
        entries, errors, options = parser.parse_string(
            textwrap.dedent("""
          1970-01-01 * "Test"
            Assets:Tests  10.00 USD

          1970-01-01 * "Test"
            Assets:Tests  10.00 USD
        """)
        )
        compare = similar.heuristic_comparator()
        extract.mark_duplicate_entries(entries, existing, timedelta(days=2), compare)
        self.assertIs(entries[0].meta[extract.DUPLICATE], existing[0])
        self.assertNotIn(extract.DUPLICATE, entries[1].meta)
        self.assertEqual(entries[1].meta[extract.REVIEW], [existing[0]])

    def test_mark_duplicate_entries_two_replays_two_existing(self):
        # Two identical legitimate transactions in the ledger can each
        # be cancelled by a replay brought in by a statement that has
        # been downloaded twice.
        existing, errors, options = parser.parse_string(
            textwrap.dedent("""
          1970-01-01 * "Test"
            Assets:Tests  10.00 USD

          1970-01-01 * "Test"
            Assets:Tests  10.00 USD
        """)
        )
        entries, errors, options = parser.parse_string(
            textwrap.dedent("""
          1970-01-01 * "Test"
            Assets:Tests  10.00 USD

          1970-01-01 * "Test"
            Assets:Tests  10.00 USD
        """)
        )
        compare = similar.heuristic_comparator()
        extract.mark_duplicate_entries(entries, existing, timedelta(days=2), compare)
        self.assertIs(entries[0].meta[extract.DUPLICATE], existing[0])
        self.assertIs(entries[1].meta[extract.DUPLICATE], existing[1])
        # The first match is ambiguous: the entry could be a replay of
        # either of the two identical existing entries.
        self.assertEqual(entries[0].meta[extract.REVIEW], existing)
        self.assertNotIn(extract.REVIEW, entries[1].meta)

    def test_mark_duplicate_entries_reconciliation_links(self):
        # Reconciliation links, when available, establish the correct
        # match unequivocally.
        existing, errors, options = parser.parse_string(
            textwrap.dedent("""
          1970-01-01 * "Test" ^one
            Assets:Tests  10.00 USD

          1970-01-01 * "Test" ^two
            Assets:Tests  10.00 USD
        """)
        )
        entries, errors, options = parser.parse_string(
            textwrap.dedent("""
          1970-01-01 * "Test" ^two
            Assets:Tests  10.00 USD
        """)
        )
        compare = similar.heuristic_comparator()
        extract.mark_duplicate_entries(entries, existing, timedelta(days=2), compare)
        self.assertIs(entries[0].meta[extract.DUPLICATE], existing[1])
        self.assertNotIn(extract.REVIEW, entries[0].meta)

    def test_mark_duplicate_entries_mismatched_links(self):
        # When both entries carry reconciliation links and they do not
        # agree, the entries are distinct transactions.
        existing, errors, options = parser.parse_string(
            textwrap.dedent("""
          1970-01-01 * "Test" ^one
            Assets:Tests  10.00 USD
        """)
        )
        entries, errors, options = parser.parse_string(
            textwrap.dedent("""
          1970-01-01 * "Test" ^three
            Assets:Tests  10.00 USD
        """)
        )
        compare = similar.heuristic_comparator()
        extract.mark_duplicate_entries(entries, existing, timedelta(days=2), compare)
        self.assertNotIn(extract.DUPLICATE, entries[0].meta)
        self.assertNotIn(extract.REVIEW, entries[0].meta)


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

    def test_print_extracted_entries_review(self):
        entries, error, options = parser.parse_string(
            textwrap.dedent("""
            1970-01-01 * "Test"
              Assets:Tests  10.00 USD

            1970-01-01 * "Test"
              Assets:Tests  10.00 USD""")
        )

        # Flag the second entry for review.
        entries[1].meta[extract.REVIEW] = [entries[0]]

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

            ; review: possible duplicate of <string>:2
            1970-01-01 * "Test"
              Assets:Tests  10.00 USD


            """),
        )

    def test_print_extracted_entries_duplicate_and_review(self):
        entries, error, options = parser.parse_string(
            textwrap.dedent("""
            1970-01-01 * "Test"
              Assets:Tests  10.00 USD

            1970-01-01 * "Test"
              Assets:Tests  10.00 USD

            1970-01-01 * "Test"
              Assets:Tests  10.00 USD""")
        )

        # Mark the third entry as an ambiguous duplicate.
        entries[2].meta[extract.DUPLICATE] = entries[0]
        entries[2].meta[extract.REVIEW] = [entries[0], entries[1]]

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

            1970-01-01 * "Test"
              Assets:Tests  10.00 USD

            ; duplicate of <string>:2
            ; review: also matches <string>:5
            ; 1970-01-01 * "Test"
            ;   Assets:Tests  10.00 USD


            """),
        )

import io
import textwrap
import unittest

from datetime import date
from datetime import timedelta
from os import path
from unittest import mock

from beancount.core import data
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


def _parse(string):
    entries, errors, _ = parser.parse_string(textwrap.dedent(string))
    assert not errors
    return entries


def _ref(entries, filepath, narration):
    for entry in entries:
        if isinstance(entry, data.Transaction) and entry.narration == narration:
            return (filepath, entry.meta["lineno"])
    raise KeyError(narration)


class TestMerge(unittest.TestCase):
    """Two documents for the same account and date.

    File A contains two purchases A1 and A2, file B a reversal B1 and
    a balance assertion. The bank declares that A1 occurred before B1
    and B1 before A2. A third document for an unrelated account
    contains one entry that does not depend on these relations.

    """

    def setUp(self):
        self.entries_a = _parse("""
            2023-01-01 * "A1"
              Assets:Tests  1.00 USD

            2023-01-01 * "A2"
              Assets:Tests  2.00 USD
        """)
        self.entries_b = _parse("""
            2023-01-01 * "B1"
              Assets:Tests  -1.00 USD

            2023-01-01 balance Assets:Tests  100.00 USD
        """)
        self.entries_c = _parse("""
            2023-01-01 * "C1"
              Assets:Other  3.00 USD
        """)
        day = date(2023, 1, 1)
        account = "Assets:Tests"
        a1 = _ref(self.entries_a, "/path/a.csv", "A1")
        a2 = _ref(self.entries_a, "/path/a.csv", "A2")
        b1 = _ref(self.entries_b, "/path/b.csv", "B1")
        self.relations = [
            (account, day, a1, b1),
            (account, day, b1, a2),
        ]
        self.conflicting = self.relations + [
            (account, day, a2, a1),
        ]

    def extracted(self):
        return [
            ("/path/a.csv", self.entries_a, "Assets:Tests", None),
            ("/path/b.csv", self.entries_b, "Assets:Tests", None),
            ("/path/c.csv", self.entries_c, "Assets:Other", None),
        ]

    def merge(self, relations=()):
        output = io.StringIO()
        conflicts = extract.print_merged_entries(
            self.extracted(), output, relations
        )
        return output.getvalue(), conflicts

    def test_merge_reproducible(self):
        # Swapping the order in which the documents are discovered
        # must not change the merged output.
        forward, conflicts = self.merge(self.relations)
        self.assertEqual(conflicts, [])

        output = io.StringIO()
        conflicts = extract.print_merged_entries(
            list(reversed(self.extracted())), output, self.relations
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(output.getvalue(), forward)

    def test_merge_order(self):
        output, conflicts = self.merge(self.relations)
        self.assertEqual(conflicts, [])

        # The balance assertion is effective at the beginning of the
        # day and comes first. The declared relations order A1 before
        # B1 before A2. The unrelated entry is present.
        balance = output.index("balance Assets:Tests")
        a1 = output.index('"A1"')
        b1 = output.index('"B1"')
        a2 = output.index('"A2"')
        c1 = output.index('"C1"')
        self.assertLess(balance, a1)
        self.assertLess(a1, b1)
        self.assertLess(b1, a2)

    def test_merge_traceability(self):
        output, conflicts = self.merge(self.relations)
        self.assertEqual(conflicts, [])

        # Each entry can be traced back to the document and line it
        # was extracted from.
        a1 = _ref(self.entries_a, "/path/a.csv", "A1")
        b1 = _ref(self.entries_b, "/path/b.csv", "B1")
        c1 = _ref(self.entries_c, "/path/c.csv", "C1")
        self.assertIn(f"; source: {a1[0]}:{a1[1]}", output)
        self.assertIn(f"; source: {b1[0]}:{b1[1]}", output)
        self.assertIn(f"; source: {c1[0]}:{c1[1]}", output)

    def test_merge_conflict(self):
        # Requiring A2 before A1 contradicts the other relations.
        output, conflicts = self.merge(self.conflicting)

        # The conflicting relations are listed explicitly.
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict.account, "Assets:Tests")
        self.assertEqual(conflict.date, date(2023, 1, 1))
        self.assertEqual(
            [tuple(r) for r in conflict.relations],
            sorted(tuple(r) for r in self.conflicting),
        )

        # The entries involved in the conflict are not emitted in some
        # seemingly legal order. The unrelated entries are still
        # there.
        self.assertNotIn('"A1"', output)
        self.assertNotIn('"B1"', output)
        self.assertNotIn('"A2"', output)
        self.assertIn("balance Assets:Tests", output)
        self.assertIn('"C1"', output)

        # The conflict is reported in the output.
        a1 = _ref(self.entries_a, "/path/a.csv", "A1")
        b1 = _ref(self.entries_b, "/path/b.csv", "B1")
        self.assertIn("Conflicting ordering relations", output)
        self.assertIn(f"{a1[0]}:{a1[1]} must come before {b1[0]}:{b1[1]}", output)

    def test_merge_conflict_reproducible(self):
        # Conflicts are reported deterministically, independently of
        # the order in which the documents are discovered.
        forward = io.StringIO()
        extract.print_merged_entries(self.extracted(), forward, self.conflicting)

        backward = io.StringIO()
        extract.print_merged_entries(
            list(reversed(self.extracted())), backward, self.conflicting
        )
        self.assertEqual(backward.getvalue(), forward.getvalue())

    def test_merge_relations_scoped(self):
        # Relations are only valid within their account and date.
        day = date(2023, 1, 1)
        account = "Assets:Tests"
        a1 = _ref(self.entries_a, "/path/a.csv", "A1")
        b1 = _ref(self.entries_b, "/path/b.csv", "B1")

        # A relation requiring B1 before A1 applies when scoped to the
        # right account and date...
        output, conflicts = self.merge([(account, day, b1, a1)])
        self.assertEqual(conflicts, [])
        self.assertLess(output.index('"B1"'), output.index('"A1"'))

        # ...but is ignored for a different account...
        output, conflicts = self.merge([("Assets:Other", day, b1, a1)])
        self.assertEqual(conflicts, [])
        self.assertLess(output.index('"A1"'), output.index('"B1"'))

        # ...or a different date.
        output, conflicts = self.merge([(account, date(2023, 1, 2), b1, a1)])
        self.assertEqual(conflicts, [])
        self.assertLess(output.index('"A1"'), output.index('"B1"'))

    def test_merge_tiebreak(self):
        # Entries with the same date and no relation are ordered by
        # source document and line number, not by discovery order.
        entries_x = _parse("""
            2023-01-01 * "X"
              Assets:Tests  1.00 USD
        """)
        entries_y = _parse("""
            2023-01-01 * "Y"
              Assets:Tests  2.00 USD
        """)

        def run(extracted):
            output = io.StringIO()
            extract.print_merged_entries(extracted, output)
            return output.getvalue()

        forward = run([
            ("/path/x.csv", entries_x, "Assets:Tests", None),
            ("/path/y.csv", entries_y, "Assets:Tests", None),
        ])
        backward = run([
            ("/path/y.csv", entries_y, "Assets:Tests", None),
            ("/path/x.csv", entries_x, "Assets:Tests", None),
        ])
        self.assertEqual(forward, backward)
        self.assertLess(forward.index('"X"'), forward.index('"Y"'))

    def test_print_merged_entries(self):
        entries = _parse("""
            1970-01-01 * "Test"
              Assets:Tests  10.00 USD
        """)
        extracted = [
            ("/path/to/test.csv", entries, "Assets:Tests", None),
            ("/path/to/empty.pdf", [], "Assets:Tests", None),
        ]

        output = io.StringIO()
        conflicts = extract.print_merged_entries(extracted, output)
        self.assertEqual(conflicts, [])

        lineno = entries[0].meta["lineno"]
        self.assertEqual(
            output.getvalue(),
            textwrap.dedent(f"""\
            ;; -*- mode: beancount -*-

            ; source: /path/to/test.csv:{lineno}
            1970-01-01 * "Test"
              Assets:Tests  10.00 USD

            """),
        )

import re
import tempfile
import unittest

from os import path

import click.testing

import beangulp

from beangulp import exceptions
from beangulp import identify
from beangulp import tests


class ExplainingImporter(tests.utils.Importer):
    def __init__(self, name, account, mimetype, explanation=None, source=None):
        super().__init__(name, account, mimetype)
        self.explanation = explanation
        self.source = source

    def explain_identification(self, filepath):
        return self.explanation

    def identify_source(self, filepath):
        return self.source


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

        with self.assertRaises(exceptions.Error) as ctx:
            identify.identify(importers, path.abspath("test.csv"))
        self.assertIsInstance(ctx.exception, identify.IdentificationError)


class TestConflict(unittest.TestCase):
    def setUp(self):
        self.importers = [
            ExplainingImporter(
                "bank.a",
                "Assets:A",
                "text/csv",
                ["CSV header matches bank A statement"],
                "bank-a",
            ),
            ExplainingImporter(
                "bank.b",
                "Assets:B",
                "text/csv",
                ["CSV header matches bank B statement"],
                "bank-b",
            ),
        ]

    def test_conflict_reports_every_importer_explanation(self):
        result = identify.resolve(self.importers, path.abspath("statement.csv"))

        self.assertTrue(result.has_conflict)
        self.assertIsNone(result.importer)
        self.assertEqual(result.conflict.candidates, ("bank.a", "bank.b"))
        self.assertEqual(
            result.conflict.explanations,
            {
                "bank.a": "CSV header matches bank A statement",
                "bank.b": "CSV header matches bank B statement",
            },
        )
        self.assertIn("bank.a: CSV header matches bank A statement", result.report())
        self.assertIn("bank.b: CSV header matches bank B statement", result.report())

    def test_conflict_is_raised_with_details(self):
        with self.assertRaises(identify.IdentificationError) as ctx:
            identify.identify(self.importers, path.abspath("statement.csv"))

        error = ctx.exception
        self.assertEqual(error.conflict.candidates, ("bank.a", "bank.b"))
        self.assertIn("CSV header matches bank A statement", str(error))
        self.assertIn("CSV header matches bank B statement", str(error))

    def test_result_can_be_requested_from_identify(self):
        result = identify.identify(
            self.importers, path.abspath("statement.csv"), return_result=True
        )
        self.assertIsInstance(result, identify.Identification)
        self.assertTrue(result.has_conflict)

    def test_no_conflict(self):
        result = identify.resolve(
            [tests.utils.Importer("A", "Assets:Tests", "text/csv")],
            path.abspath("statement.csv"),
        )
        self.assertFalse(result.has_conflict)
        self.assertEqual(result.importer.name, "A")

        result = identify.resolve(self.importers, path.abspath("statement.pdf"))
        self.assertFalse(result.has_conflict)
        self.assertIsNone(result.importer)


class TestSelection(unittest.TestCase):
    def setUp(self):
        self.a = ExplainingImporter(
            "bank.a", "Assets:A", "text/csv", ["header matches bank A"], "bank-a"
        )
        self.b = ExplainingImporter(
            "bank.b", "Assets:B", "text/csv", ["header matches bank B"], "bank-b"
        )

    def test_default_rule_selects_configured_importer(self):
        result = identify.resolve(
            [self.a, self.b],
            path.abspath("statement.csv"),
            rules={"statement.csv": "bank.a"},
        )
        self.assertTrue(result.has_conflict)
        self.assertTrue(result.conflict.resolved)
        self.assertEqual(result.importer.name, "bank.a")
        self.assertIs(result.rule, result.conflict.rule)

    def test_rule_can_match_importer_source(self):
        result = identify.resolve(
            [self.a, self.b],
            path.abspath("statement.csv"),
            rules={re.compile("bank-b"): ["bank.b"]},
        )
        self.assertEqual(result.importer.name, "bank.b")

    def test_rule_can_match_importer_name(self):
        result = identify.resolve(
            [self.a, self.b],
            path.abspath("statement.csv"),
            rules={"bank.a": "bank.a"},
        )
        self.assertEqual(result.importer.name, "bank.a")

    def test_rule_preference_order(self):
        result = identify.resolve(
            [self.a, self.b],
            path.abspath("statement.csv"),
            rules={"statement.csv": ["bank.b", "bank.a"]},
        )
        self.assertEqual(result.importer.name, "bank.b")

    def test_rule_does_not_apply_to_other_sources(self):
        result = identify.resolve(
            [self.a, self.b],
            path.abspath("other.csv"),
            rules={"statement.csv": "bank.a"},
        )
        self.assertTrue(result.has_conflict)
        self.assertFalse(result.conflict.resolved)
        self.assertIsNone(result.importer)

    def test_unmatching_preference_leaves_conflict(self):
        result = identify.resolve(
            [self.a, self.b],
            path.abspath("statement.csv"),
            rules={"statement.csv": ["bank.unknown"]},
        )
        self.assertTrue(result.has_conflict)
        self.assertFalse(result.conflict.resolved)
        with self.assertRaises(identify.IdentificationError):
            identify.identify(
                [self.a, self.b],
                path.abspath("statement.csv"),
                rules={"statement.csv": ["bank.unknown"]},
            )

    def test_callable_selector(self):
        def choose(matches):
            return next(item for item in matches if item.name == "bank.b")

        result = identify.resolve(
            [self.a, self.b], path.abspath("statement.csv"), selectors=choose
        )
        self.assertEqual(result.importer.name, "bank.b")

    def test_priority_mapping_rule(self):
        # A mapping of importer name to priority; lower values win.
        result = identify.resolve(
            [self.a, self.b],
            path.abspath("statement.csv"),
            rules={"statement.csv": {"bank.b": 1, "bank.a": 2}},
        )
        self.assertEqual(result.importer.name, "bank.b")

    def test_importer_name_rule_metadata_is_not_a_preference(self):
        result = identify.resolve(
            [self.a, self.b],
            path.abspath("statement.csv"),
            rules=[
                {
                    "source": "statement.csv",
                    "importers": ["bank.a"],
                    "name": "bank-a-statements",
                }
            ],
        )
        self.assertEqual(result.importer.name, "bank.a")
        self.assertEqual(result.rule.label, "bank-a-statements")

    def test_selection_rule_instance(self):
        rule = identify.SelectionRule("statement.csv", "bank.a", name="bank-a-rule")
        result = identify.resolve(
            [self.a, self.b], path.abspath("statement.csv"), rules=rule
        )
        self.assertEqual(result.importer.name, "bank.a")
        self.assertEqual(result.rule.label, "bank-a-rule")


class TestOrderIndependence(unittest.TestCase):
    def setUp(self):
        self.importers = [
            ExplainingImporter(
                "bank.a", "Assets:A", "text/csv", ["header matches bank A"], "bank-a"
            ),
            ExplainingImporter(
                "bank.b", "Assets:B", "text/csv", ["header matches bank B"], "bank-b"
            ),
            ExplainingImporter(
                "bank.c", "Assets:C", "text/csv", ["header matches bank C"], "bank-c"
            ),
            tests.utils.Importer("pdf", "Assets:PDF", "application/pdf"),
        ]
        self.filepath = path.abspath("statement.csv")

    def _snapshot(self, importers, rules=None):
        result = identify.resolve(importers, self.filepath, rules)
        return (
            tuple(result.conflict.candidates),
            result.conflict.explanations,
            result.selected.name if result.selected else None,
            result.report(),
            result.asdict(),
        )

    def test_conflict_report_is_order_independent(self):
        expected = self._snapshot(self.importers)
        self.assertEqual(
            expected,
            self._snapshot(list(reversed(self.importers))),
        )

    def test_resolved_selection_is_order_independent(self):
        rules = {"statement.csv": ["bank.c", "bank.a"]}
        expected = self._snapshot(self.importers, rules)
        self.assertEqual(expected[2], "bank.c")

        permutations = [
            list(reversed(self.importers)),
            [self.importers[1], self.importers[3], self.importers[2], self.importers[0]],
            [self.importers[2], self.importers[0], self.importers[3], self.importers[1]],
        ]
        for importers in permutations:
            self.assertEqual(expected, self._snapshot(importers, rules))

    def test_selecting_by_name_does_not_depend_on_order(self):
        # Without a rule the conflict remains a conflict, regardless of the
        # order in which the importers are configured.
        for importers in (self.importers, list(reversed(self.importers))):
            with self.assertRaises(identify.IdentificationError):
                identify.identify(importers, self.filepath)

    def test_matches_are_sorted_by_name(self):
        self.assertEqual(
            [item.name for item in identify.match(self.importers, self.filepath)],
            ["bank.a", "bank.b", "bank.c"],
        )


class TestExplanations(unittest.TestCase):
    def test_explanation_from_identify_result(self):
        class MappingImporter(tests.utils.Importer):
            def identify(self, filepath):
                return {"matched": True, "reason": "header and filename"}

        class TupleImporter(tests.utils.Importer):
            def identify(self, filepath):
                return True, "header only"

        class AttributeImporter(tests.utils.Importer):
            class Result:
                matched = True
                explanation = ["header and account"]

            def identify(self, filepath):
                return self.Result()

        for cls, expected in (
            (MappingImporter, "header and filename"),
            (TupleImporter, "header only"),
            (AttributeImporter, "header and account"),
        ):
            importer = cls("A", "Assets:Tests", "text/csv")
            result = identify.resolve([importer], path.abspath("statement.csv"))
            self.assertEqual(result.matches[0].reason, expected)

    def test_explanation_from_attribute(self):
        importer = tests.utils.Importer("A", "Assets:Tests", "text/csv")
        importer.explanation = "header matches"
        result = identify.resolve([importer], path.abspath("statement.csv"))
        self.assertEqual(result.matches[0].reason, "header matches")

    def test_identify_returning_string_is_a_match(self):
        class TextImporter(tests.utils.Importer):
            def identify(self, filepath):
                return "header matches"

        importer = TextImporter("A", "Assets:Tests", "text/csv")
        result = identify.resolve([importer], path.abspath("statement.csv"))
        self.assertEqual(result.importer.name, "A")
        self.assertEqual(result.matches[0].reason, "header matches")


class TestCli(unittest.TestCase):
    def test_conflict_report_in_cli(self):
        importers = [
            ExplainingImporter(
                "bank.a", "Assets:A", "text/csv", ["header matches bank A"], "bank-a"
            ),
            ExplainingImporter(
                "bank.b", "Assets:B", "text/csv", ["header matches bank B"], "bank-b"
            ),
        ]
        runner = click.testing.CliRunner()

        with tempfile.TemporaryDirectory() as directory:
            filepath = path.join(directory, "statement.csv")
            with open(filepath, "w") as fd:
                fd.write("Date,Amount\n")

            for order in (importers, list(reversed(importers))):
                ingest = beangulp.Ingest(order)
                result = runner.invoke(
                    ingest.cli, ["identify", filepath], catch_exceptions=False
                )
                self.assertEqual(result.exit_code, 1)
                self.assertIn("bank.a: header matches bank A", result.output)
                self.assertIn("bank.b: header matches bank B", result.output)

    def test_cli_selection_rule(self):
        importers = [
            ExplainingImporter(
                "bank.a", "Assets:A", "text/csv", ["header matches bank A"], "bank-a"
            ),
            ExplainingImporter(
                "bank.b", "Assets:B", "text/csv", ["header matches bank B"], "bank-b"
            ),
        ]
        runner = click.testing.CliRunner()

        with tempfile.TemporaryDirectory() as directory:
            filepath = path.join(directory, "statement.csv")
            with open(filepath, "w") as fd:
                fd.write("Date,Amount\n")

            for order in (importers, list(reversed(importers))):
                ingest = beangulp.Ingest(order, rules={"statement.csv": "bank.b"})
                result = runner.invoke(
                    ingest.cli, ["identify", filepath], catch_exceptions=False
                )
                self.assertEqual(result.exit_code, 0)
                self.assertIn("bank.b", result.output)
                self.assertNotIn("bank.a", result.output)

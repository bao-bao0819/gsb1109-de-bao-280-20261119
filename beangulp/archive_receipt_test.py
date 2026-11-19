import json
import os
import unittest

from os import path
from tempfile import TemporaryDirectory
from unittest import mock

import click.testing

import beangulp
from beangulp import receipt
from beangulp.tests.utils import Importer


class TestArchiveReceipt(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.downloads = path.join(self.temp.name, "downloads")
        self.documents = path.join(self.temp.name, "documents")
        os.mkdir(self.downloads)
        os.mkdir(self.documents)
        self.receipt_path = path.join(self.temp.name, "receipt.json")
        self.importer = Importer("test.ImporterA", "Assets:Tests", "text/csv")
        self.runner = click.testing.CliRunner()

    def run_cli(self, *args):
        ingest = beangulp.Ingest([self.importer])
        return self.runner.invoke(ingest.cli, args, catch_exceptions=False)

    def create(self, name, contents=None):
        filename = path.join(self.downloads, name)
        with open(filename, "w", encoding="utf-8") as outfile:
            outfile.write(contents if contents is not None else name)
        return filename

    def extract(self, *args):
        output = path.join(self.temp.name, "output.beancount")
        return self.run_cli(
            "extract", self.downloads, "-o", output, "--receipt", self.receipt_path, *args
        )

    def archive(self, *args):
        return self.run_cli(
            "archive",
            self.downloads,
            "-o",
            self.documents,
            "--receipt",
            self.receipt_path,
            *args,
        )

    def load_receipt(self):
        with open(self.receipt_path, encoding="utf-8") as infile:
            return json.load(infile)

    def test_extract_records_pending(self):
        source = self.create("statement.csv")
        result = self.extract()
        self.assertEqual(result.exit_code, 0, result.output)

        data = self.load_receipt()
        record = data["files"][source]
        self.assertEqual(record["status"], receipt.STATUS_PENDING)
        self.assertEqual(record["account"], "Assets:Tests")
        self.assertEqual(record["importer"], "test.ImporterA")
        self.assertEqual(record["extracted"]["entries"], 0)
        self.assertIn("sha1", record)

    def test_archive_only_files_recorded_documents(self):
        recorded = self.create("statement.csv")
        self.assertEqual(self.extract().exit_code, 0)
        self.create("other.csv")

        result = self.archive()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(path.exists(recorded))
        self.assertTrue(path.exists(path.join(self.downloads, "other.csv")))

    def test_archive_skips_archived_documents(self):
        source = self.create("statement.csv")
        self.assertEqual(self.extract().exit_code, 0)
        self.assertEqual(self.archive().exit_code, 0)
        self.assertEqual(self.load_receipt()["files"][source]["status"], "archived")

        # A second run with the same downloads directory sees no new
        # files, but the alias for the archived source is never moved
        # again when it reappears.
        with open(source, "w", encoding="utf-8") as outfile:
            outfile.write("replacement")
        result = self.archive()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("already archived", result.output)
        self.assertTrue(path.exists(source))

    def test_failed_move_is_recorded_and_retried_alone(self):
        first = self.create("aaa.csv")
        second = self.create("bbb.csv")
        self.assertEqual(self.extract().exit_code, 0)

        original_move = beangulp.archive.move
        calls = []

        def flaky_move(src, dst):
            calls.append(src)
            if src == first:
                raise OSError("permission denied")
            return original_move(src, dst)

        with mock.patch("beangulp.archive.move", side_effect=flaky_move):
            result = self.archive()

        self.assertEqual(result.exit_code, 1)
        data = self.load_receipt()
        self.assertEqual(data["files"][first]["status"], receipt.STATUS_PENDING)
        self.assertEqual(data["files"][first]["archive"]["status"], "failed")
        self.assertIn("permission denied", data["files"][first]["archive"]["error"])
        self.assertEqual(data["files"][second]["status"], "archived")

        # The first document was not moved and its extracted record is
        # still there. The second document was moved successfully.
        self.assertTrue(path.exists(first))
        self.assertFalse(path.exists(second))

        calls.clear()
        with mock.patch("beangulp.archive.move", side_effect=original_move) as move:
            result = self.archive()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(move.call_args_list, [mock.call(first, mock.ANY)])
        self.assertFalse(path.exists(first))

        data = self.load_receipt()
        self.assertEqual(data["files"][first]["status"], "archived")
        self.assertEqual(data["files"][first]["archive"]["status"], "archived")
        self.assertEqual(data["files"][second]["status"], "archived")

    def test_retry_preserves_extraction_record(self):
        source = self.create("statement.csv")
        self.assertEqual(self.extract().exit_code, 0)
        before = self.load_receipt()["files"][source]["extracted"]

        with mock.patch("beangulp.archive.move", side_effect=OSError("busy")):
            result = self.archive()
        self.assertEqual(result.exit_code, 1)

        data = self.load_receipt()
        self.assertEqual(data["files"][source]["extracted"], before)
        self.assertEqual(len(data["files"]), 1)

    def test_dry_run_writes_nothing(self):
        source = self.create("statement.csv")
        self.assertEqual(self.extract().exit_code, 0)
        before = self.load_receipt()

        result = self.archive("-n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(path.exists(source))
        self.assertEqual(self.load_receipt(), before)

    def test_archive_skips_unrecorded_documents(self):
        self.create("statement.csv")
        result = self.archive()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("not extracted", result.output)

    def test_archive_detects_changed_document(self):
        source = self.create("statement.csv")
        self.assertEqual(self.extract().exit_code, 0)
        with open(source, "a", encoding="utf-8") as outfile:
            outfile.write("more")

        result = self.archive()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("changed since extraction", result.output)
        self.assertTrue(path.exists(source))

    def test_reconciles_move_interrupted_after_copy(self):
        source = self.create("statement.csv")
        self.assertEqual(self.extract().exit_code, 0)
        record = self.load_receipt()["files"][source]

        # Simulate a previous interruption after the file was moved but
        # before the receipt was updated.
        destination = path.join(self.documents, "Assets/Tests/1970-01-01.statement.csv")
        os.makedirs(path.dirname(destination))
        os.replace(source, destination)
        receipt_file = receipt.Receipt.load(self.receipt_path)
        receipt_file.record_archive_failed(
            source,
            destination,
            "interrupted",
            sha1=record["sha1"],
        )
        receipt_file.save()

        result = self.archive()
        self.assertEqual(result.exit_code, 0, result.output)
        data = self.load_receipt()
        self.assertEqual(data["files"][source]["status"], "archived")
        self.assertEqual(data["files"][source]["archive"]["destination"], destination)
        self.assertTrue(path.exists(destination))

    def test_completes_copy_interrupted_before_source_removal(self):
        source = self.create("statement.csv")
        self.assertEqual(self.extract().exit_code, 0)
        record = self.load_receipt()["files"][source]

        # Simulate a cross-device move that copied the file but was
        # interrupted before removing the source.
        destination = path.join(self.documents, "Assets/Tests/1970-01-01.statement.csv")
        os.makedirs(path.dirname(destination))
        with open(source, "rb") as infile, open(destination, "wb") as outfile:
            outfile.write(infile.read())
        receipt_file = receipt.Receipt.load(self.receipt_path)
        receipt_file.record_archive_failed(
            source, destination, "interrupted", sha1=record["sha1"]
        )
        receipt_file.save()

        result = self.archive()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(path.exists(source))
        self.assertTrue(path.exists(destination))
        data = self.load_receipt()
        self.assertEqual(data["files"][source]["status"], "archived")

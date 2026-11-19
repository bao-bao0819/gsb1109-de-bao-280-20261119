import json
import os
import unittest

from os import path
from tempfile import TemporaryDirectory

from beangulp import receipt


class TestReceipt(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.filepath = path.join(self.temp.name, "receipt.json")

    def create(self, name="statement.csv", contents="data"):
        filename = path.join(self.temp.name, name)
        with open(filename, "w", encoding="utf-8") as outfile:
            outfile.write(contents)
        return filename

    def test_load_missing_returns_empty(self):
        receipt_file = receipt.Receipt.load(self.filepath)
        self.assertEqual(receipt_file.files, {})

    def test_round_trip(self):
        source = self.create()
        receipt_file = receipt.Receipt.load(self.filepath)
        receipt_file.record_extracted(
            source,
            "digest",
            size=4,
            importer="test.Importer",
            account="Assets:Tests",
            output="output.beancount",
            entries=3,
            duplicates=1,
            timestamp="2026-11-20T00:00:00Z",
        )
        receipt_file.save()

        loaded = receipt.Receipt.load(self.filepath)
        record = loaded.get(source)
        self.assertEqual(record["status"], receipt.STATUS_PENDING)
        self.assertEqual(record["account"], "Assets:Tests")
        self.assertEqual(record["extracted"]["at"], "2026-11-20T00:00:00Z")
        self.assertEqual(record["extracted"]["entries"], 3)
        self.assertEqual(len(loaded.pending()), 1)
        self.assertEqual(loaded.archived(), [])

        with open(self.filepath, encoding="utf-8") as infile:
            data = json.load(infile)
        self.assertEqual(data["version"], receipt.VERSION)

    def test_record_archived_preserves_extraction(self):
        source = self.create()
        receipt_file = receipt.Receipt.load(self.filepath)
        receipt_file.record_extracted(
            source,
            "digest",
            entries=2,
            timestamp="2026-11-20T00:00:00Z",
        )
        receipt_file.record_archived(
            source,
            "/documents/Assets/Tests/2026-11-19.statement.csv",
            sha1="digest",
            timestamp="2026-11-20T00:00:01Z",
        )

        record = receipt_file.get(source)
        self.assertEqual(record["status"], receipt.STATUS_ARCHIVED)
        self.assertEqual(record["extracted"]["at"], "2026-11-20T00:00:00Z")
        self.assertEqual(record["extracted"]["entries"], 2)
        self.assertEqual(record["archive"]["status"], receipt.STATUS_ARCHIVED)
        self.assertEqual(receipt_file.pending(), [])
        self.assertEqual(len(receipt_file.archived()), 1)

    def test_record_archive_failed_keeps_pending_and_extraction(self):
        source = self.create()
        receipt_file = receipt.Receipt.load(self.filepath)
        receipt_file.record_extracted(
            source,
            "digest",
            entries=2,
            timestamp="2026-11-20T00:00:00Z",
        )
        receipt_file.record_archive_failed(
            source,
            "/documents/target.csv",
            "disk full",
            sha1="digest",
            timestamp="2026-11-20T00:00:01Z",
        )

        record = receipt_file.get(source)
        self.assertEqual(record["status"], receipt.STATUS_PENDING)
        self.assertEqual(record["extracted"]["entries"], 2)
        self.assertEqual(record["archive"]["status"], receipt.STATUS_FAILED)
        self.assertEqual(record["archive"]["error"], "disk full")
        self.assertEqual(len(receipt_file.pending()), 1)

    def test_reextract_unchanged_keeps_archive_status(self):
        source = self.create()
        receipt_file = receipt.Receipt.load(self.filepath)
        receipt_file.record_extracted(source, "digest", entries=2)
        receipt_file.record_archived(source, "/documents/target.csv", sha1="digest")
        receipt_file.record_extracted(source, "digest", entries=2)

        record = receipt_file.get(source)
        self.assertEqual(record["status"], receipt.STATUS_ARCHIVED)
        self.assertEqual(record["archive"]["destination"], "/documents/target.csv")

    def test_reextract_changed_resets_record(self):
        source = self.create()
        receipt_file = receipt.Receipt.load(self.filepath)
        receipt_file.record_extracted(
            source, "old", entries=2, timestamp="2026-11-20T00:00:00Z"
        )
        receipt_file.record_archived(source, "/documents/target.csv", sha1="old")
        receipt_file.record_extracted(
            source, "new", entries=5, timestamp="2026-11-20T01:00:00Z"
        )

        record = receipt_file.get(source)
        self.assertEqual(record["status"], receipt.STATUS_PENDING)
        self.assertNotIn("archive", record)
        self.assertEqual(record["extracted"]["entries"], 5)
        self.assertEqual(record["extracted"]["at"], "2026-11-20T01:00:00Z")

    def test_hash_mismatch_raises(self):
        source = self.create()
        receipt_file = receipt.Receipt.load(self.filepath)
        receipt_file.record_extracted(source, "digest")
        with self.assertRaises(ValueError):
            receipt_file.record_archived(source, "/documents/target.csv", sha1="other")

    def test_normalizes_source_path(self):
        source = self.create()
        receipt_file = receipt.Receipt.load(self.filepath)
        receipt_file.record_extracted(source, "digest")

        relative = path.join(self.temp.name, ".", "statement.csv")
        self.assertIsNotNone(receipt_file.get(relative))
        self.assertEqual(os.path.basename(next(iter(receipt_file.files))), "statement.csv")

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        source = self.create()
        receipt_file = receipt.Receipt.load(self.filepath)
        receipt_file.record_extracted(source, "digest")
        receipt_file.save()

        leftovers = [name for name in os.listdir(self.temp.name) if name.startswith(".")]
        self.assertEqual(leftovers, [])
        self.assertTrue(path.exists(self.filepath))

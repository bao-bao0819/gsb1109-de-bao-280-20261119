import datetime
import os
import time
import unittest

from os import path
from shutil import rmtree
from tempfile import mkdtemp
from unittest import mock

from beangulp import archive
from beangulp import exceptions
from beangulp import tests


class TestFilepath(unittest.TestCase):
    def setUp(self):
        self.importer = tests.utils.Importer(None, "Assets:Tests", None)

    def test_filepath(self):
        importer = mock.MagicMock(wraps=self.importer)
        importer.filename.return_value = "foo.csv"
        filepath = archive.filepath(importer, path.abspath("test.pdf"))
        self.assertEqual(filepath, "Assets/Tests/1970-01-01.foo.csv")

    def test_filepath_no_filename(self):
        filepath = archive.filepath(self.importer, path.abspath("test.pdf"))
        self.assertEqual(filepath, "Assets/Tests/1970-01-01.test.pdf")

    def test_filepath_no_date(self):
        importer = mock.MagicMock(wraps=self.importer)
        importer.date.return_value = None
        with mock.patch(
            "beangulp.archive.utils.getmdate",
            return_value=datetime.datetime.fromtimestamp(0, datetime.timezone.utc),
        ):
            filepath = archive.filepath(importer, path.abspath("test.pdf"))
        self.assertEqual(filepath, "Assets/Tests/1970-01-01.test.pdf")

    def test_filepath_sep_in_name(self):
        importer = mock.MagicMock(wraps=self.importer)
        importer.filename.return_value = f"dir{os.sep:}name.pdf"
        with self.assertRaises(exceptions.Error) as ex:
            archive.filepath(importer, path.abspath("test.pdf"))
        self.assertRegex(ex.exception.message, r"contains path separator")

    def test_filepath_date_in_name(self):
        importer = mock.MagicMock(wraps=self.importer)
        importer.filename.return_value = "1970-01-03.name.pdf"
        with self.assertRaises(exceptions.Error) as ex:
            archive.filepath(importer, path.abspath("test.pdf"))
        self.assertRegex(ex.exception.message, r"contains [\w\s]+ date")


class TestVersionedFilepath(unittest.TestCase):
    def test_version_zero(self):
        self.assertEqual("a/b.pdf", archive.versioned_filepath("a/b.pdf", 0))

    def test_version_inserted_before_extension(self):
        self.assertEqual("a/b.1.pdf", archive.versioned_filepath("a/b.pdf", 1))
        self.assertEqual("a/b.12.csv", archive.versioned_filepath("a/b.csv", 12))

    def test_no_extension(self):
        self.assertEqual("a/b.1", archive.versioned_filepath("a/b", 1))


class TestMove(unittest.TestCase):
    def setUp(self):
        self.temp = mkdtemp()
        self.src = path.join(self.temp, "src.pdf")
        self.dst = path.join(self.temp, "documents", "dst.pdf")
        with open(self.src, "wb") as outfile:
            outfile.write(b"content")

    def tearDown(self):
        rmtree(self.temp)

    def test_move(self):
        archive.move(self.src, self.dst)
        self.assertFalse(path.exists(self.src))
        with open(self.dst, "rb") as infile:
            self.assertEqual(b"content", infile.read())

    def test_move_creates_directories(self):
        dest = path.join(self.temp, "a", "b", "c", "dst.pdf")
        archive.move(self.src, dest)
        self.assertTrue(path.exists(dest))

    def test_move_existing_destination(self):
        archive.move(self.src, self.dst)
        with open(self.src, "wb") as outfile:
            outfile.write(b"other")
        with self.assertRaises(exceptions.Error) as ex:
            archive.move(self.src, self.dst)
        self.assertRegex(ex.exception.message, "already exists")
        # The source and the archived file are both intact.
        self.assertTrue(path.exists(self.src))
        with open(self.dst, "rb") as infile:
            self.assertEqual(b"content", infile.read())

    def test_move_checksum_mismatch(self):
        with self.assertRaises(exceptions.Error) as ex:
            archive.move(self.src, self.dst, checksum="0" * 40)
        self.assertRegex(ex.exception.message, "changed during archiving")
        self.assertTrue(path.exists(self.src))
        self.assertFalse(path.exists(self.dst))

    def test_move_overwrite(self):
        archive.move(self.src, self.dst)
        with open(self.src, "wb") as outfile:
            outfile.write(b"other")
        archive.move(self.src, self.dst, overwrite=True)
        with open(self.dst, "rb") as infile:
            self.assertEqual(b"other", infile.read())

    def test_move_without_hardlink_support(self):
        error = OSError("hard links not supported")
        error.errno = getattr(os, "EPERM", 1)
        with mock.patch("beangulp.archive.os.link", side_effect=error):
            archive.move(self.src, self.dst)
        self.assertFalse(path.exists(self.src))
        with open(self.dst, "rb") as infile:
            self.assertEqual(b"content", infile.read())

    def test_move_error_leaves_no_temporary_files(self):
        with open(self.src, "wb") as outfile:
            outfile.write(b"content")
        with self.assertRaises(exceptions.Error):
            archive.move(self.src, self.dst, checksum="0" * 40)
        self.assertListEqual(
            [path.join(self.temp, "documents", name) for name in os.listdir(
                path.join(self.temp, "documents")
            )],
            [],
        )

    def test_move_interrupted_keeps_destination_and_source(self):
        archive.move(self.src, self.dst)

        with open(self.src, "wb") as outfile:
            outfile.write(b"corrected")

        # Simulate an interruption while the copy is in progress: the
        # destination must not have been modified and the source must
        # still be there for a later run.
        with mock.patch(
            "beangulp.archive.shutil.copyfileobj", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                archive.move(self.src, self.dst, overwrite=True)

        with open(self.dst, "rb") as infile:
            self.assertEqual(b"content", infile.read())
        self.assertTrue(path.exists(self.src))
        self.assertListEqual(
            [name for name in os.listdir(path.dirname(self.dst)) if ".tmp" in name],
            [],
        )

        # The next run completes the archival.
        archive.move(self.src, self.dst, overwrite=True)
        with open(self.dst, "rb") as infile:
            self.assertEqual(b"corrected", infile.read())
        self.assertFalse(path.exists(self.src))


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.temp = mkdtemp()
        self.dst = path.join(self.temp, "1970-01-01.statement.pdf")

    def tearDown(self):
        rmtree(self.temp)

    def write(self, filepath, content):
        os.makedirs(path.dirname(filepath), exist_ok=True)
        with open(filepath, "wb") as outfile:
            outfile.write(content)
        return archive.file_checksum(filepath)

    def test_empty_destination(self):
        action, filepath = archive.resolve(self.dst, "abc")
        self.assertEqual(archive.ARCHIVE, action)
        self.assertEqual(self.dst, filepath)

    def test_identical_content_is_duplicate(self):
        checksum = self.write(self.dst, b"content")
        action, filepath = archive.resolve(self.dst, checksum)
        self.assertEqual(archive.DUPLICATE, action)
        self.assertEqual(self.dst, filepath)

    def test_different_content_is_versioned(self):
        self.write(self.dst, b"old")
        checksum = self.write(path.join(self.temp, "new"), b"new")
        action, filepath = archive.resolve(self.dst, checksum)
        self.assertEqual(archive.ARCHIVE, action)
        self.assertEqual(path.join(self.temp, "1970-01-01.statement.1.pdf"), filepath)

    def test_identical_to_a_version_is_duplicate(self):
        self.write(self.dst, b"old")
        version = path.join(self.temp, "1970-01-01.statement.1.pdf")
        checksum = self.write(version, b"corrected")
        action, filepath = archive.resolve(self.dst, checksum)
        self.assertEqual(archive.DUPLICATE, action)
        self.assertEqual(version, filepath)

    def test_version_allocation_skips_used_numbers(self):
        self.write(self.dst, b"zero")
        self.write(path.join(self.temp, "1970-01-01.statement.1.pdf"), b"one")
        self.write(path.join(self.temp, "1970-01-01.statement.3.pdf"), b"three")
        action, filepath = archive.resolve(self.dst, "unseen")
        self.assertEqual(archive.ARCHIVE, action)
        self.assertEqual(path.join(self.temp, "1970-01-01.statement.2.pdf"), filepath)

    def test_reserved_paths_in_same_batch(self):
        reserved = {}
        checksum1 = "checksum1"
        action, filepath = archive.resolve(self.dst, checksum1, reserved)
        self.assertEqual(archive.ARCHIVE, action)
        reserved[filepath] = checksum1

        action, filepath = archive.resolve(self.dst, "checksum2", reserved)
        self.assertEqual(archive.ARCHIVE, action)
        self.assertEqual(path.join(self.temp, "1970-01-01.statement.1.pdf"), filepath)
        reserved[filepath] = "checksum2"

        action, filepath = archive.resolve(self.dst, checksum1, reserved)
        self.assertEqual(archive.DUPLICATE, action)
        self.assertEqual(self.dst, filepath)

    def test_overwrite_canonical_path(self):
        self.write(self.dst, b"old")
        action, filepath = archive.resolve(self.dst, "new", overwrite=True)
        self.assertEqual(archive.REPLACE, action)
        self.assertEqual(self.dst, filepath)

    def test_overwrite_still_deduplicates(self):
        checksum = self.write(self.dst, b"same")
        action, filepath = archive.resolve(self.dst, checksum, overwrite=True)
        self.assertEqual(archive.DUPLICATE, action)

    def test_overwrite_does_not_clobber_reserved_path(self):
        self.write(self.dst, b"old")
        reserved = {self.dst: "planned"}
        action, filepath = archive.resolve(self.dst, "new", reserved, overwrite=True)
        self.assertEqual(archive.ARCHIVE, action)
        self.assertEqual(path.join(self.temp, "1970-01-01.statement.1.pdf"), filepath)


class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.temp = mkdtemp()
        self.stale = path.join(self.temp, ".beangulp-stale.tmp")
        self.fresh = path.join(self.temp, ".beangulp-fresh.tmp")
        with open(self.stale, "wb") as outfile:
            outfile.write(b"partial")
        with open(self.fresh, "wb") as outfile:
            outfile.write(b"in progress")
        old = time.time() - archive.TEMP_MAX_AGE - 60
        os.utime(self.stale, (old, old))

    def tearDown(self):
        rmtree(self.temp)

    def test_cleanup_removes_only_stale_temporary_files(self):
        archive.cleanup(self.temp)
        self.assertFalse(path.exists(self.stale))
        self.assertTrue(path.exists(self.fresh))

    def test_cleanup_ignores_archived_documents(self):
        archived = path.join(self.temp, "1970-01-01.statement.pdf")
        with open(archived, "wb") as outfile:
            outfile.write(b"document")
        old = time.time() - archive.TEMP_MAX_AGE - 60
        os.utime(archived, (old, old))
        archive.cleanup(self.temp)
        self.assertTrue(path.exists(archived))


class TestDiscard(unittest.TestCase):
    def setUp(self):
        self.temp = mkdtemp()
        self.src = path.join(self.temp, "src.pdf")
        self.existing = path.join(self.temp, "existing.pdf")

    def tearDown(self):
        rmtree(self.temp)

    def test_discard(self):
        for filepath in (self.src, self.existing):
            with open(filepath, "wb") as outfile:
                outfile.write(b"same")
        archive.discard(self.src, self.existing)
        self.assertFalse(path.exists(self.src))
        self.assertTrue(path.exists(self.existing))

    def test_discard_changed_content(self):
        with open(self.src, "wb") as outfile:
            outfile.write(b"changed")
        with open(self.existing, "wb") as outfile:
            outfile.write(b"same")
        with self.assertRaises(exceptions.Error) as ex:
            archive.discard(self.src, self.existing)
        self.assertRegex(ex.exception.message, "changed before removal")
        self.assertTrue(path.exists(self.src))

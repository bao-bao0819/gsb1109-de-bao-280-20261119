import functools
import os
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta
from decimal import Decimal

from beangulp import simple_cache


class ColumnReader:
    """Converter class configured with the column to read."""

    calls = []

    def __init__(self, column):
        self.column = column

    def convert(self, filename):
        ColumnReader.calls.append(self.column)
        with open(filename) as f:
            header = f.readline().strip().split(",")
            value = f.readline().strip().split(",")[header.index(self.column)]
        return Decimal(value)


class SimpleCacheTest(unittest.TestCase):
    """Test the simple_cache module."""

    def setUp(self):
        """Set up a temporary directory for cache files."""
        self.tempdir = tempfile.mkdtemp()
        self.original_cachedir = simple_cache.CACHEDIR
        simple_cache.CACHEDIR = os.path.join(self.tempdir, "cache")

        # Create a test file
        self.test_file = os.path.join(self.tempdir, "test.txt")
        with open(self.test_file, "w") as f:
            f.write("test content")

    def tearDown(self):
        """Clean up the temporary directory."""
        simple_cache.CACHEDIR = self.original_cachedir

    def test_convert_creates_cache_dir(self):
        """Test that the cache directory is created if it doesn't exist."""

        def converter(filename):
            return "converted content"

        simple_cache.convert(self.test_file, converter)
        self.assertTrue(os.path.exists(simple_cache.CACHEDIR))

    def test_convert_caches_result(self):
        """Test that the result is cached."""
        call_count = 0

        def converter(filename):
            nonlocal call_count
            call_count += 1
            return f"converted content {call_count}"

        # First call should execute the converter
        result1 = simple_cache.convert(self.test_file, converter)
        self.assertEqual(result1, "converted content 1")
        self.assertEqual(call_count, 1)

        # Second call should use the cached result
        result2 = simple_cache.convert(self.test_file, converter)
        self.assertEqual(result2, "converted content 1")
        self.assertEqual(call_count, 1)  # Converter not called again

    def test_different_converters_different_cache(self):
        """Test that different converter functions use different cache entries."""

        def converter1(filename):
            return "result from converter1"

        def converter2(filename):
            return "result from converter2"

        result1 = simple_cache.convert(self.test_file, converter1)
        result2 = simple_cache.convert(self.test_file, converter2)

        self.assertEqual(result1, "result from converter1")
        self.assertEqual(result2, "result from converter2")

    def test_different_files_different_cache(self):
        """Test that different files use different cache entries."""
        test_file2 = os.path.join(self.tempdir, "test2.txt")
        with open(test_file2, "w") as f:
            f.write("different content")

        def converter(filename):
            with open(filename, "r") as f:
                return f"converted: {f.read()}"

        result1 = simple_cache.convert(self.test_file, converter)
        result2 = simple_cache.convert(test_file2, converter)

        self.assertEqual(result1, "converted: test content")
        self.assertEqual(result2, "converted: different content")

    def test_convert_recomputes_when_contents_change(self):
        """Test that replacing the file invalidates the cached results."""
        call_count = 0

        def converter(filename):
            nonlocal call_count
            call_count += 1
            with open(filename, "r") as f:
                return f.read()

        result1 = simple_cache.convert(self.test_file, converter)
        self.assertEqual(result1, "test content")
        self.assertEqual(call_count, 1)

        # Replace the file with a new version preserving the file
        # modification time and size.
        stat = os.stat(self.test_file)
        with open(self.test_file, "w") as f:
            f.write("changed data")
        self.assertEqual(os.path.getsize(self.test_file), stat.st_size)
        os.utime(self.test_file, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        result2 = simple_cache.convert(self.test_file, converter)
        self.assertEqual(result2, "changed data")
        self.assertEqual(call_count, 2)

    def test_convert_reuses_results_for_identical_contents(self):
        """Test that replacing the file with an identical copy keeps the cache."""
        call_count = 0

        def converter(filename):
            nonlocal call_count
            call_count += 1
            return "converted content"

        result1 = simple_cache.convert(self.test_file, converter)
        self.assertEqual(call_count, 1)

        # Replace the file with a copy with identical contents.
        os.remove(self.test_file)
        with open(self.test_file, "w") as f:
            f.write("test content")

        result2 = simple_cache.convert(self.test_file, converter)
        self.assertEqual(result2, result1)
        self.assertEqual(call_count, 1)

    def test_converters_same_code_different_configuration(self):
        """Test that converters sharing the same code but configured
        differently do not mix up their cached results."""
        control_file = os.path.join(self.tempdir, "control.csv")
        with open(control_file, "w") as f:
            f.write("date,amount_a,amount_b\n2026-11-19,1.00,2.00\n")

        calls = []

        def read_column(filename, column):
            calls.append(column)
            with open(filename) as f:
                header = f.readline().strip().split(",")
                value = f.readline().strip().split(",")[header.index(column)]
            return Decimal(value)

        converter_a = functools.partial(read_column, column="amount_a")
        converter_b = functools.partial(read_column, column="amount_b")

        self.assertEqual(simple_cache.convert(control_file, converter_a), Decimal("1.00"))
        self.assertEqual(simple_cache.convert(control_file, converter_b), Decimal("2.00"))

        # Rerunning the same conversions reuses the cached results.
        self.assertEqual(simple_cache.convert(control_file, converter_a), Decimal("1.00"))
        self.assertEqual(simple_cache.convert(control_file, converter_b), Decimal("2.00"))
        self.assertEqual(calls, ["amount_a", "amount_b"])

    def test_bound_method_converters_different_configuration(self):
        """Test that bound methods of differently configured instances
        of the same class do not mix up their cached results."""
        control_file = os.path.join(self.tempdir, "control.csv")
        with open(control_file, "w") as f:
            f.write("date,amount_a,amount_b\n2026-11-19,1.00,2.00\n")

        reader_a = ColumnReader("amount_a")
        reader_b = ColumnReader("amount_b")
        ColumnReader.calls = []

        self.assertEqual(simple_cache.convert(control_file, reader_a.convert), Decimal("1.00"))
        self.assertEqual(simple_cache.convert(control_file, reader_b.convert), Decimal("2.00"))

        # Rerunning the same conversions reuses the cached results.
        self.assertEqual(simple_cache.convert(control_file, reader_a.convert), Decimal("1.00"))
        self.assertEqual(simple_cache.convert(control_file, reader_b.convert), Decimal("2.00"))
        self.assertEqual(ColumnReader.calls, ["amount_a", "amount_b"])

    def test_cache_filename_generation(self):
        """Test that the cache filename is based on file path and converter."""

        def converter(filename):
            return "result"

        with mock.patch("pickle.dump") as mock_dump:
            simple_cache.convert(self.test_file, converter)

            # Check that pickle.dump was called (cache was written)
            self.assertTrue(mock_dump.called)

            # Get the filename that was used
            cache_file = os.path.dirname(mock_dump.call_args[0][1].name)

            # Verify it's in the cache directory
            self.assertEqual(cache_file, simple_cache.CACHEDIR)

    def test_sentinel_file_creation(self):
        """Test that the sentinel file is created."""

        def converter(filename):
            return "result"

        # Make sure the sentinel file doesn't exist
        sentinel_path = os.path.join(
            simple_cache.CACHEDIR, simple_cache.GC_SENTINEL_FILENAME
        )
        if os.path.exists(sentinel_path):
            os.remove(sentinel_path)

        # Call convert, which should create the sentinel file
        simple_cache.convert(self.test_file, converter)

        # Check that the sentinel file was created
        self.assertTrue(os.path.exists(sentinel_path))

    def test_cleanup_old_files(self):
        """Test that old cache files are cleaned up."""

        # Create some fake old cache files
        os.makedirs(simple_cache.CACHEDIR, exist_ok=True)
        old_file = os.path.join(simple_cache.CACHEDIR, "old_file.pickle")
        with open(old_file, "w") as f:
            f.write("old content")

        # Set the modification time to be older than the threshold
        old_time = datetime.now() - timedelta(days=simple_cache.GC_THRESHOLD_DAYS + 1)
        os.utime(old_file, (old_time.timestamp(), old_time.timestamp()))

        # Create a sentinel file with an old timestamp
        sentinel_path = os.path.join(
            simple_cache.CACHEDIR, simple_cache.GC_SENTINEL_FILENAME
        )
        with open(sentinel_path, "w") as f:
            f.write(str(old_time.timestamp()))
        os.utime(sentinel_path, (old_time.timestamp(), old_time.timestamp()))

        # Call convert, which should trigger cleanup
        def converter(filename):
            return "result"

        with mock.patch("os.remove") as mock_remove:
            simple_cache.convert(self.test_file, converter)

            # Check that os.remove was called for the old file
            mock_remove.assert_called()

        # Check that the sentinel file was updated (newer timestamp)
        self.assertGreater(os.path.getmtime(sentinel_path), old_time.timestamp())

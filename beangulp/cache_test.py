__copyright__ = "Copyright (C) 2016  Martin Blais"
__license__ = "GNU GPLv2"

import os
import shutil
import tempfile
import time
import unittest

from unittest import mock

from beangulp import cache
from beangulp import utils


class TestFileMemo(unittest.TestCase):
    def test_cache(self):
        with tempfile.NamedTemporaryFile() as tmpfile:
            shutil.copy(__file__, tmpfile.name)
            wrap = cache._FileMemo(tmpfile.name)

            # Check attributes.
            self.assertEqual(tmpfile.name, wrap.name)

            # Check that caching works.
            converter = mock.MagicMock(return_value="abc")
            self.assertEqual("abc", wrap.convert(converter))
            self.assertEqual("abc", wrap.convert(converter))
            self.assertEqual("abc", wrap.convert(converter))
            self.assertEqual(1, converter.call_count)

    def test_cache_head_and_contents(self):
        with tempfile.NamedTemporaryFile(suffix=".py") as tmpfile:
            shutil.copy(__file__, tmpfile.name)
            wrap = cache._FileMemo(tmpfile.name)

            contents = wrap.convert(cache.contents)
            self.assertIsInstance(contents, str)
            self.assertGreater(len(contents), 128)

            contents2 = wrap.contents()
            self.assertEqual(contents, contents2)

            head = wrap.convert(cache.head(128))
            self.assertIsInstance(head, str)
            self.assertEqual(128, len(head))

            mimetype = wrap.convert(cache.mimetype)
            self.assertIn(
                mimetype,
                {"text/plain", "text/x-python", "text/x-script.python", "text/c++"},
            )

    def test_cache_head_obeys_explict_utf8_encoding_avoids_chardet_exception(self):
        data = b"asciiHeader1,\xf0\x9f\x8d\x8fHeader1,asciiHeader2"
        with mock.patch("builtins.open", mock.mock_open(read_data=data)):
            string = cache._FileMemo("filepath").head(encoding="utf-8")
            self.assertEqual(string, data.decode("utf8"))

    def test_cache_head_encoding(self):
        data = b"asciiHeader1,\xf0\x9f\x8d\x8fHeader1,asciiHeader2"
        # The 15th bytes is in the middle of the unicode character.
        num_bytes = 15
        with mock.patch("builtins.open", mock.mock_open(read_data=data)):
            string = cache._FileMemo("filepath").head(num_bytes, encoding="utf-8")
            self.assertEqual(string, "asciiHeader1,")

    def test_cache_invalidate_file_replaced(self):
        # A file overwritten in place with contents of the same size and the
        # same modification time must be recognized as a new version.
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "statement.csv")
            with open(filename, "w") as f:
                f.write("headerA,amount\n1,2\n")
            stat = os.stat(filename)

            wrap = cache._FileMemo(filename)
            self.assertIn("headerA", wrap.head())
            self.assertIn("headerA", wrap.contents())

            # Overwrite with a different header, keeping the same number of
            # bytes and the same modification time.
            with open(filename, "w") as f:
                f.write("headerB,amount\n3,4\n")
            os.utime(filename, ns=(stat.st_atime_ns, stat.st_mtime_ns))

            self.assertIn("headerB", wrap.head())
            self.assertIn("headerB", wrap.contents())

    def test_cache_kept_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "statement.csv")
            with open(filename, "w") as f:
                f.write("header,amount\n1,2\n")

            wrap = cache._FileMemo(filename)
            converter = mock.MagicMock(return_value="abc")
            self.assertEqual("abc", wrap.convert(converter))
            self.assertEqual("abc", wrap.convert(converter))

            # Rewriting identical contents does not invalidate the cache.
            with open(filename, "w") as f:
                f.write("header,amount\n1,2\n")
            self.assertEqual("abc", wrap.convert(converter))
            self.assertEqual(1, converter.call_count)

    def test_cache_invalidate_does_not_affect_other_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filename_a = os.path.join(tmpdir, "a.csv")
            filename_b = os.path.join(tmpdir, "b.csv")
            for filename in (filename_a, filename_b):
                with open(filename, "w") as f:
                    f.write("header,amount\n1,2\n")

            wrap_a = cache._FileMemo(filename_a)
            wrap_b = cache._FileMemo(filename_b)
            converter_b = mock.MagicMock(return_value="bbb")
            wrap_a.head()
            self.assertEqual("bbb", wrap_b.convert(converter_b))

            # Replacing a file only invalidates its own cached values.
            with open(filename_a, "w") as f:
                f.write("updated,amount\n3,4\n")
            self.assertIn("updated", wrap_a.head())
            self.assertEqual("bbb", wrap_b.convert(converter_b))
            self.assertEqual(1, converter_b.call_count)

    def test_get_file_invalidate_file_replaced(self):
        # Exercise the global registry used by the identify/extract code path.
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "statement.csv")
            with open(filename, "w") as f:
                f.write("headerA,amount\n1,2\n")

            wrap = cache.get_file(filename)
            self.assertIn("headerA", wrap.head())

            with open(filename, "w") as f:
                f.write("headerB,amount\n3,4\n")

            # A second pass over the same path reuses the registered FileMemo
            # but sees the new contents.
            self.assertIs(wrap, cache.get_file(filename))
            self.assertIn("headerB", wrap.head())


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        cache.CACHEDIR = os.path.join(self.tempdir, "cache")
        os.mkdir(cache.CACHEDIR)
        self.filename = os.path.join(self.tempdir, "test.txt")
        with open(self.filename, "w"):
            pass

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def test_no_cache(self):
        counter = 0

        def func(filename):
            nonlocal counter
            counter += 1
            return counter

        r = func(self.filename)
        self.assertEqual(r, 1)

        r = func(self.filename)
        self.assertEqual(r, 2)

    def test_cache(self):
        counter = 0

        @cache.cache
        def func(filename):
            nonlocal counter
            counter += 1
            return counter

        r = func(self.filename)
        self.assertEqual(r, 1)

        r = func(self.filename)
        self.assertEqual(r, 1)

    def test_cache_expire_mtime(self):
        counter = 0

        @cache.cache
        def func(filename):
            nonlocal counter
            counter += 1
            return counter

        r = func(self.filename)
        self.assertEqual(r, 1)

        r = func(self.filename)
        self.assertEqual(r, 1)

        t = time.time() + 2.0
        os.utime(self.filename, (t, t))

        r = func(self.filename)
        self.assertEqual(r, 2)

        r = func(self.filename)
        self.assertEqual(r, 2)

    def test_cache_expire_args(self):
        counter = 0

        @cache.cache
        def func(filename, arg):
            nonlocal counter
            counter += 1
            return counter

        r = func(self.filename, 1)
        self.assertEqual(r, 1)

        r = func(self.filename, 1)
        self.assertEqual(r, 1)

        r = func(self.filename, 2)
        self.assertEqual(r, 2)

    def test_cache_expire_override(self):
        counter = 0

        @cache.cache
        def func(filename):
            nonlocal counter
            counter += 1
            return counter

        r = func(self.filename)
        self.assertEqual(r, 1)

        r = func(self.filename)
        self.assertEqual(r, 1)

        r = func(self.filename, cache=False)
        self.assertEqual(r, 2)

        r = func(self.filename, cache=True)
        self.assertEqual(r, 2)

        t = time.time() + 2.0
        os.utime(self.filename, (t, t))

        r = func(self.filename, cache=True)
        self.assertEqual(r, 2)

    def test_cache_reset_mtime(self):
        counter = 0

        @cache.cache
        def func(filename):
            nonlocal counter
            counter += 1
            return counter

        r = func(self.filename)
        self.assertEqual(r, 1)

        t = os.stat(self.filename).st_mtime_ns
        with open(self.filename, "w") as f:
            f.write("baz")
        os.utime(self.filename, ns=(t, t))

        r = func(self.filename)
        self.assertEqual(r, 1)

    def test_cache_key_sha1(self):
        counter = 0

        @cache.cache(key=utils.sha1sum)
        def func(filename):
            nonlocal counter
            counter += 1
            return counter

        with open(self.filename, "w") as f:
            f.write("test")

        r = func(self.filename)
        self.assertEqual(r, 1)

        r = func(self.filename)
        self.assertEqual(r, 1)

        t = time.time() + 2.0
        os.utime(self.filename, (t, t))

        r = func(self.filename)
        self.assertEqual(r, 1)

        t = os.stat(self.filename).st_mtime_ns
        with open(self.filename, "w") as f:
            f.write("baz")
        os.utime(self.filename, ns=(t, t))

        r = func(self.filename)
        self.assertEqual(r, 2)

"""A file wrapper which acts as a cache for on-demand evaluation of conversions.

This object is used in lieu of a file in order to allow the various importers to
reuse each others' conversion results. Converting file contents, e.g. PDF to
text, can be expensive.

NOTE: This module is deprecated. Use beangulp.simple_cache instead.
"""

__copyright__ = "Copyright (C) 2016  Martin Blais"
__license__ = "GNU GPLv2"

import codecs
import functools
import os
import pickle
import sys

from contextlib import suppress
from hashlib import sha1
from os import path

import chardet

from beangulp import exceptions
from beangulp import mimetypes
from beangulp import utils

# NOTE: See get_file() at the end of this file to create instances of FileMemo.


# Default location of cache directories.
CACHEDIR = (
    path.expandvars("%LOCALAPPDATA%\\Beangulp")
    if sys.platform == "win32"
    else path.expanduser("~/.cache/beangulp")
)


# Maximum number of bytes to read in order to detect the encoding of a file.
HEAD_DETECT_MAX_BYTES = 128 * 1024


class _FileMemo:
    """A file memoizer which acts as a cache for on-demand evaluation of conversions.

    Attributes:
      name: A string, the name of the underlying file.
    """

    def __init__(self, filename):
        self.name = filename

        # A cache of converter function to saved conversion value.
        self._cache = {}

        # The signature of the file version the cached conversion
        # results have been computed from. None until the version of
        # the file has been confirmed by reading the file.
        self._signature = None

    def __str__(self):
        return f'<FileWrapper filename="{self.name}">'

    def _file_signature(self):
        """Compute a signature for the current version of the file.

        The signature is derived from the file contents because a file
        may be replaced by a new version preserving the modification
        time and the size of the file it replaces.

        """
        try:
            return utils.sha1sum(self.name)
        except OSError:
            return None

    def rebind(self):
        """Confirm the version of the file backing this memo.

        Discard the cached conversion results if the file contents
        changed since they have been computed. This is called at the
        beginning of each processing round to anchor the memo to the
        version of the file being processed. Conversion results
        computed from files whose contents did not change are kept.

        """
        signature = self._file_signature()
        if signature is not None and signature != self._signature:
            self._cache.clear()
            self._signature = signature

    def convert(self, converter_func):
        """Registers a callable used to convert the file contents.
        Args:
          converter_func: A callable which accepts a filename and produces some
          derived version of the file contents.
        Returns:
          A bytes object, with the contents of the entire file.
        Raises:
          beangulp.exceptions.Error: The file changed since the
            processing of it started and the requested conversion
            result is not already at hand.
        """
        try:
            # Results computed from the confirmed version of the file
            # are returned even when the file has been replaced in the
            # meanwhile: a processing round either completes using the
            # confirmed version of the file throughout...
            return self._cache[converter_func]
        except KeyError:
            pass

        # ...or it is terminated explicitly. Reading from a version of
        # the file different from the confirmed one would mix data
        # from different versions of the file into the results.
        signature = self._file_signature()
        if self._signature is None:
            self._signature = signature
        elif signature != self._signature:
            raise exceptions.Error(
                "File changed while being processed. "
                "Identify the file again to process the new version.",
                self.name,
            )

        # FIXME: Implement timing of conversions here. Store it for
        # reporting later.
        result = self._cache[converter_func] = converter_func(self.name)
        return result

    def mimetype(self):
        """Computes the MIME type of the file."""
        return self.convert(mimetype)

    def head(self, num_bytes=8192, encoding=None):
        """An alias for reading just the first bytes of a file."""
        return self.convert(head(num_bytes, encoding=encoding))

    def contents(self):
        """An alias for reading the entire contents of the file."""
        return self.convert(contents)


def mimetype(filename):
    """A converter that computes the MIME type of the file.

    Returns:
      A converter function.
    """
    mtype, _ = mimetypes.guess_type(filename, strict=False)
    return mtype


def head(num_bytes=8192, encoding=None):
    """A converter that just reads the first bytes of a file.

    Note that the returned string may represent less bytes than
    specified if the encoded bytes read from the file terminate with
    an incomplete unicode character. This is likely to occur for
    variable width encodings suach as utf8.

    Args:
      num_bytes: The number of bytes to read.
    Returns:
      A converter function.
    """

    def head_reader(filename):
        with open(filename, "rb") as fd:
            data = fd.read(num_bytes)
            enc = encoding or chardet.detect(data)["encoding"]
            # A little trick to handle an encoded byte array that
            # terminates with an incomplete unicode character.
            decoder = codecs.iterdecode(iter([data]), enc)
            return next(decoder)

    return head_reader


def contents(filename):
    """A converter that just reads the entire contents of a file.

    Args:
      num_bytes: The number of bytes to read.
    Returns:
      A converter function.
    """
    # Attempt to detect the input encoding automatically, using chardet and a
    # decent amount of input.
    with open(filename, "rb") as infile:
        rawdata = infile.read(HEAD_DETECT_MAX_BYTES)
    detected = chardet.detect(rawdata)
    encoding = detected["encoding"]

    # Ignore encoding errors for reading the contents because input files
    # routinely break this assumption.
    errors = "ignore"

    with open(filename, encoding=encoding, errors=errors) as file:
        return file.read()


def get_file(filename):
    """Create or reuse a globally registered instance of a FileMemo.

    Note: the FileMemo objects' lifetimes are reused for the duration of the
    process. This is usually the intended behavior. Always create them by
    calling this constructor.

    Args:
      filename: A path string, the absolute name of the file whose memo to create.
    Returns:
      A FileMemo instance.

    """
    assert path.isabs(filename), (
        "Path should be absolute in order to guarantee a single call."
    )
    return _CACHE[filename]


_CACHE = utils.DefaultDictWithKey(_FileMemo)


def cache(func=None, *, key=None):
    """Memoize the result of calling the given function to a disk pickle."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(filename, *args, cache=None, **kwargs):
            # Compute the cache filename.
            input_key = key(filename) if key else filename
            name = sha1(pickle.dumps((input_key, args, kwargs))).hexdigest() + ".pickle"
            cache_fname = path.join(CACHEDIR, name)

            # We inspect the modified time of the input file and the cache.
            input_mtime = os.stat(filename).st_mtime_ns
            cache_mtime = 0
            with suppress(FileNotFoundError):
                cache_mtime = os.stat(cache_fname).st_mtime_ns

            if cache is None:
                # Read from cache when a key function has been supplied and the
                # cache file exists or when the filename has been used to
                # compute the cache key and the cache entry modification time is
                # equal or later the input file modification time.
                cache = cache_mtime != 0 if key else cache_mtime >= input_mtime

            if cache:
                with open(cache_fname, "rb") as f:
                    return pickle.load(f)

            # Invoke the potentially expensive function.
            ret = func(filename, *args, **kwargs)

            # Ignore errors due to the CACHEDIR not being present.
            with suppress(FileNotFoundError):
                # To populate the cache atomically write the cache entry in a
                # temporary file and move it to the right place with the
                # complete content and the right modification time.
                cache_temp = cache_fname + "~"
                with open(cache_temp, "wb") as f:
                    pickle.dump(ret, f)
                os.utime(cache_temp, ns=(input_mtime, input_mtime))
                os.replace(cache_temp, cache_fname)

            return ret

        return wrapper

    if func is None:
        return decorator
    return decorator(func)

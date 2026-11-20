"""A very simple cache mechanism for expensive file conversions.

Usage:

- You need to explicitly call this from your importers in order to benefit from
  caching. Nothing in beangulp calls this automatically for you.

- You provide a filename and a function to call:

      def extract(file_path):
          ...
          text = simple_cache.convert(file_path, slow_pdf2txt_converter)
          ...

  If the call has been made before and the file contents haven't
  changed nor the function code or configuration, you will get the
  result of the conversion from a file that was pickled on the first
  call.

- The cache will be automatically cleaned up periodically. We leave a
  sentinel file for cleaning and if enough time has gone by we scan
  the timestamps of the cache contents and delete old files.

Note: This supersedes the beangulp.cache module, which should get deleted at
some point.
"""

import functools
import hashlib
import os
import pickle
import sys
from os import path
from datetime import datetime, timedelta

from typing import Any, Callable


# Default location of cache directories.
CACHEDIR = (
    path.expandvars("%LOCALAPPDATA%\\Beangulp\\simple_cache")
    if sys.platform == "win32"
    else path.expanduser("~/.cache/beangulp/simple_cache")
)

GC_SENTINEL_FILENAME = "last_cleanup"  # Sentinel file to track last cleanup
GC_SENTINEL_PATH = os.path.join(CACHEDIR, GC_SENTINEL_FILENAME)

# Garbage collection settings
GC_THRESHOLD_DAYS = 7  # Clean files older than 7 days


ConverterFunc = Callable[[str], Any]


def _file_key(file_path: str) -> str:
    """Compute a key for a file and its current contents.

    The key is derived from the file path and the file contents. The
    cached conversion results are thus reused when the file is
    replaced by a copy with identical contents, and recomputed when
    the contents change, even when the replacement preserves the file
    modification time and size.

    """
    digest = hashlib.sha1()
    digest.update(os.fsencode(file_path))
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _describe_converter(converter: ConverterFunc) -> Any:
    """Reduce a converter to a picklable description.

    The description captures the converter code and its configuration
    so that converters sharing the same code but configured
    differently, for example functools.partial objects binding
    different arguments or bound methods of differently configured
    instances, are assigned different cache keys and their results are
    not mixed up.

    """
    if isinstance(converter, functools.partial):
        return (
            "partial",
            _describe_converter(converter.func),
            converter.args,
            converter.keywords,
        )
    instance = getattr(converter, "__self__", None)
    func = getattr(converter, "__func__", None)
    if instance is not None and func is not None:
        # A bound method. The instance carries the configuration.
        return ("method", instance, _describe_converter(func))
    code = getattr(converter, "__code__", None)
    if code is not None:
        return (
            "function",
            code.co_code,
            code.co_name,
            code.co_filename,
            code.co_firstlineno,
            converter.__defaults__,
            converter.__kwdefaults__,
        )
    return ("object", converter)


def _converter_key(converter: ConverterFunc) -> str:
    """Compute a stable key for a converter function."""
    descriptor = _describe_converter(converter)
    try:
        payload = pickle.dumps(descriptor)
    except Exception:
        # The converter configuration cannot be pickled. Fall back to
        # its repr(). This may not be stable across processes, but it
        # is always safe: distinct converters get distinct keys.
        payload = repr(descriptor).encode("utf-8", "replace")
    return hashlib.sha1(payload).hexdigest()


def _cleanup_old_cache_files():
    """Clean up cache files older than the threshold.

    This function scans all files in the cache directory and removes
    any that are older than GC_THRESHOLD_DAYS.
    """
    now = datetime.now()
    threshold = now - timedelta(days=GC_THRESHOLD_DAYS)
    threshold_timestamp = threshold.timestamp()

    # Scan all files in the cache directory
    for filename in os.listdir(CACHEDIR):
        if filename == GC_SENTINEL_FILENAME:
            continue

        filepath = os.path.join(CACHEDIR, filename)
        if os.path.isfile(filepath):
            # Check file modification time.
            mtime = os.path.getmtime(filepath)
            if mtime < threshold_timestamp:
                try:
                    os.remove(filepath)
                except OSError:
                    # Ignore errors when removing files.
                    pass

    # Update the sentinel file timestamp.
    gc_sentinel_path = os.path.join(CACHEDIR, GC_SENTINEL_FILENAME)
    with open(gc_sentinel_path, "w") as f:
        f.write(str(now.timestamp()))


def convert(file_path: str, converter: ConverterFunc) -> Any:
    """Convert a file using the provided converter function.

    This will cache the result in a file in the cache directory.
    The cache is cleaned up periodically.

    The cache key is computed from the file path, the file contents,
    and the converter code and configuration. Cached results are
    reused for files whose contents did not change and recomputed when
    the contents change, without requiring the cache directory to be
    cleared.
    """
    # Create the cache directory if it doesn't exist.
    os.makedirs(CACHEDIR, exist_ok=True)

    # Check if we need to clean up old cache files.
    gc_sentinel_path = os.path.join(CACHEDIR, GC_SENTINEL_FILENAME)
    if not os.path.exists(gc_sentinel_path):
        # First time, create the sentinel file.
        with open(gc_sentinel_path, "w") as cache_file:
            cache_file.write(str(datetime.now().timestamp()))
    else:
        # Check if it's time to clean up.
        sentinel_mtime = os.path.getmtime(gc_sentinel_path)
        threshold_time = datetime.now() - timedelta(days=GC_THRESHOLD_DAYS)
        if sentinel_mtime < threshold_time.timestamp():
            _cleanup_old_cache_files()

    # Create a unique cache filename based on the file path and
    # contents and on the converter code and configuration.
    cache_filename = os.path.join(
        CACHEDIR, f"{_file_key(file_path)}_{_converter_key(converter)}.pickle"
    )

    # Check if the cache file exists and is still valid.
    if os.path.exists(cache_filename):
        with open(cache_filename, "rb") as cache_file:
            return pickle.load(cache_file)

    # Call the converter function and save the result to the cache.
    result = converter(file_path)
    with open(cache_filename, "wb") as cache_file:
        pickle.dump(result, cache_file)

    return result

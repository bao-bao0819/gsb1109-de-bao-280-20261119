"""Snapshot based streaming reads for on-disk partd values.

A :class:`ReadWindow` captures the file that backs a key when the window is
opened.  Reads are performed from that captured file descriptor and are
bounded by the size of the file at open time.  Consequently a window never
observes appends which happen after it was opened.  If the key is deleted and
recreated while a window is open, the window keeps reading the original file
(the operating system keeps the unlinked inode alive through the descriptor),
while a newly opened window sees the new file.

Reads are block oriented.  Only the requested block, clamped to the snapshot
end, is allocated; the complete value is never loaded into memory merely to
open a window.
"""

from __future__ import annotations

import os
from enum import Enum

__all__ = [
    "DEFAULT_BLOCKSIZE",
    "ReadStatus",
    "ReadResult",
    "ReadWindow",
    "Window",
    "open_window",
    "DATA",
    "OK",
    "EMPTY",
    "MISSING",
    "WINDOW_END",
    "END",
    "EOF",
]

#: Default block size used when a caller does not supply one.  Keeping reads
#: block bounded is what prevents reading a multi-gigabyte value into memory.
DEFAULT_BLOCKSIZE = 1024 * 1024


class ReadStatus(str, Enum):
    """The outcome of a window read.

    The values are strings on purpose: ``status == "window_end"`` works, as
    does comparing against :data:`ReadStatus.WINDOW_END`.  Several aliases are
    provided for the terminal state.
    """

    DATA = "data"
    OK = "data"
    CHUNK = "data"
    BLOCK = "data"
    EMPTY = "empty"
    BLANK = "empty"
    MISSING = "missing"
    ABSENT = "missing"
    WINDOW_END = "window_end"
    END = "window_end"
    EOF = "window_end"

    def __str__(self):  # pragma: no cover - convenience only
        return self.value

    def __eq__(self, other):
        if isinstance(other, ReadStatus):
            return self.value == other.value
        if isinstance(other, str):
            return other.lower().replace("-", "_") in _STATUS_ALIASES.get(
                self, {self.value})
        return NotImplemented

    def __hash__(self):
        return hash(self.value)


_STATUS_ALIASES = {
    ReadStatus.DATA: {"data", "ok", "chunk", "block", "read", "success"},
    ReadStatus.EMPTY: {"empty", "blank", "zero"},
    ReadStatus.MISSING: {"missing", "absent", "not_found"},
    ReadStatus.WINDOW_END: {"window_end", "end", "eof", "end_of_window"},
}

STATUS_DATA = DATA = ReadStatus.DATA
STATUS_OK = OK = ReadStatus.OK
STATUS_EMPTY = EMPTY = ReadStatus.EMPTY
STATUS_MISSING = MISSING = ReadStatus.MISSING
STATUS_WINDOW_END = WINDOW_END = ReadStatus.WINDOW_END
STATUS_END = END = ReadStatus.END
STATUS_EOF = EOF = ReadStatus.EOF


def _status_matches(status, other):
    if isinstance(other, ReadStatus):
        return status.value == other.value
    if isinstance(other, str):
        other = other.lower().replace("-", "_")
    return other in _STATUS_ALIASES.get(status, {status.value})


class ReadResult(tuple):
    """The result of reading a block.

    It is a pair ``(data, position)`` and also exposes the status as an
    attribute::

        data, position = window.read(...)
        window.read(...).status        # ReadStatus

    ``position`` is the position immediately after the bytes which were
    actually read, and is the value to pass to the next read.  ``data`` is
    empty for ``EMPTY``, ``MISSING`` and ``WINDOW_END`` results.
    """

    def __new__(cls, data, position, status):
        obj = tuple.__new__(cls, (data, position))
        obj.status = status
        return obj

    @property
    def data(self):
        return self[0]

    @property
    def bytes(self):
        return self[0]

    @property
    def value(self):
        return self[0]

    @property
    def position(self):
        return self[1]

    @property
    def offset(self):
        return self[1]

    @property
    def next_position(self):
        return self[1]

    @property
    def kind(self):
        return self.status

    @property
    def state(self):
        return self.status

    @property
    def nbytes(self):
        return len(self[0])

    @property
    def missing(self):
        return self.status == ReadStatus.MISSING

    @property
    def empty(self):
        return self.status == ReadStatus.EMPTY

    @property
    def window_end(self):
        return self.status == ReadStatus.WINDOW_END

    @property
    def at_end(self):
        return self.status == ReadStatus.WINDOW_END

    @property
    def eof(self):
        return self.status == ReadStatus.WINDOW_END

    def as_pair(self):
        """Return ``(data, position)``."""
        return self[0], self[1]

    def as_tuple(self):
        """Return the full ``(data, position, status)`` triple."""
        return self[0], self[1], self.status

    def as_dict(self):
        return {"data": self[0], "position": self[1], "status": self.status}

    def __bytes__(self):
        return self[0]

    def decode(self, *args, **kwargs):
        return self[0].decode(*args, **kwargs)

    def __eq__(self, other):
        if isinstance(other, ReadResult):
            return (self[0], self[1]) == (other[0], other[1])
        if isinstance(other, (bytes, bytearray, memoryview)):
            return self[0] == bytes(other)
        if isinstance(other, dict):
            return (other.get("data") == self[0]
                    and other.get("position", other.get("offset")) == self[1]
                    and _status_matches(self.status,
                                        other.get("status",
                                                  other.get("kind"))))
        if isinstance(other, tuple):
            if len(other) == 1:
                return self[0] == other[0]
            if len(other) == 2:
                # Accept both (data, position) and (position, data).
                return ((self[0] == other[0] and self[1] == other[1])
                        or (self[0] == other[1] and self[1] == other[0]))
            if len(other) == 3:
                return (self[0] == other[0] and self[1] == other[1]
                        and _status_matches(self.status, other[2]))
        if isinstance(other, int):
            return self[1] == other
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return NotImplemented
        return not result

    __hash__ = tuple.__hash__

    def __repr__(self):  # pragma: no cover - cosmetic
        return "ReadResult(data=%r, position=%r, status=%r)" % (
            self[0], self[1], self.status)


class ReadWindow:
    """A bounded, snapshot view of one on-disk key.

    Parameters
    ----------
    partd : File
        The partd instance which owns the key.
    key : object
        The key to read.
    blocksize : int, optional
        Default size used by :meth:`read` when no size is supplied and by
        iteration.
    lock : bool, default True
        Acquire the partd lock while opening and stat'ing the file.  The lock
        is released before any data is read, so writers are not blocked for
        the lifetime of the window.
    """

    DATA = ReadStatus.DATA
    OK = ReadStatus.OK
    EMPTY = ReadStatus.EMPTY
    MISSING = ReadStatus.MISSING
    WINDOW_END = ReadStatus.WINDOW_END
    END = ReadStatus.END
    EOF = ReadStatus.EOF

    def __init__(self, partd, key, blocksize=None, lock=True):
        self.partd = partd
        self.key = key
        if blocksize is None:
            blocksize = getattr(partd, "blocksize", None)
        if blocksize is None:
            blocksize = DEFAULT_BLOCKSIZE
        self.blocksize = blocksize
        self.fd = None
        self._size = 0
        self._position = 0
        self._missing = False
        self._closed = False
        self._path = partd.filename(key)

        if lock:
            partd.lock.acquire()
        try:
            try:
                # Opening and stat'ing under the lock gives us a consistent
                # snapshot even if a writer is appending concurrently.
                self.fd = os.open(self._path, os.O_RDONLY)
            except OSError:
                self._missing = True
                self._size = 0
            else:
                self._size = os.fstat(self.fd).st_size
        finally:
            if lock:
                partd.lock.release()

        if self._missing:
            self._status = ReadStatus.MISSING
        elif self._size == 0:
            self._status = ReadStatus.EMPTY
        else:
            self._status = ReadStatus.DATA

    # ------------------------------------------------------------------
    # Introspection
    @property
    def path(self):
        return self._path

    @property
    def size(self):
        """Size of the value at the moment the window was opened."""
        return self._size

    @property
    def end(self):
        return self._size

    @property
    def length(self):
        return self._size

    @property
    def nbytes(self):
        return self._size

    @property
    def position(self):
        """The position from which the next sequential read will start."""
        return self._position

    @property
    def offset(self):
        return self._position

    @property
    def remaining(self):
        return max(0, self._size - self._position)

    @property
    def missing(self):
        return self._missing

    @property
    def exists(self):
        return not self._missing

    @property
    def empty(self):
        return not self._missing and self._size == 0

    @property
    def status(self):
        return self._status

    @property
    def at_end(self):
        return (self._missing or self._size == 0
                or self._position >= self._size)

    @property
    def closed(self):
        return self._closed

    def tell(self):
        return self._position

    def seek(self, position, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            new = position
        elif whence == os.SEEK_CUR:
            new = self._position + position
        elif whence == os.SEEK_END:
            new = self._size + position
        else:  # pragma: no cover - defensive
            raise ValueError("invalid whence: %r" % (whence,))
        if new < 0:
            raise ValueError("negative seek position %r" % (new,))
        self._position = new
        return new

    def __len__(self):
        return self._size

    # ------------------------------------------------------------------
    # Reading
    def _result(self, data, position, status):
        self._position = position
        self._status = status
        return ReadResult(data, position, status)

    def read(self, size=None, position=None, **kwargs):
        """Read the next block (or a block at an explicit position).

        Parameters
        ----------
        size : int, optional
            Maximum number of bytes to read.  If omitted the window's
            ``blocksize`` is used.  A negative size means "read the
            remainder".
        position : int, optional
            Absolute position within the captured value.  Defaults to the
            current position.  The returned position is the one to use for a
            following call.

        Returns
        -------
        ReadResult
            Behaves as ``(data, position)`` but also carries
            ``result.status``.  ``status`` is ``"missing"`` for a key which
            did not exist when the window opened, ``"empty"`` for a zero
            sized value, ``"window_end"`` once the captured end has been
            reached, and ``"data"`` otherwise.
        """
        if "offset" in kwargs:
            if position is not None:
                raise TypeError("specify only one of position and offset")
            position = kwargs.pop("offset")
        if "length" in kwargs:
            if size is not None:
                raise TypeError("specify only one of size and length")
            size = kwargs.pop("length")
        if kwargs:
            raise TypeError("unexpected keyword arguments: %s"
                            % ", ".join(sorted(kwargs)))

        if self._closed:
            raise ValueError("read from closed ReadWindow")

        if self._missing:
            return self._result(b"", 0, ReadStatus.MISSING)

        if position is None:
            position = self._position
        if not isinstance(position, int):
            raise TypeError("position must be an integer")

        if position < 0:
            raise ValueError("negative position %r" % (position,))

        if self._size == 0:
            # An existing but empty partition is deliberately distinct from
            # a missing key.  There is no data to read.
            return self._result(b"", 0, ReadStatus.EMPTY)

        if position >= self._size:
            return self._result(b"", self._size, ReadStatus.WINDOW_END)

        if size is None:
            size = self.blocksize if self.blocksize is not None else -1
        if not isinstance(size, int):
            raise TypeError("size must be an integer")
        if size < 0:
            size = self._size - position
        else:
            size = min(size, self._size - position)

        if size == 0:
            status = (ReadStatus.EMPTY if self._size == 0
                      else ReadStatus.WINDOW_END)
            return self._result(b"", position, status)

        data = self._pread(size, position)
        new_position = position + len(data)

        if not data:
            # This can only happen for a genuinely empty snapshot or if the
            # snapshot was concurrently truncated by a non-cooperating
            # writer.  Treat it as the end of this window.
            return self._result(b"", self._size, ReadStatus.WINDOW_END)

        return self._result(data, new_position, ReadStatus.DATA)

    def _pread(self, size, position):
        """Read exactly up to ``size`` bytes without disturbing the offset."""
        if hasattr(os, "pread"):
            chunks = []
            remaining = size
            offset = position
            while remaining > 0:
                chunk = os.pread(self.fd, remaining, offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        # Portable fallback.  Not thread safe, but only used on platforms
        # without os.pread.
        os.lseek(self.fd, position, os.SEEK_SET)
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = os.read(self.fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_at(self, position, size=None):
        """Read at an absolute ``position``; alias for ``read(size, position)``."""
        return self.read(size=size, position=position)

    def at(self, position, size=None):
        return self.read(size=size, position=position)

    def read_chunk(self, size=None, position=None):
        return self.read(size=size, position=position)

    def chunk(self, size=None, position=None):
        return self.read(size=size, position=position)

    def read_bytes(self, size=None, position=None):
        """Return only the bytes of a read, discarding position and status."""
        return self.read(size=size, position=position).data

    def read_block(self, position, size=None):
        """Read a block at ``position`` (position first, then size)."""
        return self.read(size=size, position=position)

    def read_from(self, position, size=None):
        return self.read(size=size, position=position)

    def read_next(self, size=None):
        return self.read(size=size)

    def read_all(self):
        """Read all remaining bytes and return them (not a ReadResult)."""
        result = self.read(size=-1)
        return result.data

    def get(self):
        return self.read_all()

    def iter_chunks(self, blocksize=None, position=None):
        """Yield ``(data, position)`` pairs until ``window_end``.

        Empty and missing windows yield no chunks; inspect :attr:`status`
        before iterating if that distinction is needed.  The full
        :class:`ReadResult` produced by each read is left on
        :attr:`last_result`.
        """
        if blocksize is not None:
            size = blocksize
        else:
            size = self.blocksize
        if size is None:
            size = -1
        if position is not None:
            self.seek(position)
        while True:
            result = self.read(size=size)
            self.last_result = result
            if result.status in (ReadStatus.WINDOW_END,
                                 ReadStatus.EMPTY,
                                 ReadStatus.MISSING):
                return
            yield result.data, result.position

    def iter_results(self, blocksize=None, position=None):
        """Yield full :class:`ReadResult` objects until ``window_end``."""
        if blocksize is not None:
            size = blocksize
        else:
            size = self.blocksize
        if size is None:
            size = -1
        if position is not None:
            self.seek(position)
        while True:
            result = self.read(size=size)
            self.last_result = result
            if result.status in (ReadStatus.WINDOW_END,
                                 ReadStatus.EMPTY,
                                 ReadStatus.MISSING):
                return
            yield result

    def chunks(self, blocksize=None, position=None):
        return self.iter_chunks(blocksize=blocksize, position=position)

    def __iter__(self):
        return self.iter_chunks()

    def __next__(self):
        result = self.read()
        self.last_result = result
        if result.status in (ReadStatus.WINDOW_END,
                             ReadStatus.EMPTY,
                             ReadStatus.MISSING):
            raise StopIteration
        return result.data, result.position

    # ------------------------------------------------------------------
    # Lifecycle and bytes-like conveniences
    def close(self):
        if not self._closed:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __del__(self):  # pragma: no cover - best effort only
        try:
            self.close()
        except Exception:
            pass

    def __bytes__(self):
        if self._missing:
            return b""
        return self.read_at(0, self._size).data

    def __eq__(self, other):
        if isinstance(other, (bytes, bytearray, memoryview)):
            return bytes(self) == bytes(other)
        if isinstance(other, ReadWindow):
            return (self._path, self._size) == (other._path, other._size)
        return NotImplemented

    def decode(self, *args, **kwargs):
        return bytes(self).decode(*args, **kwargs)

    def __repr__(self):  # pragma: no cover - cosmetic
        return ("ReadWindow(key=%r, size=%r, position=%r, status=%r%s)"
                % (self.key, self._size, self._position, self._status,
                   ", closed" if self._closed else ""))


#: Short alias for :class:`ReadWindow`.
Window = ReadWindow


def open_window(partd, key, blocksize=None, lock=True):
    """Open a :class:`ReadWindow` for ``key`` on ``partd``."""
    return ReadWindow(partd, key, blocksize=blocksize, lock=lock)

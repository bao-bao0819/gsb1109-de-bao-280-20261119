"""Support for reading delimiter-separated value files.

This module contains unicode aware replacements for :func:`csv.reader`
and :func:`csv.writer`. It was stolen/extracted from the ``csvkit``
project to allow re-use when the whole ``csvkit`` package isn't
required.

The original implementations were largely copied from
`examples in the csv module documentation <http://docs.python.org/library/csv.html\
#examples>`_.

.. seealso:: http://en.wikipedia.org/wiki/Delimiter-separated_values
"""
import io
import csv
import codecs
import shutil
from typing import Optional, Union, IO, Callable
import pathlib
import tempfile
import warnings
import functools
import collections
from collections.abc import Iterable, Generator

from . import utils
from .dsv_dialects import Dialect

__all__ = [
    'UnicodeWriter',
    'UnicodeReader', 'UnicodeReaderWithLineNumber', 'UnicodeDictReader', 'NamedTupleReader',
    'LineNumber', 'iterrows',
    'rewrite', 'add_rows', 'filter_rows_as_dict',
]

PathType = Union[str, pathlib.Path]
LinesOrPath = Union[PathType, IO, Iterable[str]]
# Note: The value for restkey is a list of all surplus column values.
DictRowType = collections.OrderedDict[str, Union[str, list[str]]]


def normalize_encoding(encoding: str) -> str:
    """Normalize the name of the encoding."""
    return codecs.lookup(encoding).name


def _count_lines(s: str) -> int:
    """Count the line breaks in a cell value, supporting `\\r\\n` and bare `\\r` line endings."""
    return s.count('\n') + s.count('\r') - s.count('\r\n')


class LineNumber(collections.namedtuple('LineNumber', ['start', 'end'])):
    """
    The range of physical lines occupied by a logical record in a DSV file.

    `start` and `end` are 1-based line numbers in the source file. For records spanning a single
    line `start == end`. The object unpacks as a `(start, end)` tuple; for backwards compatibility
    it also compares equal to and converts to an `int` giving the last physical line of the
    record, as plain line numbers did before.
    """
    __slots__ = ()

    def __new__(cls, start: int, end: Optional[int] = None):
        return super().__new__(cls, start, start if end is None else end)

    @property
    def start_lineno(self) -> int:
        """Alias for `start`."""
        return self.start

    @property
    def end_lineno(self) -> int:
        """Alias for `end`."""
        return self.end

    def __str__(self):
        return f'{self.start}' if self.start == self.end else f'{self.start}-{self.end}'

    def __int__(self):
        return self.end

    def __index__(self):
        return self.end

    def __eq__(self, other):
        if isinstance(other, int):
            return other == self.end or other == self.start
        return tuple.__eq__(self, other)

    def __ne__(self, other):
        res = self.__eq__(other)
        return res if res is NotImplemented else not res

    def __hash__(self):
        return tuple.__hash__(self)


class UnicodeWriter:  # pylint: disable=too-many-instance-attributes
    """
    Write Unicode data to a csv file.

    :param f: The target to which to write the data; a local path specified as `str` or \
    `pathlib.Path` or `None`, in which case the data, formatted as DSV can be retrieved \
    via :meth:`~UnicodeWriter.read`
    :param dialect: Either a dialect name as recognized by `csv.writer` or a \
    :class:`~Dialect` instance for dialect customization beyond what can be done with \
    `csv.writer`.
    :param kw: Keyword arguments passed through to `csv.writer`.

    .. code-block:: python

        >>> from csvw import UnicodeWriter
        >>> with UnicodeWriter('data.tsv', delimiter='\t') as writer:
        ...     writer.writerow(['ä', 'ö', 'ü'])
    """

    def __init__(
            self,
            f: Optional[PathType] = None,
            dialect: Optional[Union[Dialect, str]] = None,
            **kw):
        self.f = f
        self.encoding = kw.pop('encoding', 'utf-8')
        if isinstance(dialect, Dialect):
            self.encoding = dialect.python_encoding
            self.kw = dialect.as_python_formatting_parameters()
            self.kw.update(kw)
        else:
            self.kw = kw
            if dialect:
                self.kw['dialect'] = dialect
        self.encoding = normalize_encoding(self.encoding)
        self.escapechar = self.kw.get('escapechar')
        if self.escapechar and self.kw.get('quoting') != csv.QUOTE_NONE:
            # work around https://bugs.python.org/issue12178
            # (csv.writer doesn't escape escapechar while csv.reader expects it)
            def _escapedoubled(row,
                               _type=str,
                               _old=self.escapechar,
                               _new=2 * self.escapechar):
                return [s.replace(_old, _new) if isinstance(s, _type) else s for s in row]
        else:
            def _escapedoubled(row):
                return row
        self._escapedoubled = _escapedoubled
        self._close = False
        self._rows_written = 0
        self.writer = None

    def __enter__(self):
        if isinstance(self.f, (str, pathlib.Path)):
            if isinstance(self.f, pathlib.Path):
                self.f = str(self.f)

            self.f = io.open(self.f, 'wt', encoding=self.encoding, newline='')
            self._close = True
        elif self.f is None:
            self.f = io.StringIO(newline='')

        self.writer = csv.writer(self.f, **self.kw)
        return self

    def read(self) -> Optional[bytes]:
        """
        If the writer has been initialized passing `None` as target, the CSV data as `bytes` can be
        retrieved calling this method.
        """
        if hasattr(self.f, 'seek'):
            self.f.seek(0)
        if hasattr(self.f, 'read'):
            return self.f.read().encode('utf-8')
        return None  # pragma: no cover

    def __exit__(self, type_, value, traceback):
        if self._close:
            self.f.close()

    def writerow(self, row: Iterable[Union[str, None]]):
        """Write multiple rows."""
        self.writer.writerow(self._escapedoubled(row))
        self._rows_written += 1

    def writerows(self, rows: Iterable[Union[tuple, list, dict]]):
        """
        Writes each row in `rows` formatted as CSV row. This behaves as
        [`csvwriter.writerows`](https://docs.python.org/3/library/csv.html#csv.csvwriter.writerows)
        except when an iterable of `dict` objects is passed. In that case, it is assumed that all
        items in `rows` are `dict`s and all have the same keys in the same order (as what would
        be read by `UnicodeDictReader`). Then, the keys of the first item are written as header row
        and the values of each row are written as subsequent rows.

        :param rows: The data to be written.
        """
        for i, row in enumerate(rows):
            if isinstance(row, dict):
                if i == 0 and not self._rows_written:
                    # We write a header row.
                    self.writerow(row.keys())
                row = row.values()
            self.writerow(row)


class UnicodeReader:  # pylint: disable=too-many-instance-attributes
    """
    Read Unicode data from a csv file.

    :param f: The source from which to read the data; a local path specified as `str` or \
    `pathlib.Path`, a file-like object or a `list` of lines.
    :param dialect: Either a dialect name as recognized by `csv.reader` or a \
    :class:`~Dialect` instance for dialect customization beyond what can be done with \
    `csv.writer`.
    :param kw: Keyword arguments passed through to `csv.reader`.

    .. code-block:: python

        >>> with UnicodeReader('tests/fixtures/frictionless-data.csv', delimiter='|') as reader:
        ...     for row in reader:
        ...         print(row)
        ...         break
        ...
        ['FK', 'Year', 'Location name', 'Value', 'binary', 'anyURI', 'email', 'boolean', 'array',
        'geojson']
    """
    def __init__(
            self,
            f: LinesOrPath,
            dialect: Optional[Union[Dialect, str]] = None,
            **kw):
        self.f = f
        self.encoding = normalize_encoding(kw.pop('encoding', 'utf-8-sig'))
        self.newline = kw.pop('lineterminator', None)
        self.dialect = dialect if isinstance(dialect, Dialect) else None
        self.lineno = None
        self._start_lineno = None
        self.reader = None
        if self.dialect:
            self.encoding = self.dialect.python_encoding
            self.kw = dialect.as_python_formatting_parameters()
            self.kw.update(kw)
        else:
            self.kw = kw
            if dialect:
                self.kw['dialect'] = dialect
        self._close = False
        self.comments = []

        # We potentially screw people with valid CSV files where the content - presumably the
        # header - starts with 0xfeff. But the chance of irritating people trying to read Excel
        # exported CSV with the defaults seems way bigger - and anyone with CSV column names
        # starting with 0xfeff will run into more trouble down the line anyway ...
        if self.encoding == 'utf-8':
            self.encoding = 'utf-8-sig'

        # encoding of self.reader rows: differs from source encoding
        # where we need to recode from non-8bit clean source encoding
        # to utf-8 first to feed into the (byte-based) PY2 csv.reader
        self._reader_encoding = self.encoding

    def __enter__(self):
        if isinstance(self.f, (str, pathlib.Path)):
            if isinstance(self.f, pathlib.Path):
                self.f = str(self.f)

            self.f = io.open(self.f, mode='rt', encoding=self.encoding, newline=self.newline or '')
            self._close = True
        elif not hasattr(self.f, 'read'):
            lines = []
            for line in self.f:
                lines.append(line.decode(self.encoding) if isinstance(line, bytes) else line)
            self.f = lines
        self.reader = csv.reader(self.f, **self.kw)
        self.lineno = -1
        return self

    def _next_row(self):
        self.lineno += 1
        self._start_lineno = self.lineno
        row = [
            s if isinstance(s, str) else s.decode(self._reader_encoding)
            for s in next(self.reader)]
        self.lineno += sum(_count_lines(s) for s in row)
        return row

    def __next__(self):
        row = self._next_row()
        if self.dialect:
            while (row and self.dialect.commentPrefix and  # noqa: W504
                   row[0].startswith(self.dialect.commentPrefix)) or \
                    ((not row or set(row) == {''}) and self.dialect.skipBlankRows) or \
                    (self.lineno < self.dialect.skipRows):
                if (row and self.dialect.commentPrefix and  # noqa: W504
                        row[0].startswith(self.dialect.commentPrefix)) or \
                        (row and self.lineno < self.dialect.skipRows):
                    self.comments.append((
                        self.lineno,
                        self.dialect.delimiter.join(row).lstrip(self.dialect.commentPrefix).strip(),
                    ))
                row = self._next_row()
            row = [self.dialect.trimmer(s) for s in row][self.dialect.skipColumns:]
        return row

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._close:
            self.f.close()

    def __iter__(self):
        return self


class UnicodeReaderWithLineNumber(UnicodeReader):  # pylint: disable=too-few-public-methods
    """
    A `UnicodeReader` yielding `(lineno, row)` pairs, where `lineno` is a :class:`LineNumber`:

    - `lineno.start` is the 1-based number of the first **text line** occupied by the
      (possibly multi-line) logical record in the DSV file.
    - `lineno.end` is the 1-based number of the last text line occupied by the record.

    For single-line records `start == end`. Blank lines and skipped rows are not renumbered,
    i.e. the numbers always refer to physical lines of the source file.
    """
    def __next__(self):
        """
        :return: a pair (`LineNumber`, row)
        """
        # Retrieve the row. `self.lineno` points to the last physical line of the record
        # after this call, `self._start_lineno` to the first one.
        row = super().__next__()
        return LineNumber(self._start_lineno + 1, self.lineno + 1), row


class UnicodeDictReader(UnicodeReader):
    """
    A `UnicodeReader` yielding one `dict` per row.

    :param f: As for :class:`UnicodeReader`
    :param fieldnames:

    .. code-block:: python

        >>> with UnicodeDictReader(
        ...         'tests/fixtures/frictionless-data.csv',
        ...         dialect=Dialect(delimiter='|', header=False),
        ...         fieldnames=[str(i) for i in range(1, 11)]) as reader:
        ...     for row in reader:
        ...         print(row)
        ...         break
        ...
        OrderedDict([('1', 'FK'), ('2', 'Year'), ('3', 'Location name'), ('4', 'Value'),
        ('5', 'binary'), ('6', 'anyURI'), ('7', 'email'), ('8', 'boolean'), ('9', 'array'),
        ('10', 'geojson')])

    """

    def __init__(
            self,
            f,
            fieldnames: Optional[list[str]] = None,
            restkey: Optional[str] = None,
            restval: Optional[str] = None,
            **kw):
        self._fieldnames = fieldnames   # list of keys for the dict
        self.restkey = restkey          # key to catch long rows
        self.restval = restval          # default value for short rows
        self.line_num = 0
        super().__init__(f, **kw)

    @property
    def fieldnames(self) -> Optional[list[str]]:
        """Get the fieldnames, i.e. the dictionary keys for the rows."""
        if self._fieldnames is None:
            try:  # Read the first row.
                self._fieldnames = super().__next__()
            except StopIteration:  # No rows, so no fieldnames is ok.
                pass
        self.line_num = self.reader.line_num
        if self._fieldnames:
            if len(set(self._fieldnames)) != len(self._fieldnames):
                warnings.warn('Duplicate column names!')
        return self._fieldnames

    def __next__(self) -> DictRowType:
        if self.line_num == 0:
            # Used only for its side effect.
            self.fieldnames  # pylint: disable=pointless-statement
        row = super().__next__()
        self.line_num = self.reader.line_num

        # unlike the basic reader, we prefer not to return blanks,
        # because we will typically wind up with a dict full of None
        # values
        while row == []:
            row = super().__next__()
        return self.item(row)

    def item(self, row) -> DictRowType:
        """Turn a row into a dict."""
        d = collections.OrderedDict((k, v) for k, v in zip(self.fieldnames, row))
        lf = len(self.fieldnames)
        lr = len(row)
        if lf < lr:
            d[self.restkey] = row[lf:]
        elif lf > lr:
            for key in self.fieldnames[lr:]:
                d[key] = self.restval
        return d


class NamedTupleReader(UnicodeDictReader):
    """
    A `UnicodeReader` yielding one `namedtuple` per row.

    .. note::

        This reader has some limitations, notably that fieldnames must be normalized to be
        admissible Python names, but also bad performance (compared with `UnicodeDictReader`).
    """

    _normalize_fieldname = staticmethod(utils.normalize_name)

    @functools.cached_property
    def cls(self):
        """
        Creates a namedtuple class suitable for the columns of the CSV content.
        """
        fieldnames = list(map(self._normalize_fieldname, self.fieldnames))
        return collections.namedtuple('Row', fieldnames)

    def item(self, row):
        """Create a namedtuple from a row."""
        d = UnicodeDictReader.item(self, row)
        for name in self.fieldnames:
            d.setdefault(name, None)
        return self.cls(
            **{self._normalize_fieldname(k): v for k, v in d.items() if k in self.fieldnames})


def iterrows(lines_or_file: LinesOrPath,
             namedtuples: Optional[bool] = False,
             dicts: Optional[bool] = False,
             encoding: Optional[str] = 'utf-8',
             **kw) -> Generator:
    """Convenience factory function for csv reader.

    :param lines_or_file: Content to be read. Either a file handle, a file path or a list\
    of strings.
    :param namedtuples: Yield namedtuples.
    :param dicts: Yield dicts.
    :param encoding: Encoding of the content.
    :param kw: Keyword parameters are passed through to csv.reader.
    :return: A generator over the rows.
    """
    if namedtuples and dicts:
        raise ValueError('either namedtuples or dicts can be chosen as output format')
    if namedtuples:
        _reader = NamedTupleReader
    elif dicts:
        _reader = UnicodeDictReader
    else:
        _reader = UnicodeReader

    with _reader(lines_or_file, encoding=encoding, **kw) as r:
        yield from r


reader = iterrows


def rewrite(fname: PathType, visitor: Callable[[int, list[str]], Union[None, list[str]]], **kw):
    """Utility function to rewrite rows in dsv files.

    :param fname: Path of the dsv file to operate on.
    :param visitor: A callable that takes a line-number and a row as input and returns a \
    (modified) row or None to filter out the row.
    :param kw: Keyword parameters are passed through to csv.reader/csv.writer.
    """
    fname = pathlib.Path(fname)
    assert fname.is_file()
    with tempfile.NamedTemporaryFile(delete=False) as fp:
        tmp = pathlib.Path(fp.name)

    with UnicodeReader(fname, **kw) as reader_:
        with UnicodeWriter(tmp, **kw) as writer:
            for i, row in enumerate(reader_):
                row = visitor(i, row)
                if row is not None:
                    writer.writerow(row)
    shutil.move(tmp, fname)


def add_rows(fname: PathType, *rows: list[str]):
    """
    Add rows to a CSV file.
    """
    with tempfile.NamedTemporaryFile(delete=False) as fp:
        tmp = pathlib.Path(fp.name)

    fname = pathlib.Path(fname)
    with UnicodeWriter(tmp) as writer:
        if fname.exists():
            with UnicodeReader(fname) as reader_:
                for row in reader_:
                    writer.writerow(row)
        writer.writerows(rows)
    shutil.move(tmp, fname)


def filter_rows_as_dict(fname: PathType, filter_: Callable[[dict], bool], **kw) -> int:
    """Rewrite a dsv file, filtering the rows.

    :param fname: Path to dsv file
    :param filter_: callable which accepts a `dict` with a row's data as single argument\
    returning a `Boolean` indicating whether to keep the row (`True`) or to discard it \
    `False`.
    :param kw: Keyword arguments to be passed `UnicodeReader` and `UnicodeWriter`.
    :return: The number of rows that have been removed.
    """
    filter_ = DictFilter(filter_)
    rewrite(fname, filter_, **kw)
    return filter_.removed


class DictFilter:  # pylint: disable=R0903
    """Utility to apply a filter to a row as dict, while iterating over rows a list."""
    def __init__(self, filter_: Callable[[dict[str, str]], bool]):
        self.header: Optional[list[str]] = None
        self.filter = filter_
        self.removed: int = 0

    def __call__(self, i: int, row: list[str]) -> Optional[list[str]]:
        if i == 0:
            self.header = row
            return row
        if row:
            item = dict(zip(self.header, row))
            if self.filter(item):
                return row
            self.removed += 1
        return None

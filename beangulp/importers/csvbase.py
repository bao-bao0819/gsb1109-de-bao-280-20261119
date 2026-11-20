import abc
import csv
import datetime
import decimal
from enum import Enum
import re

from collections import defaultdict
from itertools import groupby, islice, tee
from typing import NamedTuple
from beancount.core import data

import beangulp

EMPTY = frozenset()

NA = object()
"""Marker to indicate that a value was not specified."""


class MonthDay(NamedTuple):
    """A month and day without an associated year.

    Values of this type result from parsing date fields that do not
    specify a year. The year can be resolved providing a statement
    period and information about the ordering of the rows in the
    input file. See CSVReader for details.

    """

    month: int
    day: int

    def __str__(self):
        return f"{self.month:02d}/{self.day:02d}"


def _has_year(frmt):
    """Check whether a date format specification includes the year."""
    return any(code in frmt for code in ("%Y", "%y", "%G"))


def _candidates(monthday, start, end):
    """Generate the dates matching a MonthDay within a statement period.

    Only the caller provided period is considered. The current date
    plays no role in determining the candidate years.

    """
    for year in range(start.year, end.year + 1):
        try:
            candidate = datetime.date(year, monthday.month, monthday.day)
        except ValueError:
            # Not a valid date in this year, e.g. February 29th.
            continue
        if start <= candidate <= end:
            yield candidate


class DateResolutionError(ValueError):
    """Base class for errors occurred resolving partial dates."""


class AmbiguousDateError(DateResolutionError):
    """The year of a partial date cannot be uniquely determined.

    Attributes:
      lineno: Line number of the offending row in the input file.
      row: The offending row.
      candidates: The dates consistent with the statement period and
        the ordering of the rows in the input file.

    """

    def __init__(self, message, lineno, row, candidates):
        super().__init__(message)
        self.lineno = lineno
        self.row = row
        self.candidates = candidates


class InconsistentDateError(DateResolutionError):
    """Rows cannot be ordered chronologically within the statement period.

    Attributes:
      rows: The conflicting rows as a list of (lineno, row) pairs.

    """

    def __init__(self, message, rows):
        super().__init__(message)
        self.rows = rows


def _chomp(iterable, head, tail):
    """Return an iterator that yields selected values from an iterable.

    Args:
      iterable: The iterable to iterate.
      head: Number of initial elements to skip.
      tail: Number of trailing elements to skip.

    >>> list(_chomp(range(10), 2, 3))
    [2, 3, 4, 5, 6]

    """
    iterator = islice(iterable, head, None)
    if not tail:
        yield from iterator
    iterator, sentinel = tee(iterator)
    for _ in islice(sentinel, tail, None):
        yield next(iterator)


def _resolve(spec, names):
    """Resolve column specification into column index.

    Args:
      spec: Column name or index.
      names: A dict mapping column names to column indices.

    Returns:
      Column index.
    """
    if isinstance(spec, int):
        return spec
    if names is None:
        raise KeyError(f"Column {spec!r} cannot be found in file without column names")
    col = names.get(spec)
    if col is None:
        cols = ", ".join(repr(name) for name in names.keys())
        raise KeyError(f"Cannot find column {spec!r} in column names: {cols}")
    return col


class Column:
    """Field descriptor.

    Args:
      name: Column name or index.
      default: Value to return if the field is empty. When a default
        value is not provided, emty fields are passed to the parser
        to generate a value.

    """

    def __init__(self, *names, default=NA):
        self.names = names
        self.default = default

    def __repr__(self):
        names = ", ".join(repr(i) for i in self.names)
        return f"{self.__class__.__module__}.{self.__class__.__name__}({names})"

    def getter(self, names):
        """Generate an attribute accessor for the column specification.

        The returned function is a getitem-like function that takes a
        tuple as argument and returns the value of the field described
        by the column specificaion. The function returns the field
        default value if all columns are empty. The value is parsed
        with the column parser function.

        Args:
          names: A dict mapping column names to column indices.

        Returns:
          An accessor function.

        """
        idxs = [_resolve(x, names) for x in self.names]

        def func(obj):
            value = tuple(obj[i] for i in idxs)
            if self.default is not NA and not any(value):
                return self.default
            return self.parse(*value)

        return func

    def parse(self, value):
        """Parse column value.

        Args:
          value: Field string value obtained from the CSV reader.

        Returns:
          Field parsed value.

        """
        return value.strip()


class Columns(Column):
    """Specialized Column for multiple column fields.

    Args:
      name: Column names or indexes.
      sep: Separator to use to join columns.
      default: Value to return all the fields are empty, if specified.

    """

    def __init__(self, *names, sep=" ", default=NA):
        super().__init__(*names, default=default)
        self.sep = sep

    def parse(self, *values):
        return self.sep.join(val.strip() for val in values if val)


class Date(Column):
    """Specialized Column descriptor for date fields.

    Parse strings into datetime.date objects accordingly to the
    provided format specification. The format specification is the
    same understood by datetime.datetime.strptime().

    Multiple format specifications can be provided as a sequence.
    They are tried in order and the first matching one is used.
    Formats that do not include the year result in MonthDay values.
    The year of these partial dates can be resolved by the CSVReader
    when a statement period is provided. This is useful to import
    statements where the year is only implied by the context.

    Args:
      name: Column name or index.
      frmt: Date format specification or a sequence of date format
        specifications.
      default: Value to return if the field is empty, if specified.

    """

    def __init__(self, name, frmt="%Y-%m-%d", default=NA):
        super().__init__(name, default=default)
        self.frmt = frmt
        self.frmts = (frmt,) if isinstance(frmt, str) else tuple(frmt)
        self.partial = any(not _has_year(frmt) for frmt in self.frmts)

    def parse(self, value):
        value = value.strip()
        for frmt in self.frmts:
            try:
                parsed = datetime.datetime.strptime(value, frmt)
            except ValueError:
                continue
            if _has_year(frmt):
                return parsed.date()
            return MonthDay(parsed.month, parsed.day)
        formats = ", ".join(repr(frmt) for frmt in self.frmts)
        raise ValueError(f"Cannot parse date {value!r} using formats {formats}")


class Amount(Column):
    """Specialized Column descriptor for decimal fields.

    Parse strings into decimal.Decimal objects. Optionally apply
    regexp substitutions before parsing the decimal number. This
    allows to normalize locale formatted decimal numbers into the
    format expected by decimal.Decimal().

    Args:
      name: Column name or index.
      subs: Dictionary mapping regular expression patterns to
        replacement strings. Substitutions are performed with
        re.sub() in the order they are specified.
      negate: If true, negate the amount.
      default: Value to return if the field is empty, if specified.

    """

    def __init__(self, name, subs=None, negate=False, default=NA):
        super().__init__(name, default=default)
        self.subs = subs if subs is not None else {}
        self.negate = negate

    def parse(self, value):
        for pattern, replacement in self.subs.items():
            value = re.sub(pattern, replacement, value)
        parsed = decimal.Decimal(value)
        if self.negate:
            parsed = -parsed
        return parsed


class CreditOrDebit(Column):
    """Specialized Column for positive and negative amounts on separate columns.

    Parse and return the amount present in the credit or debit
    fields. The amount in the debit field is negated before being
    returned. Only one of the two fields may be populated. The parsing
    is done as per the Amount column type.

    Args:
      credit: Column name or index for amount.
      debit: Column name or index for negated amount.
      subs: Dictionary mapping regular expression patterns to
        replacement strings. Substitutions are performed with
        re.sub() in the order they are specified.
      default: Value to return if both fields are empty, if specified.

    """

    def __init__(self, credit, debit, subs=None, default=NA):
        super().__init__(credit, debit, default=default)
        self.subs = subs if subs is not None else {}

    def parse(self, credit, debit):
        if credit and debit:
            raise ValueError(
                "The credit and debit fields cannot be populated at the same time"
            )
        if not credit and not debit:
            raise ValueError("Neither credit or debit fields are populated")
        value = credit if credit else debit
        for pattern, replacement in self.subs.items():
            value = re.sub(pattern, replacement, value)
        parsed = decimal.Decimal(value)
        return parsed if credit else -parsed


# The CSV Importer class needs to inherit from beangulp.Importer which
# is an abstract base class having abc.ABCMeta as metaclass. To be
# able to do so out CSV metaclass need to be a sublcass of
# abc.ABCMeta.
class CSVMeta(abc.ABCMeta):
    """A metaclass that extracts column specifications from class members
    and stores them in a columns dictionary keyed by the member name."""

    def __new__(mcs, name, bases, dct):
        columns = {}
        others = {}
        for key, value in dct.items():
            if isinstance(value, Column):
                columns[key] = value
                continue
            others[key] = value
        others["columns"] = columns
        return super().__new__(mcs, name, bases, others)


class Order(Enum):
    ASCENDING = 1
    """Entries are listed in chronological order."""
    DESCENDING = 2
    """Entries are listed in reverse chronological order."""


class CSVReader(metaclass=CSVMeta):
    encoding = "utf8"
    """File encoding."""
    header = 0
    """Number of header lines to skip."""
    footer = 0
    """Number of footer lines to ignore."""
    names = True
    """Whether the data file contains a row with column names."""
    dialect = None
    """The CSV dialect used in the input file."""
    comments = "#"
    """Comment character."""
    order = None
    """Order of entries in the CSV file. If None the order will be inferred from the file content."""
    statement_period = None
    """Optional statement period as a (start, end) pair of dates.

    The statement period is required to resolve partial dates, dates
    without a year, in the input file. Candidate years for partial
    dates are derived exclusively from the statement period; the
    current date is never consulted.
    """
    grouper = None
    """Optional callable mapping a row to a group key.

    Consecutive rows with the same group key form a group. Groups
    are assumed to be independently ordered by date and partial
    dates are resolved independently within each group. If None, all
    the rows in the file form a single group.
    """

    # This is populated by the CSVMeta metaclass.
    columns = {}

    def __init__(self, statement_period=None, grouper=None):
        if hasattr(self, 'skiplines'):
            # Warn about use of deprecated class attribute, eventually.
            # warnings.warn('skiplines is deprecated, use header instead', DeprecationWarning)
            self.header = self.skiplines
        if statement_period is not None:
            self.statement_period = statement_period
        if grouper is not None:
            self.grouper = grouper

    def read(self, filepath, strict=True):
        """Read CSV file according to class defined columns specification.

        Use the first rown in the data file to resolve columns
        specification. Yield namedtuple-like objects with attribute
        accessors to access the data fields as defined by the class
        columns specification.

        Partial dates, dates without a year, are resolved using the
        configured statement period assuming rows within each group
        are sorted by date. Rows for which the year cannot be
        uniquely determined raise an AmbiguousDateError, unless
        strict is False, in which case they are returned unresolved.
        Rows that cannot possibly be ordered chronologically raise
        an InconsistentDateError.

        Args:
          filepath: Filesystem path to the input file.
          strict: Whether to raise an AmbiguousDateError when the
            year of a partial date cannot be uniquely determined.

        Yields:
          Namedtuple-like objects.

        """

        with open(filepath, encoding=self.encoding) as fd:
            # Skip header and footer lines.
            lines = _chomp(fd, self.header, self.footer)

            # Filter out comment lines.
            if self.comments:
                lines = filter(lambda x: not x.startswith(self.comments), lines)

            reader = csv.reader(lines, dialect=self.dialect)

            # Map column names to column indices.
            names = None
            if self.names:
                headers = next(reader, None)
                if headers is None:
                    raise IndexError("The input file does not contain an header line")
                names = {name.strip(): index for index, name in enumerate(headers)}

            # Construct a class with attribute accessors for the
            # configured columns that works similarly to a namedtuple.
            attrs = {}
            for name, column in self.columns.items():
                attrs[name] = property(column.getter(names))
            row = type("Row", (tuple,), attrs)

            # Read data rows.
            rows = [row(fields) for fields in reader]

        # Resolve partial dates.
        partial = {
            name: column
            for name, column in self.columns.items()
            if isinstance(column, Date) and column.partial
        }
        resolved = self._resolve(rows, partial, strict) if partial else {}

        # Return data rows, overriding resolved partial dates.
        for index, r in enumerate(rows):
            values = resolved.get(index)
            if values:
                attrs = {
                    name: property(lambda self, value=value: value)
                    for name, value in values.items()
                }
                yield type("Row", (row,), attrs)(r)
            else:
                yield r

    def _resolve(self, rows, partial, strict):
        """Resolve partial dates.

        Args:
          rows: The data rows read from the input file.
          partial: A dict mapping column names to the Date column
            descriptors that can produce partial dates.
          strict: Whether to raise an AmbiguousDateError when the
            year of a partial date cannot be uniquely determined.

        Returns:
          A dict mapping row indexes to dicts mapping column names to
          the resolved dates.

        """
        # Compute the line number of the first data line.
        offset = int(self.header) + bool(self.names) + 1

        # Split the rows into groups. Empty lines do not contribute
        # to the grouping.
        indexed = [(index, row) for index, row in enumerate(rows) if row]
        if self.grouper is None:
            groups = [indexed]
        else:
            groups = [
                list(group)
                for _, group in groupby(indexed, key=lambda item: self.grouper(item[1]))
            ]

        resolved = {}
        for group in groups:
            for name in partial:
                self._resolve_column(group, name, offset, resolved, strict)
        return resolved

    def _resolve_column(self, group, name, offset, resolved, strict):
        """Resolve partial dates in one column within one group of rows."""
        values = [(index, row, getattr(row, name)) for index, row in group]
        if not any(isinstance(value, MonthDay) for _, _, value in values):
            return
        if self.statement_period is None:
            raise ValueError(
                f"A statement period is required to resolve partial dates "
                f"in column {name!r}"
            )
        start, end = self.statement_period

        # Compute the candidate dates for each row. Full dates are
        # their own only candidate and constrain the resolution of
        # the partial dates. Values that are not dates do not
        # constrain the resolution.
        candidates = []
        for index, row, value in values:
            if isinstance(value, MonthDay):
                choices = list(_candidates(value, start, end))
                if not choices:
                    lineno = offset + index
                    raise InconsistentDateError(
                        f"No date matching {value} on line {lineno} falls "
                        f"within the statement period from {start} to {end}",
                        [(lineno, tuple(row))],
                    )
                candidates.append(choices)
            elif isinstance(value, datetime.date):
                candidates.append([value])
            else:
                candidates.append(None)

        # Forward pass: compute the componentwise minimal assignment
        # consistent with the rows being sorted by date.
        forward = []
        previous = None
        previous_row = None
        for (index, row, value), choices in zip(values, candidates):
            if choices is None:
                forward.append(previous)
                continue
            feasible = [d for d in choices if previous is None or d >= previous]
            if not feasible:
                involved = []
                if previous_row is not None:
                    involved.append((offset + previous_row[0], tuple(previous_row[1])))
                involved.append((offset + index, tuple(row)))
                raise InconsistentDateError(
                    f"Rows cannot be ordered chronologically within the "
                    f"statement period from {start} to {end}: "
                    + "; ".join(f"line {lineno} {row!r}" for lineno, row in involved),
                    involved,
                )
            forward.append(feasible[0])
            previous = feasible[0]
            previous_row = (index, row)

        # Backward pass: compute the componentwise maximal
        # assignment. One is guaranteed to exist because the forward
        # pass found one.
        backward = [None] * len(values)
        following = None
        for pos in range(len(values) - 1, -1, -1):
            choices = candidates[pos]
            if choices is None:
                backward[pos] = following
                continue
            feasible = [d for d in choices if following is None or d <= following]
            backward[pos] = feasible[-1]
            following = feasible[-1]

        # The assignment is unique where the minimal and maximal
        # solutions agree.
        for pos, (index, row, value) in enumerate(values):
            if not isinstance(value, MonthDay):
                continue
            first, last = forward[pos], backward[pos]
            if first == last:
                resolved.setdefault(index, {})[name] = first
            elif strict:
                lineno = offset + index
                choices = [d for d in candidates[pos] if first <= d <= last]
                raise AmbiguousDateError(
                    f"Cannot determine the year of date {value} on line "
                    f"{lineno} {tuple(row)!r}: candidates "
                    + ", ".join(str(d) for d in choices)
                    + f" are all consistent with the statement period "
                    f"from {start} to {end}",
                    lineno,
                    tuple(row),
                    choices,
                )


class Importer(beangulp.Importer, CSVReader):
    """CSV files importer base class.

    This class provides the infrastructure and the basic functionality
    necessary for importing transactions and balance assertions from
    CSV files. To do anything useful it needs to be subclassed to add
    fields definitions. Fields are defined as class attributes of type
    Column.

    Args:
      account: Importer default account.
      currency: Importer default currency.
      flag: Importer default flag for new transactions.
      statement_period: Optional (start, end) pair of dates bounding
        the statement period. Required to resolve partial dates.
      grouper: Optional callable mapping a row to a group key.
        Consecutive rows with the same key form a group within which
        partial dates are resolved independently.

    """

    def __init__(self, account, currency, flag="*", statement_period=None, grouper=None):
        super().__init__(statement_period=statement_period, grouper=grouper)
        self.importer_account = account
        self.currency = currency
        self.flag = flag

    def date(self, filepath):
        """Implement beangulp.Importer::date()

        Return the last date seen in the source file. Only dates
        confirmed while parsing the file contribute to the result:
        partial dates whose year cannot be uniquely determined are
        skipped, but all rows are still checked for consistency. The
        current date plays no role in the result.
        """
        dates = []
        for row in self.read(filepath, strict=False):
            if not row:
                continue
            value = row.date
            if value is None or isinstance(value, MonthDay):
                continue
            dates.append(value)
        return max(dates) if dates else None

    def account(self, filepath):
        """Implement beangulp.Importer::account()"""
        return self.importer_account

    def extract(self, filepath, existing):
        """Implement beangulp.Importer::extract()

        This methods costructs a transaction for each data row using
        the date, narration, and amount required fields and the flag,
        payee, account, currency, tag, link, balance optional fields.

        Transaction metadata is constructed with the metadata() method
        and the finalize() method is called on each transaction. These
        can be redefine in subclasses. For customization that cannot
        be implemented with these two extension points, consider
        basing the importer on the CSVReader class and implement
        tailored data processing in the extract() method.

        """

        entries = []
        balances = defaultdict(list)
        default_account = self.account(filepath)

        # Compute the line number of the first data line.
        offset = int(self.header) + bool(self.names) + 1

        for lineno, row in enumerate(self.read(filepath), offset):
            # Skip empty lines.
            if not row:
                continue

            try:
                tag = getattr(row, "tag", None)
                tags = {tag} if tag else EMPTY

                link = getattr(row, "link", None)
                links = {link} if link else EMPTY

                # This looks like an exercise in defensive programming
                # gone too far, but we do not want to depend on any field
                # being defined other than the essential ones.
                flag = getattr(row, "flag", self.flag)
                payee = getattr(row, "payee", None)
                account = getattr(row, "account", default_account)
                currency = getattr(row, "currency", self.currency)
                units = data.Amount(row.amount, currency)

                # Create a transaction.
                txn = data.Transaction(
                    self.metadata(filepath, lineno, row),
                    row.date,
                    flag,
                    payee,
                    row.narration,
                    tags,
                    links,
                    [
                        data.Posting(account, units, None, None, None, None),
                    ],
                )

                # Apply user processing to the transaction.
                txn = self.finalize(txn, row)

            except Exception as ex:
                # Report input file location of processing errors. This could
                # use Exception.add_note() instead, but this is available only
                # with Python 3.11 and later.
                raise RuntimeError(
                    f"Error processing {filepath} line {lineno + 1} with values {row!r}"
                ) from ex

            # Allow finalize() to reject the row extracted from the current row.
            if txn is None:
                continue

            # Add the transaction to the output list.
            entries.append(txn)

            # Add balance to balances list.
            balance = getattr(row, "balance", None)
            if balance is not None:
                date = row.date + datetime.timedelta(days=1)
                units = data.Amount(balance, currency)
                meta = data.new_metadata(filepath, lineno)
                balances[currency].append(
                    data.Balance(meta, date, account, units, None, None)
                )

        if not entries:
            return []

        if self.order is None:
            order = (
                Order.ASCENDING if entries[0].date <= entries[-1].date else Order.DESCENDING
            )
        else:
            order = self.order

        # Reverse the list if the file is in descending order.
        if order is Order.DESCENDING:
            entries.reverse()

        # Append balances.
        for currency, balances in balances.items():
            entries.append(balances[-1 if order is Order.ASCENDING else 0])

        return entries

    def metadata(self, filepath, lineno, row):
        """Build transaction metadata dictionary.

        This method can be extended to add customized metadata
        entries based on the content of the data row.

        Args:
          filepath: Path to the file being imported.
          lineno: Line number of the data being processed.
          row: The data row being processed.

        Returns:
          A metadata dictionary.

        """
        return data.new_metadata(filepath, lineno)

    def finalize(self, txn, row):
        """Post process the transaction.

        Returning None results in the transaction being discarded and
        in source row to do not contribute to the determination of the
        balances.

        Args:
          txn: The just build Transaction object.
          row: The data row being processed.

        Returns:
          A potentially extended or modified Transaction object or None.

        """
        return txn

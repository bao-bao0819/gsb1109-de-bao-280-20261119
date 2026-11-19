import abc
import csv
import datetime
import decimal
from enum import Enum
import re

from collections import defaultdict
from itertools import islice, tee
from beancount.core import data

import beangulp
from beangulp import date_utils

EMPTY = frozenset()

NA = object()
"""Marker to indicate that a value was not specified."""


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

    def getter(self, names, period=None):
        """Generate an attribute accessor for the column specification.

        The returned function is a getitem-like function that takes a
        tuple as argument and returns the value of the field described
        by the column specificaion. The function returns the field
        default value if all columns are empty. The value is parsed
        with the column parser function.

        Args:
          names: A dict mapping column names to column indices.
          period: Optional default statement period for columns that
            need one (see Date).

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

    When the format does not contain a year, a statement period can be
    provided to assign one. The year is only assigned when exactly one
    year within the period matches the month and day. Otherwise a
    DateResolutionError is raised, whose original cell value can be
    collected in an import receipt.

    Args:
      name: Column name or index.
      frmt: Date format specification.
      default: Value to return if the field is empty, if specified.
      period: A date_utils.Period instance, a (start, end) pair, a
        dict with start and end keys, or a callable returning one of
        those. When omitted, the reader or importer period is used.
        A callable is resolved by the reader, with the input file path.

    """

    def __init__(self, name, frmt="%Y-%m-%d", default=NA, period=None):
        super().__init__(name, default=default)
        self.frmt = frmt
        self.period = period

    def getter(self, names, period=None):
        idx = _resolve(self.names[0], names)
        if self.period is not None and not callable(self.period):
            effective_period = self.period
        else:
            effective_period = period
        effective_period = date_utils.as_period(effective_period)

        def func(obj):
            value = obj[idx]
            if self.default is not NA and not value:
                return self.default
            # A subclass overriding parse() takes full control of parsing.
            if type(self).parse is not Date.parse or effective_period is None:
                return self.parse(value)
            return self.parse_with_period(value, effective_period)

        return func

    def parse(self, value):
        return datetime.datetime.strptime(value.strip(), self.frmt).date()

    def parse_with_period(self, value, period):
        """Parse a date, using a period to assign a year if needed."""
        return date_utils.parse_date_format(value, self.frmt, period)


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
    period = None
    """Statement period used to assign a year to year-less dates.

    Accepts a date_utils.Period, a (start, end) pair, a dict with
    start and end keys, or a callable taking the input file path and
    returning one of those. May alternatively be supplied with the
    statement_period or date_window keyword argument to Importer().
    """

    # This is populated by the CSVMeta metaclass.
    columns = {}

    def __init__(self, **kwds):
        period = kwds.pop("period", None)
        period = date_utils.pop_period_alias(kwds) or period
        if kwds:
            unexpected = ", ".join(sorted(kwds))
            raise TypeError(f"Unexpected keyword arguments: {unexpected}")
        if period is not None:
            self.period = (
                period if callable(period) else date_utils.as_period(period)
            )
        if hasattr(self, 'skiplines'):
            # Warn about use of deprecated class attribute, eventually.
            # warnings.warn('skiplines is deprecated, use header instead', DeprecationWarning)
            self.header = self.skiplines
        self.receipt = date_utils.Receipt()

    def _period(self, filepath, period=None):
        """Return the effective statement period for an input file."""
        period = self.period if period is None else period
        if callable(period):
            period = period(filepath)
        return date_utils.as_period(period)

    def _column_label(self, name):
        """Return a human readable label for a configured column."""
        column = self.columns.get(name)
        if column is None:
            return name
        spec = column.names[0] if column.names else name
        if isinstance(spec, int):
            return f"column {spec}"
        return str(spec)

    def read(self, filepath, period=None):
        """Read CSV file according to class defined columns specification.

        Use the first rown in the data file to resolve columns
        specification. Yield namedtuple-like objects with attribute
        accessors to access the data fields as defined by the class
        columns specification.

        Rows whose date cannot be resolved unambiguously are skipped
        and recorded in the receipt, together with the original cell
        value. The receipt is available as the "receipt" attribute.

        Args:
          filepath: Filesystem path to the input file.
          period: Optional statement period overriding the class one.

        Yields:
          Namedtuple-like objects.

        """
        self.receipt = date_utils.Receipt()
        effective_period = self._period(filepath, period)

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
                if isinstance(column, Date):
                    column_period = column.period
                    if callable(column_period):
                        column_period = column_period(filepath)
                    if column_period is None:
                        column_period = effective_period
                    attrs[name] = property(column.getter(names, column_period))
                else:
                    attrs[name] = property(column.getter(names))
            row_type = type("Row", (tuple,), attrs)

            # Number of the first data line. Comment lines are removed
            # before parsing, so this can only be an approximation for
            # files with interleaved comments.
            lineno = int(self.header) + bool(self.names)

            # Return data rows.
            for x in reader:
                lineno += 1
                row = row_type(x)
                # Keep the source line number with the row so that it is
                # preserved even when malformed rows are skipped.
                row._lineno = lineno
                for name, column in self.columns.items():
                    if not isinstance(column, Date):
                        continue
                    try:
                        # Force date resolution so that a bad date is
                        # reported once, before the row is processed.
                        getattr(row, name)
                    except date_utils.DateResolutionError as error:
                        problem = date_utils.DateProblem.from_error(
                            error,
                            filename=filepath,
                            line=lineno,
                            column=self._column_label(name),
                        )
                        self.receipt.append(problem)
                        break
                else:
                    yield row


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
      period: Statement period used to assign a year to year-less
        dates. See CSVReader.period.

    """

    def __init__(self, account, currency, flag="*", period=None, **kwds):
        if period is not None:
            kwds["period"] = period
        super().__init__(**kwds)
        self.importer_account = account
        self.currency = currency
        self.flag = flag

    def date(self, filepath):
        """Implement beangulp.Importer::date()

        Return the last date seen in the source file.
        """
        dates = [row.date for row in self.read(filepath) if row]
        if not dates:
            return None
        return max(dates)

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

        for row in self.read(filepath):
            lineno = getattr(row, "_lineno", offset)

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

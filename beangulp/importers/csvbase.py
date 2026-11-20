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

    Args:
      name: Column name or index.
      frmt: Date format specification.
      default: Value to return if the field is empty, if specified.

    """

    def __init__(self, name, frmt="%Y-%m-%d", default=NA):
        super().__init__(name, default=default)
        self.frmt = frmt

    def parse(self, value):
        return datetime.datetime.strptime(value.strip(), self.frmt).date()


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


class CreditOrDebitError(ValueError):
    """A credit/debit row cannot be interpreted under the configured convention."""


class CreditOrDebit(Column):
    """Specialized Column for positive and negative amounts on separate columns.

    Parse and return the amount present in the credit or debit
    fields. Only one of the two fields may be populated. The parsing
    is done as per the Amount column type.

    The sign convention used by the source bank is selected through the
    sign_policy argument:

    SIGN_COLUMN (default): the columns carry unsigned magnitudes. The
      amount found in the debit column is negated before being returned
      and the amount found in the credit column is returned as-is. A
      negative value in the debit column represents the reversal of an
      entry originally posted as a debit (for example a refund of a
      payment) and is thus accepted. Negative values in the credit
      column have no meaning in this convention and are rejected.

    SIGN_SIGNED: the columns carry globally signed values and no sign
      transformation is applied. A negative debit is an expense and a
      positive debit is its refund. Negative values in the credit
      column conflict with this convention and are rejected.

    Args:
      credit: Column name or index for amount.
      debit: Column name or index for negated amount.
      subs: Dictionary mapping regular expression patterns to
        replacement strings. Substitutions are performed with
        re.sub() in the order they are specified.
      sign_policy: Sign convention used by the source bank, either
        SIGN_COLUMN or SIGN_SIGNED.
      default: Value to return if both fields are empty, if specified.

    """

    SIGN_COLUMN = "column"
    SIGN_SIGNED = "signed"

    def __init__(
        self, credit, debit, subs=None, sign_policy=SIGN_COLUMN, default=NA
    ):
        super().__init__(credit, debit, default=default)
        self.subs = subs if subs is not None else {}
        self.sign_policy = sign_policy

    def parse(self, credit, debit):
        if credit and debit:
            raise CreditOrDebitError(
                "The credit and debit fields cannot be populated at the same time"
            )
        if not credit and not debit:
            raise CreditOrDebitError("Neither credit or debit fields are populated")
        value = credit if credit else debit
        for pattern, replacement in self.subs.items():
            value = re.sub(pattern, replacement, value)
        parsed = decimal.Decimal(value)
        if credit and parsed < 0:
            raise CreditOrDebitError(
                "Negative credit values conflict with the configured source "
                f"sign convention ({self.sign_policy})"
            )
        if self.sign_policy is self.SIGN_SIGNED:
            # The sign is carried by the value itself: do not negate.
            return parsed
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
        # Inherit column specifications defined on base classes so that
        # a subclass can override a single column without redefining all
        # of them.
        for base in bases:
            columns.update(getattr(base, "columns", {}))
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

    # This is populated by the CSVMeta metaclass.
    columns = {}

    def __init__(self):
        if hasattr(self, 'skiplines'):
            # Warn about use of deprecated class attribute, eventually.
            # warnings.warn('skiplines is deprecated, use header instead', DeprecationWarning)
            self.header = self.skiplines

    def read(self, filepath):
        """Read CSV file according to class defined columns specification.

        Use the first rown in the data file to resolve columns
        specification. Yield namedtuple-like objects with attribute
        accessors to access the data fields as defined by the class
        columns specification.

        Args:
          filepath: Filesystem path to the input file.

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

            # Return data rows.
            for x in reader:
                yield row(x)


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

    """

    reconcile = False
    """Whether to verify each row against the running book balance.

    When enabled, rows that cannot be interpreted under the configured
    amount sign convention (for example a row with both the credit and
    debit columns populated, or a value with a sign conflicting with the
    source bank convention) are not turned into transactions. They are
    reported in a Note directive carrying the raw source values and the
    reason for the rejection, and the running balance reconciliation is
    marked as unconfirmed until the end of the file. Subsequent parsed
    entries are flagged and statement balances are reported as Notes
    rather than Balance assertions.
    """

    opening_balances = None
    """Opening balances keyed by currency, used as starting point for
    reconciliation. If not set, running balances start from zero."""

    precision = None
    """Number of decimal digits used by each currency, either a dict
    mapping currency codes to integers or a plain integer. Used to
    compare statement and computed balances at the currency precision."""

    def __init__(self, account, currency, flag="*"):
        super().__init__()
        self.importer_account = account
        self.currency = currency
        self.flag = flag

    def _quantize(self, value, currency):
        """Quantize a value at the configured precision for a currency.

        If no precision is configured for the given currency, the value
        is returned unchanged.
        """
        if self.precision is None:
            return value
        if isinstance(self.precision, dict):
            digits = self.precision.get(currency)
        else:
            digits = self.precision
        if digits is None:
            return value
        return value.quantize(decimal.Decimal(1).scaleb(-digits))

    def _note(self, filepath, lineno, date, account, comment):
        """Build a Note directive recording a reconciliation event."""
        meta = data.new_metadata(filepath, lineno)
        return data.Note(meta, date, account, comment, EMPTY, EMPTY)

    def date(self, filepath):
        """Implement beangulp.Importer::date()

        Return the last date seen in the source file.
        """
        return max(row.date for row in self.read(filepath) if row)

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
        extras = []
        balances = defaultdict(list)
        default_account = self.account(filepath)

        # Reconciliation state, used only when self.reconcile is set.
        opening = self.opening_balances or {}
        running = dict(opening)
        unconfirmed = set()
        confirmed = defaultdict(list)
        pending = defaultdict(list)

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
                amount = row.amount
                units = data.Amount(amount, currency)
                stated_balance = getattr(row, "balance", None)

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
                if not self.reconcile:
                    # Report input file location of processing errors. This could
                    # use Exception.add_note() instead, but this is available only
                    # with Python 3.11 and later.
                    raise RuntimeError(
                        f"Error processing {filepath} line {lineno + 1} "
                        f"with values {row!r}"
                    ) from ex
                # The row cannot be interpreted under the configured
                # conventions: preserve the raw source values instead of
                # fabricating an entry, and interrupt the balance chain.
                currency = locals().get("currency", self.currency)
                account = locals().get("account", default_account)
                try:
                    note_date = row.date
                except Exception:
                    note_date = datetime.date.today()
                comment = (
                    f"rejected source row at line {lineno + 1}: {ex}; "
                    f"raw values: {tuple(row)}"
                )
                extras.append(
                    (lineno, self._note(filepath, lineno, note_date, account, comment))
                )
                unconfirmed.add(currency)
                continue

            # Allow finalize() to reject the row extracted from the current row.
            if txn is None:
                continue

            if not self.reconcile:
                # Add the transaction to the output list.
                entries.append(txn)

                # Add balance to balances list.
                if stated_balance is not None:
                    date = row.date + datetime.timedelta(days=1)
                    units = data.Amount(stated_balance, currency)
                    meta = data.new_metadata(filepath, lineno)
                    balances[currency].append(
                        data.Balance(meta, date, account, units, None, None)
                    )
                continue

            # Reconciliation path: update the running balance using only
            # the amounts of the rows that parsed successfully. Rejected
            # rows never contribute to it.
            running[currency] = running.get(currency, decimal.Decimal()) + amount
            computed = self._quantize(running[currency], currency)

            if stated_balance is not None:
                stated = self._quantize(stated_balance, currency)
                # Preserve the per-row book balance for comparison.
                txn.meta["balance"] = data.Amount(stated_balance, currency)
                txn.meta["computed_balance"] = data.Amount(computed, currency)
                if currency not in unconfirmed and stated != computed:
                    # The statement chain does not add up: keep the entry
                    # but do not assert any balance until the discrepancy
                    # is resolved.
                    unconfirmed.add(currency)
                    comment = (
                        f"statement balance {stated} {currency} at line {lineno + 1} "
                        f"does not match computed balance {computed} {currency}"
                    )
                    extras.append(
                        (lineno, self._note(filepath, lineno, row.date, account, comment))
                    )

            if currency in unconfirmed:
                # Mark entries parsed after the chain was interrupted as
                # pending confirmation rather than silently accepting them.
                txn = txn._replace(flag="!")
                txn.meta["reconcile"] = (
                    "unconfirmed: running balance chain interrupted by a rejected row"
                )

            entries.append(txn)

            if stated_balance is not None:
                date = row.date + datetime.timedelta(days=1)
                record = (lineno, date, account, stated_balance, currency)
                if currency in unconfirmed:
                    pending[currency].append(record)
                else:
                    confirmed[currency].append(record)

        if not entries and not (self.reconcile and extras):
            return []

        if entries:
            if self.order is None:
                order = (
                    Order.ASCENDING
                    if entries[0].date <= entries[-1].date
                    else Order.DESCENDING
                )
            else:
                order = self.order

            # Reverse the list if the file is in descending order.
            if order is Order.DESCENDING:
                entries.reverse()
        else:
            order = Order.ASCENDING

        if not self.reconcile:
            # Append balances.
            for currency, statements in balances.items():
                entries.append(statements[-1 if order is Order.ASCENDING else 0])
            return entries

        # Append confirmed balance assertions. Only the latest statement
        # for each currency with an uninterrupted chain becomes a Balance
        # directive. Everything that remains unconfirmed is reported in
        # Notes and never asserted, even if the numbers happen to match.
        for currency, statements in confirmed.items():
            lineno, date, account, value, code = statements[
                -1 if order is Order.ASCENDING else 0
            ]
            meta = data.new_metadata(filepath, lineno)
            extras.append(
                (
                    lineno,
                    data.Balance(
                        meta,
                        date,
                        account,
                        data.Amount(value, code),
                        None,
                        None,
                    ),
                )
            )
        for currency, statements in pending.items():
            for lineno, date, account, value, code in statements:
                comment = (
                    f"unconfirmed statement balance {value} {code}: "
                    "balance chain interrupted by a rejected row"
                )
                extras.append(
                    (lineno, self._note(filepath, lineno, date, account, comment))
                )

        extras.sort(key=lambda item: item[0])
        entries.extend(directive for _, directive in extras)
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

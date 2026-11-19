import datetime
import re

import dateutil.parser


class DateResolutionError(ValueError):
    """Raised when a date without a year cannot be resolved unambiguously.

    The original cell value is kept on the exception so callers can
    include it verbatim in an import receipt.

    """

    def __init__(self, value, reason, period=None, candidates=None):
        self.value = value
        self.reason = reason
        self.period = period
        self.candidates = list(candidates or [])
        super().__init__(self._message())

    def _message(self):
        message = f"Cannot assign a year to date {self.value!r}: {self.reason}"
        if self.period is not None:
            message += f" (statement period {self.period.start} to {self.period.end})"
        if len(self.candidates) > 1:
            years = ", ".join(str(candidate.year) for candidate in self.candidates)
            message += f"; possible years: {years}"
        return message


class AmbiguousDateError(DateResolutionError):
    """Raised when more than one year in the period matches a date."""


class DateOutOfPeriodError(DateResolutionError):
    """Raised when no year in the period matches a date."""


# Short aliases for convenience.
AmbiguousDate = AmbiguousDateError
DateOutOfPeriod = DateOutOfPeriodError


class Period:
    """A known statement or billing period.

    Both ends of the period are inclusive. The period is used to
    assign a year to dates which only carry a month and a day.

    Args:
      start: Period start date or string.
      end: Period end date or string. A two-item tuple or list can be
        passed as the first argument instead.

    """

    def __init__(self, start, end=None):
        if end is None and isinstance(start, dict):
            start, end = start.get("start"), start.get("end")
        elif end is None and isinstance(start, (tuple, list)):
            if len(start) != 2:
                raise ValueError("A period tuple must contain a start and an end date")
            start, end = start
        if end is None:
            raise ValueError("A period requires both a start and an end date")
        self.start = _coerce_date(start)
        self.end = _coerce_date(end)
        if self.end < self.start:
            raise ValueError(
                f"Invalid period: end date {self.end} precedes start date {self.start}"
            )

    @property
    def window(self):
        """Alias for the period bounds, matching "date window" terminology."""
        return self.start, self.end

    def __repr__(self):
        return f"Period({self.start!r}, {self.end!r})"

    def __eq__(self, other):
        if isinstance(other, Period):
            return self.start == other.start and self.end == other.end
        if isinstance(other, (tuple, list)) and len(other) == 2:
            return (self.start, self.end) == (
                _coerce_date(other[0]),
                _coerce_date(other[1]),
            )
        return NotImplemented

    def resolve(self, month, day, value=None):
        """Resolve a month and day into a single date in the period.

        Args:
          month: Month number.
          day: Day number.
          value: Original cell value, used in error messages.

        Returns:
          A date.

        Raises:
          DateOutOfPeriodError: If no date in the period matches.
          AmbiguousDateError: If more than one year in the period matches.

        """
        candidates = []
        for year in range(self.start.year, self.end.year + 1):
            try:
                candidate = datetime.date(year, month, day)
            except ValueError:
                continue
            if self.start <= candidate <= self.end:
                candidates.append(candidate)
        if not candidates:
            try:
                datetime.date(self.start.year, month, day)
            except ValueError:
                reason = "the date is not a valid calendar date"
            else:
                reason = "the date falls outside the statement period"
            raise DateOutOfPeriodError(value, reason, self, candidates)
        if len(candidates) > 1:
            raise AmbiguousDateError(
                value,
                "the statement period is wide enough to match more than one year",
                self,
                candidates,
            )
        return candidates[0]


# An alternate name that reads naturally for statement data.
StatementPeriod = Period


def _coerce_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        _, _, has_year, _, _ = _dateutil_components(value, None)
        if not has_year:
            raise ValueError(
                f"Period bounds must include a year, got {value!r}"
            )
        return parse_date(value)
    raise TypeError(f"Expected a date or a date string, got {type(value)!r}")


def _coerce_period(period):
    if period is None or isinstance(period, Period):
        return period
    if isinstance(period, (tuple, list, dict)):
        if isinstance(period, dict):
            return Period(period.get("start"), period.get("end"))
        return Period(period)
    return Period(period)


def as_period(period):
    """Normalize a period specification into a Period instance.

    Accepts a Period, a (start, end) pair, a dict with start and end
    keys, or a string pair. Returns None unchanged.

    """
    return _coerce_period(period)


def pop_period_alias(kwds):
    """Remove and return the period from a kwargs dict of aliases.

    In addition to the canonical "period" argument on the importer
    constructors, "statement_period" and "date_window" are accepted in
    the extra keyword arguments.

    """
    for name in ("statement_period", "date_window"):
        if name in kwds:
            return kwds.pop(name)
    return None


def _has_year_format(frmt):
    """Return True if a strptime format contains a year directive."""
    return re.search(r"(?<!%)%[yY]", frmt) is not None


def _parse_with_default(string, default, parse_kwargs_dict):
    kwargs = dict(parse_kwargs_dict or {})
    kwargs["default"] = default
    return dateutil.parser.parse(string, **kwargs)


def _dateutil_components(string, parse_kwargs_dict):
    """Return (month, day, has_explicit_year) for a dateutil-parsed string.

    Two parses with different defaults are compared. Components that
    change are the ones that dateutil filled in from the default.

    """
    first = _parse_with_default(
        string, datetime.datetime(2000, 1, 1), parse_kwargs_dict
    )
    second = _parse_with_default(
        string, datetime.datetime(2004, 3, 2), parse_kwargs_dict
    )
    has_year = first.year == second.year
    has_month = first.month == second.month
    has_day = first.day == second.day
    return first.month, first.day, has_year, has_month, has_day


def resolve_date(month, day, period, value=None):
    """Resolve a month/day pair using a period.

    Args:
      month: Month number.
      day: Day number.
      period: A Period instance, (start, end) pair, or period strings.
      value: Original cell value, included in error messages.

    Returns:
      A date.

    """
    return _coerce_period(period).resolve(month, day, value)


def parse_date_format(string, frmt, period=None):
    """Parse a date using a strptime format, optionally using a period.

    Formats containing a year directive are parsed exactly as before.
    Formats without a year are resolved with the period when one is
    provided, so that the year does not depend on the current date.

    Args:
      string: The cell value to parse.
      frmt: A datetime.strptime() format specification.
      period: Known statement period used to assign a year.

    Returns:
      A datetime.date object.

    """
    value = string.strip()
    if _has_year_format(frmt):
        return datetime.datetime.strptime(value, frmt).date()
    if period is None:
        # Preserve the historical behavior when no period is known.
        return datetime.datetime.strptime(value, frmt).date()
    # Parse with a leap year prepended so that February 29 is valid
    # even though strptime would otherwise default the year to 1900.
    probe = datetime.datetime.strptime(f"2000 {value}", f"%Y {frmt}")
    return resolve_date(probe.month, probe.day, period, value=string)


def parse_date(string, parse_kwargs_dict=None, period=None):
    """Parse arbitrary strings to dates.

    This function is intended to support liberal inputs, so that we can use it
    in accepting user-specified dates on command-line scripts.

    When a period is provided, dates that contain only a month and a
    day are assigned the unique year within the period that matches
    them. If there is no match, or more than one match, a
    DateResolutionError is raised instead of silently using the
    current date.

    Args:
      string: A string to parse.
      parse_kwargs_dict: Dict of kwargs to pass to dateutil parser.
      period: Optional known statement period for year-less dates.

    Returns:
      A datetime.date object.

    """
    # At the moment, rely on the most excellent dateutil.
    if parse_kwargs_dict is None:
        parse_kwargs_dict = {}
    if period is None:
        return dateutil.parser.parse(string, **parse_kwargs_dict).date()

    month, day, has_year, has_month, has_day = _dateutil_components(
        string, parse_kwargs_dict
    )
    if has_year:
        return dateutil.parser.parse(string, **parse_kwargs_dict).date()
    if not has_month or not has_day:
        raise DateResolutionError(
            string,
            "the date does not contain both a month and a day",
            _coerce_period(period),
        )
    return resolve_date(month, day, period, value=string)


class DateProblem:
    """One unresolved date cell in an import receipt.

    The object exposes attributes and dict-style access so it can be
    inspected or serialized easily.

    """

    def __init__(
        self,
        value,
        reason,
        period=None,
        candidates=None,
        filename=None,
        line=None,
        column=None,
    ):
        self.value = value
        self.reason = reason
        self.period = _coerce_period(period)
        self.candidates = list(candidates or [])
        self.filename = filename
        self.line = line
        self.column = column

    @classmethod
    def from_error(cls, error, filename=None, line=None, column=None):
        return cls(
            error.value,
            error.reason,
            error.period,
            error.candidates,
            filename=filename,
            line=line,
            column=column,
        )

    def __str__(self):
        location = ""
        if self.filename is not None:
            location = self.filename
            if self.line is not None:
                location += f":{self.line}"
            location += ": "
        column = f"{self.column}: " if self.column else ""
        message = f"{location}{column}cannot assign a year to {self.value!r}: {self.reason}"
        if self.period is not None:
            message += f" (statement period {self.period.start} to {self.period.end})"
        if len(self.candidates) > 1:
            years = ", ".join(str(candidate.year) for candidate in self.candidates)
            message += f"; possible years: {years}"
        return message

    def __repr__(self):
        return (
            f"DateProblem({self.value!r}, {self.reason!r}, "
            f"line={self.line!r}, column={self.column!r})"
        )

    def __getitem__(self, key):
        return getattr(self, key)

    def as_dict(self):
        return {
            "value": self.value,
            "reason": self.reason,
            "period": (self.period.start, self.period.end) if self.period else None,
            "candidates": list(self.candidates),
            "filename": self.filename,
            "line": self.line,
            "column": self.column,
        }


class Receipt(list):
    """A list of DateProblem items with a readable string form."""

    def __str__(self):
        return "\n".join(str(problem) for problem in self)

    def as_dicts(self):
        """Return the receipt as a list of plain dictionaries."""
        return [problem.as_dict() for problem in self]

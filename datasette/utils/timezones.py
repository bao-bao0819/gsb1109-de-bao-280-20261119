import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from datasette.utils.asgi import BadRequest

EMPTY_DATE = "empty"
INVALID_DATE = "invalid"

# Query string arguments that select the business timezone. Datasette's
# special-argument convention is a leading underscore, which also keeps these
# out of the column filters. Column-scoped forms are accepted too, so callers
# can associate a timezone with the date facet they are using.
TIMEZONE_ARGUMENTS = (
    "_tz",
    "_timezone",
    "_date_tz",
    "_date_timezone",
    "_facet_date_tz",
    "_facet_date_timezone",
)

# These are only treated as timezone selectors when a date facet or __date
# filter is active, so an ordinary ?tz= column filter keeps working.
BARE_TIMEZONE_ARGUMENTS = ("tz", "timezone", "date_tz", "date_timezone")


def _is_date_only(value):
    # A bare date has no instant to convert, so it should keep its own date in
    # any timezone. Timestamps with a time (even midnight) are converted.
    return (
        len(value) == 10
        and value[4] == "-"
        and value[7] == "-"
        and value[:4].isdigit()
        and value[5:7].isdigit()
        and value[8:].isdigit()
    )


def _parse_datetime(value):
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        # Datasette's existing date() behavior treats naive timestamps as UTC.
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


@lru_cache(maxsize=128)
def get_timezone(name):
    return ZoneInfo(name)


def validate_timezone(name):
    """Return name if it is a valid IANA timezone, raising ValueError otherwise."""
    try:
        get_timezone(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as ex:
        raise ValueError(f"Invalid timezone: {name}") from ex
    return name


def timezone_date(value, timezone):
    """
    Return the date a timestamp falls on in the named business timezone.

    Returns None for an empty value and ``INVALID_DATE`` for a value that
    cannot be parsed. A bare ``YYYY-MM-DD`` value keeps that date unchanged.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return INVALID_DATE
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    if not text:
        return None
    if _is_date_only(text):
        try:
            return datetime.date.fromisoformat(text).isoformat()
        except ValueError:
            return INVALID_DATE
    try:
        parsed = _parse_datetime(text)
        return parsed.astimezone(get_timezone(timezone)).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return INVALID_DATE


def timezone_argument(request):
    """Return (argument_name, value) for the timezone selected by a request."""
    for name in TIMEZONE_ARGUMENTS:
        value = request.args.get(name)
        if value is not None:
            value = value.strip()
            if not value:
                return None
            try:
                validate_timezone(value)
            except ValueError as ex:
                raise BadRequest(str(ex)) from ex
            return name, value
    if date_filter_requested(request):
        for name in BARE_TIMEZONE_ARGUMENTS:
            value = request.args.get(name)
            if value is not None:
                value = value.strip()
                if not value:
                    return None
                try:
                    validate_timezone(value)
                except ValueError:
                    # A bare argument might be an ordinary column filter.
                    return None
                return name, value
    return None


def timezone_from_request(request):
    """Return the validated timezone from the query string, or None."""
    argument = timezone_argument(request)
    return argument[1] if argument else None


def date_filter_requested(request):
    if request.args.get("_facet_date"):
        return True
    return any(
        key.endswith("__date") or key == "date" for key in request.args.keys()
    )


def register_timezone_sql_function(connection):
    """
    Register ``datasette_date_in_timezone(value, timezone)`` on a connection.

    The timezone is a bound parameter rather than captured state so one
    connection can serve requests that select different business timezones.
    """

    def _date_in_timezone(value, name):
        if not name:
            return timezone_date(value, "UTC")
        return timezone_date(value, name)

    connection.create_function(
        "datasette_date_in_timezone",
        2,
        _date_in_timezone,
        deterministic=True,
    )


def _timezone_from_config(config, column=None):
    if isinstance(config, str):
        return config
    if not isinstance(config, dict):
        return None
    if column and isinstance(config.get("columns"), dict):
        column_config = config["columns"].get(column)
        if isinstance(column_config, str):
            return column_config
        if isinstance(column_config, dict) and column_config.get("timezone"):
            return column_config["timezone"]
    timezone = config.get("timezone")
    return timezone


def config_timezone(table_config, column=None):
    """Timezone configured for a date facet column, or for the whole table."""
    table_config = table_config or {}
    for facet_config in table_config.get("facets") or []:
        if not isinstance(facet_config, dict):
            continue
        date_config = facet_config.get("date")
        if date_config is None:
            continue
        if isinstance(date_config, str):
            if column is None or date_config == column:
                configured = _timezone_from_config(facet_config)
                if configured:
                    return configured
        elif isinstance(date_config, dict):
            date_column = date_config.get("column") or date_config.get("simple")
            if column is not None and date_column not in (None, column):
                continue
            configured = _timezone_from_config(date_config, column)
            if configured:
                return configured
            configured = _timezone_from_config(facet_config, column)
            if configured:
                return configured
    return _timezone_from_config(table_config, column)


def resolve_timezone(request, table_config=None):
    """
    Pick the timezone for a table: an explicit query string argument wins over
    configuration for the table or its date facet. Returns None when timezone
    support is not enabled for this request.
    """
    timezone = timezone_from_request(request)
    if timezone is not None:
        return timezone
    configured = config_timezone(table_config)
    if configured:
        try:
            return validate_timezone(str(configured).strip())
        except ValueError as ex:
            raise BadRequest(str(ex)) from ex
    return None


def resolve_column_timezone(request, table_config, column):
    """Timezone for one date facet column: query string, then configuration."""
    timezone = timezone_from_request(request)
    if timezone is not None:
        return timezone
    configured = config_timezone(table_config, column)
    if configured:
        try:
            return validate_timezone(str(configured).strip())
        except ValueError as ex:
            raise BadRequest(str(ex)) from ex
    return None


def timezone_state_argument(request, table_config=None, column=None):
    """
    Return a (name, value) pair needed to carry this timezone in a copied URL.

    An explicit query string argument is preserved as-is. A timezone that came
    only from table configuration is added as ``_tz`` so the copied link keeps
    the same business-day interpretation.
    """
    argument = timezone_argument(request)
    if argument:
        return argument
    if column is not None:
        timezone = resolve_column_timezone(request, table_config, column)
    else:
        timezone = resolve_timezone(request, table_config)
    if timezone:
        return "_tz", timezone
    return None


def timezone_cursor_argument(request, table_config=None):
    """Value used to invalidate a ?_next= cursor if the timezone changes."""
    timezone = resolve_timezone(request, table_config)
    if not timezone:
        return None
    argument = timezone_argument(request)
    if argument:
        # Keep the cursor tied to the exact state the user chose.
        return "{}={}".format(argument[0], argument[1])
    return "{}={}".format("_tz", timezone)

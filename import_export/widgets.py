import json
import logging
import numbers
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import django
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import F, Model
from django.utils import timezone
from django.utils.dateparse import parse_duration
from django.utils.encoding import force_str, smart_str
from django.utils.formats import number_format, sanitize_separators
from django.utils.translation import gettext_lazy as _

from import_export.exceptions import WidgetError
from import_export.utils import resolve_model

logger = logging.getLogger(__name__)


def format_datetime(value, datetime_format):
    # handle correct formatting of dates
    # see https://code.djangoproject.com/ticket/32738
    format_ = django.utils.formats.sanitize_strftime_format(datetime_format)
    return value.strftime(format_)


class _ParseDateTimeMixin:
    """Internal Mixin for shared logic with date and datetime conversions."""

    def __init__(
        self,
        format=None,
        input_formats=None,
        default_format="%Y-%m-%d",
        coerce_to_string=True,
    ):
        super().__init__(coerce_to_string=coerce_to_string)
        self.formats = (format,) if format else (input_formats or (default_format,))

    def _parse_value(self, value, value_type):
        """Attempt to parse the value using the provided formats.
        Raise ValueError if parsing fails."""
        if not value:
            return None
        if value_type is date:
            if isinstance(value, datetime):
                if settings.USE_TZ and timezone.is_aware(value):
                    value = timezone.make_naive(value, timezone.get_default_timezone())
                return value.date()
            if isinstance(value, date):
                return value
        elif value_type is datetime:
            if isinstance(value, datetime):
                return value
            if isinstance(value, date):
                return datetime(value.year, value.month, value.day)
        elif value_type is time:
            if isinstance(value, time):
                return value
            if isinstance(value, datetime):
                return value.time()

        for format_ in self.formats:
            try:
                parsed_date = datetime.strptime(value, format_)
                if value_type is date:
                    return parsed_date.date()
                if value_type is time:
                    return parsed_date.time()
                return parsed_date
            except (ValueError, TypeError) as e:
                logger.debug(str(e))
        raise ValueError("Value could not be parsed using defined formats.")

    #: The largest difference absorbed when a numeric (e.g. xlsx) round-trip
    #: rounds a value.  Spreadsheet storage is accurate to well under a
    #: millisecond, so any larger difference is a genuine change.  Set to
    #: ``timedelta(0)`` to require an exact match.
    rounding_tolerance = timedelta(milliseconds=1)

    def is_equal(self, instance_value, original_value, **kwargs):
        """
        Compare two ``datetime`` or ``time`` values, treating the imported
        value as unchanged if it could be an unmodified export of the
        persisted value.

        The imported value is considered unchanged if it is within
        :attr:`rounding_tolerance` of either:

        1. the persisted value itself.  This is what a native (xlsx) export
           produces, allowing for the spreadsheet's own numeric rounding,
           which can carry a value across a second boundary (for example
           ``12:54:31.999900`` becoming ``12:54:32.000000``).
        2. the persisted value after a round-trip through this widget's
           :meth:`render` and :meth:`clean`.  This is what a text export
           produces, at whatever precision the widget's format preserves.
           The default formats (``"%Y-%m-%d %H:%M:%S"`` and ``"%H:%M:%S"``)
           drop sub-second precision, so re-importing unmodified,
           previously-exported data yields a value which differs from the
           persisted one only by its microseconds.  See #2018.

        A genuine edit satisfies neither, so for example ``12:54:31.500000``
        persisted and ``12:54:32`` imported is correctly detected as a change,
        as is a sub-second edit when the format or export preserves
        microseconds.  The one ambiguity is a value within
        :attr:`rounding_tolerance` below a second boundary which is edited to
        that boundary: it is indistinguishable from spreadsheet rounding and
        is treated as unchanged.

        Values are compared as instants, so timezone-aware values in different
        zones compare correctly, including during a daylight-saving fold.  A
        timezone-aware value is never equal to a naive one.  Values which are
        not both ``datetime`` or both ``time`` instances (for example ``None``
        or ``date`` values) are compared with plain equality.

        :param instance_value: The value on the instance after the row has
            been applied.
        :param original_value: The value from the previously persisted
            instance.
        :returns: ``True`` if the values are equal under the rules above.
        """
        for value_type in (datetime, time):
            if isinstance(instance_value, value_type) and isinstance(
                original_value, value_type
            ):
                break
        else:
            return instance_value == original_value

        if (instance_value.utcoffset() is None) != (original_value.utcoffset() is None):
            # a timezone-aware value is never equal to a naive one
            return False
        if self._within_tolerance(instance_value, original_value):
            return True
        try:
            round_tripped = self.clean(self.render(original_value))
        except ValueError:
            # the persisted value cannot be rendered or re-parsed with this
            # widget's configuration, so no text round-trip can match it
            return False
        return isinstance(round_tripped, value_type) and self._within_tolerance(
            instance_value, round_tripped
        )

    def _within_tolerance(self, value, other):
        delta = abs(self._as_datetime(value) - self._as_datetime(other))
        return delta <= self.rounding_tolerance

    @staticmethod
    def _as_datetime(value):
        """
        Convert a ``time`` to a ``datetime`` on an arbitrary fixed date so that
        two values can be subtracted. Other values are returned unchanged.
        """
        if isinstance(value, time):
            return datetime.combine(date(2000, 1, 1), value)
        return value


class Widget:
    """
    A Widget handles converting between import and export representations.
    """

    def __init__(self, coerce_to_string=True):
        """
        :param coerce_to_string: If True, :meth:`~import_export.widgets.Widget.render`
          will return a string representation of the value, otherwise the value is
          returned.
        """
        self.coerce_to_string = coerce_to_string

    def clean(self, value, row=None, **kwargs):
        """
        Returns an appropriate python object for an imported value.
        For example, a date string will be converted to a python datetime instance.

        :param value: The value to be converted to a native type.
        :param row: A dict containing row key/value pairs.
        :param **kwargs: Optional kwargs.
        """
        return value

    def render(self, value, **kwargs):
        """
        Returns an export representation of a python value.

        :param value: The python value to be rendered.

        :return: By default, this value will be a string, with ``None`` values returned
          as empty strings.
        """
        return force_str(value) if value is not None else ""

    def is_equal(self, instance_value, original_value, **kwargs):
        r"""
        Compare the value on an instance being imported against the previously
        persisted value.  This is used by
        :meth:`~import_export.resources.Resource.skip_row` and by the import
        preview diff, which does not show a change for values that are equal
        but render differently (for example ``Decimal("1.60")`` and
        ``Decimal("1.6")``).

        By default this is a plain equality check. Widgets whose
        :meth:`render` loses precision on round-trip (for example,
        ``DateTimeWidget``'s default text format has no sub-second
        precision) should override this to compare values at the
        same precision that would actually survive an export, so
        that re-importing unmodified, previously-exported data is
        correctly detected as unchanged.

        :param instance_value: The value on the instance after the row has
            been applied.  For most fields this is the cleaned value from the
            import row, but for a readonly field, a column absent from the
            import file, or an empty cell where ``saves_null_values`` is
            ``False``, it is the persisted value itself.
        :param original_value: The value from the previously persisted
            instance.
        :param \**kwargs: Optional keyword arguments.  ``row`` is the import
            row as a ``dict``.
        """
        return instance_value == original_value


class NumberWidget(Widget):
    """
    Widget for converting numeric fields.
    """

    def is_empty(self, value):
        if isinstance(value, str):
            value = value.strip()
        # 0 is not empty
        return value is None or value == ""

    def render(self, value, **kwargs):
        if self.coerce_to_string and not kwargs.get("force_native_type"):
            return (
                ""
                if value is None or not isinstance(value, numbers.Number)
                else "" + number_format(value)
            )
        return value


class FloatWidget(NumberWidget):
    """
    Widget for converting float fields.
    """

    def clean(self, value, row=None, **kwargs):
        """
        Converts the input value to a Python float.

        :param value: The value to be converted to float. Can be a string or numeric
            type.
        :param row: The current row being processed.
        :param **kwargs: Optional keyword arguments.
        :returns: A Python float instance, or None if value is empty.
        :raises ValueError: If the value cannot be converted to float.
        """
        if self.is_empty(value):
            return None
        return float(sanitize_separators(value))


class IntegerWidget(NumberWidget):
    """
    Widget for converting integer fields.
    """

    def clean(self, value, row=None, **kwargs):
        """
        Converts the input value to a Python integer.

        Uses Decimal for precise conversion to handle locale-specific number formatting.

        :param value: The value to be converted to integer. Can be a string or
            numeric type.
        :param row: The current row being processed.
        :param **kwargs: Optional keyword arguments.
        :returns: A Python int instance, or None if value is empty.
        :raises ValueError: If the value cannot be converted to integer, or if
            its magnitude exceeds ``sys.get_int_max_str_digits()`` digits.
        :raises InvalidOperation: If Decimal conversion fails.
        """
        if self.is_empty(value):
            return None
        d = Decimal(sanitize_separators(value))
        # Reject values whose magnitude would make the Decimal-to-int conversion
        # disproportionately expensive. ``sys.set_int_max_str_digits`` does not
        # guard ``Decimal.__int__``, so the same limit is applied explicitly.
        # See GHSA-fcgw-hjjv-4rh6.
        max_digits = sys.get_int_max_str_digits()
        if d.is_finite() and d.adjusted() >= max_digits:
            raise ValueError(
                f"the value's magnitude exceeds the maximum of {max_digits} "
                "digits allowed for integer conversion"
            )
        return int(d)


class DecimalWidget(NumberWidget):
    """
    Widget for converting decimal fields.
    """

    def clean(self, value, row=None, **kwargs):
        """
        Converts the input value to a Python Decimal for precise numeric operations.

        :param value: The value to be converted to Decimal. Can be a string or
            numeric type.
        :param row: The current row being processed.
        :param **kwargs: Optional keyword arguments.
        :returns: A Python Decimal instance, or None if value is empty.
        :raises InvalidOperation: If the value cannot be converted to Decimal.
        """
        if self.is_empty(value):
            return None
        return Decimal(force_str(sanitize_separators(value)))


class CharWidget(Widget):
    """
    Widget for converting text fields.

    :param allow_blank:  If True, then :meth:`~import_export.widgets.Widget.clean`
      will return null values as empty strings, otherwise as ``None``.
    """

    def __init__(self, coerce_to_string=True, allow_blank=True):
        """ """
        self.allow_blank = allow_blank
        super().__init__(coerce_to_string)

    def clean(self, value, row=None, **kwargs):
        """
        Converts the input value to a string, handling None values based on
        allow_blank setting.

        :param value: The value to be converted to string.
        :param row: The current row being processed.
        :param **kwargs: Optional keyword arguments.
        :returns: A string representation of the value. Returns empty string if
            value is None
            and ``allow_blank`` is True, otherwise returns None.
        """
        val = super().clean(value, row, **kwargs)
        if val is None:
            return "" if self.allow_blank is True else None
        return force_str(val)

    def render(self, value, **kwargs):
        if self.coerce_to_string:
            return "" if value is None else force_str(value)
        return value


class BooleanWidget(Widget):
    """
    Widget for converting boolean fields.

    The widget assumes that ``True``, ``False``, and ``None`` are all valid
    values, as to match Django's `BooleanField
    <https://docs.djangoproject.com/en/dev/ref/models/fields/#booleanfield>`_.
    That said, whether the database/Django will actually accept NULL values
    will depend on if you have set ``null=True`` on that Django field.

    Recognizes standard boolean representations. For custom boolean values,
    see :ref:`custom_boolean_handling` in the advanced usage documentation.
    """

    TRUE_VALUES = ["1", 1, True, "true", "TRUE", "True"]
    FALSE_VALUES = ["0", 0, False, "false", "FALSE", "False"]
    NULL_VALUES = ["", None, "null", "NULL", "none", "NONE", "None"]

    def __init__(self, coerce_to_string=True):
        """ """
        super().__init__(coerce_to_string)

    def clean(self, value, row=None, **kwargs):
        """
        Converts the input value to a Python boolean or None.

        Recognizes common string representations of boolean values:
        - True values: '1', 1, True, 'true', 'TRUE', 'True'
        - False values: '0', 0, False, 'false', 'FALSE', 'False'
        - Null values: '', None, 'null', 'NULL', 'none', 'NONE', 'None'

        :param value: The value to be converted to boolean.
        :param row: The current row being processed.
        :param **kwargs: Optional keyword arguments.
        :returns: True, False, or None depending on the input value.
        """
        if value in self.NULL_VALUES:
            return None
        return True if value in self.TRUE_VALUES else False

    def render(self, value, **kwargs):
        """
        :return: ``True`` is represented as ``1``, ``False`` as ``0``, and
          ``None``/NULL as an empty string.

          If ``coerce_to_string`` is ``False``, the python Boolean type is
          returned (may be ``None``).
        """
        if self.coerce_to_string and not kwargs.get("force_native_type"):
            if value in self.NULL_VALUES or not type(value) is bool:
                return ""
            return self.TRUE_VALUES[0] if value else self.FALSE_VALUES[0]
        return value


class DateWidget(_ParseDateTimeMixin, Widget):
    """
    Widget for converting date fields to Python date instances.

    Takes optional ``format`` parameter. If none is set, either
    ``settings.DATE_INPUT_FORMATS`` or ``"%Y-%m-%d"`` is used.
    """

    def __init__(self, format=None, coerce_to_string=True):
        super().__init__(
            format, settings.DATE_INPUT_FORMATS, "%Y-%m-%d", coerce_to_string
        )

    def clean(self, value, row=None, **kwargs):
        """
        Converts a date string to a Python date instance using configured formats.

        Attempts to parse the value using formats specified during widget
        initialization.
        If no format was provided, uses ``settings.DATE_INPUT_FORMATS`` or "%Y-%m-%d".

        :param value: A date string to be parsed, or an existing date or datetime
            instance (datetimes are converted to dates).
        :param row: The current row being processed.
        :param **kwargs: Optional keyword arguments.
        :returns: A Python date instance, or None if value is empty.
        :raises ValueError: If the value cannot be parsed using any of the
            defined formats.
        """
        return self._parse_value(value, date)

    def render(self, value, **kwargs):
        if self.coerce_to_string is False or kwargs.get("force_native_type"):
            return value
        if not value or not isinstance(value, date):
            return ""
        return format_datetime(value, self.formats[0])


class DateTimeWidget(_ParseDateTimeMixin, Widget):
    """
    Widget for converting datetime fields to Python datetime instances.

    Takes optional ``format`` parameter. If none is set, either
    ``settings.DATETIME_INPUT_FORMATS`` or ``"%Y-%m-%d %H:%M:%S"`` is used.
    """

    def __init__(self, format=None, coerce_to_string=True):
        super().__init__(
            format,
            settings.DATETIME_INPUT_FORMATS,
            "%Y-%m-%d %H:%M:%S",
            coerce_to_string,
        )

    def clean(self, value, row=None, **kwargs):
        """
        :param value: A datetime string to be parsed, or an existing datetime or
            date instance (dates are converted to midnight datetimes).
        :returns: A python datetime instance.
        :raises: ValueError if the value cannot be parsed using defined formats.
        """
        dt = self._parse_value(value, datetime)
        if dt is None:
            return None
        if settings.USE_TZ and timezone.is_naive(dt):
            return timezone.make_aware(dt)
        return dt

    def render(self, value, **kwargs):
        if not value or not isinstance(value, datetime):
            return ""
        if settings.USE_TZ:
            value = timezone.localtime(value)

        force_native_type = kwargs.get("force_native_type")
        if self.coerce_to_string is False or force_native_type:
            # binary formats such as xlsx must not have tz set
            return value.replace(tzinfo=None) if force_native_type else value

        return format_datetime(value, self.formats[0])


class TimeWidget(_ParseDateTimeMixin, Widget):
    """
    Widget for converting time fields.

    Takes optional ``format`` parameter. If none is set, either
    ``settings.DATETIME_INPUT_FORMATS`` or ``"%H:%M:%S"`` is used.
    """

    def __init__(self, format=None, coerce_to_string=True):
        super().__init__(
            format, settings.TIME_INPUT_FORMATS, "%H:%M:%S", coerce_to_string
        )

    def clean(self, value, row=None, **kwargs):
        """
        :param value: A time string to be parsed, or an existing time or datetime
            instance (datetimes are converted to times).
        :returns: A python time instance.
        :raises: ValueError if the value cannot be parsed using defined formats.
        """
        return self._parse_value(value, time)

    def render(self, value, **kwargs):
        if self.coerce_to_string is False or kwargs.get("force_native_type"):
            return value
        if not value or not isinstance(value, time):
            return ""
        return value.strftime(self.formats[0])


class DurationWidget(Widget):
    """
    Widget for converting time duration fields.
    """

    def clean(self, value, row=None, **kwargs):
        """
        :returns: A python duration instance.
        :raises: ValueError if the value cannot be parsed.
        """
        if not value:
            return None

        try:
            return parse_duration(value)
        except (ValueError, TypeError) as e:
            logger.debug(str(e))
            raise ValueError(_("Value could not be parsed."))

    def render(self, value, **kwargs):
        if self.coerce_to_string is False or kwargs.get("force_native_type"):
            return value
        if value is None or not type(value) is timedelta:
            return ""
        return str(value)


class SimpleArrayWidget(Widget):
    """
    Widget for an Array field. Can be used for Postgres' Array field.

    :param separator: Defaults to ``','``
    """

    def __init__(self, separator=None, coerce_to_string=True):
        if separator is None:
            separator = ","
        self.separator = separator
        super().__init__(coerce_to_string=coerce_to_string)

    def clean(self, value, row=None, **kwargs):
        """
        Converts a separated string into a Python array.

        Splits the input string by the configured separator. Empty strings result
        in empty arrays rather than arrays containing empty strings.

        :param value: A string containing values separated by the configured separator.
        :param row: The current row being processed.
        :param **kwargs: Optional keyword arguments.
        :returns: A Python list derived from splitting the value by separator.
            Returns an empty list if value is None or empty.
        """
        return value.split(self.separator) if value else []

    def render(self, value, **kwargs):
        """
        :return: A string with values separated by ``separator``.
          If ``coerce_to_string`` is ``False``, the native array will be returned.
          If ``value`` is None, None will be returned if ``coerce_to_string``
            is ``False``, otherwise an empty string will be returned.
        """
        if value is None:
            return "" if self.coerce_to_string is True else None
        if not self.coerce_to_string:
            return value
        return self.separator.join(str(v) for v in value)


class JSONWidget(Widget):
    """
    Widget for a JSON object
    (especially required for jsonb fields in PostgreSQL database.)

    :param value: Defaults to JSON format.
    The widget covers two cases: Proper JSON string with double quotes, else it
    tries to use single quotes and then convert it to proper JSON.
    """

    def clean(self, value, row=None, **kwargs):
        """
        Parses the input value as JSON and returns the corresponding Python object.

        Attempts to parse as valid JSON first, then falls back to single-quote format
        by converting single quotes to double quotes before parsing.

        :param value: A JSON string to be parsed.
        :param row: The current row being processed.
        :param **kwargs: Optional keyword arguments.
        :returns: The parsed Python object (dict, list, etc.) or None if value is empty.
        :raises JSONDecodeError: If the value cannot be parsed as JSON.
        """
        val = super().clean(value)
        if val is not None and val != "":
            try:
                return json.loads(val)
            except json.decoder.JSONDecodeError:
                return json.loads(val.replace("'", '"'))

    def render(self, value, **kwargs):
        """
        Set the ``sort_json_keys`` keyword argument to ``True`` to sort the
        keys in JSON objects.

        :return: A JSON formatted string derived from ``value``.
          ``coerce_to_string`` has no effect on the return value.
        """
        if value is None:
            return None
        return json.dumps(value, sort_keys=kwargs.get("sort_json_keys", False))


class _LookupField:
    """
    Internal description of one component of a composite foreign key lookup.

    ``model_field`` is a field (or ``__`` path) on the related model.
    ``row_key`` is the column in the import row which supplies the value.
    """

    __slots__ = ("model_field", "row_key")

    def __init__(self, model_field, row_key=None):
        self.model_field = model_field
        self.row_key = row_key

    def __repr__(self):
        return (
            f"<_LookupField model_field={self.model_field!r} "
            f"row_key={self.row_key!r}>"
        )


def _resolve_attr(value, path):
    for attr in path.split("__"):
        value = getattr(value, attr, None)
        if value is None:
            return None
    return value


def _relation_model_for_path(model, path):
    """
    Return the related model when ``path`` ends at a relation on ``model``,
    otherwise ``None``.
    """
    current = model
    field = None
    parts = path.split("__")
    for index, part in enumerate(parts):
        try:
            field = current._meta.get_field(part)
        except Exception:
            return None
        if field.is_relation:
            current = field.related_model
        elif index < len(parts) - 1:
            return None
    if field is not None and field.is_relation:
        return field.related_model
    return None


def _supports_natural_key(model):
    return hasattr(model, "natural_key") and hasattr(
        model.objects, "get_by_natural_key"
    )


def _natural_key_value(value):
    """
    Return a JSON friendly natural key.  A single element key is unwrapped so
    that a component stays scalar in a rendered composite value.
    """
    natural_key = value.natural_key()
    if len(natural_key) == 1:
        return natural_key[0]
    return list(natural_key)


class RelatedModelWidgetMixin:
    """
    Mixin for widgets which reference a related model.

    The ``model`` attribute may be set either to a model class or to an
    ``"app_label.ModelName"`` string, so that a resource does not need to
    import models from other apps directly.  A string is resolved to the model
    class via the app registry immediately if the registry is ready, so that a
    misspelled name fails at startup.  If the registry is not yet ready, the
    string is resolved the first time ``model`` is accessed.
    """

    @property
    def model(self):
        if isinstance(self._model, str):
            self._model = resolve_model(self._model)
        return self._model

    @model.setter
    def model(self, value):
        if isinstance(value, str) and apps.models_ready:
            value = resolve_model(value)
        self._model = value


class ForeignKeyWidget(RelatedModelWidgetMixin, Widget):
    """
    Widget for a ``ForeignKey`` field which looks up a related model using
    either the PK or a user specified field that uniquely identifies the
    instance in both export and import.

    The lookup field defaults to using the primary key (``pk``) as lookup
    criterion but can be customized to use any field on the related model.

    Unlike specifying a related field in your resource like so…

    ::

        class Meta:
            fields = ('author__name',)

    …using a :class:`~import_export.widgets.ForeignKeyWidget` has the
    advantage that it can not only be used for exporting, but also importing
    data with foreign key relationships.

    Here's an example on how to use
    :class:`~import_export.widgets.ForeignKeyWidget` to lookup related objects
    using ``Author.name`` instead of ``Author.pk``::

        from import_export import fields, resources
        from import_export.widgets import ForeignKeyWidget

        class BookResource(resources.ModelResource):
            author = fields.Field(
                column_name='author',
                attribute='author',
                widget=ForeignKeyWidget(Author, 'name'))

            class Meta:
                fields = ('author',)

    :param model: The Model the ForeignKey refers to (required).  Either a
        model class or an ``"app_label.ModelName"`` string.  See
        :class:`~import_export.widgets.RelatedModelWidgetMixin`.
    :param field: A field on the related model used for looking up a particular
        object.
    :param use_natural_foreign_keys: Use natural key functions to identify
        related object, default to False
    :param lookup_fields: An optional declaration which identifies the related
        object using more than one field.  This is needed when a single field
        value is only unique within a parent, for example a supplier specific
        city code.  The value can be a mapping of related model lookup paths to
        import column names::

            ForeignKeyWidget(
                City,
                field="code",
                lookup_fields={"supplier": "supplier", "code": "code"},
            )

        or a sequence of lookup paths (in which case the column name defaults
        to the related field name).  A sequence item may also be a
        ``(lookup_path, column_name)`` pair when the column name differs::

            ForeignKeyWidget(
                City,
                field="code",
                lookup_fields=[("supplier__name", "supplier"), "code"],
            )

        The lookup path which matches ``field`` is populated from the widget's
        own cell value; every other value is read from the import row.  If
        ``field`` is not present in ``lookup_fields`` it is added, so
        ``lookup_fields=["supplier"]`` is shorthand for looking up by the
        supplier and the widget's field value.
    """

    def __init__(
        self,
        model,
        field="pk",
        use_natural_foreign_keys=False,
        key_is_id=False,
        lookup_fields=None,
        fields=None,
        field_name=None,
        **kwargs,
    ):
        self.model = model
        if lookup_fields is None:
            lookup_fields = fields if fields is not None else field_name
        if isinstance(field, (list, tuple)):
            # A sequence of lookup paths is shorthand for a composite lookup;
            # the last component is supplied by this field's own cell.
            field, lookup_fields = field[-1], lookup_fields or list(field)
        self.lookup_fields = lookup_fields
        self._lookup_field_specs = self._normalise_lookup_fields(lookup_fields)
        if field == "pk" and self._lookup_field_specs:
            field = self._infer_composite_field()
        self.field = field
        self.key_is_id = key_is_id
        self.use_natural_foreign_keys = use_natural_foreign_keys
        if use_natural_foreign_keys is True and key_is_id is True:
            raise WidgetError(
                _("use_natural_foreign_keys and key_is_id cannot both be True")
            )
        super().__init__(**kwargs)

    def _infer_composite_field(self):
        """
        Choose the component supplied by the widget's own cell when ``field``
        is left at its ``pk`` default.  A ``code`` component is the common
        case; otherwise the last declared component is used.
        """
        paths = [spec.model_field for spec in self._lookup_field_specs]
        if "pk" in paths:
            return "pk"
        for candidate in ("code", "name", "id"):
            if candidate in paths:
                return candidate
        return paths[-1]

    @staticmethod
    def _normalise_lookup_fields(lookup_fields):
        if not lookup_fields:
            return ()
        if isinstance(lookup_fields, str):
            return (_LookupField(lookup_fields),)
        if isinstance(lookup_fields, dict):
            items = lookup_fields.items()
        else:
            items = lookup_fields
        specs = []
        for item in items:
            if isinstance(item, str):
                specs.append(_LookupField(item))
                continue
            try:
                path, column = item
            except (TypeError, ValueError):
                raise WidgetError(
                    _(
                        "lookup_fields items must be a lookup path or a "
                        "(lookup path, column name) pair"
                    )
                )
            specs.append(_LookupField(path, column))
        return tuple(specs)

    def _is_value_component(self, spec):
        return spec.model_field == self.field

    def _get_component_value(self, spec, value, row, parsed=None):
        parsed_value = self._parsed_component(spec, parsed)
        if parsed_value is not None:
            return self._resolve_component(spec, parsed_value)
        if self._is_value_component(spec):
            return value
        if row is None:
            raise KeyError(
                "Column '%s' is required to look up %s but no row was supplied."
                % (spec.row_key or spec.model_field, self.model.__name__)
            )
        keys = []
        if spec.row_key:
            keys.append(spec.row_key)
        keys.extend(
            [
                spec.model_field,
                spec.model_field.split("__")[-1],
                spec.model_field.split("__")[0],
            ]
        )
        for key in keys:
            if key in row:
                return self._resolve_component(spec, row[key])
        raise KeyError(
            "Column '%s' not found in dataset. Available columns are: %s"
            % (keys[0], list(row))
        )

    def _resolve_component(self, spec, component):
        """
        Resolve a component which addresses a relation.

        Export renders relations as their natural key, so re-importing an
        exported value needs to turn that natural key back into a model
        instance.  A primary key is also accepted, so a value which is not a
        natural key is tried as a pk before the lookup is reported as missing.
        Non-relation components are returned unchanged.
        """
        related_model = _relation_model_for_path(self.model, spec.model_field)
        if related_model is None or isinstance(component, Model):
            return component
        if not _supports_natural_key(related_model):
            return component
        if isinstance(component, (list, tuple)):
            natural_key = component
        else:
            natural_key = (component,)
        try:
            return related_model.objects.get_by_natural_key(*natural_key)
        except related_model.DoesNotExist:
            pass
        try:
            return related_model._default_manager.get(pk=component)
        except (related_model.DoesNotExist, TypeError, ValueError):
            raise related_model.DoesNotExist(
                "%s matching query does not exist for %s value %r."
                % (related_model.__name__, spec.model_field, component)
            )

    def _split_composite_value(self, value):
        """
        Split a value rendered by a composite lookup back into its components.

        Both the list form produced by :meth:`render` and a mapping of lookup
        paths to component values are accepted.  ``None`` is returned for
        values which are not a complete rendered representation.
        """
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if not stripped or stripped[0] not in "[{":
            return None
        try:
            components = json.loads(stripped)
        except (TypeError, ValueError):
            return None
        if not isinstance(components, (list, dict)):
            return None
        return components

    def _parsed_component(self, spec, parsed):
        if parsed is None:
            return None
        if isinstance(parsed, dict):
            for key in (spec.model_field, spec.model_field.split("__")[-1]):
                if key in parsed:
                    return parsed[key]
            return None
        index = self._get_lookup_field_specs().index(spec)
        if index < len(parsed):
            return parsed[index]
        return None

    def _get_lookup_field_specs(self):
        if not self._lookup_field_specs:
            return ()
        if any(self._is_value_component(s) for s in self._lookup_field_specs):
            return self._lookup_field_specs
        return self._lookup_field_specs + (_LookupField(self.field),)

    def _lookup_keys(self):
        """
        Return the lookup paths used to identify the related object, without
        querying the database.
        """
        specs = self._get_lookup_field_specs()
        if specs:
            return [spec.model_field for spec in specs]
        return [self.field]

    def _has_custom_lookup_kwargs(self):
        """Whether ``get_lookup_kwargs`` has been overridden by a subclass."""
        implementation = type(self).get_lookup_kwargs
        return implementation not in (
            ForeignKeyWidget.get_lookup_kwargs,
            CachedForeignKeyWidget.get_lookup_kwargs,
        )

    def _has_composite_lookup(self):
        return bool(self._lookup_field_specs) or self._has_custom_lookup_kwargs()

    def _render_natural_key(self, value):
        if _supports_natural_key(self.model) and hasattr(value, "natural_key"):
            return json.dumps(value.natural_key())
        return None

    def _render_components(self, value):
        """
        Render each declared lookup component as a JSON list.

        A component addressing a relation is rendered as that object's
        natural key (or its primary key if it has no natural key), so the
        original supplier key is carried in the output.
        """
        components = []
        for spec in self._get_lookup_field_specs():
            component = _resolve_attr(value, spec.model_field)
            if component is None:
                return None
            if isinstance(component, Model):
                if hasattr(component, "natural_key"):
                    component = _natural_key_value(component)
                else:
                    component = component.pk
            components.append(component)
        return json.dumps(components, default=str)

    def get_queryset(self, value, row, *args, **kwargs):
        """
        Returns a queryset of all objects for this Model.

        Overwrite this method if you want to limit the pool of objects from
        which the related object is retrieved.

        :param value: The field's value in the dataset.
        :param row: The dataset's current row.
        :param \\*args:
            Optional args.
        :param \\**kwargs:
            Optional kwargs.

        As an example; if you'd like to have ForeignKeyWidget look up a Person
        by their pre- **and** lastname column, you could subclass the widget
        like so::

            class FullNameForeignKeyWidget(ForeignKeyWidget):
                def get_queryset(self, value, row, *args, **kwargs):
                    return self.model.objects.filter(
                        first_name__iexact=row["first_name"],
                        last_name__iexact=row["last_name"]
                    )
        """
        return self.model.objects.all()

    def clean(self, value, row=None, **kwargs):
        """
        :return: a single Foreign Key instance derived from the args.
          ``None`` can be returned if the value passed is a null value.

        :param value: The field's value in the dataset.
        :param row: The dataset's current row.
        :param \\**kwargs:
            Optional kwargs.
        :raises: ``ObjectDoesNotExist`` if no valid instance can be found.
        """
        val = super().clean(value)
        if val:
            if self._has_composite_lookup():
                obj = self.get_instance_by_composite_key(value, row, **kwargs)
                return obj.pk if self.key_is_id else obj
            if self.use_natural_foreign_keys:
                return self.get_instance_by_natural_key(value)
            obj = self.get_instance_by_lookup_fields(value, row, **kwargs)
            if self.key_is_id:
                return obj.pk
            return obj
        return None

    def get_instance_by_composite_key(self, value, row, **kwargs):
        """
        Return the related object for a composite lookup.

        A value previously rendered by :meth:`render` is the related object's
        natural key, so try that first.  Otherwise the value is the field's own
        component (for example the supplier specific city code), and the
        remaining components are read from ``row`` by
        :meth:`get_lookup_kwargs`.
        """
        parsed = self._split_composite_value(value)
        if isinstance(parsed, list) and _supports_natural_key(self.model):
            try:
                return self.get_instance_by_natural_key(value)
            except (ObjectDoesNotExist, TypeError, ValueError):
                pass
        try:
            return self.get_instance_by_lookup_fields(value, row, **kwargs)
        except ObjectDoesNotExist as e:
            raise self.model.DoesNotExist(
                "%s matching query does not exist for lookup fields %s "
                "with value %r and row %r."
                % (
                    self.model._meta.object_name,
                    self._lookup_keys(),
                    value,
                    dict(row) if row is not None else None,
                )
            ) from e

    def get_instance_by_natural_key(self, value):
        # natural keys will always be a tuple, which ends up as a json list.
        value = json.loads(value)
        return self.model.objects.get_by_natural_key(*value)

    def get_instance_by_lookup_fields(self, value, row, **kwargs):
        lookup_kwargs = self.get_lookup_kwargs(value, row, **kwargs)
        return self.get_queryset(value, row, **kwargs).get(**lookup_kwargs)

    def get_lookup_kwargs(self, value, row, **kwargs):
        """
        :return: the key value pairs used to identify a model instance.
          Override this to customize instance lookup.

        :param value: The field's value in the dataset.
        :param row: The dataset's current row.
        :param \\**kwargs:
            Optional kwargs.

        If ``lookup_fields`` is declared, every declared component is
        included, with the component matching ``field`` taken from ``value``
        and the remaining components taken from ``row``.
        """
        specs = self._get_lookup_field_specs()
        if specs:
            parsed = self._split_composite_value(value)
            return {
                spec.model_field: self._get_component_value(
                    spec, value, row, parsed
                )
                for spec in specs
            }
        return {self.field: value}

    def render(self, value, **kwargs):
        """
        :return: A string representation of the related value.
          If ``use_natural_foreign_keys``, the value's natural key is returned.
          ``coerce_to_string`` has no effect on the return value.
        """

        if self.key_is_id:
            return value or ""

        if value is None:
            return ""

        if self._has_composite_lookup():
            # A single component cannot represent a composite lookup.  Prefer
            # the related object's natural key, which carries the supplier's
            # own key and can be imported back into the same lookup.
            natural_key = self._render_natural_key(value)
            if natural_key is not None:
                return natural_key
            if self._lookup_field_specs:
                return self._render_components(value)

        attrs = self.field.split("__")
        for attr in attrs:
            try:
                if self.use_natural_foreign_keys:
                    # inbound natural keys must be a json list.
                    return json.dumps(value.natural_key())
                else:
                    value = getattr(value, attr, None)
            except (ValueError, ObjectDoesNotExist):
                # needs to have a primary key value before a many-to-many
                # relationship can be used.
                return None
            if value is None:
                return None

        return value


class _CachedQuerySetWrapper:
    """
    A wrapper around a Django QuerySet that caches its results in a dictionary
    for quick lookups.

    This class has the same 'get()' method signature as a QuerySet
    because it is intended to be used as a drop-in replacement for QuerySet
    in case of ForeignKeyWidget that calls 'get()' method for every row
    in the import dataset.
    """

    def __init__(self, queryset):
        self.queryset = queryset
        self.model = queryset.model
        self._instances = None
        self._indexes = {}

    def _get_instances(self):
        if self._instances is None:
            self._instances = list(self.queryset)
        return self._instances

    @staticmethod
    def _key_value(value):
        if value is None:
            return None
        if isinstance(value, Model):
            return value.pk
        return str(value)

    def _get_index(self, field_names):
        key = tuple(field_names)
        index = self._indexes.get(key)
        if index is None:
            index = defaultdict(list)
            for instance in self._get_instances():
                values = tuple(
                    self._key_value(_resolve_attr(instance, field))
                    for field in field_names
                )
                index[values].append(instance)
            self._indexes[key] = index
        return index

    def get(self, **lookup_fields):
        field_names = tuple(lookup_fields.keys())
        index = self._get_index(field_names)
        key = tuple(
            self._key_value(lookup_fields[field]) for field in field_names
        )
        result = index.get(key, [])

        if len(result) == 1:
            return result[0]

        if len(result) == 0:
            raise self.model.DoesNotExist(
                "%s matching query does not exist." % self.model._meta.object_name
            )
        raise self.model.MultipleObjectsReturned(
            "get() returned more than one %s -- it returned %s!"
            % (self.model._meta.object_name, len(result))
        )


class CachedForeignKeyWidget(ForeignKeyWidget):
    """
    A :class:`~import_export.widgets.ForeignKeyWidget` subclass that caches
    the queryset results to minimize database hits during import. The default
    :class:`~import_export.widgets.ForeignKeyWidget` makes query for each row,
    which can be inefficient for large imports. This widget fetches all related
    instances once and caches them in memory for subsequent lookups.

    Using this class has some limitations:

    - It does not support caching when ``use_natural_foreign_keys=True`` is set.

    - It calls :meth:`~import_export.widgets.ForeignKeyWidget.get_queryset` only once,
      so if the queryset depends on the row data, this widget may not work as expected.
      You must be sure that the queryset is static for all rows.
      Avoid using :class:`~import_export.widgets.CachedForeignKeyWidget`
      in the following way::

            class FullNameForeignKeyWidget(CachedForeignKeyWidget):
                def get_queryset(self, value, row, *args, **kwargs):
                    return self.model.objects.filter(
                        first_name__iexact=row["first_name"],
                        last_name__iexact=row["last_name"]
                    )

      It makes more sense to filter by static values::

            class ActiveForeignKeyWidget(CachedForeignKeyWidget):
                def get_queryset(self, value, row, *args, **kwargs):
                    return self.model.objects.filter(active=True)

    - It stores data in a hash table where the key is a tuple of the fields that
      returned by :meth:`~import_export.widgets.ForeignKeyWidget.get_lookup_kwargs`.
      You must be sure that the lookup fields are the same for all rows.
      If the lookup fields differ between rows, this widget may not work as expected.
      The following example is incorrect usage::

            class MultiColumnForeignKeyWidget(CachedForeignKeyWidget):
                def get_lookup_kwargs(self, value, row, **kwargs):
                    if row['active'] == 'yes':
                        return {self.field: value, 'active': True}
                    else:
                        return {self.field: value, 'inactive': True}

    - It does not support complex lookups like ``__gt`` or ``__lt`` in the
    ``get_lookup_kwargs()``.
      For example, the following code won't work::

            class BookForeignKeyWidget(CachedForeignKeyWidget):
                def get_lookup_kwargs(self, value, row, **kwargs):
                    return {f'{self.field}__gt': value}

    :param model: The Model the ForeignKey refers to (required).  Either a
        model class or an ``"app_label.ModelName"`` string.  See
        :class:`~import_export.widgets.RelatedModelWidgetMixin`.
    :param field: A field on the related model used for looking up a particular
        object.
    :param use_natural_foreign_keys: Use natural key functions to identify
        related object, default to False
    """

    def _contains_relations(self):
        return "__" in self.field

    def get_queryset(self, value, row, *args, **kwargs):
        queryset = super().get_queryset(value, row, *args, **kwargs)
        for path in self._cached_lookup_paths():
            if "__" in path:
                queryset = queryset.annotate(
                    **{self._cached_lookup_alias(path): F(path)}
                )
        return queryset

    @staticmethod
    def _cached_lookup_alias(path):
        return "django_import_export_cached_lookup_%s" % path.replace("__", "_")

    def _cached_lookup_key(self, path):
        if "__" in path:
            return self._cached_lookup_alias(path)
        return path

    def _cached_lookup_paths(self):
        specs = self._get_lookup_field_specs()
        if specs:
            return [spec.model_field for spec in specs]
        return [self.field]

    def get_lookup_kwargs(self, value, row, **kwargs):
        specs = self._get_lookup_field_specs()
        if specs:
            return {
                self._cached_lookup_key(spec.model_field): self._get_component_value(
                    spec, value, row
                )
                for spec in specs
            }
        if self._contains_relations():
            return {self._cached_lookup_key(self.field): value}
        return super().get_lookup_kwargs(value, row, **kwargs)

    def get_instance_by_lookup_fields(self, value, row, **kwargs):
        if not hasattr(self, "_cached_qs"):
            queryset = self.get_queryset(value, row, **kwargs)
            self._cached_qs = _CachedQuerySetWrapper(queryset)

        lookup_kwargs = self.get_lookup_kwargs(value, row, **kwargs)
        return self._cached_qs.get(**lookup_kwargs)


class CompositeForeignKeyWidget(ForeignKeyWidget):
    """
    A :class:`~import_export.widgets.ForeignKeyWidget` which identifies the
    related object using more than one field.

    This is a convenience alias for ``ForeignKeyWidget`` with
    ``lookup_fields`` declared.  The related object is looked up by every
    declared component, so a code which is only unique within a parent (for
    example a supplier specific city code) resolves to the correct row.
    """


class CachedCompositeForeignKeyWidget(CachedForeignKeyWidget):
    """
    A :class:`~import_export.widgets.CachedForeignKeyWidget` which identifies
    the related object using more than one field.

    This is a convenience alias for ``CachedForeignKeyWidget`` with
    ``lookup_fields`` declared.
    """


class ManyToManyWidget(RelatedModelWidgetMixin, Widget):
    """
    Widget that converts between representations of a ManyToMany relationships
    as a list and an actual ManyToMany field.

    :param model: The model the ManyToMany field refers to (required).  Either
        a model class or an ``"app_label.ModelName"`` string.  See
        :class:`~import_export.widgets.RelatedModelWidgetMixin`.
    :param separator: Defaults to ``','``.
    :param field: A field on the related model. Default is ``pk``.
    """

    def __init__(self, model, separator=None, field=None, **kwargs):
        if separator is None:
            separator = ","
        if field is None:
            field = "pk"
        self.model = model
        self.separator = separator
        self.field = field
        super().__init__(**kwargs)

    def get_queryset(self, value, row, *args, **kwargs):
        """
        Returns a queryset of all objects for this Model.

        Override this method to limit the pool of objects from which related
        objects are retrieved.

        :param value: The field's value in the dataset.
        :param row: The dataset's current row.
        :param \\*args: Optional args.
        :param \\**kwargs: Optional kwargs.
        :return: A QuerySet containing all objects for this model.
        """
        return self.model.objects.all()

    def clean(self, value, row=None, **kwargs):
        """
        Converts a separated string of values into a QuerySet for ManyToMany
        relationships.

        Splits the input by the configured separator and looks up model instances
        using the specified field. Filters out empty values after splitting.

        :param value: String of separated values, or a single numeric value.
        :param row: The current row being processed.
        :param **kwargs: Optional keyword arguments.
        :returns: A QuerySet containing the related model instances, or an empty
            QuerySet
            if no value provided.
        """
        if not value:
            return self.model.objects.none()
        queryset = self.get_queryset(value, row, **kwargs)
        if isinstance(value, (float, int)):
            ids = [int(value)]
        else:
            ids = value.split(self.separator)
            ids = filter(None, [i.strip() for i in ids])
        return queryset.filter(**{"%s__in" % self.field: ids})

    def render(self, value, **kwargs):
        """
        :return: A string with values separated by ``separator``.
          ``None`` values are returned as empty strings.
          ``coerce_to_string`` has no effect on the return value.
        """
        if value is not None:
            ids = [smart_str(getattr(obj, self.field)) for obj in value.all()]
            return self.separator.join(ids)
        return ""

    def is_equal(self, instance_value, original_value, **kwargs):
        r"""
        Compare two collections of related objects, ignoring order.

        :param instance_value: An iterable of related model instances parsed
            from the imported row (see :meth:`clean`).
        :param original_value: An iterable of the related model instances
            currently persisted for the original instance.
        :param \**kwargs: Optional keyword arguments.  ``row`` is the import
            row as a ``dict``.
        :returns: ``True`` if both collections contain the same objects, as
            determined by primary key.
        """
        return sorted(v.pk for v in instance_value) == sorted(
            v.pk for v in original_value
        )

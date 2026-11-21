# -*- coding: utf-8 -*-

from __future__ import unicode_literals, absolute_import

import itertools

from ..common import *
from ..datastructures import Context
from ..exceptions import *
from ..provenance import (
    BatchIngestResult, IngestError, ListIngestResult, Provenance,
    ProvenanceTracker, Rejection, SourceCoordinate, TrackedItem,
    _attach_provenance)
from ..undefined import Undefined
from ..transforms import (
    export_loop,
    get_import_context, get_export_context,
    import_converter, to_native_converter, to_primitive_converter)
from ..translator import _
from ..util import get_all_subclasses, import_string
from .base import BaseType, get_value_in

try:
    import typing
except ImportError:
    pass
else:
    T = typing.TypeVar("T")

try:
    from collections.abc import Iterable, Sequence, Mapping  # PY3
except ImportError:
    from collections import Iterable, Sequence, Mapping  # PY2

__all__ = ['CompoundType', 'MultiType', 'ModelType', 'ListType', 'DictType',
    'PolyModelType']


class CompoundType(BaseType):

    def __init__(self, **kwargs):
        super(CompoundType, self).__init__(**kwargs)
        self.is_compound = True
        try:
            self.field.parent_field = self
        except AttributeError:
            pass

    def _setup(self, field_name, owner_model):
        # Recursively set up inner fields.
        if hasattr(self, 'field'):
            self.field._setup(None, owner_model)
        super(CompoundType, self)._setup(field_name, owner_model)

    def convert(self, value, context=None):
        context = context or get_import_context()
        return self._convert(value, context)

    def _convert(self, value, context):
        raise NotImplementedError

    def export(self, value, format, context=None):
        context = context or get_export_context()
        return self._export(value, format, context)

    def _export(self, value, format, context):
        raise NotImplementedError

    def to_native(self, value, context=None):
        context = context or get_export_context(to_native_converter)
        return to_native_converter(self, value, context)

    def to_primitive(self, value, context=None):
        context = context or get_export_context(to_primitive_converter)
        return to_primitive_converter(self, value, context)

    def _init_field(self, field, options):
        """
        Instantiate the inner field that represents each element within this compound type.
        In case the inner field is itself a compound type, its inner field can be provided
        as the ``nested_field`` keyword argument.
        """
        if not isinstance(field, BaseType):
            nested_field = options.pop('nested_field', None) or options.pop('compound_field', None)
            if nested_field:
                field = field(field=nested_field, **options)
            else:
                field = field(**options)
        return field

MultiType = CompoundType


class ModelType(CompoundType):
    """A field that can hold an instance of the specified model."""

    primitive_type = dict

    @property
    def native_type(self):
        return self.model_class

    @property
    def fields(self):
        return self.model_class.fields

    @property
    def model_class(self):
        if self._model_class:
            return self._model_class

        model_class = import_string(self.model_name)
        self._model_class = model_class
        return model_class

    def __init__(self,
                 model_spec,  # type: typing.Type[T]
                 **kwargs):
        # type: (...) -> T

        if isinstance(model_spec, ModelMeta):
            self._model_class = model_spec
            self.model_name = self.model_class.__name__
        elif isinstance(model_spec, string_type):
            self._model_class = None
            self.model_name = model_spec
        else:
            raise TypeError("ModelType: Expected a model, got an argument "
                            "of the type '{}'.".format(model_spec.__class__.__name__))

        super(ModelType, self).__init__(**kwargs)

    def _repr_info(self):
        return self.model_class.__name__

    def _mock(self, context=None):
        return self.model_class.get_mock_object(context)

    def _setup(self, field_name, owner_model):
        # Resolve possible name-based model reference.
        if not self._model_class:
            if self.model_name == owner_model.__name__:
                self._model_class = owner_model
            else:
                pass  # Intentionally left blank, it will be setup later.
        super(ModelType, self)._setup(field_name, owner_model)

    def pre_setattr(self, value):
        if value is not None \
          and not isinstance(value, Model):
            if not isinstance(value, dict):
                raise ConversionError(_('Model conversion requires a model or dict'))
            value = self.model_class(value)
        return value

    def _convert(self, value, context):
        field_model_class = self.model_class
        if isinstance(value, field_model_class):
            model_class = type(value)
        elif isinstance(value, dict):
            model_class = field_model_class
        else:
            raise ConversionError(
                _("Input must be a mapping or '%s' instance") % field_model_class.__name__)

        ingest = context._get('ingest', False) if hasattr(context, '_get') else False
        tracker = context._get('ingest_tracker', None) if ingest else None
        start = len(tracker.results) if tracker is not None else 0

        if context.convert and context.oo:
            result = model_class(value, context=context)
        else:
            result = model_class.convert(value, context=context)

        if tracker is not None and isinstance(result, Model):
            nested = tracker.results[start:]
            if nested:
                result._ingest_results = dict(
                    (entry.field_name or str(index), entry)
                    for index, entry in enumerate(nested))
        return result

    def _export(self, value, format, context):
        if isinstance(value, Model):
            model_class = type(value)
        else:
            model_class = self.model_class
        return export_loop(model_class, value, context=context)


class ListType(CompoundType):
    """A field for storing a list of items, all of which must conform to the
    type specified by the ``field`` parameter.

    Use it like this::

        ...
        categories = ListType(StringType)

    An optional ingest mode can be enabled for lists whose input may be a
    one-shot iterator, may contain ``None`` placeholders, or whose rejected
    items must be reported at their original positions::

        ...
        details = ListType(ModelType(Detail), ingest=True)

    In ingest mode :meth:`ingest` returns a :class:`ListIngestResult`.  The
    ordinary conversion path, including ``strict`` validation, is unchanged
    when the option is not used.
    """

    primitive_type = list
    native_type = list

    def __init__(self,
                 field,  # type: T
                 min_size=None, max_size=None, **kwargs):
        # type: (...) -> typing.List[T]

        # Ingest is opt-in.  Accept a few equivalent spellings so a caller can
        # choose the wording that matches its own vocabulary.
        self._ingest_enabled = any((
            kwargs.pop('ingest', False),
            kwargs.pop('collect_errors', False),
            kwargs.pop('report', False),
            kwargs.pop('track', False),
            kwargs.pop('track_provenance', False),
            kwargs.pop('provenance', False),
            kwargs.pop('partial', False),
            kwargs.pop('allow_partial', False),
            kwargs.pop('on_error', None) in ('collect', 'ignore', 'skip'),
        ))
        placeholders = kwargs.pop('placeholders', None)
        if placeholders is None:
            placeholders = kwargs.pop('placeholder', Undefined)
            if placeholders is Undefined:
                placeholders = None
            else:
                placeholders = (placeholders,)
        skip_none = any((
            kwargs.pop('skip_none', False),
            kwargs.pop('skip_placeholder', False),
            kwargs.pop('skip_placeholders', False),
            kwargs.pop('ignore_none', False),
            kwargs.pop('allow_none', False),
        ))
        if placeholders is not None:
            if isinstance(placeholders, (str, bytes)):
                placeholders = (placeholders,)
            elif not isinstance(placeholders, (list, tuple, set)):
                placeholders = (placeholders,)
            self._placeholders = tuple(placeholders)
        elif skip_none or self._ingest_enabled:
            self._placeholders = (None,)
        else:
            self._placeholders = ()
        self._skip_placeholders = bool(self._placeholders)

        self.field = self._init_field(field, kwargs)
        self.min_size = min_size
        self.max_size = max_size

        validators = [self.check_length] + kwargs.pop("validators", [])

        super(ListType, self).__init__(validators=validators, **kwargs)

    @property
    def model_class(self):
        return self.field.model_class

    @property
    def ingest_enabled(self):
        return self._ingest_enabled

    @property
    def placeholders(self):
        return self._placeholders

    @property
    def skips_placeholders(self):
        return self._skip_placeholders

    def _field_name(self):
        return self.name or getattr(self.parent_field, 'name', None) or 'items'

    def _is_placeholder(self, value):
        if value is None:
            # ``None`` is an ignorable placeholder on the ingest entry point
            # regardless of field options; the configured placeholders add
            # further values.
            return True
        for placeholder in self._placeholders:
            if placeholder is None:
                continue
            # Identity first: 0 and False are valid values and must never be
            # treated as placeholders merely because they are falsy.
            if value is placeholder:
                return True
            if type(value) is type(placeholder) and value == placeholder:
                return True
        return False

    def _repr_info(self):
        return self.field.__class__.__name__

    def _mock(self, context=None):
        random_length = get_value_in(self.min_size, self.max_size)

        return [self.field._mock(context) for dummy in range(random_length)]

    def _coerce(self, value):
        if isinstance(value, list):
            return value
        elif isinstance(value, (string_type, Mapping)): # unacceptable iterables
            pass
        elif isinstance(value, Sequence):
            return value
        elif isinstance(value, Iterable):
            return value
        raise ConversionError(_('Could not interpret the value as a list'))

    def _convert(self, value, context):
        # The explicit ingest entry point always reports.  A field with
        # ``ingest=True`` also reports on the ordinary import path; fields
        # without the option keep the original strict behavior.
        if self._ingest_enabled or (context is not None and
                                    context._get('ingest', False)):
            context = self._ensure_ingest_context(context)
            values, result = self._ingest_values(value, context)
            if result.errors or result.length_error:
                raise IngestError(result)
            return values

        value = self._coerce(value)
        data = []
        errors = {}
        for index, item in enumerate(value):
            try:
                data.append(context.field_converter(self.field, item, context))
            except BaseError as exc:
                errors[index] = exc
        if errors:
            raise CompoundError(errors)
        return data

    def ingest(self, value, context=None):
        """Read ``value`` once and return an ingest report.

        ``value`` may be any iterable, including a generator that can only be
        consumed once.  ``None`` entries are placeholders and are skipped;
        their original positions are retained for every item after them.  A
        rejected item does not prevent other items from being delivered.  If
        ``min_size`` or ``max_size`` is configured, the check applies to the
        retained (non-placeholder, successfully converted) items.

        When this field holds parent models that themselves contain an
        ingest-enabled ``ListType``, the call returns a
        :class:`BatchIngestResult` with one isolated result per parent.  A
        parent with any rejected detail is not delivered at all.
        """
        context = self._ingest_context(context)

        if self._has_nested_ingest():
            return self.ingest_batch(value, context=context)

        values, result = self._ingest_values(value, context)
        return result

    def ingest_batch(self, values, context=None):
        """Ingest a batch of parent items, isolating failures per parent.

        Each parent is converted independently.  A parent whose own type is
        wrong, or whose nested required details fail, contributes no details
        at all.  Original parent positions are preserved in the result.
        """
        context = self._ingest_context(context)

        batch = BatchIngestResult(field_name=self._field_name())
        for index, item in enumerate(values):
            if self._is_placeholder(item):
                result = ListIngestResult(field_name=self._field_name(),
                                          parent_index=index)
                result.outcomes.append({'index': index, 'status': 'skipped'})
                batch.add(result)
                continue

            parent_context = context._branch(
                ingest=True,
                ingest_tracker=ProvenanceTracker(),
                oo=True)
            tracker = parent_context._get('ingest_tracker')
            start = len(tracker.results)
            try:
                converted = parent_context.field_converter(
                    self.field, item, parent_context)
            except (FieldError, CompoundError) as exc:
                rejections = self._expand_error(index, exc)
                result = ListIngestResult(field_name=self._field_name(),
                                          errors=rejections,
                                          parent_index=index)
                result.outcomes.append({
                    'index': index, 'status': 'rejected',
                    'error': exc, 'rejections': rejections})
                batch.add(result)
                continue

            nested = [entry for entry in tracker.results[start:]
                      if isinstance(entry, ListIngestResult)]
            nested_errors = [rejection
                             for entry in nested for rejection in entry.errors]
            nested_length_errors = [entry for entry in nested
                                    if entry.length_error is not None]

            if nested_errors or nested_length_errors:
                prefix = (SourceCoordinate(self._field_name(), index),)
                rejections = [rejection.with_prefix(prefix)
                              for rejection in nested_errors]
                for entry in nested_length_errors:
                    rejections.append(Rejection(
                        index=index, path=prefix,
                        error=entry.length_error, scope='parent'))
                result = ListIngestResult(field_name=self._field_name(),
                                          errors=rejections,
                                          parent_index=index)
                result.outcomes.append({
                    'index': index, 'status': 'rejected',
                    'rejections': rejections})
                batch.add(result)
                continue

            if nested:
                prefix = (SourceCoordinate(self._field_name(), index),)
                tracked = []
                for entry in nested:
                    for child in entry.tracked:
                        provenance = child.provenance.map_path(prefix)
                        _attach_provenance(child.value, provenance)
                        tracked.append(TrackedItem(child.value, provenance))
                result = ListIngestResult(
                    tracked=tracked,
                    skipped=[child_index
                             for entry in nested for child_index in entry.skipped],
                    tracker=tracker,
                    field_name=self._field_name(),
                    parent_index=index,
                    min_size=self.min_size,
                    max_size=self.max_size)
                result.outcomes.append({'index': index, 'status': 'accepted'})
            else:
                provenance = Provenance(
                    path=(SourceCoordinate(self._field_name(), index),),
                    kind=self._field_name())
                _attach_provenance(converted, provenance)
                result = ListIngestResult(
                    tracked=[TrackedItem(converted, provenance)],
                    field_name=self._field_name(),
                    parent_index=index)
                result.outcomes.append({'index': index, 'status': 'accepted'})

            batch.add(result)

        length_error = self._check_ingest_length(len(batch.deliverable))
        batch.length_error = length_error
        batch.tracked_count = len(batch.deliverable)
        return batch

    def _ingest_context(self, context=None):
        if context is None:
            context = get_import_context(oo=True)
        elif isinstance(context, Mapping):
            options = dict(context)
            options.setdefault('oo', True)
            if 'field_converter' not in options:
                options['field_converter'] = import_converter
            context = get_import_context(**options)
        else:
            context = Context._make(context)
            if context._get('field_converter') is None:
                context.field_converter = import_converter
            if context._get('oo') is None:
                context.oo = True

        context._setdefaults({
            'ingest': True,
            'ingest_tracker': ProvenanceTracker(),
        })
        context['ingest'] = True
        if 'oo' not in context:
            context['oo'] = True
        return context

    def _ensure_ingest_context(self, context):
        """Return a context carrying ingest state, without mutating the input."""
        if context is None:
            return self._ingest_context(None)
        if context._get('ingest_tracker') is not None:
            return context
        branched = Context(context)
        if 'ingest_tracker' not in branched:
            branched.ingest_tracker = ProvenanceTracker()
        if 'ingest' not in branched:
            branched.ingest = True
        return branched

    def _has_nested_ingest(self):
        field = self.field
        if isinstance(field, ListType):
            return True
        model_class = getattr(field, 'model_class', None)
        if model_class is None:
            return False
        for candidate in getattr(model_class, 'fields', {}).values():
            if isinstance(candidate, ListType):
                return True
        return False

    def _rejection_for(self, index, error):
        """Build a rejection for a parent-level failure at ``index``."""
        return Rejection(index=index, path=(
            SourceCoordinate(self._field_name(), index),), error=error)

    def _expand_error(self, index, error):
        """Expand a rejected parent into original child coordinates.

        A nested list reports its failures in ``IngestError.result``.  Those
        rejections already know the child's original position; this method
        only prepends the outer parent position.  Nothing is inferred from the
        child's value, so duplicate business numbers are safe.  An error of
        the parent itself (including its own fields) is reported as a
        parent-level rejection without a detail position.
        """
        prefix = (SourceCoordinate(self._field_name(), index),)
        rejections = self._collect_nested(prefix, error)
        if rejections:
            return rejections
        return [Rejection(index=index, path=prefix, error=error,
                          scope='parent')]

    def _collect_nested(self, prefix, error):
        nested = getattr(error, 'result', None)
        if nested is not None:
            rejections = [rejection.with_prefix(prefix)
                          for rejection in nested.errors]
            if not rejections and nested.length_error is not None:
                rejections = [Rejection(index=prefix[-1].index, path=prefix,
                                        error=nested.length_error)]
            if rejections:
                return rejections

        if isinstance(error, CompoundError):
            rejections = []
            for value in error.errors.values():
                rejections.extend(self._collect_nested(prefix, value))
            return rejections

        return []

    def _expand_item_error(self, index, error):
        """Expand a rejected detail item; its own position is the detail."""
        prefix = (SourceCoordinate(self._field_name(), index),)
        nested = getattr(error, 'result', None)
        if nested is not None:
            rejections = [rejection.with_prefix(prefix)
                          for rejection in nested.errors]
            if not rejections and nested.length_error is not None:
                rejections = [Rejection(index=index, path=prefix,
                                        error=nested.length_error)]
            if rejections:
                return rejections
        return [Rejection(index=index, path=prefix, error=error,
                          scope='item')]

    def _ingest_values(self, value, context):
        """Single-pass conversion used by :meth:`ingest` and ingest mode."""
        value = self._coerce(value)
        name = self._field_name()
        tracker = context._get('ingest_tracker') or ProvenanceTracker()
        tracked = []
        errors = []
        skipped = []
        outcomes = []
        consumed = 0

        for index, item in enumerate(value):
            consumed += 1
            if self._is_placeholder(item):
                skipped.append(index)
                outcomes.append({'index': index, 'status': 'skipped'})
                continue

            start = len(tracker.records)
            item_context = context._branch(ingest_tracker=tracker)
            try:
                converted = item_context.field_converter(
                    self.field, item, item_context)
            except (FieldError, CompoundError) as exc:
                del tracker.records[start:]
                rejections = self._expand_item_error(index, exc)
                for rejection in rejections:
                    rejection.value = item
                errors.extend(rejections)
                outcomes.append({
                    'index': index, 'status': 'rejected',
                    'error': exc, 'rejections': rejections})
                continue

            provenance = Provenance(
                path=(SourceCoordinate(name, index),), kind=name)
            entry = TrackedItem(converted, provenance)
            _attach_provenance(converted, provenance)
            tracked.append(entry)
            tracker.record(provenance, converted)
            outcomes.append({'index': index, 'status': 'accepted',
                             'provenance': provenance})

        result = ListIngestResult(
            tracked=tracked, errors=errors, skipped=skipped,
            outcomes=outcomes, tracker=tracker, field_name=name,
            min_size=self.min_size, max_size=self.max_size)

        length_error = self._check_ingest_length(len(tracked))
        if length_error is not None:
            result.length_error = length_error

        result.consumed = consumed
        result.original_count = consumed
        tracker.record_result(result)
        return result.items, result

    def _check_ingest_length(self, length):
        if self.min_size is not None and length < self.min_size:
            message = ({
                True: _('Please provide at least %d item.'),
                False: _('Please provide at least %d items.'),
            }[self.min_size == 1]) % self.min_size
            return ValidationError(message)

        if self.max_size is not None and length > self.max_size:
            message = ({
                True: _('Please provide no more than %d item.'),
                False: _('Please provide no more than %d items.'),
            }[self.max_size == 1]) % self.max_size
            return ValidationError(message)

        return None

    def check_length(self, value, context):
        list_length = len(value) if value else 0

        if self.min_size is not None and list_length < self.min_size:
            message = ({
                True: _('Please provide at least %d item.'),
                False: _('Please provide at least %d items.'),
            }[self.min_size == 1]) % self.min_size
            raise ValidationError(message)

        if self.max_size is not None and list_length > self.max_size:
            message = ({
                True: _('Please provide no more than %d item.'),
                False: _('Please provide no more than %d items.'),
            }[self.max_size == 1]) % self.max_size
            raise ValidationError(message)

    def _export(self, list_instance, format, context):
        """Loops over each item in the model and applies either the field
        transform or the multitype transform.  Essentially functions the same
        as `transforms.export_loop`.
        """
        data = []
        _export_level = self.field.get_export_level(context)
        if _export_level == DROP:
            return data
        for value in list_instance:
            shaped = self.field.export(value, format, context)
            if shaped is None:
                if _export_level <= NOT_NONE:
                    continue
            elif self.field.is_compound and len(shaped) == 0:
                if _export_level <= NONEMPTY:
                    continue
            data.append(shaped)
        return data


class DictType(CompoundType):
    """A field for storing a mapping of items, the values of which must conform to the type
    specified by the ``field`` parameter.

    Use it like this::

        ...
        categories = DictType(StringType)

    """

    primitive_type = dict
    native_type = dict

    def __init__(self, field, coerce_key=None, **kwargs):
        # type: (...) -> typing.Dict[str, T]

        self.field = self._init_field(field, kwargs)
        self.coerce_key = coerce_key or str
        super(DictType, self).__init__(**kwargs)

    @property
    def model_class(self):
        return self.field.model_class

    def _repr_info(self):
        return self.field.__class__.__name__

    def _convert(self, value, context, safe=False):
        if not isinstance(value, Mapping):
            raise ConversionError(_('Only mappings may be used in a DictType'))

        data = {}
        errors = {}
        for k, v in iteritems(value):
            try:
                data[self.coerce_key(k)] = context.field_converter(self.field, v, context)
            except BaseError as exc:
                errors[k] = exc
        if errors:
            raise CompoundError(errors)
        return data

    def _export(self, dict_instance, format, context):
        """Loops over each item in the model and applies either the field
        transform or the multitype transform.  Essentially functions the same
        as `transforms.export_loop`.
        """
        data = {}
        _export_level = self.field.get_export_level(context)
        if _export_level == DROP:
            return data
        for key, value in iteritems(dict_instance):
            shaped = self.field.export(value, format, context)
            if shaped is None:
                if _export_level <= NOT_NONE:
                    continue
            elif self.field.is_compound and len(shaped) == 0:
                if _export_level <= NONEMPTY:
                    continue
            data[key] = shaped
        return data


class PolyModelType(CompoundType):
    """A field that accepts an instance of any of the specified models."""

    primitive_type = dict
    native_type = None  # cannot be determined from a PolyModelType instance

    def __init__(self, model_spec, **kwargs):

        if isinstance(model_spec, (ModelMeta, string_type)):
            self.model_classes = (model_spec,)
            allow_subclasses = True
        elif isinstance(model_spec, Iterable):
            self.model_classes = tuple(model_spec)
            allow_subclasses = False
        else:
            raise Exception("The first argument to PolyModelType.__init__() "
                            "must be a model or an iterable.")

        self.claim_function = kwargs.pop("claim_function", None)
        self.allow_subclasses = kwargs.pop("allow_subclasses", allow_subclasses)

        CompoundType.__init__(self, **kwargs)

    def _setup(self, field_name, owner_model):
        # Resolve possible name-based model references.
        resolved_classes = []
        for m in self.model_classes:
            if isinstance(m, string_type):
                if m == owner_model.__name__:
                    resolved_classes.append(owner_model)
                else:
                    raise Exception("PolyModelType: Unable to resolve model '{}'.".format(m))
            else:
                resolved_classes.append(m)
        self.model_classes = tuple(resolved_classes)
        super(PolyModelType, self)._setup(field_name, owner_model)

    def is_allowed_model(self, model_instance):
        if self.allow_subclasses:
            if isinstance(model_instance, self.model_classes):
                return True
        else:
            if model_instance.__class__ in self.model_classes:
                return True
        return False

    def _convert(self, value, context):

        if value is None:
            return None

        if not context.validate:
            if self.is_allowed_model(value):
                return value
            if not isinstance(value, dict):
                if len(self.model_classes) > 1:
                    instanceof_msg = 'one of: {}'.format(', '.join(
                        cls.__name__ for cls in self.model_classes))
                else:
                    instanceof_msg = self.model_classes[0].__name__
                raise ConversionError(_('Please use a mapping for this field or '
                                        'an instance of {}').format(instanceof_msg))

        model_class = self.find_model(value)
        return model_class(value, context=context)

    def find_model(self, data):
        """Finds the intended type by consulting potential classes or `claim_function`."""

        if self.claim_function:
            kls = self.claim_function(self, data)
            if not kls:
                raise Exception("Input for polymorphic field did not match any model")
            return kls

        fallback = None
        matching_classes = []
        for kls in self._get_candidates():
            try:
                # If a model defines a _claim_polymorphic method, use
                # it to see if the model matches the data.
                kls_claim = kls._claim_polymorphic
            except AttributeError:
                # The first model that doesn't define the hook can be
                # used as a default if there's no match.
                if not fallback:
                    fallback = kls
            else:
                if kls_claim(data):
                    matching_classes.append(kls)

        if not matching_classes and fallback:
            return fallback

        elif len(matching_classes) != 1:
            raise Exception("Got ambiguous input for polymorphic field")

        return matching_classes[0]

    def _export(self, model_instance, format, context):

        model_class = model_instance.__class__
        if not self.is_allowed_model(model_instance):
            raise Exception("Cannot export: {} is not an allowed type".format(model_class))

        return model_instance.export(context=context)

    def _get_candidates(self):
        candidates = self.model_classes

        if self.allow_subclasses:
            candidates = itertools.chain.from_iterable(
                ([m] + get_all_subclasses(m) for m in candidates)
            )

        return candidates


if PY2:
    # Python 2 names cannot be unicode
    __all__ = [n.encode('ascii') for n in __all__]

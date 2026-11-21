# -*- coding: utf-8 -*-

from __future__ import unicode_literals, absolute_import

from collections import OrderedDict

from .compat import iteritems
from .exceptions import BaseError
from .iteration import atoms
from .types.compound import CompoundType, DictType, ListType, ModelType, PolyModelType
from .undefined import Undefined

__all__ = [
    'MigrationError', 'MigrationStageError', 'MigrationConversionError',
    'MigrationValidationError', 'migrate', 'try_migrate',
    'CONVERT', 'DATE', 'VALIDATE',
]

#: Stage names reported on migration failures.
CONVERT = 'convert'
DATE = 'date'
VALIDATE = 'validate'


class MigrationError(BaseError):
    """Base class for errors raised by a trial migration.

    ``stage`` identifies the migration step that failed and ``field_path``
    contains the path to the offending field in the source model.  The path is
    a tuple of field names and list indexes.  ``errors`` contains the
    structured field error payload when one is available.
    """

    stage = None

    def __init__(self, message, stage=None, field_path=None, errors=None,
                 partial_data=None):
        if errors is None:
            errors = []
        super(MigrationError, self).__init__(errors)
        self.message = message
        self.stage = stage
        self.field_path = tuple(field_path or ())
        self.partial_data = partial_data

    @property
    def path(self):
        return self.field_path

    @property
    def errors(self):
        return self.args[0]

    @property
    def error(self):
        """Alias used by callers that prefer a singular name."""
        return self.errors

    def __str__(self):
        path = '.'.join(str(item) for item in self.field_path)
        details = self.message
        if path:
            details = '%s: %s' % (details, path)
        if self.stage:
            details = '[%s] %s' % (self.stage, details)
        return details

    def to_dict(self):
        return {
            'stage': self.stage,
            'field_path': self.field_path,
            'errors': self.to_primitive(),
        }


class MigrationStageError(MigrationError):
    """A conversion step failed before the target could be validated.

    ``stage`` names the step (for example ``'address'``, ``'date'`` or the
    generic ``'convert'``).
    """


class MigrationValidationError(MigrationError):
    """The converted target failed new-model validation."""


MigrationConversionError = MigrationStageError


class _Node(object):
    """A converted value plus the target path that produced it."""

    __slots__ = ('value', 'path')

    def __init__(self, value, path=None):
        self.value = value
        self.path = path or ()


class _CycleDetected(Exception):
    pass


def _join_path(path, key):
    return tuple(path) + (key,)


def _is_model(value):
    # Avoid importing models at module import time (models imports this
    # module to expose the public entry point).
    from .models import Model
    return isinstance(value, Model)


class _Migration(object):
    """Converts and validates one source model into one target model."""

    def __init__(self, source, target_class, partial=False, strict=False,
                 app_data=None, context=None, normalize=None, stage_for=None,
                 **kwargs):
        self.source = source
        self.target_class = target_class
        self.partial = partial
        self.strict = strict
        self.app_data = app_data
        self.context = context
        self.normalize = normalize
        self.stage_for = stage_for
        self.kwargs = kwargs
        # Source object id -> converted object.  Identity, not equality, is
        # what keeps aliases aliases and equal-but-distinct values distinct.
        self._objects = {}
        # Target object id -> the path where it was normalized.  Shared target
        # nodes must not be normalized twice.
        self._normalized = set()
        # Source object ids currently being converted, to detect cycles.
        self._converting = set()

    def run(self):
        try:
            data = self._convert_model(self.source, self.target_class, ())
        except _CycleDetected as exc:
            raise MigrationStageError(
                'cyclic model graph cannot be converted',
                stage='convert',
                field_path=(),
                errors='cyclic reference',
            )
        target = self._build_target(self.target_class, data, ())

        if self.normalize is not None:
            target = self._normalize_target(target, ())

        error = self._validate_model(target, self.partial)
        if error is not None:
            raise error

        return target

    def _normalize_target(self, model, path):
        """Apply ``normalize`` to the target graph, preserving aliases."""
        normalized = {}
        for field_name, field, value in atoms(model._schema,
                                             dict((name, model._data[name])
                                                  for name in model
                                                  if name in model._schema.fields)):
            if value is Undefined:
                continue
            field_path = _join_path(path, field_name)
            try:
                normalized[field_name] = self._normalize_value(
                    field, value, field_path)
            except MigrationError:
                raise
            except (BaseError, ValueError, TypeError) as exc:
                raise self._conversion_error(field_name, field, value, exc,
                                             field_path)

        for field_name, value in iteritems(normalized):
            setattr(model, field_name, value)
        return model

    def _normalize_value(self, field, value, path):
        if value is None or value is Undefined:
            return value

        if isinstance(field, ModelType):
            if _is_model(value):
                return self._normalize_model(value, path)
            return value

        if isinstance(field, PolyModelType):
            if _is_model(value):
                return self._normalize_model(value, path)
            return value

        if isinstance(field, ListType):
            result = []
            for index, item in enumerate(value):
                result.append(self._normalize_value(
                    field.field, item, _join_path(path, index)))
            return result

        if isinstance(field, DictType):
            return dict((key, self._normalize_value(
                field.field, item, _join_path(path, key)))
                for key, item in iteritems(value))

        normalized = self.normalize(value, path)
        return value if normalized is None else normalized

    def _normalize_model(self, model, path):
        object_id = id(model)
        if object_id in self._normalized:
            return model
        self._normalized.add(object_id)

        normalized = self.normalize(model, path)
        if normalized is None:
            normalized = model
        self._normalize_target(normalized, path)
        return normalized

    # Conversion -----------------------------------------------------------

    def _convert_model(self, source, model_class, path):
        source_values = self._visible_data(source)
        converted = OrderedDict()

        for field_name, field, value in atoms(model_class._schema, source_values):
            if value is Undefined:
                continue
            try:
                node = self._convert_field(field, value, _join_path(path, field_name))
            except MigrationError:
                raise
            except (BaseError, ValueError, TypeError) as exc:
                raise self._conversion_error(field_name, field, value, exc,
                                             _join_path(path, field_name))
            converted[field_name] = node

        return converted

    def _convert_field(self, field, value, path):
        if value is None or value is Undefined:
            return _Node(value, path)

        if isinstance(field, ModelType):
            model_class = field.model_class
            if _is_model(value) or isinstance(value, dict):
                return self._convert_model_value(value, model_class, path)
            raise self._conversion_error(None, field, value,
                                         'expected a model or mapping', path)

        if isinstance(field, PolyModelType):
            return self._convert_poly_model(field, value, path)

        if isinstance(field, ListType):
            return self._convert_sequence(field, value, path)

        if isinstance(field, DictType):
            return self._convert_mapping(field, value, path)

        if isinstance(field, CompoundType):
            # Unknown compound type: let Schematics do its normal conversion,
            # then recursively make the result independent.
            converted = field.convert(value, self._context())
            return _Node(self._detach(converted), path)

        converted = field.convert(value, self._context())
        return _Node(converted, path)

    def _convert_model_value(self, value, model_class, path):
        if _is_model(value):
            object_id = id(value)
            if object_id in self._objects:
                return self._objects[object_id]
            if object_id in self._converting:
                raise _CycleDetected(object_id)

            self._converting.add(object_id)
            try:
                converted = self._convert_model(value, model_class, path)
            finally:
                self._converting.discard(object_id)
            target = self._build_target(model_class, converted, path)
            node = _Node(target, path)
            self._objects[object_id] = node
            return node

        # A plain mapping cannot carry source identity, so each occurrence is
        # an independent value.
        converted = self._convert_model(value, model_class, path)
        target = self._build_target(model_class, converted, path)
        return _Node(target, path)

    def _convert_poly_model(self, field, value, path):
        if _is_model(value):
            model_class = type(value)
        elif isinstance(value, dict):
            model_class = field.find_model(value)
        else:
            raise self._conversion_error(None, field, value,
                                         'invalid polymorphic input', path)
        if not field.is_allowed_model(value) and not isinstance(value, dict):
            raise self._conversion_error(None, field, value,
                                         'model is not an allowed type', path)
        return self._convert_model_value(value, model_class, path)

    def _convert_sequence(self, field, value, path):
        if not isinstance(value, (list, tuple)):
            raise self._conversion_error(None, field, value,
                                         'expected a list', path)
        converted = []
        for index, item in enumerate(value):
            item_path = _join_path(path, index)
            try:
                converted.append(self._convert_field(field.field, item, item_path))
            except MigrationError:
                raise
            except (BaseError, ValueError, TypeError) as exc:
                raise self._conversion_error(None, field.field, item, exc,
                                             item_path)

        if field.min_size is not None and len(converted) < field.min_size:
            raise self._conversion_error(None, field, value,
                                         'list is shorter than min_size', path)
        if field.max_size is not None and len(converted) > field.max_size:
            raise self._conversion_error(None, field, value,
                                         'list is longer than max_size', path)

        return _Node([node.value for node in converted], path)

    def _convert_mapping(self, field, value, path):
        if not hasattr(value, 'items'):
            raise self._conversion_error(None, field, value,
                                         'expected a mapping', path)
        converted = OrderedDict()
        for key, item in iteritems(value):
            item_path = _join_path(path, key)
            try:
                node = self._convert_field(field.field, item, item_path)
            except MigrationError:
                raise
            except (BaseError, ValueError, TypeError) as exc:
                raise self._conversion_error(None, field.field, item, exc,
                                             item_path)
            converted[field.coerce_key(key)] = node.value
        return _Node(converted, path)

    def _visible_data(self, model):
        """Return the data the source currently exposes to readers.

        ``ModelDict`` is a ChainMap whose first maps win.  Reading through the
        model itself would be enough for field access, but using an isolated
        plain dict here guarantees no import/validation writes ever reach the
        source and includes the pending (converted/unsafe) overlay.
        """
        data = {}
        # ModelDict.maps is [unsafe, converted, valid]; earlier maps win.
        maps = getattr(model._data, 'maps', None)
        if maps:
            for mapping in maps:
                for field_name in model._schema.fields:
                    if field_name in mapping and field_name not in data:
                        data[field_name] = mapping[field_name]
        else:
            for field_name in model:
                data[field_name] = model[field_name]
        return data

    def _conversion_error(self, field_name, field, value, error, path):
        if isinstance(error, MigrationError):
            return error
        errors = error.errors if isinstance(error, BaseError) else error
        if field_name is None and field is not None:
            field_name = getattr(field, 'name', None)
        return MigrationStageError(
            'conversion failed',
            stage=self._stage(field, path),
            field_path=path,
            errors=errors,
        )

    @staticmethod
    def _stage_for_field(field):
        # Date/time parsing is a distinct step in many migrations.  Keep the
        # generic name for other fields, where the stage is less meaningful.
        from .types.base import DateTimeType
        if isinstance(field, DateTimeType):
            return 'date'
        return 'convert'

    def _stage(self, field, path):
        if self.stage_for is not None:
            return self.stage_for(field, path)
        return self._stage_for_field(field)

    def _context(self):
        if self.context is not None:
            return self.context
        from .transforms import get_import_context
        return get_import_context(app_data=self.app_data, **self.kwargs)

    # Target construction --------------------------------------------------

    def _build_target(self, model_class, converted, path=()):
        target = model_class(init=False)
        target._data.valid = self._values(converted)
        # Path of this target inside the migration result.  Kept off the
        # ModelDict so ordinary model behaviour is unaffected.
        target._migration_path = path
        return target

    def _values(self, converted):
        return OrderedDict((name, node.value)
                           for name, node in iteritems(converted))

    # Validation -----------------------------------------------------------

    def _validate_model(self, model, partial, path=None):
        schema = model._schema
        if path is None:
            path = getattr(model, '_migration_path', ())
        data = OrderedDict((name, model._data[name])
                           for name in model if name in schema.fields)
        errors = OrderedDict()
        invalid_fields = []
        first_path = []

        context = self._validation_context(partial)

        for field_name, field, value in atoms(schema, data):
            if value is Undefined:
                continue
            field_path = _join_path(path, field_name)
            try:
                self._validate_field(field, value, partial, field_path, context)
            except MigrationError as exc:
                errors[field_name] = exc.errors
                invalid_fields.append(field_name)
                if not first_path:
                    first_path.append(exc.field_path or field_path)
            except BaseError as exc:
                errors[field_name] = exc.errors
                invalid_fields.append(field_name)
                if not first_path:
                    first_path.append(field_path)

        for field_name in invalid_fields:
            data.pop(field_name, None)

        for field_name, field, value in atoms(schema, data,
                                             filter=lambda atom: (
                                                 atom.value is not Undefined and
                                                 atom.name in schema._validator_functions)):
            try:
                self._call_model_validator(
                    schema._validator_functions[field_name],
                    model, data, value, context)
            except (BaseError, ValueError, TypeError) as exc:
                errors[field_name] = getattr(exc, 'errors', exc)
                data.pop(field_name, None)
                if not first_path:
                    first_path.append(_join_path(path, field_name))

        if errors:
            return MigrationValidationError(
                'new model validation failed',
                stage='validate',
                field_path=first_path[0] if first_path else path,
                errors=errors,
                partial_data=data,
            )
        return None

    def _validation_context(self, partial):
        from .validate import get_validation_context
        context = get_validation_context(partial=partial, convert=False)
        context.app_data = self.app_data if self.app_data is not None else {}
        return context

    def _validate_field(self, field, value, partial, path, context):
        if value is None or value is Undefined:
            if field.required and not partial:
                raise MigrationValidationError(
                    'field is required', stage='validate', field_path=path,
                    errors='This field is required.')
            return value

        if isinstance(field, ModelType):
            if _is_model(value):
                error = self._validate_model(value, partial, path)
                if error is not None:
                    raise error
            return value

        if isinstance(field, PolyModelType):
            if _is_model(value):
                error = self._validate_model(value, partial, path)
                if error is not None:
                    raise error
            return value

        if isinstance(field, ListType):
            for index, item in enumerate(value):
                self._validate_field(field.field, item, partial,
                                     _join_path(path, index), context)
            try:
                field.check_length(value, context)
            except BaseError as exc:
                raise MigrationValidationError(
                    'field validation failed', stage='validate',
                    field_path=path, errors=exc.errors)
            return value

        if isinstance(field, DictType):
            for key, item in iteritems(value):
                self._validate_field(field.field, item, partial,
                                     _join_path(path, key), context)
            return value

        if isinstance(field, CompoundType):
            return value

        # Primitive field: run the normal validator chain against the already
        # converted value.  Conversion happened during migration, so the
        # validation context disables a second conversion.
        try:
            return field.validate(value, context)
        except MigrationError:
            raise
        except BaseError as exc:
            raise MigrationValidationError(
                'field validation failed', stage='validate',
                field_path=path, errors=exc.errors)

    @staticmethod
    def _call_model_validator(validator, model, data, value, context):
        """Call a model validator using Schematics' normal conventions.

        The core pipeline invokes model validators as
        ``func(model, data, value, context)``; ``prepare_validator`` adapts
        validators that omit the context parameter.
        """
        return validator(model, data, value, context)

    def _detach(self, value):
        """Recursively detach a value without using deepcopy.

        ``ModelDict.valid`` is a MappingProxyType, which ``deepcopy`` cannot
        pickle.  Models are the only problematic values in practice, so they
        are copied explicitly; other containers are handled structurally.
        """
        if _is_model(value):
            model_class = type(value)
            values = {}
            for name in value:
                values[name] = self._detach(value[name])
            target = model_class(init=False)
            target._data.valid = values
            return target
        if isinstance(value, list):
            return [self._detach(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._detach(item) for item in value)
        if isinstance(value, dict):
            return dict((key, self._detach(item))
                        for key, item in iteritems(value))
        return value


def migrate(source, target_class, partial=False, strict=False, app_data=None,
            context=None, normalize=None, stage_for=None, **kwargs):
    """Trial-convert ``source`` into a new ``target_class`` instance.

    The source model is never imported into or validated, so its pending
    (unvalidated) overlay stays pending and its nested values keep their
    original identities.  Nothing is returned until every conversion step and
    the target model's normal validation have succeeded.

    Source object identity is preserved in the target graph: two source fields
    that point at the same model produce two target fields that point at the
    same new model.  Values that are merely equal, but distinct in the source,
    remain distinct in the target.

    :param normalize:
        Optional callable ``normalize(value, path)``.  It is applied to each
        converted target value after conversion and before validation.  It may
        mutate and return the value, return a replacement, or return ``None``
        to leave the value unchanged.  Because the target graph preserves
        aliases, a shared value is normalized once and every alias sees the
        result.
    :param stage_for:
        Optional callable ``stage_for(field, path)`` returning the stage name
        reported for a conversion failure.  By default date fields report
        ``'date'`` and other fields report ``'convert'``.
    :raises MigrationStageError: if a conversion or normalization step fails.
    :raises MigrationValidationError: if new-model validation fails.
    """
    return _Migration(
        source, target_class,
        partial=partial, strict=strict, app_data=app_data,
        context=context, normalize=normalize, stage_for=stage_for, **kwargs
    ).run()


def try_migrate(source, target_class, **kwargs):
    """Like :func:`migrate`, but return ``(target, error)`` instead of raising.

    Exactly one of ``target`` and ``error`` is ``None``.  On error, no target
    has been published, the source is unchanged, and ``error`` is a
    :class:`MigrationError` carrying the failed stage and field path.
    """
    try:
        return migrate(source, target_class, **kwargs), None
    except MigrationError as exc:
        return None, exc


migrate_to = migrate
trial_migrate = migrate
trial_convert = migrate
convert_trial = migrate

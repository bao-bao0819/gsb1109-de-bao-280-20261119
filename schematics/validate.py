# -*- coding: utf-8 -*-

from __future__ import unicode_literals, absolute_import

import inspect
import functools

from .common import *
from .datastructures import Context
from .exceptions import FieldError, DataError
from .transforms import import_loop, validation_converter
from .undefined import Undefined
from .iteration import atoms

__all__ = ['reads']


def reads(*field_names):
    """
    Declare which fields a model level validation rule reads.

    Rules decorated with ``reads`` may be skipped by
    :meth:`schematics.models.Model.update` when none of the declared
    fields are affected by the update. Rules without a declaration keep
    the existing behavior and are always called.
    """
    def wrap(func):
        func._reads = frozenset(field_names)
        return func
    return wrap


def validate(schema, mutable, raw_data=None, trusted_data=None,
             partial=False, strict=False, convert=True, context=None,
             validate_model=True, changed_fields=None, **kwargs):
    """
    Validate some untrusted data using a model. Trusted data can be passed in
    the `trusted_data` parameter.

    :param schema:
        The Schema to use as source for validation.
    :param mutable:
        A mapping or instance that can be changed during validation by Schema
        functions.
    :param raw_data:
        A mapping or instance containing new data to be validated.
    :param partial:
        Allow partial data to validate; useful for PATCH requests.
        Essentially drops the ``required=True`` arguments from field
        definitions. Default: False
    :param strict:
        Complain about unrecognized keys. Default: False
    :param trusted_data:
        A ``dict``-like structure that may contain already validated data.
    :param convert:
        Controls whether to perform import conversion before validating.
        Can be turned off to skip an unnecessary conversion step if all values
        are known to have the right datatypes (e.g., when validating immediately
        after the initial import). Default: True
    :param validate_model:
        Controls whether model level validation rules are executed.
        Default: True
    :param changed_fields:
        An optional set of field names that were affected by the validated
        input. Model level rules that declare their field dependencies with
        ``reads`` are only executed when one of the declared fields is in
        this set. Rules without a declaration are always executed.
        Default: None (execute all rules)

    :returns: data
        ``dict`` containing the valid raw_data plus ``trusted_data``.
        If errors are found, they are raised as a ValidationError with a list
        of errors attached.
    """
    if raw_data is None:
        raw_data = mutable

    context = context or get_validation_context(partial=partial, strict=strict,
        convert=convert)

    errors = {}
    try:
        data = import_loop(schema, mutable, raw_data, trusted_data=trusted_data,
            context=context, **kwargs)
    except DataError as exc:
        errors = dict(exc.errors)
        data = exc.partial_data

    if validate_model:
        errors.update(_validate_model(schema, mutable, data, context,
            changed_fields=changed_fields))

    if errors:
        raise DataError(errors, data)

    return data


def _validate_model(schema, mutable, data, context, changed_fields=None):
    """
    Validate data using model level methods.

    :param schema:
        The Schema to validate ``data`` against.
    :param mutable:
        A mapping or instance that will be passed to the validator containing
        the original data and that can be mutated.
    :param data:
        A dict with data to validate. Invalid items are removed from it.
    :param changed_fields:
        An optional set of field names affected by the current input. Rules
        that declare their field dependencies with ``reads`` are skipped
        unless one of the declared fields is in this set. Rules without a
        declaration are always executed.
    :returns:
        Errors of the fields that did not pass validation.
    """
    errors = {}
    invalid_fields = []

    has_validator = lambda atom: (
        atom.value is not Undefined and
        atom.name in schema._validator_functions
    )
    for field_name, field, value in atoms(schema, data, filter=has_validator):
        validator = schema._validator_functions[field_name]
        if changed_fields is not None:
            dependencies = getattr(validator, '_reads', None)
            if dependencies is not None \
              and not dependencies.intersection(changed_fields):
                continue
        try:
            validator(mutable, data, value, context)
        except (FieldError, DataError) as exc:
            serialized_field_name = field.serialized_name or field_name
            errors[serialized_field_name] = exc.errors
            invalid_fields.append(field_name)

    for field_name in invalid_fields:
        data.pop(field_name)

    return errors


def get_validation_context(**options):
    validation_options = {
        'field_converter': validation_converter,
        'partial': False,
        'strict': False,
        'convert': True,
        'validate': True,
        'new': False,
    }
    validation_options.update(options)
    return Context(**validation_options)


def prepare_validator(func, argcount):
    if isinstance(func, classmethod):
        func = func.__get__(object).__func__
    try:
        func_args = inspect.getfullargspec(func).args  # PY3
    except AttributeError:
        func_args = inspect.getargspec(func).args  # PY2
    if len(func_args) < argcount:
        @functools.wraps(func)
        def newfunc(*args, **kwargs):
            if not kwargs or kwargs.pop('context', 0) == 0:
                args = args[:-1]
            return func(*args, **kwargs)
        return newfunc
    return func

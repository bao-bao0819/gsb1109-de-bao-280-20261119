# -*- coding: utf-8 -*-
"""
Generation of executable change lists from untrusted import data.

Unlike a full import, :func:`import_changes` never mutates the current model
and never applies defaults.  It converts the submitted data, collapses the
allowed input aliases of every field into one canonical leaf, and reports only
the leaves that actually differ from the current order.

Two roles may be used against the very same input.  A role hides fields from
the generated list, but a conflict or conversion error on a hidden field still
rejects the whole submission; the accompanying error never carries hidden
values, only the original input paths.
"""

from __future__ import unicode_literals, absolute_import

from collections import OrderedDict

from .exceptions import CompoundError, DataError, FieldError
from .transforms import get_import_context, import_converter
from .types.compound import ModelType
from .undefined import Undefined

try:
    from collections.abc import Mapping
except ImportError:
    from collections import Mapping


__all__ = ['import_changes', 'ChangeConflictError']


_HIDDEN_FIELD_ERROR = 'Invalid value for a field not available in this role'


class ChangeConflictError(DataError):
    """Raised when one canonical leaf receives unequal converted values from
    two or more original input paths.

    The error structure mirrors the change list, using recursive serialized
    keys.  Every conflict is a mapping of the form::

        {'conflict': {'sources': ['address.city', 'address_alt.city'],
                      'values': ['Town A', 'Town B']}}

    ``values`` is omitted when the conflicting leaf is hidden from the role
    that produced the error, so hidden data never leaks through the message.
    """


def import_changes(schema, instance, raw_data, role=None, mapping=None,
                   partial=True, strict=False, raise_error_on_role=True,
                   app_data=None, context=None):
    """Build an executable change list for ``instance`` from ``raw_data``.

    :param schema:
        The schema describing the accepted input.
    :param instance:
        The current model instance or mapping.  It is never modified.
    :param raw_data:
        The untrusted payload delivered by the supplier.
    :param role:
        Optional export role.  Hidden fields are left out of the change list,
        but conflicts and conversion errors on them still reject the input.
    :param mapping:
        Optional deserialize mapping, as accepted by the regular import loop.
    :param partial:
        Allow partial data.  Defaults to ``True`` because a change list is a
        patch rather than a full replacement.
    :param strict:
        When ``True``, unrecognized input keys produce a :class:`DataError`.
    :returns:
        An ordered mapping of recursive serialized keys to converted values.
        An explicit ``None`` means the leaf must be deleted.
    """
    mapping = mapping or {}
    role_filter = _role_filter(schema, role, raise_error_on_role)
    current = _collect_current(schema, instance, role, role_filter)

    if context is None:
        context = get_import_context(
            field_converter=import_converter, partial=partial, strict=strict,
            mapping=mapping, app_data=app_data, oo=True, role=role)

    changes, conflicts, errors = _walk(
        schema, raw_data or {}, current, role_filter, mapping, '',
        context, strict, False)

    if errors:
        raise DataError(errors, {})
    if conflicts:
        raise ChangeConflictError(conflicts)
    return _emit(changes)


def _walk(schema, raw_data, current, role_filter, mapping, prefix, context,
          strict, force_hidden):
    """Convert one mapping level.

    Returns three ordered dicts, keyed by serialized field name:

    * ``changes``: ``(converted_value, [input paths], hidden)`` entries
    * ``conflicts``: conflict entries (possibly nested)
    * ``errors``: conversion errors (possibly nested)

    Hidden leaves stay in ``changes`` until the result is emitted, so that
    conflicts between two input sources can still be detected.
    """
    changes = OrderedDict()
    conflicts = OrderedDict()
    errors = {}

    grouped = _group_input(schema, raw_data, mapping, prefix, strict, errors)

    for field_name, sources in grouped.items():
        field = schema.fields[field_name]
        serialized_name = field.serialized_name or field_name
        hidden = force_hidden or (role_filter is not None and role_filter(
            field_name, current.get(field_name, Undefined)))

        if _is_model_field(field):
            _walk_model_field(
                field, sources, current.get(field_name), serialized_name,
                mapping, field_name, context, strict, hidden,
                changes, conflicts, errors)
        else:
            _walk_scalar_field(
                field, sources, current.get(field_name, Undefined),
                serialized_name, hidden, context, changes, conflicts, errors)

    return changes, conflicts, errors


def _walk_scalar_field(field, sources, old_value, serialized_name, hidden,
                       context, changes, conflicts, errors):
    candidates = []
    for raw_path, raw_value in sources:
        try:
            value = context.field_converter(field, raw_value, context)
        except (FieldError, CompoundError) as exc:
            errors[serialized_name] = (
                _HIDDEN_FIELD_ERROR if hidden else exc)
            return
        candidates.append((raw_path, value))

    distinct = []
    for raw_path, value in candidates:
        if not any(_values_equal(value, kept_value)
                   for kept_value, _ in distinct):
            distinct.append((value, raw_path))

    if len(distinct) > 1:
        conflicts[serialized_name] = _conflict_entry(
            [path for path, _ in candidates],
            [value for value, _ in distinct], hidden)
        return

    value = distinct[0][0]
    if value is None and old_value is Undefined:
        return
    if old_value is not Undefined and _values_equal(old_value, value):
        return
    changes[serialized_name] = (
        value, [path for path, _ in sources], hidden)


def _walk_model_field(field, sources, sub_current, serialized_name, mapping,
                      field_name, context, strict, hidden,
                      changes, conflicts, errors):
    sub_schema = field.model_class._schema
    sub_mapping = mapping.get('model_mapping', {}).get(field_name, {})
    sub_filter = _role_filter(
        sub_schema, getattr(context, 'role', None), False)
    if sub_current is None:
        sub_current = {}

    deletion_paths = []
    merged = OrderedDict()
    nested_conflicts = OrderedDict()
    saw_mapping = False

    for raw_path, raw_value in sources:
        if raw_value is None:
            deletion_paths.append(raw_path)
            continue
        if hasattr(raw_value, '_schema'):
            raw_value = dict(raw_value)
        if not isinstance(raw_value, Mapping):
            try:
                context.field_converter(field, raw_value, context)
            except (FieldError, CompoundError) as exc:
                errors[serialized_name] = (
                    _HIDDEN_FIELD_ERROR if hidden else exc)
            continue

        saw_mapping = True
        sub_changes, sub_conflicts, sub_errors = _walk(
            sub_schema, raw_value, sub_current, sub_filter, sub_mapping,
            raw_path + '.', context, strict, hidden)

        for key, sub_error in sub_errors.items():
            bucket = errors.setdefault(serialized_name, {})
            if isinstance(bucket, dict) and key not in bucket:
                bucket[key] = sub_error
        _merge_conflicts(nested_conflicts, sub_conflicts)
        _merge_leaf_changes(merged, sub_changes, nested_conflicts)

    if serialized_name in errors:
        return

    if deletion_paths and saw_mapping:
        all_paths = list(deletion_paths)
        all_paths.extend(_all_paths(merged, nested_conflicts))
        conflicts[serialized_name] = _conflict_entry(all_paths, [], hidden)
        return

    if deletion_paths:
        if hidden or not sub_current:
            return
        changes[serialized_name] = (None, deletion_paths, hidden)
        return

    if nested_conflicts:
        conflicts[serialized_name] = nested_conflicts
    elif merged:
        changes[serialized_name] = (
            _emit(merged), [path for path, _ in sources], hidden)


def _group_input(schema, raw_data, mapping, prefix, strict, errors):
    grouped = OrderedDict()
    for key, raw_value in raw_data.items():
        field_name = _resolve_field(schema, key, mapping)
        if field_name is None:
            if strict:
                errors[prefix + key] = 'Rogue field'
            continue
        grouped.setdefault(field_name, []).append((prefix + key, raw_value))
    return grouped


def _resolve_field(schema, key, mapping):
    for field_name, field in schema.fields.items():
        if key in field.get_input_keys(mapping):
            return field_name
    return None


def _merge_leaf_changes(merged, incoming, conflicts):
    for key, (value, raw_paths, hidden) in incoming.items():
        if key in conflicts:
            continue
        if key not in merged:
            merged[key] = (value, raw_paths, hidden)
            continue
        existing_value, existing_paths, existing_hidden = merged[key]
        if _values_equal(existing_value, value):
            continue
        paths = list(existing_paths) + list(raw_paths)
        conflicts[key] = _conflict_entry(
            paths, [existing_value, value],
            hidden or existing_hidden)
        del merged[key]


def _merge_conflicts(target, incoming):
    for key, entry in incoming.items():
        if key not in target:
            target[key] = entry
            continue
        existing = target[key]['conflict']
        new = entry['conflict']
        existing['sources'] = list(existing['sources']) + list(new['sources'])
        if 'values' in existing and 'values' in new:
            for value in new['values']:
                if not any(_values_equal(value, kept)
                           for kept in existing['values']):
                    existing['values'].append(value)
        else:
            existing.pop('values', None)


def _conflict_entry(sources, values, hidden):
    conflict = OrderedDict()
    conflict['sources'] = list(sources)
    if not hidden and values:
        conflict['values'] = list(values)
    return OrderedDict([('conflict', conflict)])


def _all_paths(merged, conflicts):
    paths = []
    for _value, raw_paths, _hidden in merged.values():
        paths.extend(raw_paths)
    for entry in conflicts.values():
        paths.extend(entry['conflict']['sources'])
    return paths


def _emit(changes):
    """Strip hidden leaves and path bookkeeping from a change mapping."""
    result = OrderedDict()
    for key, entry in changes.items():
        value, _paths, hidden = entry
        if hidden:
            continue
        if isinstance(value, dict):
            emitted = _emit(value) if _has_path_entries(value) else value
            if emitted:
                result[key] = emitted
        else:
            result[key] = value
    return result


def _has_path_entries(value):
    return any(isinstance(item, tuple) for item in value.values())


def _collect_current(schema, instance, role, role_filter):
    values = {}
    if instance is None:
        return values
    for field_name, field in schema.fields.items():
        try:
            value = instance[field_name]
        except (KeyError, AttributeError, TypeError):
            continue
        if role_filter is not None and role_filter(field_name, value):
            continue
        if _is_model_field(field) and _is_model_like(value):
            sub_filter = _role_filter(
                field.model_class._schema, role, False)
            value = _collect_current(
                field.model_class._schema, value, role, sub_filter)
        values[field_name] = value
    return values


def _role_filter(schema, role, raise_error_on_role):
    if role is None:
        return None
    filter_func = schema.options.roles.get(role)
    if filter_func is None and raise_error_on_role:
        raise ValueError('%s Model has no role "%s"' % (schema.name, role))
    return filter_func


def _is_model_field(field):
    return isinstance(field, ModelType)


def _is_model_like(value):
    return isinstance(value, Mapping) or hasattr(value, '_schema')


def _values_equal(left, right):
    if left is None or right is None:
        return left is right
    try:
        return bool(left == right)
    except Exception:
        return False

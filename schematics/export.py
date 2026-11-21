# -*- coding: utf-8 -*-
"""
Export paths and cycle-safe reference handling.

An :class:`ExportPath` binds a model field (typically a ``ModelType`` field)
to a set of role names, so the same model instance can be exported with
different roles depending on which edge of the object graph it is reached
through.  Paths also carry an optional fallback role that is used when the
outlet runs in non-strict mode and the requested role is missing.

The cycle support in :mod:`schematics.transforms` tracks model instances by
identity on a single stack that is popped whenever a branch finishes.
Re-encountering an instance that is still on the stack means the current
edge closes a real cycle; it is reported through a user supplied handler,
which can emit a checkable reference instead of recursing forever.  The
same instance reached from two independent branches (for example a shared
address used for both pickup and delivery) is exported twice and never
reported as a cycle.
"""

from __future__ import unicode_literals, absolute_import

from collections import OrderedDict


__all__ = ['ExportPath', 'RoleError', 'field_allowed', 'visible_reference',
           'default_cycle_handler', 'schema_name']


def schema_name(schema):
    """
    Return the model name from either a ``Schema`` instance or a model class.
    """
    if isinstance(schema, type):
        return schema.__name__
    return schema.name


def _schema_name_chain(schema):
    """
    Yield model names from the most specific class to its model bases.

    Entries on an :class:`ExportPath` are matched against these names in
    order, so a subclass without its own path entry inherits the role
    configured for its nearest model base class.
    """
    model_class = getattr(schema, 'model', None)
    if model_class is not None:
        schema = model_class
    if not isinstance(schema, type):
        yield schema.name
        return
    for klass in schema.__mro__:
        if hasattr(klass, '_schema'):
            yield klass.__name__


class RoleError(ValueError):
    """Raised when an export path asks for a role a model does not define."""

    def __init__(self, model_name, role_name, path_name=None):
        self.model_name = model_name
        self.role_name = role_name
        self.path_name = path_name
        message = '%s Model has no role "%s"' % (model_name, role_name)
        if path_name is not None:
            message += ' on export path "%s"' % path_name
        super(RoleError, self).__init__(message)


class ExportPath(object):
    """
    Role selection rules for one model field.

    :param name:
        Name of the field this path is attached to (used in error messages
        and cycle references).
    :param roles:
        Mapping of model class names to the role to apply on this path.
        Models without an entry fall back to the outlet level role.
    :param fallback_roles:
        Mapping of model class names to the role to use when the outlet
        runs with ``raise_error_on_role=False`` and the requested role is
        not defined on the model.
    """

    def __init__(self, name, roles=None, fallback_roles=None):
        self.name = name
        self.roles = dict(roles or {})
        self.fallback_roles = dict(fallback_roles or {})

    def role_for(self, schema):
        for name in _schema_name_chain(schema):
            if name in self.roles:
                return self.roles[name]
        return None

    def fallback_role_for(self, schema):
        for name in _schema_name_chain(schema):
            if name in self.fallback_roles:
                return self.fallback_roles[name]
        return None

    def __repr__(self):
        return '<ExportPath %s>' % self.name


def _schema_of(model_or_schema):
    return getattr(model_or_schema, '_schema', model_or_schema)


def _role_predicate(schema, role_name):
    if callable(role_name):
        return role_name
    role = schema._options.roles.get(role_name)
    if role is None:
        role = schema._options.roles.get('default')
    return role


def field_allowed(model_or_schema, field_name, role_name):
    """
    Return whether ``field_name`` survives the given role without ever
    exporting the field value.

    The decision only inspects the role predicate, so callers can decide
    whether a field may be read without touching a field that the role
    forbids.
    """
    schema = _schema_of(model_or_schema)
    role = _role_predicate(schema, role_name)
    if role is None:
        return True
    return not role(field_name, None)


def _read_field(instance, field_name):
    try:
        return getattr(instance, field_name)
    except AttributeError:
        try:
            return instance[field_name]
        except (KeyError, TypeError):
            return None


def visible_reference(instance, role_name, field_names):
    """
    Build a cycle reference from ``instance`` using fields the role allows.

    ``field_names`` are tried in order and the first present, role-visible,
    non-``None`` value wins for each name.  Fields forbidden by the role are
    never read, so internal identifiers cannot leak through a reference.
    Plain mappings without a schema include every requested field, since no
    role predicate can be evaluated for them.
    """
    schema = getattr(instance, '_schema', None)
    reference = OrderedDict()
    for field_name in field_names:
        if schema is not None and not field_allowed(schema, field_name, role_name):
            continue
        value = _read_field(instance, field_name)
        if value is not None:
            reference[field_name] = value
    return reference


def default_cycle_handler(instance, export_path, role_name, context):
    """
    Cycle handler used when no explicit handler is supplied.

    It stops the recursion with a plain marker so a cyclic object graph
    cannot trigger infinite recursion.
    """
    return {'cycle': True}

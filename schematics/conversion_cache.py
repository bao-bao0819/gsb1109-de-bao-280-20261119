# -*- coding: utf-8 -*-

from __future__ import unicode_literals, absolute_import

from .exceptions import FieldError, CompoundError

__all__ = ['ConversionCache', 'cached_conversion', 'get_cache', 'new_cache_id']


_MISS = object()


def new_cache_id():
    """Return a fresh identifier for a source configuration revision."""
    new_cache_id.counter += 1
    return new_cache_id.counter


new_cache_id.counter = 0


class ConversionCache(object):
    """
    A per-context memo of expensive field conversions.

    A result is only reused when every part of the conversion key is
    identical: the field instance, the exact raw value, the source
    configuration revision, the root model and the field path inside it.
    The root identity keeps caches of different top-level instances apart
    even though field objects are shared between instances of the same
    model, while the path distinguishes embedded objects of the same type
    (e.g. two address subobjects). The configuration revision makes
    in-place mutations of a mutable source configuration observable
    without relying on object identity.

    A second, path-indexed lookup covers the case where the value handed
    to a later pass is already the converted result resident on the
    model: when the incoming value is the exact object produced by the
    cached conversion under the same configuration, the conversion is not
    repeated.
    """

    def __init__(self):
        self._entries = {}
        self._resident = {}
        self.hits = 0
        self.misses = 0

    def get(self, field, value, config_id, owner, path):
        exact_key = (id(field), value, config_id, id(owner), path)
        try:
            entry = self._entries[exact_key]
        except TypeError:
            entry = _MISS
        except KeyError:
            entry = _MISS
        if entry is not _MISS:
            self.hits += 1
            return entry
        resident_key = (id(owner), path)
        resident = self._resident.get(resident_key)
        if resident is not None \
                and resident['field_id'] == id(field) \
                and resident['config_id'] == config_id \
                and value is resident['result']:
            self.hits += 1
            return resident['result']
        self.misses += 1
        return _MISS

    def put(self, field, value, config_id, owner, path, result):
        exact_key = (id(field), value, config_id, id(owner), path)
        try:
            self._entries[exact_key] = result
        except TypeError:
            pass
        if not isinstance(result, BaseException):
            self._resident[(id(owner), path)] = {
                'field_id': id(field),
                'config_id': config_id,
                'result': result,
            }


def get_cache(context):
    """Return the conversion cache attached to ``context``, if any."""
    return getattr(context, 'conversion_cache', None)


def cached_conversion(field, value, context, owner):
    """
    Convert ``value`` through ``field``, reusing a cached result when the
    field, the raw value, the source configuration, the root model and the
    field path all match a previous conversion in the same context. A
    cached conversion error is re-raised unchanged.
    """
    cache = get_cache(context)
    if cache is None or not getattr(field, 'reuse_conversion', False):
        return field.convert(value, context)
    config_id = getattr(context, 'config_id', None)
    path = tuple(getattr(context, 'conversion_path', ()) or ())
    entry = cache.get(field, value, config_id, owner, path)
    if entry is not _MISS:
        if isinstance(entry, BaseException):
            raise entry
        return entry
    try:
        result = field.convert(value, context)
    except (FieldError, CompoundError) as exc:
        cache.put(field, value, config_id, owner, path, exc)
        raise
    cache.put(field, value, config_id, owner, path, result)
    return result

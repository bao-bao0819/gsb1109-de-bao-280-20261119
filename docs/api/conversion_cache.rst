.. _api_doc_conversion_cache:

==================
Conversion Caching
==================

.. automodule:: schematics.conversion_cache
   :members:


Usage
=====

A field opts into conversion reuse by declaring ``reuse_conversion=True``
(or by passing the same flag to its constructor). Such a field must convert a
given raw value deterministically for a given source configuration.

Caching is scoped to a model instance (or to an explicit
:class:`~schematics.conversion_cache.ConversionCache` passed through the
functional :func:`~schematics.transforms.convert` and
:func:`~schematics.validate` entry points), so separate instances never share
results even when their field objects are identical.

When the source configuration is stored in a mutable container and modified in
place, pass an explicit ``config_id`` and change it on every revision. The
cache keys on that identifier rather than on object identity, which makes
in-place configuration changes observable. Set ``reuse_conversions=False`` to
disable reuse for a single run.

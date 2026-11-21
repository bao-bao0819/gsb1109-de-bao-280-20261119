# -*- coding: utf-8 -*-

"""Source tracking for values that are filtered, split, merged and reordered.

The helpers in this module deliberately keep provenance on objects, never on
the value itself.  Two converted records may therefore have equal amounts,
equal business numbers and even be the same Python value while still being
traceable to different positions in the input.

Coordinates are expressed as a tuple of :class:`SourceCoordinate` instances.
Each coordinate names the list field that produced the item and the item's
position in that original, unconverted input.  The first position of a
provenance path is the outermost item (for example an order), the last one is
the innermost detail.
"""

from __future__ import unicode_literals, absolute_import

from collections import namedtuple

from .exceptions import CompoundError

try:
    from collections.abc import Mapping  # PY3
except ImportError:
    from collections import Mapping  # PY2

__all__ = [
    'SourceCoordinate', 'Provenance', 'TrackedItem', 'ProvenanceTracker',
    'Rejection', 'IngestError', 'ListIngestResult', 'BatchIngestResult',
    'track', 'split', 'merge', 'reorder', 'group_by_kind',
    'describe_positions', 'provenance_at', 'locate', 'as_tracked',
    'walk_tracked', 'ingest_batch',
    'AmbiguousProvenanceError', 'MissingProvenanceError',
]


class SourceCoordinate(namedtuple('SourceCoordinate', 'field index')):
    """A list field and the original position of an item in that list."""

    __slots__ = ()

    def __new__(cls, field, index):
        return super(SourceCoordinate, cls).__new__(cls, field, index)


class AmbiguousProvenanceError(LookupError):
    """Raised when a value cannot identify a single tracked item."""


class MissingProvenanceError(LookupError):
    """Raised when a requested reorganized position does not exist."""


def _coordinate(entry):
    if isinstance(entry, SourceCoordinate):
        return entry
    return SourceCoordinate(entry[0], entry[1])


class Provenance(object):
    """The origin of one converted value.

    ``path`` is a tuple of :class:`SourceCoordinate` objects.  A directly
    converted item has no ``base`` and no ``contributors``.  An item produced
    by splitting another item has a ``base``.  An item produced by merging
    several items has ``contributors``.  The three cases are intentionally
    distinct: a split keeps one original position but adds a derived kind,
    while a merge keeps every contributing position.
    """

    __slots__ = ('path', 'field', 'kind', 'base', 'contributors', 'label',
                 'children')

    def __init__(self, path=(), field=None, kind=None, base=None,
                 contributors=(), label=None, children=()):
        self.path = tuple(_coordinate(coordinate) for coordinate in path)
        self.field = field
        self.kind = kind
        self.base = base
        self.contributors = tuple(contributors)
        self.label = label
        self.children = tuple(children)

    @property
    def positions(self):
        """Original positions, outermost first."""
        return tuple(coordinate.index for coordinate in self.path)

    @property
    def original_positions(self):
        return self.positions

    @property
    def parent_index(self):
        """The original position of the parent item, if present."""
        if len(self.path) >= 2:
            return self.path[-2].index
        if self.base is not None:
            return self.base.parent_index
        if self.contributors:
            return self.contributors[0].parent_index
        return None

    @property
    def detail_index(self):
        """The original position of the innermost item, if present."""
        if self.path:
            return self.path[-1].index
        if self.base is not None:
            return self.base.detail_index
        if self.contributors:
            return self.contributors[0].detail_index
        return None

    @property
    def position(self):
        """The item's own original position, independent of its parent."""
        return self.detail_index

    @property
    def is_derived(self):
        return self.base is not None or bool(self.contributors)

    @property
    def is_split(self):
        return self.base is not None

    @property
    def is_merged(self):
        return bool(self.contributors)

    def source_refs(self, role=None):
        """Return a flat list of original contributions.

        Each entry contains both ``parent``/``detail`` convenience fields and
        the complete ``positions`` tuple.  ``kind`` describes the value that
        was produced; ``original_kind`` describes the input record it came
        from.  For a split item the two differ, which is how a freight value
        remains distinguishable from the goods value produced by the same
        mixed input record.
        """
        if self.contributors:
            refs = []
            for contributor in self.contributors:
                refs.extend(contributor.source_refs(role=contributor.kind))
            return refs

        path = self.path
        if not path and self.contributors:
            path = self.contributors[0].path
        if not path and self.base is not None:
            path = self.base.path
        original_kind = self.base.kind if self.base is not None else self.kind
        return [{
            'positions': tuple(coordinate.index for coordinate in path),
            'path': tuple((coordinate.field, coordinate.index)
                          for coordinate in path),
            'parent': path[-2].index if len(path) >= 2 else None,
            'detail': path[-1].index if path else None,
            'field': path[-1].field if path else None,
            'kind': self.kind,
            'original_kind': original_kind,
            'derived_kind': self.kind if self.base is not None else None,
            'role': role if role is not None else self.kind,
        }]

    @property
    def contributors_refs(self):
        """Sources of a merged item, one group per contributor."""
        return [contributor.source_refs() for contributor in self.contributors]

    def describe(self):
        """A description used in reports.

        The result is intentionally free of the value itself.  Two tracked
        items with identical values therefore produce different descriptions
        whenever their original positions differ.
        """
        return {
            'positions': self.positions,
            'path': tuple((coordinate.field, coordinate.index)
                          for coordinate in self.path),
            'parent': self.parent_index,
            'detail': self.detail_index,
            'position': self.position,
            'kind': self.kind,
            'is_derived': self.is_derived,
            'is_split': self.is_split,
            'is_merged': self.is_merged,
            'sources': self.source_refs(),
            'contributors': self.contributors_refs,
            'children': [child.describe() for child in self.children],
        }

    def map_path(self, prefix):
        """Return the same provenance with an outer path prepended."""
        prefix = tuple(_coordinate(coordinate) for coordinate in prefix)
        return Provenance(path=prefix + self.path, field=self.field,
                          kind=self.kind, base=self.base,
                          contributors=self.contributors, label=self.label)

    def __eq__(self, other):
        if not isinstance(other, Provenance):
            return False
        return (self.path == other.path and self.kind == other.kind and
                self.source_refs() == other.source_refs())

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((self.path, self.kind, tuple(
            (ref['positions'], ref['kind']) for ref in self.source_refs())))

    def __repr__(self):
        return 'Provenance(%r, kind=%r)' % (self.positions, self.kind)


class TrackedItem(object):
    """A converted value together with its provenance.

    Equality includes provenance.  In particular, two tracked items holding
    identical values still compare unequal when they came from different
    original records.  Use ``.value`` when only the converted value matters.
    """

    __slots__ = ('value', 'provenance')

    def __init__(self, value, provenance):
        self.value = value
        self.provenance = provenance

    @property
    def kind(self):
        return self.provenance.kind

    @property
    def positions(self):
        return self.provenance.positions

    @property
    def parent_index(self):
        return self.provenance.parent_index

    @property
    def detail_index(self):
        return self.provenance.detail_index

    @property
    def position(self):
        return self.provenance.position

    @property
    def contributors(self):
        return self.provenance.contributors

    def source_refs(self, role=None):
        return self.provenance.source_refs(role=role)

    def describe(self, value=False):
        description = self.provenance.describe()
        if value:
            description['value'] = self.value
        return description

    def __eq__(self, other):
        if not isinstance(other, TrackedItem):
            return False
        return (self.value == other.value and
                self.provenance.path == other.provenance.path and
                self.provenance.kind == other.provenance.kind and
                self.provenance.contributors == other.provenance.contributors)

    def __ne__(self, other):
        return not self == other

    # Values may be unhashable model instances, and equality includes the
    # value, so a consistent hash is not available.  Tracked items are used
    # by position and identity, not as dictionary keys.
    __hash__ = None

    def __repr__(self):
        return 'TrackedItem(%r, %r)' % (self.value, self.provenance)


class Rejection(object):
    """One rejected input item, addressed in original coordinates.

    ``scope`` is ``'item'`` for a rejection of the item at the innermost
    coordinate and ``'parent'`` when the item itself could not be interpreted
    (for example a non-mapping where a model was expected).  A parent-level
    rejection therefore has ``parent`` and ``detail`` set to ``None`` even
    though it still has an original position.
    """

    __slots__ = ('index', 'path', 'error', 'reason', 'code', 'value', 'scope')

    def __init__(self, index=None, path=(), error=None, reason=None, code=None,
                 value=None, scope='item'):
        self.index = index
        self.path = tuple(_coordinate(coordinate) for coordinate in path)
        self.error = error
        self.reason = reason if reason is not None else str(error)
        self.code = code or self._code_for(error)
        self.value = value
        self.scope = scope

    @staticmethod
    def _code_for(error):
        if error is None:
            return 'invalid'
        name = type(error).__name__.lower()
        if 'conversion' in name:
            return 'conversion'
        if 'validation' in name:
            return 'validation'
        if 'compound' in name or 'data' in name:
            return 'nested'
        return 'error'

    @property
    def position(self):
        if self.path:
            return self.path[-1].index
        return self.index

    @property
    def original_index(self):
        return self.position

    @property
    def parent(self):
        if self.scope == 'parent':
            return None
        if len(self.path) >= 2:
            return self.path[-2].index
        return None

    @property
    def parent_index(self):
        return self.parent

    @property
    def detail(self):
        if self.scope == 'parent':
            return None
        if len(self.path) >= 2:
            return self.path[-1].index
        if len(self.path) == 1:
            return self.path[-1].index
        return None

    @property
    def detail_index(self):
        return self.detail

    @property
    def field(self):
        if self.path:
            return self.path[-1].field
        return None

    @property
    def message(self):
        return self.reason

    def with_prefix(self, prefix):
        """Prepend an outer path, keeping the innermost original positions."""
        return Rejection(index=self.index, path=tuple(prefix) + self.path,
                         error=self.error, reason=self.reason,
                         code=self.code, value=self.value, scope=self.scope)

    def to_dict(self):
        return {
            'position': self.position,
            'index': self.position,
            'original_index': self.original_index,
            'parent': self.parent,
            'parent_index': self.parent_index,
            'detail': self.detail,
            'detail_index': self.detail_index,
            'path': tuple((coordinate.field, coordinate.index)
                          for coordinate in self.path),
            'field': self.field,
            'reason': self.reason,
            'message': self.message,
            'code': self.code,
            'scope': self.scope,
            'error': type(self.error).__name__ if self.error is not None else None,
        }

    def __repr__(self):
        return 'Rejection(position=%r, parent=%r, reason=%r)' % (
            self.position, self.parent, self.reason)


class IngestError(CompoundError):
    """Raised by a converting list so a parent item cannot be delivered.

    The exception carries the complete :class:`ListIngestResult`, including
    every rejection in original coordinates.  A failed parent is therefore
    distinguishable from an error in its own type, and no partially converted
    children are exposed as deliverable values.
    """

    def __init__(self, result):
        self.result = result
        self.rejections = tuple(result.errors)
        errors = {}
        for rejection in self.rejections:
            key = rejection.position
            if key is None:
                key = len(errors)
            errors[key] = rejection.error
        self._by_position = errors
        super(IngestError, self).__init__(errors)

    def __getitem__(self, position):
        return self._by_position[position]

    def __contains__(self, position):
        return position in self._by_position

    def __iter__(self):
        return iter(self._by_position)

    def __len__(self):
        return len(self._by_position)

    def keys(self):
        return self._by_position.keys()

    def items(self):
        return self._by_position.items()

    def to_dict(self):
        return self.result.to_dict()

    def __repr__(self):
        return 'IngestError(%r)' % (self.rejections,)


class ProvenanceTracker(object):
    """Collects every provenance node produced during one ingest call.

    The tracker is attached to the conversion context, so nested ``ListType``
    fields append their coordinates without any value-based lookup.  Lookups
    are performed with ``is`` only, never with ``==``; duplicate business
    numbers and equal amounts therefore cannot select the wrong source.
    """

    def __init__(self):
        self.records = []
        self.results = []

    def record(self, provenance, value=None):
        self.records.append((provenance, value))
        return provenance

    def record_result(self, result):
        self.results.append(result)
        return result

    def truncate(self, length):
        del self.records[length:]

    def results_for(self, parent_index):
        """Nested list results whose outer parent position matches."""
        return [result for result in self.results
                if result.parent_index == parent_index]

    def tracked_children(self, parent_index):
        entries = []
        for result in self.results_for(parent_index):
            entries.extend(result.tracked)
        return entries

    def provenance_for(self, value):
        """Return the provenance of the exact object ``value`` (identity)."""
        found = None
        for provenance, candidate in self.records:
            if candidate is value:
                if found is not None:
                    raise AmbiguousProvenanceError(
                        'Value occurs at more than one source position')
                found = provenance
        if found is None:
            raise MissingProvenanceError(
                'Value has no tracked source in this ingest call')
        return found

    def tracked_for(self, value):
        provenance = self.provenance_for(value)
        return TrackedItem(value, provenance)

    def snapshot(self):
        return tuple(self.records)


def walk_tracked(value):
    """Yield every tracked child reachable from ``value``, depth first.

    Nested ``ListType`` ingest results are attached to their converted models
    as private attributes; this helper makes them discoverable without relying
    on field names, duplicate identifiers, or equal values.
    """
    results = getattr(value, '_ingest_results', None) or {}
    for result in results.values():
        for entry in result.tracked:
            yield entry
            for child in walk_tracked(entry.value):
                yield child


class ListIngestResult(object):
    """The outcome of ingesting one list.

    ``items`` (and its aliases) contains only deliverable converted values.
    ``errors`` contains :class:`Rejection` objects whose ``parent`` and
    ``detail`` attributes are original input positions.  ``tracked`` contains
    the same deliverable values wrapped with provenance.
    """

    def __init__(self, tracked=(), errors=(), skipped=(), outcomes=(),
                 tracker=None, field_name=None, min_size=None, max_size=None,
                 parent_index=None, length_error=None):
        self.tracked = list(tracked)
        self.errors = list(errors)
        self.skipped = list(skipped)
        self.outcomes = list(outcomes)
        self.tracker = tracker if tracker is not None else ProvenanceTracker()
        self.field_name = field_name
        self.min_size = min_size
        self.max_size = max_size
        self.parent_index = parent_index
        self.length_error = length_error
        self.consumed = 0
        self.original_count = 0

    def track(self, value, index):
        """Add a converted value at an original position, returning it.

        Nesting prepends the outer coordinate, so a detail ingested by an
        inner list still knows the original order it belonged to.
        """
        provenance = Provenance(
            path=(SourceCoordinate(self.field_name, index),),
            kind=self.field_name)
        if self.parent_index is not None:
            prefix = (SourceCoordinate(self.field_name, self.parent_index),)
            provenance = provenance.map_path(prefix)
        entry = TrackedItem(value, provenance)
        self.tracked.append(entry)
        self.tracker.record(provenance, value)
        return entry

    def reject(self, index, error, path=None):
        """Record a rejected item at its original position."""
        if path is None:
            path = (SourceCoordinate(self.field_name, index),)
        rejection = Rejection(index=index, path=path, error=error)
        self.errors.append(rejection)
        return rejection

    def skip(self, index):
        self.skipped.append(index)

    @property
    def items(self):
        return [entry.value for entry in self.tracked]

    @property
    def values(self):
        return self.items

    @property
    def delivered(self):
        return self.items

    @property
    def data(self):
        return self.items

    @property
    def accepted(self):
        return self.items

    @property
    def records(self):
        return self.items

    @property
    def entries(self):
        return self.tracked

    @property
    def tracked_items(self):
        return self.tracked

    @property
    def sources(self):
        return [entry.provenance for entry in self.tracked]

    @property
    def positions(self):
        return [entry.position for entry in self.tracked]

    @property
    def original_indices(self):
        return self.positions

    @property
    def children(self):
        """All nested tracked items, grouped by their parent's position."""
        return dict((entry.position, walk_tracked(entry.value))
                    for entry in self.tracked)

    @property
    def rejected(self):
        return self.errors

    @property
    def rejections(self):
        return self.errors

    @property
    def reasons(self):
        return [rejection.reason for rejection in self.errors]

    @property
    def ok(self):
        return not self.errors and self.length_error is None

    @property
    def valid(self):
        return self.ok

    @property
    def complete(self):
        return self.ok

    @property
    def partial(self):
        return bool(self.errors) and bool(self.tracked)

    def __len__(self):
        return len(self.tracked)

    def __iter__(self):
        return iter(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def __bool__(self):
        return bool(self.tracked)

    __nonzero__ = __bool__

    def tracked_at(self, position):
        try:
            return self.tracked[position]
        except (IndexError, TypeError):
            raise MissingProvenanceError(
                'No deliverable item at reorganized position %r' % (position,))

    def provenance_at(self, position):
        return self.tracked_at(position).provenance

    def provenance_for(self, value):
        """Return provenance for a nested value by object identity."""
        for entry in self.tracked:
            if entry.value is value:
                return entry.provenance
        return self.tracker.provenance_for(value)

    def describe_positions(self, positions):
        """Map reorganized positions back to original and derived sources."""
        return describe_positions(self.tracked, positions)

    resolve_positions = describe_positions

    def locate(self, target):
        return locate(self.tracked, target)

    def split(self, position, parts):
        """Split the deliverable item at ``position`` into derived items."""
        return split(self.tracked_at(position), parts)

    def merge(self, items, value, kind='merged'):
        """Merge tracked items (or positions) into one tracked value."""
        if items and isinstance(items[0], int):
            items = [self.tracked_at(position) for position in items]
        return merge(items, value, kind=kind)

    def reorder(self, order=None, key=None):
        return reorder(self.tracked, order=order, key=key)

    def group_by_kind(self):
        return group_by_kind(self.tracked)

    def outcome(self, index):
        for outcome in self.outcomes:
            if outcome['index'] == index:
                return outcome
        return None

    @property
    def accepted_outcomes(self):
        return [outcome for outcome in self.outcomes
                if outcome['status'] == 'accepted']

    @property
    def rejected_outcomes(self):
        return [outcome for outcome in self.outcomes
                if outcome['status'] == 'rejected']

    @property
    def skipped_outcomes(self):
        return [outcome for outcome in self.outcomes
                if outcome['status'] == 'skipped']

    def as_pair(self):
        """Return ``(deliverable values, rejections)`` for convenient unpacking."""
        return self.items, self.errors

    def as_report(self):
        """Return ``(deliverable values, rejection details, provenance)``."""
        return (self.items,
                [rejection.to_dict() for rejection in self.errors],
                [entry.describe() for entry in self.tracked])

    def acceptable(self):
        """Deliverable values when every item converted, otherwise nothing.

        A caller that must not deliver partial data (for example an order
        whose detail list failed) uses this instead of ``items``.
        """
        return self.items if self.ok else []

    def raise_for_errors(self):
        if self.errors:
            raise IngestError(self)
        return self

    def to_dict(self, include_values=True):
        result = {
            'ok': self.ok,
            'valid': self.valid,
            'partial': self.partial,
            'field': self.field_name,
            'parent': self.parent_index,
            'items': [entry.describe(value=include_values)
                      for entry in self.tracked],
            'positions': self.positions,
            'skipped': list(self.skipped),
            'errors': [rejection.to_dict() for rejection in self.errors],
            'rejections': [rejection.to_dict() for rejection in self.errors],
            'outcomes': list(self.outcomes),
        }
        if include_values:
            result['values'] = self.items
        return result

    report = to_dict

    def __repr__(self):
        return 'ListIngestResult(items=%r, errors=%r)' % (
            self.items, self.errors)


def _attach_provenance(value, provenance):
    """Attach provenance to a converted object without wrapping the value.

    Models allow private attributes; immutable primitives simply ignore the
    attachment, and their provenance remains available through the tracked
    wrapper and the ingest report.
    """
    try:
        value._provenance = provenance
        value._source = provenance
    except (AttributeError, TypeError):
        pass


def _as_provenance(source, kind=None, field=None):
    if isinstance(source, Provenance):
        if kind is None:
            return source
        return Provenance(path=source.path, field=source.field, kind=kind,
                          base=source.base, contributors=source.contributors,
                          label=source.label)
    return Provenance(path=source, kind=kind, field=field)


def track(value, source, kind=None, field=None):
    """Wrap ``value`` with provenance from a path or another provenance."""
    return TrackedItem(value, _as_provenance(source, kind=kind, field=field))


def as_tracked(item):
    """Return a :class:`TrackedItem` for a tracked item or annotated model."""
    if isinstance(item, TrackedItem):
        return item
    provenance = getattr(item, '_provenance', None)
    if provenance is None:
        provenance = getattr(item, '_source', None)
    if isinstance(provenance, Provenance):
        return TrackedItem(item, provenance)
    raise TypeError(
        'Value %r does not carry provenance; ingest it with a tracked ListType'
        % (item,))


def split(item, parts):
    """Split one tracked item into independently tracked parts.

    ``parts`` is a mapping or an iterable of ``(kind, value)`` pairs.  Every
    returned item keeps the original position of ``item`` and records its own
    derived kind.  This is what makes a mixed detail's freight value
    distinguishable from the goods value produced from the same input record.
    """
    item = as_tracked(item)

    if isinstance(parts, Mapping):
        parts = parts.items()

    result = []
    for kind, value in parts:
        provenance = Provenance(path=item.provenance.path,
                                field=item.provenance.field,
                                kind=kind,
                                base=item.provenance)
        result.append(TrackedItem(value, provenance))
    return result


def merge(items, value, kind='merged'):
    """Merge several tracked items into one value while retaining all sources.

    The merged item's contributors are kept in order.  A caller that merges a
    goods item (itself derived from a mixed detail) with a tail discount item
    can therefore recover both original positions and the role each played.
    """
    contributors = []
    for item in items:
        item = as_tracked(item)
        contributors.append(item.provenance)

    path = contributors[0].path if contributors else ()
    provenance = Provenance(path=path, kind=kind,
                            contributors=tuple(contributors))
    return TrackedItem(value, provenance)


def reorder(items, order=None, key=None):
    """Return a new ordering without changing any provenance object.

    ``order`` may be an iterable of positions, in which case items are picked
    by identity.  Otherwise ``key`` (or the item's derived kind) is used for a
    stable sort.  The position used by an order-level rule is the position in
    the returned list; :func:`describe_positions` maps it back.
    """
    items = [as_tracked(item) for item in items]
    if order is not None:
        return [items[position] for position in order]
    if key is None:
        key = lambda item: item.kind or ''
    return sorted(items, key=key)


def group_by_kind(items):
    """Group tracked items by their concrete kind, preserving input order."""
    groups = {}
    for item in items:
        item = as_tracked(item)
        groups.setdefault(item.kind, []).append(item)
    return groups


def provenance_at(items, position):
    """Return the provenance at a reorganized list position."""
    try:
        item = items[position]
    except (IndexError, TypeError):
        raise MissingProvenanceError(
            'No tracked item at reorganized position %r' % (position,))
    if not isinstance(item, TrackedItem):
        raise MissingProvenanceError(
            'Item at position %r is not tracked' % (position,))
    return item.provenance


def describe_positions(items, positions):
    """Map reorganized positions back to original detail and derived sources.

    This is the bridge an order-level rule needs: the rule reports positions in
    the reorganized list, and the report names the original parent/detail
    positions plus every participating derived item.  Positions are resolved
    by list indexing, never by comparing amounts or business numbers.
    """
    reports = []
    for position in positions:
        provenance = provenance_at(items, position)
        report = provenance.describe()
        report['position'] = position
        reports.append(report)
    return reports


def locate(items, target):
    """Return the unique position of a tracked item, by identity only.

    Values are intentionally not accepted: identical output values and
    duplicate business numbers cannot identify a source.  Two distinct
    ``TrackedItem`` objects are never considered the same even when their
    values compare equal.
    """
    if not isinstance(target, TrackedItem):
        raise TypeError(
            'locate() requires a TrackedItem; values cannot identify a source')

    found = [index for index, item in enumerate(items) if item is target]
    if not found:
        raise MissingProvenanceError('Tracked item is not present in the list')
    if len(found) > 1:
        raise AmbiguousProvenanceError(
            'Tracked item occurs at more than one position: %r' % (found,))
    return found[0]


def ingest_batch(field, values):
    """Ingest an iterable of parent items with an explicit ``ListType``.

    This is a thin convenience wrapper; ``field.ingest(values)`` is equivalent.
    """
    from .types.compound import ListType

    if not isinstance(field, ListType):
        raise TypeError('ingest_batch() requires a ListType')
    return field.ingest(values)


class BatchIngestResult(object):
    """The outcome of one batch ingest call.

    Each parent item is isolated: ``results`` contains one
    :class:`ListIngestResult` per original parent position, including parents
    skipped as ``None``.  ``items`` concatenates the deliverable details of
    successful parents.  ``errors`` contains rejections for failed parents and
    for invalid details inside otherwise successful parents.  A failed parent
    contributes no details at all.
    """

    def __init__(self, field_name=None):
        self.field_name = field_name
        self.results = []
        self.length_error = None

    def add(self, result):
        self.results.append(result)

    @property
    def items(self):
        values = []
        for result in self.results:
            values.extend(result.items)
        return values

    values = items
    delivered = items
    data = items

    @property
    def deliverable(self):
        """Details whose parent order itself converted successfully.

        This is the "can be stored" view for a batch: an order with a
        rejected detail contributes nothing, even if sibling details
        converted.
        """
        values = []
        for result in self.results:
            if not result.errors:
                values.extend(result.items)
        return values

    acceptable = deliverable

    @property
    def rejected_parents(self):
        return [result.parent_index for result in self.results
                if result.errors and not result.tracked]

    @property
    def tracked(self):
        entries = []
        for result in self.results:
            entries.extend(result.tracked)
        return entries

    @property
    def errors(self):
        rejections = []
        for result in self.results:
            rejections.extend(result.errors)
        return rejections

    rejections = errors

    @property
    def skipped(self):
        """Top-level parent positions skipped as placeholders.

        Placeholders inside a parent are available from that parent's own
        result (``result_for(index).skipped``).  Keeping the levels separate
        prevents an inner position from being mistaken for an outer one.
        """
        return [result.parent_index for result in self.results
                if result.parent_index is not None
                and not result.tracked and not result.errors
                and result.outcomes
                and result.outcomes[0].get('status') == 'skipped']

    @property
    def parent_index(self):
        for result in self.results:
            if result.parent_index is not None:
                return result.parent_index
        return None

    @property
    def outcomes(self):
        return [outcome for result in self.results
                for outcome in result.outcomes]

    @property
    def ok(self):
        return not self.errors and self.length_error is None

    valid = ok
    complete = ok

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def result_for(self, parent_index):
        for result in self.results:
            if result.parent_index == parent_index:
                return result
        raise MissingProvenanceError(
            'No ingest result for parent position %r' % (parent_index,))

    def items_for(self, parent_index):
        return self.result_for(parent_index).items

    def errors_for(self, parent_index):
        return self.result_for(parent_index).errors

    def describe_positions(self, positions):
        """Resolve flat deliverable positions back to original coordinates."""
        return describe_positions(self.tracked, positions)

    resolve_positions = describe_positions

    def children_for(self, parent_index):
        """Nested tracked details belonging to one original parent position."""
        return [entry for entry in self.tracked
                if entry.parent_index == parent_index]

    def children_of(self, result):
        """Tracked details of one parent's result (object identity)."""
        for parent in self.results:
            if parent is result:
                return parent.tracked
        raise MissingProvenanceError('Result does not belong to this batch')

    def deliverables_by_parent(self):
        """Mapping of original parent position to deliverable details.

        A rejected parent maps to an empty list; no partial details leak.
        """
        return dict((result.parent_index, result.items)
                    for result in self.results)

    def reorder(self, order=None, key=None):
        """Return this result's items in a new order, keeping provenance."""
        return ListIngestResult(
            tracked=reorder(self.tracked, order=order, key=key),
            errors=self.errors, skipped=self.skipped, outcomes=self.outcomes,
            tracker=self.tracker, field_name=self.field_name,
            min_size=self.min_size, max_size=self.max_size,
            parent_index=self.parent_index, length_error=self.length_error)

    def split(self, position, parts):
        """Split the item at a deliverable position into derived items."""
        return split(self.tracked_at(position), parts)

    def merge(self, items, value, kind='merged'):
        """Merge tracked items or deliverable positions into one tracked item."""
        if items and isinstance(items[0], int):
            items = [self.tracked_at(position) for position in items]
        return merge(items, value, kind=kind)

    def describe_split(self, items):
        return [entry.describe() for entry in items]

    def contributions(self, items):
        """All original contributions of split/merged tracked items."""
        refs = []
        for item in items:
            for ref in as_tracked(item).source_refs():
                entry = dict(ref)
                entry['positions'] = tuple(entry['positions'])
                refs.append(entry)
        return refs

    def to_dict(self, include_values=True):
        return {
            'ok': self.ok,
            'field': self.field_name,
            'items': [entry.describe(value=include_values)
                      for entry in self.tracked],
            'errors': [rejection.to_dict() for rejection in self.errors],
            'rejections': [rejection.to_dict() for rejection in self.errors],
            'skipped': list(self.skipped),
            'parents': [result.to_dict(include_values=include_values)
                        for result in self.results],
        }

    report = to_dict

    def __repr__(self):
        return 'BatchIngestResult(items=%r, errors=%r)' % (
            self.items, self.errors)

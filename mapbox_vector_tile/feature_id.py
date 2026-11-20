"""Stable numeric feature IDs for batched MVT publishing.

MVT feature IDs are unsigned 64-bit integers, while the upstream source of a
feature is often identified by a string key.  A :class:`FeatureIdMapping`
keeps the assignment between source keys and numeric IDs for a single scope,
so that successive publishing batches can reuse the same IDs and so that a
point lookup on a decoded tile can be traced back to the original source.

A source key is either a string or an integer that is already a native MVT
feature ID.  The two forms are kept distinct: the string ``"42"`` and the
integer ``42`` are different sources.  The integer form reserves the numeric
ID ``42`` directly, while the string form receives an ID from the mapping's
available range.
"""

import json
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from numbers import Integral
from pathlib import Path

UINT64_MAX = (1 << 64) - 1

STRING_KIND = "string"
NUMBER_KIND = "number"

_MAPPING_VERSION = 1


class FeatureIdError(ValueError):
    """Base class for errors raised while maintaining a feature ID mapping."""


class FeatureIdValueError(FeatureIdError):
    """Raised when a scope, source key, or numeric ID has an invalid value."""


class FeatureIdRangeError(FeatureIdError):
    """Raised when the configured or requested ID range is not usable."""


class FeatureIdRangeExhaustedError(FeatureIdRangeError):
    """Raised when a batch needs more new IDs than the mapping has available."""

    def __init__(self, scope, minimum, maximum, requested):
        self.scope = scope
        self.minimum = minimum
        self.maximum = maximum
        self.requested = requested
        super().__init__(
            f"No free feature ids remain in scope {scope!r} for the range "
            f"[{minimum}, {maximum}]; {requested} new string source key(s) requested."
        )


class FeatureIdConflictError(FeatureIdError):
    """Raised when a numeric ID is requested for a different source key.

    The exception exposes both sides of the conflict through
    :attr:`existing_source` and :attr:`requested_source` (and their ``kind``
    counterparts), in addition to the scope and numeric ID.
    """

    def __init__(self, scope, numeric_id, existing_kind, existing_source, requested_kind, requested_source):
        self.scope = scope
        self.numeric_id = numeric_id
        self.existing_kind = existing_kind
        self.existing_source = existing_source
        self.requested_kind = requested_kind
        self.requested_source = requested_source
        super().__init__(
            f"Feature id {numeric_id} in scope {scope!r} is already assigned to "
            f"{_describe_source(existing_kind, existing_source)}; cannot assign it to "
            f"{_describe_source(requested_kind, requested_source)}."
        )

    @property
    def sources(self):
        """Return the existing and requested source keys as a pair."""
        return self.existing_source, self.requested_source


@dataclass(frozen=True)
class FeatureIdEntry:
    """A single source-key to numeric-ID assignment."""

    scope: str
    numeric_id: int
    source: object
    kind: str


def _describe_source(kind, source):
    return f"{kind} source {source!r}"


def _validate_scope(scope):
    if not isinstance(scope, str) or not scope:
        raise FeatureIdValueError(f"The mapping scope must be a non-empty string. {scope!r} was provided.")
    return scope


def _validate_range_value(value, name):
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise FeatureIdRangeError(f"The {name} feature id must be an integer. {value!r} was provided.")
    value = int(value)
    if value < 0 or value > UINT64_MAX:
        raise FeatureIdRangeError(f"The {name} feature id must be between 0 and {UINT64_MAX}. {value!r} was provided.")
    return value


def _validate_range(minimum, maximum):
    minimum = _validate_range_value(minimum, "minimum")
    maximum = _validate_range_value(maximum, "maximum")
    if minimum > maximum:
        raise FeatureIdRangeError(
            f"The minimum feature id must not exceed the maximum. [{minimum}, {maximum}] was provided."
        )
    return minimum, maximum


def _normalize_source(source):
    if isinstance(source, bool):
        raise FeatureIdValueError(f"A feature source key must be a string or an integer. {source!r} was provided.")
    if isinstance(source, str):
        return STRING_KIND, source
    if isinstance(source, Integral):
        value = int(source)
        if value < 0 or value > UINT64_MAX:
            raise FeatureIdValueError(
                f"A native feature id must be between 0 and {UINT64_MAX}. {value!r} was provided."
            )
        return NUMBER_KIND, value
    raise FeatureIdValueError(f"A feature source key must be a string or an integer. {source!r} was provided.")


def _validate_numeric_id(numeric_id):
    return _validate_range_value(numeric_id, "requested")


class FeatureIdMapping:
    """A persistable source-key to numeric feature-ID mapping for one scope.

    Args:
        scope:
            The publishing scope this mapping belongs to, for example a layer
            name.  Different scopes are maintained by different mapping
            objects and do not affect each other.
        minimum / maximum:
            The inclusive range of numeric IDs available to newly allocated
            string source keys.  Native integer source keys must also fall
            inside this range.
        id_range:
            Alternative way to pass ``minimum`` and ``maximum`` as a pair.
        entries:
            Optional iterable of :class:`FeatureIdEntry` objects or entry
            dictionaries used to restore an existing mapping.

    Assignments are only changed by :meth:`allocate`.  A batch is validated in
    full before anything is committed, so a conflict or range exhaustion
    leaves the mapping exactly as it was.
    """

    def __init__(self, scope, minimum=None, maximum=None, entries=None, id_range=None):
        if id_range is not None:
            if minimum is not None or maximum is not None:
                raise FeatureIdRangeError("Provide either id_range or minimum/maximum, not both.")
            try:
                minimum, maximum = id_range
            except (TypeError, ValueError) as exc:
                raise FeatureIdRangeError(
                    f"The id_range must be a (minimum, maximum) pair. {id_range!r} was provided."
                ) from exc
        elif maximum is None and isinstance(minimum, (tuple, list)):
            minimum, maximum = minimum

        if minimum is None or maximum is None:
            raise FeatureIdRangeError("An inclusive feature id range must be provided.")

        self.scope = _validate_scope(scope)
        self.minimum, self.maximum = _validate_range(minimum, maximum)
        self._by_source = {}
        self._by_id = {}

        if entries is not None:
            for entry in entries:
                self._install_entry(entry)

    @classmethod
    def from_dict(cls, data):
        """Restore a mapping from the dictionary produced by :meth:`to_dict`."""
        if not isinstance(data, dict):
            raise FeatureIdError(f"A feature id mapping must be a dictionary. {data!r} was provided.")
        if data.get("version") != _MAPPING_VERSION:
            raise FeatureIdError(
                f"Unsupported feature id mapping version {data.get('version')!r}; expected {_MAPPING_VERSION}."
            )

        scope = data.get("scope")
        id_range = data.get("range")
        if id_range is None:
            id_range = (data.get("minimum"), data.get("maximum"))

        mapping = cls(scope, id_range=id_range)
        for entry in data.get("entries", ()):
            if not isinstance(entry, dict):
                raise FeatureIdError(f"Feature id mapping entries must be dictionaries. {entry!r} was provided.")
            try:
                numeric_id = entry["id"]
                kind = entry["kind"]
                source = entry["source"]
            except KeyError as exc:
                raise FeatureIdError(f"Feature id mapping entry is missing {exc.args[0]!r}: {entry!r}.") from exc
            mapping._install(kind, source, numeric_id)
        return mapping

    @classmethod
    def from_json(cls, text):
        """Restore a mapping from JSON text."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise FeatureIdError(f"Invalid feature id mapping JSON: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def load(cls, path):
        """Load a mapping saved by :meth:`save`."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise FeatureIdError(f"Unable to read feature id mapping {str(path)!r}: {exc}") from exc
        return cls.from_json(text)

    def to_dict(self):
        """Return a JSON-serializable representation of the mapping."""
        entries = []
        for numeric_id in sorted(self._by_id):
            kind, source = self._by_id[numeric_id]
            entries.append({"id": numeric_id, "kind": kind, "source": source})
        return {
            "version": _MAPPING_VERSION,
            "scope": self.scope,
            "range": [self.minimum, self.maximum],
            "entries": entries,
        }

    def to_json(self):
        """Return the mapping as stable, pretty-printed JSON text."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def save(self, path):
        """Persist the mapping as UTF-8 JSON.

        The file is replaced atomically so that an interrupted save cannot
        leave a truncated mapping behind.
        """
        path = Path(path)
        text = self.to_json()
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temp_name = handle.name
                handle.write(text)
            Path(temp_name).replace(path)
        except OSError as exc:
            if temp_name is not None:
                with suppress(OSError):
                    Path(temp_name).unlink()
            raise FeatureIdError(f"Unable to save feature id mapping {str(path)!r}: {exc}") from exc

    def copy(self):
        """Return an independent copy of the mapping."""
        return type(self).from_dict(self.to_dict())

    @contextmanager
    def transaction(self):
        """Run a block so that the mapping is restored if the block raises.

        This is useful when a numeric ID assignment and the tile emission that
        depends on it must succeed or fail together::

            with mapping.transaction():
                mapping.allocate(sources)
                emit_tile()
        """
        snapshot = self.copy()
        try:
            yield self
        except BaseException:
            self._replace_with(snapshot)
            raise

    def allocate(self, sources):
        """Assign numeric IDs for all ``sources`` as one atomic batch.

        Existing source keys keep their IDs.  New string keys receive the
        lowest free IDs in the configured range, in sorted order, which makes
        the result independent of the input order.  Native integer keys use
        their own value as the numeric ID and reserve it.

        Args:
            sources:
                An iterable of source keys, or a single source key.  Each key
                is either a string or a non-negative integer.

        Returns:
            A dictionary mapping every requested source key to its numeric ID.

        Raises:
            FeatureIdConflictError:
                If a requested numeric ID already belongs to another source.
            FeatureIdRangeError:
                If a native ID is outside the configured range.
            FeatureIdRangeExhaustedError:
                If there are not enough free IDs for the new string keys.

        No assignment is changed when any error is raised.
        """
        if isinstance(sources, bool) or sources is None:
            raise FeatureIdValueError(f"A feature source key must be a string or an integer. {sources!r} was provided.")
        if isinstance(sources, (str, Integral)):
            sources = [sources]

        normalized = {}
        for source in sources:
            kind, value = _normalize_source(source)
            normalized[(kind, value)] = source

        plan = {}
        result = {}

        for key, source in normalized.items():
            if key in self._by_source:
                result[source] = self._by_source[key]

        for key, source in normalized.items():
            if key in self._by_source:
                continue
            kind, value = key
            if kind != NUMBER_KIND:
                continue

            numeric_id = value
            if numeric_id < self.minimum or numeric_id > self.maximum:
                raise FeatureIdRangeError(
                    f"Native feature id {numeric_id} in scope {self.scope!r} is outside the available "
                    f"range [{self.minimum}, {self.maximum}]."
                )
            if numeric_id in self._by_id:
                existing_kind, existing_source = self._by_id[numeric_id]
                raise FeatureIdConflictError(self.scope, numeric_id, existing_kind, existing_source, kind, source)
            plan[numeric_id] = key
            result[source] = numeric_id

        new_strings = sorted(
            value for kind, value in normalized if kind == STRING_KIND and (kind, value) not in self._by_source
        )

        next_id = self.minimum
        for value in new_strings:
            while next_id <= self.maximum and (next_id in self._by_id or next_id in plan):
                next_id += 1
            if next_id > self.maximum:
                raise FeatureIdRangeExhaustedError(self.scope, self.minimum, self.maximum, len(new_strings))
            key = (STRING_KIND, value)
            plan[next_id] = key
            result[normalized[key]] = next_id
            next_id += 1

        for numeric_id, key in plan.items():
            self._by_id[numeric_id] = key
            self._by_source[key] = numeric_id

        return result

    def allocate_one(self, source):
        """Assign and return the numeric ID for a single source key."""
        return self.allocate([source])[source]

    def id_for(self, source):
        """Return the numeric ID already assigned to ``source``."""
        kind, value = _normalize_source(source)
        try:
            return self._by_source[(kind, value)]
        except KeyError as exc:
            raise KeyError(f"Source {source!r} is not assigned in feature id scope {self.scope!r}.") from exc

    def lookup(self, numeric_id):
        """Return the original source key assigned to ``numeric_id``."""
        kind, source = self._lookup_key(numeric_id)
        return source

    def kind_for(self, numeric_id):
        """Return ``"string"`` or ``"number"`` for ``numeric_id``."""
        kind, _ = self._lookup_key(numeric_id)
        return kind

    def entry_for(self, numeric_id):
        """Return the full :class:`FeatureIdEntry` for ``numeric_id``."""
        kind, source = self._lookup_key(numeric_id)
        return FeatureIdEntry(scope=self.scope, numeric_id=numeric_id, source=source, kind=kind)

    def resolve(self, numeric_id):
        """Return ``(source, kind, scope, minimum, maximum)`` for a numeric ID.

        This is the complete round-trip used by point lookups: the numeric ID
        from a decoded tile is mapped back to its original key and to the
        scope and inclusive range it was drawn from.
        """
        kind, source = self._lookup_key(numeric_id)
        return source, kind, self.scope, self.minimum, self.maximum

    @property
    def id_range(self):
        """The inclusive ``(minimum, maximum)`` range of this mapping."""
        return self.minimum, self.maximum

    def _lookup_key(self, numeric_id):
        numeric_id = _validate_numeric_id(numeric_id)
        try:
            return self._by_id[numeric_id]
        except KeyError as exc:
            raise KeyError(f"Feature id {numeric_id} is not assigned in scope {self.scope!r}.") from exc

    def _install_entry(self, entry):
        if isinstance(entry, FeatureIdEntry):
            self._install(entry.kind, entry.source, entry.numeric_id)
        elif isinstance(entry, dict):
            try:
                self._install(entry["kind"], entry["source"], entry["id"])
            except KeyError as exc:
                raise FeatureIdError(f"Feature id mapping entry is missing {exc.args[0]!r}: {entry!r}.") from exc
        else:
            raise FeatureIdError(
                f"Feature id mapping entries must be FeatureIdEntry objects or dictionaries. {entry!r} was provided."
            )

    def _install(self, kind, source, numeric_id):
        normalized_kind, value = _normalize_source(source)
        if kind != normalized_kind:
            raise FeatureIdValueError(f"Feature id source {source!r} is a {normalized_kind} key, not a {kind!r} key.")
        if kind not in (STRING_KIND, NUMBER_KIND):
            raise FeatureIdValueError(f"Unsupported feature id source kind {kind!r}.")

        numeric_id = _validate_numeric_id(numeric_id)
        if numeric_id < self.minimum or numeric_id > self.maximum:
            raise FeatureIdRangeError(
                f"Feature id {numeric_id} in scope {self.scope!r} is outside the available "
                f"range [{self.minimum}, {self.maximum}]."
            )
        if kind == NUMBER_KIND and value != numeric_id:
            raise FeatureIdConflictError(self.scope, numeric_id, NUMBER_KIND, value, kind, source)

        key = (kind, value)
        if numeric_id in self._by_id:
            existing_kind, existing_source = self._by_id[numeric_id]
            raise FeatureIdConflictError(self.scope, numeric_id, existing_kind, existing_source, kind, source)
        if key in self._by_source:
            raise FeatureIdError(f"Source {value!r} is assigned more than once in scope {self.scope!r}.")

        self._by_id[numeric_id] = key
        self._by_source[key] = numeric_id

    def _replace_with(self, other):
        if self.scope != other.scope or self.minimum != other.minimum or self.maximum != other.maximum:
            raise FeatureIdError("Cannot replace a feature id mapping with a different scope or range.")
        self._by_id = dict(other._by_id)
        self._by_source = dict(other._by_source)

    @property
    def allocated(self):
        """Return a ``{source: numeric_id}`` copy of the current assignments."""
        return {source: numeric_id for numeric_id, (_, source) in sorted(self._by_id.items())}

    def __len__(self):
        return len(self._by_id)

    def __iter__(self):
        for numeric_id in sorted(self._by_id):
            yield self.entry_for(numeric_id)

    def __contains__(self, numeric_id):
        try:
            _validate_numeric_id(numeric_id)
        except FeatureIdError:
            return False
        return numeric_id in self._by_id

    def __getitem__(self, numeric_id):
        return self.entry_for(numeric_id)

    def __repr__(self):
        return (
            f"{type(self).__name__}(scope={self.scope!r}, range=({self.minimum}, {self.maximum}), "
            f"allocated={len(self)})"
        )

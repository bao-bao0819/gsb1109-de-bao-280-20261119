"""Key-based matching of list items for ``DeepDiff(iterable_match_keys=...)``.

``iterable_match_keys`` maps paths (exact or glob) to a key spec. For the
lists selected by a path, items in t1 and t2 are paired by their key instead
of by position, so inserts, removals and reorders don't cascade into
``values_changed`` reports.
"""
from collections import deque
from collections.abc import Mapping
from typing import Any, Callable, Dict, Hashable, List, NamedTuple, Optional, Tuple, Union

from deepdiff.helper import (
    CannotCompare, ListItemRemovedOrAdded, add_root_to_paths, booleans, numbers,
)
from deepdiff.path import GlobPathMatcher, _path_to_elements, path_has_wildcard

KeySpec = Union[str, Tuple[str, ...], Callable[[Any], Hashable]]


class MatchKey(NamedTuple):
    """A primary key plus a secondary key used to disambiguate duplicate primaries.

    ``MatchKey("sku", "warehouse")`` groups items by ``sku`` and, within a
    group, pairs each t1 item with the most similar t2 item by ``warehouse``.
    Both fields use the same forms as ``iterable_match_keys``: a key name, a
    tuple of key names for a composite value, or a callable.
    """
    key: KeySpec
    secondary: KeySpec


# A mapping entry may be a plain KeySpec or a MatchKey. A two-tuple of complete
# key specs is also accepted as shorthand for MatchKey(*spec); a tuple of
# strings by itself stays a composite key.
MatchKeySpec = Union[KeySpec, MatchKey, Tuple[KeySpec, KeySpec]]

_ROOT_ELEMENT = ('root', 'GETATTR')


class _NoKey:
    """The item has no key: it is paired in order with other key-less items."""

    def __repr__(self):  # pragma: no cover
        return 'NO_KEY'


class _BoolTag:
    """Tags boolean key components so that ``True`` never matches ``1``."""

    def __repr__(self):  # pragma: no cover
        return 'BOOL'

    def __reduce__(self):
        # Keep the module-level singleton identity across pickling (multiprocessing).
        return 'BOOL_TAG'


NO_KEY = _NoKey()
BOOL_TAG = _BoolTag()

# Exceptions that mean "this item has no key" rather than a programming error.
_NO_KEY_EXCEPTIONS = (KeyError, IndexError, TypeError, CannotCompare)


def _validate_key_spec(path: str, spec: Any) -> MatchKeySpec:
    if isinstance(spec, MatchKey):
        return MatchKey(_validate_key_spec(path, spec.key), _validate_key_spec(path, spec.secondary))
    if isinstance(spec, str) or callable(spec):
        return spec
    if isinstance(spec, tuple) and spec and all(isinstance(i, str) for i in spec):
        return spec
    if isinstance(spec, (tuple, list)) and len(spec) == 2 and all(_is_key_spec(i) for i in spec):
        primary, secondary = spec
        if not (isinstance(primary, str) and isinstance(secondary, str)):
            return MatchKey(_validate_key_spec(path, primary), _validate_key_spec(path, secondary))
    raise ValueError(
        "iterable_match_keys[{!r}] must be a key name (str), a non-empty tuple of key names, "
        "a callable that takes an item and returns a hashable key, or a MatchKey / "
        "a (primary spec, secondary spec) pair. Got: {!r}".format(path, spec))


def _is_key_spec(spec: Any) -> bool:
    if isinstance(spec, str) or callable(spec):
        return True
    if isinstance(spec, tuple) and spec and all(isinstance(i, str) for i in spec):
        return True
    return False


def _path_to_segments(path: str) -> Tuple[Any, ...]:
    """The path's segments without the root and without the access action.

    ``root['a']`` and ``root.a`` give the same segments, the same way
    ``GlobPathMatcher`` compares paths.
    """
    return tuple(elem for elem, _ in _path_to_elements(path, root_element=_ROOT_ELEMENT)[1:])


class CompiledMatchKeys:
    """``iterable_match_keys`` compiled for fast lookup by list path."""

    def __init__(self, iterable_match_keys: Any) -> None:
        if not isinstance(iterable_match_keys, Mapping):
            raise ValueError(
                "iterable_match_keys must be a mapping of path to key spec. Got: {!r}".format(iterable_match_keys))
        self.exact: Dict[Tuple[Any, ...], MatchKeySpec] = {}
        self.globs: List[Tuple[GlobPathMatcher, MatchKeySpec]] = []
        for path, spec in iterable_match_keys.items():
            if not isinstance(path, str):
                raise ValueError("iterable_match_keys paths must be strings. Got: {!r}".format(path))
            spec = _validate_key_spec(path, spec)
            if path_has_wildcard(path):
                if not path.startswith('root'):
                    raise ValueError("Wildcard paths must start with 'root'. Got: {}".format(path))
                self.globs.append((GlobPathMatcher(path), spec))
                continue
            for variant in add_root_to_paths([path]):  # type: ignore
                segments = _path_to_segments(variant)
                existing = self.exact.get(segments, NO_KEY)
                if existing is not NO_KEY and existing != spec:
                    raise ValueError(
                        "iterable_match_keys has more than one key spec for the path {!r}.".format(path))
                self.exact[segments] = spec

    def get_spec(self, path: Optional[str]) -> Optional[MatchKeySpec]:
        """Return the key spec for the list at ``path``, or None if no entry selects it.

        An exact path takes precedence over globs. If more than one glob matches
        and there is no exact path, raise ValueError rather than guess.
        """
        if not path:
            return None
        if self.exact:
            try:
                segments = _path_to_segments(path)
            except Exception:  # pragma: no cover. Paths DeepDiff builds are parseable.
                segments = None
            if segments is not None and segments in self.exact:
                return self.exact[segments]
        matched = [(matcher, spec) for matcher, spec in self.globs if matcher.match(path)]
        if not matched:
            return None
        if len(matched) > 1:
            raise ValueError(
                "More than one iterable_match_keys glob matches the path {}: {}. "
                "Add an exact path for it or make the globs more specific.".format(
                    path, ', '.join(repr(matcher.original_pattern) for matcher, _ in matched)))
        return matched[0][1]


def _get_secondary_value(item: Any, spec: KeySpec) -> Any:
    """The normalized secondary value of ``item``, or ``NO_KEY`` if absent."""
    return get_item_key(item, spec)


def _secondary_distance(value1: Any, value2: Any) -> Any:
    """A sortable distance between two secondary values.

    Equal values are ``0``. Numbers compare by absolute difference, so a line
    number or timestamp that moved pairs with its nearest counterpart.
    Everything else falls back to equality, expressed as 0 or 1.
    """
    if value1 == value2:
        return 0
    if isinstance(value1, booleans) or isinstance(value2, booleans):
        return 1
    if not (isinstance(value1, numbers) and isinstance(value2, numbers)):
        return 1
    try:
        return abs(value1 - value2)
    except (TypeError, ValueError, ArithmeticError):
        return 1


def _secondary_distance_has_order(value1: Any, value2: Any) -> bool:
    """Whether two secondary values support a meaningful "closest" comparison."""
    if isinstance(value1, booleans) or isinstance(value2, booleans):
        return False
    return isinstance(value1, numbers) and isinstance(value2, numbers)


def _normalize_key(value: Any) -> Any:
    """Normalize a key so that int and float ids match but booleans never match numbers.

    Python already treats ``1 == 1.0`` with equal hashes and ``"1" != 1``.
    Only booleans need special handling since ``True == 1``.
    """
    if isinstance(value, booleans):
        return (BOOL_TAG, bool(value))
    if isinstance(value, tuple):
        return tuple(_normalize_key(i) for i in value)
    return value


def get_item_key(item: Any, spec: KeySpec) -> Any:
    """Return the normalized key of ``item``, or ``NO_KEY`` if it has none."""
    try:
        if isinstance(spec, str):
            key = item[spec]
        elif isinstance(spec, tuple):
            key = tuple(item[i] for i in spec)
        else:
            key = spec(item)
        key = _normalize_key(key)
        hash(key)
    except _NO_KEY_EXCEPTIONS:
        return NO_KEY
    return key


def get_key_matching_pairs(
    t1: Any, t2: Any, spec: MatchKeySpec,
) -> List[Tuple[Tuple[int, int], Tuple[Any, Any]]]:
    """Pair the items of t1 and t2 by key in O(n + m).

    - The k-th occurrence of a key in t1 pairs with the k-th occurrence in t2.
      Extra occurrences are reported as removed or added.
    - With a ``MatchKey``, occurrences of a duplicate primary key are paired by
      their secondary key first: identical secondary values pair up, then the
      closest remaining values. Occurrence order breaks exact ties, and items
      without a usable secondary value fall back to occurrence order.
    - Items without a key are paired in order among themselves.

    Returns the same structure as ``DeepDiff._get_matching_pairs``.
    """
    t1_items = list(t1)
    t2_items = list(t2)
    if isinstance(spec, MatchKey):
        return _get_key_and_secondary_matching_pairs(t1_items, t2_items, spec)

    t2_index: Dict[Any, deque] = {}
    t2_no_key: List[int] = []
    for j, y in enumerate(t2_items):
        key = get_item_key(y, spec)
        if key is NO_KEY:
            t2_no_key.append(j)
        else:
            t2_index.setdefault(key, deque()).append(j)

    matches: List[Tuple[Tuple[int, int], Tuple[Any, Any]]] = []
    t2_matched = set()
    no_key_position = 0
    for i, x in enumerate(t1_items):
        key = get_item_key(x, spec)
        j = None
        if key is NO_KEY:
            if no_key_position < len(t2_no_key):
                j = t2_no_key[no_key_position]
                no_key_position += 1
        else:
            candidates = t2_index.get(key)
            if candidates:
                j = candidates.popleft()
        if j is None:
            matches.append(((i, -1), (x, ListItemRemovedOrAdded)))
        else:
            t2_matched.add(j)
            matches.append(((i, j), (x, t2_items[j])))
    for j, y in enumerate(t2_items):
        if j not in t2_matched:
            matches.append(((-1, j), (ListItemRemovedOrAdded, y)))
    return matches


def _get_key_and_secondary_matching_pairs(
    t1_items: List[Any], t2_items: List[Any], spec: MatchKey,
) -> List[Tuple[Tuple[int, int], Tuple[Any, Any]]]:
    """Pair by primary key and disambiguate duplicates by the secondary key."""
    t2_index: Dict[Any, List[int]] = {}
    t2_no_key: List[int] = []
    for j, y in enumerate(t2_items):
        key = get_item_key(y, spec.key)
        if key is NO_KEY:
            t2_no_key.append(j)
        else:
            t2_index.setdefault(key, []).append(j)

    matches: List[Tuple[Tuple[int, int], Tuple[Any, Any]]] = []
    t2_matched = set()
    no_key_position = 0
    groups: Dict[Any, List[int]] = {}
    for i, x in enumerate(t1_items):
        key = get_item_key(x, spec.key)
        if key is NO_KEY:
            if no_key_position < len(t2_no_key):
                j = t2_no_key[no_key_position]
                no_key_position += 1
                t2_matched.add(j)
                matches.append(((i, j), (x, t2_items[j])))
            else:
                matches.append(((i, -1), (x, ListItemRemovedOrAdded)))
        else:
            groups.setdefault(key, []).append(i)

    for key, t1_indices in groups.items():
        candidates = t2_index.get(key, [])
        for i, j in _match_secondary_group(t1_items, t2_items, t1_indices, candidates, spec.secondary):
            if j is None:
                matches.append(((i, -1), (t1_items[i], ListItemRemovedOrAdded)))
            else:
                t2_matched.add(j)
                matches.append(((i, j), (t1_items[i], t2_items[j])))

    for j, y in enumerate(t2_items):
        if j not in t2_matched:
            matches.append(((-1, j), (ListItemRemovedOrAdded, y)))
    matches.sort(key=lambda match: (
        match[0][0] if match[0][0] != -1 else len(t1_items),
        match[0][1] if match[0][1] != -1 else 0,
    ))
    return matches


def _match_secondary_group(
    t1_items: List[Any], t2_items: List[Any], t1_indices: List[int], t2_indices: List[int],
    spec: KeySpec,
) -> List[Tuple[int, Optional[int]]]:
    """Match one primary-key group by secondary value.

    Exact secondary matches are reserved first across the whole group, then
    whatever remains on both sides is paired by closest secondary value. This
    is what keeps an extra or missing duplicate from stealing the exact match
    of its sibling.
    """
    unmatched_t2 = set(t2_indices)
    matched: Dict[int, Optional[int]] = {}

    t2_by_value: Dict[Any, deque] = {}
    for j in t2_indices:
        value = _get_secondary_value(t2_items[j], spec)
        if value is not NO_KEY:
            t2_by_value.setdefault(value, deque()).append(j)

    for i in t1_indices:
        value = _get_secondary_value(t1_items[i], spec)
        candidates = t2_by_value.get(value) if value is not NO_KEY else None
        if candidates:
            j = candidates.popleft()
            unmatched_t2.discard(j)
            matched[i] = j

    # Pair leftovers by closest secondary value globally, so a far row cannot
    # claim a counterpart that is a much better match for another row.
    remaining_t2 = sorted(unmatched_t2)
    t2_values = {j: _get_secondary_value(t2_items[j], spec) for j in remaining_t2}
    remaining_t1 = [i for i in t1_indices if i not in matched]
    if remaining_t1 and remaining_t2:
        pairs = []
        for i in remaining_t1:
            value = _get_secondary_value(t1_items[i], spec)
            for j in remaining_t2:
                other_value = t2_values.get(j, NO_KEY)
                if value is NO_KEY or other_value is NO_KEY:
                    # Both sides lack a secondary value: pair in order.
                    distance = 1 if value is NO_KEY and other_value is NO_KEY else None
                else:
                    distance = _secondary_distance(value, other_value)
                    if distance and not _secondary_distance_has_order(value, other_value):
                        # Names and other opaque values have no "closest";
                        # only exact matches pair. The rest are add/remove.
                        distance = None
                if distance is not None:
                    pairs.append((distance, i, j))
        used_t1 = set()
        used_t2 = set()
        for distance, i, j in sorted(pairs, key=lambda pair: (pair[0], pair[1], pair[2])):
            if i in used_t1 or j in used_t2:
                continue
            used_t1.add(i)
            used_t2.add(j)
            matched[i] = j
    for i in remaining_t1:
        matched.setdefault(i, None)
    return list(matched.items())

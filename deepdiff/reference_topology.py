"""
Reference topology analysis.

This module implements the optional ``report_reference_topology`` report of
DeepDiff. It answers two questions that a value based comparison cannot:

1. Shared references that got split: the same object is referenced from two
   or more paths in t1, but the corresponding paths in t2 hold distinct
   objects (equal by value perhaps, but no longer the same object).
2. Reference cycles: objects that (transitively) reference themselves. The
   traversal is guaranteed to terminate on such structures and every cycle
   is reported with the path where it was detected and the ancestor path it
   points back to.
"""
import types
from collections.abc import Mapping

from deepdiff.path import stringify_element

__all__ = ['analyze_reference_topology']

SHARED_REFERENCE_SPLITS_KEY = 'shared_reference_splits'
REFERENCE_CYCLES_KEY = 'reference_cycles'

# Accessor kinds used to build and resolve paths.
_KEY = 'key'
_INDEX = 'index'
_ATTR = 'attr'
_VALUE = 'value'

# Objects of these types are never descended into even though they may
# expose a __dict__. Walking them would be useless and can explode into
# huge object graphs (think of a module's namespace).
_NEVER_DESCEND_TYPES = (
    type,
    types.ModuleType,
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    property,
    staticmethod,
    classmethod,
)


def _iter_children(obj):
    """
    Yield (accessor, child) pairs for the containers and objects we track.
    An accessor is a (kind, param) tuple that knows how to locate the child
    within its parent.
    """
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            yield (_KEY, key), value
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            yield (_INDEX, index), value
    elif isinstance(obj, (set, frozenset)):
        for value in obj:
            yield (_VALUE, value), value
    elif isinstance(obj, _NEVER_DESCEND_TYPES):
        return
    elif hasattr(obj, '__dict__'):
        for key, value in vars(obj).items():
            yield (_ATTR, key), value
    else:
        slots = getattr(type(obj), '__slots__', None)
        if slots:
            if isinstance(slots, str):
                slots = (slots,)
            for key in slots:
                if key in ('__dict__', '__weakref__'):
                    continue
                try:
                    value = getattr(obj, key)
                except AttributeError:
                    continue
                yield (_ATTR, key), value


def _is_trackable(obj):
    """
    Only containers and custom objects carry a meaningful identity for the
    topology report. Immutables such as strings and numbers are interned
    and shared by the interpreter itself, so tracking them would only
    produce noise.
    """
    if isinstance(obj, (Mapping, list, tuple, set, frozenset)):
        return True
    if isinstance(obj, _NEVER_DESCEND_TYPES):
        return False
    return hasattr(obj, '__dict__') or hasattr(type(obj), '__slots__')


def _format_step(path, accessor):
    kind, param = accessor
    if kind == _KEY:
        if isinstance(param, str):
            param = stringify_element(param, quote_str="'{}'")
        return "{}[{}]".format(path, param)
    elif kind == _INDEX:
        return "{}[{}]".format(path, param)
    elif kind == _ATTR:
        return "{}.{}".format(path, param)
    else:  # _VALUE: set members have no addressable path.
        return "{}[{}]".format(path, stringify_element(repr(param)))


def _resolve(root, accessors):
    """
    Follow a chain of accessors starting at root.
    Returns the object found there or None when the path does not exist.
    """
    obj = root
    for kind, param in accessors:
        try:
            if kind == _KEY:
                if not isinstance(obj, Mapping):
                    return None
                obj = obj[param]
            elif kind == _INDEX:
                if not isinstance(obj, (list, tuple)):
                    return None
                obj = obj[param]
            elif kind == _ATTR:
                obj = getattr(obj, param)
            else:  # _VALUE can not be resolved reliably.
                return None
        except (KeyError, IndexError, TypeError, AttributeError):
            return None
    return obj


def _walk(obj, path, accessors, ancestors, id_to_paths, cycles, expanded, tree_label):
    """
    Depth first traversal that records every path an object is referenced
    from. Cycles are detected via the ancestors chain and reported instead
    of being followed, which guarantees termination. Shared objects that
    are not part of a cycle are expanded only once so DAG shaped data does
    not cause exponential blowup.
    """
    if not _is_trackable(obj):
        return
    obj_id = id(obj)
    id_to_paths.setdefault(obj_id, []).append((path, accessors))
    if obj_id in ancestors:
        cycles.append({
            'tree': tree_label,
            'path': path,
            'target': ancestors[obj_id],
        })
        return
    if obj_id in expanded:
        return
    expanded.add(obj_id)
    ancestors[obj_id] = path
    for accessor, child in _iter_children(obj):
        _walk(
            child,
            _format_step(path, accessor),
            accessors + (accessor,),
            ancestors,
            id_to_paths,
            cycles,
            expanded,
            tree_label,
        )
    del ancestors[obj_id]


def _is_descendant_of(path, ancestor):
    """Whether path lies strictly below ancestor in the object tree."""
    return path.startswith(ancestor) and path[len(ancestor):len(ancestor) + 1] in {'[', '.'}


def _drop_splits_implied_by_parents(splits):
    """
    When a shared object is split, every object inside it is split along
    with it. Reporting every nested pair again would only restate the
    outer split, so keep the outermost groups only.
    """
    splits = sorted(splits, key=lambda entry: min(len(path) for path in entry['paths']))
    kept = []
    for entry in splits:
        implied = any(
            all(_is_descendant_of(path, parent_path) for path in entry['paths'])
            for parent_entry in kept
            for parent_path in parent_entry['paths']
        )
        if not implied:
            kept.append(entry)
    return kept


def _find_shared_reference_splits(t1, t2, t1_id_to_paths):
    """
    For every object that is referenced from multiple paths in t1, check
    whether the matching paths in t2 still point to one and the same
    object. If they do not, the previously shared reference was split into
    separate copies and is reported.
    """
    splits = []
    for paths_and_accessors in t1_id_to_paths.values():
        if len(paths_and_accessors) < 2:
            continue
        t2_objects = []
        for _path, accessors in paths_and_accessors:
            t2_obj = _resolve(t2, accessors)
            if t2_obj is None:
                # The path does not exist in t2 anymore. The regular diff
                # already reports the structural change; there is no
                # meaningful topology statement to make here.
                t2_objects = None
                break
            t2_objects.append(t2_obj)
        if not t2_objects:
            continue
        first = t2_objects[0]
        if any(obj is not first for obj in t2_objects[1:]):
            splits.append({'paths': [path for path, _accessors in paths_and_accessors]})
    return _drop_splits_implied_by_parents(splits)


def analyze_reference_topology(t1, t2):
    """
    Compare the reference topology of t1 and t2.

    Returns a dictionary with two keys:

    - ``shared_reference_splits``: a list of ``{'paths': [...]}`` entries.
      Each entry lists the paths that referenced a single shared object in
      t1 while the corresponding paths in t2 reference distinct objects.
    - ``reference_cycles``: a list of ``{'tree', 'path', 'target'}``
      entries. Each entry means the object found at ``path`` is identical
      to its ancestor at ``target``, i.e. the structure references itself.
    """
    t1_id_to_paths = {}
    cycles = []
    _walk(t1, 'root', (), {}, t1_id_to_paths, cycles, set(), 't1')
    _walk(t2, 'root', (), {}, {}, cycles, set(), 't2')
    return {
        SHARED_REFERENCE_SPLITS_KEY: _find_shared_reference_splits(t1, t2, t1_id_to_paths),
        REFERENCE_CYCLES_KEY: cycles,
    }

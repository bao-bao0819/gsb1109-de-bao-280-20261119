"""Identify the importer to use for a document.

The identification process can produce a conflict when multiple importers
recognize the same document.  Conflicts are reported with the explanation
provided by each matching importer and can be resolved by configuring a
selection rule for the document source.
"""

__copyright__ = "Copyright (C) 2016  Martin Blais"
__license__ = "GNU GPLv2"

import fnmatch
import inspect
import os
import re
from collections.abc import Callable as AbcCallable
from collections.abc import Iterable as AbcIterable
from collections.abc import Mapping as AbcMapping
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from typing import Sequence, Tuple

from beangulp.exceptions import Error
from beangulp.importer import Importer


__all__ = [
    "Conflict",
    "Identification",
    "IdentificationError",
    "IdentifyError",
    "Match",
    "SelectionRule",
    "combine_rules",
    "explain",
    "identify",
    "identify_all",
    "identify_with_conflicts",
    "match",
    "normalize_rules",
    "resolve",
]


# A file size beyond which we will simply ignore the file. This is
# used to skip large files that are commonly present in a Downloads
# directory.
FILE_TOO_LARGE_THRESHOLD = 8 * 1024 * 1024

# Methods that importers can implement to explain why they identified a
# document.  They are tried in this order.
_EXPLANATION_METHODS = (
    "explain_identification",
    "explain",
    "identification_explanation",
)

# Attributes that can provide free-form matching information.
_EXPLANATION_ATTRIBUTES = (
    "identification_explanation",
    "identification_reason",
    "explanation",
    "reasons",
)

# Attributes that can identify the source (bank, institution, ...) an
# importer is configured for.
_SOURCE_METHODS = ("source", "get_source", "identify_source")
_SOURCE_ATTRIBUTES = (
    "source",
    "sources",
    "source_names",
    "source_id",
    "institution",
    "bank",
)


def _as_texts(value: Any) -> Tuple[str, ...]:
    """Normalize a value into a tuple of non-empty strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    elif isinstance(value, AbcMapping):
        value = value.values()
    try:
        values = list(value)
    except TypeError:
        values = [value]
    texts = []
    for item in values:
        if item is None:
            continue
        if isinstance(item, str):
            text = item
        elif isinstance(item, Mapping):
            text = "; ".join(_as_texts(item))
        else:
            for attribute in ("reason", "message", "explanation", "text"):
                if hasattr(item, attribute):
                    text = str(getattr(item, attribute))
                    break
            else:
                text = str(item)
        text = text.strip()
        if text:
            texts.append(text)
    return tuple(texts)


def _call_optional(func: AbcCallable, *args):
    """Call an optional hook, allowing it to take fewer arguments."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind == parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    return func(*args[: len(positional)]) if not has_varargs else func(*args)


def _explanation_from_result(result: Any) -> Tuple[str, ...]:
    """Extract an explanation from the value returned by identify()."""
    if result is True or result is False or result is None:
        return ()
    if isinstance(result, Match):
        return result.explanation
    if isinstance(result, str):
        return _as_texts(result)
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        # Identify methods may return (matched, explanation).
        return _as_texts(result[1]) if result[0] else ()
    if isinstance(result, Mapping):
        for key in ("explanation", "explanations", "reason", "reasons", "detail"):
            if key in result:
                return _as_texts(result[key])
        return ()
    for attribute in ("explanation", "explanations", "reason", "reasons"):
        if hasattr(result, attribute):
            return _as_texts(getattr(result, attribute))
    return ()


def _result_matched(result: Any) -> bool:
    """Interpret the value returned by identify() as a boolean match."""
    if isinstance(result, Match):
        return True
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        return result[0]
    if isinstance(result, Mapping):
        for key in ("matched", "match", "identified", "identify"):
            if key in result:
                return bool(result[key])
    for attribute in ("matched", "is_match", "identified"):
        if hasattr(result, attribute):
            return bool(getattr(result, attribute))
    return bool(result)


def _importer_explanation(importer: Importer, filepath: str) -> Tuple[str, ...]:
    """Get the explanation provided by an importer."""
    for method_name in _EXPLANATION_METHODS:
        method = getattr(importer, method_name, None)
        if callable(method):
            explanation = _as_texts(_call_optional(method, filepath))
            if explanation:
                return explanation
    for attribute in _EXPLANATION_ATTRIBUTES:
        value = getattr(importer, attribute, None)
        if value is None:
            continue
        if callable(value):
            value = _call_optional(value, filepath)
        explanation = _as_texts(value)
        if explanation:
            return explanation
    return ()


def _importer_sources(importer: Importer, filepath: str) -> Tuple[str, ...]:
    """Get source identifiers declared by an importer."""
    sources: List[str] = []
    for method_name in _SOURCE_METHODS:
        method = getattr(importer, method_name, None)
        if callable(method):
            sources.extend(_as_texts(_call_optional(method, filepath)))
    for attribute in _SOURCE_ATTRIBUTES:
        value = getattr(importer, attribute, None)
        if value is None or callable(value):
            continue
        sources.extend(_as_texts(value))
    return tuple(dict.fromkeys(sources))


def _source_keys(filepath: str, match: "Match") -> Tuple[str, ...]:
    """Build the strings a selection rule source can be matched against."""
    basename = os.path.basename(filepath)
    stem, _ = os.path.splitext(basename)
    keys = [
        filepath,
        os.path.abspath(filepath),
        basename,
        stem,
        match.name,
        *match.source,
    ]
    # Keep insertion order while removing duplicates.
    return tuple(dict.fromkeys(key for key in keys if key))


@dataclass(frozen=True)
class Match:
    """A single importer match.

    Attributes:
      importer: The importer that identified the document.
      explanation: Human readable reasons provided by the importer.
      source: Optional source identifiers provided by the importer.
    """

    importer: Importer
    explanation: Tuple[str, ...] = ()
    source: Tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.importer.name

    @property
    def reason(self) -> Optional[str]:
        """The explanation as a single string, if one was provided."""
        return "; ".join(self.explanation) if self.explanation else None

    @property
    def reasons(self) -> Tuple[str, ...]:
        return self.explanation

    def describe(self) -> str:
        """Return the importer name and its explanation."""
        if not self.explanation:
            return self.name
        return f"{self.name}: {'; '.join(self.explanation)}"

    def asdict(self) -> Dict[str, Any]:
        return {
            "importer": self.name,
            "explanation": list(self.explanation),
            "source": list(self.source),
        }


def _match_importer(importer: Importer, filepath: str) -> Optional[Match]:
    """Run an importer's identify() method and normalize the result."""
    result = importer.identify(filepath)
    if not _result_matched(result):
        return None
    explanation = _explanation_from_result(result)
    if not explanation:
        explanation = _importer_explanation(importer, filepath)
    source = ()
    if isinstance(result, Match):
        source = result.source
    elif isinstance(result, Mapping):
        source = _as_texts(result.get("source") or result.get("sources"))
    source = tuple(dict.fromkeys((*source, *_importer_sources(importer, filepath))))
    return Match(importer, explanation, source)


def _stable_match_key(match: Match):
    """Sort key that does not depend on the order of the importer list."""
    importer_type = type(match.importer)
    return (
        str(match.name or ""),
        "\n".join(match.explanation),
        "\n".join(match.source),
        importer_type.__module__,
        importer_type.__qualname__,
    )


def match(importers: Sequence[Importer], filepath: str) -> List[Match]:
    """Return all importers matching a document.

    The returned list is sorted by importer name, and then by explanation,
    so that its order does not depend on the order in which the importers
    were supplied.

    Args:
      importers: List of importer instances.
      filepath: Filesystem path to the document.

    Returns:
      A list of Match instances.
    """
    matches = []
    for importer in importers:
        matched = _match_importer(importer, filepath)
        if matched is not None:
            matches.append(matched)
    matches.sort(key=_stable_match_key)
    return matches


def _normalize_preferences(preference: Any) -> Tuple[str, ...]:
    """Normalize the preferred importer names of a selection rule."""
    if preference is None:
        return ()
    if isinstance(preference, str):
        return (preference,)
    if isinstance(preference, AbcMapping):
        # Accept {"importer": "A"} and {"importers": ["A", "B"]}.
        for key in ("importers", "preferences", "order", "names", "importer"):
            if key in preference:
                return _normalize_preferences(preference[key])
        # Accept a mapping of importer name to priority. Lower values
        # have precedence, and names break ties deterministically.
        try:
            items = sorted(
                preference.items(),
                key=lambda item: (item[1], str(item[0])),
            )
        except TypeError:
            items = sorted(preference.items(), key=lambda item: str(item[0]))
        return tuple(str(name) for name, _ in items)
    if isinstance(preference, (set, frozenset)):
        return tuple(sorted(str(name) for name in preference))
    if isinstance(preference, AbcIterable):
        return tuple(str(name) for name in preference if name)
    return (str(preference),)


@dataclass(frozen=True)
class SelectionRule:
    """A selection rule for documents from a particular source.

    Args:
      source: The source to which the rule applies.  It can be a string
        (matched as a glob against the document path, basename, stem,
        importer name, and importer source), a compiled regular expression,
        a callable, or None to apply the rule to every document.
      importers: Importer names in order of preference.  The first name
        present in the set of matching importers is selected.
      name: An optional name used when reporting the rule.
      selector: An optional callable that receives the sequence of Match
        objects and returns a Match, an importer, or an importer name.
        The selector is only consulted when this rule applies.
    """

    source: Any = None
    importers: Tuple[str, ...] = ()
    name: Optional[str] = None
    selector: Optional[AbcCallable] = None

    def __post_init__(self):
        object.__setattr__(self, "importers", _normalize_preferences(self.importers))

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        if self.source is None:
            return "default"
        if isinstance(self.source, str):
            return self.source
        if hasattr(self.source, "pattern"):
            return str(self.source.pattern)
        return getattr(self.source, "__name__", str(self.source))

    def matches(self, filepath: str, match: Match) -> bool:
        """Return True if the rule applies to the given match."""
        if self.source is None:
            return True
        keys = _source_keys(filepath, match)
        if callable(self.source):
            return bool(_call_optional(self.source, filepath, match))
        if isinstance(self.source, re.Pattern):
            return any(self.source.search(key) for key in keys)
        source = str(self.source)
        if source in ("*", "default"):
            return True
        lowered = source.casefold()
        for key in keys:
            if fnmatch.fnmatchcase(key, source):
                return True
            if key.casefold() == lowered:
                return True
            if lowered in key.casefold():
                return True
        return False

    def select(self, matches: Sequence[Match]) -> Optional[Match]:
        """Select the preferred importer among the matching importers."""
        if self.selector is not None:
            choice = _call_optional(self.selector, tuple(matches))
            return _as_match_choice(choice, matches)
        by_name: Dict[str, List[Match]] = {}
        for item in matches:
            by_name.setdefault(item.name, []).append(item)
        for name in self.importers:
            candidates = by_name.get(name)
            if candidates:
                return sorted(candidates, key=_stable_match_key)[0]
        return None

    def asdict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "source": self.label,
            "importers": list(self.importers),
        }


def _as_match_choice(choice: Any, matches: Sequence[Match]) -> Optional[Match]:
    """Normalize the value returned by a selector into a Match."""
    if choice is None:
        return None
    if isinstance(choice, Match):
        return choice
    if isinstance(choice, str):
        for item in matches:
            if item.name == choice:
                return item
        return None
    for item in matches:
        if choice is item.importer:
            return item
    if hasattr(choice, "name") and not isinstance(choice, type):
        name = choice.name
        if isinstance(name, str):
            for item in matches:
                if item.name == name:
                    return item
    return None


def _as_selection_rule(source: Any, preference: Any) -> SelectionRule:
    """Build a SelectionRule from a source and a preference value."""
    if isinstance(preference, SelectionRule):
        if preference.source is None:
            return SelectionRule(
                source,
                preference.importers,
                preference.name,
                preference.selector,
            )
        return preference
    if isinstance(preference, AbcMapping):
        name = preference.get("name")
        selector = preference.get("selector") or preference.get("choose")
        importers = None
        for key in ("importers", "preferences", "order", "names", "importer"):
            if key in preference:
                importers = preference[key]
                break
        if importers is None and selector is None:
            # A mapping of importer name to priority.  Rule metadata keys
            # are not importer names.
            importers = {
                key: value
                for key, value in preference.items()
                if key not in ("name", "selector", "choose")
            } or None
        return SelectionRule(
            source, _normalize_preferences(importers), name, selector
        )
    return SelectionRule(source, _normalize_preferences(preference))


def _normalize_rules(value: Any) -> List[SelectionRule]:
    """Normalize user supplied selection rules into a list."""
    if value is None:
        return []
    if isinstance(value, SelectionRule):
        return [value]
    if isinstance(value, AbcCallable) and not isinstance(value, type):
        # A bare callable is used as a selector for all documents.
        return [SelectionRule(selector=value)]
    if isinstance(value, AbcMapping):
        if any(
            key in value
            for key in ("source", "importers", "preferences", "order", "selector",
                        "choose")
        ):
            # A single rule expressed as a mapping.
            source = value.get("source")
            preference = {
                key: value[key]
                for key in ("importers", "preferences", "order", "names", "importer")
                if key in value
            }
            if "name" in value:
                preference["name"] = value["name"]
            if "selector" in value:
                preference["selector"] = value["selector"]
            elif "choose" in value:
                preference["selector"] = value["choose"]
            return [_as_selection_rule(source, preference)]
        rules = []
        for source, preference in value.items():
            rules.append(_as_selection_rule(source, preference))
        return rules
    if isinstance(value, (set, frozenset)):
        value = sorted(value, key=repr)
    if isinstance(value, AbcIterable):
        rules = []
        for item in value:
            if isinstance(item, SelectionRule):
                rules.append(item)
            elif isinstance(item, Mapping):
                source = item.get("source")
                preference = {
                    key: item[key]
                    for key in ("importers", "preferences", "order", "names", "importer")
                    if key in item
                }
                if "name" in item:
                    preference["name"] = item["name"]
                if "selector" in item:
                    preference["selector"] = item["selector"]
                elif "choose" in item:
                    preference["selector"] = item["choose"]
                if not preference:
                    if len(item) == 1:
                        source, preference = next(iter(item.items()))
                    else:
                        raise ValueError(f"Invalid selection rule: {item!r}")
                rules.append(_as_selection_rule(source, preference))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                rules.append(_as_selection_rule(item[0], item[1]))
            elif isinstance(item, AbcCallable) and not isinstance(item, type):
                rules.append(SelectionRule(selector=item))
            else:
                raise ValueError(f"Invalid selection rule: {item!r}")
        return rules
    raise ValueError(f"Invalid selection rules: {value!r}")


def _combined_rules(
    rules: Any = None, selection: Any = None, selectors: Any = None
) -> List[SelectionRule]:
    """Combine the accepted spellings of the selection rules option."""
    combined = []
    for value in (rules, selection, selectors):
        combined.extend(_normalize_rules(value))
    return combined


def _apply_selection(
    matches: Sequence[Match], filepath: str, rules: Sequence[SelectionRule]
) -> Tuple[Optional[Match], Optional[SelectionRule]]:
    """Apply selection rules in order and return the selected match."""
    for rule in rules:
        if any(rule.matches(filepath, item) for item in matches):
            selected = rule.select(matches)
            if selected is not None:
                return selected, rule
    return None, None


@dataclass(frozen=True)
class Conflict:
    """An auditable identification conflict.

    Attributes:
      filepath: The document that produced the conflict.
      matches: All matching importers, sorted deterministically.
      selected: The match selected by a rule, if the conflict was resolved.
      rule: The rule that selected the match, if any.
    """

    filepath: str
    matches: Tuple[Match, ...]
    selected: Optional[Match] = None
    rule: Optional[SelectionRule] = None

    @property
    def resolved(self) -> bool:
        return self.selected is not None

    @property
    def candidates(self) -> Tuple[str, ...]:
        return tuple(item.name for item in self.matches)

    @property
    def names(self) -> Tuple[str, ...]:
        return self.candidates

    @property
    def explanations(self) -> Dict[str, str]:
        return {
            item.name: "; ".join(item.explanation)
            for item in self.matches
        }

    @property
    def importer(self) -> Optional[Importer]:
        return self.selected.importer if self.selected else None

    def lines(self) -> List[str]:
        """Return one stable line per matching importer."""
        lines = []
        for item in self.matches:
            lines.append(f"  {item.describe()}")
        return lines

    def report(self) -> str:
        """Return a human readable conflict report."""
        lines = ["Document identified by more than one importer.", *self.lines()]
        if self.selected is not None and self.rule is not None:
            lines.append(
                f"Selected {self.selected.name} using selection rule "
                f"{self.rule.label!r}."
            )
        return "\n".join(lines)

    def asdict(self) -> Dict[str, Any]:
        return {
            "filepath": self.filepath,
            "matches": [item.asdict() for item in self.matches],
            "selected": self.selected.name if self.selected else None,
            "rule": self.rule.asdict() if self.rule else None,
            "resolved": self.resolved,
            "report": self.report(),
        }

    def __str__(self) -> str:
        return self.report()


@dataclass(frozen=True)
class Identification:
    """The complete result of identifying a document.

    This is returned by resolve() and can be used by callers that need the
    conflict details rather than only the selected importer.
    """

    filepath: str
    matches: Tuple[Match, ...] = ()
    selected: Optional[Match] = None
    rule: Optional[SelectionRule] = None

    @property
    def importer(self) -> Optional[Importer]:
        return self.selected.importer if self.selected else None

    @property
    def selected_importer(self) -> Optional[Importer]:
        return self.importer

    @property
    def winner(self) -> Optional[Importer]:
        return self.importer

    @property
    def conflict(self) -> Optional[Conflict]:
        if len(self.matches) <= 1:
            return None
        return Conflict(self.filepath, self.matches, self.selected, self.rule)

    @property
    def conflicts(self) -> Tuple[Conflict, ...]:
        conflict = self.conflict
        return (conflict,) if conflict else ()

    @property
    def has_conflict(self) -> bool:
        return self.conflict is not None

    @property
    def is_conflict(self) -> bool:
        return self.has_conflict

    def report(self) -> str:
        conflict = self.conflict
        if conflict is None:
            if not self.matches:
                return ""
            return f"{self.matches[0].name} identified {self.filepath}."
        return conflict.report()

    def asdict(self) -> Dict[str, Any]:
        return {
            "filepath": self.filepath,
            "matches": [item.asdict() for item in self.matches],
            "selected": self.selected.name if self.selected else None,
            "rule": self.rule.asdict() if self.rule else None,
            "conflict": self.conflict.asdict() if self.conflict else None,
        }

    def __bool__(self) -> bool:
        return self.selected is not None


class IdentificationError(Error):
    """Raised when a document matches more than one importer.

    The exception carries the auditable Conflict object, which includes
    the explanation provided by every matching importer.
    """

    def __init__(self, conflict: Conflict):
        self.conflict = conflict
        self.filepath = conflict.filepath
        self.matches = conflict.matches
        self.selected = conflict.selected
        self.rule = conflict.rule
        self.result = Identification(
            conflict.filepath, conflict.matches, conflict.selected, conflict.rule
        )
        super().__init__(
            "Document identified by more than one importer.", *conflict.lines()
        )

    @property
    def report(self) -> str:
        return self.conflict.report()


# Common spelling used by callers that want the importer or an error.
IdentifyError = IdentificationError


def normalize_rules(value: Any) -> List[SelectionRule]:
    """Normalize selection rules supplied in any accepted form.

    Accepted forms are a SelectionRule, a mapping of source to preference,
    a sequence of rules, or a callable selector.

    Args:
      value: The selection rules to normalize.

    Returns:
      A list of SelectionRule instances.
    """
    return _normalize_rules(value)


def combine_rules(
    rules: Any = None, selection: Any = None, selectors: Any = None
) -> List[SelectionRule]:
    """Combine the accepted spellings of the selection rules option."""
    return _combined_rules(rules, selection, selectors)


def resolve(
    importers: Sequence[Importer],
    filepath: str,
    rules: Any = None,
    selection: Any = None,
    selectors: Any = None,
) -> Identification:
    """Identify a document and return the complete auditable result.

    Unlike identify(), this function never raises on a conflict.  The
    returned Identification exposes the matching importers, their
    explanations, and the conflict (if any).  When a selection rule
    resolves the conflict, Identification.importer is the selected importer.

    Args:
      importers: List of importer instances.
      filepath: Filesystem path to the document.
      rules: Selection rules, as a mapping, a sequence of rules, or a
        SelectionRule instance.
      selection: Alias for rules.
      selectors: Alias for rules.

    Returns:
      An Identification result.
    """
    matches = match(importers, filepath)
    selection_rules = _combined_rules(rules, selection, selectors)
    selected: Optional[Match] = None
    rule: Optional[SelectionRule] = None
    if len(matches) == 1:
        # A single match is not a conflict and does not need a rule.
        selected = matches[0]
    elif matches:
        selected, rule = _apply_selection(matches, filepath, selection_rules)
    return Identification(filepath, tuple(matches), selected, rule)


def identify(
    importers: Sequence[Importer],
    filepath: str,
    rules: Any = None,
    selection: Any = None,
    selectors: Any = None,
    *,
    return_result: bool = False,
) -> Optional[Importer]:
    """Identify the correct importer to handle a document.

    Args:
      importers: List of importer instances.
      filepath: Filesystem path to the document.
      rules: Selection rules for specific document sources.
      selection: Alias for rules.
      selectors: Alias for rules.
      return_result: If true, return the Identification result instead of
        the selected importer.

    Returns:
      The selected importer, or None if no importer matches the document.
      When return_result is true, an Identification result is returned
      instead.

    Raises:
      beangulp.identify.IdentificationError: More than one importer matched
      the file and no selection rule selected one of them.

    """
    result = resolve(importers, filepath, rules, selection, selectors)
    if return_result:
        return result
    conflict = result.conflict
    if conflict is not None and result.selected is None:
        raise IdentificationError(conflict)
    return result.importer


# Readable aliases for the audit-oriented entry point.
identify_with_conflicts = resolve
identify_all = resolve


def explain(
    importers: Sequence[Importer],
    filepath: str,
    rules: Any = None,
    selection: Any = None,
    selectors: Any = None,
) -> str:
    """Return a stable report for the identification of a document."""
    return resolve(importers, filepath, rules, selection, selectors).report()

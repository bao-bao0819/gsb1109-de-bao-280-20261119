"""
External (redacted) diff view.

This module implements a privacy-preserving report view of a DeepDiff
result. It is meant for cases where the list of changes needs to be
shared with an external party (e.g. a reconciler) who should be able to
verify how many things changed and correlate placeholders within one
report, without ever seeing the underlying sensitive values.

Sensitive values are redacted both when they appear as values and when
they appear as dictionary keys (i.e. path elements). Within one report
the same original value is always replaced by the same codename so the
external party can confirm that two entries refer to the same original
value. The codenames are salted per report, so codenames from separate
reports can not be correlated with each other.
"""
import datetime
import decimal
import fractions
import secrets
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional

from deepdiff.helper import (
    strings, notpresent, get_type, dict_, SetOrdered, NotPresent)
from deepdiff.model import (
    REPORT_KEYS, CUSTOM_FIELD, FORCE_DEFAULT, ResultDict)
from deepdiff.path import stringify_element


class ExternalReportError(Exception):
    """
    Raised when a privacy-compliant external report can not be produced.
    The error messages of this exception never contain the original data.
    """
    pass


UNREPRESENTABLE_PATH_MSG = (
    'Unable to produce a compliant external report: '
    'a change path could not be represented safely.')
UNSAFE_VALUE_MSG = (
    'Unable to produce a compliant external report: '
    'a reported value of type "{}" can not be verified to be free of sensitive data.')
UNPROCESSED_CHANGES_MSG = (
    'Unable to produce a compliant external report: '
    'unprocessed changes are present.')
EXTERNAL_VIEW_REQUIRES_VALUES_MSG = (
    'external_redact_values needs to be provided and non-empty when using the external view.')
EXTERNAL_REPORT_UNAVAILABLE_MSG = (
    'The external report is not available on this diff object.')

CODENAME_FORMAT = 'REDACTED-{}-{}'

# Types that can not contain sensitive data and are safe to pass through.
_SAFE_SCALAR_TYPES = (
    type(None),
    bool,
    int,
    float,
    complex,
    str,
    bytes,
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    decimal.Decimal,
    fractions.Fraction,
    uuid.UUID,
    range,
    type,
    NotPresent,
)

_PRETTY_TEMPLATES = {
    'type_changes': 'Type of {path} changed from {old_type} to {new_type} and value changed from {old} to {new}.',
    'values_changed': 'Value of {path} changed from {old} to {new}.',
    'dictionary_item_added': 'Item {path} added to dictionary.',
    'dictionary_item_removed': 'Item {path} removed from dictionary.',
    'iterable_item_added': 'Item {path} added to iterable.',
    'iterable_item_removed': 'Item {path} removed from iterable.',
    'iterable_item_moved': 'Item {path} moved to {new_path}.',
    'attribute_added': 'Attribute {path} added.',
    'attribute_removed': 'Attribute {path} removed.',
    'set_item_added': 'Item {path}[{item}] added to set.',
    'set_item_removed': 'Item {path}[{item}] removed from set.',
    'repetition_change': 'Repetition change for item {path}.',
}

_PRETTY_VERBOSE2_TEMPLATES = {
    'dictionary_item_added': 'Item {path} ({item}) added to dictionary.',
    'dictionary_item_removed': 'Item {path} ({item}) removed from dictionary.',
    'iterable_item_added': 'Item {path} ({item}) added to iterable.',
    'iterable_item_removed': 'Item {path} ({item}) removed from iterable.',
    'attribute_added': 'Attribute {path} ({item}) added.',
    'attribute_removed': 'Attribute {path} ({item}) removed.',
}


def _pretty_value(item: Any) -> str:
    if isinstance(item, str):
        return '"{}"'.format(item)
    return str(item)


def _disambiguate(existing: Any, path: str) -> str:
    """
    Make sure a redacted path does not collide with another entry.
    Two different original paths can look identical once the sensitive
    parts are masked. Both changes need to be kept, so a distinguishable
    suffix is appended when a collision is detected.
    """
    candidate = path
    counter = 2
    while candidate in existing:
        candidate = '{}#{}'.format(path, counter)
        counter += 1
    return candidate


class ValueRedactor:
    """
    Replaces sensitive values with stable per-report codenames.

    The same original value always maps to the same codename within one
    report. The salt is random per report so codenames of different
    reports can not be correlated.
    """

    def __init__(self, sensitive_values: Iterable[Any], salt: Optional[str] = None) -> None:
        self._sensitive_values = list(sensitive_values)
        self._salt = salt if salt is not None else secrets.token_hex(4)
        self._codenames: Dict[int, str] = {}
        # Longest first so that overlapping sensitive strings are
        # replaced by the codename of the most specific value.
        self._sensitive_strings = sorted(
            (v for v in self._sensitive_values if isinstance(v, str) and v),
            key=len, reverse=True)
        self._sensitive_bytes = sorted(
            (v for v in self._sensitive_values if isinstance(v, bytes) and v),
            key=len, reverse=True)

    def _find_index(self, value: Any) -> Optional[int]:
        for index, sensitive in enumerate(self._sensitive_values):
            if type(value) is not type(sensitive):
                continue
            try:
                if value == sensitive:
                    return index
            except Exception:
                continue
        return None

    def codename_for_index(self, index: int) -> str:
        if index not in self._codenames:
            self._codenames[index] = CODENAME_FORMAT.format(self._salt, len(self._codenames) + 1)
        return self._codenames[index]

    def redact_string(self, value: str) -> str:
        result = value
        for sensitive in self._sensitive_strings:
            if sensitive in result:
                index = self._find_index(sensitive)
                result = result.replace(sensitive, self.codename_for_index(index))  # type: ignore
        return result

    def redact_bytes(self, value: bytes) -> bytes:
        result = value
        for sensitive in self._sensitive_bytes:
            if sensitive in result:
                index = self._find_index(sensitive)
                result = result.replace(sensitive, self.codename_for_index(index).encode())  # type: ignore
        return result

    def redact_value(self, value: Any) -> Any:
        """
        Redact a reported value. Sensitive values are replaced by their
        codename, containers are redacted recursively and safe scalar
        values (e.g. public amounts) are passed through. Anything that
        can not be verified to be free of sensitive data raises
        ExternalReportError so that no non-compliant report is exported.
        """
        index = self._find_index(value)
        if index is not None:
            return self.codename_for_index(index)
        if isinstance(value, str):
            return self.redact_string(value)
        if isinstance(value, bytes):
            return self.redact_bytes(value)
        if isinstance(value, Mapping):
            return {self.redact_value(k): self.redact_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            redacted_items = [self.redact_value(item) for item in value]
            if isinstance(value, list):
                return redacted_items
            if isinstance(value, tuple):
                return tuple(redacted_items)
            if isinstance(value, frozenset):
                return frozenset(redacted_items)
            return set(redacted_items)
        if isinstance(value, _SAFE_SCALAR_TYPES):
            return value
        raise ExternalReportError(UNSAFE_VALUE_MSG.format(get_type(value).__name__))

    def redact_segment(self, rel: Any) -> str:
        """
        Redact a single path segment described by a ChildRelationship.
        """
        param = rel.param
        index = self._find_index(param)
        if index is not None:
            return '[{}]'.format(self.codename_for_index(index))
        if isinstance(param, str):
            redacted_param = self.redact_string(param)
            if redacted_param != param:
                element = stringify_element(redacted_param, quote_str=rel.quote_str)
                param_repr_format = rel.param_repr_format
                return param_repr_format.format(element) if param_repr_format else element
        segment = rel.get_param_repr(FORCE_DEFAULT)
        if segment is None:
            raise ExternalReportError(UNREPRESENTABLE_PATH_MSG)
        return segment


class ExternalReport:
    """
    A redacted view of a DeepDiff tree result that is safe to share with
    external parties. Paths, old/new values and added or removed objects
    never contain the original sensitive values, while non-sensitive
    data such as public amounts stays visible for verification.
    """

    def __init__(self,
                 tree_results: Any,
                 sensitive_values: Iterable[Any],
                 verbose_level: int = 1,
                 salt: Optional[str] = None) -> None:
        if not sensitive_values:
            raise ValueError(EXTERNAL_VIEW_REQUIRES_VALUES_MSG)
        self._redactor = ValueRedactor(sensitive_values, salt=salt)
        self.verbose_level = verbose_level
        self._report = ResultDict()
        self._pretty_lines: List[str] = []
        self.change_count = 0
        self._build(tree_results)

    @property
    def redacted(self) -> ResultDict:
        return self._report

    def to_dict(self) -> dict:
        return deepcopy(dict(self._report))

    def to_json(self, default_mapping: Optional[dict] = None, force_use_builtin_json: bool = False, **kwargs) -> str:
        from deepdiff.serialization import json_dumps
        return json_dumps(
            self.to_dict(),
            default_mapping=default_mapping,
            force_use_builtin_json=force_use_builtin_json,
            **kwargs)

    def pretty(self) -> str:
        return '\n'.join(self._pretty_lines)

    def __str__(self) -> str:
        return str(self._report)

    def _redacted_path(self, change: Any, use_t2: bool = False, reporting_move: bool = False) -> str:
        segments = []
        level = change.all_up
        while level and level is not change:
            if level.additional.get("moved") and not reporting_move:
                level_use_t2 = not use_t2
            else:
                level_use_t2 = use_t2
            if level_use_t2:
                next_rel = level.t2_child_rel or level.t1_child_rel
            else:
                next_rel = level.t1_child_rel or level.t2_child_rel
            if next_rel is None:
                break
            segments.append(self._redactor.redact_segment(next_rel))
            level = level.down
        return 'root' + ''.join(segments)

    def _build(self, tree: Any) -> None:
        if tree.get('unprocessed'):
            raise ExternalReportError(UNPROCESSED_CHANGES_MSG)
        self._build_type_changes(tree)
        self._build_default(tree, 'dictionary_item_added')
        self._build_default(tree, 'dictionary_item_removed')
        self._build_values_changed(tree)
        self._build_default(tree, 'iterable_item_added')
        self._build_default(tree, 'iterable_item_removed')
        self._build_iterable_item_moved(tree)
        self._build_default(tree, 'attribute_added')
        self._build_default(tree, 'attribute_removed')
        self._build_set_items(tree, 'set_item_added')
        self._build_set_items(tree, 'set_item_removed')
        self._build_repetition_change(tree)
        if 'deep_distance' in tree:
            self._report['deep_distance'] = tree['deep_distance']
        self._build_custom_results(tree)
        self._report.remove_empty_keys()

    def _pretty(self, report_type: str, **kwargs: Any) -> None:
        if self.verbose_level >= 2 and report_type in _PRETTY_VERBOSE2_TEMPLATES:
            template = _PRETTY_VERBOSE2_TEMPLATES[report_type]
        else:
            template = _PRETTY_TEMPLATES[report_type]
        self._pretty_lines.append(template.format(**kwargs))

    def _build_type_changes(self, tree: Any) -> None:
        changes = tree.get('type_changes')
        if not changes:
            return
        report = self._report['type_changes'] = dict_()
        for change in changes:
            path = self._redacted_path(change)
            if type(change.t1) is type:
                include_values = False
                old_type = change.t1
                new_type = change.t2
            else:
                include_values = True
                old_type = get_type(change.t1)
                new_type = get_type(change.t2)
            entry = {
                'old_type': old_type,
                'new_type': new_type,
            }
            if self.verbose_level > 1:
                new_path = self._redacted_path(change, use_t2=True)
                if path != new_path:
                    entry['new_path'] = new_path
            if include_values:
                old_value = self._redactor.redact_value(change.t1)
                new_value = self._redactor.redact_value(change.t2)
                if self.verbose_level:
                    entry['old_value'] = old_value
                    entry['new_value'] = new_value
                old = _pretty_value(old_value)
                new = _pretty_value(new_value)
            else:
                old = str(old_type)
                new = str(new_type)
            key = _disambiguate(report, path)
            report[key] = entry
            self.change_count += 1
            self._pretty(
                'type_changes', path=key,
                old_type=getattr(old_type, '__name__', old_type),
                new_type=getattr(new_type, '__name__', new_type),
                old=old, new=new)

    def _build_default(self, tree: Any, report_type: str) -> None:
        changes = tree.get(report_type)
        if not changes:
            return
        if self.verbose_level >= 2:
            report = self._report[report_type] = dict_()
            for change in changes:
                item = change.t2 if change.t2 is not notpresent else change.t1
                path = self._redacted_path(change)
                key = _disambiguate(report, path)
                redacted_item = self._redactor.redact_value(item)
                report[key] = redacted_item
                self.change_count += 1
                self._pretty(report_type, path=key, item=_pretty_value(redacted_item))
        else:
            report = self._report[report_type] = SetOrdered()
            for change in changes:
                path = self._redacted_path(change)
                key = _disambiguate(report, path)
                report.add(key)
                self.change_count += 1
                self._pretty(report_type, path=key)

    def _build_values_changed(self, tree: Any) -> None:
        changes = tree.get('values_changed')
        if not changes or self.verbose_level == 0:
            return
        report = self._report['values_changed'] = dict_()
        for change in changes:
            path = self._redacted_path(change)
            old_value = self._redactor.redact_value(change.t1)
            new_value = self._redactor.redact_value(change.t2)
            entry = {
                'new_value': new_value,
                'old_value': old_value,
            }
            if self.verbose_level > 1:
                new_path = self._redacted_path(change, use_t2=True)
                if path != new_path:
                    entry['new_path'] = new_path
            if 'diff' in change.additional:
                entry['diff'] = self._redactor.redact_value(change.additional['diff'])
            key = _disambiguate(report, path)
            report[key] = entry
            self.change_count += 1
            self._pretty(
                'values_changed', path=key,
                old=_pretty_value(old_value), new=_pretty_value(new_value))

    def _build_iterable_item_moved(self, tree: Any) -> None:
        changes = tree.get('iterable_item_moved')
        if not changes or self.verbose_level <= 1:
            return
        report = self._report['iterable_item_moved'] = dict_()
        for change in changes:
            path = self._redacted_path(change, use_t2=False, reporting_move=True)
            new_path = self._redacted_path(change, use_t2=True, reporting_move=True)
            entry = {
                'new_path': new_path,
                'value': self._redactor.redact_value(change.t2),
            }
            key = _disambiguate(report, path)
            report[key] = entry
            self.change_count += 1
            self._pretty('iterable_item_moved', path=key, new_path=new_path)

    def _build_set_items(self, tree: Any, report_type: str) -> None:
        changes = tree.get(report_type)
        if not changes:
            return
        report = self._report[report_type] = SetOrdered()
        for change in changes:
            path = self._redacted_path(change.up)
            item = change.t2 if report_type == 'set_item_added' else change.t1
            redacted_item = self._redactor.redact_value(item)
            if isinstance(redacted_item, strings):
                redacted_item = "'%s'" % redacted_item
            entry = _disambiguate(report, "{}[{}]".format(path, redacted_item))
            report.add(entry)
            self.change_count += 1
            self._pretty(report_type, path=path, item=redacted_item)

    def _build_repetition_change(self, tree: Any) -> None:
        changes = tree.get('repetition_change')
        if not changes:
            return
        report = self._report['repetition_change'] = dict_()
        for change in changes:
            path = self._redacted_path(change)
            entry = dict(change.additional['repetition'])
            entry['value'] = self._redactor.redact_value(change.t1)
            key = _disambiguate(report, path)
            report[key] = entry
            self.change_count += 1
            self._pretty('repetition_change', path=key)

    def _build_custom_results(self, tree: Any) -> None:
        for report_type, levels in tree.items():
            if report_type in REPORT_KEYS or report_type == 'deep_distance':
                continue
            if not isinstance(levels, SetOrdered):
                continue
            report = dict_()
            for level in levels:
                path = self._redacted_path(level)
                key = _disambiguate(report, path)
                report[key] = self._redactor.redact_value(level.additional.get(CUSTOM_FIELD, {}))
                self.change_count += 1
            if report:
                self._report[report_type] = report

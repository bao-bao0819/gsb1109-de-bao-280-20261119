import re
import pytest
from deepdiff import DeepDiff, Delta, ExternalReportError
from deepdiff.helper import SetOrdered

EMAIL = 'alice@example.com'
PHONE_OLD = '555-0100'
PHONE_NEW = '555-0199'
SENSITIVE_VALUES = [EMAIL, PHONE_OLD, PHONE_NEW]

CODENAME_PATTERN = re.compile(r'REDACTED-[0-9a-f]+-\d+')


def build_questionnaire_batch():
    """
    Two questionnaire versions. The contact value shows up both as a
    dictionary key indexing a record and as a field value. One contact
    method changed and one nested object holding both a contact value
    and a public amount was added.
    """
    t1 = {
        EMAIL: {'contact': PHONE_OLD, 'plan': 'basic'},
        'r2': {'email': EMAIL, 'contact': PHONE_OLD},
    }
    t2 = {
        EMAIL: {'contact': PHONE_NEW, 'plan': 'basic'},
        'r2': {'email': EMAIL, 'contact': PHONE_OLD},
        'r3': {'email': EMAIL, 'amount': 500},
    }
    return t1, t2


def count_changes(result):
    total = 0
    for value in result.values():
        if isinstance(value, (dict, SetOrdered, list, set)):
            total += len(value)
    return total


def codenames(payload):
    return set(CODENAME_PATTERN.findall(payload))


class TestExternalView:

    def test_report_then_delta_then_exports(self):
        t1, t2 = build_questionnaire_batch()

        # The external report is produced first.
        diff = DeepDiff(
            t1, t2, view='external',
            external_redact_values=SENSITIVE_VALUES, verbose_level=2)

        # The real number of changes matches the internal one.
        internal = DeepDiff(t1, t2, verbose_level=2)
        assert count_changes(internal) == diff._external_report.change_count == 2

        # The original diff still restores the revised batch internally.
        delta = Delta(diff)
        assert t1 + delta == t2

        # The report is exported once as text view and once as JSON.
        text_view = diff.pretty()
        json_view = diff.to_json()
        for export in (text_view, json_view, str(diff), str(diff.to_dict())):
            for sensitive in SENSITIVE_VALUES:
                assert sensitive not in export
        # The public amount is still verifiable.
        assert '500' in json_view
        assert '500' in text_view

    def test_same_original_gets_same_codename_within_report(self):
        t1, t2 = build_questionnaire_batch()
        diff = DeepDiff(
            t1, t2, view='external',
            external_redact_values=SENSITIVE_VALUES, verbose_level=2)
        result = diff.to_dict()
        # The email is the dict key of the changed record's path and a
        # field value of the added nested object. Both point to the same
        # codename so the external party can confirm they are equal.
        (changed_path,) = list(result['values_changed'])
        added = result['dictionary_item_added']["root['r3']"]
        codename = added['email']
        assert CODENAME_PATTERN.fullmatch(codename)
        assert codename in changed_path
        assert added == {'email': codename, 'amount': 500}
        # The changed contact values are redacted with their own codenames.
        change = result['values_changed'][changed_path]
        assert CODENAME_PATTERN.fullmatch(change['old_value'])
        assert CODENAME_PATTERN.fullmatch(change['new_value'])
        assert change['old_value'] != change['new_value']

    def test_separate_batches_are_not_correlatable(self):
        # Another batch containing the same email is delivered separately.
        t1 = {'record': {'email': EMAIL}}
        t2 = {'record': {'email': EMAIL, 'amount': 500}, 'added': {'email': EMAIL}}
        batch1 = DeepDiff(
            t1, t2, view='external',
            external_redact_values=SENSITIVE_VALUES, verbose_level=2)
        batch2 = DeepDiff(
            t1, t2, view='external',
            external_redact_values=SENSITIVE_VALUES, verbose_level=2)

        tokens1 = codenames(batch1.to_json())
        tokens2 = codenames(batch2.to_json())
        assert tokens1
        assert tokens2
        # Codenames of the second batch can not be linked via the first report.
        assert tokens1.isdisjoint(tokens2)

        # Each batch is still consistent within itself.
        added1 = batch1.to_dict()['dictionary_item_added']["root['added']"]
        assert added1['email'] in tokens1

        # Both batches still apply corrections accurately internally.
        assert t1 + Delta(batch1) == t2
        assert t1 + Delta(batch2) == t2

    def test_masked_path_collision_keeps_both_changes(self):
        t1 = {'k1@example.com': 1, 'k2@example.com': 2}
        t2 = {'k1@example.com': 10, 'k2@example.com': 20}
        diff = DeepDiff(
            t1, t2, view='external',
            external_redact_values=['k1@example.com', 'k2@example.com'])
        values_changed = diff['values_changed']
        # Masking the two paths must not merge them: both actual changes
        # are kept and their codenames are distinguishable.
        assert len(values_changed) == 2
        paths = list(values_changed)
        assert paths[0] != paths[1]
        assert len(DeepDiff(t1, t2)['values_changed']) == 2
        assert 'k1@example.com' not in str(values_changed)
        assert 'k2@example.com' not in str(values_changed)

    def test_unverifiable_value_refuses_export_without_leaking(self):
        class Secret:
            def __init__(self, value):
                self.value = value

            def __repr__(self):
                return 'Secret({})'.format(self.value)

        t1 = {'key': Secret(EMAIL)}
        t2 = {'key': 'public'}
        with pytest.raises(ExternalReportError) as exc_info:
            DeepDiff(t1, t2, view='external', external_redact_values=[EMAIL])
        assert EMAIL not in str(exc_info.value)
        assert 'public' not in str(exc_info.value)

    def test_external_view_requires_redact_values(self):
        with pytest.raises(ValueError) as exc_info:
            DeepDiff({'a': 1}, {'a': 2}, view='external')
        assert 'external_redact_values' in str(exc_info.value)

    def test_default_view_format_is_unchanged(self):
        t1, t2 = build_questionnaire_batch()
        diff = DeepDiff(t1, t2)
        assert diff == {
            'values_changed': {
                "root['{}']['contact']".format(EMAIL): {
                    'new_value': PHONE_NEW,
                    'old_value': PHONE_OLD,
                },
            },
            'dictionary_item_added': {"root['r3']"},
        }
        assert EMAIL in diff.to_json()

    def test_single_string_redact_value_is_accepted(self):
        diff = DeepDiff(
            {EMAIL: 1}, {EMAIL: 2},
            view='external', external_redact_values=EMAIL)
        (path,) = list(diff['values_changed'])
        assert EMAIL not in path
        assert CODENAME_PATTERN.search(path)

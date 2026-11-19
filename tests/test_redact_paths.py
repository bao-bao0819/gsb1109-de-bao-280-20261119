import pickle

import pytest
from deepdiff import DeepDiff


class TestRedactPaths:

    def test_values_changed_shows_codes_not_values(self):
        t1 = {'phone': '13800001111', 'price': 100}
        t2 = {'phone': '13900002222', 'price': 120}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['phone']"])
        assert ddiff == {
            'values_changed': {
                "root['phone']": {'old_value': '<redacted:1>', 'new_value': '<redacted:2>'},
                "root['price']": {'old_value': 100, 'new_value': 120},
            }
        }

    def test_raw_values_not_in_report_text(self):
        t1 = {'phone': '13800001111', 'price': 100}
        t2 = {'phone': '13900002222', 'price': 120}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['phone']"])
        for output in (str(ddiff), ddiff.to_json(), repr(ddiff)):
            assert '13800001111' not in output
            assert '13900002222' not in output
        assert '100' in str(ddiff)

    def test_same_value_reuses_same_code(self):
        t1 = {'phone': '111', 'emergency_contact': {'phone': '111'}}
        t2 = {'phone': '222', 'emergency_contact': {'phone': '222'}}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['phone']", "root['emergency_contact']"])
        changes = ddiff['values_changed']
        assert changes["root['phone']"]['old_value'] == changes["root['emergency_contact']['phone']"]['old_value']
        assert changes["root['phone']"]['new_value'] == changes["root['emergency_contact']['phone']"]['new_value']
        assert changes["root['phone']"]['old_value'] != changes["root['phone']"]['new_value']

    def test_different_values_get_different_codes(self):
        t1 = {'a': 'x', 'b': 'y'}
        t2 = {'a': 'z', 'b': 'w'}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['a']", "root['b']"])
        changes = ddiff['values_changed']
        codes = {
            changes["root['a']"]['old_value'],
            changes["root['a']"]['new_value'],
            changes["root['b']"]['old_value'],
            changes["root['b']"]['new_value'],
        }
        assert len(codes) == 4

    def test_change_detection_uses_real_values(self):
        t1 = {'phone': '111'}
        t2 = {'phone': '111'}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['phone']"])
        assert ddiff == {}

    def test_descendants_of_redacted_path_are_redacted(self):
        t1 = {'contact': {'phone': '111', 'email': 'a@example.com'}}
        t2 = {'contact': {'phone': '222', 'email': 'b@example.com'}}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['contact']"])
        result = str(ddiff)
        assert '111' not in result
        assert 'a@example.com' not in result
        assert '222' not in result
        assert 'b@example.com' not in result
        assert set(ddiff['values_changed']) == {"root['contact']['phone']", "root['contact']['email']"}

    def test_glob_paths(self):
        t1 = {'contacts': [{'phone': '111'}, {'phone': '222'}]}
        t2 = {'contacts': [{'phone': '333'}, {'phone': '222'}]}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['contacts'][*]['phone']"])
        result = str(ddiff)
        assert '111' not in result
        assert '333' not in result
        assert ddiff['values_changed'] == {
            "root['contacts'][0]['phone']": {'old_value': '<redacted:1>', 'new_value': '<redacted:2>'},
        }

    def test_unmatched_paths_show_real_values(self):
        t1 = {'phone': '111', 'price': 100}
        t2 = {'phone': '222', 'price': 200}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['phone']"])
        assert ddiff['values_changed']["root['price']"] == {'old_value': 100, 'new_value': 200}

    def test_type_changes_keep_real_types(self):
        t1 = {'phone': '111'}
        t2 = {'phone': 111}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['phone']"])
        change = ddiff['type_changes']["root['phone']"]
        assert change['old_type'] is str
        assert change['new_type'] is int
        assert change['old_value'] == '<redacted:1>'
        assert change['new_value'] == '<redacted:2>'
        assert '111' not in str(ddiff)

    def test_dictionary_item_added_verbose(self):
        t1 = {'phone': '111'}
        t2 = {'phone': '111', 'email': 'a@example.com'}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['email']"], verbose_level=2)
        assert ddiff['dictionary_item_added']["root['email']"] == '<redacted:1>'
        assert 'a@example.com' not in str(ddiff)

    def test_iterable_item_added(self):
        t1 = {'phones': ['a']}
        t2 = {'phones': ['a', 'b']}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['phones']"])
        assert ddiff['iterable_item_added'] == {"root['phones'][1]": '<redacted:1>'}
        assert "'b'" not in str(ddiff)

    def test_set_items(self):
        t1 = {'tags': {'a', 'b'}}
        t2 = {'tags': {'a', 'c'}}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['tags']"])
        result = str(ddiff)
        assert "'b'" not in result
        assert "'c'" not in result

    def test_repetition_change(self):
        t1 = {'phones': ['a', 'a', 'b']}
        t2 = {'phones': ['a', 'b', 'b']}
        ddiff = DeepDiff(t1, t2, ignore_order=True, report_repetition=True, redact_paths=["root['phones']"])
        result = str(ddiff)
        assert "'a'" not in result
        assert "'b'" not in result
        for change in ddiff['repetition_change'].values():
            assert change['value'].startswith('<redacted:')

    def test_unhashable_value_redacted(self):
        t1 = {'contact': {'aliases': ['x', 'y']}}
        t2 = {'contact': {'aliases': ['x', 'z']}}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['contact']"])
        result = str(ddiff)
        assert "'y'" not in result
        assert "'z'" not in result

    def test_tree_view(self):
        t1 = {'phone': '111'}
        t2 = {'phone': '222'}
        ddiff = DeepDiff(t1, t2, redact_paths=["root['phone']"], view='tree')
        (level,) = ddiff['values_changed']
        assert level.t1 == '<redacted:1>'
        assert level.t2 == '<redacted:2>'

    @pytest.mark.parametrize("view", ['colored', 'colored_compact'])
    def test_colored_views_not_supported(self, view):
        with pytest.raises(ValueError) as excinfo:
            DeepDiff({'phone': '111'}, {'phone': '222'}, redact_paths=["root['phone']"], view=view)
        assert 'redact_paths' in str(excinfo.value)

    def test_get_deep_distance_still_calculated(self):
        ddiff = DeepDiff({'phone': '111'}, {'phone': '222'}, redact_paths=["root['phone']"], get_deep_distance=True)
        assert 'deep_distance' in ddiff
        assert ddiff['deep_distance'] > 0

    def test_single_string_path(self):
        ddiff = DeepDiff({'phone': '111'}, {'phone': '222'}, redact_paths="root['phone']")
        assert ddiff['values_changed']["root['phone']"] == {'old_value': '<redacted:1>', 'new_value': '<redacted:2>'}

    def test_path_without_root_prefix(self):
        ddiff = DeepDiff({'phone': '111'}, {'phone': '222'}, redact_paths=["phone"])
        assert ddiff['values_changed']["root['phone']"] == {'old_value': '<redacted:1>', 'new_value': '<redacted:2>'}

    def test_no_redact_paths_unchanged_behavior(self):
        ddiff = DeepDiff({'phone': '111'}, {'phone': '222'})
        assert ddiff['values_changed']["root['phone']"] == {'old_value': '111', 'new_value': '222'}

    def test_pickle_round_trip_keeps_codes(self):
        ddiff = DeepDiff({'phone': '111'}, {'phone': 222}, redact_paths=["root['phone']"])
        restored = pickle.loads(pickle.dumps(ddiff))
        change = restored['type_changes']["root['phone']"]
        assert change['old_value'] == '<redacted:1>'
        assert change['new_value'] == '<redacted:2>'
        assert change['old_type'] is str
        assert change['new_type'] is int
        assert '111' not in str(restored)

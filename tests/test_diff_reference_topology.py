import pytest
from deepdiff import DeepDiff


class TestReferenceTopology:

    def test_shared_reference_split_is_reported(self):
        shared = {'id': 1, 'name': 'canonical'}
        t1 = {'a': shared, 'b': shared}
        t2 = {'a': {'id': 1, 'name': 'canonical'}, 'b': {'id': 1, 'name': 'canonical'}}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff == {'shared_reference_splits': [{'paths': ["root['a']", "root['b']"]}]}

    def test_shared_reference_preserved_is_not_reported(self):
        shared1 = {'id': 1}
        shared2 = {'id': 1}
        t1 = {'a': shared1, 'b': shared1}
        t2 = {'a': shared2, 'b': shared2}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert 'shared_reference_splits' not in diff
        assert 'reference_cycles' not in diff

    def test_independent_copies_are_not_reported(self):
        t1 = {'a': {'id': 1}, 'b': {'id': 1}}
        t2 = {'a': {'id': 1}, 'b': {'id': 1}}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff == {}

    def test_report_is_opt_in(self):
        shared = {'id': 1}
        t1 = {'a': shared, 'b': shared}
        t2 = {'a': {'id': 1}, 'b': {'id': 1}}
        diff = DeepDiff(t1, t2)
        assert diff == {}

    def test_shared_reference_split_inside_iterables(self):
        item = {'x': 1}
        t1 = {'items': [item, item]}
        t2 = {'items': [{'x': 1}, {'x': 1}]}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff == {'shared_reference_splits': [{'paths': ["root['items'][0]", "root['items'][1]"]}]}

    def test_only_outermost_split_is_reported(self):
        inner = {'y': 2}
        outer = {'inner': inner}
        t1 = {'a': outer, 'b': outer}
        t2 = {'a': {'inner': {'y': 2}}, 'b': {'inner': {'y': 2}}}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff == {'shared_reference_splits': [{'paths': ["root['a']", "root['b']"]}]}

    def test_partial_split_is_reported(self):
        shared = {'id': 1}
        still_shared = {'id': 1}
        t1 = {'a': shared, 'b': shared, 'c': shared}
        t2 = {'a': still_shared, 'b': still_shared, 'c': {'id': 1}}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff == {'shared_reference_splits': [{'paths': ["root['a']", "root['b']", "root['c']"]}]}

    def test_split_not_reported_when_path_missing_in_t2(self):
        shared = {'id': 1}
        t1 = {'a': shared, 'b': shared}
        t2 = {'a': {'id': 1}}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert 'shared_reference_splits' not in diff
        assert diff == {'dictionary_item_removed': ["root['b']"]}

    def test_self_reference_terminates_and_reports_cycle(self):
        t1 = {}
        t1['self'] = t1
        t2 = {}
        t2['self'] = t2
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff == {
            'reference_cycles': [
                {'tree': 't1', 'path': "root['self']", 'target': 'root'},
                {'tree': 't2', 'path': "root['self']", 'target': 'root'},
            ]
        }

    def test_mutual_cycle_terminates_and_reports_cycle(self):
        a1, b1 = {}, {}
        a1['b'] = b1
        b1['a'] = a1
        a2, b2 = {}, {}
        a2['b'] = b2
        b2['a'] = a2
        diff = DeepDiff({'x': a1}, {'x': a2}, report_reference_topology=True)
        assert diff == {
            'reference_cycles': [
                {'tree': 't1', 'path': "root['x']['b']['a']", 'target': "root['x']"},
                {'tree': 't2', 'path': "root['x']['b']['a']", 'target': "root['x']"},
            ]
        }

    def test_cycle_in_t2_only(self):
        t1 = {'x': 1}
        t2 = {'x': 1}
        t2['loop'] = t2
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff['reference_cycles'] == [{'tree': 't2', 'path': "root['loop']", 'target': 'root'}]
        assert diff['dictionary_item_added'] == ["root['loop']"]

    def test_cycle_through_list(self):
        t1 = [1]
        t1.append(t1)
        t2 = [1]
        t2.append(t2)
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff == {
            'reference_cycles': [
                {'tree': 't1', 'path': 'root[1]', 'target': 'root'},
                {'tree': 't2', 'path': 'root[1]', 'target': 'root'},
            ]
        }

    def test_cycles_not_reported_without_flag(self):
        t1 = {}
        t1['self'] = t1
        t2 = {}
        t2['self'] = t2
        diff = DeepDiff(t1, t2)
        assert diff == {}

    def test_shared_custom_object_split(self):
        class Config:
            def __init__(self, name):
                self.name = name

        shared = Config('canonical')
        t1 = {'a': shared, 'b': shared}
        t2 = {'a': Config('canonical'), 'b': Config('canonical')}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff == {'shared_reference_splits': [{'paths': ["root['a']", "root['b']"]}]}

    def test_shared_immutable_leaves_are_ignored(self):
        text = 'some shared string'
        t1 = {'a': text, 'b': text}
        t2 = {'a': 'some shared string', 'b': 'some shared string'}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        assert diff == {}

    def test_to_dict_and_to_json_include_report(self):
        shared = {'id': 1}
        t1 = {'a': shared, 'b': shared}
        t2 = {'a': {'id': 1}, 'b': {'id': 1}}
        diff = DeepDiff(t1, t2, report_reference_topology=True)
        expected = {'shared_reference_splits': [{'paths': ["root['a']", "root['b']"]}]}
        assert diff.to_dict() == expected
        assert 'shared_reference_splits' in diff.to_json()

    @pytest.mark.parametrize('view', ['tree', 'text'])
    def test_report_available_in_views(self, view):
        shared = {'id': 1}
        t1 = {'a': shared, 'b': shared}
        t2 = {'a': {'id': 1}, 'b': {'id': 1}}
        diff = DeepDiff(t1, t2, report_reference_topology=True, view=view)
        assert diff['shared_reference_splits'] == [{'paths': ["root['a']", "root['b']"]}]

import pytest

from deepdiff import DeepDiff, DeepHash, DeepSearch, grep
from deepdiff.helper import (
    compare_selector_to_key,
    compare_selector_to_level,
    separate_wildcard_and_exact_paths,
)
from deepdiff.path import (
    GET,
    GETATTR,
    MULTI_WILDCARD,
    SINGLE_WILDCARD,
    _path_to_elements,
    concrete_path_segments,
    is_structured_path,
    selector_has_wildcard,
    structured_path_has_wildcard,
    structured_path_segments,
)


# ── parsing helpers ──────────────────────────────────────────────────


class TestStructuredParsing:

    def test_is_structured_path(self):
        assert is_structured_path(('items', 0))
        assert is_structured_path(['items', 0])
        assert not is_structured_path("root['items'][0]")

    def test_path_to_elements_adds_root_and_get_action(self):
        assert _path_to_elements(('items', 0)) == (
            ('root', GETATTR), ('items', GET), (0, GET),
        )

    def test_path_to_elements_respects_getattr_pair(self):
        assert _path_to_elements(('obj', ('name', GETATTR))) == (
            ('root', GETATTR), ('obj', GET), ('name', GETATTR),
        )

    def test_already_parsed_element_tuples_pass_through(self):
        parsed = (('root', GETATTR), ('a', GET), (1, GET))
        assert _path_to_elements(parsed) == parsed

    def test_segment_helpers(self):
        assert structured_path_segments(('items', 0)) == ('items', 0)
        assert concrete_path_segments("root['items'][0]") == ('items', 0)

    @pytest.mark.parametrize("selector, expected", [
        (('*',), False),
        (('**',), False),
        ((SINGLE_WILDCARD,), True),
        ((MULTI_WILDCARD,), True),
        (('items', SINGLE_WILDCARD, 'v'), True),
        (('items', '*'), False),
    ])
    def test_wildcard_detection(self, selector, expected):
        assert structured_path_has_wildcard(selector) is expected
        assert selector_has_wildcard(selector) is expected

    def test_literal_star_is_not_wildcard_in_elements(self):
        elements = _path_to_elements(('*',))
        assert elements[1][0] == '*'
        assert isinstance(elements[1][0], str)
        assert elements[1][0] is not SINGLE_WILDCARD

    def test_separate_wildcard_and_exact_paths(self):
        exact, globs = separate_wildcard_and_exact_paths(
            {('items', 0), ('items', SINGLE_WILDCARD)}
        )
        assert exact == {('items', 0)}
        assert len(globs) == 1


# ── segment relation helpers ─────────────────────────────────────────


class TestSegmentRelations:

    @pytest.mark.parametrize("selector, level_path, relation", [
        (('items', 0), "root['items'][0]", 'equal'),
        (('items',), "root['items'][0]", 'ancestor'),
        (('items', 0, 'name'), "root['items'][0]", 'descendant'),
        (('items', 1), "root['items'][0]", None),
        (('other',), "root['items'][0]", None),
    ])
    def test_compare_selector_to_level(self, selector, level_path, relation):
        assert compare_selector_to_level(selector, level_path) == relation

    def test_compare_selector_to_key_keeps_int_index(self):
        # The quoted key_path form would be root['items']['0']; the raw-key
        # helper must still match the integer-index selector.
        assert compare_selector_to_key(('items', 0), "root['items']", 0) == 'equal'
        assert compare_selector_to_key(('items',), "root['items']", 0) == 'ancestor'
        assert compare_selector_to_key(('items', 1), "root['items']", 0) is None


# ── the supplier scenario: literal field name vs real list item ─────


class TestSupplierScenario:

    def test_literal_items_zero_field_name(self):
        t1 = {'items[0]': 'old', 'other': 'same'}
        t2 = {'items[0]': 'new', 'other': 'same'}
        diff = DeepDiff(t1, t2, include_paths=[('items[0]',)])
        assert list(diff.get('values_changed', {})) == ["root['items[0]']"]

    def test_real_items_list_zeroth_member(self):
        t1 = {'items': [{'name': 'a'}, {'name': 'b'}]}
        t2 = {'items': [{'name': 'A'}, {'name': 'b'}]}
        diff = DeepDiff(t1, t2, include_paths=[('items', 0)])
        assert list(diff.get('values_changed', {})) == ["root['items'][0]['name']"]

    def test_both_selectors_select_their_own_shape(self):
        flat_t1 = {'items[0]': 'old', 'note': 'x'}
        flat_t2 = {'items[0]': 'new', 'note': 'x'}
        list_t1 = {'items': [{'name': 'a'}], 'note': 'x'}
        list_t2 = {'items': [{'name': 'A'}], 'note': 'x'}
        selectors = [('items[0]',), ('items', 0)]

        flat_diff = DeepDiff(flat_t1, flat_t2, include_paths=selectors)
        assert list(flat_diff['values_changed']) == ["root['items[0]']"]

        list_diff = DeepDiff(list_t1, list_t2, include_paths=selectors)
        assert list(list_diff['values_changed']) == ["root['items'][0]['name']"]

    def test_exclude_literal_field_leaves_real_list_alone(self):
        t1 = {'items[0]': 'old', 'items': [0]}
        t2 = {'items[0]': 'new', 'items': [1]}
        diff = DeepDiff(t1, t2, exclude_paths=[('items[0]',)])
        changed = diff.get('values_changed', {})
        assert "root['items[0]']" not in changed
        assert "root['items'][0]" in changed


# ── literal star vs explicit wildcard ────────────────────────────────


class TestLiteralStarVersusWildcard:

    def test_literal_star_key_segment(self):
        t1 = {'*': 1, 'a': 2}
        t2 = {'*': 9, 'a': 8}
        diff = DeepDiff(t1, t2, include_paths=[('*',)])
        assert list(diff.get('values_changed', {})) == ["root['*']"]

    def test_wildcard_token_matches_one_member(self):
        t1 = {'*': 1, 'a': 2}
        t2 = {'*': 9, 'a': 8}
        diff = DeepDiff(t1, t2, include_paths=[(SINGLE_WILDCARD,)])
        # One level only: the top-level keys themselves, not their contents.
        assert set(diff.get('values_changed', {})) == {"root['*']", "root['a']"}

    def test_wildcard_over_list_members(self):
        t1 = {'items': [{'v': 1}, {'v': 2}], 'n': 3}
        t2 = {'items': [{'v': 9}, {'v': 8}], 'n': 30}
        # The wildcard matches exactly one member level; the leaf 'v' is the
        # reported change. Parity with string path root['items'][*]['v'].
        diff = DeepDiff(t1, t2, include_paths=[('items', SINGLE_WILDCARD, 'v')])
        assert set(diff.get('values_changed', {})) == {
            "root['items'][0]['v']", "root['items'][1]['v']",
        }

    def test_multi_wildcard_reaches_any_depth(self):
        t1 = {'items': [{'meta': {'v': 1}}]}
        t2 = {'items': [{'meta': {'v': 9}}]}
        diff = DeepDiff(t1, t2, include_paths=[(MULTI_WILDCARD, 'v')])
        assert list(diff.get('values_changed', {})) == ["root['items'][0]['meta']['v']"]

    def test_structured_and_string_glob_agree(self):
        t1 = {'*': {'v': 1}, 'a': {'v': 2}}
        t2 = {'*': {'v': 9}, 'a': {'v': 8}}
        from_string = DeepDiff(t1, t2, include_paths=["root[*]"])
        from_structured = DeepDiff(t1, t2, include_paths=[(SINGLE_WILDCARD,)])
        assert dict(from_string) == dict(from_structured)


# ── DeepDiff include / exclude integration ───────────────────────────


class TestStructuredDeepDiff:

    def test_include_nested_index_path(self):
        t1 = {'data': [{'id': 1, 'v': 0}, {'id': 2, 'v': 0}]}
        t2 = {'data': [{'id': 1, 'v': 9}, {'id': 2, 'v': 8}]}
        diff = DeepDiff(t1, t2, include_paths=[('data', 1, 'v')])
        assert list(diff['values_changed']) == ["root['data'][1]['v']"]

    def test_exclude_index_path(self):
        t1 = {'data': [1, 2, 3]}
        t2 = {'data': [1, 20, 30]}
        diff = DeepDiff(t1, t2, exclude_paths=[('data', 2)])
        assert list(diff['values_changed']) == ["root['data'][1]"]

    def test_include_subtree_includes_descendants(self):
        t1 = {'a': {'x': 1, 'y': 2}, 'b': 3}
        t2 = {'a': {'x': 10, 'y': 20}, 'b': 30}
        diff = DeepDiff(t1, t2, include_paths=[('a',)])
        assert set(diff['values_changed']) == {"root['a']['x']", "root['a']['y']"}

    def test_mixed_string_and_structured_selectors(self):
        t1 = {'items[0]': 'old', 'keep': 1}
        t2 = {'items[0]': 'new', 'keep': 2}
        diff = DeepDiff(t1, t2, include_paths=[('items[0]',), "root['keep']"])
        assert set(diff['values_changed']) == {"root['items[0]']", "root['keep']"}

    def test_attribute_selector(self):
        class Obj:
            def __init__(self, name, secret):
                self.name = name
                self.secret = secret
        diff = DeepDiff(Obj('a', 's1'), Obj('b', 's2'),
                        include_paths=[('name', GETATTR)])
        assert list(diff['values_changed']) == ['root.name']

    def test_single_tuple_argument(self):
        diff = DeepDiff({'a': 1, 'b': 2}, {'a': 9, 'b': 8}, include_paths=('a',))
        assert list(diff['values_changed']) == ["root['a']"]

    def test_list_selector_inside_container(self):
        diff = DeepDiff({'a': {'b': 1}}, {'a': {'b': 2}},
                        include_paths=[['a', 'b']])
        assert list(diff['values_changed']) == ["root['a']['b']"]

    def test_tree_view_paths_point_back_to_json(self):
        t1 = {'items': [{'v': 1}], 'x': 2}
        t2 = {'items': [{'v': 9}], 'x': 2}
        diff = DeepDiff(t1, t2, include_paths=[('items', 0)], view='tree')
        assert list(diff['values_changed'])[0].path() == "root['items'][0]['v']"

    def test_structured_glob_exclude_with_ignore_order(self):
        t1 = [{'v': 1}, {'v': 2}]
        t2 = [{'v': 10}, {'v': 20}]
        diff = DeepDiff(t1, t2, ignore_order=True,
                        exclude_paths=[(SINGLE_WILDCARD, 'v')])
        assert diff == {}


# ── DeepSearch and DeepHash integration ──────────────────────────────


class TestStructuredSearchAndHash:

    def test_search_exclude_structured(self):
        obj = [{'secret': 'findme', 'name': 'x'},
               {'secret': 'findme', 'name': 'y'}]
        result = DeepSearch(obj, 'findme', exclude_paths=[(0, 'secret')])
        assert list(result['matched_values']) == ["root[1]['secret']"]

    def test_search_exclude_structured_via_grep(self):
        obj = [{'secret': 'findme'}, {'secret': 'findme'}]
        result = obj | grep('findme', exclude_paths=[(SINGLE_WILDCARD, 'secret')])
        assert result == {}

    def test_hash_exclude_structured(self):
        t1 = [{'secret': 'a', 'keep': 1}]
        t2 = [{'secret': 'b', 'keep': 1}]
        h1 = DeepHash(t1, exclude_paths=[(0, 'secret')])
        h2 = DeepHash(t2, exclude_paths=[(0, 'secret')])
        assert h1[t1] == h2[t2]

    def test_hash_include_structured_ignores_outside_change(self):
        t1 = {'pick': {'v': 1}, 'other': 1}
        t2 = {'pick': {'v': 1}, 'other': 2}
        h1 = DeepHash(t1, include_paths=[('pick',)])
        h2 = DeepHash(t2, include_paths=[('pick',)])
        assert h1[t1] == h2[t2]

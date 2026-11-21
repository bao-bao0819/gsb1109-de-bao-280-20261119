# -*- coding: utf-8 -*-

"""Acceptance coverage for tracked list ingest and reorganized provenance."""

from collections import OrderedDict

import pytest

from schematics.exceptions import ConversionError, DataError
from schematics.models import Model
from schematics.provenance import (
    AmbiguousProvenanceError, BatchIngestResult, IngestError, ListIngestResult,
    MissingProvenanceError, Provenance, TrackedItem, describe_positions, locate,
    merge, split)
from schematics.types import IntType, StringType
from schematics.types.compound import ListType, ModelType


class Detail(Model):
    code = StringType(required=True)
    amount = IntType(required=True)
    kind = StringType(default='goods')


class Order(Model):
    order_no = StringType(required=True)
    lines = ListType(ModelType(Detail), ingest=True)


class Batch(Model):
    batch_no = StringType(required=True)
    orders = ListType(ModelType(Order), ingest=True)


def _batch():
    return Batch({'batch_no': 'B-1'})


def _order(order_no):
    return {'order_no': order_no}


def _detail(code, amount, kind='goods'):
    return {'code': code, 'amount': amount, 'kind': kind}


def _ingest_batch(orders):
    return Batch.orders.ingest(orders)


def test_placeholder_zero_and_duplicate_codes_are_preserved():
    result = Order.lines.ingest([
        None,
        _detail('A-1', 5),
        None,
        _detail('A-1', 0),
        _detail('A-2', 7),
    ])

    assert result.items == [Detail(_detail('A-1', 5)),
                            Detail(_detail('A-1', 0)),
                            Detail(_detail('A-2', 7))]
    assert result.positions == [1, 3, 4]
    assert result.skipped == [0, 2]
    assert all(rejection is None for rejection in result.errors)


def test_single_pass_iterator_is_consumed_once():
    consumed = []

    def source():
        for item in [None, _detail('X', 1), _detail('Y', 0)]:
            consumed.append(item)
            yield item

    result = Order.lines.ingest(source())

    assert len(consumed) == 3
    assert result.consumed == 3
    assert result.positions == [1, 2]
    assert [detail.code for detail in result.items] == ['X', 'Y']


def test_conversion_error_reports_original_position():
    result = Order.lines.ingest([
        _detail('OK-1', 1),
        _detail('BAD-1', 'not-an-amount'),
        _detail('OK-2', 2),
    ])

    assert [detail.code for detail in result.items] == ['OK-1', 'OK-2']
    assert result.positions == [0, 2]
    assert len(result.errors) == 1
    rejection = result.errors[0]
    assert rejection.position == 1
    assert rejection.detail == 1
    assert rejection.parent is None
    assert 'not int' in rejection.reason


def test_nested_error_distinguishes_child_from_bad_parent():
    result = _ingest_batch([
        None,
        {'order_no': 'O-1', 'lines': [
            None,
            _detail('D-1', 'bad'),
            _detail('D-2', 1),
        ]},
        {'order_no': 'O-2', 'lines': [
            None,
            _detail('D-3', 4),
            _detail('D-3', 0),
        ]},
        42,
    ])

    assert isinstance(result, BatchIngestResult)
    assert result.skipped == [0]

    bad_child = result.result_for(1)
    assert bad_child.ok is False
    assert bad_child.items == []
    assert [(r.parent, r.detail) for r in bad_child.errors] == [(1, 1)]
    assert 'not int' in bad_child.errors[0].reason

    good_child = result.result_for(2)
    assert good_child.ok is True
    assert good_child.positions == [1, 2]
    assert [detail.code for detail in good_child.items] == ['D-3', 'D-3']
    assert good_child.items[0].amount != 0
    assert good_child.items[1].amount == 0

    bad_parent = result.result_for(3)
    assert bad_parent.ok is False
    assert bad_parent.items == []
    assert bad_parent.errors[0].parent is None
    assert 'mapping' in bad_parent.errors[0].reason


def test_failed_parent_never_delivers_partial_details():
    result = _ingest_batch([
        {'order_no': 'O-1', 'lines': [
            _detail('A', 1),
            _detail('B', 'bad'),
            _detail('C', 3),
        ]},
        {'order_no': 'O-2', 'lines': [_detail('D', 4)]},
    ])

    assert result.items == [Detail(_detail('D', 4))]
    assert result.errors
    assert result.result_for(0).items == []
    assert result.result_for(1).items == [Detail(_detail('D', 4))]
    assert [entry.positions for entry in result.tracked] == [(1, 0)]


def test_placeholder_moves_coordinates_only():
    with_leading = Order.lines.ingest([
        None, _detail('A', 1), _detail('B', 2)])
    with_trailing = Order.lines.ingest([
        _detail('A', 1), _detail('B', 2), None])
    with_middle = Order.lines.ingest([
        _detail('A', 1), None, _detail('B', 2)])

    assert with_leading.items == with_trailing.items == with_middle.items
    assert with_leading.positions == [1, 2]
    assert with_trailing.positions == [0, 1]
    assert with_middle.positions == [0, 2]
    assert with_trailing.skipped == [2]
    assert with_middle.skipped == [1]


def test_same_details_from_different_source_positions():
    first = Order.lines.ingest([_detail('A', 1), _detail('B', 2)])
    second = Order.lines.ingest([None, _detail('A', 1), _detail('B', 2)])

    assert first.items == second.items
    assert first.positions == [0, 1]
    assert second.positions == [1, 2]
    assert first.tracked[0] != second.tracked[0]

    batch = _ingest_batch([
        {'order_no': 'O-1', 'lines': [_detail('A', 1)]},
        {'order_no': 'O-2', 'lines': [None, _detail('A', 1)]},
    ])
    assert batch.items == [Detail(_detail('A', 1)), Detail(_detail('A', 1))]
    assert [entry.positions for entry in batch.tracked] == [(0, 0), (1, 1)]


@pytest.mark.parametrize('source,expected', [
    ([_detail('A', 1), _detail('B', 2)], True),
    ([None, _detail('A', 1), _detail('B', 2)], True),
    ([_detail('A', 1), None, _detail('B', 2)], True),
    ([_detail('A', 1), _detail('B', 2), None], True),
])
def test_min_size_counts_retained_items_only(source, expected):
    field = ListType(ModelType(Detail), ingest=True, min_size=2)
    result = field.ingest(source)
    assert (result.length_error is None) is expected


def test_min_size_with_placeholders_does_not_count_placeholder():
    field = ListType(ModelType(Detail), ingest=True, min_size=3)
    result = field.ingest([None, _detail('A', 1), _detail('B', 2), None])

    assert result.length_error is not None
    assert 'at least 3' in str(result.length_error)
    assert result.ok is False


def test_strict_path_unchanged_without_ingest_option():
    strict = ListType(ModelType(Detail))

    with pytest.raises(Exception) as excinfo:
        strict([_detail('A', 1), _detail('B', 'bad')])
    assert 'B' not in str(excinfo.value) or 'int' in str(excinfo.value)

    fresh = Order({'order_no': 'O', 'lines': [_detail('A', 1)]})
    assert fresh.lines[0].amount == 1

    # Error shape of the ordinary path is unchanged as well: the list reports
    # the original item position, now with structured provenance attached.
    with pytest.raises(DataError) as data_error:
        Order({'order_no': 'O', 'lines': [_detail('A', 'bad')]})
    nested = data_error.value.errors['lines']
    report = getattr(nested, 'result', None)
    if report is not None:
        assert report.errors[0].detail == 0
    else:
        assert 0 in nested


def test_strict_list_still_raises_on_placeholder():
    """The ordinary path is unchanged: None is still converted as before.

    A placeholder is only skipped by the opt-in ingest path.  Strict
    conversion of a non-mapping item still raises as it always did.
    """
    class Plain(Model):
        lines = ListType(ModelType(Detail))

    with pytest.raises(Exception):
        Plain({'lines': [42]})

    # The default path does not silently drop None either.
    assert Plain({'lines': [None]}).lines == [None]


def test_report_exposes_deliverables_and_rejections_separately():
    result = Order.lines.ingest([
        _detail('A', 1), _detail('B', 'bad'), None, _detail('C', 0)])

    deliverables, rejections = result.as_pair()
    assert [detail.code for detail in deliverables] == ['A', 'C']
    assert [(r.detail, 'not int' in r.reason) for r in rejections] == [(1, True)]

    as_dict = result.to_dict()
    assert as_dict['ok'] is False
    assert as_dict['positions'] == [0, 3]
    assert as_dict['skipped'] == [2]
    assert as_dict['errors'][0]['position'] == 1


def test_split_keeps_both_parts_attached_to_original_position():
    result = _ingest_batch([
        {'order_no': 'O-1', 'lines': [
            _detail('MIXED', 100),
            _detail('MIXED', 5),
        ]},
    ])
    good = result.tracked[0]
    other = result.tracked[1]

    goods, freight = split(good, [('goods', 100), ('freight', 25)])
    assert goods.positions == (0, 0)
    assert freight.positions == (0, 0)
    assert goods.kind == 'goods'
    assert freight.kind == 'freight'
    assert goods != freight

    contributions = [ref for ref in freight.source_refs()]
    assert contributions[0]['detail'] == 0
    assert contributions[0]['derived_kind'] == 'freight'


def test_merged_discount_lists_goods_and_discount_sources():
    result = Order.lines.ingest([
        _detail('MIXED', 100),
        _detail('DISCOUNT', -10),
    ])
    goods, _ = split(result.tracked[0], [('goods', 100), ('freight', 0)])
    discount = result.tracked[1]

    net = merge([goods, discount], 90, kind='net_goods')

    refs = net.source_refs()
    assert [ref['detail'] for ref in refs] == [0, 1]
    assert [ref['kind'] for ref in refs] == ['goods', 'lines']
    assert net.contributors[0].is_split is True
    assert net.position == 0


def test_reorganized_positions_map_back_to_distinct_sources():
    result = Order.lines.ingest([
        _detail('A', 1),
        _detail('B', 1),
    ])
    left, right = split(result.tracked[0], [('goods', 1), ('freight', 2)])

    # Two outputs that compare completely equal, from different sources.
    same_a = split(result.tracked[1], [('goods', 1)])[0]
    same_b = split(result.tracked[1], [('freight', 1)])[0]
    assert same_a.value == same_b.value
    assert same_a != same_b

    reorganized = [left, right, same_a, same_b]
    reports = describe_positions(reorganized, [0, 1, 2, 3])
    assert [report['detail'] for report in reports] == [0, 0, 1, 1]
    assert [report['kind'] for report in reports] == [
        'goods', 'freight', 'goods', 'freight']

    with pytest.raises(TypeError):
        # Values cannot select a source, even when their values differ.
        locate([result.tracked[0], result.tracked[1]], result.tracked[0].value)
    assert locate([result.tracked[0], result.tracked[1]],
                  result.tracked[1]) == 1


def test_discounted_item_reports_goods_and_discount_positions():
    result = Order.lines.ingest([
        _detail('MIXED', 100),
        _detail('DISC', -10),
    ])
    goods, _ = split(result.tracked[0], [('goods', 100), ('freight', 0)])
    net = merge([goods, result.tracked[1]], 90, kind='net_goods')

    report = describe_positions([net], [0])[0]
    assert report['is_merged'] is True
    assert report['detail'] == 0
    assert [source['detail'] for source in report['sources']] == [0, 1]
    assert report['contributors'][0][0]['kind'] == 'goods'
    assert report['contributors'][1][0]['kind'] == 'lines'


def test_order_level_conflict_positions_map_to_originals():
    result = Order.lines.ingest([
        _detail('SKU-9', 100, kind='mixed'),
        _detail('SKU-9', 4),
    ])
    goods, freight = split(result.tracked[0], [('goods', 100), ('freight', 4)])
    second = result.tracked[1]
    report_list = [freight, second]

    # The order rule reports positions in the reorganized list only.
    conflict = describe_positions(report_list, [0, 1])

    assert conflict[0]['detail'] == 0
    assert conflict[0]['contributors'] == []
    assert conflict[0]['kind'] == 'freight'
    assert conflict[1]['detail'] == 1
    assert conflict[1]['kind'] == 'lines'


def test_batch_reorder_matches_existing_outputs_by_identity():
    result = _ingest_batch([
        {'order_no': 'O-1', 'lines': [
            _detail('A', 1), _detail('B', 2), _detail('C', 3)]},
        {'order_no': 'O-2', 'lines': [
            _detail('D', 4), None, _detail('E', 5)]},
    ])

    assert [entry.positions for entry in result.tracked] == [
        (0, 0), (0, 1), (0, 2), (1, 0), (1, 2)]
    assert result.items == [
        Detail(_detail('A', 1)), Detail(_detail('B', 2)),
        Detail(_detail('C', 3)), Detail(_detail('D', 4)),
        Detail(_detail('E', 5))]

    assert result.children_for(0) == result.tracked[:3]
    assert result.children_for(1) == result.tracked[3:]


def test_delivered_fragments_remain_traceable():
    result = _ingest_batch([
        {'order_no': 'O-1', 'lines': [
            _detail('MIXED', 50),
            _detail('TAIL', -5),
        ]},
        {'order_no': 'O-2', 'lines': [_detail('ONLY', 7)]},
    ])
    mixed, tail = result.children_for(0)
    only, = result.children_for(1)

    goods, freight = split(mixed, [('goods', 50), ('freight', 9)])
    net = merge([goods, tail], 45, kind='net_goods')

    fragments = [freight, net, only]
    reports = result.describe_positions

    assert [entry.positions for entry in fragments[:2]] == [(0, 0), (0, 0)]
    assert net.positions == (0, 0)
    assert len(reports([0, 1, 2])) == 3
    assert only.provenance.positions == (1, 0)

    # The delivery fragments still point at their own orders.
    assert [entry.provenance.parent_index for entry in fragments[:2]] == [0, 0]
    assert only.provenance.parent_index == 1


def test_ingest_helper_accepts_explicit_context():
    result = Order.lines.ingest([_detail('A', 1)], context={})
    assert [detail.code for detail in result.items] == ['A']


def test_ingest_mode_import_requires_complete_parent():
    order = Order()
    with pytest.raises(Exception) as excinfo:
        order.import_data({'order_no': 'O', 'lines': [
            _detail('A', 1), _detail('B', 'bad')]})

    exception = excinfo.value
    report = getattr(exception, 'result', None)
    if report is not None:
        assert report.positions == [0]
        assert report.errors[0].detail == 1
    else:
        # Model import wraps a list failure in its own DataError; the original
        # child position is still present in the nested error mapping.
        assert 'lines' in exception.errors
        assert 1 in exception.errors['lines']


def test_identical_outputs_trace_to_different_original_records():
    result = Order.lines.ingest([
        _detail('SAME', 7),
        _detail('SAME', 7),
    ])
    left = split(result.tracked[0], [('goods', 7)])[0]
    right = split(result.tracked[1], [('goods', 7)])[0]

    # The converted values, business numbers and derived kinds all match.
    assert left.value == right.value
    assert left.kind == right.kind
    assert left != right

    reports = describe_positions([left, right], [0, 1])
    assert [report['detail'] for report in reports] == [0, 1]
    assert [report['sources'][0]['detail'] for report in reports] == [0, 1]


def test_three_order_acceptance_end_to_end():
    batch = _ingest_batch([
        # First order: placeholder, a valid zero and an illegal quantity.
        {'order_no': 'O-1', 'lines': [
            None, _detail('A', 0), _detail('B', 'oops')]},
        # Second order: all single conversions pass; mixed item and two
        # duplicate business numbers.
        {'order_no': 'O-2', 'lines': [
            _detail('M', 100, 'mixed'), _detail('S', 5), _detail('S', 0)]},
        # Third order: placeholder and a mixed item that can be split.
        {'order_no': 'O-3', 'lines': [
            None, _detail('M', 40, 'mixed'), _detail('T', -4, 'discount')]},
    ])

    assert batch.ok is False
    assert batch.rejected_parents == [0]
    assert batch.skipped == []
    assert [len(parent.items) for parent in batch.results] == [0, 3, 2]
    assert batch.results[0].errors[0].detail == 2
    assert batch.results[2].skipped == [0]
    assert [entry.positions for entry in batch.results[2].tracked] == [
        (2, 1), (2, 2)]

    # Reorder the second order by type; the rule reports only positions in
    # the reorganized list.
    parent = batch.result_for(1)
    mixed, same_left, same_right = parent.tracked
    goods, freight = split(mixed, [('goods', 100), ('freight', 100)])
    reorganized = [freight, same_left, goods, same_right]
    conflict = describe_positions(reorganized, [0, 2])
    assert [report['detail'] for report in conflict] == [0, 0]
    assert [report['kind'] for report in conflict] == ['freight', 'goods']
    # Both derived parts still name the same original mixed input.
    assert conflict[0]['sources'][0]['detail'] == 0
    assert conflict[1]['sources'][0]['detail'] == 0
    # The two equal-valued, same-code siblings still map to different records.
    assert same_left != same_right
    assert (same_left.detail_index, same_right.detail_index) == (1, 2)

    # The third order's merged discount names both contributors.
    third = batch.result_for(2)
    mixed3, discount = third.tracked
    goods3, freight3 = split(mixed3, [('goods', 40), ('freight', 6)])
    net = merge([goods3, discount], 36, kind='net_goods')
    discounted = describe_positions([freight3, net], [1])[0]
    assert discounted['is_merged'] is True
    assert [(source['detail'], source['kind'])
            for source in discounted['sources']] == [(1, 'goods'), (2, 'lines')]

    # Move the placeholder; only the coordinates that must move change.
    moved = _ingest_batch([
        {'order_no': 'O-3', 'lines': [
            _detail('M', 40, 'mixed'), None, _detail('T', -4, 'discount')]},
        {'order_no': 'O-1', 'lines': [
            _detail('A', 0), _detail('B', 'oops'), None]},
    ])
    assert moved.result_for(0).positions == [0, 2]
    assert moved.result_for(0).items == batch.result_for(2).items
    assert moved.result_for(1).errors[0].detail == 1

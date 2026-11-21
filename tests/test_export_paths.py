# -*- coding: utf-8 -*-
"""
Tests for per-path export roles, role fallback and cycle-safe references.

The scenario mirrors an order whose address object is exported twice, once
for the purchasing master list and once for an external review desk.  The
review desk never receives contacts or internal address ids, pickup and
delivery choose address roles independently, and a recursive "forward
address" edge can point back into the graph already visited.
"""

from __future__ import unicode_literals

import pytest

from schematics.models import Model
from schematics.types import StringType, IntType
from schematics.types.compound import ModelType, ListType
from schematics.transforms import blacklist, whitelist
from schematics.export import (
    ExportPath, RoleError, field_allowed, visible_reference,
    default_cycle_handler)


class Address(Model):
    internal_id = IntType()
    public_no = StringType()
    contact = StringType()
    line = StringType()
    forward = ModelType('Address')

    class Options:
        roles = {
            # The review desk never sees contacts or internal ids.
            'review': blacklist('contact', 'internal_id'),
            # The purchasing master list is allowed the full record.
            'procurement': whitelist(
                'internal_id', 'public_no', 'contact', 'line', 'forward'),
        }


class WarehouseAddress(Address):
    """One object usable through both the pickup and delivery paths."""
    class Options:
        roles = {
            'pickup_default': whitelist('public_no', 'line'),
            'delivery_default': whitelist('public_no', 'line', 'forward'),
        }


class PickupAddress(Address):
    class Options:
        roles = {'pickup_default': whitelist('public_no', 'line')}


class StandardDelivery(Address):
    class Options:
        roles = {'delivery_default': whitelist('public_no', 'line', 'forward')}


class NoRoleDelivery(Address):
    """Reachable through the delivery path but lacks ``delivery_default``."""
    class Options:
        roles = {'delivery_fallback': whitelist('public_no', 'line')}


class InternalOnly(StandardDelivery):
    """Role permits the forward edge but not a public address number."""
    class Options:
        roles = {'delivery_default': whitelist('line', 'forward')}


class Order(Model):
    order_no = StringType()
    internal_note = StringType()
    pickup = ModelType(Address)
    delivery = ModelType(Address)

    class Options:
        roles = {
            'review': blacklist('internal_note'),
            'procurement': blacklist(),
        }


review_paths = {
    'pickup': ExportPath('pickup', {
        'WarehouseAddress': 'pickup_default',
        'PickupAddress': 'pickup_default'}),
    'delivery': ExportPath('delivery', {
        'WarehouseAddress': 'delivery_default',
        'StandardDelivery': 'delivery_default',
        'NoRoleDelivery': 'delivery_default'}),
    'forward': ExportPath('forward', {
        'WarehouseAddress': 'delivery_default',
        'StandardDelivery': 'delivery_default',
        'NoRoleDelivery': 'delivery_default'}),
}

procurement_paths = {
    'pickup': ExportPath('pickup', {
        'WarehouseAddress': 'procurement',
        'PickupAddress': 'procurement'}),
    'delivery': ExportPath('delivery', {
        'WarehouseAddress': 'procurement',
        'StandardDelivery': 'procurement',
        'NoRoleDelivery': 'procurement'}),
    'forward': ExportPath('forward', {
        'WarehouseAddress': 'procurement',
        'StandardDelivery': 'procurement',
        'NoRoleDelivery': 'procurement'}),
}


def reference_handler(instance, export_path, role_name, context):
    return {'ref': visible_reference(
        instance, role_name, ['public_no', 'internal_id', 'line'])}


def number_handler(instance, export_path, role_name, context):
    return {'ref': visible_reference(
        instance, role_name, ['public_no', 'internal_id'])}


def make_shared_order():
    shared = WarehouseAddress({
        'internal_id': 7, 'public_no': 'P-7',
        'contact': 'Alice', 'line': 'Main St 1'})
    return Order({
        'order_no': 'O-1', 'internal_note': 'buyer remark',
        'pickup': shared, 'delivery': shared})


def test_shared_address_produces_independent_fields_per_path():
    order = make_shared_order()

    data = order.to_primitive(role='review', export_paths=review_paths)

    assert data['order_no'] == 'O-1'
    assert data['pickup'] == {'public_no': 'P-7', 'line': 'Main St 1'}
    assert data['delivery'] == {
        'public_no': 'P-7', 'line': 'Main St 1', 'forward': None}
    # Normal sharing of one object is never reported as a cycle.
    assert 'ref' not in str(data)


def test_outlet_field_caps_apply_on_top_of_path_roles():
    order = make_shared_order()

    review = order.to_primitive(role='review', export_paths=review_paths)
    procurement = order.to_primitive(
        role='procurement', export_paths=procurement_paths)

    # The review desk cannot see order-internal notes, contacts or ids.
    assert 'internal_note' not in review
    assert 'contact' not in str(review)
    assert 'internal_id' not in str(review)
    assert 'Alice' not in str(review)

    # The purchasing master list keeps the internal fields, on the order
    # itself and on the address reached through its paths.
    assert procurement['internal_note'] == 'buyer remark'
    assert procurement['pickup']['contact'] == 'Alice'
    assert procurement['pickup']['internal_id'] == 7
    assert procurement['delivery']['public_no'] == 'P-7'


def test_strict_missing_role_errors_only_on_the_visible_path():
    broken = NoRoleDelivery({
        'internal_id': 9, 'public_no': 'D-9',
        'contact': 'Eve', 'line': 'Yard 4'})
    healthy = PickupAddress({
        'internal_id': 3, 'public_no': 'K-3',
        'contact': 'Joe', 'line': 'Dock 1'})
    order = Order({
        'order_no': 'O-10', 'pickup': healthy, 'delivery': broken})

    with pytest.raises(RoleError) as exc_info:
        order.to_primitive(
            role='review', export_paths=review_paths,
            raise_error_on_role=True)

    error = exc_info.value
    assert error.model_name == 'NoRoleDelivery'
    assert error.role_name == 'delivery_default'
    assert error.path_name == 'delivery'
    # Error text carries identifiers for locating the edge, never data.
    assert 'Eve' not in str(error) and '9' not in str(error)
    assert isinstance(error, ValueError)


def test_fallback_uses_subclass_default_role_and_keeps_field_caps():
    broken = NoRoleDelivery({
        'internal_id': 9, 'public_no': 'D-9',
        'contact': 'Eve', 'line': 'Yard 4'})
    order = Order({'order_no': 'O-9', 'delivery': broken})

    fallback_paths = dict(review_paths)
    fallback_paths['delivery'] = ExportPath(
        'delivery', {'NoRoleDelivery': 'delivery_default'},
        fallback_roles={'NoRoleDelivery': 'delivery_fallback'})

    data = order.to_primitive(
        role='review', export_paths=fallback_paths,
        raise_error_on_role=False)

    assert data['delivery'] == {'public_no': 'D-9', 'line': 'Yard 4'}
    assert 'contact' not in str(data)
    assert 'internal_id' not in str(data)


def test_retry_after_strict_error_matches_direct_fallback_run():
    broken = NoRoleDelivery({
        'internal_id': 9, 'public_no': 'D-9',
        'contact': 'Eve', 'line': 'Yard 4'})
    order = Order({'order_no': 'O-9', 'delivery': broken})

    fallback_paths = dict(review_paths)
    fallback_paths['delivery'] = ExportPath(
        'delivery', {'NoRoleDelivery': 'delivery_default'},
        fallback_roles={'NoRoleDelivery': 'delivery_fallback'})

    with pytest.raises(RoleError):
        order.to_primitive(
            role='review', export_paths=fallback_paths,
            raise_error_on_role=True)

    retried = order.to_primitive(
        role='review', export_paths=fallback_paths,
        raise_error_on_role=False)
    direct = order.to_primitive(
        role='review', export_paths=fallback_paths,
        raise_error_on_role=False)

    assert retried == direct
    assert retried['delivery'] == {'public_no': 'D-9', 'line': 'Yard 4'}


def wire_cycle(first, second):
    # Import conversion cannot ingest a cycle, so the back edge is wired
    # after the model instances have been constructed.
    first.forward = second
    second.forward = first


def test_forward_cycle_stops_at_closing_edge_with_checked_reference():
    first = StandardDelivery({
        'internal_id': 1, 'public_no': 'A-1',
        'contact': 'C1', 'line': 'L1'})
    second = StandardDelivery({
        'internal_id': 2, 'public_no': 'A-2',
        'contact': 'C2', 'line': 'L2'})
    wire_cycle(first, second)

    order = Order({'order_no': 'O-3'})
    order.delivery = first

    data = order.to_primitive(
        role='review', export_paths=review_paths,
        cycle_handler=number_handler)

    assert data['delivery']['public_no'] == 'A-1'
    assert data['delivery']['forward']['public_no'] == 'A-2'
    assert data['delivery']['forward']['forward'] == {
        'ref': {'public_no': 'A-1'}}
    # The review desk cannot learn contacts or internal ids from the cycle.
    assert 'internal_id' not in str(data)
    assert 'contact' not in str(data)
    assert 'C1' not in str(data)


def test_cycle_reference_omits_number_when_role_forbids_it():
    first = InternalOnly({
        'internal_id': 10, 'public_no': 'B-1',
        'contact': 'x', 'line': 'LL'})
    second = InternalOnly({
        'internal_id': 11, 'public_no': 'B-2',
        'contact': 'y', 'line': 'MM'})
    wire_cycle(first, second)

    order = Order({'order_no': 'O-4'})
    order.delivery = first

    data = order.to_primitive(
        role='review', export_paths=review_paths,
        cycle_handler=reference_handler)

    reference = data['delivery']['forward']['forward']['ref']
    assert reference == {'line': 'LL'}
    assert 'public_no' not in reference
    assert 'internal_id' not in str(data)
    assert 'contact' not in str(data)


def test_cycle_reference_falls_back_to_internal_id_without_a_number():
    # On the procurement outlet a role may permit the internal id while
    # forbidding the public number.  The reference then carries the internal
    # id but must not invent a number.
    class InternalIdDelivery(Address):
        class Options:
            roles = {'procurement_delivery': whitelist(
                'internal_id', 'line', 'forward')}

    first = InternalIdDelivery({
        'internal_id': 31, 'public_no': 'P-31',
        'contact': 'z', 'line': 'QQ'})
    second = InternalIdDelivery({
        'internal_id': 32, 'public_no': 'P-32',
        'contact': 'w', 'line': 'RR'})
    wire_cycle(first, second)

    order = Order({'order_no': 'O-7'})
    order.delivery = first

    paths = {
        'delivery': ExportPath('delivery', {
            'InternalIdDelivery': 'procurement_delivery'}),
        'forward': ExportPath('forward', {
            'InternalIdDelivery': 'procurement_delivery'}),
    }

    data = order.to_primitive(
        role='procurement', export_paths=paths,
        cycle_handler=number_handler)

    reference = data['delivery']['forward']['forward']['ref']
    assert reference == {'internal_id': 31}
    assert 'public_no' not in reference


def test_default_cycle_handler_prevents_infinite_recursion():
    first = StandardDelivery({'public_no': 'A-1', 'line': 'L1'})
    second = StandardDelivery({'public_no': 'A-2', 'line': 'L2'})
    wire_cycle(first, second)

    order = Order({'order_no': 'O-5'})
    order.delivery = first

    data = order.to_primitive(
        role='review', export_paths=review_paths)

    assert data['delivery']['forward']['forward'] == {'cycle': True}


def test_cycle_edge_uses_fallback_role_and_never_leaks_hidden_fields():
    class CyclicNoRoleDelivery(Address):
        class Options:
            roles = {'delivery_fallback': whitelist('public_no', 'forward')}

    cyclic = CyclicNoRoleDelivery({
        'internal_id': 41, 'public_no': 'N-41',
        'contact': 'Eve', 'line': 'Yard 9'})
    cyclic.forward = cyclic

    order = Order({'order_no': 'O-8'})
    order.delivery = cyclic

    paths = {
        'delivery': ExportPath(
            'delivery', {'CyclicNoRoleDelivery': 'delivery_default'},
            fallback_roles={'CyclicNoRoleDelivery': 'delivery_fallback'}),
        'forward': ExportPath(
            'forward', {'CyclicNoRoleDelivery': 'delivery_default'},
            fallback_roles={'CyclicNoRoleDelivery': 'delivery_fallback'}),
    }

    with pytest.raises(RoleError) as exc_info:
        order.to_primitive(
            role='review', export_paths=paths,
            raise_error_on_role=True, cycle_handler=number_handler)
    assert exc_info.value.path_name == 'delivery'

    seen_roles = []

    def handler(instance, export_path, role_name, context):
        seen_roles.append(role_name)
        return {'ref': visible_reference(
            instance, role_name, ['public_no', 'internal_id'])}

    data = order.to_primitive(
        role='review', export_paths=paths,
        raise_error_on_role=False, cycle_handler=handler)

    assert seen_roles == ['delivery_fallback']
    assert data['delivery']['forward'] == {'ref': {'public_no': 'N-41'}}
    assert 'internal_id' not in str(data)
    assert 'contact' not in str(data)
    assert 'Eve' not in str(data)


def test_repeated_and_reversed_export_runs_are_stable():
    order = make_shared_order()

    def review_strict():
        return order.to_primitive(
            role='review', export_paths=review_paths,
            raise_error_on_role=True)

    def review_relaxed():
        return order.to_primitive(
            role='review', export_paths=review_paths,
            raise_error_on_role=False)

    def procurement():
        return order.to_primitive(
            role='procurement', export_paths=procurement_paths)

    forward = [review_strict(), review_relaxed(), procurement()]
    reverse = [procurement(), review_relaxed(), review_strict()]

    assert forward == list(reversed(reverse))


def test_strict_relaxed_procurement_sequence_survives_reverse_order():
    broken = NoRoleDelivery({
        'internal_id': 9, 'public_no': 'D-9',
        'contact': 'Eve', 'line': 'Yard 4'})
    order = Order({'order_no': 'O-9', 'delivery': broken})

    fallback_paths = dict(review_paths)
    fallback_paths['delivery'] = ExportPath(
        'delivery', {'NoRoleDelivery': 'delivery_default'},
        fallback_roles={'NoRoleDelivery': 'delivery_fallback'})

    def strict_review():
        return order.to_primitive(
            role='review', export_paths=fallback_paths,
            raise_error_on_role=True)

    def relaxed_review():
        return order.to_primitive(
            role='review', export_paths=fallback_paths,
            raise_error_on_role=False)

    def procurement():
        return order.to_primitive(
            role='procurement', export_paths=procurement_paths,
            raise_error_on_role=True)

    # Strict review is the only run that fails, and it fails on exactly the
    # visible delivery edge rather than the whole outlet.
    with pytest.raises(RoleError) as first_error:
        strict_review()
    assert first_error.value.path_name == 'delivery'

    relaxed_forward = relaxed_review()
    procurement_forward = procurement()

    # Reverse order: procurement first, then the fallback review, then the
    # failing strict review again.
    procurement_reverse = procurement()
    relaxed_reverse = relaxed_review()
    with pytest.raises(RoleError) as second_error:
        strict_review()

    assert procurement_forward == procurement_reverse
    assert relaxed_forward == relaxed_reverse
    assert str(first_error.value) == str(second_error.value)

    # Retrying the failed strict run with fallback must equal a direct run.
    assert relaxed_review() == relaxed_forward
    assert relaxed_forward['delivery'] == {'public_no': 'D-9', 'line': 'Yard 4'}


def test_legacy_entries_without_paths_keep_original_role_behavior():
    order = make_shared_order()

    legacy = order.to_primitive(role='review')
    explicit = order.to_primitive(
        role='review', export_paths=None, cycle_handler=None)

    assert legacy == explicit
    assert 'contact' not in legacy['pickup']
    assert 'internal_id' not in legacy['pickup']

    full = order.to_primitive()
    assert full['pickup']['contact'] == 'Alice'
    assert full['pickup']['internal_id'] == 7


def test_path_role_inherits_through_subclass_mro():
    first = InternalOnly({'public_no': 'B-1', 'line': 'LL'})
    second = InternalOnly({'public_no': 'B-2', 'line': 'MM'})
    wire_cycle(first, second)

    order = Order({'order_no': 'O-6'})
    order.delivery = first

    data = order.to_primitive(
        role='review', export_paths=review_paths,
        cycle_handler=reference_handler)

    # InternalOnly has no path entry of its own; it inherits the
    # StandardDelivery entry and its own overriding role still applies.
    assert data['delivery']['forward']['forward'] == {
        'ref': {'line': 'LL'}}


def test_paths_work_inside_list_models():
    shared = WarehouseAddress({
        'internal_id': 7, 'public_no': 'P-7',
        'contact': 'Alice', 'line': 'Main St 1'})

    class Batch(Model):
        deliveries = ListType(ModelType(Address))
        class Options:
            roles = {'review': blacklist()}

    batch = Batch({'deliveries': [shared, shared]})
    data = batch.to_primitive(
        role='review',
        export_paths={'deliveries': review_paths['delivery']},
        cycle_handler=reference_handler)

    assert len(data['deliveries']) == 2
    assert data['deliveries'][0] == data['deliveries'][1]
    assert 'ref' not in str(data)
    assert 'contact' not in str(data)


def test_to_native_honors_export_paths():
    order = make_shared_order()

    data = order.to_native(role='review', export_paths=review_paths)

    assert data['pickup'] == {'public_no': 'P-7', 'line': 'Main St 1'}
    assert 'contact' not in str(data)
    assert 'internal_id' not in str(data)


def test_field_allowed_and_visible_reference_helpers():
    address = WarehouseAddress({
        'internal_id': 7, 'public_no': 'P-7',
        'contact': 'Alice', 'line': 'Main St 1'})

    assert field_allowed(address, 'public_no', 'review') is True
    assert field_allowed(address, 'contact', 'review') is False
    assert field_allowed(address, 'internal_id', 'review') is False

    reference = visible_reference(
        address, 'review', ['public_no', 'internal_id', 'contact'])
    assert reference == {'public_no': 'P-7'}
    assert default_cycle_handler(address, None, 'review', None) == {
        'cycle': True}

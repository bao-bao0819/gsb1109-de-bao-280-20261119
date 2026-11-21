# -*- coding: utf-8 -*-
import pytest

from schematics.models import Model
from schematics.types import IntType, StringType
from schematics.types.compound import ModelType
from schematics.transforms import blacklist
from schematics.diff import import_changes, ChangeConflictError
from schematics.exceptions import DataError
from schematics.undefined import Undefined


sequence_calls = []


def next_sequence():
    value = 1000 + len(sequence_calls) + 1
    sequence_calls.append(value)
    return value


class Address(Model):
    city = StringType(deserialize_from=['city_name'])
    postal_code = StringType(deserialize_from=['zip'])
    notes = StringType()

    class Options:
        roles = {'outgoing': blacklist('notes')}


class Order(Model):
    address = ModelType(Address)
    contact = StringType(deserialize_from=['contact_ref'])
    priority = IntType(deserialize_from=['level'])
    sequence = IntType(default=next_sequence)

    class Options:
        roles = {'outgoing': blacklist('contact')}


@pytest.fixture(autouse=True)
def reset_sequence_calls():
    sequence_calls[:] = []
    yield
    sequence_calls[:] = []


def make_order():
    return Order({
        'address': {
            'city': 'Old Town',
            'postal_code': '100000',
            'notes': 'legacy note',
        },
        'contact': 'old-contact',
    }, init=False)


def test_single_sided_city_update_keeps_postal_code():
    order = make_order()

    payload = {
        'address': {'city_name': 'New Town', 'notes': None},
        'contact': 'secret-contact',
    }

    internal = order.import_changes(payload)
    assert internal == {
        'address': {'city': 'New Town', 'notes': None},
        'contact': 'secret-contact',
    }

    outgoing = order.import_changes(payload, role='outgoing')
    assert outgoing == {'address': {'city': 'New Town'}}


def test_complementary_leaves_from_two_address_keys_merge():
    order = make_order()

    changes = order.import_changes({
        'address': {'city': 'New Town', 'zip': '200000'},
    })

    assert changes == {
        'address': {'city': 'New Town', 'postal_code': '200000'},
    }


def test_same_leaf_through_two_aliases_is_delivered_once():
    order = make_order()

    changes = order.import_changes({
        'address': {'city': 'New Town', 'city_name': 'New Town'},
    })

    assert changes == {'address': {'city': 'New Town'}}


def test_different_input_spellings_equal_after_conversion_collapse():
    order = make_order()

    changes = order.import_changes({'priority': 5, 'level': '5'})

    assert changes == {'priority': 5}


def test_conflicting_leaf_names_both_original_input_paths():
    order = make_order()

    with pytest.raises(ChangeConflictError) as exc_info:
        order.import_changes({
            'address': {'city': 'New Town', 'city_name': 'Other Town'},
        })

    conflict = exc_info.value.errors['address']['city']['conflict']
    assert conflict['sources'] == ['address.city', 'address.city_name']
    assert conflict['values'] == ['New Town', 'Other Town']


def test_hidden_conflict_rejects_without_leaking_hidden_values():
    order = make_order()
    payload = {'contact': 'hidden-one', 'contact_ref': 'hidden-two'}

    with pytest.raises(ChangeConflictError) as internal_error:
        order.import_changes(payload)
    internal_conflict = internal_error.value.errors['contact']['conflict']
    assert internal_conflict['sources'] == ['contact', 'contact_ref']
    assert internal_conflict['values'] == ['hidden-one', 'hidden-two']

    with pytest.raises(ChangeConflictError) as outgoing_error:
        order.import_changes(payload, role='outgoing')
    outgoing_conflict = outgoing_error.value.errors['contact']['conflict']
    assert outgoing_conflict['sources'] == ['contact', 'contact_ref']
    assert 'values' not in outgoing_conflict
    rendered = str(outgoing_error.value)
    assert 'hidden-one' not in rendered
    assert 'hidden-two' not in rendered


def test_hidden_nested_conflict_rejects_without_leaking():
    order = make_order()
    mapping = {'address': 'address_alt'}
    payload = {
        'address': {'notes': 'a'},
        'address_alt': {'notes': 'b'},
    }

    with pytest.raises(ChangeConflictError) as internal_error:
        order.import_changes(payload, deserialize_mapping=mapping)
    internal_conflict = \
        internal_error.value.errors['address']['notes']['conflict']
    assert internal_conflict['sources'] == [
        'address.notes', 'address_alt.notes']
    assert internal_conflict['values'] == ['a', 'b']

    with pytest.raises(ChangeConflictError) as outgoing_error:
        order.import_changes(
            payload, deserialize_mapping=mapping, role='outgoing')
    outgoing_conflict = \
        outgoing_error.value.errors['address']['notes']['conflict']
    assert outgoing_conflict['sources'] == [
        'address.notes', 'address_alt.notes']
    assert 'values' not in outgoing_conflict
    rendered = str(outgoing_error.value)
    assert '"a"' not in rendered
    assert '"b"' not in rendered


def test_hidden_field_errors_never_carry_hidden_values():
    order = make_order()

    with pytest.raises(DataError) as exc_info:
        order.import_changes({'priority': 'not-a-number'}, role='outgoing')

    # priority is visible, so the real conversion error stays intact.
    assert 'priority' in exc_info.value.errors

    class GuardedOrder(Order):
        guarded = StringType(deserialize_from=['guarded_alias'])

        class Options:
            roles = {'outgoing': blacklist('guarded')}

    guarded_order = GuardedOrder(make_order().to_native(), init=False)
    with pytest.raises(DataError) as hidden_error:
        guarded_order.import_changes(
            {'guarded': object()}, role='outgoing')
    assert hidden_error.value.errors == {
        'guarded': 'Invalid value for a field not available in this role'}


def test_role_call_order_does_not_change_outcome():
    order_a = make_order()
    order_b = make_order()
    hidden_conflict = {'contact': 'hidden-one', 'contact_ref': 'hidden-two'}
    visible_aliases = {
        'address': {'city': 'New Town', 'city_name': 'New Town'},
    }

    for payload in (hidden_conflict, visible_aliases):
        first_internal, first_outgoing = [], []
        second_internal, second_outgoing = [], []

        for bucket, call in (
                (first_internal, lambda: order_a.import_changes(payload)),
                (first_outgoing,
                 lambda: order_a.import_changes(payload, role='outgoing')),
                (second_outgoing,
                 lambda: order_b.import_changes(payload, role='outgoing')),
                (second_internal,
                 lambda: order_b.import_changes(payload))):
            try:
                bucket.append(('ok', call()))
            except ChangeConflictError as exc:
                bucket.append(('conflict', exc.errors))

        assert first_internal == second_internal
        assert first_outgoing == second_outgoing


def test_omitted_fields_and_defaults_never_enter_change_list():
    order = make_order()

    changes = order.import_changes({'address': {'city': 'New Town'}})

    assert changes == {'address': {'city': 'New Town'}}
    assert 'sequence' not in changes
    assert 'postal_code' not in changes['address']
    assert sequence_calls == []


def test_explicit_null_deletes_but_null_on_absent_leaf_is_omitted():
    order = make_order()

    changes = order.import_changes({
        'address': {'city': 'New Town', 'notes': None, 'zip': None},
        'sequence': None,
    })

    assert changes == {
        'address': {'city': 'New Town', 'notes': None,
                    'postal_code': None},
    }


def test_unchanged_values_are_not_listed():
    order = make_order()

    assert order.import_changes({
        'address': {'city': 'Old Town', 'postal_code': '100000'},
        'contact': 'old-contact',
    }) == {}


def test_generating_or_rejecting_change_list_keeps_order_untouched():
    order = make_order()
    before = order.to_primitive()

    order.import_changes({
        'address': {'city_name': 'New Town', 'notes': None},
        'contact': 'secret-contact',
    })
    with pytest.raises(ChangeConflictError):
        order.import_changes(
            {'address': {'city': 'A', 'city_name': 'B'}}, role='outgoing')

    assert order.to_primitive() == before
    assert order.address.city == 'Old Town'
    assert order.address.postal_code == '100000'
    assert order.address.notes == 'legacy note'
    assert order.contact == 'old-contact'
    assert 'sequence' not in order
    assert sequence_calls == []


def test_module_level_entry_point_matches_model_method():
    order = make_order()
    payload = {'address': {'city_name': 'New Town'}}

    assert import_changes(Order._schema, order, payload) == \
        order.import_changes(payload)
    assert import_changes(
        Order._schema, order, payload, role='outgoing') == \
        order.import_changes(payload, role='outgoing')


def test_full_import_and_full_export_keep_original_contract():
    full_input = {
        'address': {'city': 'New Town', 'postal_code': '200000'},
        'contact': 'secret-contact',
        'priority': '7',
    }

    order = Order(full_input)

    assert order.priority == 7
    assert sequence_calls == [order.sequence]
    assert order.to_primitive() == {
        'address': {'city': 'New Town', 'postal_code': '200000',
                    'notes': None},
        'contact': 'secret-contact',
        'priority': 7,
        'sequence': order.sequence,
    }
    assert order.to_primitive(role='outgoing') == {
        'address': {'city': 'New Town', 'postal_code': '200000'},
        'priority': 7,
        'sequence': order.sequence,
    }

    order.import_data({'priority': 9}, recursive=True)
    assert order.priority == 9
    assert order.address.city == 'New Town'


def test_undefined_is_not_part_of_change_list_contract():
    order = make_order()
    changes = order.import_changes({'address': {'city': 'New Town'}})
    assert all(value is not Undefined
               for value in changes['address'].values())

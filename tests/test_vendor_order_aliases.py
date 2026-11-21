# -*- coding: utf-8 -*-
"""
Regression tests for inherited input aliases combined with runtime field
registration, polymorphic import and nested validation.

Scenario
--------
A vendor order hierarchy is built while an access plugin is still loading:

* ``SupplierOrder`` (parent) declares a legacy price field that accepts both an
  old inbound key (``deserialize_from``) and a serialized key
  (``serialized_name``).
* Mid-startup the plugin calls ``_append_field`` on the parent schema to
  register ``legacy_price`` (old key ``archived_price``, serialized key
  ``legacyPrice``).
* ``AlphaOrder`` is defined *before* that registration and prices its orders as
  integers; ``BetaOrder`` is defined *afterwards* and must keep leading zeros in
  order numbers / prices, so its price is a string.
* ``OrderBatch.orders`` is a ``PolyModelType`` rooted at the parent, so real
  subclass instances and raw dicts share one parent-typed branch.

The assertions below pin the actual, reproducible behavior of the pinned
library version. Each non-obvious outcome is tagged with the phase in which it
manifests:

* IMPORT  - conversion / strict import (``Model(...)``, ``import_data``)
* VALIDATE - ``Model.validate()``
* EXPORT  - ``to_primitive()`` / ``serialize()``

Three plausible-but-wrong assumptions are explicitly disproven, each with a
case that a top-level-only assertion would miss:

1. A field registered on the parent after a child schema was created is NOT
   backported to that child (IMPORT: strict import rejects the new key).
2. A bare dict equal to a subclass export does NOT travel through the subclass
   fields; without a polymorphic discriminator it falls back to the parent
   (IMPORT/EXPORT: leading zeros are lost and subclass-only keys are dropped).
3. A nested validation error does NOT preserve the already processed sibling
   branches on the parent instance (VALIDATE: the whole list is dropped, while
   references captured before the call still hold their old values).
"""

import pytest

from schematics.exceptions import DataError, UndefinedValueError
from schematics.models import Model
from schematics.types import IntType, StringType
from schematics.types.compound import ListType, PolyModelType


LEGACY_FIELD = 'legacy_price'
LEGACY_OLD_KEY = 'archived_price'
LEGACY_SERIAL_KEY = 'legacyPrice'

PRICE_OLD_KEY = 'old_price'
PRICE_SERIAL_KEY = 'priceCode'

ALPHA_COST_KEY = 'alpha_cost'
BETA_COST_KEY = 'beta_cost'

NESTED_MAPPING = {
    'model_mapping': {
        'orders': {
            'price': [ALPHA_COST_KEY, BETA_COST_KEY],
        },
    },
}

NESTED_PAYLOAD = {
    'orders': [
        # Beta branch: illegal new price, trustworthy legacy value on the
        # field registered at runtime.
        {'order_no': '0007', BETA_COST_KEY: 'oops', LEGACY_OLD_KEY: 40},
        # Alpha branch: serialized key and mapped key carry conflicting values.
        {'order_no': '0008', PRICE_SERIAL_KEY: 99, ALPHA_COST_KEY: 5},
    ],
}

BETA_RECORD = {
    'order_no': '0007',
    'price': '0042',
    LEGACY_OLD_KEY: 40,
    'beta_note': 'n',
}


def build_vendor_schemas(register_first):
    """Build the order hierarchy.

    ``register_first`` swaps the order of the plugin registration relative to
    the subclass definitions, providing the control run:

    * False (integration order): AlphaOrder is created before the plugin
      registers ``legacy_price`` on the parent; BetaOrder is created after.
    * True (control order): the registration happens before either subclass is
      created, so both children inherit the field.
    """

    class SupplierOrder(Model):
        order_no = StringType()
        price = IntType(
            deserialize_from=[PRICE_OLD_KEY],
            serialized_name=PRICE_SERIAL_KEY,
        )

    if register_first:
        SupplierOrder._append_field(
            LEGACY_FIELD,
            IntType(
                deserialize_from=[LEGACY_OLD_KEY],
                serialized_name=LEGACY_SERIAL_KEY,
            ),
        )

    class AlphaOrder(SupplierOrder):
        # Integer pricing; overrides the parent alias set with its own field.
        price = IntType(serialized_name=PRICE_SERIAL_KEY)

        @classmethod
        def _claim_polymorphic(cls, data):
            if isinstance(data, Model):
                return isinstance(data, cls)
            return ALPHA_COST_KEY in data

    if not register_first:
        # Plugin startup step: register a brand-new field on the parent only.
        SupplierOrder._append_field(
            LEGACY_FIELD,
            IntType(
                deserialize_from=[LEGACY_OLD_KEY],
                serialized_name=LEGACY_SERIAL_KEY,
            ),
        )

    class BetaOrder(SupplierOrder):
        # String pricing so leading zeros on order/price values survive.
        beta_note = StringType()
        price = StringType(regex=r'^\d+$')

        @classmethod
        def _claim_polymorphic(cls, data):
            if isinstance(data, Model):
                return isinstance(data, cls)
            return BETA_COST_KEY in data

    class OrderBatch(Model):
        orders = ListType(PolyModelType(SupplierOrder))

    return SupplierOrder, AlphaOrder, BetaOrder, OrderBatch


@pytest.fixture
def vendor_schemas():
    return build_vendor_schemas(register_first=False)


@pytest.fixture
def control_schemas():
    return build_vendor_schemas(register_first=True)


def test_strict_import_on_all_three_classes(vendor_schemas):
    # IMPORT: every class must pass one strict import before any failure is
    # exercised. The parent accepts both the legacy inbound key and the
    # serialized key for the same field.
    SupplierOrder, AlphaOrder, BetaOrder, _ = vendor_schemas

    via_old_key = SupplierOrder(
        {'order_no': '9', PRICE_OLD_KEY: 7}, strict=True)
    via_serial_key = SupplierOrder(
        {'order_no': '9', PRICE_SERIAL_KEY: 8}, strict=True)
    assert via_old_key.price == 7
    assert via_serial_key.price == 8
    assert via_old_key.to_primitive()[PRICE_SERIAL_KEY] == 7

    alpha = AlphaOrder({'order_no': '0008', PRICE_SERIAL_KEY: 5}, strict=True)
    assert isinstance(alpha.price, int) and alpha.price == 5

    beta = BetaOrder(dict(BETA_RECORD), strict=True)
    # EXPORT: the string field preserves the leading zeros end to end.
    assert beta.price == '0042'
    assert beta.to_primitive()['price'] == '0042'
    assert beta.legacy_price == 40


def test_registered_parent_field_is_not_backported_to_existing_child(vendor_schemas):
    # Assumption 1 (IMPORT): the plugin-registered field exists on the parent
    # and on the child created afterwards, but not on the child created first.
    SupplierOrder, AlphaOrder, BetaOrder, _ = vendor_schemas

    assert LEGACY_FIELD in SupplierOrder._schema.fields
    assert LEGACY_FIELD in BetaOrder._schema.fields
    assert LEGACY_FIELD not in AlphaOrder._schema.fields

    # A top-level-only check on the parent would hide the gap: the parent
    # accepts the old key under strict import.
    parent = SupplierOrder(
        {'order_no': '1', LEGACY_OLD_KEY: 40}, strict=True)
    assert parent.legacy_price == 40

    # The already-established child schema rejects the very same key.
    with pytest.raises(DataError) as excinfo:
        AlphaOrder(
            {'order_no': '0008', LEGACY_OLD_KEY: 40}, strict=True)

    error = excinfo.value
    assert error.to_primitive() == {LEGACY_OLD_KEY: 'Rogue field'}
    # Literal partial data: the recognized fields are still converted.
    assert error.partial_data == {'order_no': '0008', 'price': None}

    # Non-strict import silently ignores the unknown key instead of failing;
    # the established child schema still does not know the registered field.
    loose_alpha = AlphaOrder(
        {'order_no': '0008', LEGACY_OLD_KEY: 40}, strict=False)
    assert LEGACY_FIELD not in AlphaOrder._schema.fields
    assert LEGACY_FIELD not in loose_alpha

    # The child created after registration accepts the same payload.
    beta = BetaOrder(dict(BETA_RECORD), strict=True)
    assert beta.legacy_price == 40


def test_control_order_gives_registered_field_to_both_children(control_schemas):
    # Control (IMPORT): with registration moved ahead of both subclass
    # definitions, the previously failing child now owns the field.
    SupplierOrder, AlphaOrder, BetaOrder, _ = control_schemas

    assert LEGACY_FIELD in AlphaOrder._schema.fields
    assert LEGACY_FIELD in BetaOrder._schema.fields

    alpha = AlphaOrder(
        {'order_no': '0008', LEGACY_OLD_KEY: 40}, strict=True)
    assert alpha.legacy_price == 40

    # The other vendor's data is unchanged: same payload, same export shape,
    # same leading-zero preservation.
    beta = BetaOrder(dict(BETA_RECORD), strict=True)
    assert beta.to_primitive() == {
        'order_no': '0007',
        LEGACY_SERIAL_KEY: 40,
        'beta_note': 'n',
        'price': '0042',
    }


def test_real_subclass_instances_travel_through_parent_typed_branch(vendor_schemas):
    # IMPORT/VALIDATE/EXPORT: instances of both real subclasses are loaded into
    # the PolyModelType branch rooted at the parent and keep their concrete type.
    _, AlphaOrder, BetaOrder, OrderBatch = vendor_schemas

    alpha = AlphaOrder({'order_no': '0008', PRICE_SERIAL_KEY: 5})
    beta = BetaOrder(dict(BETA_RECORD))

    batch = OrderBatch({'orders': [alpha, beta]})

    assert [type(order) for order in batch.orders] == [AlphaOrder, BetaOrder]
    batch.validate()

    exported = batch.to_primitive()
    assert exported['orders'][0] == {
        'order_no': '0008',
        PRICE_SERIAL_KEY: 5,
    }
    assert exported['orders'][1] == {
        'order_no': '0007',
        LEGACY_SERIAL_KEY: 40,
        'beta_note': 'n',
        'price': '0042',
    }


def test_bare_dict_equal_to_subclass_export_uses_parent_schema(vendor_schemas):
    # Assumption 2 (IMPORT/EXPORT): a bare dict whose contents are identical to
    # a BetaOrder export has no polymorphic discriminator, so PolyModelType
    # falls back to the parent instead of reusing the subclass fields.
    SupplierOrder, AlphaOrder, BetaOrder, OrderBatch = vendor_schemas

    exported = BetaOrder(dict(BETA_RECORD)).to_primitive()
    bare_dict = dict(exported)
    assert bare_dict == {
        'order_no': '0007',
        LEGACY_SERIAL_KEY: 40,
        'beta_note': 'n',
        'price': '0042',
    }

    loose_batch = OrderBatch({'orders': [bare_dict]}, strict=False)
    parsed = loose_batch.orders[0]

    # A top-level type check alone would pass (it is still a Model); the
    # concrete type and field-level effects expose the fallback.
    assert type(parsed) is SupplierOrder
    assert not isinstance(parsed, BetaOrder)
    # The parent's integer price coerces the string and loses leading zeros.
    assert parsed.price == 42 and isinstance(parsed.price, int)
    # The subclass-only field is silently dropped on the non-strict path.
    assert 'beta_note' not in parsed
    # The runtime-registered parent field still resolves via its serialized key.
    assert parsed.legacy_price == 40

    # Strict import surfaces the subclass-only key as a rogue field, located
    # inside the nested branch rather than at the top level.
    with pytest.raises(DataError) as excinfo:
        OrderBatch({'orders': [dict(bare_dict)]}, strict=True)

    error = excinfo.value
    assert error.to_primitive() == {
        'orders': {0: {'beta_note': 'Rogue field'}},
    }
    assert error.partial_data == {}


def test_nested_orders_map_distinct_input_keys_to_price(vendor_schemas):
    # IMPORT: the two vendors use different inbound keys, both mapped onto the
    # price field through the nested model mapping.
    _, AlphaOrder, BetaOrder, OrderBatch = vendor_schemas

    batch = OrderBatch(
        dict(NESTED_PAYLOAD), strict=True, deserialize_mapping=NESTED_MAPPING)

    assert [type(order) for order in batch.orders] == [BetaOrder, AlphaOrder]

    beta_branch, alpha_branch = batch.orders
    assert beta_branch.price == 'oops'
    assert beta_branch.legacy_price == 40

    # The serialized key (99) wins over the mapped key (5): atoms() resolves the
    # field through ``serialized_name`` before the mapping fallback is consulted.
    assert alpha_branch.price == 99


def test_nested_validation_error_location_partial_data_and_old_value(vendor_schemas):
    # VALIDATE: validate the failing nested instance directly. The error is
    # located on the string price field, partial data is returned literally,
    # and the trustworthy legacy value remains readable afterwards.
    _, AlphaOrder, BetaOrder, OrderBatch = vendor_schemas

    batch = OrderBatch(
        dict(NESTED_PAYLOAD), strict=True, deserialize_mapping=NESTED_MAPPING)
    beta_branch, alpha_branch = batch.orders

    with pytest.raises(DataError) as excinfo:
        beta_branch.validate()

    error = excinfo.value
    assert error.to_primitive() == {
        'price': ['String value did not match validation regex.'],
    }
    # Literal partial data keyed by field names; the illegal price is absent.
    assert error.partial_data == {
        'order_no': '0007',
        LEGACY_FIELD: 40,
        'beta_note': None,
    }

    # Same instance, re-read after failure: trusted old values survive.
    assert beta_branch.legacy_price == 40
    assert beta_branch.order_no == '0007'
    with pytest.raises(UndefinedValueError):
        beta_branch.price

    # The healthy sibling branch is unaffected and keeps the conflict winner.
    alpha_branch.validate()
    assert alpha_branch.price == 99


def test_batch_validation_drops_processed_branches_on_nested_error(vendor_schemas):
    # Assumption 3 (VALIDATE): validating through the parent keeps the nested
    # error location but drops the entire processed list from the parent
    # instance. References captured before the call still expose old values.
    _, AlphaOrder, BetaOrder, OrderBatch = vendor_schemas

    batch = OrderBatch(
        dict(NESTED_PAYLOAD), strict=True, deserialize_mapping=NESTED_MAPPING)
    beta_ref, alpha_ref = batch.orders

    with pytest.raises(DataError) as excinfo:
        batch.validate()

    error = excinfo.value
    # The error stays anchored to the failing nested branch and field, keyed by
    # the list index and the field name rather than its serialized name.
    assert error.to_primitive() == {
        'orders': {0: {'price': ['String value did not match validation regex.']}},
    }
    # The parent retains no partial branch data at all.
    assert error.partial_data == {}

    # Top-level-only assertion would miss this: the whole list, including the
    # valid Alpha branch, is gone from the parent.
    with pytest.raises(UndefinedValueError):
        batch.orders

    # The captured nested instances still hold the values read before failure.
    assert beta_ref.legacy_price == 40
    assert beta_ref.order_no == '0007'
    assert alpha_ref.price == 99

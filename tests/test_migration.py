# -*- coding: utf-8 -*-

from datetime import datetime

import pytest

from schematics.exceptions import ValidationError
from schematics.migration import (
    MigrationStageError, MigrationValidationError,
)
from schematics.models import Model
from schematics.types import DateTimeType, IntType, StringType
from schematics.types.compound import ListType, ModelType


class Address(Model):
    street = StringType()
    city = StringType()


class LegacyAddress(Model):
    street = StringType()
    city = StringType()


class LegacyOrder(Model):
    billing = ModelType(LegacyAddress)
    shipping = ModelType(LegacyAddress)
    extra = ListType(ModelType(LegacyAddress))
    placed_at = StringType()
    status = StringType()
    total = IntType()


class CleanAddress(Model):
    street = StringType(required=True)
    city = StringType(required=True)

    def validate_city(self, value):
        if value != value.strip().upper():
            raise ValidationError('city must be normalized')


def normalize_address(value, path=None):
    """Example normalizer for a target nested address."""
    if isinstance(value, CleanAddress):
        value.city = (value.city or '').strip().upper()
        value.street = (value.street or '').strip()
        return value
    return value


class CleanOrder(Model):
    billing = ModelType(CleanAddress, required=True)
    shipping = ModelType(CleanAddress, required=True)
    extra = ListType(ModelType(CleanAddress))
    placed_at = DateTimeType()
    status = StringType(choices=['open', 'closed'])
    total = IntType()

    def validate_shipping(self, data, value):
        if data['billing'] is not value:
            raise ValidationError('billing and shipping must share an address')
        if value.city != data['billing'].city:
            raise ValidationError('city mismatch')


def make_order(placed_at='2020-01-01T10:00:00', city='BOSTON'):
    shared = LegacyAddress({'street': '1 Main', 'city': city})
    order = LegacyOrder(init=False)
    order._data.valid = {
        'billing': shared,
        'shipping': shared,
        'extra': [LegacyAddress({'street': '1 Main', 'city': city})],
        'placed_at': placed_at,
        'status': 'open',
        'total': 5,
    }
    return order


def test_conversion_preserves_source_and_aliases():
    order = make_order()
    order.placed_at = '2020-02-02T10:00:00'
    before = order.to_primitive()

    migrated = order.migrate(CleanOrder)

    assert migrated.placed_at == datetime(2020, 2, 2, 10, 0)
    assert migrated.billing is migrated.shipping
    assert migrated.extra[0] is not migrated.billing
    assert migrated.extra[0] == migrated.billing
    assert order.to_primitive() == before
    assert order.billing is order.shipping


def test_failed_conversion_does_not_publish_or_mutate():
    order = make_order(placed_at='not-a-date')

    with pytest.raises(MigrationStageError) as excinfo:
        order.migrate(CleanOrder)

    assert excinfo.value.stage == 'date'
    assert excinfo.value.field_path == ('placed_at',)
    assert order.placed_at == 'not-a-date'
    assert order.billing is order.shipping


def test_validation_failure_reports_conflict_path():
    order = make_order()
    order.status = 'archived'

    with pytest.raises(MigrationValidationError) as excinfo:
        order.migrate(CleanOrder)

    assert excinfo.value.stage == 'validate'
    assert excinfo.value.field_path == ('status',)
    assert order.status == 'archived'


def test_normalize_then_retry_is_consistent():
    order = make_order(placed_at='not-a-date', city=' boston ')
    before = order.to_primitive()

    with pytest.raises(MigrationStageError) as excinfo:
        order.migrate(CleanOrder, normalize=normalize_address)
    assert excinfo.value.stage == 'date'
    assert excinfo.value.field_path == ('placed_at',)
    assert order.to_primitive() == before
    assert order.billing is order.shipping

    order.placed_at = '2020-03-03T09:30:00'
    migrated = order.migrate(CleanOrder, normalize=normalize_address)
    assert migrated.placed_at == datetime(2020, 3, 3, 9, 30)
    assert migrated.billing is migrated.shipping
    assert migrated.extra[0] is not migrated.billing
    assert migrated.billing.city == 'BOSTON'
    assert order.placed_at == '2020-03-03T09:30:00'
    assert order.billing.city == ' boston '


def test_parseable_date_conflicts_with_status():
    order = make_order(placed_at='2020-01-01T10:00:00')
    order.status = 'archived'

    with pytest.raises(MigrationValidationError) as excinfo:
        order.migrate(CleanOrder)

    assert excinfo.value.stage == 'validate'
    assert excinfo.value.field_path == ('status',)
    assert order.status == 'archived'


def test_editing_result_does_not_touch_source():
    order = make_order()
    migrated = order.migrate(CleanOrder)
    source_address = order.billing
    migrated.billing.city = 'CHANGED'
    assert migrated.shipping.city == 'CHANGED'
    assert source_address.city == 'BOSTON'
    assert order.billing is source_address


def test_source_pending_value_is_read_but_not_committed():
    order = make_order()
    order._data.valid = {'placed_at': '2020-01-01T10:00:00'}
    order.placed_at = '2020-04-04T04:04:04'

    migrated = order.migrate(CleanOrder)
    assert migrated.placed_at == datetime(2020, 4, 4, 4, 4, 4)
    assert order.placed_at == '2020-04-04T04:04:04'


def test_valid_and_pending_layers_stay_intact_after_failure():
    order = make_order()
    order.placed_at = 'bad-date'
    before_valid = dict(order._data.valid)
    before_converted = dict(order._data.converted)

    with pytest.raises(MigrationStageError):
        order.migrate(CleanOrder)

    assert dict(order._data.valid) == before_valid
    assert dict(order._data.converted) == before_converted
    assert order.placed_at == 'bad-date'


def test_nested_list_error_path():
    order = make_order()
    order.extra[0].city = ' bad '

    with pytest.raises(MigrationValidationError) as excinfo:
        order.migrate(CleanOrder)

    assert excinfo.value.field_path == ('extra', 0, 'city')


def test_try_migrate_returns_error_without_raising():
    order = make_order(placed_at='not-a-date')

    target, error = order.try_migrate(CleanOrder)
    assert target is None
    assert error.stage == 'date'
    assert error.field_path == ('placed_at',)

    order.placed_at = '2020-06-06T06:06:06'
    target, error = order.try_migrate(CleanOrder)
    assert error is None
    assert target.placed_at == datetime(2020, 6, 6, 6, 6, 6)


def test_app_data_reaches_target_validators():
    seen = {}

    class ContextAddress(Model):
        city = StringType()

        def validate_city(self, value, context):
            seen['city'] = context.app_data['tag']

    class ContextOrder(Model):
        billing = ModelType(ContextAddress)

    class LegacyContextOrder(Model):
        billing = ModelType(LegacyAddress)

    legacy = LegacyContextOrder(init=False)
    legacy._data.valid = {'billing': LegacyAddress({'city': 'x'})}

    legacy.migrate(ContextOrder, app_data={'tag': 'ok'})
    assert seen['city'] == 'ok'


def test_stage_for_can_name_custom_steps():
    order = make_order(placed_at='bad')

    with pytest.raises(MigrationStageError) as excinfo:
        order.migrate(CleanOrder, stage_for=lambda field, path: 'parse-date')

    assert excinfo.value.stage == 'parse-date'
    assert excinfo.value.field_path == ('placed_at',)

# -*- coding: utf-8 -*-

from __future__ import unicode_literals, absolute_import

import datetime

import pytest

from schematics.models import Model
from schematics.transforms import convert
from schematics.validate import validate
from schematics.conversion_cache import ConversionCache, new_cache_id
from schematics.types import BaseType, StringType
from schematics.types.compound import DictType, ListType, ModelType
from schematics.exceptions import ConversionError, DataError, ValidationError


class SourceConfig(object):
    """Mutable source configuration carrying an explicit revision."""

    def __init__(self, formats, version=1):
        self.formats = list(formats)
        self.version = version


class DateParseType(BaseType):
    """Expensive, explicitly reusable parser of a date literal."""

    reuse_conversion = True

    parse_calls = 0
    validator_calls = 0

    def __init__(self, config, **kwargs):
        self.config = config
        super(DateParseType, self).__init__(**kwargs)

    def to_native(self, value, context=None):
        type(self).parse_calls += 1
        if isinstance(value, datetime.date):
            return value
        try:
            return datetime.datetime.strptime(value, self.config.formats[0]).date()
        except ValueError:
            raise ConversionError('could not parse %r' % value)

    def validate_day(self, value, context=None):
        type(self).validator_calls += 1


class NonReusableDateType(BaseType):
    """A timing/counting custom field parsing the same literal, not reusable."""

    parse_calls = 0
    validator_calls = 0

    def __init__(self, config, **kwargs):
        self.config = config
        super(NonReusableDateType, self).__init__(**kwargs)

    def to_native(self, value, context=None):
        type(self).parse_calls += 1
        if isinstance(value, datetime.date):
            return value
        try:
            return datetime.datetime.strptime(value, self.config.formats[0]).date()
        except ValueError:
            raise ConversionError('could not parse %r' % value)

    def validate_value(self, value, context=None):
        type(self).validator_calls += 1


def build_order_models(config):
    class Address(Model):
        day = DateParseType(config)
        city = StringType()

    class Order(Model):
        name = StringType(required=True)
        measured_on = NonReusableDateType(config)
        billing = ModelType(Address)
        shipping = ModelType(Address)

        def validate_name(self, data, value, context):
            global name_validator_calls
            name_validator_calls += 1
            if not value.startswith('o'):
                raise ValidationError('name must start with o')

        def validate_billing(self, data, value, context):
            global billing_validator_calls
            billing_validator_calls += 1
            # Depends on the day/month produced by the date conversion.
            if value['day'].month != 3:
                raise ValidationError('billing must be in March')

    return Address, Order


RAW = {
    'name': 'o1',
    'measured_on': '03/04',
    'billing': {'day': '03/04', 'city': 'A-town'},
    'shipping': {'day': '03/04', 'city': 'B-town'},
}


name_validator_calls = 0
billing_validator_calls = 0


@pytest.fixture(autouse=True)
def reset_counters():
    global name_validator_calls, billing_validator_calls
    DateParseType.parse_calls = 0
    DateParseType.validator_calls = 0
    NonReusableDateType.parse_calls = 0
    NonReusableDateType.validator_calls = 0
    name_validator_calls = 0
    billing_validator_calls = 0
    yield


def test_conversion_then_validation_parses_each_reusable_field_once():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)

    order = Order(RAW, config_id=config.version)
    assert DateParseType.parse_calls == 2  # one per address subobject
    order.validate(config_id=config.version)

    # No extra expensive parsing in the validation pass, but validation runs.
    assert DateParseType.parse_calls == 2
    assert DateParseType.validator_calls == 2
    assert billing_validator_calls == 1
    assert name_validator_calls == 1
    assert order.billing.day == datetime.date(1900, 3, 4)
    assert order.shipping.day == datetime.date(1900, 3, 4)


def test_non_reusable_field_is_parsed_on_every_pass():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)

    order = Order(RAW, config_id=config.version)
    order.validate(config_id=config.version)

    # One parsing per pass, never served from the cache.
    assert NonReusableDateType.parse_calls == 2
    assert NonReusableDateType.validator_calls == 1


def test_two_same_typed_subobjects_keep_separate_cached_parses():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)

    order = Order(RAW, config_id=config.version)
    order.validate(config_id=config.version)

    assert order.billing is not order.shipping
    assert order.billing.day == order.shipping.day == datetime.date(1900, 3, 4)
    # Both address fields were parsed; neither result was reused for the other.
    assert DateParseType.parse_calls == 2


def test_local_modification_invalidates_only_affected_parse():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)

    order = Order(RAW, config_id=config.version)
    order.validate(config_id=config.version)
    assert DateParseType.parse_calls == 2

    order.import_data(
        {'shipping': {'day': '03/05', 'city': 'B-town'}},
        config_id=config.version)
    order.validate(config_id=config.version)

    assert DateParseType.parse_calls == 3
    assert order.billing.day == datetime.date(1900, 3, 4)
    assert order.shipping.day == datetime.date(1900, 3, 5)


def test_config_switch_uses_new_format_and_matches_fresh_reparse():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)
    order = Order(RAW, config_id=config.version)

    # Reference result produced with reuse disabled under the new format.
    reference_config = SourceConfig(['%d/%m'])
    _, ReferenceOrder = build_order_models(reference_config)
    reference = ReferenceOrder(RAW, config_id=reference_config.version,
                               reuse_conversions=False)
    expected_day = reference.billing.day
    assert expected_day == datetime.date(1900, 4, 3)
    with pytest.raises(DataError) as reference_exc:
        reference.validate(config_id=reference_config.version,
                           reuse_conversions=False)

    # Mutate the configuration container in place without a new object.
    config.formats = ['%d/%m']
    snapshot = DateParseType.parse_calls

    # Without bumping the explicit revision the stale cached parse survives.
    order.import_data(RAW, config_id=1)
    assert order.billing.day == datetime.date(1900, 3, 4)
    assert DateParseType.parse_calls == snapshot

    # Bumping the revision reparses the original values under the new format.
    config.version = 2
    order.import_data(RAW, config_id=config.version)
    assert order.billing.day == expected_day
    # Both address fields had to be parsed again under the new configuration.
    assert DateParseType.parse_calls == snapshot + 2
    with pytest.raises(DataError) as switched_exc:
        order.validate(config_id=config.version)
    assert switched_exc.value.to_primitive() == reference_exc.value.to_primitive()


def test_config_switch_does_not_replay_error_from_previous_configuration():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)
    # '13/03' is invalid as %m/%d (month 13) but valid as %d/%m (Mar 13),
    # which also satisfies the March rule of the model-level validator.
    bad_raw = dict(RAW, billing={'day': '13/03', 'city': 'A-town'})

    order = Order(RAW, config_id=config.version)
    with pytest.raises(DataError) as first_exc:
        order.import_data(bad_raw, config_id=config.version)
    assert 'billing' in first_exc.value.to_primitive()

    # Switching the source configuration makes the previously failing value
    # parse successfully; the cached failure must not be replayed.
    config.formats = ['%d/%m']
    config.version = 2
    order.import_data(bad_raw, config_id=config.version)
    order.validate(config_id=config.version)
    assert order.billing.day == datetime.date(1900, 3, 13)


def test_separate_instances_do_not_share_cached_conversions():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)

    Order(RAW, config_id=config.version)
    assert DateParseType.parse_calls == 2
    # An interleaved instance, even with the same source, parses independently.
    other = Order(RAW, config_id=config.version)
    assert DateParseType.parse_calls == 4
    other.validate(config_id=config.version)
    assert DateParseType.parse_calls == 4


def test_source_b_instance_is_not_polluted_by_source_a_cache():
    config_a = SourceConfig(['%m/%d'])
    config_b = SourceConfig(['%d/%m'])
    _, OrderA = build_order_models(config_a)
    _, OrderB = build_order_models(config_b)

    order_a = OrderA(RAW, config_id=config_a.version)
    order_a.validate(config_id=config_a.version)
    assert order_a.billing.day == datetime.date(1900, 3, 4)

    order_b = OrderB(RAW, config_id=config_b.version)
    assert order_b.billing.day == datetime.date(1900, 4, 3)
    with pytest.raises(DataError) as exc:
        order_b.validate(config_id=config_b.version)
    assert 'billing' in exc.value.to_primitive()


def test_reuse_disabled_reparses_on_both_passes():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)

    order = Order(RAW, config_id=config.version, reuse_conversions=False)
    order.validate(config_id=config.version, reuse_conversions=False)

    # Two reusable fields parsed once per pass.
    assert DateParseType.parse_calls == 4
    assert DateParseType.validator_calls == 2


def test_conversion_error_is_replayed_without_reparsing():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)
    bad_raw = dict(RAW, billing={'day': 'not-a-date', 'city': 'A-town'})
    cache = ConversionCache()
    mutable = dict(bad_raw)

    with pytest.raises(DataError):
        convert(Order._schema, mutable, oo=True,
                conversion_cache=cache, config_id=config.version)
    assert DateParseType.parse_calls == 2  # shipping parsed, billing attempted

    # Re-running with the same bad value replays the cached failure without
    # invoking the expensive parser again.
    with pytest.raises(DataError):
        convert(Order._schema, mutable, oo=True,
                conversion_cache=cache, config_id=config.version)
    assert DateParseType.parse_calls == 2


def test_functional_convert_and_validate_share_a_cache():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)
    cache = ConversionCache()
    mutable = dict(RAW)

    converted = convert(Order._schema, mutable, oo=True,
                        conversion_cache=cache, config_id=config.version)
    assert DateParseType.parse_calls == 2
    validated = validate(Order._schema, mutable, oo=True,
                         conversion_cache=cache, config_id=config.version)
    assert DateParseType.parse_calls == 2
    assert cache.hits == 2
    assert converted['billing']['day'] == validated['billing']['day']


def test_cache_reports_hits_and_misses():
    config = SourceConfig(['%m/%d'])
    _, Order = build_order_models(config)
    cache = ConversionCache()
    mutable = dict(RAW)

    convert(Order._schema, mutable, oo=True,
            conversion_cache=cache, config_id=config.version)
    validate(Order._schema, mutable, oo=True,
             conversion_cache=cache, config_id=config.version)

    assert cache.misses == 2
    assert cache.hits == 2


def test_list_elements_keep_separate_cached_parses():
    config = SourceConfig(['%m/%d'])

    class Item(Model):
        day = DateParseType(config)

    class Box(Model):
        items = ListType(ModelType(Item))

    raw = {'items': [{'day': '03/04'}, {'day': '03/04'}, {'day': '03/05'}]}
    box = Box(raw, config_id=config.version)
    assert DateParseType.parse_calls == 3
    box.validate(config_id=config.version)
    assert DateParseType.parse_calls == 3
    assert box.items[0].day == box.items[1].day == datetime.date(1900, 3, 4)
    assert box.items[2].day == datetime.date(1900, 3, 5)


def test_dict_entries_keep_separate_cached_parses():
    config = SourceConfig(['%m/%d'])

    class Bag(Model):
        entries = DictType(DateParseType(config))

    bag = Bag({'entries': {'a': '03/04', 'b': '03/04'}},
              config_id=config.version)
    assert DateParseType.parse_calls == 2
    bag.validate(config_id=config.version)
    assert DateParseType.parse_calls == 2
    assert bag.entries['a'] == bag.entries['b'] == datetime.date(1900, 3, 4)


def test_reuse_can_be_enabled_per_field_via_constructor_argument():
    class ExplicitDateType(BaseType):
        def to_native(self, value, context=None):
            ExplicitDateType.calls += 1
            if isinstance(value, datetime.date):
                return value
            return datetime.datetime.strptime(value, '%m/%d').date()

    ExplicitDateType.calls = 0

    class Doc(Model):
        day = ExplicitDateType(reuse_conversion=True)

    doc = Doc({'day': '03/04'}, config_id=1)
    doc.validate(config_id=1)
    assert ExplicitDateType.calls == 1


def test_new_cache_id_returns_distinct_revisions():
    first = new_cache_id()
    second = new_cache_id()
    assert second == first + 1
    assert first != second

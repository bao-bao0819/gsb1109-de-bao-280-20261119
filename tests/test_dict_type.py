from decimal import Decimal

import pytest

from schematics.exceptions import DataError
from schematics.models import Model
from schematics.types import DecimalType, IntType, StringType
from schematics.types.serializable import serializable
from schematics.types.compound import KeyRule, ModelType, DictType

try:
    long
except NameError:
    long = int

def test_basic_type():
    class PlayerInfo(Model):
        categories = DictType(StringType)

    info = PlayerInfo(dict(categories={
        "math": "math",
        "batman": "batman",
    }))

    assert info.categories["math"] == "math"

    d = info.serialize()
    assert d == {
        "categories": {
            "math": "math",
            "batman": "batman",
        }
    }


def test_dict_type_with_model_type():
    class CategoryStats(Model):
        category_slug = StringType()
        total_wins = IntType()

    class PlayerInfo(Model):
        categories = DictType(ModelType(CategoryStats))
        # TODO: Maybe it would be cleaner to have
        #       DictType(CategoryStats) and implicitly convert to ModelType(CategoryStats)

    info = PlayerInfo(dict(categories={
        "math": {
            "category_slug": "math",
            "total_wins": 1
        },
        "batman": {
            "category_slug": "batman",
            "total_wins": 3
        }
    }))

    math_stats = CategoryStats({"category_slug": "math", "total_wins": 1})
    assert info.categories["math"] == math_stats

    d = info.serialize()
    assert d == {
        "categories": {
            "math": {
                "category_slug": "math",
                "total_wins": 1
            },
            "batman": {
                "category_slug": "batman",
                "total_wins": 3
            }
        }
    }


def test_dict_type_with_model_type_init_with_instance():
    class ExperienceLevel(Model):
        level = IntType()

    class CategoryStats(Model):
        category_slug = StringType()
        total_wins = IntType()

        @serializable(type=ModelType(ExperienceLevel))
        def xp_level(self):
            return ExperienceLevel(dict(level=self.total_wins))

    class PlayerInfo(Model):
        id = IntType()
        categories = DictType(ModelType(CategoryStats))
        # TODO: Maybe it would be cleaner to have
        #       DictType(CategoryStats) and implicitly convert to ModelType(CategoryStats)

    math_stats = CategoryStats({
        "category_slug": "math",
        "total_wins": 1
    })

    info = PlayerInfo(dict(id=1, categories={
        "math": math_stats,
    }))

    assert info.categories["math"] == math_stats

    d = info.serialize()

    assert d == {
        "id": 1,
        "categories": {
            "math": {
                "category_slug": "math",
                "total_wins": 1,
                "xp_level": {
                    "level": 1
                }
            },
        }
    }


def test_with_empty():
    class CategoryStatsInfo(Model):
        slug = StringType()

    class PlayerInfo(Model):
        categories = DictType(
            ModelType(CategoryStatsInfo),
            default=lambda: {},
            serialize_when_none=True,
        )

    info = PlayerInfo()

    assert info.categories == {}

    d = info.serialize()
    assert d == {
        "categories": {},
    }


def test_key_type():
    def player_id(value):
        return long(value)

    class CategoryStatsInfo(Model):
        slug = StringType()

    class PlayerInfo(Model):
        categories = DictType(ModelType(CategoryStatsInfo), coerce_key=player_id)

    stats = CategoryStatsInfo({
        "slug": "math",
    })

    info = PlayerInfo({
        "categories": {
            1: {"slug": "math"}
        },
    })

    assert info.categories == {1: stats}

    d = info.serialize()
    assert d == {
        "categories": {1: {"slug": "math"}}
    }


def normalize_metric_key(key):
    return key.strip().lower().replace(' ', '_').replace('-', '_')


def metric_key_rules(reverse=False):
    rules = [
        KeyRule('temp_state', StringType(), exact=True),
        KeyRule('temp', DecimalType()),
        KeyRule('count', IntType()),
    ]
    if reverse:
        rules = rules[::-1]
    return rules


def telemetry_model(reverse_rules=False):
    class Telemetry(Model):
        metrics = DictType(
            key_rules=metric_key_rules(reverse=reverse_rules),
            key_normalize=normalize_metric_key)

    return Telemetry


def legacy_telemetry_model(reverse_rules=False):
    rules = [
        KeyRule('temp_state', StringType(), exact=True),
        KeyRule('temp', DecimalType()),
        KeyRule('temp_', DecimalType()),
        KeyRule('count', IntType()),
    ]
    if reverse_rules:
        rules = rules[::-1]

    class LegacyTelemetry(Model):
        metrics = DictType(key_rules=rules, key_normalize=normalize_metric_key)

    return LegacyTelemetry


def test_dict_type_requires_field_or_key_rules():
    with pytest.raises(TypeError):
        DictType()


def test_single_field_dict_type_keeps_original_behavior():
    class PlayerInfo(Model):
        categories = DictType(StringType)

    info = PlayerInfo({'categories': {' Math ': '1', 'math': '2'}})

    assert info.categories == {' Math ': '1', 'math': '2'}
    assert info.serialize() == {'categories': {' Math ': '1', 'math': '2'}}


@pytest.mark.parametrize('reverse_items', [False, True])
@pytest.mark.parametrize('reverse_rules', [False, True])
def test_keyed_dict_type_merges_equal_synonymous_keys(reverse_items, reverse_rules):
    Telemetry = telemetry_model(reverse_rules=reverse_rules)
    items = [
        ('Temp Ambient', '21.5'),
        (' temp_ambient  ', '21.50'),
        ('COUNT Events', '3'),
        ('TEMP_STATE', 'nominal'),
    ]
    if reverse_items:
        items = items[::-1]

    reading = Telemetry({'metrics': dict(items)})

    assert reading.metrics == {
        'temp_ambient': Decimal('21.5'),
        'count_events': 3,
        'temp_state': 'nominal',
    }


@pytest.mark.parametrize('reverse_items', [False, True])
@pytest.mark.parametrize('reverse_rules', [False, True])
def test_keyed_dict_type_rejects_unequal_synonymous_keys(reverse_items, reverse_rules):
    Telemetry = telemetry_model(reverse_rules=reverse_rules)
    items = [('Temp Ambient', '21.5'), ('temp_ambient', '22.0')]
    if reverse_items:
        items = items[::-1]

    with pytest.raises(DataError) as exc_info:
        Telemetry({'metrics': dict(items)})

    metric_errors = exc_info.value.errors['metrics']
    assert set(metric_errors) == {'Temp Ambient', 'temp_ambient'}
    for error in metric_errors.values():
        assert 'Temp Ambient' in str(error)
        assert 'temp_ambient' in str(error)


def test_exact_rule_takes_precedence_over_prefix_rule():
    Telemetry = telemetry_model()

    reading = Telemetry({'metrics': {'TEMP_STATE': 'nominal'}})

    assert reading.metrics == {'temp_state': 'nominal'}
    assert isinstance(reading.metrics['temp_state'], str)


@pytest.mark.parametrize('reverse_rules', [False, True])
def test_same_rank_prefix_conflict_is_reported_by_source_key(reverse_rules):
    LegacyTelemetry = legacy_telemetry_model(reverse_rules=reverse_rules)

    reading = LegacyTelemetry({'metrics': {'TEMP_STATE': 'degraded', 'count_boots': '2'}})
    assert reading.metrics == {'temp_state': 'degraded', 'count_boots': 2}

    with pytest.raises(DataError) as exc_info:
        LegacyTelemetry({'metrics': {'Temp Probe': '21.5'}})

    metric_errors = exc_info.value.errors['metrics']
    assert set(metric_errors) == {'Temp Probe'}


def test_conversion_error_is_reported_under_original_key():
    Telemetry = telemetry_model()

    with pytest.raises(DataError) as exc_info:
        Telemetry({'metrics': {'Temp Ambient': 'not-a-decimal'}})

    assert set(exc_info.value.errors['metrics']) == {'Temp Ambient'}


def test_key_normalization_is_scoped_to_each_model():
    Telemetry = telemetry_model()

    class AltTelemetry(Model):
        metrics = DictType(
            key_rules=[
                KeyRule('TEMP', DecimalType()),
                KeyRule('COUNT', IntType()),
            ],
            key_normalize=lambda key: key.strip().upper().replace(' ', '_'))

    alt = AltTelemetry({'metrics': {'temp ambient': '21.5', 'count events': '3'}})
    assert alt.metrics == {'TEMP_AMBIENT': Decimal('21.5'), 'COUNT_EVENTS': 3}

    # Processing the other source must not leak its metric interpretation.
    reading = Telemetry({'metrics': {'temp ambient': '21.5'}})
    assert reading.metrics == {'temp_ambient': Decimal('21.5')}

    alt_again = AltTelemetry({'metrics': {'TEMP_AMBIENT': '22.0'}})
    assert alt_again.metrics == {'TEMP_AMBIENT': Decimal('22.0')}

    reading_again = Telemetry({'metrics': {'TEMP_AMBIENT': '22.0'}})
    assert reading_again.metrics == {'temp_ambient': Decimal('22.0')}


def test_custom_field_raw_string_conversion_vs_native_validation():
    class TempReadingType(DecimalType):
        """Raw strings may carry a unit suffix; native values are range-checked."""

        def to_native(self, value, context=None):
            if isinstance(value, str):
                value = value.strip().rstrip('Cc').strip()
            return super(TempReadingType, self).to_native(value, context)

    class Telemetry(Model):
        metrics = DictType(
            key_rules=[KeyRule('temp', TempReadingType(min_value=-50, max_value=60))],
            key_normalize=normalize_metric_key)

    reading = Telemetry({'metrics': {'Temp Ambient': '21.5C'}})
    assert reading.metrics == {'temp_ambient': Decimal('21.5')}
    reading.validate()

    out_of_range = Telemetry({'metrics': {'Temp Ambient': '99.9C'}})
    assert out_of_range.metrics == {'temp_ambient': Decimal('99.9')}
    with pytest.raises(DataError) as exc_info:
        out_of_range.validate()
    assert set(exc_info.value.errors['metrics']) == {'temp_ambient'}


def test_keyed_dict_type_revalidation_and_serialization_round_trip():
    Telemetry = telemetry_model()

    reading = Telemetry({'metrics': {
        'Temp Ambient': '21.5',
        'count_events': '3',
        'TEMP_STATE': 'nominal',
    }})
    reading.validate()
    serialized = reading.serialize()
    assert serialized == {
        'metrics': {
            'temp_ambient': '21.5',
            'count_events': 3,
            'temp_state': 'nominal',
        }
    }

    # Feeding the unified collection back into the same model is a no-op:
    # normalized keys and native values keep their metric interpretation.
    again = Telemetry({'metrics': reading.metrics})
    assert again.metrics == reading.metrics
    assert isinstance(again.metrics['temp_ambient'], Decimal)
    assert isinstance(again.metrics['count_events'], int)
    assert isinstance(again.metrics['temp_state'], str)

    again.validate()
    assert again.serialize() == serialized

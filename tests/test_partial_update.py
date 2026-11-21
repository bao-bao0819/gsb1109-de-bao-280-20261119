# -*- coding: utf-8 -*-
"""
Tests for partial update validation (``Model.update``) with validation
rules that declare the fields they read via ``@reads``.
"""
import datetime

import pytest

from schematics.models import Model, reads
from schematics.types.base import StringType, IntType
from schematics.types.compound import ModelType
from schematics.types.serializable import serializable
from schematics.exceptions import DataError, ValidationError


# Fixed offsets in minutes, standing in for a time zone database.
TZ_OFFSETS = {
    'Europe/Berlin': 60,
    'Pacific/Honolulu': -600,
    'UTC': 0,
}

TZ_ALIASES = {
    'CET': 'Europe/Berlin',
    'BERLIN': 'Europe/Berlin',
    'HST': 'Pacific/Honolulu',
    'UTC': 'UTC',
}

MINIMUM_AMOUNTS = {
    'EUR': 100,
    'USD': 100,
    'JPY': 10000,
}

RULE_EXECUTIONS = []


def to_utc_time(local_text, timezone):
    """Interprets a local 'HH:MM' source text in the given time zone and
    returns the converted UTC wall clock time."""
    hours, minutes = map(int, local_text.split(':'))
    total = (hours * 60 + minutes - TZ_OFFSETS[timezone]) % (24 * 60)
    return datetime.time(total // 60, total % 60)


class PricingTerms(Model):
    timezone = StringType(required=True)


class Quote(Model):
    supplier_code = StringType(required=True)
    local_start = StringType(required=True)
    local_end = StringType(required=True)
    currency = StringType(required=True)
    amount = IntType(required=True)
    terms = ModelType(PricingTerms)

    @serializable
    def timezone_alias(self):
        return self.terms.timezone

    @timezone_alias.setter
    def timezone_alias(self, value):
        if value is not None:
            self.terms.timezone = TZ_ALIASES.get(value, value)

    @serializable
    def start_at(self):
        return to_utc_time(self.local_start, self.terms.timezone)

    @serializable
    def end_at(self):
        return to_utc_time(self.local_end, self.terms.timezone)

    @reads('start_at', 'end_at')
    def validate_local_end(self, data, value):
        timezone = data['terms'].timezone
        start_at = to_utc_time(data['local_start'], timezone)
        end_at = to_utc_time(data['local_end'], timezone)
        RULE_EXECUTIONS.append(('interval', start_at, end_at))
        if end_at <= start_at:
            raise ValidationError(
                'interval is inverted: %s is not after %s'
                % (end_at, start_at))

    @reads('currency', 'amount')
    def validate_amount(self, data, value):
        RULE_EXECUTIONS.append(('discount', data['currency'], value))
        minimum = MINIMUM_AMOUNTS[data['currency']]
        if value < minimum:
            raise ValidationError(
                'amount %s is below the %s minimum of %s'
                % (value, data['currency'], minimum))

    def validate_currency(self, data, value):
        # No dependency declaration: keeps the existing calling
        # convention and runs on every validation.
        RULE_EXECUTIONS.append(('currency_format', value))
        if not value.isupper():
            raise ValidationError('currency must be upper case')


def legacy_quote():
    """A quote stored before supplier codes became mandatory."""
    return Quote({
        'local_start': '09:00',
        'local_end': '17:00',
        'currency': 'EUR',
        'amount': 500,
        'terms': {'timezone': 'Europe/Berlin'},
    }, partial=True, validate=True)


@pytest.fixture
def quote():
    return legacy_quote()


@pytest.fixture
def executions():
    del RULE_EXECUTIONS[:]
    return RULE_EXECUTIONS


def executions_named(executions, name):
    return [entry for entry in executions if entry[0] == name]


def test_equivalent_timezone_alias_does_not_retrigger_interval_rule(quote, executions):
    quote.update({'timezone_alias': 'CET'})

    assert quote.terms.timezone == 'Europe/Berlin'
    assert executions_named(executions, 'interval') == []
    assert executions_named(executions, 'discount') == []
    # Rules without a dependency declaration keep the existing
    # calling convention and still run.
    assert executions_named(executions, 'currency_format') == [
        ('currency_format', 'EUR')]


def test_inverting_timezone_update_is_rejected_without_side_effects(quote, executions):
    state_before = quote.to_native()
    pending_terms_before = dict(quote.terms._data.converted)

    with pytest.raises(DataError) as exc:
        quote.update({'timezone_alias': 'HST'})

    assert 'local_end' in exc.value.errors
    # The interval rule ran exactly once, with the final merged values
    # (09:00/17:00 Honolulu time convert to 19:00/03:00 UTC).
    assert executions_named(executions, 'interval') == [
        ('interval', datetime.time(19, 0), datetime.time(3, 0))]
    assert executions_named(executions, 'discount') == []

    # The existing quote and any pending fields are preserved, even
    # though the setter had already written the nested pricing terms.
    assert quote.to_native() == state_before
    assert quote.terms.timezone == 'Europe/Berlin'
    assert dict(quote._data.converted) == {}
    assert dict(quote.terms._data.converted) == pending_terms_before

    # Retrying a legal update behaves as if it were executed on the
    # original record in the first place.
    pristine = legacy_quote()
    quote.update({'timezone_alias': 'HST', 'local_end': '13:00'})
    pristine.update({'timezone_alias': 'HST', 'local_end': '13:00'})
    assert quote.to_native() == pristine.to_native()


def test_combined_timezone_and_end_fix_validates_interval_once(quote, executions):
    quote.update({'timezone_alias': 'HST', 'local_end': '13:00'})

    # The interval rule ran exactly once, against the final merged
    # values (13:00 Honolulu time converts to 23:00 UTC).
    assert executions_named(executions, 'interval') == [
        ('interval', datetime.time(19, 0), datetime.time(23, 0))]
    assert executions_named(executions, 'discount') == []
    assert quote.terms.timezone == 'Pacific/Honolulu'
    assert quote.local_end == '13:00'


def test_amount_update_still_checks_discount(quote, executions):
    quote.update({'amount': 450})

    assert executions_named(executions, 'discount') == [('discount', 'EUR', 450)]
    assert executions_named(executions, 'interval') == []
    assert quote.amount == 450

    with pytest.raises(DataError) as exc:
        quote.update({'amount': 50})  # below the EUR minimum

    assert 'amount' in exc.value.errors
    assert quote.amount == 450


def test_missing_supplier_code_does_not_block_partial_update(quote, executions):
    quote.update({'amount': 600})
    assert quote.amount == 600

    with pytest.raises(DataError) as exc:
        quote.validate()

    assert 'supplier_code' in exc.value.errors

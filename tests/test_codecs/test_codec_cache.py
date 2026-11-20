import gc
import weakref
from dataclasses import dataclass
from datetime import date

import pytest

from mashumaro.codecs import _cache as codec_cache
from mashumaro.codecs.basic import BasicDecoder, BasicEncoder
from mashumaro.dialect import Dialect


def _is_generated_code(source: str) -> bool:
    return (
        "__mashumaro" in source
        or "decoder_obj" in source
        or "encoder_obj" in source
    )


@pytest.fixture
def generated_code_counter(monkeypatch):
    count = 0
    real_exec = exec

    def counting_exec(source, globals=None, locals=None):
        nonlocal count
        if isinstance(source, str) and _is_generated_code(source):
            count += 1
        return real_exec(source, globals, locals)

    monkeypatch.setattr("builtins.exec", counting_exec)
    return lambda: count


@dataclass
class CachedModel:
    value: int


class OrdinalDialect(Dialect):
    serialization_strategy = {
        date: {"serialize": date.toordinal, "deserialize": date.fromordinal}
    }


def test_equivalent_decoders_share_code(generated_code_counter):
    def pre_decoder(value):
        return {"value": value["value"]}

    first = BasicDecoder(CachedModel, pre_decoder_func=pre_decoder)
    after_first = generated_code_counter()
    assert after_first > 0

    second = BasicDecoder(CachedModel, pre_decoder_func=pre_decoder)
    assert generated_code_counter() == after_first

    assert first.decode({"value": 1}) == CachedModel(1)
    assert second.decode({"value": 2}) == CachedModel(2)


def test_equivalent_encoders_share_code(generated_code_counter):
    def post_encoder(value):
        return value["value"]

    first = BasicEncoder(CachedModel, post_encoder_func=post_encoder)
    after_first = generated_code_counter()
    assert after_first > 0

    second = BasicEncoder(CachedModel, post_encoder_func=post_encoder)
    assert generated_code_counter() == after_first

    assert first.encode(CachedModel(1)) == 1
    assert second.encode(CachedModel(2)) == 2


def test_different_dialects_compile_and_produce_own_results(
    generated_code_counter,
):
    @dataclass
    class DatedModel:
        value: date

    class ISODialect(Dialect):
        serialization_strategy = {
            date: {
                "serialize": date.isoformat,
                "deserialize": date.fromisoformat,
            }
        }

    ordinal = BasicDecoder(DatedModel, default_dialect=OrdinalDialect)
    after_ordinal = generated_code_counter()
    assert after_ordinal > 0

    iso = BasicDecoder(DatedModel, default_dialect=ISODialect)
    assert generated_code_counter() > after_ordinal

    assert ordinal.decode({"value": 738785}) == DatedModel(date(2023, 9, 22))
    assert iso.decode({"value": "2023-09-22"}) == DatedModel(
        date(2023, 9, 22)
    )

    another_ordinal = BasicDecoder(
        DatedModel, default_dialect=OrdinalDialect
    )
    after_repeat = generated_code_counter()
    assert another_ordinal.decode({"value": 738786}) == DatedModel(
        date(2023, 9, 23)
    )
    assert generated_code_counter() == after_repeat
    assert iso.decode({"value": "2023-09-23"}) == DatedModel(
        date(2023, 9, 23)
    )


def test_same_named_callbacks_do_not_share_code(generated_code_counter):
    def decoder_factory(key):
        def handler(value):
            return {"value": value[key]}

        return handler

    def encoder_factory(tag):
        def handler(value):
            return {"value": value["value"], "tag": tag}

        return handler

    first_pre = decoder_factory("first")
    second_pre = decoder_factory("second")
    assert first_pre.__name__ == second_pre.__name__ == "handler"

    first_decoder = BasicDecoder(CachedModel, pre_decoder_func=first_pre)
    after_first = generated_code_counter()
    second_decoder = BasicDecoder(CachedModel, pre_decoder_func=second_pre)
    assert generated_code_counter() > after_first

    payload = {"first": 1, "second": 2}
    assert first_decoder.decode(payload) == CachedModel(1)
    assert second_decoder.decode(payload) == CachedModel(2)

    first_post = encoder_factory("first")
    second_post = encoder_factory("second")
    assert first_post.__name__ == second_post.__name__ == "handler"

    after_decoders = generated_code_counter()
    first_encoder = BasicEncoder(CachedModel, post_encoder_func=first_post)
    after_first_encoder = generated_code_counter()
    assert after_first_encoder > after_decoders
    second_encoder = BasicEncoder(CachedModel, post_encoder_func=second_post)
    assert generated_code_counter() > after_first_encoder

    assert first_encoder.encode(CachedModel(1)) == {
        "value": 1,
        "tag": "first",
    }
    assert second_encoder.encode(CachedModel(2)) == {
        "value": 2,
        "tag": "second",
    }


def test_equivalent_configurations_still_build_independent_instances():
    def pre_decoder(value):
        return value

    first = BasicDecoder(CachedModel, pre_decoder_func=pre_decoder)
    second = BasicDecoder(CachedModel, pre_decoder_func=pre_decoder)

    assert first is not second
    assert first.decode is not second.decode
    assert first.decode.__globals__["decoder_obj"] is first
    assert second.decode.__globals__["decoder_obj"] is second
    assert first.decode({"value": 1}) == CachedModel(1)
    assert second.decode({"value": 2}) == CachedModel(2)


def test_alternating_equivalent_codecs_compile_once(generated_code_counter):
    def pre_decoder(value):
        return value

    def post_encoder(value):
        return value

    decoders = [
        BasicDecoder(CachedModel, pre_decoder_func=pre_decoder)
        for _ in range(2)
    ]
    encoders = [
        BasicEncoder(CachedModel, post_encoder_func=post_encoder)
        for _ in range(2)
    ]
    after_first_pair = generated_code_counter()
    assert after_first_pair > 0

    for index in range(3):
        for decoder in decoders:
            assert decoder.decode({"value": index}) == CachedModel(index)
        for encoder in encoders:
            assert encoder.encode(CachedModel(index)) == {"value": index}
        assert generated_code_counter() == after_first_pair


def test_unshareable_callback_falls_back_to_fresh_compilation(
    generated_code_counter,
):
    class UnweakrefableCallback:
        __slots__ = ("value",)

        def __init__(self, value):
            self.value = value

        def __call__(self, data):
            return {"value": self.value}

    first_callback = UnweakrefableCallback(1)
    second_callback = UnweakrefableCallback(2)

    first = BasicDecoder(CachedModel, pre_decoder_func=first_callback)
    after_first = generated_code_counter()
    assert after_first > 0
    second = BasicDecoder(CachedModel, pre_decoder_func=second_callback)
    assert generated_code_counter() > after_first

    assert first.decode({}) == CachedModel(1)
    assert second.decode({}) == CachedModel(2)


def test_short_lived_models_and_callbacks_are_released(generated_code_counter):
    def pre_decoder(value):
        return value

    def post_encoder(value):
        return value

    survivor_decoder = BasicDecoder(
        CachedModel, pre_decoder_func=pre_decoder
    )
    survivor_encoder = BasicEncoder(
        CachedModel, post_encoder_func=post_encoder
    )
    gc.collect()
    baseline_cache_size = len(codec_cache._CACHE)

    def build_temporary_codec():
        @dataclass
        class TemporaryModel:
            value: int

        def temporary_pre_decoder(value):
            return value

        def temporary_post_encoder(value):
            return value

        decoder = BasicDecoder(
            TemporaryModel, pre_decoder_func=temporary_pre_decoder
        )
        encoder = BasicEncoder(
            TemporaryModel, post_encoder_func=temporary_post_encoder
        )
        assert decoder.decode({"value": 1}) == TemporaryModel(1)
        assert encoder.encode(TemporaryModel(2)) == {"value": 2}
        return (
            weakref.ref(TemporaryModel),
            weakref.ref(temporary_pre_decoder),
            weakref.ref(temporary_post_encoder),
            weakref.ref(decoder),
            weakref.ref(encoder),
            weakref.ref(getattr(decoder, codec_cache._TEMPLATE_ATTR)),
            weakref.ref(getattr(encoder, codec_cache._TEMPLATE_ATTR)),
        )

    (
        model_ref,
        pre_callback_ref,
        post_callback_ref,
        decoder_ref,
        encoder_ref,
        decoder_template_ref,
        encoder_template_ref,
    ) = build_temporary_codec()
    assert len(codec_cache._CACHE) > baseline_cache_size

    gc.collect()

    assert model_ref() is None
    assert pre_callback_ref() is None
    assert post_callback_ref() is None
    assert decoder_ref() is None
    assert encoder_ref() is None
    assert decoder_template_ref() is None
    assert encoder_template_ref() is None
    assert len(codec_cache._CACHE) == baseline_cache_size

    assert survivor_decoder.decode({"value": 3}) == CachedModel(3)
    assert survivor_encoder.encode(CachedModel(4)) == {"value": 4}

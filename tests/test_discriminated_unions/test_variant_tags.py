from dataclasses import dataclass
from typing import Union

import pytest
from typing_extensions import Annotated, Literal

from mashumaro import DataClassDictMixin
from mashumaro.codecs import BasicDecoder, BasicEncoder
from mashumaro.config import BaseConfig
from mashumaro.exceptions import (
    DuplicatedVariantTagError,
    SuitableVariantNotFoundError,
)
from mashumaro.types import Discriminator


# --------------------------------------------------------------------------
# Two independent data flows. The same tag spelling may legally be reused
# across the flows, but two variants of the same flow must not share a tag.
# --------------------------------------------------------------------------


@dataclass
class FlowOneA:
    value: int = 0


@dataclass
class FlowOneB:
    value: int = 0


@dataclass
class FlowTwoA:
    value: int = 0


@dataclass
class FlowTwoB:
    value: int = 0


FlowOne = Annotated[
    Union[FlowOneA, FlowOneB],
    Discriminator(
        field="type",
        include_supertypes=True,
        variant_tags={
            FlowOneA: ("one-a-new", "one-a-legacy", "shared"),
            FlowOneB: ("one-b",),
        },
    ),
]

FlowTwo = Annotated[
    Union[FlowTwoA, FlowTwoB],
    Discriminator(
        field="type",
        include_supertypes=True,
        variant_tags={
            FlowTwoA: ("two-a",),
            FlowTwoB: ("two-b-new", "two-b-legacy", "shared"),
        },
    ),
]


def test_independent_flows_keep_their_own_tag_binding():
    decoder_one = BasicDecoder(FlowOne)
    encoder_one = BasicEncoder(FlowOne)
    decoder_two = BasicDecoder(FlowTwo)
    encoder_two = BasicEncoder(FlowTwo)

    # The identical spelling resolves to a different variant in each flow.
    one = decoder_one.decode({"type": "shared", "value": 1})
    two = decoder_two.decode({"type": "shared", "value": 2})
    assert type(one) is FlowOneA
    assert type(two) is FlowTwoB

    # Every flow writes back only the primary tag of its own variant.
    assert encoder_one.encode(one) == {"value": 1, "type": "one-a-new"}
    assert encoder_two.encode(two) == {"value": 2, "type": "two-b-new"}


def test_alternating_shared_tag_decode_encode_read_back():
    decoder_one = BasicDecoder(FlowOne)
    encoder_one = BasicEncoder(FlowOne)
    decoder_two = BasicDecoder(FlowTwo)
    encoder_two = BasicEncoder(FlowTwo)

    # Alternate the two flows on the same spelling, serializing and reading
    # back after every step to make sure no state leaks across codecs.
    for _ in range(3):
        one = decoder_one.decode({"type": "shared", "value": 1})
        assert type(one) is FlowOneA
        one_back = decoder_one.decode(encoder_one.encode(one))
        assert type(one_back) is FlowOneA
        assert one_back == one

        two = decoder_two.decode({"type": "shared", "value": 2})
        assert type(two) is FlowTwoB
        two_back = decoder_two.decode(encoder_two.encode(two))
        assert type(two_back) is FlowTwoB
        assert two_back == two

    # The legacy and the new spelling of one flow read back as one variant.
    legacy = decoder_one.decode({"type": "one-a-legacy", "value": 5})
    current = decoder_one.decode({"type": "one-a-new", "value": 6})
    assert type(legacy) is type(current) is FlowOneA
    assert decoder_one.decode(encoder_one.encode(legacy)) == legacy


@pytest.mark.parametrize(
    "decoder, encoder, legacy_tag, new_tag, variant",
    [
        (
            BasicDecoder(FlowOne),
            BasicEncoder(FlowOne),
            "one-a-legacy",
            "one-a-new",
            FlowOneA,
        ),
        (
            BasicDecoder(FlowTwo),
            BasicEncoder(FlowTwo),
            "two-b-legacy",
            "two-b-new",
            FlowTwoB,
        ),
    ],
)
def test_legacy_and_primary_tags_read_back_as_same_variant(
    decoder, encoder, legacy_tag, new_tag, variant
):
    legacy_payload = {"type": legacy_tag, "value": 3}
    new_payload = {"type": new_tag, "value": 4}

    from_legacy = decoder.decode(legacy_payload)
    from_new = decoder.decode(new_payload)
    assert type(from_legacy) is variant
    assert type(from_new) is variant

    # Serializing an object built from a legacy tag writes the new tag,
    # and reading that output back keeps the same variant ownership.
    serialized = encoder.encode(from_legacy)
    assert serialized["type"] == new_tag
    read_back = decoder.decode(serialized)
    assert type(read_back) is variant
    assert read_back == from_legacy


def test_unknown_tag_is_not_retried_by_field_shape():
    decoder = BasicDecoder(FlowOne)
    # Both variants have the very same field shape, so the tag is the only
    # evidence. An unknown tag must fail instead of matching on shape.
    with pytest.raises(SuitableVariantNotFoundError):
        decoder.decode({"type": "unknown", "value": 1})
    with pytest.raises(SuitableVariantNotFoundError):
        decoder.decode({"type": "unknown"})


def test_independent_codec_is_not_rewritten_by_another_flow():
    encoder_one = BasicEncoder(FlowOne)
    decoder_one = BasicDecoder(FlowOne)
    instance = FlowOneA(9)
    before = encoder_one.encode(instance)
    assert before == {"value": 9, "type": "one-a-new"}

    # Building a second flow that reuses the same variant classes but
    # changes the compatible and primary tags must not touch codec one.
    changed_flow = Annotated[
        Union[FlowOneA, FlowOneB],
        Discriminator(
            field="type",
            include_supertypes=True,
            variant_tags={
                FlowOneA: ("one-a-v2", "one-a-legacy-v2"),
                FlowOneB: ("one-b-v2",),
            },
        ),
    ]
    BasicDecoder(changed_flow)
    changed_encoder = BasicEncoder(changed_flow)

    assert encoder_one.encode(instance) == before
    assert changed_encoder.encode(instance) == {
        "value": 9,
        "type": "one-a-v2",
    }
    assert type(
        decoder_one.decode({"type": "one-a-legacy", "value": 1})
    ) is FlowOneA


# --------------------------------------------------------------------------
# Colliding tags inside a single flow. The configuration is invalid before
# any variant is selected, even if the payload only carries an unrelated
# legal tag. Subclass definition order must not make it silently valid.
# --------------------------------------------------------------------------


def _build_colliding_shape(alpha_first: bool):
    if alpha_first:

        @dataclass
        class Alpha:
            value: int = 0

        @dataclass
        class Beta:
            value: int = 0

    else:

        @dataclass
        class Beta:
            value: int = 0

        @dataclass
        class Alpha:
            value: int = 0

    shape = Annotated[
        Union[Alpha, Beta],
        Discriminator(
            field="type",
            include_supertypes=True,
            variant_tags={
                Alpha: ("alpha", "shared"),
                Beta: ("beta", "shared"),
            },
        ),
    ]
    return shape, Alpha, Beta


@pytest.mark.parametrize("alpha_first", [True, False])
def test_duplicated_tag_raises_before_selection(alpha_first):
    shape, Alpha, Beta = _build_colliding_shape(alpha_first)
    decoder = BasicDecoder(shape)

    # Payloads that use unrelated legal tags, including one that would
    # resolve to a different variant first in a naive per-input lookup.
    # The whole configuration must be rejected before any selection.
    for payload in (
        {"type": "alpha", "value": 1},
        {"type": "beta", "value": 2},
    ):
        with pytest.raises(DuplicatedVariantTagError) as exc_info:
            decoder.decode(payload)

        message = str(exc_info.value)
        assert exc_info.value.tag == "shared"
        assert set(exc_info.value.variants) == {Alpha, Beta}
        assert "shared" in message
        assert Alpha.__name__ in message
        assert Beta.__name__ in message


def test_duplicated_tag_also_guards_nailed_configuration():
    @dataclass
    class Base(DataClassDictMixin):
        class Config(BaseConfig):
            discriminator = Discriminator(
                field="type",
                include_subtypes=True,
                variant_tags={
                    "Left": ("left", "dup"),
                    "Right": ("right", "dup"),
                },
            )

    @dataclass
    class Left(Base):
        value: int = 0

    @dataclass
    class Right(Base):
        value: int = 0

    with pytest.raises(DuplicatedVariantTagError) as exc_info:
        Base.from_dict({"type": "left", "value": 1})
    assert exc_info.value.tag == "dup"
    assert set(exc_info.value.variants) == {Left, Right}


# --------------------------------------------------------------------------
# Configuration based (nailed) discriminator with string name keys.
# --------------------------------------------------------------------------


def test_config_based_discriminator_migrates_tags():
    @dataclass
    class Device(DataClassDictMixin):
        class Config(BaseConfig):
            discriminator = Discriminator(
                field="type",
                include_subtypes=True,
                variant_tags={
                    "Thermo": ("thermo", "thermo-legacy"),
                    "Pressure": ("pressure",),
                },
            )

    @dataclass
    class Thermo(Device):
        value: int = 0

    @dataclass
    class Pressure(Device):
        value: int = 0

    legacy = Device.from_dict({"type": "thermo-legacy", "value": 7})
    assert type(legacy) is Thermo
    assert legacy.to_dict() == {"value": 7, "type": "thermo"}

    fresh = Device.from_dict({"type": "thermo", "value": 8})
    assert type(fresh) is Thermo
    assert fresh.to_dict() == {"value": 8, "type": "thermo"}

    pressure = Device.from_dict({"type": "pressure", "value": 9})
    assert type(pressure) is Pressure
    assert pressure.to_dict() == {"value": 9, "type": "pressure"}

    # Read-back ownership of the legacy and new values stays identical.
    assert type(Device.from_dict(legacy.to_dict())) is Thermo


def test_config_discriminator_with_widened_literal_field():
    @dataclass
    class Device(DataClassDictMixin):
        class Config(BaseConfig):
            discriminator = Discriminator(
                field="type",
                include_subtypes=True,
                variant_tags={
                    "Thermo": ("thermo", "thermo-legacy"),
                },
            )

    @dataclass
    class Thermo(Device):
        value: int = 0
        type: Literal["thermo", "thermo-legacy"] = "thermo"

    legacy = Device.from_dict({"type": "thermo-legacy", "value": 1})
    assert isinstance(legacy, Thermo)
    assert legacy.type == "thermo-legacy"
    serialized = legacy.to_dict()
    assert serialized == {"value": 1, "type": "thermo"}
    assert type(Device.from_dict(serialized)) is Thermo


# --------------------------------------------------------------------------
# Existing discriminators that do not opt into the new rules stay untouched.
# --------------------------------------------------------------------------


def test_legacy_tagger_discriminator_is_unchanged():
    @dataclass
    class Variant(DataClassDictMixin):
        class Config(BaseConfig):
            discriminator = Discriminator(
                field="type",
                include_subtypes=True,
                variant_tagger_fn=lambda cls: [
                    cls.__name__.lower(),
                    cls.__name__.upper(),
                ],
            )

    @dataclass
    class One(Variant):
        value: int = 0

    decoded = Variant.from_dict({"type": "one", "value": 5})
    assert type(decoded) is One
    # No declared discriminator field and no variant_tags: nothing extra is
    # written back, preserving the previous behaviour.
    assert decoded.to_dict() == {"value": 5}


def test_plain_field_discriminator_is_unchanged():
    @dataclass
    class Variant(DataClassDictMixin):
        class Config(BaseConfig):
            discriminator = Discriminator(
                field="type", include_subtypes=True
            )

    @dataclass
    class One(Variant):
        value: int = 0
        type: Literal["one"] = "one"

    decoded = Variant.from_dict({"type": "one", "value": 5})
    assert type(decoded) is One
    assert decoded.to_dict() == {"value": 5, "type": "one"}


# --------------------------------------------------------------------------
# Discriminator validation.
# --------------------------------------------------------------------------


def test_variant_tags_require_a_field():
    with pytest.raises(ValueError, match="discriminator field"):
        Discriminator(
            include_subtypes=True,
            variant_tags={"X": ("x",)},
        )


def test_variant_tags_reject_bad_keys_and_empty_values():
    with pytest.raises(ValueError, match="classes or class names"):
        Discriminator(
            field="type",
            include_subtypes=True,
            variant_tags={1: ("x",)},
        )
    with pytest.raises(ValueError, match="lists or tuples"):
        Discriminator(
            field="type",
            include_subtypes=True,
            variant_tags={"X": "x"},
        )
    with pytest.raises(ValueError, match="at least one tag"):
        Discriminator(
            field="type",
            include_subtypes=True,
            variant_tags={"X": ()},
        )

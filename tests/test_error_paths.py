import json
from dataclasses import dataclass, field
from typing import Annotated, Literal, Union

import pytest

from mashumaro import DataClassDictMixin
from mashumaro.codecs import BasicDecoder
from mashumaro.codecs.json import JSONDecoder
from mashumaro.config import BaseConfig
from mashumaro.exceptions import (
    InvalidFieldValue,
    MissingField,
    SuitableVariantNotFoundError,
)
from mashumaro.types import Discriminator

DEVICE_KEY = "plant.1[west].dev-7"


@dataclass
class ReadingBase:
    kind: str = ""


@dataclass
class TemperatureA(ReadingBase):
    kind: Literal["temperature"] = "temperature"
    sample_rate: int = 0

    class Config(BaseConfig):
        aliases = {"sample_rate": "sampleRate"}


@dataclass
class HumidityA(ReadingBase):
    kind: Literal["humidity"] = "humidity"
    sample_rate: int = 0

    class Config(BaseConfig):
        aliases = {"sample_rate": "sampleRate"}


@dataclass
class TemperatureB(ReadingBase):
    kind: Literal["temperature"] = "temperature"
    sample_rate: int = 0

    class Config(BaseConfig):
        aliases = {"sample_rate": ("sample_rate", "sampleRate")}


@dataclass
class HumidityB(ReadingBase):
    kind: Literal["humidity"] = "humidity"
    sample_rate: int = 0

    class Config(BaseConfig):
        aliases = {"sample_rate": ("sample_rate", "sampleRate")}


ReadingsA = list[
    Annotated[
        Union[TemperatureA, HumidityA],
        Discriminator(field="kind", include_supertypes=True),
    ]
]
ReadingsB = list[
    Annotated[
        Union[TemperatureB, HumidityB],
        Discriminator(field="kind", include_supertypes=True),
    ]
]


@dataclass
class DeviceConfigA:
    readings: ReadingsA


@dataclass
class DeviceConfigB:
    readings: ReadingsB


@dataclass
class BatchA(DataClassDictMixin):
    devices: dict[str, DeviceConfigA]


@dataclass
class BatchB(DataClassDictMixin):
    devices: dict[str, DeviceConfigB]


def payload(third, key=DEVICE_KEY):
    return {
        "devices": {
            key: {
                "readings": [
                    {"kind": "temperature", "sampleRate": 1},
                    {"kind": "humidity", "sampleRate": 2},
                    third,
                ]
            }
        }
    }


def test_received_key_and_third_element_are_located():
    with pytest.raises(InvalidFieldValue) as exc_info:
        BatchA.from_dict(
            payload({"kind": "temperature", "sampleRate": "not-an-int"})
        )

    exc = exc_info.value
    assert exc.path == ["devices", DEVICE_KEY, "readings", 2, "sampleRate"]
    assert exc.path[1] == DEVICE_KEY
    assert exc.path[3] == 2
    assert exc.path[4] == "sampleRate"


def test_each_configuration_reports_the_spelling_it_received():
    with pytest.raises(InvalidFieldValue) as exc_info:
        BatchA.from_dict(payload({"kind": "temperature", "sampleRate": "bad"}))
    assert exc_info.value.path[-1] == "sampleRate"

    with pytest.raises(InvalidFieldValue) as exc_info:
        BatchB.from_dict(
            payload({"kind": "temperature", "sample_rate": "bad"}),
        )
    assert exc_info.value.path[-1] == "sample_rate"

    with pytest.raises(InvalidFieldValue) as exc_info:
        BatchB.from_dict(
            payload({"kind": "temperature", "sampleRate": "bad"}),
        )
    assert exc_info.value.path[-1] == "sampleRate"


def test_alternating_configurations_do_not_share_aliases():
    for batch, third, expected in (
        (BatchA, {"kind": "temperature", "sampleRate": "bad"}, "sampleRate"),
        (BatchB, {"kind": "temperature", "sample_rate": "bad"}, "sample_rate"),
        (BatchA, {"kind": "humidity", "sampleRate": "bad"}, "sampleRate"),
        (BatchB, {"kind": "humidity", "sampleRate": "bad"}, "sampleRate"),
    ):
        with pytest.raises(InvalidFieldValue) as exc_info:
            batch.from_dict(payload(third))
        assert exc_info.value.path == [
            "devices",
            DEVICE_KEY,
            "readings",
            2,
            expected,
        ]


def test_other_subtype_can_be_selected_without_errors():
    instance = BatchB.from_dict(payload({"kind": "humidity", "sampleRate": 3}))
    assert isinstance(instance.devices[DEVICE_KEY].readings[2], HumidityB)
    assert instance.devices[DEVICE_KEY].readings[2].sample_rate == 3


def exception_chain(exc):
    seen = []
    while exc is not None:
        seen.append(exc)
        exc = exc.__context__
    return seen


def test_unknown_tag_and_invalid_value_are_distinguishable():
    with pytest.raises(InvalidFieldValue) as exc_info:
        BatchA.from_dict(payload({"kind": "unknown", "sampleRate": 3}))

    assert exc_info.value.path == ["devices", DEVICE_KEY, "readings", 2]
    assert any(
        isinstance(item, SuitableVariantNotFoundError)
        for item in exception_chain(exc_info.value)
    )

    with pytest.raises(InvalidFieldValue) as exc_info:
        BatchA.from_dict(payload({"kind": "temperature", "sampleRate": "bad"}))

    assert exc_info.value.path[-1] == "sampleRate"
    assert not any(
        isinstance(item, SuitableVariantNotFoundError)
        for item in exception_chain(exc_info.value)
    )


def test_existing_field_exception_attributes_are_unchanged():
    with pytest.raises(InvalidFieldValue) as exc_info:
        BatchA.from_dict(payload({"kind": "temperature", "sampleRate": "bad"}))
    leaf = next(
        item
        for item in reversed(exception_chain(exc_info.value))
        if isinstance(item, InvalidFieldValue)
        and item.field_name == "sample_rate"
    )
    assert leaf.field_name == "sample_rate"
    assert leaf.field_value == "bad"
    assert leaf.holder_class is TemperatureA


def test_missing_field_reports_the_expected_input_key():
    @dataclass
    class RequiredTemperature:
        sample_rate: int = field(metadata={"alias": "sampleRate"})
        kind: Literal["temperature"] = "temperature"

    @dataclass
    class RequiredDevice:
        readings: list[
            Annotated[
                Union[RequiredTemperature, HumidityA],
                Discriminator(field="kind", include_supertypes=True),
            ]
        ]

    @dataclass
    class RequiredBatch(DataClassDictMixin):
        devices: dict[str, RequiredDevice]

    with pytest.raises(InvalidFieldValue) as exc_info:
        RequiredBatch.from_dict(payload({"kind": "temperature"}))
    assert exc_info.value.path == [
        "devices",
        DEVICE_KEY,
        "readings",
        2,
        "sampleRate",
    ]
    assert any(
        isinstance(item, MissingField)
        for item in exception_chain(exc_info.value)
    )


def test_dict_and_json_codecs_report_the_same_path():
    data = payload({"kind": "temperature", "sampleRate": "bad"})

    paths = []
    for decode, value in (
        (BatchA.from_dict, data),
        (BasicDecoder(BatchA).decode, data),
        (JSONDecoder(BatchA).decode, json.dumps(data)),
    ):
        with pytest.raises(InvalidFieldValue) as exc_info:
            decode(value)
        paths.append(list(exc_info.value.path))

    assert paths[0] == paths[1] == paths[2]
    assert paths[0] == ["devices", DEVICE_KEY, "readings", 2, "sampleRate"]


def test_a_later_batch_does_not_inherit_a_previous_path():
    with pytest.raises(InvalidFieldValue):
        BatchA.from_dict(payload({"kind": "temperature", "sampleRate": "bad"}))

    good = BatchA.from_dict(payload({"kind": "temperature", "sampleRate": 9}))
    assert good.devices[DEVICE_KEY].readings[2].sample_rate == 9

    other_key = "plant.2[roof].dev-8"
    with pytest.raises(InvalidFieldValue) as exc_info:
        BatchA.from_dict(
            payload(
                {"kind": "temperature", "sampleRate": "bad"},
                key=other_key,
            )
        )
    assert exc_info.value.path == [
        "devices",
        other_key,
        "readings",
        2,
        "sampleRate",
    ]

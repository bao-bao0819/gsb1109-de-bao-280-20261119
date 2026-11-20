import json
from dataclasses import dataclass, field

from mashumaro import DataClassDictMixin
from mashumaro.config import BaseConfig
from mashumaro.mixins.json import DataClassJSONMixin


@dataclass
class Device(DataClassJSONMixin):
    retries: int = 0
    slots: list[int] = field(default_factory=list)

    class Config(BaseConfig):
        omit_default = True
        serialize_by_alias = True
        aliases = {"retries": "retry_count", "slots": "slot_ids"}
        preserve_explicit_fields = True


@dataclass
class DeviceBatch(DataClassJSONMixin):
    devices: list[Device]

    class Config(BaseConfig):
        omit_default = True


@dataclass
class PlainDevice(DataClassDictMixin):
    retries: int = 0
    slots: list[int] = field(default_factory=list)

    class Config(BaseConfig):
        omit_default = True


def test_explicit_defaults_survive_roundtrip_with_aliases():
    payload = {"devices": [{"retry_count": 0, "slot_ids": []}, {}]}
    batch = DeviceBatch.from_dict(payload)

    first, second = batch.devices
    assert first == second
    assert first.slots is not second.slots

    # swap the sub-objects and change one originally defaulted
    # field of the second device to a non-default value
    batch.devices = [second, first]
    second.retries = 3

    data = batch.to_dict()
    assert data == {
        "devices": [
            {"retry_count": 3},
            {"retry_count": 0, "slot_ids": []},
        ]
    }
    assert json.loads(batch.to_json()) == data

    # reset attribution follows the sub-object, not the list position
    redecoded = DeviceBatch.from_dict(data)
    assert redecoded.to_dict() == data
    assert json.loads(DeviceBatch.from_json(batch.to_json()).to_json()) == data


def test_explicit_fields_are_tracked_per_instance():
    payload_a = {"retry_count": 0}
    payload_b = {"slot_ids": []}
    device_a = Device.from_dict(payload_a)
    device_b = Device.from_dict(payload_b)

    assert device_a.to_dict() == {"retry_count": 0}
    assert device_b.to_dict() == {"slot_ids": []}
    # tracking one instance must not leak into another
    assert device_a.to_dict() == {"retry_count": 0}


def test_normally_constructed_objects_still_omit_defaults():
    assert Device().to_dict() == {}
    assert Device(retries=0, slots=[]).to_dict() == {}
    assert Device(retries=2).to_dict() == {"retry_count": 2}


def test_disabled_option_keeps_current_omit_default_behavior():
    device = PlainDevice.from_dict({"retries": 0, "slots": []})
    assert device.to_dict() == {}
    assert not hasattr(device, "__mashumaro_explicit_fields__")


def test_explicit_none_default_is_preserved():
    @dataclass
    class Nullable(DataClassDictMixin):
        value: int | None = None

        class Config(BaseConfig):
            omit_default = True
            preserve_explicit_fields = True

    assert Nullable.from_dict({"value": None}).to_dict() == {"value": None}
    assert Nullable.from_dict({}).to_dict() == {}
    assert Nullable().to_dict() == {}


def test_explicit_fields_with_frozen_dataclass():
    @dataclass(frozen=True)
    class FrozenDevice(DataClassDictMixin):
        retries: int = 0

        class Config(BaseConfig):
            omit_default = True
            preserve_explicit_fields = True

    assert FrozenDevice.from_dict({"retries": 0}).to_dict() == {"retries": 0}
    assert FrozenDevice.from_dict({}).to_dict() == {}

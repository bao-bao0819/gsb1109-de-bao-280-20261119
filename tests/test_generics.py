from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Generic, List, Mapping, Optional, TypeVar

import pytest
import typing_extensions

from mashumaro import DataClassDictMixin
from mashumaro.config import BaseConfig
from mashumaro.mixins.json import DataClassJSONMixin
from tests.entities import MyGenericDataClass, SerializableTypeGenericList

T = TypeVar("T")
S = TypeVar("S")
K = TypeVar("K")
P = TypeVar("P", Mapping[int, int], List[float])


@dataclass
class Foo(Generic[T], DataClassJSONMixin):
    x: T
    y: "Optional[Foo[Any]]"


@dataclass
class Bar(Foo): ...


def test_one_generic():
    @dataclass
    class A(Generic[T]):
        x: T

    @dataclass
    class B(A[datetime], DataClassDictMixin):
        pass

    obj = B(datetime(2021, 8, 15))
    assert B.from_dict({"x": "2021-08-15T00:00:00"}) == obj
    assert obj.to_dict() == {"x": "2021-08-15T00:00:00"}


def test_one_generic_list():
    @dataclass
    class A(List[T]):
        x: List[T]

    @dataclass
    class B(A[datetime], DataClassDictMixin):
        pass

    obj = B(x=[datetime(2021, 8, 15)])
    assert B.from_dict({"x": ["2021-08-15T00:00:00"]}) == obj
    assert obj.to_dict() == {"x": ["2021-08-15T00:00:00"]}


def test_two_generics():
    @dataclass
    class A1(Generic[T]):
        x: List[T]

    @dataclass
    class A2(Generic[T, S]):
        y: Mapping[T, S]

    @dataclass
    class B(A1[datetime], A2[datetime, date], DataClassDictMixin):
        pass

    obj = B(
        x=[datetime(2021, 8, 15), datetime(2021, 8, 16)],
        y={datetime(2021, 8, 17): date(2021, 8, 18)},
    )
    dump = {
        "x": ["2021-08-15T00:00:00", "2021-08-16T00:00:00"],
        "y": {"2021-08-17T00:00:00": "2021-08-18"},
    }
    assert B.from_dict(dump) == obj
    assert obj.to_dict() == dump


def test_partially_concrete_generic():
    @dataclass
    class A(Generic[T, S]):
        x: Mapping[T, S]

    @dataclass
    class B(A[datetime, S], DataClassDictMixin):
        pass

    obj = B(x={datetime(2022, 11, 21): "3.14"})
    assert B.from_dict({"x": {"2022-11-21T00:00:00": "3.14"}}) == obj
    assert obj.to_dict() == {"x": {"2022-11-21T00:00:00": "3.14"}}


def test_partially_concrete_generic_with_bound():
    @dataclass
    class A(Generic[T, P]):
        x: Mapping[T, P]

    @dataclass
    class B(A[date, P], DataClassDictMixin):
        pass

    obj1 = B(x={date(2022, 11, 21): {1: 2, 3: 4}})
    assert B.from_dict({"x": {"2022-11-21": {"1": "2", "3": "4"}}}) == obj1
    assert obj1.to_dict() == {"x": {"2022-11-21": {1: 2, 3: 4}}}
    obj2 = B(x={date(2022, 11, 21): [1.1, 3.3]})
    assert (
        B.from_dict({"x": {"2022-11-21": {"1.1": "2.2", "3.3": "4.4"}}})
        == obj2
    )
    assert obj2.to_dict() == {"x": {"2022-11-21": [1.1, 3.3]}}
    obj3 = B(x={date(2022, 11, 21): [1.1, 2.2, 3.3, 4.4]})
    assert (
        B.from_dict({"x": {"2022-11-21": ["1.1", "2.2", "3.3", "4.4"]}})
        == obj3
    )
    assert obj3.to_dict() == {"x": {"2022-11-21": [1.1, 2.2, 3.3, 4.4]}}


def test_concrete_generic_with_different_type_var():
    @dataclass
    class A(Generic[T]):
        x: T

    @dataclass
    class B(A[P], DataClassDictMixin):
        pass

    obj = B.from_dict({"x": {"1": "2", "3": "4"}})
    assert obj == B(x={1: 2, 3: 4})
    obj = B.from_dict({"x": {"1.1": "2.2", "3.3": "4.4"}})
    assert obj == B(x=[1.1, 3.3])
    obj = B.from_dict({"x": ["1.1", "2.2", "3.3", "4.4"]})
    assert obj == B(x=[1.1, 2.2, 3.3, 4.4])


def test_loose_generic_info_with_any_type():
    @dataclass
    class A(Generic[T]):
        x: T

    @dataclass
    class B(A, DataClassDictMixin):
        pass

    obj = B.from_dict({"x": {"1.1": "2.2", "3.3": "4.4"}})
    assert obj == B(x={"1.1": "2.2", "3.3": "4.4"})
    obj = B.from_dict({"x": ["1.1", "2.2", "3.3", "4.4"]})
    assert obj == B(x=["1.1", "2.2", "3.3", "4.4"])


def test_loose_generic_info_with_bound():
    @dataclass
    class A(Generic[P]):
        x: P

    @dataclass
    class B(A, DataClassDictMixin):
        pass

    obj = B.from_dict({"x": {"1": "2", "3": "4"}})
    assert obj == B(x={1: 2, 3: 4})
    obj = B.from_dict({"x": {"1.1": "2.2", "3.3": "4.4"}})
    assert obj == B(x=[1.1, 3.3])
    obj = B.from_dict({"x": ["1.1", "2.2", "3.3", "4.4"]})
    assert obj == B(x=[1.1, 2.2, 3.3, 4.4])


def test_loose_generic_info_with_constraints_and_default():
    DateOrString = typing_extensions.TypeVar(
        "DateOrString", str, date, default=date
    )

    @dataclass
    class Constrained(Generic[DateOrString], DataClassDictMixin):
        value: DateOrString

    @dataclass
    class StringConstrained(Constrained[str]):
        pass

    assert Constrained.from_dict({"value": "2023-01-01"}) == Constrained(
        value=date(2023, 1, 1)
    )
    assert Constrained(value=date(2023, 1, 1)).to_dict() == {
        "value": "2023-01-01"
    }
    assert StringConstrained.from_dict(
        {"value": "2023-01-01"}
    ) == StringConstrained(value="2023-01-01")


def test_loose_generic_info_in_first_generic():
    @dataclass
    class A(Generic[P]):
        x: P

    @dataclass
    class B(A):
        pass

    @dataclass
    class C(B, Generic[P]):
        y: P

    @dataclass
    class D(C[List[float]], DataClassDictMixin):
        pass

    obj = D.from_dict({"x": {"1": "2"}, "y": {"3.3": "4.4"}})
    assert obj == D(x={1: 2}, y=[3.3])
    obj = D.from_dict({"x": {"1.1": "2.2"}, "y": {"3.3": "4.4"}})
    assert obj == D(x=[1.1], y=[3.3])


def test_not_dataclass_generic():
    class MyGeneric(Generic[P, T]):
        pass

    @dataclass
    class GenericDataClass(Generic[P]):
        x: P

    @dataclass
    class DataClass(MyGeneric[P, T], GenericDataClass[P]):
        pass

    @dataclass
    class ConcreteDataClass(DataClass[List[float], float], DataClassDictMixin):
        pass

    obj = ConcreteDataClass.from_dict({"x": {"1": "2", "3": "4"}})
    assert obj == ConcreteDataClass(x=[1.0, 3.0])


def test_generic_dataclass_as_field_type():
    @dataclass
    class DataClass(DataClassDictMixin):
        date: MyGenericDataClass[date]
        str: MyGenericDataClass[str]

    obj = DataClass(
        date=MyGenericDataClass(date(2021, 9, 14)),
        str=MyGenericDataClass("2021-09-14"),
    )
    dictionary = {"date": {"x": "2021-09-14"}, "str": {"x": "2021-09-14"}}
    assert DataClass.from_dict(dictionary) == obj
    assert obj.to_dict() == dictionary


def test_serializable_type_generic_class():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: SerializableTypeGenericList[str]

    obj = DataClass(SerializableTypeGenericList(["a", "b", "c"]))
    assert DataClass.from_dict({"x": ["a", "b", "c"]}) == obj
    assert obj.to_dict() == {"x": ["a", "b", "c"]}


def test_self_referenced_generic_no_max_recursion_error():
    obj = Bar(42, Foo(33, None))
    assert obj.to_dict() == {"x": 42, "y": {"x": 33, "y": None}}
    assert Bar.from_dict({"x": 42, "y": {"x": 33, "y": None}}) == obj
    assert obj.to_json() == '{"x": 42, "y": {"x": 33, "y": null}}'
    assert Bar.from_json('{"x": 42, "y": {"x": 33, "y": null}}') == obj


@pytest.mark.parametrize("lazy", [True, False])
def test_nested_generic_no_inf_recursion(lazy):

    T = TypeVar("T")

    @dataclass
    class A(Generic[T], DataClassDictMixin):
        class Config(BaseConfig):
            lazy_compilation = lazy

        field: T

    @dataclass
    class B(Generic[T], DataClassDictMixin):
        class Config(BaseConfig):
            lazy_compilation = lazy

        fieldB: A[T]

    @dataclass
    class C(Generic[T], DataClassDictMixin):
        class Config(BaseConfig):
            lazy_compilation = lazy

        fieldC: B[T]

    @dataclass
    class D(C[int], B[int], Generic[T], DataClassDictMixin):
        class Config(BaseConfig):
            lazy_compilation = lazy

        fieldD: A[T]

    obj = D(
        fieldD=A(field=1.2), fieldC=B(fieldB=A(field=2)), fieldB=A(field=2)
    )
    assert D.from_dict(obj.to_dict()) == obj


def test_vars_order_when_generic_presented_in_bases() -> None:
    @dataclass
    class Base(DataClassDictMixin, Generic[T]):
        kind: str = "base"

    class NotSerializable:
        pass

    @dataclass
    class Extended(Base[K], Generic[S, K]):
        payload: S | None = None

    @dataclass
    class Sub(Extended[Base, NotSerializable]):
        pass

    assert Sub(payload=Base()).to_dict() == {
        "kind": "base",
        "payload": {"kind": "base"},
    }


def _make_same_named_entry(field_name: str) -> type:
    """Build a fresh class whose module/qualname match prior entries."""
    namespace: dict[str, Any] = {}
    exec(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class Entry:\n"
        f"    {field_name}: int\n",
        namespace,
    )
    entry = namespace["Entry"]
    entry.__module__ = "tests.same_named_entries"
    entry.__qualname__ = "Entry"
    return entry


@dataclass
class Envelope(Generic[T]):
    payload: T


@dataclass
class EnvelopeWithLabel(Envelope[T], Generic[T]):
    label: str = "x"


def _make_record_with_entry(entry: type, name: str) -> type:
    """Build a record holding ``EnvelopeWithLabel[entry]``.

    The record is assembled dynamically so each factory call can pass a
    distinct class that shares its module and qualname with the previous one.
    """
    namespace: dict[str, Any] = {
        "EnvelopeWithLabel": EnvelopeWithLabel,
        "entry": entry,
        "DataClassDictMixin": DataClassDictMixin,
        "dataclass": dataclass,
    }
    exec(
        "@dataclass\n"
        f"class {name}(DataClassDictMixin):\n"
        "    envelope: EnvelopeWithLabel[entry]\n",
        namespace,
    )
    return namespace[name]


def _assert_same_named_generic_entries_are_isolated(old_first: bool) -> None:
    old_entry = _make_same_named_entry("amount")
    new_entry = _make_same_named_entry("status")
    assert old_entry.__module__ == new_entry.__module__
    assert old_entry.__qualname__ == new_entry.__qualname__
    assert old_entry is not new_entry

    if old_first:
        old_record = _make_record_with_entry(old_entry, "OldRecord")
        first = old_record(
            envelope=EnvelopeWithLabel(
                payload=old_entry(amount=1), label="first"
            )
        )
        reloaded = old_record.from_dict(first.to_dict())
        assert type(reloaded.envelope.payload) is old_entry
        new_record = _make_record_with_entry(new_entry, "NewRecord")
    else:
        new_record = _make_record_with_entry(new_entry, "NewRecord")
        first = new_record(
            envelope=EnvelopeWithLabel(
                payload=new_entry(status=1), label="first"
            )
        )
        assert type(
            new_record.from_dict(first.to_dict()).envelope.payload
        ) is new_entry
        old_record = _make_record_with_entry(old_entry, "OldRecord")

    old_obj = old_record(
        envelope=EnvelopeWithLabel(
            payload=old_entry(amount=5), label="old"
        )
    )
    new_obj = new_record(
        envelope=EnvelopeWithLabel(
            payload=new_entry(status=7), label="new"
        )
    )

    for _ in range(3):
        assert old_obj.to_dict() == {
            "envelope": {"payload": {"amount": 5}, "label": "old"}
        }
        assert new_obj.to_dict() == {
            "envelope": {"payload": {"status": 7}, "label": "new"}
        }

        old_loaded = old_record.from_dict(
            {"envelope": {"payload": {"amount": 9}, "label": "old"}}
        )
        assert type(old_loaded.envelope.payload) is old_entry
        assert old_loaded.envelope.payload.amount == 9
        assert not hasattr(old_loaded.envelope.payload, "status")

        new_loaded = new_record.from_dict(
            {"envelope": {"payload": {"status": 11}, "label": "new"}}
        )
        assert type(new_loaded.envelope.payload) is new_entry
        assert new_loaded.envelope.payload.status == 11
        assert not hasattr(new_loaded.envelope.payload, "amount")


@pytest.mark.parametrize("old_first", [True, False])
def test_same_named_generic_entries_are_isolated(old_first: bool) -> None:
    _assert_same_named_generic_entries_are_isolated(old_first)


def test_same_named_entry_has_its_own_converter() -> None:
    old_entry = _make_same_named_entry("amount")
    old_record = _make_record_with_entry(old_entry, "OldRecord")
    old_obj = old_record(
        envelope=EnvelopeWithLabel(payload=old_entry(amount=1))
    )
    assert old_obj.to_dict() == {
        "envelope": {"payload": {"amount": 1}, "label": "x"}
    }

    new_entry = _make_same_named_entry("status")
    new_record = _make_record_with_entry(new_entry, "NewRecord")
    new_obj = new_record(
        envelope=EnvelopeWithLabel(payload=new_entry(status=2))
    )

    # The new entry is a different class of the same name, so it must have
    # gotten its own converter instead of reusing the old entry's one.
    assert hasattr(new_entry, "__mashumaro_to_dict__")
    assert hasattr(new_entry, "__mashumaro_from_dict__")
    assert new_obj.to_dict() == {
        "envelope": {"payload": {"status": 2}, "label": "x"}
    }

    # Converting one record must not turn its entry into the other class.
    reloaded_new = new_record.from_dict(new_obj.to_dict())
    assert type(reloaded_new.envelope.payload) is new_entry
    assert reloaded_new.envelope.payload.status == 2

    reloaded_old = old_record.from_dict(old_obj.to_dict())
    assert type(reloaded_old.envelope.payload) is old_entry
    assert reloaded_old.envelope.payload.amount == 1

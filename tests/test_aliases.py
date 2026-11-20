from dataclasses import dataclass, field
from typing import Optional

import pytest
from typing_extensions import Annotated

from mashumaro import DataClassDictMixin, pass_through
from mashumaro.config import (
    TO_DICT_ADD_BY_ALIAS_FLAG,
    TO_DICT_ADD_OMIT_NONE_FLAG,
    BaseConfig,
)
from mashumaro.exceptions import (
    AmbiguousFieldAliasError,
    ConflictingFieldValues,
    ExtraKeysError,
    InvalidFieldValue,
    MissingField,
)
from mashumaro.types import Alias


@dataclass
class Aliased(DataClassDictMixin):
    a: int = field(metadata={"alias": "alias_a"})
    b: Annotated[int, Alias("alias_b")]


@dataclass
class AliasedWithSerializeByAliasFlag(DataClassDictMixin):
    a: int = field(metadata={"alias": "alias_a"})
    b: Annotated[int, Alias("alias_b")]

    class Config(BaseConfig):
        code_generation_options = [TO_DICT_ADD_BY_ALIAS_FLAG]


def test_alias():
    assert Aliased.from_dict({"alias_a": 123, "alias_b": 456}) == Aliased(
        a=123, b=456
    )
    with pytest.raises(MissingField):
        assert Aliased.from_dict({"a": 123, "alias_b": 456})
    with pytest.raises(MissingField):
        assert Aliased.from_dict({"alias_a": 123, "b": 456})


def test_alias_with_default():
    @dataclass
    class DataClass(DataClassDictMixin):
        a: int = field(default=111, metadata={"alias": "alias_a"})
        b: Annotated[int, Alias("alias_b")] = 222

    assert DataClass.from_dict({"alias_a": 123, "alias_b": 456}) == DataClass(
        a=123, b=456
    )
    assert DataClass.from_dict({}) == DataClass(a=111, b=222)


def test_alias_with_omit_none():
    @dataclass
    class DataClass(DataClassDictMixin):
        a: Optional[int] = field(default=None, metadata={"alias": "alias_a"})
        b: Annotated[Optional[int], Alias("alias_b")] = None

        class Config(BaseConfig):
            code_generation_options = [
                TO_DICT_ADD_BY_ALIAS_FLAG,
                TO_DICT_ADD_OMIT_NONE_FLAG,
            ]

    instance = DataClass()
    assert instance.to_dict(omit_none=True) == {}
    assert instance.to_dict(by_alias=True) == {
        "alias_a": None,
        "alias_b": None,
    }
    assert instance.to_dict(omit_none=True, by_alias=True) == {}
    instance = DataClass(a=123, b=456)
    assert instance.to_dict(omit_none=True) == {"a": 123, "b": 456}
    assert instance.to_dict(by_alias=True) == {"alias_a": 123, "alias_b": 456}
    assert instance.to_dict(omit_none=True, by_alias=True) == {
        "alias_a": 123,
        "alias_b": 456,
    }


def test_serialize_by_alias_config_option():
    @dataclass
    class DataClass(DataClassDictMixin):
        a: int = field(metadata={"alias": "alias_a"})
        b: Annotated[int, Alias("alias_b")]

        class Config(BaseConfig):
            serialize_by_alias = True

    assert DataClass(123, 456).to_dict() == {"alias_a": 123, "alias_b": 456}


def test_serialize_by_alias_code_generation_flag():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(metadata={"alias": "alias_x"})
        y: Annotated[int, Alias("alias_y")]

        class Config(BaseConfig):
            code_generation_options = [TO_DICT_ADD_BY_ALIAS_FLAG]

    instance = DataClass(x=123, y=456)
    assert instance.to_dict() == {"x": 123, "y": 456}
    assert instance.to_dict(by_alias=True) == {"alias_x": 123, "alias_y": 456}


def test_serialize_by_alias_code_generation_flag_without_alias():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int

        class Config(BaseConfig):
            code_generation_options = [TO_DICT_ADD_BY_ALIAS_FLAG]

    instance = DataClass(x=123)
    assert instance.to_dict() == {"x": 123}
    assert instance.to_dict(by_alias=True) == {"x": 123}


def test_no_serialize_by_alias_code_generation_flag():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(metadata={"alias": "alias_x"})
        y: Annotated[int, Alias("alias_y")]

    instance = DataClass(x=123, y=456)
    assert instance.to_dict() == {"x": 123, "y": 456}
    with pytest.raises(TypeError):
        instance.to_dict(by_alias=True)


def test_serialize_by_alias_flag_for_inner_class_without_it():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: Aliased = field(metadata={"alias": "alias_x"})
        y: Annotated[Aliased, Alias("alias_y")]

        class Config(BaseConfig):
            code_generation_options = [TO_DICT_ADD_BY_ALIAS_FLAG]

    instance = DataClass(Aliased(a=1, b=2), Aliased(a=3, b=4))
    assert instance.to_dict() == {"x": {"a": 1, "b": 2}, "y": {"a": 3, "b": 4}}
    assert instance.to_dict(by_alias=True) == {
        "alias_x": {"a": 1, "b": 2},
        "alias_y": {"a": 3, "b": 4},
    }


def test_serialize_by_alias_flag_for_inner_class_with_it():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: AliasedWithSerializeByAliasFlag = field(
            metadata={"alias": "alias_x"}
        )
        y: Annotated[AliasedWithSerializeByAliasFlag, Alias("alias_y")]

        class Config(BaseConfig):
            code_generation_options = [TO_DICT_ADD_BY_ALIAS_FLAG]

    instance = DataClass(
        AliasedWithSerializeByAliasFlag(a=1, b=2),
        AliasedWithSerializeByAliasFlag(a=3, b=4),
    )
    assert instance.to_dict() == {"x": {"a": 1, "b": 2}, "y": {"a": 3, "b": 4}}
    assert instance.to_dict(by_alias=True) == {
        "alias_x": {"alias_a": 1, "alias_b": 2},
        "alias_y": {"alias_a": 3, "alias_b": 4},
    }


def test_aliases_in_config():
    @dataclass
    class DataClass(DataClassDictMixin):
        a: int = 111
        b: Annotated[int, Alias("alias_b")] = 222

        class Config(BaseConfig):
            aliases = {"a": "alias_a", "b": "alias_c"}
            serialize_by_alias = True

    assert DataClass.from_dict({"alias_a": 123, "alias_b": 456}) == DataClass(
        a=123, b=456
    )
    assert DataClass.from_dict({}) == DataClass(a=111, b=222)
    assert DataClass(a=123, b=456).to_dict() == {
        "alias_a": 123,
        "alias_b": 456,
    }


def test_by_alias_with_serialize_by_alias():
    @dataclass
    class DataClass(DataClassDictMixin):
        a: int = field(metadata={"alias": "alias_a"})
        b: Annotated[int, Alias("alias_b")]

        class Config(BaseConfig):
            serialize_by_alias = True
            code_generation_options = [TO_DICT_ADD_BY_ALIAS_FLAG]

    instance = DataClass(a=123, b=456)
    assert DataClass.from_dict({"alias_a": 123, "alias_b": 456}) == instance
    assert instance.to_dict() == {"alias_a": 123, "alias_b": 456}
    assert instance.to_dict(by_alias=False) == {"a": 123, "b": 456}


def test_no_serialize_by_alias_with_serialize_by_alias_and_optional():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: Optional[int] = field(metadata={"alias": "alias_x"})
        y: Annotated[Optional[int], Alias("alias_y")]

        class Config(BaseConfig):
            serialize_by_alias = True

    assert DataClass(x=123, y=456).to_dict() == {
        "alias_x": 123,
        "alias_y": 456,
    }
    assert DataClass(x=None, y=None).to_dict() == {
        "alias_x": None,
        "alias_y": None,
    }


def test_by_field_with_allow_deserialization_not_by_alias():
    @dataclass
    class DataClass(DataClassDictMixin):
        a1: int = field(metadata={"alias": "alias_a1"})
        a2: Annotated[int, Alias("alias_a2")]
        b1: Optional[int] = field(metadata={"alias": "alias_b1"})
        b2: Annotated[Optional[int], Alias("alias_b2")]
        c1: Optional[str] = field(metadata={"alias": "alias_c1"})
        c2: Annotated[Optional[str], Alias("alias_c2")]
        d1: int = field(
            metadata={"alias": "alias_d1", "deserialize": pass_through}
        )
        d2: Annotated[int, Alias("alias_d2")] = field(
            metadata={"deserialize": pass_through}
        )
        e1: int = field(metadata={"alias": "alias_e1"}, default=5)
        e2: Annotated[int, Alias("alias_e2")] = 5
        f1: Optional[int] = field(metadata={"alias": "alias_f1"}, default=6)
        f2: Annotated[Optional[int], Alias("alias_f2")] = 6
        g1: Optional[str] = field(metadata={"alias": "alias_g1"}, default="7")
        g2: Annotated[Optional[str], Alias("alias_g2")] = "7"
        h1: int = field(
            metadata={"alias": "alias_h1", "deserialize": pass_through},
            default=8,
        )
        h2: Annotated[int, Alias("alias_h2")] = field(
            metadata={"deserialize": pass_through}, default=8
        )

        class Config(BaseConfig):
            serialize_by_alias = True
            code_generation_options = [TO_DICT_ADD_BY_ALIAS_FLAG]
            allow_deserialization_not_by_alias = True

    instance = DataClass(a1=1, a2=1, b1=2, b2=2, c1="3", c2="3", d1=4, d2=4)
    assert (
        DataClass.from_dict(
            {
                "a1": 1,
                "a2": 1,
                "b1": 2,
                "b2": 2,
                "c1": "3",
                "c2": "3",
                "d1": 4,
                "d2": 4,
                "e1": 5,
                "e2": 5,
                "f1": 6,
                "f2": 6,
                "g1": "7",
                "g2": "7",
                "h1": 8,
                "h2": 8,
            }
        )
        == instance
    )
    assert (
        DataClass.from_dict(
            {
                "alias_a1": 1,
                "alias_a2": 1,
                "alias_b1": 2,
                "alias_b2": 2,
                "alias_c1": "3",
                "alias_c2": "3",
                "alias_d1": 4,
                "alias_d2": 4,
                "alias_e1": 5,
                "alias_e2": 5,
                "alias_f1": 6,
                "alias_f2": 6,
                "alias_g1": "7",
                "alias_g2": "7",
                "alias_h1": 8,
                "alias_h2": 8,
            }
        )
        == instance
    )
    assert instance.to_dict() == {
        "alias_a1": 1,
        "alias_a2": 1,
        "alias_b1": 2,
        "alias_b2": 2,
        "alias_c1": "3",
        "alias_c2": "3",
        "alias_d1": 4,
        "alias_d2": 4,
        "alias_e1": 5,
        "alias_e2": 5,
        "alias_f1": 6,
        "alias_f2": 6,
        "alias_g1": "7",
        "alias_g2": "7",
        "alias_h1": 8,
        "alias_h2": 8,
    }
    assert instance.to_dict(by_alias=False) == {
        "a1": 1,
        "a2": 1,
        "b1": 2,
        "b2": 2,
        "c1": "3",
        "c2": "3",
        "d1": 4,
        "d2": 4,
        "e1": 5,
        "e2": 5,
        "f1": 6,
        "f2": 6,
        "g1": "7",
        "g2": "7",
        "h1": 8,
        "h2": 8,
    }


def test_order_of_metadata_and_annotated():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: Annotated[int, Alias("foo")] = field(metadata={"alias": "bar"})

        class Config(BaseConfig):
            serialize_by_alias = True

    instance = DataClass(42)
    assert DataClass.from_dict({"bar": 42}) == instance
    assert instance.to_dict() == {"bar": 42}


def test_multiple_aliases_field_option():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(metadata={"alias": ["id", "product_id"]})

    assert DataClass.from_dict({"id": 1}) == DataClass(1)
    assert DataClass.from_dict({"product_id": 2}) == DataClass(2)
    # the field name is not an accepted key unless explicitly allowed
    with pytest.raises(MissingField):
        DataClass.from_dict({"x": 3})


def test_empty_aliases_field_option():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(metadata={"alias": []})

    assert DataClass.from_dict({"x": 1}) == DataClass(1)
    with pytest.raises(MissingField):
        DataClass.from_dict({})


def test_multiple_aliases_config_option():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int

        class Config(BaseConfig):
            aliases = {"x": ["id", "product_id"]}

    assert DataClass.from_dict({"id": 1}) == DataClass(1)
    assert DataClass.from_dict({"product_id": 2}) == DataClass(2)


def test_empty_aliases_config_option():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int

        class Config(BaseConfig):
            aliases = {"x": []}

    assert DataClass.from_dict({"x": 1}) == DataClass(1)
    with pytest.raises(MissingField):
        DataClass.from_dict({})


def test_multiple_aliases_annotated():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: Annotated[int, Alias("id"), Alias("product_id")]

    assert DataClass.from_dict({"id": 1}) == DataClass(1)
    assert DataClass.from_dict({"product_id": 2}) == DataClass(2)


def test_multiple_aliases_missing():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(metadata={"alias": ["id", "product_id"]})

    with pytest.raises(MissingField):
        DataClass.from_dict({"other": 1})


def test_multiple_aliases_with_default():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(default=99, metadata={"alias": ["id", "product_id"]})

    assert DataClass.from_dict({"product_id": 2}) == DataClass(2)
    assert DataClass.from_dict({}) == DataClass(99)


def test_empty_aliases_with_default():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(default=99, metadata={"alias": []})

    assert DataClass.from_dict({"x": 2}) == DataClass(2)
    assert DataClass.from_dict({}) == DataClass(99)


def test_multiple_aliases_serialize_uses_first():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(metadata={"alias": ["id", "product_id"]})

        class Config(BaseConfig):
            code_generation_options = [TO_DICT_ADD_BY_ALIAS_FLAG]

    # serialization writes to the first (primary) alias
    assert DataClass(5).to_dict(by_alias=True) == {"id": 5}
    assert DataClass(5).to_dict() == {"x": 5}


def test_multiple_aliases_allow_deserialization_not_by_alias():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(metadata={"alias": ["id", "product_id"]})

        class Config(BaseConfig):
            allow_deserialization_not_by_alias = True

    assert DataClass.from_dict({"id": 1}) == DataClass(1)
    assert DataClass.from_dict({"product_id": 2}) == DataClass(2)
    # the field name is accepted as a fallback
    assert DataClass.from_dict({"x": 3}) == DataClass(3)


def test_multiple_aliases_forbid_extra_keys():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(metadata={"alias": ["id", "product_id"]})

        class Config(BaseConfig):
            forbid_extra_keys = True

    # every alias is an allowed key
    assert DataClass.from_dict({"id": 1}) == DataClass(1)
    assert DataClass.from_dict({"product_id": 2}) == DataClass(2)
    with pytest.raises(ExtraKeysError):
        DataClass.from_dict({"id": 1, "unexpected": 2})


def test_multiple_aliases_field_name_listed_as_alias():
    # The field name itself may appear among the aliases; combined with
    # allow_deserialization_not_by_alias it must not matter how often a key
    # repeats, and decoding by any of them works.
    @dataclass
    class DataClass(DataClassDictMixin):
        x: int = field(metadata={"alias": ["x", "id"]})

        class Config(BaseConfig):
            allow_deserialization_not_by_alias = True

    assert DataClass.from_dict({"x": 1}) == DataClass(1)
    assert DataClass.from_dict({"id": 2}) == DataClass(2)


@dataclass
class _Order(DataClassDictMixin):
    quantity: Annotated[int, Alias("new_quantity"), Alias("old_quantity")]
    product_code: Annotated[
        str, Alias("new_product_code"), Alias("old_product_code")
    ]
    note: Annotated[
        Optional[str], Alias("new_note"), Alias("old_note")
    ] = None
    legacy_only: Annotated[int, Alias("old_legacy")] = 7

    class Config(BaseConfig):
        serialize_by_alias = True


def test_multiple_keys_converted_with_field_rules_before_comparison():
    # The old quantity key carries the string "01" while the new one carries
    # the number 1. Both are ints after the field's own conversion, so the
    # payload is accepted rather than rejected because raw values differ.
    order = _Order.from_dict(
        {
            "new_quantity": 1,
            "old_quantity": "01",
            "new_product_code": "007",
            "old_product_code": "007",
        }
    )
    assert order.quantity == 1
    assert order.product_code == "007"


def test_multiple_keys_serialize_only_canonical_new_key():
    order = _Order.from_dict(
        {
            "new_quantity": 1,
            "old_quantity": "01",
            "new_product_code": "007",
            "old_product_code": "007",
        }
    )
    assert order.to_dict() == {
        "new_quantity": 1,
        "new_product_code": "007",
        "new_note": None,
        "old_legacy": 7,
    }


def test_multiple_keys_text_field_leading_zeros_stay_distinct():
    # product_code is a str field, so "007" and "7" are different values
    # even though the quantity field accepts "01" and 1 as equal.
    with pytest.raises(ConflictingFieldValues) as exc_info:
        _Order.from_dict(
            {
                "new_quantity": 1,
                "old_quantity": "01",
                "new_product_code": "007",
                "old_product_code": "7",
            }
        )
    exc = exc_info.value
    assert exc.field_name == "product_code"
    assert set(exc.keys) == {"new_product_code", "old_product_code"}
    assert exc.key_to_value == {
        "new_product_code": "007",
        "old_product_code": "7",
    }


def test_multiple_keys_bad_value_is_reported_with_its_key():
    with pytest.raises(InvalidFieldValue) as exc_info:
        _Order.from_dict(
            {
                "new_quantity": 1,
                "old_quantity": "not-a-number",
                "new_product_code": "007",
                "old_product_code": "007",
            }
        )
    exc = exc_info.value
    assert exc.field_name == "quantity"
    assert exc.key == "old_quantity"
    assert exc.field_value == "not-a-number"


def test_bad_value_is_not_masked_by_valid_value_of_another_key():
    # The first key holds a valid value, but every present key is converted
    # before a value is selected, so the bad second key must still fail.
    with pytest.raises(InvalidFieldValue) as exc_info:
        _Order.from_dict(
            {
                "new_quantity": 1,
                "old_quantity": object(),
                "new_product_code": "007",
                "old_product_code": "007",
            }
        )
    exc = exc_info.value
    assert exc.field_name == "quantity"
    assert exc.key == "old_quantity"


def test_single_new_key_order_and_single_old_key_order():
    assert _Order.from_dict(
        {
            "new_quantity": 1,
            "new_product_code": "007",
        }
    ) == _Order(quantity=1, product_code="007")
    assert _Order.from_dict(
        {
            "old_quantity": "01",
            "old_product_code": "007",
        }
    ) == _Order(quantity=1, product_code="007")


def test_multiple_keys_default_applies_only_when_both_absent():
    assert _Order.from_dict(
        {"new_quantity": 1, "new_product_code": "007"}
    ).legacy_only == 7
    assert _Order.from_dict(
        {
            "new_quantity": 1,
            "new_product_code": "007",
            "old_legacy": 8,
        }
    ).legacy_only == 8


def test_nested_order_fields_use_their_own_keys():
    @dataclass
    class Line(DataClassDictMixin):
        quantity: Annotated[int, Alias("new_quantity"), Alias("old_quantity")]

    @dataclass
    class Document(DataClassDictMixin):
        line: Line = field(metadata={"alias": ["new_line", "old_line"]})

    # Both outer keys are present and equivalent; each nested record is
    # decoded according to the Line field names, not the Document ones.
    document = Document.from_dict(
        {
            "new_line": {"new_quantity": 1, "old_quantity": "01"},
            "old_line": {"new_quantity": 1, "old_quantity": "01"},
        }
    )
    assert document.line == Line(quantity=1)

    with pytest.raises(ConflictingFieldValues) as exc_info:
        Document.from_dict(
            {
                "new_line": {"new_quantity": 1, "old_quantity": "01"},
                "old_line": {"new_quantity": 2, "old_quantity": "02"},
            }
        )
    assert exc_info.value.field_name == "line"


def test_ambiguous_config_raises_at_compile_time_before_reading_data():
    with pytest.raises(AmbiguousFieldAliasError) as exc_info:

        @dataclass
        class Ambiguous(DataClassDictMixin):
            first: int = 0
            second: int = 0

            class Config(BaseConfig):
                aliases = {
                    "first": ["shared"],
                    "second": ["shared"],
                }

    exc = exc_info.value
    assert exc.input_key == "shared"
    assert set(exc.field_names) == {"first", "second"}


def test_ambiguous_config_does_not_double_dispatch():
    with pytest.raises(AmbiguousFieldAliasError) as exc_info:

        @dataclass
        class Ambiguous(DataClassDictMixin):
            first: int = 0
            second: int = 0

            class Config(BaseConfig):
                aliases = {
                    "first": ["new_first", "shared"],
                    "second": ["new_second", "shared"],
                }

    assert exc_info.value.input_key == "shared"
    assert set(exc_info.value.field_names) == {"first", "second"}


def test_extra_keys_and_defaults_kept_with_multiple_keys():
    @dataclass
    class DataClass(DataClassDictMixin):
        x: Annotated[int, Alias("new_x"), Alias("old_x")] = 5

        class Config(BaseConfig):
            forbid_extra_keys = True

    assert DataClass.from_dict({"new_x": 1, "old_x": "01"}) == DataClass(1)
    assert DataClass.from_dict({}) == DataClass(5)
    with pytest.raises(ExtraKeysError) as exc_info:
        DataClass.from_dict({"new_x": 1, "extra": 2})
    assert exc_info.value.extra_keys == {"extra"}

    @dataclass
    class NoStrictKeys(DataClassDictMixin):
        x: Annotated[int, Alias("new_x"), Alias("old_x")] = 5

    # Undeclared extra keys continue to be ignored when not forbidden.
    assert NoStrictKeys.from_dict(
        {"new_x": 1, "old_x": "01", "extra": 2}
    ) == NoStrictKeys(1)

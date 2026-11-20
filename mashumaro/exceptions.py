from collections.abc import Mapping, Sequence
from typing import Any

from typing_extensions import TypeForm

from mashumaro.core.meta.helpers import type_name


class MissingField(LookupError):
    def __init__(self, field_name: str, field_type: Any, holder_class: type):
        self.field_name = field_name
        self.field_type = field_type
        self.holder_class = holder_class

    @property
    def field_type_name(self) -> str:
        return type_name(self.field_type, short=True)

    @property
    def holder_class_name(self) -> str:
        return type_name(self.holder_class, short=True)

    def __str__(self) -> str:
        return (
            f'Field "{self.field_name}" of type {self.field_type_name}'
            f" is missing in {self.holder_class_name} instance"
        )


class ExtraKeysError(ValueError):
    def __init__(self, extra_keys: set[str], target_type: type):
        self.extra_keys = extra_keys
        self.target_type = target_type

    @property
    def target_class_name(self) -> str:
        return type_name(self.target_type, short=True)

    def __str__(self) -> str:
        extra_keys_str = ", ".join(k for k in self.extra_keys)
        return (
            "Serialized dict has keys that are not defined in "
            f"{self.target_class_name}: {extra_keys_str}"
        )


class UnserializableDataError(TypeError):
    pass


class UnserializableField(UnserializableDataError):
    def __init__(
        self,
        field_name: str,
        field_type: Any,
        holder_class: type,
        msg: str | None = None,
    ):
        self.field_name = field_name
        self.field_type = field_type
        self.holder_class = holder_class
        self.msg = msg

    @property
    def field_type_name(self) -> str:
        return type_name(self.field_type, short=True)

    @property
    def holder_class_name(self) -> str:
        return type_name(self.holder_class, short=True)

    def __str__(self) -> str:
        s = (
            f'Field "{self.field_name}" of type {self.field_type_name} '
            f"in {self.holder_class_name} is not serializable"
        )
        if self.msg:
            s += f": {self.msg}"
        return s


class UnsupportedSerializationEngine(UnserializableField):
    def __init__(
        self, field_name: str, field_type: Any, holder_class: type, engine: Any
    ):
        super().__init__(
            field_name,
            field_type,
            holder_class,
            msg=f'Unsupported serialization engine "{engine}"',
        )


class UnsupportedDeserializationEngine(UnserializableField):
    def __init__(
        self, field_name: str, field_type: Any, holder_class: type, engine: Any
    ):
        super().__init__(
            field_name,
            field_type,
            holder_class,
            msg=f'Unsupported deserialization engine "{engine}"',
        )


class InvalidFieldValue(ValueError):
    def __init__(
        self,
        field_name: str,
        field_type: Any,
        field_value: Any,
        holder_class: type,
        msg: str | None = None,
        key: str | None = None,
    ):
        self.field_name = field_name
        self.field_type = field_type
        self.field_value = field_value
        self.holder_class = holder_class
        self.msg = msg
        self.key = key
        self.key_name = key
        self.input_key = key

    @property
    def field_type_name(self) -> str:
        return type_name(self.field_type, short=True)

    @property
    def holder_class_name(self) -> str:
        return type_name(self.holder_class, short=True)

    def __str__(self) -> str:
        s = (
            f'Field "{self.field_name}" of type {self.field_type_name} '
            f"in {self.holder_class_name} has invalid value "
            f"{self.field_value!r}"
        )
        if self.key is not None:
            s += f" for input key {self.key!r}"
        if self.msg:
            s += f": {self.msg}"
        return s


class ConflictingFieldValues(InvalidFieldValue):
    """Several input keys for one field convert to different values.

    ``key_to_value`` maps each present input key to the value produced by
    applying the field's own deserialization rules to it.  Because the
    comparison happens after conversion, equivalent representations (for
    example ``"01"`` and ``1`` for an ``int`` field) are accepted instead of
    being reported as a conflict.
    """

    def __init__(
        self,
        field_name: str,
        field_type: Any,
        key_to_value: Mapping[str, Any],
        holder_class: type,
    ):
        self.key_to_value = dict(key_to_value)
        self.keys = tuple(self.key_to_value)
        keys_str = ", ".join(repr(k) for k in self.keys)
        values_str = ", ".join(
            f"{k!r}: {v!r}" for k, v in self.key_to_value.items()
        )
        msg = (
            f"input keys {keys_str} have conflicting values after "
            f"conversion ({values_str})"
        )
        super().__init__(
            field_name,
            field_type,
            self.key_to_value,
            holder_class,
            msg=msg,
        )


class AmbiguousFieldAliasError(ValueError):
    """One input key is accepted by more than one field of a model.

    Raised while the deserialization method is built, before any payload is
    read, so the model can never silently dispatch the key to one field.
    """

    def __init__(
        self,
        input_key: str,
        field_names: Sequence[str],
        holder_class: type,
    ):
        self.input_key = input_key
        self.key = input_key
        self.key_name = input_key
        self.field_names = tuple(field_names)
        self.holder_class = holder_class

    @property
    def holder_class_name(self) -> str:
        return type_name(self.holder_class, short=True)

    def __str__(self) -> str:
        names = ", ".join(f'"{name}"' for name in self.field_names)
        return (
            f'Input key "{self.input_key}" in {self.holder_class_name} '
            f"is ambiguous: it is accepted by fields {names}"
        )


class MissingDiscriminatorError(LookupError):
    def __init__(self, field_name: str):
        self.field_name = field_name

    def __str__(self) -> str:
        return f"Discriminator '{self.field_name}' is missing"


class SuitableVariantNotFoundError(ValueError):
    def __init__(
        self,
        variants_type: TypeForm,
        discriminator_name: str | None = None,
        discriminator_value: Any = None,
    ):
        self.variants_type = variants_type
        self.discriminator_name = discriminator_name
        self.discriminator_value = discriminator_value

    def __str__(self) -> str:
        s = f"{type_name(self.variants_type)} has no "
        if self.discriminator_value is not None:
            s += (
                f"subtype with attribute '{self.discriminator_name}' "
                f"equal to {self.discriminator_value!r}"
            )
        else:
            s += "suitable subtype"
        return s


class BadHookSignature(TypeError):
    pass


class ThirdPartyModuleNotFoundError(ModuleNotFoundError):
    def __init__(self, module_name: str, field_name: str, holder_class: type):
        self.module_name = module_name
        self.field_name = field_name
        self.holder_class = holder_class

    @property
    def holder_class_name(self) -> str:
        return type_name(self.holder_class, short=True)

    def __str__(self) -> str:
        s = (
            f'Install "{self.module_name}" to use it as the serialization '
            f'method for the field "{self.field_name}" '
            f"in {self.holder_class_name}"
        )
        return s


class UnresolvedTypeReferenceError(NameError):
    def __init__(self, holder_class: type, unresolved_type_name: str):
        self.holder_class = holder_class
        self.name = unresolved_type_name

    @property
    def holder_class_name(self) -> str:
        return type_name(self.holder_class, short=True)

    def __str__(self) -> str:
        return (
            f"Class {self.holder_class_name} has unresolved type reference "
            f"{self.name} in some of its fields"
        )


class BadDialect(ValueError):
    pass

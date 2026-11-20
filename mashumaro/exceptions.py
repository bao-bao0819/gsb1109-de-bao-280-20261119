from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from typing_extensions import TypeForm

from mashumaro.core.meta.helpers import type_name

_T = TypeVar("_T")


class ErrorPath(list):
    """A sequence of input keys/indexes leading to a failing value.

    Each element is the key or index exactly as it appears in the input.
    Unlike a dotted string, separate elements stay unambiguous when a key
    itself contains dots or brackets.
    """

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return list(self) == list(other)
        return super().__eq__(other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return repr(list(self))

    def __str__(self) -> str:
        return repr(list(self))


def prepend_error_path(segment: Any, exc: BaseException) -> ErrorPath:
    """Prepend an input segment to an exception's path and return it."""
    current = getattr(exc, "path", None)
    if not isinstance(current, (list, tuple)):
        current = ()
    path = ErrorPath([segment, *current])
    try:
        exc.path = path  # type: ignore[attr-defined]
    except (AttributeError, TypeError):  # pragma: no cover
        pass
    return path


def unpack_sequence_with_path(
    iterable: Iterable[Any],
    converter: Callable[[Any], _T],
) -> list[_T]:
    """Convert each item, reporting its positional index on failure."""
    result: list[_T] = []
    for index, item in enumerate(iterable):
        try:
            result.append(converter(item))
        except Exception as exc:
            prepend_error_path(index, exc)
            raise
    return result


def unpack_mapping_with_path(
    source: Any,
    key_converter: Callable[[Any], Any],
    value_converter: Callable[[Any], Any],
) -> dict[Any, Any]:
    """Convert keys and values, reporting the original key on failure."""
    result: dict[Any, Any] = {}
    for key, value in source.items():
        try:
            converted_key = key_converter(key)
        except Exception as exc:
            prepend_error_path(key, exc)
            raise
        try:
            converted_value = value_converter(value)
        except Exception as exc:
            prepend_error_path(key, exc)
            raise
        result[converted_key] = converted_value
    return result


class _ErrorPathMixin:
    path: ErrorPath

    def _set_path(self, path: Iterable[Any] | None) -> None:
        self.path = ErrorPath(path or ())

    @property
    def field_path(self) -> ErrorPath:
        return self.path

    @property
    def input_path(self) -> ErrorPath:
        return self.path

    @property
    def error_path(self) -> ErrorPath:
        return self.path

    def _path_suffix(self) -> str:
        if self.path:
            return f" (at path {self.path!r})"
        return ""


class MissingField(_ErrorPathMixin, LookupError):
    def __init__(
        self,
        field_name: str,
        field_type: Any,
        holder_class: type,
        path: Iterable[Any] | None = None,
    ):
        self.field_name = field_name
        self.field_type = field_type
        self.holder_class = holder_class
        self._set_path(path)

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
            f"{self._path_suffix()}"
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


class InvalidFieldValue(_ErrorPathMixin, ValueError):
    def __init__(
        self,
        field_name: str,
        field_type: Any,
        field_value: Any,
        holder_class: type,
        msg: str | None = None,
        path: Iterable[Any] | None = None,
    ):
        self.field_name = field_name
        self.field_type = field_type
        self.field_value = field_value
        self.holder_class = holder_class
        self.msg = msg
        self._set_path(path)

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
        if self.msg:
            s += f": {self.msg}"
        return s + self._path_suffix()


class MissingDiscriminatorError(_ErrorPathMixin, LookupError):
    def __init__(
        self,
        field_name: str,
        path: Iterable[Any] | None = None,
    ):
        self.field_name = field_name
        self._set_path(path)

    def __str__(self) -> str:
        return (
            f"Discriminator '{self.field_name}' is missing"
            f"{self._path_suffix()}"
        )


class SuitableVariantNotFoundError(_ErrorPathMixin, ValueError):
    def __init__(
        self,
        variants_type: TypeForm,
        discriminator_name: str | None = None,
        discriminator_value: Any = None,
        path: Iterable[Any] | None = None,
    ):
        self.variants_type = variants_type
        self.discriminator_name = discriminator_name
        self.discriminator_value = discriminator_value
        self._set_path(path)

    def __str__(self) -> str:
        s = f"{type_name(self.variants_type)} has no "
        if self.discriminator_value is not None:
            s += (
                f"subtype with attribute '{self.discriminator_name}' "
                f"equal to {self.discriminator_value!r}"
            )
        else:
            s += "suitable subtype"
        return s + self._path_suffix()


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

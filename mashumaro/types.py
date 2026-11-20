import decimal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from mashumaro.core.const import Sentinel

__all__ = [
    "Alias",
    "Discriminator",
    "GenericSerializableType",
    "RoundedDecimal",
    "SerializableType",
    "SerializationStrategy",
]


class SerializableType:
    __slots__ = ()

    __use_annotations__ = False

    def __init_subclass__(
        cls,
        use_annotations: bool | Literal[Sentinel.MISSING] = Sentinel.MISSING,
        **kwargs: Any,
    ):
        super().__init_subclass__(**kwargs)
        if use_annotations is not Sentinel.MISSING:
            cls.__use_annotations__ = use_annotations

    def _serialize(self) -> Any:
        raise NotImplementedError

    @classmethod
    def _deserialize(cls, value: Any) -> Any:
        raise NotImplementedError


class GenericSerializableType:
    __slots__ = ()

    def _serialize(self, types: list[type]) -> Any:
        raise NotImplementedError

    @classmethod
    def _deserialize(cls, value: Any, types: list[type]) -> Any:
        raise NotImplementedError


class SerializationStrategy:
    __use_annotations__ = False
    __match_subclasses__ = False

    def __init_subclass__(
        cls,
        use_annotations: bool | Literal[Sentinel.MISSING] = Sentinel.MISSING,
        match_subclasses: bool | Literal[Sentinel.MISSING] = Sentinel.MISSING,
        **kwargs: Any,
    ):
        super().__init_subclass__(**kwargs)
        if use_annotations is not Sentinel.MISSING:
            cls.__use_annotations__ = use_annotations
        if match_subclasses is not Sentinel.MISSING:
            cls.__match_subclasses__ = match_subclasses

    def serialize(self, value: Any) -> Any:
        raise NotImplementedError

    def deserialize(self, value: Any) -> Any:
        raise NotImplementedError


class RoundedDecimal(SerializationStrategy):
    def __init__(self, places: int | None = None, rounding: str | None = None):
        if places is not None:
            self.exp = decimal.Decimal((0, (1,), -places))
        else:
            self.exp = None  # type: ignore
        self.rounding = rounding

    def serialize(self, value: decimal.Decimal) -> str:
        if self.exp:
            if self.rounding:
                return str(value.quantize(self.exp, rounding=self.rounding))
            else:
                return str(value.quantize(self.exp))
        else:
            return str(value)

    def deserialize(self, value: str) -> decimal.Decimal:
        return decimal.Decimal(str(value))


@dataclass(unsafe_hash=True)
class Discriminator:
    field: str | None = None
    include_supertypes: bool = False
    include_subtypes: bool = False
    variant_tagger_fn: Callable[[Any], Any] | None = None
    # Per-variant tag mappings. A tag is the only evidence that a payload
    # belongs to a particular variant, so these mappings are keyed by the
    # variant class rather than inferred from the field shape.
    # Each variant accepts the tags it declares for decoding, while only
    # the first tag is written back on serialization.
    # A user supplied mapping is normalized into an immutable, hashable
    # tuple of (variant, tags) pairs so that Discriminator stays hashable
    # and two discriminators with different tags compare unequal.
    variant_tags: (
        Mapping[type | str, Sequence[Any]]
        | tuple[tuple[type | str, tuple[Any, ...]], ...]
        | None
    ) = None

    def __post_init__(self) -> None:
        if not self.include_supertypes and not self.include_subtypes:
            raise ValueError(
                "Either 'include_supertypes' or 'include_subtypes' "
                "must be enabled"
            )
        if self.variant_tags is not None:
            if not self.field:
                raise ValueError(
                    "'variant_tags' can only be used together with a "
                    "discriminator field"
                )
            if isinstance(self.variant_tags, Mapping):
                raw_items = self.variant_tags.items()
            else:
                # Accept an already normalized mapping, which happens when
                # a Discriminator is reconstructed from another one.
                raw_items = self.variant_tags
            normalized = []
            for variant, tags in raw_items:
                if not isinstance(variant, (type, str)):
                    raise ValueError(
                        "Keys of 'variant_tags' must be classes or "
                        "class names"
                    )
                if not isinstance(tags, (list, tuple)):
                    raise ValueError(
                        "Values of 'variant_tags' must be lists or tuples"
                    )
                if not tags:
                    raise ValueError(
                        "Each variant in 'variant_tags' must declare "
                        "at least one tag"
                    )
                normalized.append((variant, tuple(tags)))
            self.variant_tags = tuple(normalized)


class Alias:
    def __init__(self, name: str, /):
        self.name = name

    def __repr__(self) -> str:
        return f"Alias(name='{self.name}')"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Alias):
            return False
        return self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)

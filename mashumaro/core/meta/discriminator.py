from typing import Any

from mashumaro.exceptions import DuplicatedVariantTagError
from mashumaro.types import Discriminator

__all__ = [
    "build_variant_tags_map",
    "get_variant_primary_tag",
    "variant_has_declared_tags",
]


def variant_has_declared_tags(
    discriminator: Discriminator, variant: type
) -> bool:
    if discriminator.variant_tags is None:
        return False
    return _lookup_tags(discriminator, variant) is not None


def _lookup_tags(
    discriminator: Discriminator, variant: type
) -> tuple[Any, ...] | None:
    assert discriminator.variant_tags is not None
    for key, tags in discriminator.variant_tags:
        if key is variant or key == variant.__name__:
            return tags
    return None


def _iter_variant_tag_specs(
    discriminator: Discriminator, variants: Any
):
    """Yield (variant, tags) pairs explicitly declared for this flow."""
    if discriminator.variant_tags is None:
        for variant in variants:
            tags = list(_iter_fallback_tags(discriminator, variant))
            if tags:
                yield variant, tuple(tags)
        return
    available = list(variants)
    for key, tags in discriminator.variant_tags:
        if isinstance(key, str):
            for variant in available:
                if variant.__name__ == key:
                    yield variant, tags
                    break
        elif key in available:
            yield key, tags


def _iter_fallback_tags(
    discriminator: Discriminator, variant: type
):
    if discriminator.variant_tagger_fn is not None:
        tags = discriminator.variant_tagger_fn(variant)
        if isinstance(tags, list):
            yield from tags
        else:
            yield tags
    elif discriminator.field is not None:
        try:
            yield variant.__dict__[discriminator.field]
        except KeyError:
            return


def build_variant_tags_map(
    discriminator: Discriminator,
    variants: Any,
) -> dict[Any, type]:
    """Build a tag -> variant mapping for one independent discriminator.

    Tags, rather than the field shape, are the only evidence used to tell
    variants apart. When per-variant tags are explicitly declared, a tag
    claimed by two variants of the same data flow makes the whole
    configuration invalid before any variant is selected, regardless of
    whether a payload happens to carry the colliding tag. Discriminators
    that do not opt into the new rules keep the historical last-wins
    behaviour untouched.
    """
    strict = discriminator.variant_tags is not None
    variants_map: dict[Any, type] = {}
    collisions: dict[Any, list[type]] = {}
    for variant, tags in _iter_variant_tag_specs(discriminator, variants):
        for tag in tags:
            if strict:
                if tag in collisions:
                    if variant not in collisions[tag]:
                        collisions[tag].append(variant)
                elif tag in variants_map:
                    registered = variants_map[tag]
                    if registered is not variant:
                        collisions[tag] = [registered, variant]
                else:
                    variants_map[tag] = variant
            else:
                variants_map[tag] = variant
    if collisions:
        tag, claimants = next(iter(collisions.items()))
        raise DuplicatedVariantTagError(
            tag,
            tuple(claimants),
            discriminator.field,
        )
    return variants_map


def get_variant_primary_tag(
    discriminator: Discriminator,
    variant: type,
) -> Any:
    """Return the single tag that must be written back for a variant."""
    if discriminator.variant_tags is not None:
        tags = _lookup_tags(discriminator, variant)
        if tags is not None:
            return tags[0]
    for tag in _iter_fallback_tags(discriminator, variant):
        return tag
    raise KeyError(variant)

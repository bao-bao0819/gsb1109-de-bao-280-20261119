from mapbox_vector_tile.encoder import VectorTile
from mapbox_vector_tile.errors import (
    DuplicateFeatureIdError,
    FeatureNotFoundError,
    LayerNotFoundError,
    PropertyNotFoundError,
)
from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vector_tile

# Order mirrors TileData.parse_value and the encoder's value writing logic.
_VALUE_FIELDS = (
    "bool_value",
    "double_value",
    "float_value",
    "int_value",
    "sint_value",
    "string_value",
    "uint_value",
)


class _PlannedRectification:
    """A rectification whose targets have all been resolved against the original tile."""

    def __init__(self, layer, feature, set_properties, delete_properties):
        self.layer = layer
        self.feature = feature
        self.set_properties = set_properties
        self.delete_properties = delete_properties


def _value_field_name(value):
    if isinstance(value, bool):
        return "bool_value"
    if isinstance(value, str):
        return "string_value"
    if isinstance(value, int):
        return "int_value"
    return "double_value"


def _value_matches(value_message, field_name, value):
    for candidate in _VALUE_FIELDS:
        if value_message.HasField(candidate):
            return candidate == field_name and getattr(value_message, candidate) == value
    return False


def _find_feature_positions(layer, feature_id):
    # HasField distinguishes an explicitly written id 0 from a feature whose id was never written.
    # The latter keeps the protobuf default 0 but must not be matched.
    return [
        position
        for position, feature in enumerate(layer.features)
        if feature.HasField("id") and feature.id == feature_id
    ]


def _find_tag_pair(feature, layer, key):
    for pair in range(0, len(feature.tags), 2):
        key_index = feature.tags[pair]
        if layer.keys[key_index] == key:
            return pair
    return None


def _validate_rectification(rectification, layers_by_name):
    layer_name = rectification["layer"]
    feature_id = rectification["id"]
    if not isinstance(feature_id, int) or isinstance(feature_id, bool) or feature_id < 0:
        raise ValueError(f"The feature id must be a non-negative integer, {feature_id!r} provided.")

    layer = layers_by_name.get(layer_name)
    if layer is None:
        raise LayerNotFoundError(layer_name)

    positions = _find_feature_positions(layer, feature_id)
    if not positions:
        raise FeatureNotFoundError(layer_name, feature_id)
    if len(positions) > 1:
        raise DuplicateFeatureIdError(layer_name, feature_id, positions)

    set_properties = rectification.get("set_properties", {})
    if not isinstance(set_properties, dict):
        raise ValueError("'set_properties' must be a dictionary of property names to replacement values.")
    for key, value in set_properties.items():
        if not VectorTile._can_handle_attr(key, value):
            raise ValueError(f"Unsupported property {key!r} with value {value!r}.")

    delete_properties = rectification.get("delete_properties", [])
    if not isinstance(delete_properties, list):
        raise ValueError("'delete_properties' must be a list of property names to delete.")
    if len(set(delete_properties)) != len(delete_properties):
        raise ValueError(f"Duplicate property names in 'delete_properties' for feature {feature_id}.")

    overlapping = set(delete_properties).intersection(set_properties)
    if overlapping:
        names = ", ".join(sorted(overlapping))
        raise ValueError(f"Properties cannot be both replaced and deleted in one rectification: {names}.")

    feature = layer.features[positions[0]]
    for key in delete_properties:
        if not VectorTile._can_handle_key(key):
            raise ValueError(f"Property names must be strings, {key!r} provided.")
        if _find_tag_pair(feature, layer, key) is None:
            raise PropertyNotFoundError(layer_name, feature_id, key)

    return _PlannedRectification(layer, feature, dict(set_properties), list(delete_properties))


def _ensure_key_index(layer, key):
    for index, existing_key in enumerate(layer.keys):
        if existing_key == key:
            return index
    layer.keys.append(key)
    return len(layer.keys) - 1


def _ensure_value_index(layer, value):
    field_name = _value_field_name(value)
    for index, existing_value in enumerate(layer.values):
        if _value_matches(existing_value, field_name, value):
            return index

    new_value = layer.values.add()
    setattr(new_value, field_name, value)
    return len(layer.values) - 1


def _apply_rectification(plan):
    layer = plan.layer
    feature = plan.feature

    for key in plan.delete_properties:
        pair = _find_tag_pair(feature, layer, key)
        del feature.tags[pair : pair + 2]

    for key, value in plan.set_properties.items():
        key_index = _ensure_key_index(layer, key)
        value_index = _ensure_value_index(layer, value)
        pair = _find_tag_pair(feature, layer, key)
        if pair is None:
            feature.tags.append(key_index)
            feature.tags.append(value_index)
        else:
            feature.tags[pair + 1] = value_index


def rectify_tile(tile_bytes, rectifications):
    """Apply attribute-only rectifications to an already published MVT tile.

    Only feature properties are touched. Geometries, feature ordering, layer ordering and the string
    dictionaries of every layer are preserved: dictionary entries are only ever appended to, never removed
    or reordered, so values still referenced by other features keep their index and representation.

    A rectification is a dictionary with:
        * ``layer``: name of the layer containing the feature,
        * ``id``: the explicitly written feature id (a feature without an id is never matched, even though
          its protobuf default id is 0),
        * ``set_properties``: optional dictionary of properties to replace or add, several entries being
          allowed in a single rectification,
        * ``delete_properties``: optional list of property names to remove, several entries being allowed.

    The whole batch is validated before anything is changed. If an id matches several features, a
    ``DuplicateFeatureIdError`` is raised naming the layer and the original zero-based feature positions,
    and no rectification is applied.

    Args:
        tile_bytes: the published MVT tile as bytes.
        rectifications: a list of rectification dictionaries, possibly spanning several layers.

    Returns:
        The rectified tile serialized as bytes.
    """
    tile = vector_tile.tile()
    tile.ParseFromString(tile_bytes)
    layers_by_name = {layer.name: layer for layer in tile.layers}

    # Validation pass: resolve every target against the original tile, mutate nothing.
    plans = []
    seen_targets = set()
    for rectification in rectifications:
        plan = _validate_rectification(rectification, layers_by_name)
        target = (plan.layer.name, plan.feature.id)
        if target in seen_targets:
            raise ValueError(
                f"Feature {plan.feature.id} in layer {plan.layer.name!r} is targeted by more than one "
                "rectification; merge its property changes into a single rectification."
            )
        seen_targets.add(target)
        plans.append(plan)

    # Application pass: the batch is applied only once every rectification is known to be valid.
    for plan in plans:
        _apply_rectification(plan)

    return tile.SerializeToString()

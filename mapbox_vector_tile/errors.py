class RectificationError(Exception):
    """Base class for the errors raised while applying MVT attribute rectifications."""


class DuplicateFeatureIdError(RectificationError):
    """Raised when a rectification targets an id which identifies more than one feature in a layer.

    The original zero-based positions of every matching feature are reported so that the conflicting
    references can be disambiguated. The tile is left untouched.
    """

    def __init__(self, layer_name, feature_id, positions):
        self.layer_name = layer_name
        self.feature_id = feature_id
        self.positions = tuple(positions)
        positions_text = ", ".join(str(position) for position in self.positions)
        super().__init__(
            f"Duplicate feature id {feature_id} in layer {layer_name!r}; "
            f"matching features at original positions: {positions_text}."
        )


class FeatureNotFoundError(RectificationError):
    """Raised when no feature with the targeted id exists in the layer. The tile is left untouched."""

    def __init__(self, layer_name, feature_id):
        self.layer_name = layer_name
        self.feature_id = feature_id
        super().__init__(f"No feature with id {feature_id} in layer {layer_name!r}.")


class LayerNotFoundError(RectificationError):
    """Raised when a rectification references a layer which is not present in the tile."""

    def __init__(self, layer_name):
        self.layer_name = layer_name
        super().__init__(f"Layer {layer_name!r} does not exist in the tile.")


class PropertyNotFoundError(RectificationError):
    """Raised when a property to delete is absent from the targeted feature."""

    def __init__(self, layer_name, feature_id, key):
        self.layer_name = layer_name
        self.feature_id = feature_id
        self.key = key
        super().__init__(f"Feature {feature_id} in layer {layer_name!r} has no property {key!r}.")

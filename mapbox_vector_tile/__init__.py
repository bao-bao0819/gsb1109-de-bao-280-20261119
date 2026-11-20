import contextlib
import warnings

from mapbox_vector_tile import decoder, encoder
from mapbox_vector_tile.feature_id import (
    FeatureIdConflictError,
    FeatureIdError,
    FeatureIdMapping,
    FeatureIdRangeError,
    FeatureIdRangeExhaustedError,
    FeatureIdValueError,
)
from mapbox_vector_tile.utils import get_encode_options

__all__ = [
    "decode",
    "encode",
    "FeatureIdConflictError",
    "FeatureIdError",
    "FeatureIdMapping",
    "FeatureIdRangeError",
    "FeatureIdRangeExhaustedError",
    "FeatureIdValueError",
]


def decode(tile, per_layer_options=None, default_options=None, **kwargs):
    """Decode the provided `tile`

    Args:
        tile:
            The tile to decode.

        per_layer_options:
            An optional dictionary containing per layer options. The keys are the layer names and the values are
            options as described further. If an option is missing for a layer or if a layer is missing, the values of
            `default_options` are taken first. Finally, the global default options are used.

        default_options:
            These options are taken for layers without entry in `per_layer_options`. For all missing options values,
            the global default values are taken.

    Returns:
        The decoded layers data.

    Notes:
        The possible options are:
            * `y_coord_down`: it suppresses flipping the y coordinate values during encoding when set to `True`.
            Default to `False`.
            * `transformer`: a function transforming the coordinates of geometry object. It takes two floats (`x`
            and `y`) as arguments and retrieves the transformed coordinates `x_transformed`, `y_transformed`. Default to
            `None`.
            * `geojson`: when set to `False`, the behaviour of mapbox-vector-tile version 1.* is used. When set
            to `False`, the retrieved dictionary is a valid geojson file. Default to `True`.
    """
    if kwargs:
        warnings.warn("`decode` signature has changed, use `default_options` instead", DeprecationWarning, stacklevel=2)
        default_options = {**kwargs, **(default_options or {})}
    vector_tile = decoder.TileData(pbf_data=tile, per_layer_options=per_layer_options, default_options=default_options)
    message = vector_tile.get_message()
    return message


def encode(layers, per_layer_options=None, default_options=None, **kwargs):
    """Encode the `layers` into a MVT tile.

    Args:
        layers:
            The layer data to encode.

        per_layer_options:
            An optional dictionary containing per layer options. The keys are the layer names and the values are
            options as described further. If an option is missing for a layer or if a layer is missing, the values of
            `default_options` are taken first. Finally, the global default options are used.

        default_options:
            These options are taken for layers without entry in `per_layer_options`. For all missing options values,
            the global default values are taken.

    Returns:
        The encoded tile.

    Notes:
        The possible options are:
            * `y_coord_down`: it suppresses flipping the y coordinate values during encoding when set to `True`.
            Default to `False`.
            * `transformer`: a function transforming the coordinates of geometry object. It takes two floats (`x`
            and `y`) as arguments and retrieves the transformed coordinates `x_transformed`, `y_transformed`. Default to
            `None`.
            * `quantize_bounds`: bounds in the form (minx, miny, maxx, maxy) used to scale coordinates during
            encoding. Default to `None`.
            * `extents`: extents of the tile which is passed through to the layer in the pbf, and honored during any
            quantization or y coordinate flipping. Default to 4096.
            * `on_invalid_geometry`: a function taking a shapely shape as argument and retrieving an optional
            valid geometry. Default to None. In the file `encoder.py`, three possible functions are defined:
                * `on_invalid_geometry_raise`: it raises an error if an invalid geometry exists.
                * `on_invalid_geometry_ignore`: it ignores the invalid geometry and replaces it with a `None`.
                * `on_invalid_geometry_make_valid`: it tries to make the geometry valid. If it fails, retrieves `None`.
            * `check_winding_order`: it forces the check of the winding order for polygons. Default to True.
            * `max_geometry_validate_tries`: the number of tries when trying to enforce the good winding order. Default
            to 5.
            * `feature_id_mapping`: an optional
            :class:`mapbox_vector_tile.feature_id.FeatureIdMapping` that assigns stable numeric feature IDs
            to source keys across batches. When set, each feature may carry a `source` key (a string or a
            native integer ID); the mapping assigns or reuses its numeric ID. Features without a `source`
            keep the existing `id` behavior. Assignment is atomic per tile: on conflict or range exhaustion
            the mapping is left unchanged. Default to `None`.
    """
    if kwargs:
        warnings.warn("`encode` signature has changed, use `default_options` instead", DeprecationWarning, stacklevel=2)
        default_options = {**kwargs, **(default_options or {})}
    vector_tile = encoder.VectorTile(default_options=default_options)

    if per_layer_options is None:
        per_layer_options = {}

    layer_specs = layers if isinstance(layers, list) else [layers]

    # Assign feature IDs for every layer as one transaction. A conflict or an
    # exhausted range in a later layer must not leave earlier layer mappings
    # partially updated.
    mappings = []
    for layer in layer_specs:
        options = get_encode_options(
            layer_options=per_layer_options.get(layer["name"]), default_options=default_options
        )
        mapping = options["feature_id_mapping"]
        if mapping is not None and all(mapping is not existing for existing in mappings):
            mappings.append(mapping)

    with contextlib.ExitStack() as stack:
        for mapping in mappings:
            stack.enter_context(mapping.transaction())
        for layer in layer_specs:
            layer_name = layer["name"]
            layer_options = per_layer_options.get(layer_name, None)
            vector_tile.add_layer(features=layer["features"], name=layer_name, options=layer_options)

    return vector_tile.tile.SerializeToString()

from collections import namedtuple

from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vector_tile

# The seven value representations defined by the MVT specification. A valid value message sets exactly one of
# these fields. Keeping the field name alongside the decoded Python value is what distinguishes, for example, an
# int_value 9 from a uint_value 9: both read back as the Python integer 9, but their on-the-wire representation
# must not be collapsed while merging.
VALUE_FIELDS = (
    "string_value",
    "float_value",
    "double_value",
    "int_value",
    "uint_value",
    "sint_value",
    "bool_value",
)

# A same-named layer that cannot be merged. `versions` and `extents` collect the values found across the inputs and
# `reason` explains why the merge was refused.
Conflict = namedtuple("Conflict", ["name", "reason", "versions", "extents"])

# Result bookkeeping of a merge:
#   merged_layers:     names of layers that were built by combining same-named input layers
#   preserved_layers:  (tile index, layer name) pairs copied into the output without any modification
#   conflicts:         Conflict instances for the rejected same-named layers
MergeReport = namedtuple("MergeReport", ["merged_layers", "preserved_layers", "conflicts"])


def _value_signature(value):
    """Return the (field name, python value) pair identifying a dictionary value, or None if it cannot be kept.

    A value is only reusable when it sets exactly one of the specification fields. Anything else (empty value,
    several fields set) cannot be preserved with its original representation, so the owning layer is refused.
    """
    set_fields = [field for field in VALUE_FIELDS if value.HasField(field)]
    if len(set_fields) != 1:
        return None
    field = set_fields[0]
    return field, getattr(value, field)


def _layer_conflict(name, group):
    """Check whether a group of same-named layers can be merged, returning a Conflict or None."""
    versions = sorted({layer.version for _, layer in group})
    extents = sorted({layer.extent for _, layer in group})
    if len(versions) > 1 or len(extents) > 1:
        return Conflict(
            name=name,
            reason=(
                f"Layer {name!r} cannot be merged: same-named layers must share the same version and extent, "
                f"got versions {tuple(versions)} and extents {tuple(extents)}."
            ),
            versions=tuple(versions),
            extents=tuple(extents),
        )

    for tile_index, layer in group:
        referenced_values = set()
        for feature in layer.features:
            if len(feature.tags) % 2 != 0:
                return Conflict(
                    name=name,
                    reason=(
                        f"Layer {name!r} cannot be merged: a feature in tile {tile_index} has an odd number of "
                        "tags, which is not a valid key/value pairing."
                    ),
                    versions=tuple(versions),
                    extents=tuple(extents),
                )
            tags = list(feature.tags)
            for tag_pos, bound in ((0, len(layer.keys)), (1, len(layer.values))):
                for index in tags[tag_pos::2]:
                    if index >= bound:
                        return Conflict(
                            name=name,
                            reason=(
                                f"Layer {name!r} cannot be merged: a feature in tile {tile_index} references "
                                f"dictionary index {index} which is out of range."
                            ),
                            versions=tuple(versions),
                            extents=tuple(extents),
                        )
            referenced_values.update(tags[1::2])

        # Only referenced values must keep their representation; unreferenced ones are dropped during the merge.
        for value_index in sorted(referenced_values):
            if _value_signature(layer.values[value_index]) is None:
                return Conflict(
                    name=name,
                    reason=(
                        f"Layer {name!r} cannot be merged: tile {tile_index} holds a referenced dictionary value "
                        "that does not set exactly one specification field, so its representation cannot be "
                        "preserved."
                    ),
                    versions=tuple(versions),
                    extents=tuple(extents),
                )
    return None


def _merge_layers(group):
    """Build a new layer from same-named, compatible input layers.

    Feature order, feature ids, geometry commands and value representations are copied verbatim. Only the key/value
    dictionaries are rebuilt, remapping feature tags accordingly. Dictionary values that no feature references are
    dropped; identical values that are actually used share a single dictionary entry.
    """
    holder = vector_tile.tile()
    merged = holder.layers.add()
    first_layer = group[0][1]
    merged.name = first_layer.name
    merged.version = first_layer.version
    merged.extent = first_layer.extent

    # Keys are plain strings. Equal keys from different tiles share one entry.
    shared_key_index = {}
    key_maps = []
    for _, layer in group:
        key_map = {}
        for old_index, key in enumerate(layer.keys):
            if key not in shared_key_index:
                merged.keys.append(key)
                shared_key_index[key] = len(merged.keys) - 1
            key_map[old_index] = shared_key_index[key]
        key_maps.append(key_map)

    # Values are keyed by their (representation field, value) signature so that int_value 9 and uint_value 9 stay
    # separate while identical and used values are shared.
    shared_value_index = {}
    value_maps = []
    for _, layer in group:
        referenced_values = set()
        for feature in layer.features:
            referenced_values.update(feature.tags[1::2])

        value_map = {}
        for old_index, value in enumerate(layer.values):
            if old_index not in referenced_values:
                # Unreferenced dictionary values are dead weight and are not carried over.
                continue
            signature = _value_signature(value)
            if signature not in shared_value_index:
                merged.values.add().CopyFrom(value)
                shared_value_index[signature] = len(merged.values) - 1
            value_map[old_index] = shared_value_index[signature]
        value_maps.append(value_map)

    # Features are appended tile by tile, in the provided order, keeping each feature's own position inside its
    # source layer. Geometry command integers and the feature id are copied without any decoding or quantization.
    for (_, layer), key_map, value_map in zip(group, key_maps, value_maps):
        for feature in layer.features:
            new_feature = merged.features.add()
            if feature.HasField("id"):
                new_feature.id = feature.id
            new_feature.type = feature.type
            tags = list(feature.tags)
            for tag_pos in range(0, len(tags), 2):
                new_feature.tags.append(key_map[tags[tag_pos]])
                new_feature.tags.append(value_map[tags[tag_pos + 1]])
            new_feature.geometry.extend(feature.geometry)

    return merged


def merge_tiles(tiles):
    """Merge encoded MVT tiles in the provided order without decoding geometry.

    Args:
        tiles: ordered iterable of serialized MVT tiles. Same-named layers are only combined when their version
            and extent match. Conflicting same-named layers are rejected with a reason in the report: the first
            occurrence is preserved verbatim and the later ones are omitted. Layers with a unique name and the
            first occurrence of conflicted names are copied byte-for-byte, keeping their original version, extent
            and content.

    Returns:
        A (serialized tile, MergeReport) tuple. The merge is all-or-nothing per layer: a layer is either fully
        merged or left untouched, never partially appended.
    """
    parsed_tiles = []
    for tile_bytes in tiles:
        tile = vector_tile.tile()
        tile.ParseFromString(tile_bytes)
        parsed_tiles.append(tile)

    occurrences = {}
    ordered_names = []
    for tile_index, tile in enumerate(parsed_tiles):
        for layer in tile.layers:
            if layer.name not in occurrences:
                occurrences[layer.name] = []
                ordered_names.append(layer.name)
            occurrences[layer.name].append((tile_index, layer))

    output_tile = vector_tile.tile()
    merged_layers = []
    preserved_layers = []
    conflicts = []

    for name in ordered_names:
        group = occurrences[name]
        first_tile_index, first_layer = group[0]

        if len(group) == 1:
            output_tile.layers.add().CopyFrom(first_layer)
            preserved_layers.append((first_tile_index, name))
            continue

        conflict = _layer_conflict(name, group)
        if conflict is not None:
            # Refuse the merge entirely and keep the first occurrence unchanged so that no input layer is ever
            # appended to halfway and treated as a successful merge.
            output_tile.layers.add().CopyFrom(first_layer)
            preserved_layers.append((first_tile_index, name))
            conflicts.append(conflict)
            continue

        output_tile.layers.add().CopyFrom(_merge_layers(group))
        merged_layers.append(name)

    return output_tile.SerializeToString(), MergeReport(
        merged_layers=merged_layers,
        preserved_layers=preserved_layers,
        conflicts=conflicts,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Merge encoded MVT tiles without decoding their geometry.")
    parser.add_argument(
        "input_files", nargs="+", help="Input MVT files, merged in the given order", type=argparse.FileType("rb")
    )
    parser.add_argument(
        "--output-file",
        required=True,
        help="Path of the merged MVT file to write",
        type=argparse.FileType("wb"),
    )
    args = parser.parse_args()

    input_tiles = [handle.read() for handle in args.input_files]
    merged_bytes, report = merge_tiles(input_tiles)
    args.output_file.write(merged_bytes)

    for layer_name in report.merged_layers:
        print(f"merged layer: {layer_name}")
    for tile_index, layer_name in report.preserved_layers:
        print(f"preserved layer: {layer_name} (from tile {tile_index}, unchanged)")
    for conflict in report.conflicts:
        print(f"refused layer: {conflict.name} - {conflict.reason}")

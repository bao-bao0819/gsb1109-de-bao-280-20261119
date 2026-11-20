"""Offline repro: one tile, two downstream read paths, one narrow lake.

Run from the repo root with the repo's own library:

    .venv/bin/python handoff_lake_hole/repro_lake_hole.py

It encodes one small tile (outer ring + one narrow inner ring = the lake),
once with a quantization fine enough to keep the inner ring (tile A) and
once with an intentionally coarse encode-time quantization (control tile B).
It then reads tile A four ways (toggle Y flip only, toggle a coarse-grid
snapping transformer only, both, neither) and does the same for tile B.

Evidence is taken at two independent levels for every read:
  1. the raw geometry command stream inside the .pbf (ring count, raw
     coordinates, signed area of each ring BEFORE any decode-side flip or
     transform), and
  2. the public mapbox_vector_tile.decode output (per-ring hierarchy of the
     resulting GeoJSON Polygon).
"""

import os

import mapbox_vector_tile
from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vt
from mapbox_vector_tile.utils import CMD_LINE_TO, CMD_MOVE_TO, CMD_SEG_END, zig_zag_decode

HERE = os.path.dirname(os.path.abspath(__file__))

EXTENT = 4096
LAKE_WIDTH = 4  # tile units; the inner ring is a 4 x 1000 slit

WKT = (
    "POLYGON ((512 512, 512 3584, 3584 3584, 3584 512, 512 512), "
    "(2000 1500, 2000 2500, 2004 2500, 2004 1500, 2000 1500))"
)
LAYER = {"name": "water", "features": [{"geometry": WKT, "properties": {"uid": 1}}]}

CMD_NAMES = {CMD_MOVE_TO: "MoveTo", CMD_LINE_TO: "LineTo", CMD_SEG_END: "ClosePath"}


def snap_coarse(x, y):
    return (round(x / 512) * 512, round(y / 512) * 512)


def snap_fine(x, y):
    return (round(x / 2) * 2, round(y / 2) * 2)


def signed_area2(ring):
    return sum(ring[k][0] * ring[k + 1][1] - ring[k + 1][0] * ring[k][1] for k in range(len(ring) - 1))


def raw_tile_rings(pbf):
    """Replay the raw command stream of feature 0: commands, rings, signed areas.

    Coordinates here are tile-local integers exactly as stored: no Y flip, no
    transformer. Rings are closed before the shoelace sum.
    """
    tile = vt.tile()
    tile.ParseFromString(pbf)
    layer = tile.layers[0]
    geom = list(layer.features[0].geometry)
    commands = []
    rings = []
    cur = None
    x = y = 0
    i = 0
    while i < len(geom):
        cmd = geom[i] & 0x7
        count = geom[i] >> 3
        i += 1
        commands.append(f"{CMD_NAMES[cmd]}x{count}")
        if cmd in (CMD_MOVE_TO, CMD_LINE_TO):
            if cmd == CMD_MOVE_TO and cur:
                rings.append(cur)
                cur = None
            if cur is None:
                cur = []
            for _ in range(count):
                x += zig_zag_decode(geom[i])
                y += zig_zag_decode(geom[i + 1])
                i += 2
                cur.append((x, y))
        elif cmd == CMD_SEG_END:
            rings.append(cur)
            cur = None
    closed = [r + [r[0]] for r in rings]
    return layer.extent, commands, closed


def show_raw(tag, pbf):
    extent, commands, rings = raw_tile_rings(pbf)
    print(f"  [{tag}] extent={extent} commands: {' '.join(commands)}")
    print(f"  [{tag}] rings in command stream: {len(rings)}")
    for n, ring in enumerate(rings):
        print(f"  [{tag}]   ring{n} area2={signed_area2(ring):+d} coords={ring}")


def show_decode(tag, pbf, opts):
    decoded = mapbox_vector_tile.decode(pbf, default_options=opts)
    geom = decoded["water"]["features"][0]["geometry"]
    coords = geom["coordinates"]
    n_rings = len(coords) if geom["type"] == "Polygon" else sum(len(p) for p in coords)
    hole = "lake KEPT (ring1 = interior)" if geom["type"] == "Polygon" and len(coords) == 2 else "NO lake"
    print(f"  [{tag}] {geom['type']} with {n_rings} ring(s) -> {hole}")
    if geom["type"] == "Polygon":
        for n, ring in enumerate(coords):
            role = "exterior" if n == 0 else "interior"
            print(f"  [{tag}]   ring{n} ({role}): {ring}")
    return geom


def geojson_area(geom):
    return abs(signed_area2(geom["coordinates"][0])) / 2


def main():
    cell_fine = (EXTENT - 0) / EXTENT
    cell_coarse = (EXTENT - 0) / 512
    print("== 0. quantization choice ==")
    print(f"  lake slit width = {LAKE_WIDTH} tile units in a [0,{EXTENT}) span")
    print(f"  tile A: extents=4096 -> grid cell {cell_fine:g} <= width {LAKE_WIDTH} -> inner ring survives encoding")
    print(f"  tile B: extents=512  -> grid cell {cell_coarse:g} >  width {LAKE_WIDTH} -> inner ring collapses at encoding")

    tile_a = mapbox_vector_tile.encode([LAYER], default_options={"quantize_bounds": (0, 0, 4096, 4096), "extents": 4096})
    tile_b = mapbox_vector_tile.encode([LAYER], default_options={"quantize_bounds": (0, 0, 4096, 4096), "extents": 512})
    for name, blob in (("tile_a_fine.pbf", tile_a), ("tile_b_coarse_encode.pbf", tile_b)):
        with open(os.path.join(HERE, name), "wb") as fh:
            fh.write(blob)

    print("\n== 1. raw command streams (ground truth, before any decode option) ==")
    show_raw("A raw", tile_a)
    show_raw("B raw", tile_b)

    reads = [
        ("read default          ", {}),
        ("read Y-flip only      ", {"y_coord_down": True}),
        ("read snap512 only     ", {"transformer": snap_coarse}),
        ("read Y-flip + snap512 ", {"y_coord_down": True, "transformer": snap_coarse}),
    ]

    print("\n== 2. tile A (fine encode) through the four read paths ==")
    geoms_a = {}
    for tag, opts in reads:
        geoms_a[tag] = show_decode("A " + tag.strip(), tile_a, opts)

    print("\n== 3. control tile B (coarse at encode) through the same four read paths ==")
    for tag, opts in reads:
        show_decode("B " + tag.strip(), tile_b, opts)

    print("\n== 4. a snapping transformer is not inherently destructive ==")
    show_decode("A snap2 (grid 2 <= width 4)", tile_a, {"transformer": snap_fine})

    print("\n== 5. why final GeoJSON area cannot locate the lossy stage ==")
    a_snap = mapbox_vector_tile.decode(tile_a, default_options={"transformer": snap_coarse})["water"]["features"][0]["geometry"]
    b_plain = mapbox_vector_tile.decode(tile_b)["water"]["features"][0]["geometry"]
    print(f"  A+snap512 final: 1 ring, exterior area {geojson_area(a_snap):.0f} (tile units^2)")
    print(f"  B default final: 1 ring, exterior area {geojson_area(b_plain):.0f} (its own 512-extent units)")
    print("  both are lake-less single-ring polygons; the GeoJSON alone cannot say WHERE the lake went")
    print("  the raw streams in section 1 can: A still stores a non-zero-area inner ring, B does not")

    print("\n== 6. assertions ==")
    _, _, rings_a = raw_tile_rings(tile_a)
    _, _, rings_b = raw_tile_rings(tile_b)
    assert len(rings_a) == 2 and signed_area2(rings_a[1]) != 0, "tile A must store a real inner ring"
    assert signed_area2(rings_b[1]) == 0, "tile B inner ring must already be zero-area in the pbf"
    assert len(geoms_a["read Y-flip only      "]["coordinates"]) == 2, "Y flip alone must keep the lake"
    assert len(geoms_a["read snap512 only     "]["coordinates"]) == 1, "snap512 must drop the lake at decode"
    assert len(geoms_a["read Y-flip + snap512 "]["coordinates"]) == 1
    print("  all assertions passed")


if __name__ == "__main__":
    main()

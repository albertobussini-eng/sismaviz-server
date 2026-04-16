"""
tiles.py - Genera Vector Tiles MVT da GeoDataFrame
"""
import math
import mercantile
from shapely.geometry import box, mapping
from shapely.ops import transform


def _tile_bbox(z, x, y):
    bounds = mercantile.bounds(x, y, z)
    return box(bounds.west, bounds.south, bounds.east, bounds.north)


def encode_mvt(gdf, z, x, y, layer_name="buildings"):
    try:
        import mapbox_vector_tile
    except ImportError:
        raise ImportError("pip install mapbox-vector-tile")

    tile_geom = _tile_bbox(z, x, y)
    bounds = mercantile.bounds(x, y, z)

    subset = gdf[gdf.geometry.intersects(tile_geom)].copy()
    if subset.empty:
        return None

    subset["geometry"] = subset.geometry.intersection(tile_geom)
    subset = subset[~subset.geometry.is_empty]
    if subset.empty:
        return None

    def geo_to_tile(geom):
        w, s, e, n = bounds.west, bounds.south, bounds.east, bounds.north
        sx, sy = e - w, n - s
        def _t(xs, ys, zs=None):
            tx = [(x - w) / sx * 4096 for x in xs]
            ty = [(n - y) / sy * 4096 for y in ys]
            return (tx, ty, zs) if zs is not None else (tx, ty)
        return transform(_t, geom)

    features = []
    for _, row in subset.iterrows():
        try:
            tg = geo_to_tile(row.geometry)
            gm = mapping(tg)
            props = {}
            for col in subset.columns:
                if col == "geometry":
                    continue
                val = row[col]
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    continue
                if isinstance(val, (int, float, str, bool)):
                    props[col] = val
                else:
                    props[col] = str(val)
            features.append({"geometry": gm, "properties": props})
        except Exception:
            continue

    if not features:
        return None

    return mapbox_vector_tile.encode(
        {layer_name: {"features": features}},
        extents=4096,
        on_wkb_error="ignore"
    )

"""
loader.py - Scarica edifici OSM via Overpass e li salva in cache locale
"""
import os, time, math, json
import requests
import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon
import mercantile

CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

_current_gdf = None
_current_name = None


def _tile_cache_path(z, x, y):
    return os.path.join(CACHE_DIR, f"{z}_{x}_{y}.gpkg")


def _overpass_query(south, west, north, east, timeout=25):
    q = f"""[out:json][timeout:{timeout}];
(way["building"]({south},{west},{north},{east}););
out geom;"""
    try:
        r = requests.post(OVERPASS_URL, data={"data": q}, timeout=timeout + 5)
        r.raise_for_status()
        return r.json().get("elements", [])
    except Exception as e:
        print(f"Overpass error: {e}")
        return []


def _parse_elements(elements):
    rows = []
    seen = set()
    for el in elements:
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        oid = el["id"]
        if oid in seen:
            continue
        seen.add(oid)
        coords = [(n["lon"], n["lat"]) for n in el["geometry"]]
        if len(coords) < 3:
            continue
        try:
            geom = Polygon(coords)
            if not geom.is_valid:
                geom = geom.buffer(0)
        except Exception:
            continue
        tags = el.get("tags", {})
        lv = int(tags.get("building:levels") or tags.get("levels") or 2)
        try:
            ht = float(tags.get("height") or (lv * 3.2))
        except Exception:
            ht = lv * 3.2
        try:
            area = geom.area * (111320 ** 2) * math.cos(math.radians(geom.centroid.y))
        except Exception:
            area = 200
        rows.append({
            "osm_id": oid,
            "height": round(min(max(ht, 3.2), 300), 1),
            "levels": max(1, lv),
            "use": tags.get("building", "yes"),
            "area_m2": round(max(20, area), 1),
            "T1": round(0.1 * max(1, lv), 2),
            "source": "osm",
            "geometry": geom,
        })
    if not rows:
        return gpd.GeoDataFrame(
            columns=["osm_id","height","levels","use","area_m2","T1","source","geometry"],
            geometry="geometry", crs="EPSG:4326")
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def load_tile(z, x, y):
    cache_path = _tile_cache_path(z, x, y)
    if os.path.exists(cache_path + ".empty"):
        return None
    if os.path.exists(cache_path):
        try:
            return gpd.read_file(cache_path)
        except Exception:
            pass
    bounds = mercantile.bounds(x, y, z)
    elements = _overpass_query(bounds.south, bounds.west, bounds.north, bounds.east)
    gdf = _parse_elements(elements)
    try:
        if len(gdf) > 0:
            gdf.to_file(cache_path, driver="GPKG")
        else:
            open(cache_path + ".empty", "w").close()
    except Exception as e:
        print(f"Cache write error: {e}")
    return gdf


def load_area(west, south, east, north, name="area", progress_cb=None):
    tiles = list(mercantile.tiles(west, south, east, north, zooms=14))
    total = len(tiles)
    all_gdfs = []
    seen_ids = set()
    print(f"Area {name!r}: {total} tiles")
    for i, tile in enumerate(tiles):
        z, x, y = tile.z, tile.x, tile.y
        if os.path.exists(_tile_cache_path(z, x, y) + ".empty"):
            if progress_cb:
                progress_cb(i + 1, total, sum(len(g) for g in all_gdfs))
            continue
        gdf = load_tile(z, x, y)
        if gdf is not None and len(gdf) > 0:
            new_rows = gdf[~gdf["osm_id"].isin(seen_ids)]
            seen_ids.update(new_rows["osm_id"].tolist())
            all_gdfs.append(new_rows)
        if progress_cb:
            progress_cb(i + 1, total, sum(len(g) for g in all_gdfs))
        if not os.path.exists(_tile_cache_path(z, x, y)):
            time.sleep(0.15)
    if not all_gdfs:
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs="EPSG:4326"))
    result = gpd.GeoDataFrame(
        pd.concat(all_gdfs, ignore_index=True),
        geometry="geometry", crs="EPSG:4326")
    print(f"  {name!r}: {len(result)} buildings")
    return result


def load_gpkg(path, height_col=None, levels_col=None, use_col=None):
    gdf = gpd.read_file(path)
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    cols = {c.lower(): c for c in gdf.columns}
    def find(candidates):
        for c in candidates:
            if c in cols:
                return cols[c]
        return None
    h = height_col or find(["height","altezza","h","quota","hgt"])
    l = levels_col or find(["building:levels","levels","piani","floors","n_piani"])
    u = use_col   or find(["building","uso","use","destinazione","tipologia"])
    def sf(s, d=6.4):
        try: return pd.to_numeric(s, errors="coerce").fillna(d)
        except: return pd.Series([d]*len(s))
    def si(s, d=2):
        try: return pd.to_numeric(s, errors="coerce").fillna(d).astype(int)
        except: return pd.Series([d]*len(s))
    result = gpd.GeoDataFrame(geometry=gdf.geometry, crs="EPSG:4326")
    result["osm_id"] = [f"ext_{i}" for i in range(len(gdf))]
    result["source"] = "external"
    if h:
        result["height"] = sf(gdf[h], 6.4).clip(3.2, 300)
    elif l:
        lvls = si(gdf[l], 2).clip(1, 80)
        result["height"] = (lvls * 3.2).clip(3.2, 300)
        result["levels"] = lvls
    else:
        result["height"] = 6.4
        result["levels"] = 2
    if l and "levels" not in result.columns:
        result["levels"] = si(gdf[l], 2).clip(1, 80)
    elif "levels" not in result.columns:
        result["levels"] = (result["height"] / 3.2).astype(int).clip(1, 80)
    result["use"] = gdf[u].astype(str) if u else "yes"
    areas = []
    for geom in result.geometry:
        try:
            area = geom.area * (111320**2) * math.cos(math.radians(geom.centroid.y))
        except:
            area = 200
        areas.append(round(max(20, area), 1))
    result["area_m2"] = areas
    result["T1"] = (result["levels"] * 0.1).round(2)
    print(f"Loaded {len(result)} buildings from {os.path.basename(path)}")
    return result


def get_current(): return _current_gdf
def get_current_name(): return _current_name
def set_current(gdf, name):
    global _current_gdf, _current_name
    _current_gdf = gdf
    _current_name = name

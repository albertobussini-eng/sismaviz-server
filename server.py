"""
server.py - SismaViz Tile Server (FastAPI)
"""
import os, json, math, time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Query
from fastapi.responses import Response, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

import geopandas as gpd
import requests as req_lib
import pandas as pd

import loader
import tiles as tile_engine

app = FastAPI(title="SismaViz", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIR = Path(__file__).parent / "frontend"
_load_status = {"running": False, "done": 0, "total": 0, "count": 0, "name": "", "error": None}


@app.get("/", response_class=HTMLResponse)
async def root():
    idx = FRONTEND_DIR / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>SismaViz running</h1>")


@app.get("/api/status")
async def status():
    gdf = loader.get_current()
    return {"ok": True, "buildings": len(gdf) if gdf is not None else 0,
            "name": loader.get_current_name() or "", "load": _load_status}


@app.post("/api/load")
async def load_area(body: dict, background_tasks: BackgroundTasks):
    name = body.get("name", "area")
    west = body.get("west"); south = body.get("south")
    east = body.get("east"); north = body.get("north")

    if None in (west, south, east, north):
        try:
            r = req_lib.get("https://nominatim.openstreetmap.org/search",
                params={"q": name + ", Italy", "format": "json", "limit": 3},
                timeout=10, headers={"User-Agent": "SismaViz/1.0"})
            d = r.json()
            if not d:
                return JSONResponse({"error": f"'{name}' non trovato"}, status_code=404)
            hit = next((x for x in d if x["osm_type"] == "relation"), d[0])
            bb = hit["boundingbox"]
            south, north = float(bb[0]), float(bb[1])
            west, east   = float(bb[2]), float(bb[3])
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    _load_status.update({"running": True, "done": 0, "total": 0, "count": 0, "name": name, "error": None})

    def progress(done, total, count):
        _load_status.update({"done": done, "total": total, "count": count})

    def do_load():
        try:
            gdf = loader.load_area(west, south, east, north, name=name, progress_cb=progress)
            loader.set_current(gdf, name)
            _load_status.update({"running": False, "count": len(gdf)})
        except Exception as e:
            _load_status.update({"running": False, "error": str(e)})

    background_tasks.add_task(do_load)
    return {"ok": True, "message": f"Caricamento '{name}' avviato"}


@app.post("/api/upload")
async def upload_gpkg(file: UploadFile = File(...),
                      height_col: Optional[str] = Query(None),
                      levels_col: Optional[str] = Query(None),
                      use_col:    Optional[str] = Query(None)):
    if not file.filename.endswith(".gpkg"):
        return JSONResponse({"error": "Solo .gpkg supportati"}, status_code=400)
    tmp = f"cache/_upload_{int(time.time())}.gpkg"
    with open(tmp, "wb") as f:
        f.write(await file.read())
    try:
        gdf = loader.load_gpkg(tmp, height_col, levels_col, use_col)
        current = loader.get_current()
        if current is not None and len(current) > 0:
            merged = gpd.GeoDataFrame(
                pd.concat([current, gdf], ignore_index=True),
                geometry="geometry", crs="EPSG:4326")
            loader.set_current(merged, (loader.get_current_name() or "") + f" + {file.filename}")
        else:
            loader.set_current(gdf, file.filename)
        return {"ok": True, "buildings": len(gdf), "total": len(loader.get_current())}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try: os.remove(tmp)
        except: pass


@app.get("/tiles/{z}/{x}/{y}.pbf")
async def get_tile(z: int, x: int, y: int):
    gdf = loader.get_current()
    if gdf is None or len(gdf) == 0:
        return Response(content=b"", media_type="application/x-protobuf", status_code=204)
    try:
        pbf = tile_engine.encode_mvt(gdf, z, x, y, layer_name="buildings")
    except Exception as e:
        print(f"Tile {z}/{x}/{y} error: {e}")
        return Response(content=b"", media_type="application/x-protobuf", status_code=204)
    if pbf is None:
        return Response(content=b"", media_type="application/x-protobuf", status_code=204)
    return Response(content=pbf, media_type="application/x-protobuf",
                    headers={"Access-Control-Allow-Origin": "*"})


@app.get("/api/buildings/geojson")
async def buildings_geojson(limit: int = 50000):
    gdf = loader.get_current()
    if gdf is None or len(gdf) == 0:
        return JSONResponse({"type": "FeatureCollection", "features": []})
    return JSONResponse(json.loads(gdf.head(limit).to_json()))


@app.get("/api/search")
async def search(q: str):
    try:
        r = req_lib.get("https://nominatim.openstreetmap.org/search",
            params={"q": q + ", Italy", "format": "json", "limit": 5, "addressdetails": 1},
            timeout=10, headers={"User-Agent": "SismaViz/1.0"})
        return JSONResponse(r.json())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/tiles/tilejson.json")
async def tilejson():
    port = os.environ.get("PORT", "10000")
    host = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{port}")
    return {"tilejson": "3.0.0", "name": "SismaViz Buildings",
            "tiles": [f"{host}/tiles/{{z}}/{{x}}/{{y}}.pbf"],
            "minzoom": 0, "maxzoom": 18,
            "vector_layers": [{"id": "buildings", "fields": {
                "osm_id": "Number", "height": "Number", "levels": "Number",
                "use": "String", "area_m2": "Number", "T1": "Number", "source": "String"
            }}]}

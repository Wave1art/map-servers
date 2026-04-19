#!/usr/bin/env python3
"""
build_tiles.py
==============
Config-driven tile builder. Reads layers.yml and renders each layer to
a static XYZ tile directory under docs/tiles/<layer-id>/{z}/{x}/{y}.png.

Supports two source types:
  geojson — fetches a GeoJSON URL, renders filled polygons
  wmts    — downloads existing raster tiles from a public XYZ/WMTS endpoint

Usage:
    # Build all layers defined in layers.yml
    python build_tiles.py

    # Build a single layer by ID
    python build_tiles.py --layer slf-snow-depth

    # Override zoom range for this run
    python build_tiles.py --layer slope-30 --max-zoom 12

    # Use a local GeoJSON file instead of fetching (geojson layers only)
    python build_tiles.py --layer slf-snow-depth --local path/to/file.geojson

    # Also write an .mbtiles file alongside the XYZ tiles (for mobile import)
    python build_tiles.py --layer slf-snow-depth --also-mbtiles

Dependencies:
    pip install -r requirements.txt
"""

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

try:
    import mercantile
    import requests
    import yaml
    from PIL import Image, ImageDraw
    from requests.adapters import HTTPAdapter
    from shapely.geometry import box, shape
    from urllib3.util.retry import Retry
except ImportError as e:
    print(f"\nMissing dependency: {e}")
    print("Run: uv sync\n")
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
TILE_SIZE  = 256
_R         = 6378137.0   # Web Mercator Earth radius (metres)
WMTS_DELAY = 0.05        # seconds between WMTS tile requests (be polite)

# Looks like a real browser — some public servers (including SLF) block
# Python/requests default UA or known CI IP ranges at the application layer.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def make_session() -> requests.Session:
    """Return a requests Session with retry logic and a browser User-Agent."""
    retry = Retry(
        total=4,
        backoff_factor=2,          # waits 2, 4, 8, 16 s between attempts
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    s.headers.update({"User-Agent": _USER_AGENT})
    return s

CONFIG_FILE = Path(__file__).parent / "layers.yml"
DOCS_DIR    = Path(__file__).parent / "docs"
TILES_DIR   = DOCS_DIR / "tiles"


# ─────────────────────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: Path = CONFIG_FILE) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    defaults = cfg.get("defaults", {})
    layers   = []
    for layer in cfg.get("layers", []):
        merged = {
            "bounds": defaults.get("bounds", [5.96, 45.82, 10.49, 47.81]),
            "zoom":   dict(defaults.get("zoom", {"min": 5, "max": 10})),
        }
        merged.update(layer)
        # Allow per-layer zoom to be partially specified
        if "zoom" in layer:
            merged["zoom"] = {**merged["zoom"], **layer["zoom"]}
        layers.append(merged)
    return {"defaults": defaults, "layers": layers}


def get_layer(config: dict, layer_id: str) -> dict:
    for layer in config["layers"]:
        if layer["id"] == layer_id:
            return layer
    raise ValueError(
        f"Layer '{layer_id}' not found in layers.yml. "
        f"Available: {[l['id'] for l in config['layers']]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate helpers
# ─────────────────────────────────────────────────────────────────────────────

def _merc_x(lon: float) -> float:
    return lon * math.pi * _R / 180.0

def _merc_y(lat: float) -> float:
    return math.log(math.tan(math.pi / 4.0 + lat * math.pi / 360.0)) * _R

def _to_pixels(coords, left, top, right, bottom, size=TILE_SIZE):
    w, h = right - left, top - bottom
    return [
        ((_merc_x(lon) - left) / w * size,
         (top - _merc_y(lat)) / h * size)
        for lon, lat in coords
    ]

def _hex_to_rgba(hex_color: str, alpha: int) -> tuple:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


# ─────────────────────────────────────────────────────────────────────────────
# GeoJSON polygon renderer
# ─────────────────────────────────────────────────────────────────────────────

def _draw_polygon(draw, poly, fill, tile_merc):
    if poly.is_empty or poly.area == 0:
        return
    left, top, right, bottom = tile_merc
    pts = _to_pixels(list(poly.exterior.coords), left, top, right, bottom)
    if len(pts) < 3:
        return
    draw.polygon(pts, fill=fill)
    for interior in poly.interiors:
        hole = _to_pixels(list(interior.coords), left, top, right, bottom)
        if len(hole) >= 3:
            draw.polygon(hole, fill=(0, 0, 0, 0))

def _draw_geom(draw, geom, fill, tile_merc):
    t = geom.geom_type
    if t == "Polygon":
        _draw_polygon(draw, geom, fill, tile_merc)
    elif t == "MultiPolygon":
        for part in geom.geoms:
            _draw_polygon(draw, part, fill, tile_merc)
    elif t == "GeometryCollection":
        for part in geom.geoms:
            _draw_geom(draw, part, fill, tile_merc)

def render_geojson_tile(features: list, z: int, x: int, y: int) -> bytes | None:
    wgs   = mercantile.bounds(x, y, z)
    tbox  = box(wgs.west, wgs.south, wgs.east, wgs.north)
    xybds = mercantile.xy_bounds(mercantile.Tile(x, y, z))
    tm    = (xybds.left, xybds.top, xybds.right, xybds.bottom)

    img  = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    hit  = False

    for feat in features:
        if not feat["geom"].intersects(tbox):
            continue
        try:
            clipped = feat["geom"].intersection(tbox)
        except Exception:
            continue
        if clipped.is_empty:
            continue
        _draw_geom(draw, clipped, feat["fill_rgba"], tm)
        hit = True

    if not hit:
        return None
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def prepare_geojson_features(geojson_data: dict, style: dict) -> list:
    """Convert raw GeoJSON features into render-ready dicts."""
    fill_prop    = style.get("fill_property", "fill")
    partial_prop = style.get("partial_property", "partialSnowCover")
    alpha_full   = int(style.get("opacity_full",    0.82) * 255)
    alpha_part   = int(style.get("opacity_partial", 0.43) * 255)

    features = []
    for feat in geojson_data.get("features", []):
        try:
            geom = shape(feat["geometry"])
            if not geom.is_valid:
                geom = geom.buffer(0)
        except Exception as e:
            print(f"    ⚠  Skipping invalid geometry: {e}")
            continue

        props   = feat.get("properties", {})
        alpha   = alpha_part if props.get(partial_prop) else alpha_full
        fill_hex = props.get(fill_prop, "#888888")
        features.append({
            "geom":      geom,
            "fill_rgba": _hex_to_rgba(fill_hex, alpha),
            "z_index":   props.get("zIndex", 0),
            "partial":   bool(props.get(partial_prop)),
        })

    # Draw full-cover below partial (z_index, then partial on top)
    features.sort(key=lambda f: (f["z_index"], f["partial"]))
    return features


# ─────────────────────────────────────────────────────────────────────────────
# Output writers
# ─────────────────────────────────────────────────────────────────────────────

def write_xyz_tile(out_dir: Path, z: int, x: int, y: int, png: bytes) -> None:
    p = out_dir / str(z) / str(x)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{y}.png").write_bytes(png)


def write_mbtiles(out_path: Path, tiles: list[tuple], meta: dict) -> None:
    """Write an MBTiles file from a list of (z, x, y, png_bytes) tuples."""
    out_path.unlink(missing_ok=True)
    conn = sqlite3.connect(out_path)
    cur  = conn.cursor()
    cur.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    cur.execute(
        "CREATE TABLE tiles "
        "(zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"
    )
    cur.execute(
        "CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)"
    )
    cur.executemany("INSERT INTO metadata VALUES (?,?)", list(meta.items()))
    for z, x, y, png in tiles:
        tms_y = (2 ** z - 1) - y
        cur.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", (z, x, tms_y, png))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Layer builders
# ─────────────────────────────────────────────────────────────────────────────

def build_geojson_layer(
    layer: dict,
    out_dir: Path,
    *,
    local_file: str | None = None,
    also_mbtiles: bool = False,
    zoom_min: int | None = None,
    zoom_max: int | None = None,
    session: requests.Session | None = None,
) -> dict:
    """Fetch GeoJSON and render to XYZ tiles."""
    url = layer["url"]
    if local_file:
        print(f"  Loading local file: {local_file}")
        import json as _json
        data = _json.loads(Path(local_file).read_text())
    else:
        s = session or make_session()
        print(f"  Fetching {url}")
        r = s.get(url, timeout=(10, 60))   # (connect timeout, read timeout)
        r.raise_for_status()
        data = r.json()

    valid_at = data.get("validAt", datetime.now(timezone.utc).isoformat())
    style    = layer.get("style", {})
    features = prepare_geojson_features(data, style)
    print(f"  {len(features)} features  |  valid at {valid_at}")

    z_min = zoom_min if zoom_min is not None else layer["zoom"]["min"]
    z_max = zoom_max if zoom_max is not None else layer["zoom"]["max"]
    bounds = layer["bounds"]

    mbtiles_tiles = []
    total_rendered = total_empty = 0

    for z in range(z_min, z_max + 1):
        candidates = list(mercantile.tiles(*bounds, zooms=z))
        rendered   = 0
        for tile in candidates:
            png = render_geojson_tile(features, tile.z, tile.x, tile.y)
            if png:
                write_xyz_tile(out_dir, tile.z, tile.x, tile.y, png)
                if also_mbtiles:
                    mbtiles_tiles.append((tile.z, tile.x, tile.y, png))
                rendered += 1
        empty = len(candidates) - rendered
        total_rendered += rendered
        total_empty    += empty
        print(f"    z{z:>2}  {len(candidates):>5} candidates  "
              f"{rendered:>4} rendered  {empty:>4} empty")

    if also_mbtiles:
        mb_path = out_dir.parent / f"{layer['id']}.mbtiles"
        write_mbtiles(mb_path, mbtiles_tiles, {
            "name":        layer["name"],
            "type":        "overlay",
            "version":     "1.0",
            "description": layer.get("description", ""),
            "format":      "png",
            "bounds":      ",".join(str(v) for v in bounds),
            "minzoom":     str(z_min),
            "maxzoom":     str(z_max),
            "attribution": layer.get("attribution", ""),
        })
        print(f"    .mbtiles → {mb_path}")

    return {"validAt": valid_at, "rendered": total_rendered}


def build_wmts_layer(
    layer: dict,
    out_dir: Path,
    *,
    zoom_min: int | None = None,
    zoom_max: int | None = None,
    session: requests.Session | None = None,
) -> dict:
    """Download tiles from a public XYZ/WMTS endpoint."""
    s      = session or requests.Session()
    url_t  = layer["url"]   # must contain {z}, {x}, {y}
    bounds = layer["bounds"]
    z_min  = zoom_min if zoom_min is not None else layer["zoom"]["min"]
    z_max  = zoom_max if zoom_max is not None else layer["zoom"]["max"]

    total_rendered = total_empty = total_errors = 0

    for z in range(z_min, z_max + 1):
        candidates = list(mercantile.tiles(*bounds, zooms=z))
        rendered   = 0
        for tile in candidates:
            url = url_t.format(z=tile.z, x=tile.x, y=tile.y)
            try:
                r = s.get(url, timeout=15)
                if r.status_code == 200 and r.content:
                    write_xyz_tile(out_dir, tile.z, tile.x, tile.y, r.content)
                    rendered += 1
                else:
                    total_empty += 1
                time.sleep(WMTS_DELAY)
            except requests.RequestException as e:
                total_errors += 1
                if total_errors <= 3:
                    print(f"    ⚠  {url}: {e}")
        empty = len(candidates) - rendered
        total_rendered += rendered
        total_empty    += empty
        print(f"    z{z:>2}  {len(candidates):>5} candidates  "
              f"{rendered:>4} downloaded  {empty:>4} empty/skipped")

    return {"validAt": None, "rendered": total_rendered}


# ─────────────────────────────────────────────────────────────────────────────
# Manifest writer
# ─────────────────────────────────────────────────────────────────────────────

def update_manifest(layer: dict, result: dict, base_url_hint: str = "") -> None:
    """
    Keep docs/layers.json up to date. The preview index.html reads this
    to know which layers are available and what their tile URLs are.
    """
    manifest_path = DOCS_DIR / "layers.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {"layers": []}

    # Remove old entry for this layer id
    manifest["layers"] = [l for l in manifest["layers"] if l["id"] != layer["id"]]

    manifest["layers"].append({
        "id":          layer["id"],
        "name":        layer["name"],
        "description": layer.get("description", ""),
        "tileUrl":     f"tiles/{layer['id']}/{{z}}/{{x}}/{{y}}.png",
        "attribution": layer.get("attribution", ""),
        "legend":      layer.get("legend", []),
        "zoom":        layer["zoom"],
        "validAt":     result.get("validAt"),
        "builtAt":     datetime.now(timezone.utc).isoformat(),
    })

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Config-driven tile builder")
    ap.add_argument("--layer",        metavar="ID",   help="Build a specific layer (default: all)")
    ap.add_argument("--local",        metavar="FILE", help="Local GeoJSON file (geojson layers only)")
    ap.add_argument("--min-zoom",     type=int,       help="Override min zoom")
    ap.add_argument("--max-zoom",     type=int,       help="Override max zoom")
    ap.add_argument("--also-mbtiles", action="store_true",
                    help="Also write an .mbtiles file for mobile import")
    ap.add_argument("--config",       default=str(CONFIG_FILE),
                    help=f"Config file path (default: {CONFIG_FILE})")
    args = ap.parse_args()

    config = load_config(Path(args.config))
    layers = ([get_layer(config, args.layer)] if args.layer
              else config["layers"])

    session  = make_session()
    failed   = []
    skipped  = []

    for layer in layers:
        layer_id = layer["id"]
        out_dir  = TILES_DIR / layer_id
        out_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{'─' * 60}")
        print(f"  {layer['name']}  [{layer_id}]  type={layer['type']}")
        print(f"{'─' * 60}")

        try:
            if layer["type"] == "geojson":
                result = build_geojson_layer(
                    layer, out_dir,
                    local_file=args.local,
                    also_mbtiles=args.also_mbtiles,
                    zoom_min=args.min_zoom,
                    zoom_max=args.max_zoom,
                    session=session,
                )
            elif layer["type"] == "wmts":
                result = build_wmts_layer(
                    layer, out_dir,
                    zoom_min=args.min_zoom,
                    zoom_max=args.max_zoom,
                    session=session,
                )
            else:
                print(f"  ✗ Unknown type '{layer['type']}' — skipping")
                skipped.append(layer_id)
                continue

            update_manifest(layer, result)
            size_kb = sum(f.stat().st_size for f in out_dir.rglob("*.png")) // 1024
            print(f"\n  ✓ {result['rendered']} tiles  |  {size_kb} KB  →  {out_dir}")

        except requests.exceptions.ConnectTimeout:
            print(f"\n  ✗ Timed out fetching {layer.get('url', '?')}")
            print(     "    The source server may be blocking CI IP ranges.")
            print(     "    Tiles from the last successful run are unchanged.")
            failed.append(layer_id)

        except requests.exceptions.RequestException as e:
            print(f"\n  ✗ Network error: {e}")
            print(     "    Tiles from the last successful run are unchanged.")
            failed.append(layer_id)

        except Exception as e:
            print(f"\n  ✗ Unexpected error: {e}")
            failed.append(layer_id)
            raise   # unexpected errors still bubble up for visibility

    print(f"\n{'═' * 60}")
    if failed:
        print(f"  ⚠  {len(failed)} layer(s) failed: {', '.join(failed)}")
        print(     "     Existing tiles for those layers were not changed.")
    if skipped:
        print(f"  –  {len(skipped)} layer(s) skipped: {', '.join(skipped)}")
    success = len(layers) - len(failed) - len(skipped)
    print(f"  ✓  {success} layer(s) built successfully.")
    print(f"{'═' * 60}\n")

    # Exit non-zero only if ALL layers failed — partial success is still a
    # successful run (existing tiles for failed layers remain valid).
    if failed and success == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

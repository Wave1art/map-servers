#!/usr/bin/env python3
"""
road_gradient.py
================
Fetch MTB-relevant roads from the Overpass API, sample elevation from the
Copernicus GLO-30 DEM (cloud-optimised GeoTIFFs on AWS S3), and return
render-ready gradient segments for tile rendering.

Called by build_tiles.py for layers of type 'road_gradient'.
No QGIS dependency — pure Python: requests, pyproj, rasterio, shapely.

Gradient is calculated as the absolute rise/run over each road segment:
    gradient_pct = |Δ elevation (m)| / segment_length (m) × 100

Segments are formed by splitting each OSM way at ~250 m intervals using
geodesic distance (pyproj.Geod), staying entirely in WGS84 throughout.
"""

import math
import os
from typing import Optional

# ── Tell GDAL to use an in-memory HTTP cache for COG block reads.
# Set before any rasterio / GDAL call.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_CACHEMAX", "512")            # 512 MB block cache
os.environ.setdefault("CPL_VSIL_CURL_CACHE_SIZE", "134217728")  # 128 MB HTTP cache

from pyproj import Geod
from shapely.geometry import LineString

import requests

# ── Geodesic calculator (WGS84 ellipsoid) ─────────────────────────────────────
GEOD = Geod(ellps="WGS84")

# ── Overpass endpoints (tried in order; first 200 response wins) ──────────────
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OVERPASS_URL = OVERPASS_URLS[0]   # kept for backward-compat

# ── Highway types relevant for mountain biking ────────────────────────────────
DEFAULT_HIGHWAY_TYPES: list[str] = [
    "path",          # singletrack and multi-use paths
    "track",         # forestry / farm tracks
    "bridleway",     # shared with horses — often rideable
    "cycleway",      # dedicated cycle infrastructure
    "unclassified",  # minor public roads
    "tertiary",      # small through-roads
    "residential",   # estate roads
    "living_street", # shared space streets
    "service",       # access roads, driveways
]

# ── Gradient colour bands ─────────────────────────────────────────────────────
# Listed in ascending order of steepness.  The last entry (max=None) catches
# everything above the second-to-last band.
DEFAULT_BANDS: list[dict] = [
    {"max":  3,   "color": "#91cf60"},  # flat / easy
    {"max":  6,   "color": "#fee08b"},  # gentle
    {"max": 10,   "color": "#fdae61"},  # moderate
    {"max": 15,   "color": "#f46d43"},  # steep
    {"max": 20,   "color": "#d73027"},  # very steep
    {"max": None, "color": "#a50026"},  # extreme  (≥ 20 %)
]

# ── AWS S3 base URL for Copernicus GLO-30 COGs ────────────────────────────────
_DEM_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


# ─────────────────────────────────────────────────────────────────────────────
# Overpass fetch
# ─────────────────────────────────────────────────────────────────────────────

def fetch_roads(
    bounds: tuple[float, float, float, float],
    highway_types: list[str],
    session: requests.Session,
    polygon: list[list[float]] | None = None,
    dry_run: bool = False,
) -> dict:
    """
    POST an Overpass query for MTB-relevant ways and return parsed JSON.

    bounds  = (west, south, east, north) in WGS84 degrees — always required
              for the tile-generation bbox even when a polygon is also given.
    polygon = optional list of [lon, lat] pairs.  When supplied the Overpass
              query uses a ``poly:`` filter (more precise than a bbox).
    """
    west, south, east, north = bounds

    if polygon:
        # Overpass poly: format is space-separated "lat lon" pairs
        poly_str = " ".join(f"{lat} {lon}" for lon, lat in polygon)
        area_filter = f'(poly:"{poly_str}")'
    else:
        area_filter = f"({south},{west},{north},{east})"

    # Build as union of individual highway= tags to avoid regex metacharacters
    # that can trigger WAF/mod_security rules on Overpass mirrors.
    way_filters = "\n".join(
        f'  way[highway={hw}][access!=private][access!=no]'
        f'[bicycle!=private][bicycle!=no][bicycle!=dismount]'
        f"     {area_filter};"
        for hw in highway_types
    )
    query = (
        f"[out:json][timeout:300][maxsize:536870912];\n"
        f"(\n"
        f"{way_filters}\n"
        f");\n"
        f"out body;\n"
        f">;\n"
        f"out skel qt;\n"
    )
    if dry_run:
        print("── Overpass query (paste into https://overpass-turbo.eu) ──")
        print(query)
        print("──────────────────────────────────────────────────────────")
        raise SystemExit(0)

    # Overpass expects an honest, descriptive UA. The Chrome-impersonation UA used
    # elsewhere in this project trips mod_security on Overpass mirrors because the
    # matching Sec-Ch-Ua / Sec-Fetch headers are absent, which gets flagged as a
    # spoofed browser and returns 406. Override per-request.
    overpass_headers = {
        "User-Agent": "mapping-layers/1.0 (road-gradient tile builder; +https://github.com/)",
    }

    last_exc: Exception | None = None
    for url in OVERPASS_URLS:
        for method in ("POST", "GET"):
            try:
                if method == "POST":
                    resp = session.post(url, data={"data": query}, headers=overpass_headers, timeout=(10, 360))
                else:
                    resp = session.get(url, params={"data": query}, headers=overpass_headers, timeout=(10, 360))
                if resp.status_code == 406:
                    if method == "POST":
                        print(f"  ↷ {url} POST→406, retrying as GET…")
                        continue  # try GET
                    body = resp.text[:300] if resp.text else "<empty>"
                    print(f"  ↷ {url} GET→406, User-Agent={resp.request.headers.get('User-Agent','?')!r}, body={body!r}")
                    last_exc = requests.HTTPError(response=resp)
                    break
                if resp.status_code == 429:
                    print(f"  ↷ {url} rate-limited (429), trying next…")
                    last_exc = requests.HTTPError(response=resp)
                    break
                resp.raise_for_status()
                data = resp.json()
                if remark := data.get("remark"):
                    print(f"  ⚠  Overpass: {remark}")
                return data
            except requests.exceptions.RequestException as exc:
                print(f"  ↷ {url} {method} failed ({exc}), trying next…")
                last_exc = exc
                break  # don't try GET if POST raised a connection error
    raise last_exc  # all mirrors exhausted


# ─────────────────────────────────────────────────────────────────────────────
# OSM parser
# ─────────────────────────────────────────────────────────────────────────────

def parse_osm(data: dict) -> list[dict]:
    """
    Extract node coordinates and ways from Overpass JSON output.

    Returns a list of way dicts::
        {"coords": [(lon, lat), ...], "highway": str, "name": str, "surface": str}
    """
    nodes: dict[int, tuple[float, float]] = {}
    for el in data.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])

    ways: list[dict] = []
    for el in data.get("elements", []):
        if el["type"] != "way":
            continue
        refs = el.get("nodes", [])
        coords = [nodes[n] for n in refs if n in nodes]
        if len(coords) < 2:
            continue
        tags = el.get("tags", {})
        ways.append({
            "coords":  coords,
            "highway": tags.get("highway", ""),
            "name":    tags.get("name", ""),
            "surface": tags.get("surface", ""),
        })
    return ways


# ─────────────────────────────────────────────────────────────────────────────
# Geodesic line splitting
# ─────────────────────────────────────────────────────────────────────────────

def _split_way(
    coords: list[tuple[float, float]],
    max_len_m: float = 250.0,
) -> list[tuple[list[tuple[float, float]], float]]:
    """
    Split a list of (lon, lat) coordinates into sub-segments each at most
    max_len_m metres long (geodesic, WGS84 ellipsoid).

    Splits happen at existing OSM nodes — no interpolation.  Segments begin a
    new run whenever adding the next edge would exceed the limit.

    Returns list of (segment_coords, length_m) tuples.
    """
    if len(coords) < 2:
        return []

    segments: list[tuple[list, float]] = []
    current: list[tuple[float, float]] = [coords[0]]
    current_len = 0.0

    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        _, _, edge_len = GEOD.inv(lon1, lat1, lon2, lat2)

        if current_len + edge_len <= max_len_m + 1e-6:
            current.append((lon2, lat2))
            current_len += edge_len
        else:
            # Finalise the current segment at node i, start a new one
            if len(current) >= 2:
                segments.append((current, current_len))
            current = [(lon1, lat1), (lon2, lat2)]
            current_len = float(edge_len)

    if len(current) >= 2:
        segments.append((current, current_len))

    return segments


# ─────────────────────────────────────────────────────────────────────────────
# Copernicus GLO-30 DEM sampler
# ─────────────────────────────────────────────────────────────────────────────

class DEMSampler:
    """
    Lazy-loads Copernicus GLO-30 COG tiles from AWS S3 via rasterio.

    Each DEM file covers exactly 1° × 1°.  Datasets are opened once and kept
    in a dict keyed by tile identifier (e.g. "N46_E007") so repeated lookups
    within the same degree-square incur only one remote open.  GDAL's block
    cache (configured above via env vars) keeps recently read 512×512 blocks
    in memory, so repeated lookups in the same ~15 km² area are very fast.

    Usage::
        sampler = DEMSampler()
        elev = sampler.get_elevation(8.54, 47.37)   # Zürich → ~408 m
        sampler.close()
    """

    def __init__(self) -> None:
        try:
            import rasterio  # noqa: F401 — just check it's installed
        except ImportError:
            raise ImportError(
                "rasterio is required for road_gradient layers. "
                "Run: uv add rasterio"
            )
        self._cache: dict[str, object] = {}   # key → DatasetReader | None

    # ── URL helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _tile_key(lon: float, lat: float) -> str:
        # AWS key segment, e.g. "N47_00_E008" — note the _00 between lat and lon,
        # which matches the current Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM layout.
        lat_i   = int(math.floor(lat))
        lon_i   = int(math.floor(lon))
        lat_pfx = "N" if lat_i >= 0 else "S"
        lon_pfx = "E" if lon_i >= 0 else "W"
        return f"{lat_pfx}{abs(lat_i):02d}_00_{lon_pfx}{abs(lon_i):03d}"

    @staticmethod
    def _tile_url(key: str) -> str:
        name = f"Copernicus_DSM_COG_10_{key}_00_DEM"
        return f"{_DEM_BASE}/{name}/{name}.tif"

    # ── Dataset open (cached) ─────────────────────────────────────────────────

    def _open(self, key: str):
        if key in self._cache:
            return self._cache[key]
        import rasterio
        url = self._tile_url(key)
        try:
            ds = rasterio.open(url)
            self._cache[key] = ds
            print(f"    ✓ DEM tile opened: {key}")
        except Exception as exc:
            print(f"    ✗ DEM tile {key} failed to open:\n"
                  f"      URL: {url}\n"
                  f"      Error: {type(exc).__name__}: {exc}")
            self._cache[key] = None   # mark unavailable so we don't retry
        return self._cache[key]

    # ── Public API ────────────────────────────────────────────────────────────

    def get_elevation(self, lon: float, lat: float) -> Optional[float]:
        """Return elevation in metres for the given WGS84 lon/lat, or None."""
        key = self._tile_key(lon, lat)
        ds  = self._open(key)
        if ds is None:
            return None
        try:
            import rasterio.windows
            row, col = ds.index(lon, lat)
            window   = rasterio.windows.Window(col, row, 1, 1)
            data     = ds.read(1, window=window)
            val      = float(data[0, 0])
            if ds.nodata is not None and abs(val - ds.nodata) < 1:
                return None
            return val
        except Exception:
            return None

    def close(self) -> None:
        for ds in self._cache.values():
            if ds is not None:
                try:
                    ds.close()
                except Exception:
                    pass
        self._cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Gradient colouring
# ─────────────────────────────────────────────────────────────────────────────

def gradient_color(pct: float, bands: list[dict]) -> str:
    """Return the hex colour for a gradient percentage using the given bands."""
    for band in bands:
        if band["max"] is None or pct < band["max"]:
            return band["color"]
    return bands[-1]["color"]


def _hex_to_rgba(hex_color: str, alpha: int = 210) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline entry point
# ─────────────────────────────────────────────────────────────────────────────

def compute_gradients(
    ways: list[dict],
    sampler: DEMSampler,
    max_len_m: float = 250.0,
    bands: list[dict] | None = None,
    line_alpha: int = 210,
) -> list[dict]:
    """
    For each way, split into ≤ max_len_m segments, sample DEM elevation at
    the endpoints, compute the gradient, and assign a colour.

    Returns a list of render-ready dicts::
        {
            "geom":         shapely.geometry.LineString  (WGS84),
            "gradient_pct": float,
            "color_hex":    str,
            "color_rgba":   (r, g, b, a),
            "highway":      str,
            "name":         str,
        }
    """
    if bands is None:
        bands = DEFAULT_BANDS

    features: list[dict] = []
    missing_elev = 0

    for way in ways:
        for seg_coords, seg_len_m in _split_way(way["coords"], max_len_m):
            if seg_len_m < 5.0:
                # Too short to derive a meaningful gradient — skip
                continue

            elev_start = sampler.get_elevation(*seg_coords[0])
            elev_end   = sampler.get_elevation(*seg_coords[-1])

            if elev_start is None or elev_end is None:
                missing_elev += 1
                gradient_pct  = 0.0          # render as flat; no data
            else:
                gradient_pct = abs(elev_end - elev_start) / seg_len_m * 100.0

            color_hex = gradient_color(gradient_pct, bands)
            features.append({
                "geom":         LineString(seg_coords),
                "gradient_pct": gradient_pct,
                "color_hex":    color_hex,
                "color_rgba":   _hex_to_rgba(color_hex, line_alpha),
                "highway":      way["highway"],
                "name":         way["name"],
            })

    if missing_elev:
        print(
            f"    ⚠  {missing_elev} segment(s) had no DEM coverage "
            f"and are shown as flat (0 %)."
        )

    return features

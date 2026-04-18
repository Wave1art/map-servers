# slf-snow-tiles

Renders alpine snow and terrain data from public sources into static XYZ tile sets, hosted for free on GitHub Pages. Designed for use as custom overlay layers in [Gaia GPS](https://www.gaiagps.com) and any other map tool that accepts an XYZ tile URL.

Tile sets are rebuilt daily by a GitHub Actions workflow and committed back to the repo. New data is live within minutes of the source updating.

## Tile sets

| ID | Name | Source | Updates |
|----|------|--------|---------|
| `slf-snow-depth` | Snow Depth (HS 1-day) | SLF HS1D-v2 GeoJSON | Daily 07:15 UTC |
| `slf-new-snow` | New Snow (HN 1-day) | SLF HN1D-v2 GeoJSON | Daily 07:15 UTC |
| `slope-30` | Slope ≥ 30° | swisstopo WMTS | Static |
| `avalanche-zones` | Avalanche Release Zones | BAFU SilvaProtect WMTS | Static |

Tile URL pattern:
```
https://<your-username>.github.io/slf-snow-tiles/tiles/<layer-id>/{z}/{x}/{y}.png
```

A visual preview map with layer switcher, opacity control, and click-to-copy tile URLs is available at:
```
https://<your-username>.github.io/slf-snow-tiles/
```

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Setup

### 1. Fork or create the repo

Create a new GitHub repository and push this code to it.

### 2. Enable GitHub Pages

In the repo: **Settings → Pages → Source → Deploy from branch → `main` → `/docs`**

### 3. Install locally

```bash
uv sync
```

### 4. Build the first tiles

```bash
# Build all layers (fetches live data)
uv run build_tiles.py

# Or build a single layer
uv run build_tiles.py --layer slf-snow-depth

# Test with a local GeoJSON file
uv run build_tiles.py --layer slf-snow-depth --local path/to/file.geojson
```

### 5. Push

```bash
git add docs/
git commit -m "initial tiles"
git push
```

Your tiles are now live at `https://<your-username>.github.io/slf-snow-tiles/`.

## Updating tiles

### Automatic (daily)

The GitHub Actions workflow runs every day at 07:15 UTC and rebuilds all layers. No action required.

### Manual trigger

1. Go to **Actions → Update Tiles → Run workflow**
2. Optionally specify:
   - **Layer ID** — rebuild a single layer instead of all
   - **Max zoom** — override the zoom level from `layers.yml`
   - **Also generate .mbtiles** — produce a `.mbtiles` file downloadable as a workflow artifact (useful for importing directly into Gaia GPS mobile)

## Adding a new layer

All layer definitions live in `layers.yml`. Two source types are supported.

### GeoJSON source

Fetches a GeoJSON FeatureCollection and renders filled polygons using a `fill` property (hex colour) on each feature.

```yaml
- id: my-layer
  name: My Layer
  description: What this layer shows.
  type: geojson
  url: https://example.com/data.geojson
  schedule: "15 7 * * *"   # cron expression, or null for manual-only
  attribution: "© Source Name"
  bounds: [5.96, 45.82, 10.49, 47.81]   # optional, defaults to Switzerland
  zoom: { min: 5, max: 10 }             # optional override
  style:
    fill_property: fill                 # GeoJSON property containing hex colour
    partial_property: partialSnowCover  # boolean property → lower opacity
    opacity_full: 0.82
    opacity_partial: 0.43
  legend:
    - { label: "Class A", color: "#aabbcc" }
```

### WMTS / XYZ source

Downloads tiles directly from an existing public raster tile endpoint.

```yaml
- id: my-wmts-layer
  name: My WMTS Layer
  type: wmts
  url: "https://tiles.example.com/{z}/{x}/{y}.png"
  schedule: null     # static data — trigger manually once
  attribution: "© Source Name"
  zoom: { min: 5, max: 12 }
```

After editing `layers.yml`, run `uv run build_tiles.py --layer <id>` locally to test, then push.

## Using in Gaia GPS

### Web app

1. Open [gaiagps.com](https://gaiagps.com)
2. Map Layers → Add Custom Source → paste the tile URL

### Mobile (iOS / Android)

Run the workflow with **Also generate .mbtiles = true**, download the artifact, then:

- **iOS**: AirDrop or share via Files app → tap to open in Gaia GPS
- **Android**: transfer the file, open it from a file manager

The `.mbtiles` file works offline once imported.

## Project structure

```
slf-snow-tiles/
├── layers.yml                    # layer definitions — edit this to add layers
├── build_tiles.py                # tile renderer (GeoJSON + WMTS)
├── pyproject.toml
├── .github/
│   └── workflows/
│       └── update_tiles.yml      # daily + manual trigger
└── docs/                         # served by GitHub Pages
    ├── index.html                # preview map
    ├── layers.json               # auto-generated manifest
    └── tiles/
        ├── slf-snow-depth/       # {z}/{x}/{y}.png
        ├── slope-30/
        └── ...
```

## Data sources and licensing

| Layer | Source | Licence |
|-------|--------|---------|
| Snow depth / new snow | [WSL Institute for Snow and Avalanche Research SLF](https://www.slf.ch) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) — attribution required |
| Slope ≥ 30° | [swisstopo](https://www.swisstopo.admin.ch) via geo.admin.ch | [OGD Switzerland](https://opendata.swiss/en) |
| Avalanche zones | [BAFU/OFEV](https://www.bafu.admin.ch) via geo.admin.ch | [OGD Switzerland](https://opendata.swiss/en) |

When displaying these layers publicly, attribution to the original data source is required.

## Licence

This project (the code) is released under the [MIT Licence](LICENSE). Data and tile content remain under the licences of their respective sources above.

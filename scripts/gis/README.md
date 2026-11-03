# GIS acquisition pipeline

Offline data-preparation tooling for the countrywide municipal corporation directory.
It parses the source text file, queries OpenStreetMap (Nominatim + Overpass), validates
polygons, and writes the routing dataset consumed by `backend/`.

This directory is **not** imported by the FastAPI app. It only produces committed data
files under `backend/data/municipal/`.

## Setup

Use the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r scripts\gis\requirements-gis.txt
```

## 1. Parse-only check (no network)

Always run this first. It must report **54 records**:

```powershell
.\.venv\Scripts\python.exe scripts\gis\build_municipal_dataset.py --parse-only
```

## 2. Smoke test against two corporations

```powershell
.\.venv\Scripts\python.exe scripts\gis\build_municipal_dataset.py `
  --limit 2 `
  --user-agent "CivicComplaintRouting/1.0 (YOUR_EMAIL@example.com)"
```

Inspect `backend/data/municipal/review_report.csv` and `manifest.json` before running
the full set.

## 3. Full acquisition

```powershell
.\.venv\Scripts\python.exe scripts\gis\build_municipal_dataset.py `
  --user-agent "CivicComplaintRouting/1.0 (YOUR_EMAIL@example.com)"
```

To build the app dataset — 30 covered records only — add:

```powershell
.\.venv\Scripts\python.exe scripts\gis\build_municipal_dataset.py `
  --covered-only `
  --user-agent "CivicComplaintRouting/1.0 (YOUR_EMAIL@example.com)"
```

`--covered-only` drops records without geometry and writes the excluded names to
`manifest.json → excluded_missing`.

The run is rate-limited to slightly over one request per second, caches every raw
response, and checkpoints after each corporation. If interrupted, rerun the same
command; completed entries are restored from `.cache/gis/checkpoint.json`.

Use `--force-refresh` only when you intentionally want to bypass the cache.

## DataMeet overrides

The 11 DataMeet-backed corporations are applied as **manual overrides**, not as
Nominatim/Overpass results:

- Manifest: [`datameet_sources.json`](datameet_sources.json)
- Files: [`vendor/datameet/`](vendor/datameet/)
- Attribution: [`vendor/datameet/ATTRIBUTION.md`](vendor/datameet/ATTRIBUTION.md)

Each override is keyed by the same corporation `id` used in
`municipal_corporations.json`. The pipeline:

1. Loads a single `Feature` or a `FeatureCollection`.
2. Repairs invalid ward geometry with `make_valid` (needed for Vadodara).
3. Dissolves wards with `unary_union` (Mumbai, Pune, PCMC, Ahmedabad, Vadodara,
   Coimbatore).
4. Bypasses both the network and the checkpoint, so re-running upgrades a previously
   missing record without re-querying.

Overrides are marked `boundary_quality="community"`, `confidence="medium"`, and carry
the DataMeet source URL and CC BY 4.0 attribution.

To add another DataMeet city: copy the GeoJSON into `vendor/datameet/`, add an entry to
`datameet_sources.json`, verify the corporation `id`, and rerun.

## Outputs

Written to `backend/data/municipal/`:

| File | Purpose |
|---|---|
| `municipal_corporations.json` | Contacts, aliases, centroid, boundary status |
| `municipal_boundaries.geojson` | RFC 7946 polygons; `geometry: null` when missing |
| `review_report.csv` | Per-corporation QA table with manual-review flags |
| `manifest.json` | Query audit log, failures, rejection reasons |

Raw HTTP responses and checkpoints live in `.cache/gis/` and are gitignored.

## Validation rules

- Geometry must be a valid, non-empty Polygon/MultiPolygon.
- State/district boundaries (`admin_level` 2–6), district/tehsil names, and polygons
  larger than 3,000 km² are rejected.
- Polygons smaller than 1.5 km² are rejected as ward/building-scale.
- Coastal polygons whose mathematical centroid falls outside the land area are **kept**;
  a representative point is used and the record is flagged for manual review.
- Missing boundaries are emitted with `geometry: null`, never invented.

## After the run

1. Check `manifest.json` summary counts against `review_report.csv`.
2. Open the GeoJSON in QGIS or geojson.io.
3. Manually verify Mumbai, Delhi/NDMC, Bengaluru, Pune/PCMC, Chennai, Kolkata,
   Hyderabad, and Chandigarh.
4. Correct any `needs_manual_review` records before wiring the dataset into the API.

### Current coverage (2026-10-28)

After the DataMeet overrides, **30 of 54** cities have usable polygons. The checked-in
dataset is built with `--covered-only`, so the 24 missing cities are excluded from the
app output and listed in `manifest.json → excluded_missing`.

The 11 DataMeet-backed records are Mumbai, Pune, PCMC, Ahmedabad, Vadodara, Lucknow,
Kanpur, Kochi, Faridabad, Chandigarh, and Coimbatore. They are marked
`boundary_quality="community"`, `confidence="medium"`, and carry the DataMeet source
URL and CC BY 4.0 attribution.

The 24 excluded cities are: Nashik, Hubli-Dharwad, Warangal, Visakhapatnam, Surat,
Rajkot, Ghaziabad, Varanasi, Howrah, Asansol, Gaya, Ludhiana, Amritsar, Gurugram,
Thiruvananthapuram, Cuttack, Ranchi, Raipur, Guwahati, Dehradun, Shimla, Srinagar,
Jammu, and Puducherry.

> **Runtime Handoff (Tier 3 Integration)**: While these 24 cities currently lack granular GIS polygon boundaries in `municipal_boundaries.geojson`, the live application's 4-tier routing engine does **not** drop them. Coordinates in these cities (and across any unmapped location in India) are resolved via **Tier 3: Pan-India State & City Authority Router** ([`backend/state_authority_router.py`](../../backend/state_authority_router.py)), which maps city names, district bounds, and state nodal Directorates of Urban Local Bodies across all 28 States and 8 Union Territories before falling back to Tier 4.

This is an OpenStreetMap and DataMeet coverage gap, not a pipeline failure. For example,
OSM has Pune's district (`admin_level=5`) and subdistrict (`admin_level=6`) but no PMC
municipal boundary. DataMeet fills Pune; the excluded cities would need official ULB
polygons or manual GeoJSON overrides to be routed directly.

## Data source and licence

Boundary data comes from OpenStreetMap contributors (ODbL) and, for the 11 override
cities, from the DataMeet Municipal Spatial Data project (CC BY 4.0). Attribution must
be shown wherever the boundaries are displayed or redistributed. Respect the
[Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/) and
the Overpass API usage policy; always use a real contact address in the User-Agent.

DataMeet attribution:

> Maps provided by Spatial Data of Municipalities (Maps) Project by DataMeet.
> Made available under CC BY 4.0.

See [`vendor/datameet/ATTRIBUTION.md`](vendor/datameet/ATTRIBUTION.md).

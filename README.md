# ottawa-cooling-risk

Screening tool that scores site-specific environmental stress for data centre
cooling in Ottawa. Each module is independent; `grid.py` is still planned.

| Module | Status | Purpose |
|---|---|---|
| `src/freecooling.py` | done | Ottawa free-cooling baseline (airport typical year) |
| `src/uhi.py` | done | Heat-island assessment (relative to the city, checked across 2019/2021/2023) |
| `src/pttw.py` | done | Existing water demand around a site (PTTW) |
| `src/zoning.py` | done | Distance to the nearest residential zone (noise gate) |
| `src/grid.py` | planned | (future) |

## Setup

Use a project-local virtual environment (`.venv/`), e.g. with
[uv](https://docs.astral.sh/uv/), which can also fetch Python into the project:

```powershell
$env:UV_PYTHON_INSTALL_DIR = "$PWD\.python"
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

or with an existing Python: `python -m venv .venv`, then
`.venv\Scripts\pip install -r requirements.txt`. Run commands below with
`.venv\Scripts\python` (or activate the environment).

## Data setup

`data/` is not in the repository (about 15 MB of third-party data, regenerable). Most of it is
fetched and cached automatically on first use: the airport weather file (checksum-verified),
the Ottawa boundary, and the heat-island city grids. The water modules need these files
downloaded once into `data/` (Ontario Data Catalogue, Open Government Licence - Ontario):

```powershell
mkdir data\water_taking
$b = "https://files.ontario.ca/moe_mapping/downloads/2Water"
Invoke-WebRequest "$b/PTTW/PermitsToTakeWater_26-01.xlsx" -OutFile data\PermitsToTakeWater_26-01.xlsx
foreach ($y in 2019, 2020, 2022) {
  Invoke-WebRequest "$b/Water_Taking/${y}_Annual_Water_Taking.xlsx" -OutFile data\water_taking\${y}_Annual_Water_Taking.xlsx
}
```

The permit file is updated quarterly and its name changes (`_26-01` here); if the link has moved,
find the current spreadsheet at <https://data.ontario.ca/dataset/permit-to-take-water> and update
`PERMITS_XLSX` in `src/pttw.py`. Newer annual water-taking files at
<https://data.ontario.ca/dataset/water-taking> are picked up automatically (the latest year is used);
then run `python -m src.pttw 45.35 -75.91 --rebuild-cache`.

## Ottawa free-cooling baseline (`src/freecooling.py`)

```bash
python -m src.freecooling                       # defaults
python -m src.freecooling --cutoff 20 --wb-cutoff 12 --stress 28
```

One baseline for the whole city, from the Ottawa Intl Airport typical year
(NRCan CWEC 2023, 8,760 hours; the file is downloaded once, checksum-verified and
cached in `data/`). It reports, by plain counting of hours:

- hours and full days (every hour of the day at or below the cutoff) with outdoor
  **dry-bulb** at or below each cutoff (air-side free cooling; default 18 and 24 C);
- the same for **wet-bulb** (water-side / evaporative; default 10 and 15 C);
- hours of dry-bulb heat stress (default above 27 and 30 C);
- annual and monthly dry-bulb range, and annual wet-bulb range.

Defaults (typical year): 6,959 h / 223 days at 18 C dry-bulb; 5,481 h / 195 days at
10 C wet-bulb; 208 h above 27 C and 42 h above 30 C.

Wet-bulb is not in the weather file; it is calculated per hour from dry-bulb,
humidity and pressure with the ASHRAE psychrometric equations. Check: the
typical-year 0.4 / 1 / 2 % wet-bulb values (23.4 / 22.0 / 21.2 C) sit 0.2-0.5 C
below the design values published in the file (23.6 / 22.5 / 21.6 C), as expected
for a typical year versus the 23-year record behind the design values.

This is a typical year: it does not include extreme years or heat waves. The
cutoffs are design choices, so set them to match the cooling system under study.

## Existing water demand (`src/pttw.py`)

```bash
python -m src.pttw 45.35 -75.91                       # 2 km and 5 km
python -m src.pttw 45.35 -75.91 --radius 3000
```

Measures how much water-taking already exists around a site, as a relative indicator of
local water pressure. Inputs (Ontario Data Catalogue, Open Government Licence - Ontario),
kept in `data/`: the Permit To Take Water spreadsheet and the annual Water Taking
spreadsheets (`data/water_taking/`; the latest year found is used, currently 2022).

For each radius it reports **groundwater supply demand**: litres/day actually reported
taken, litres/day permitted (active permits), and a rank against 2 km-spaced reference
points inside the City of Ottawa boundary (ward polygons from the City's map service,
cached in `data/ottawa_boundary.json`). The rank is the share of reference points with
less demand, banded in thirds (low / moderate / high). It also lists the largest nearby
surface-water permits (context only, 5 km) such as cooling-water takings.

Judgement calls (all named constants at the top of `src/pttw.py`):

- Counted as supply demand: everything except temporary or returned-to-source takings,
  i.e. `EXCLUDED_PURPOSE_CATEGORIES` (dewatering, construction, remediation) and
  `EXCLUDED_SPECIFIC_PURPOSES` (cooling water, power production, heat pumps, pumping
  tests, dams, and similar).
- "Groundwater" includes permits that draw on both ground and surface water.
- Reported volumes marked `NR`/`NA` are not treated as numbers; they are counted as
  `sources_not_reported`.

Limits: demand only (no aquifer or river capacity data); takings under 50,000 L/day
(private wells) are absent; municipal supply is not captured, so urban sites (served by
the City's system) show little demand here while the City's own intakes appear in the
surface-water context list; most of Ottawa records zero demand, so a site with any
demand ranks high. Sites outside the City boundary (e.g. Gatineau) raise an error.
Reported volumes are from the data year, not today. The spreadsheets are parsed once
into `data/pttw_ottawa_cache.json`; use `--rebuild-cache` after replacing them.

## Distance to residential zoning (`src/zoning.py`)

```bash
python -m src.zoning 45.349 -76.039                         # default: D-6 Class III (conservative)
python -m src.zoning 45.349 -76.039 --facility-class II
```

Example (Carp, Class III):

```json
{
  "facility_class": "III", "minimum_separation_m": 300.0, "influence_area_m": 1000.0,
  "distance_m": 93.5, "nearest_zone_code": "V1N",
  "nearest_zone_group": "village / rural residential",
  "within_min_separation": true, "within_influence_area": true,
  "flag": "within_min_separation",
  "statement": "Residential zoning is within 300 m, the D-6 Class III minimum separation distance: the guideline treats this as incompatible even with mitigation. Noise should drive the cooling choice; favour enclosed or low-noise cooling."
}
```

For a noise-gate check: the shortest distance from the site to any residential-zoned polygon
(0 if the site is inside one), compared with two distances from Ontario guideline **D-6**
(Compatibility between Industrial Facilities) for the chosen facility class:

| Class | Describes | Minimum separation | Influence area |
|---|---|---|---|
| I | small, daytime only, sound not audible off site | 20 m | 70 m |
| II | shift work, sound occasionally audible off site | 70 m | 300 m |
| III | large, continuous operation, sound frequently audible | 300 m | 1,000 m |

Flags: `within_min_separation` (D-6 treats homes this close as incompatible even with
mitigation), `within_influence_area` (adverse effects may be felt), `clear`. Each flag maps to
one fixed sentence about the cooling choice (`FLAG_STATEMENTS`). The search radius defaults to the
influence area and may not be smaller, so `clear` always means nothing residential inside it.

- **Facility class is a judgement.** D-6 does not list data centres. A 24/7 facility cannot be
  Class I (daytime only, inaudible off site); II or III depends on the equipment. The default
  is III, the most conservative. D-6 is a land-use planning guideline measured property line
  to property line; this tool measures from a point, so a large site overstates separation.
  D-6 is also being reworked; its replacement has not been checked.
- **Noise limits (NPC-300) are not modelled.** That guideline sets sound level limits at the
  receiving home (roughly 45-50 dBA urban and 40-45 dBA rural, depending on time of day, or
  the background level if higher), not distances, and needs the equipment's noise level, which
  this tool does not have. It does count vacant lots zoned for housing as receptors, which
  supports using zoning.
- **Residential zones (conservative):** every zone that permits ordinary homes, grouped in the
  output as `nearest_zone_group`: *urban residential* R1-R5 and RM (By-law Part 6); *village /
  rural residential* RR and V1-V3; *rural countryside* RU. RU maps whole farm lots, so a hit may
  have no house; it is counted anyway and the result carries a `note`. Not counted although they
  permit dwellings: mixed-use and mainstreet zones (AM, TM, GM, MC, MD, TD, LC, VM),
  institutional (I1, I2) and AG. With RU included and Class III, most sites are within the
  influence area of some zone, so the middle flag discriminates little; read `nearest_zone_group`.
- **Result states:** no residential zone in range reports "none within radius" (`distance_m`
  null, flag `clear`); no zoning of any kind nearby (e.g. Gatineau) raises `NoZoningDataError`;
  points outside the layer extent raise `OutOfExtentError` without a network call.
- **Data:** City of Ottawa `Zoning/MapServer` layer 3 (Zoning By-law 2008-250 Consolidation).
  **That by-law has been replaced by Zoning By-law 2026-50** (enacted 2026-03-11, mostly in
  force since the OLT decision of 2026-07-02; applications deemed complete from that date must
  meet both, most restrictive applying). The City publishes the new zoning as
  `Zoning_Bylaw_2026_50/MapServer` (layer 5, field `ZN_CODE2`, different codes: N1-N6 replace
  R1-R5). Repoint `SERVICE_URL`, `LAYER_ID`, `ZONE_FIELD` and `RESIDENTIAL_ZONES` to switch;
  not yet done.
- **Zone field:** the base zone code is in `PARENTZONE`, populated on all 14,093 rows.
  `ZONE_MAIN` is not usable alone: 50 rows have `ZONE_MAIN = "*_"` (code under a pending
  amendment) with the real base zone only in `PARENTZONE`.
- **Distance method:** polygons are queried with `geometry`, `distance`, `inSR=4326`,
  `outSR=3857`, converted back to lat/lon and measured on a local metric plane centred on the
  site (Web Mercator metres are about 1.4x too long in Ottawa). Checked against an independent
  brute-force haversine over densified edges: within 1 m at three sites.
- **Fetching:** the service does not honour a stable sort order, so offset paging returned
  overlapping pages (4,617 rows for 4,019 polygons). The module fetches matching object IDs
  first, then geometries by ID in chunks (POST), splitting any chunk the service truncates.
  A 20 km query returned exactly the server's count (4,019).
## Heat-island assessment (`src/uhi.py`)

Answers one question: is the ground around this site unusually hot compared with the
rest of Ottawa, and does that hold in every year? It uses only the City's heat map
(2019, 2021 and 2023 layers) and is independent of the weather baseline above.

```bash
python -m src.uhi 45.4250 -75.6930                  # all three years
python -m src.uhi 45.35 -75.91 --radius 1000 --grid 7
python -m src.uhi 45.35 -75.91 --layer 0            # one year only (0=2019, 1=2021, 2=2023)
```

For each year the site's buffer mean (surface temperature, C) is compared with that
same year's citywide map: degrees above the city median, and percentile rank. Years
are never compared by absolute temperature (different dates and sensors). The
`assessment` is:

- `persistent_hotspot`: top third of the city in every year checked (needs at least 2 years);
- `intermittent`: top third in some years only;
- `not_hotspot`: never in the top third.

Example (downtown core): `persistent_hotspot`, 3/3 years, +5 to +6.5 C above the city
median, 89th-95th percentile. A year with no data at the site (NoData, or outside that
layer's extent) is skipped and listed under `skipped`.

Reference points (500 m buffer): downtown, the airport and the Hawthorne industrial
area are persistent hotspots (+5 to +7.5 C above median); Kanata North tech park and the
Central Experimental Farm are intermittent (+1 to +3 C); Mer Bleue bog is below the
median every year.

The first run samples a citywide grid per year (30x30 = 900 `identify` calls, about 10
seconds each) and caches it to `data/uhi_city_grid_layer<N>.json` (about 5 KB each,
sampled temperatures only). Nothing else is stored: buffer samples are fetched live.
Use `--refresh-cache` to resample. From Python:
`from src.uhi import assess_site; assess_site(45.4250, -75.6930)`.

## Poster figures (`figures/make_figures.py`)

```bash
.venv\Scripts\pip install -r requirements-figures.txt   # matplotlib; only the figures need it
python -m figures.make_figures          # all three, or e.g.  python -m figures.make_figures 2
```

Writes 300 dpi PNGs (14 x 6 in, white background, all text 24 pt or larger) to `figures/out/`;
each file carries Title, Source and Description in its metadata for the captions.

1. `fig1_site_profiles.png`: three sites across the four indicators (free cooling is one shared
   city-wide baseline; heat island, water demand and nearest homes are per site).
2. `fig2_monthly_free_cooling.png`: free-cooling hours per month, dry-bulb at or below 18 C and
   wet-bulb at or below 10 C.
3. `fig3_heat_persistence.png`: map of how many of 2019, 2021 and 2023 put each point in the
   hottest third of Ottawa, with the sample sites marked.

The sites, palette and thresholds are constants at the top of the script. Palette: teal
`#00958F` and orange `#E8590C`, validated for colour-blind separation on white (the first
teal tried failed the chroma check and read as grey); replace with the poster's exact values
and re-validate. Figures 1 and 3 query live services (about a minute).

## How it works

1. Data: City of Ottawa `Climate_Resiliency/MapServer`, layers 0-2 (Urban Heat
   Island 2019, 2021, 2023): Landsat surface temperature (C), each a single
   clear-sky day (2019-07-18 11:50, 2021-08-24 15:50, 2023-07-05 15:49). The raster
   is read with the `identify` endpoint (`Pixel Value`; `"NoData"` over
   water/unmapped areas; empty results outside the extent).
2. A 5x5 lattice over the buffer's bounding square is sampled; points inside the
   circle are kept (13 points). The radius is corrected for Web Mercator stretching
   (~1.42x at Ottawa's latitude). NoData pixels are ignored.
3. The buffer mean is compared with a citywide 30x30 lattice of the same layer.
4. Bands: bottom third of the percentile range = `low`, middle = `moderate`,
   top = `high`.

Tunables are constants at the top of `src/uhi.py` (`DEFAULT_RADIUS_M`,
`BUFFER_GRID_SIZE`, `CITY_GRID_SIZE`, `BAND_LOW_MAX_PCT`, `BAND_MODERATE_MAX_PCT`)
and CLI/function parameters.

## Errors

- Outside the raster extent: `OutOfExtentError` (no network call is made).
- Buffer is entirely NoData (e.g. river): `NoDataError`.
- HTTP / ArcGIS failures: `ServiceError`.

The CLI prints `error: ...` and exits with status 1.

## Caveats

- Each layer is a single-day snapshot (afternoon; 2019 is late morning) of
  **surface** temperature, not air temperature. It says where the ground is hot,
  not how hot the air will be, and it is not used to adjust the free-cooling numbers.
- Rankings are relative to all of Ottawa, including forest, farmland and wetland, so
  they separate built from unbuilt land well but cannot tell hot pavement from dry
  bare fields. Read a flag alongside the site's land use.
- Buffer means are smoother than point samples, which slightly compresses extremes.
  Only 13 points are sampled per buffer by default (`--grid` raises this).

## Tests

```bash
python -m unittest discover -s tests -t .
```

(Equivalently `python -m pytest` if pytest is installed.) Tests use mocked HTTP
responses and never touch the network.

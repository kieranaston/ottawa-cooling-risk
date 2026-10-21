"""Export the numbers behind the poster as CSVs (one per module) plus a notes file.

Run from the project root:   python -m figures.export_data
Output: poster_data/*.csv and poster_data/DATA_NOTES.md. Uses only the standard library and the
project's own modules; the heat-island and water files query live services (under a minute).
"""

from __future__ import annotations

import csv
import datetime
from pathlib import Path

from src import freecooling, pttw, uhi, zoning

OUT_DIR = Path(__file__).resolve().parent.parent / "poster_data"

# The same named sites in every file. (name, lat, lon)
SITES = (
    ("Downtown core", 45.4250, -75.6930),
    ("Kanata North", 45.3500, -75.9100),
    ("Carp (rural)", 45.3490, -76.0390),
    ("Ottawa airport", 45.3190, -75.6690),
)
HEAT_RADIUS_M = uhi.DEFAULT_RADIUS_M
WATER_RADII_M = (2000.0, 5000.0)
D6_CLASS = zoning.DEFAULT_FACILITY_CLASS
DRY_BULB_CUTOFF_C = freecooling.DEFAULT_CUTOFFS_C[0]
WET_BULB_CUTOFF_C = freecooling.DEFAULT_WB_CUTOFFS_C[0]
YEARS = {layer: info["year"] for layer, info in uhi.LAYERS.items()}      # {0: 2019, 1: 2021, 2: 2023}


def write_csv(name: str, header: list[str], rows: list[list]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def free_cooling_monthly() -> tuple[Path, dict]:
    hours = freecooling.read_epw(freecooling.ensure_epw())
    n, dry, wet = [0] * 12, [0] * 12, [0] * 12
    for month, _, db, wb in hours:
        n[month - 1] += 1
        dry[month - 1] += db <= DRY_BULB_CUTOFF_C
        wet[month - 1] += wb <= WET_BULB_CUTOFF_C
    rows = [[freecooling.MONTHS[m], m + 1, n[m], DRY_BULB_CUTOFF_C, dry[m], WET_BULB_CUTOFF_C, wet[m]]
            for m in range(12)]
    header = ["month", "month_number", "hours_in_month", "dry_bulb_cutoff_c",
              "hours_at_or_below_dry_bulb_cutoff", "wet_bulb_cutoff_c", "hours_at_or_below_wet_bulb_cutoff"]
    base = freecooling.ottawa_baseline()
    totals = {"dry_hours": sum(dry), "wet_hours": sum(wet),
              "dry_full_days": base["free_cooling_dry_bulb"][f"{DRY_BULB_CUTOFF_C:g}C"]["full_days"],
              "wet_full_days": base["free_cooling_wet_bulb"][f"{WET_BULB_CUTOFF_C:g}C"]["full_days"]}
    return write_csv("free_cooling_monthly.csv", header, rows), totals


def heat_island() -> Path:
    header = ["site", "lat", "lon", "buffer_radius_m"]
    for y in YEARS.values():
        header.append(f"buffer_mean_c_{y}")
    for y in YEARS.values():
        header.append(f"above_city_median_c_{y}")
    for y in YEARS.values():
        header.append(f"percentile_{y}")
    header += ["years_in_hottest_third", "verdict"]
    rows = []
    for name, lat, lon in SITES:
        r = uhi.assess_site(lat, lon, HEAT_RADIUS_M)
        by_year = r["years"]
        row = [name, lat, lon, HEAT_RADIUS_M]
        row += [by_year[y]["buffer_mean_c"] if y in by_year else "" for y in YEARS.values()]
        row += [by_year[y]["above_city_median_c"] if y in by_year else "" for y in YEARS.values()]
        row += [by_year[y]["percentile"] if y in by_year else "" for y in YEARS.values()]
        row += [r["years_in_top_third"], r["assessment"]]
        rows.append(row)
    return write_csv("heat_island.csv", header, rows)


def water_demand() -> tuple[Path, dict]:
    header = ["site", "lat", "lon", "data_year"]
    for km in (int(r / 1000) for r in WATER_RADII_M):
        header += [f"reported_groundwater_l_per_day_{km}km", f"permitted_groundwater_l_per_day_{km}km",
                   f"sources_reporting_{km}km", f"active_permit_sources_{km}km",
                   f"rank_percentile_in_ottawa_{km}km", f"rank_band_{km}km"]
    rows, ref = [], {}
    for name, lat, lon in SITES:
        r = pttw.assess_site(lat, lon, WATER_RADII_M)
        row = [name, lat, lon, r["data_year"]]
        for radius in WATER_RADII_M:
            g = r["groundwater_supply_demand"][f"{radius:g}m"]
            row += [g["reported_l_per_day"], g["permitted_max_l_per_day"], g["sources_reporting"],
                    g["active_permit_sources"], g["percentile_in_ottawa"], g["band"]]
            ref[radius] = {"points": g["city_points"], "zero": g["city_points_with_zero"],
                           "median": g["city_median_l_per_day"]}
        rows.append(row)
    return write_csv("water_demand.csv", header, rows), ref


def noise() -> Path:
    header = ["site", "lat", "lon", "d6_class_used", "minimum_separation_m", "influence_area_m",
              "distance_to_nearest_residential_zone_m", "nearest_zone_code", "nearest_zone_family",
              "nearest_zone_group", "flag", "flag_if_class_I", "flag_if_class_II", "flag_if_class_III"]
    rows = []
    for name, lat, lon in SITES:
        r = zoning.nearest_residential(lat, lon, D6_CLASS)          # radius = 1,000 m covers every class
        d = r["distance_m"]
        flags = {c: zoning.flag_for(d, *zoning.thresholds_for(c)) for c in zoning.D6_DISTANCES_M}
        rows.append([name, lat, lon, D6_CLASS, r["minimum_separation_m"], r["influence_area_m"],
                     "" if d is None else d, r["nearest_zone_code"] or "", r["nearest_zone_family"] or "",
                     r["nearest_zone_group"] or "", r["flag"], flags["I"], flags["II"], flags["III"]])
    return write_csv("noise.csv", header, rows)


def notes(totals: dict, ref: dict, water_year: int) -> Path:
    today = datetime.date.today().isoformat()
    sites = "\n".join(f"| {n} | {la} | {lo} |" for n, la, lo in SITES)
    text = f"""# Poster data notes

Generated {today} by `python -m figures.export_data` (ottawa-cooling-risk). One CSV per module, the
same four named sites in the three site-level files. All values are measured or counted from the
sources below; none is modelled except wet-bulb temperature (calculated, see file 1).

## Sites (WGS84)

| Site | Latitude | Longitude |
|---|---|---|
{sites}

Coordinates are approximate points I chose for illustration, not real data-centre locations.

## 1. `free_cooling_monthly.csv` (city-wide, not site-specific)

- **Source / year:** NRCan CWEC 2023 typical meteorological year for Ottawa Intl A (Climate ID 6106001),
  8,760 hourly records built from typical months of 1998-2020 observations. A typical year, not a
  specific year; it contains no extreme years or heat waves.
- **Columns:** `hours_in_month` (672-744); `dry_bulb_cutoff_c` = {DRY_BULB_CUTOFF_C:g} and
  `wet_bulb_cutoff_c` = {WET_BULB_CUTOFF_C:g} (degrees C); `hours_at_or_below_*` = count of hours in that
  month with outdoor temperature at or below the cutoff. Units: hours.
- **How computed:** plain counting of hourly values. Dry-bulb is read from the file. Wet-bulb is not in
  the file; it is calculated per hour from dry-bulb, relative humidity and pressure with the ASHRAE
  psychrometric equations (humidity above 100% up to 105% treated as saturated). Check: typical-year
  0.4 / 1 / 2 % wet-bulb values (23.4 / 22.0 / 21.2 C) sit 0.2-0.5 C below the design values published in
  the file (23.6 / 22.5 / 21.6 C).
- **Annual totals:** dry-bulb {totals['dry_hours']:,} h ({totals['dry_full_days']} full days, meaning every hour
  of the day at or below {DRY_BULB_CUTOFF_C:g} C); wet-bulb {totals['wet_hours']:,} h ({totals['wet_full_days']} full days at
  or below {WET_BULB_CUTOFF_C:g} C). The cutoffs are my defaults (air-side 18 C dry-bulb, water-side 10 C
  wet-bulb), not values from a standard; change them to match the cooling system.

## 2. `heat_island.csv`

- **Source / years:** City of Ottawa Urban Heat Island rasters, Landsat surface temperature (degrees C),
  one clear-sky day each: 2019-07-18 (11:50), 2021-08-24 (15:50), 2023-07-05 (15:49).
  Surface temperature, not air temperature.
- **Columns:** `buffer_radius_m` = {HEAT_RADIUS_M:g}; `buffer_mean_c_<year>` = mean of the valid pixels
  at 13 sample points inside a {HEAT_RADIUS_M:g} m circle around the site (degrees C);
  `above_city_median_c_<year>` = buffer mean minus the median of that year's citywide sample (degrees C);
  `percentile_<year>` = share of that year's citywide sample at or below the buffer mean (0-100, mid-rank);
  `years_in_hottest_third` = years (of the three) with percentile at or above 66.7;
  `verdict` = `persistent_hotspot` (hottest third in every year checked, at least 2 years),
  `intermittent` (some years only) or `not_hotspot` (never).
- **How computed:** the citywide sample is a 30 x 30 lattice over the raster extent, values with data
  only (about 440 points per year). Years are ranked against their own year, never compared by
  absolute temperature (different dates and sensors). Rankings are relative to all of Ottawa, including
  forest, farmland and wetland, so they cannot tell hot pavement from dry bare fields.

## 3. `water_demand.csv`

- **Source / year:** Ontario Permit To Take Water register (permits, active only) and Ontario Water
  Taking annual reported volumes, reporting year {water_year}. Takings under 50,000 L/day and water from
  the municipal supply system are not in the data, so urban sites can show little demand.
- **Columns (per radius, 2 km and 5 km):** `reported_groundwater_l_per_day` = litres reported taken in
  {water_year} at groundwater sources within the radius, divided by 365 (litres per day);
  `permitted_groundwater_l_per_day` = sum of the maximum litres per day allowed by active permits for those
  sources; `sources_reporting` / `active_permit_sources` = how many sources each sum covers;
  `rank_percentile_in_ottawa` = share of Ottawa reference points with strictly less reported demand
  (0-100); `rank_band` = low / moderate / high (thirds of that range).
- **What counts:** groundwater sources (including permits drawing on both ground and surface water) for
  supply-type purposes. Excluded as temporary or returned to the source: dewatering, construction,
  remediation, cooling water, power production, heat pumps, pumping tests, pipeline testing, dams and
  reservoirs, dredging, wetlands, wildlife conservation, aquaculture, fish ponds.
- **Reported vs permitted:** the two sums come from different files and are not matched row by row.
  Reported can be above zero while permitted is zero (e.g. Ottawa airport at 5 km) when the reporting
  source's permit is not in the active list, for example because it has expired.
- **How ranked:** reference points on a 2 km lattice inside the City of Ottawa boundary (ward polygons),
  {ref[2000.0]['points']} points; at 2 km, {ref[2000.0]['zero']} of them have zero demand and at 5 km {ref[5000.0]['zero']},
  so any site with some demand ranks high. This is relative demand only: no aquifer or river
  capacity figure exists in the data.

## 4. `noise.csv`

- **Source:** City of Ottawa Zoning By-law 2008-250 Consolidation (layer 3). That by-law has been replaced
  by Zoning By-law 2026-50 (enacted 2026-03-11), which has different zone codes; this file uses the
  old layer.
- **Columns:** `distance_to_nearest_residential_zone_m` = shortest distance in metres from the site point
  to the edge of the nearest residential-zoned polygon (0 if the site is inside one; blank if none within
  the search radius of {zoning.D6_DISTANCES_M[D6_CLASS][1]:g} m); `nearest_zone_code` = full zone code with subzone;
  `nearest_zone_family` = base code; `nearest_zone_group` = urban residential (R1-R5, RM), village / rural
  residential (RR, V1-V3) or rural countryside (RU).
- **D-6 class and flags:** thresholds come from Ontario guideline D-6 (Compatibility between Industrial
  Facilities): minimum separation / influence area = Class I 20 / 70 m, Class II 70 / 300 m,
  Class III 300 / 1,000 m. `d6_class_used` = {D6_CLASS} (the most conservative; D-6 does not list data centres, so
  the class is a judgement). `flag` = `within_min_separation` (distance at or below the separation
  distance), `within_influence_area` (at or below the influence area) or `clear`; `flag_if_class_I/II/III`
  give the flag under each class for the same distance.
- **How computed:** polygon vertices are converted from Web Mercator to a local metric plane centred on
  the site and the shortest point-to-edge distance is taken (checked against an independent haversine
  calculation to within 1 m). Rural countryside (RU) is counted as residential (conservative) though a
  hit may be a farm lot with no house. Mixed-use, institutional and agricultural zones are not counted.
  D-6 distances are measured property line to property line; this is measured from a point.
"""
    path = OUT_DIR / "DATA_NOTES.md"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    p1, totals = free_cooling_monthly()
    p2 = heat_island()
    p3, ref = water_demand()
    p4 = noise()
    water_year = pttw.load_tables()["data_year"]
    p5 = notes(totals, ref, water_year)
    for p in (p1, p2, p3, p4, p5):
        print("wrote", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

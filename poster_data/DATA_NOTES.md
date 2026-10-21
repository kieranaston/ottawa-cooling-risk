# Poster data notes

Generated 2026-10-21 by `python -m figures.export_data` (ottawa-cooling-risk). One CSV per module, the
same four named sites in the three site-level files. All values are measured or counted from the
sources below; none is modelled except wet-bulb temperature (calculated, see file 1).

## Sites (WGS84)

| Site | Latitude | Longitude |
|---|---|---|
| Downtown core | 45.425 | -75.693 |
| Kanata North | 45.35 | -75.91 |
| Carp (rural) | 45.349 | -76.039 |
| Ottawa airport | 45.319 | -75.669 |

Coordinates are approximate points I chose for illustration, not real data-centre locations.

## 1. `free_cooling_monthly.csv` (city-wide, not site-specific)

- **Source / year:** NRCan CWEC 2023 typical meteorological year for Ottawa Intl A (Climate ID 6106001),
  8,760 hourly records built from typical months of 1998-2020 observations. A typical year, not a
  specific year; it contains no extreme years or heat waves.
- **Columns:** `hours_in_month` (672-744); `dry_bulb_cutoff_c` = 18 and
  `wet_bulb_cutoff_c` = 10 (degrees C); `hours_at_or_below_*` = count of hours in that
  month with outdoor temperature at or below the cutoff. Units: hours.
- **How computed:** plain counting of hourly values. Dry-bulb is read from the file. Wet-bulb is not in
  the file; it is calculated per hour from dry-bulb, relative humidity and pressure with the ASHRAE
  psychrometric equations (humidity above 100% up to 105% treated as saturated). Check: typical-year
  0.4 / 1 / 2 % wet-bulb values (23.4 / 22.0 / 21.2 C) sit 0.2-0.5 C below the design values published in
  the file (23.6 / 22.5 / 21.6 C).
- **Annual totals:** dry-bulb 6,959 h (223 full days, meaning every hour
  of the day at or below 18 C); wet-bulb 5,481 h (195 full days at
  or below 10 C). The cutoffs are my defaults (air-side 18 C dry-bulb, water-side 10 C
  wet-bulb), not values from a standard; change them to match the cooling system.

## 2. `heat_island.csv`

- **Source / years:** City of Ottawa Urban Heat Island rasters, Landsat surface temperature (degrees C),
  one clear-sky day each: 2019-07-18 (11:50), 2021-08-24 (15:50), 2023-07-05 (15:49).
  Surface temperature, not air temperature.
- **Columns:** `buffer_radius_m` = 500; `buffer_mean_c_<year>` = mean of the valid pixels
  at 13 sample points inside a 500 m circle around the site (degrees C);
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
  Taking annual reported volumes, reporting year 2022. Takings under 50,000 L/day and water from
  the municipal supply system are not in the data, so urban sites can show little demand.
- **Columns (per radius, 2 km and 5 km):** `reported_groundwater_l_per_day` = litres reported taken in
  2022 at groundwater sources within the radius, divided by 365 (litres per day);
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
  720 points; at 2 km, 586 of them have zero demand and at 5 km 250,
  so any site with some demand ranks high. This is relative demand only: no aquifer or river
  capacity figure exists in the data.

## 4. `noise.csv`

- **Source:** City of Ottawa Zoning By-law 2008-250 Consolidation (layer 3). That by-law has been replaced
  by Zoning By-law 2026-50 (enacted 2026-03-11), which has different zone codes; this file uses the
  old layer.
- **Columns:** `distance_to_nearest_residential_zone_m` = shortest distance in metres from the site point
  to the edge of the nearest residential-zoned polygon (0 if the site is inside one; blank if none within
  the search radius of 1000 m); `nearest_zone_code` = full zone code with subzone;
  `nearest_zone_family` = base code; `nearest_zone_group` = urban residential (R1-R5, RM), village / rural
  residential (RR, V1-V3) or rural countryside (RU).
- **D-6 class and flags:** thresholds come from Ontario guideline D-6 (Compatibility between Industrial
  Facilities): minimum separation / influence area = Class I 20 / 70 m, Class II 70 / 300 m,
  Class III 300 / 1,000 m. `d6_class_used` = III (the most conservative; D-6 does not list data centres, so
  the class is a judgement). `flag` = `within_min_separation` (distance at or below the separation
  distance), `within_influence_area` (at or below the influence area) or `clear`; `flag_if_class_I/II/III`
  give the flag under each class for the same distance.
- **How computed:** polygon vertices are converted from Web Mercator to a local metric plane centred on
  the site and the shortest point-to-edge distance is taken (checked against an independent haversine
  calculation to within 1 m). Rural countryside (RU) is counted as residential (conservative) though a
  hit may be a farm lot with no house. Mixed-use, institutional and agricultural zones are not counted.
  D-6 distances are measured property line to property line; this is measured from a point.

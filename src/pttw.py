"""Existing water demand around a site, from Ontario's Permit To Take Water (PTTW) data.

Sources (both Ontario Data Catalogue, Open Government Licence - Ontario), kept in ``data/``:
  * PermitsToTakeWater_*.xlsx            - permits: holder, purpose, source, max litres/day, lat/lon
  * water_taking/<year>_Annual_Water_Taking.xlsx - volumes actually reported taken, per source

The indicator is **groundwater supply demand** within a radius of the site: litres/day reported
taken (latest year available) and litres/day permitted, ranked against lattice points across
Ottawa. Takings that are temporary or returned to the source (dewatering, cooling water, ...)
are excluded from it (see EXCLUDED_*) and large surface-water takings are listed as context.
This is relative demand only: there is no supply figure, takings under 50,000 L/day are not in
the data, and water supplied by the municipal system is not captured.

Usage:  python -m src.pttw 45.35 -75.91 [--radius 2000 --radius 5000]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import zipfile
import xml.etree.ElementTree as ET
from bisect import bisect_left
from pathlib import Path
from typing import Iterator, Optional

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PERMITS_XLSX = DATA_DIR / "PermitsToTakeWater_26-01.xlsx"
PERMITS_DATA_SHEET = 2                      # sheet 1 is the field dictionary
WATER_TAKING_DIR = DATA_DIR / "water_taking"
CACHE_PATH = DATA_DIR / "pttw_ottawa_cache.json"
BOUNDARY_CACHE_PATH = DATA_DIR / "ottawa_boundary.json"

# Ottawa and a margin (sources within 5 km of the city edge still count for sites near it).
BBOX = {"lat": (44.7, 45.8), "lon": (-76.6, -75.0)}

WARDS_URL = "https://maps.ottawa.ca/arcgis/rest/services/AdministrativeAreas/MapServer/0/query"
BOUNDARY_SIMPLIFY_DEG = 0.0005              # ~50 m; keeps the polygon small
REQUEST_TIMEOUT_S = 60

DEFAULT_RADII_M = (2000.0, 5000.0)
CITY_LATTICE_SPACING_M = 2000.0             # reference points inside Ottawa for the ranking
CONTEXT_RADIUS_M = 5000.0
CONTEXT_TOP_N = 5

# Takings not counted as supply demand: temporary, returned to the source, or not a withdrawal
# that reduces local supply. Matched case-insensitively. Adjust to taste.
EXCLUDED_PURPOSE_CATEGORIES = {"dewatering", "dewatering construction", "construction", "remediation"}
EXCLUDED_SPECIFIC_PURPOSES = {"cooling water", "power production", "heat pumps", "pumping test",
                              "pipeline testing", "dams and reservoirs", "dredging", "wetlands",
                              "wildlife conservation", "aquaculture", "fish ponds"}

# Ranking bands: bottom third = low, middle = moderate, top third = high (same as the heat map).
BAND_LOW_MAX_PCT = 100 / 3
BAND_MODERATE_MAX_PCT = 200 / 3

NOTES = (
    "Relative demand only: no supply (aquifer or river capacity) figure is available.",
    "Percentile = share of Ottawa reference points with less demand; most of Ottawa records zero.",
    "Takings under 50,000 L/day (e.g. private wells) are not in the data.",
    "Water supplied by the municipal system is not captured; urban sites may show little demand.",
    "Reported volumes are for data_year; permitted maximums are from active permits.",
)

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_EARTH_R_M = 6371008.8


class PTTWError(Exception):
    """Base class for errors raised by this module."""


class DataMissingError(PTTWError):
    """A required data file is missing or unreadable."""


class OutsideOttawaError(PTTWError):
    """The site is not inside the City of Ottawa boundary."""


# --- Minimal xlsx reader (standard library only) ---------------------------
def read_xlsx(path: Path, sheet: int = 1) -> Iterator[dict]:
    """Yield each data row of a worksheet as {header: string-or-None}."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise DataMissingError(f"Cannot open {path}: {exc}") from exc
    with z:
        yield from _sheet_rows(z, sheet)


def _sheet_rows(z: zipfile.ZipFile, sheet: int) -> Iterator[dict]:
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        with z.open("xl/sharedStrings.xml") as f:
            for _, el in ET.iterparse(f):
                if el.tag == _NS + "si":
                    shared.append("".join(t.text or "" for t in el.iter(_NS + "t")))
                    el.clear()

    def col(ref: str) -> int:
        n = 0
        for ch in re.match(r"[A-Z]+", ref).group():
            n = n * 26 + ord(ch) - 64
        return n - 1

    header = None
    with z.open(f"xl/worksheets/sheet{sheet}.xml") as f:
        for _, el in ET.iterparse(f):
            if el.tag != _NS + "row":
                continue
            cells = {}
            for c in el.iter(_NS + "c"):
                v = c.find(_NS + "v")
                if v is not None:
                    cells[col(c.get("r"))] = shared[int(v.text)] if c.get("t") == "s" else v.text
                else:
                    inline = c.find(_NS + "is")
                    if inline is not None:
                        cells[col(c.get("r"))] = "".join(t.text or "" for t in inline.iter(_NS + "t"))
            el.clear()
            if header is None:
                header = [(cells.get(i) or "").strip() for i in range(max(cells) + 1)] if cells else []
                continue
            yield {h: cells.get(i) for i, h in enumerate(header) if h}


# --- Geometry --------------------------------------------------------------
def utm_to_latlon(zone: int, easting: float, northing: float) -> tuple[float, float]:
    """UTM (northern hemisphere, WGS84/NAD83) to (lat, lon) in degrees."""
    a, e2, k0 = 6378137.0, 0.00669438, 0.9996
    ep2 = e2 / (1 - e2)
    x, m = easting - 500000.0, northing / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    s, c = math.sin(phi1), math.cos(phi1)
    n1 = a / math.sqrt(1 - e2 * s * s)
    t1, c1 = math.tan(phi1) ** 2, ep2 * c * c
    r1 = a * (1 - e2) / (1 - e2 * s * s) ** 1.5
    d = x / (n1 * k0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon0 = math.radians(zone * 6 - 183)
    lon = lon0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                  + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / c
    return math.degrees(lat), math.degrees(lon)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def point_in_rings(lon: float, lat: float, rings: list) -> bool:
    """Even-odd point-in-polygon over all rings of one polygon feature."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def load_boundary(path: Path = BOUNDARY_CACHE_PATH) -> list:
    """City of Ottawa ward polygons (together the city), fetched once and cached."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    params = {"f": "json", "where": "1=1", "outFields": "OBJECTID", "returnGeometry": "true",
              "outSR": 4326, "maxAllowableOffset": BOUNDARY_SIMPLIFY_DEG, "geometryPrecision": 5}
    try:
        resp = requests.get(WARDS_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise DataMissingError(f"Could not fetch the Ottawa boundary: {exc}") from exc
    if "error" in payload or not payload.get("features"):
        raise DataMissingError(f"Boundary service returned no polygons: {payload.get('error')}")
    polygons = [f["geometry"]["rings"] for f in payload["features"] if f.get("geometry")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(polygons), encoding="utf-8")
    return polygons


def in_ottawa(lat: float, lon: float, polygons: list) -> bool:
    return any(point_in_rings(lon, lat, rings) for rings in polygons)


# --- Source tables ---------------------------------------------------------
def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None   # "NR", "NA", blanks


def _in_bbox(lat: float, lon: float) -> bool:
    return BBOX["lat"][0] <= lat <= BBOX["lat"][1] and BBOX["lon"][0] <= lon <= BBOX["lon"][1]


def latest_water_taking_file(directory: Path = WATER_TAKING_DIR) -> tuple[int, Path]:
    files = {int(m.group(1)): p for p in directory.glob("*_Annual_Water_Taking.xlsx")
             if (m := re.match(r"(\d{4})_", p.name))}
    if not files:
        raise DataMissingError(f"No *_Annual_Water_Taking.xlsx files in {directory}")
    year = max(files)
    return year, files[year]


def build_tables(permits_xlsx: Path = PERMITS_XLSX, water_taking_dir: Path = WATER_TAKING_DIR) -> dict:
    """Parse the spreadsheets into compact lists for the Ottawa area."""
    if not permits_xlsx.exists():
        raise DataMissingError(f"Permits file not found: {permits_xlsx}")
    permits = []
    for r in read_xlsx(permits_xlsx, PERMITS_DATA_SHEET):
        lat, lon = _num(r.get("LATITUDE")), _num(r.get("LONGITUDE"))
        if lat is None or lon is None or not _in_bbox(lat, lon) or r.get("ACTIVE") != "Yes":
            continue
        permits.append({"lat": lat, "lon": lon, "holder": (r.get("ClientName") or "").strip(),
                        "permit": r.get("PermitNo"), "source": (r.get("SourceID") or "").strip(),
                        "category": r.get("PurposeCat") or "", "purpose": r.get("SPurpose") or "",
                        "water": r.get("SurfGrnd") or "", "max_l_day": _num(r.get("MaxL_Day"))})
    year, wt_path = latest_water_taking_file(water_taking_dir)
    reported, unreported = [], 0
    for r in read_xlsx(wt_path, 1):
        zone, e, n = _num(r.get("UTM Zone")), _num(r.get("Easting")), _num(r.get("Northing"))
        if None in (zone, e, n):
            continue
        lat, lon = utm_to_latlon(int(zone), e, n)
        if not _in_bbox(lat, lon):
            continue
        litres = _num(r.get("Source Taking Amount (Litres)"))
        if litres is None:
            unreported += 1
        reported.append({"lat": lat, "lon": lon, "permit": r.get("Permit Number"),
                         "name": r.get("Source Name") or "", "category": r.get("Purpose Category") or "",
                         "purpose": r.get("Specific Purpose") or "", "water": r.get("Surface or Ground Water") or "",
                         "litres_year": litres})
    return {"data_year": year, "permits": permits, "reported": reported, "unreported_rows": unreported}


def load_tables(rebuild: bool = False, cache_path: Path = CACHE_PATH) -> dict:
    if not rebuild and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    tables = build_tables()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(tables), encoding="utf-8")
    return tables


# --- Classification and demand ---------------------------------------------
def is_supply(category: str, purpose: str) -> bool:
    """True if the taking counts as supply demand (not temporary / returned to the source)."""
    return (category.strip().lower() not in EXCLUDED_PURPOSE_CATEGORIES
            and purpose.strip().lower() not in EXCLUDED_SPECIFIC_PURPOSES)


def uses_groundwater(water: str) -> bool:
    return "ground" in water.lower()


def uses_surface_water(water: str) -> bool:
    return "surface" in water.lower()


def groundwater_demand(lat: float, lon: float, radius_m: float, tables: dict) -> dict:
    """Supply-type groundwater demand within ``radius_m``: reported (actual) and permitted."""
    reported_l_year, n_reported, n_unreported = 0.0, 0, 0
    for s in tables["reported"]:
        if not (uses_groundwater(s["water"]) and is_supply(s["category"], s["purpose"])):
            continue
        if distance_m(lat, lon, s["lat"], s["lon"]) > radius_m:
            continue
        if s["litres_year"] is None:
            n_unreported += 1
        else:
            reported_l_year += s["litres_year"]
            n_reported += 1
    permitted, n_permits = 0.0, 0
    for p in tables["permits"]:
        if (uses_groundwater(p["water"]) and is_supply(p["category"], p["purpose"])
                and p["max_l_day"] is not None
                and distance_m(lat, lon, p["lat"], p["lon"]) <= radius_m):
            permitted += p["max_l_day"]
            n_permits += 1
    return {"reported_l_per_day": round(reported_l_year / 365.0),
            "sources_reporting": n_reported, "sources_not_reported": n_unreported,
            "permitted_max_l_per_day": round(permitted), "active_permit_sources": n_permits}


def city_lattice(polygons: list, spacing_m: float = CITY_LATTICE_SPACING_M) -> list[tuple[float, float]]:
    """Regular lattice of (lat, lon) points inside the Ottawa boundary."""
    dlat = spacing_m / 111_320.0
    pts = []
    lat = BBOX["lat"][0]
    while lat <= BBOX["lat"][1]:
        dlon = spacing_m / (111_320.0 * math.cos(math.radians(lat)))
        lon = BBOX["lon"][0]
        while lon <= BBOX["lon"][1]:
            if in_ottawa(lat, lon, polygons):
                pts.append((lat, lon))
            lon += dlon
        lat += dlat
    return pts


def percentile_of(value: float, values: list[float]) -> float:
    """Share (0-100) of reference points with strictly less demand than ``value``.

    Strict (not mid-rank) because most of Ottawa has zero recorded demand: a site with zero
    must rank at the bottom, not in the middle of a large tie.
    """
    return 100.0 * bisect_left(sorted(values), value) / len(values)


def band_for(pct: float) -> str:
    if pct < BAND_LOW_MAX_PCT:
        return "low"
    return "moderate" if pct < BAND_MODERATE_MAX_PCT else "high"


def nearby_surface_takings(lat: float, lon: float, tables: dict, radius_m: float = CONTEXT_RADIUS_M,
                           top_n: int = CONTEXT_TOP_N) -> list[dict]:
    """Largest active surface-water permits near the site (context only).

    Temporary construction/dewatering/remediation takings are left out entirely; other
    non-supply purposes such as cooling water are shown and flagged.
    """
    out = []
    for p in tables["permits"]:
        d = distance_m(lat, lon, p["lat"], p["lon"])
        if (uses_surface_water(p["water"]) and p["max_l_day"] and d <= radius_m
                and p["category"].strip().lower() not in EXCLUDED_PURPOSE_CATEGORIES):
            out.append({"holder": p["holder"], "purpose": p["purpose"], "source": p["source"],
                        "distance_km": round(d / 1000, 1), "permitted_max_l_per_day": round(p["max_l_day"]),
                        "counted_as_supply_demand": is_supply(p["category"], p["purpose"])})
    return sorted(out, key=lambda x: -x["permitted_max_l_per_day"])[:top_n]


def assess_site(lat: float, lon: float, radii_m=DEFAULT_RADII_M, rebuild: bool = False) -> dict:
    polygons = load_boundary()
    if not in_ottawa(lat, lon, polygons):
        raise OutsideOttawaError(f"({lat}, {lon}) is not inside the City of Ottawa boundary.")
    tables = load_tables(rebuild)
    lattice = city_lattice(polygons)
    result = {"lat": lat, "lon": lon, "data_year": tables["data_year"], "groundwater_supply_demand": {}}
    for radius in radii_m:
        site = groundwater_demand(lat, lon, radius, tables)
        city_reported = [groundwater_demand(la, lo, radius, tables)["reported_l_per_day"] for la, lo in lattice]
        pct = percentile_of(site["reported_l_per_day"], city_reported)
        site.update({"percentile_in_ottawa": round(pct, 1), "band": band_for(pct),
                     "city_median_l_per_day": round(statistics.median(city_reported)),
                     "city_points_with_zero": sum(v == 0 for v in city_reported),
                     "city_points": len(city_reported)})
        result["groundwater_supply_demand"][f"{radius:g}m"] = site
    result["nearby_surface_water_takings"] = nearby_surface_takings(lat, lon, tables)
    result["notes"] = NOTES
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Existing water demand around an Ottawa site (PTTW data).")
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--radius", type=float, action="append", help="radius in metres (repeatable)")
    p.add_argument("--rebuild-cache", action="store_true", help="re-read the spreadsheets")
    args = p.parse_args(argv)
    try:
        print(json.dumps(assess_site(args.lat, args.lon, args.radius or DEFAULT_RADII_M,
                                     args.rebuild_cache), indent=2))
    except PTTWError as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

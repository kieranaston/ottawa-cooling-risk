"""Distance from a site to the nearest residential zone, for a noise-gate check.

Data: City of Ottawa ``Zoning/MapServer`` layer 3, "Zoning By-law 2008-250 Consolidation"
(polygons, Web Mercator). NOTE: By-law 2008-250 has been replaced by Zoning By-law 2026-50
(enacted 2026-03-11, mostly in force); the City publishes the new zoning as a separate
service (``Zoning_Bylaw_2026_50``) with different zone codes. Point SERVICE_URL, LAYER_ID,
ZONE_FIELD and RESIDENTIAL_ZONES at it to switch.

Method: query the residential polygons within the search radius of the point, convert their
vertices to a local metric plane centred on the site, and take the shortest point-to-edge
distance (0 if the site lies inside a residential polygon).

Thresholds come from Ontario guideline D-6 (Compatibility between Industrial Facilities): for
the chosen facility class, the recommended minimum separation distance and the potential
influence area. The class is a judgement about the facility, not something this module can
determine; the default is the most conservative (Class III).

Usage:  python -m src.zoning 45.35 -75.91 [--facility-class III] [--radius 1000]
"""

from __future__ import annotations

import argparse
import json
import math
import re
from typing import Iterable, Optional

import requests

# --- Service ---------------------------------------------------------------
SERVICE_URL = "https://maps.ottawa.ca/arcgis/rest/services/Zoning/MapServer"
LAYER_ID = 3
BYLAW = "2008-250"
# PARENTZONE holds the base zone code (R1, RR, ...) on every row. ZONE_MAIN does not: 50 rows
# have ZONE_MAIN = "*_" (zone code under a pending amendment) with the base zone in PARENTZONE.
ZONE_FIELD = "PARENTZONE"
ZONE_CODE_FIELD = "ZONE_CODE"          # full code incl. subzone, e.g. "R1Y[746]"
# Matching object IDs are fetched first, then geometries in chunks of this many IDs. Offset
# paging (resultOffset) is not used: on this layer it returns overlapping pages (4,617 rows
# for 4,019 polygons in one test) because the service does not honour a stable sort order.
ID_CHUNK_SIZE = 200
REQUEST_TIMEOUT_S = 60
# Layer extent in EPSG:3857 (from layer metadata): xmin, ymin, xmax, ymax.
LAYER_EXTENT = (-8499865.2014, 5608596.669, -8376410.8608, 5707482.1309)

# --- Residential zones (conservative: every zone that permits ordinary homes) ---
# Grouped so the output can say what kind of residential zone is nearest.
ZONE_GROUPS = {
    "urban residential": ("R1", "R2", "R3", "R4", "R5", "RM"),   # By-law Part 6
    "village / rural residential": ("RR", "V1", "V2", "V3"),
    "rural countryside": ("RU",),   # whole farm lots: a house may not exist, but one is permitted
}
RESIDENTIAL_ZONES = tuple(code for codes in ZONE_GROUPS.values() for code in codes)
# Not counted although they permit dwellings: mixed-use and mainstreet zones (AM, TM, GM, MC, MD,
# TD, LC, VM), institutional (I1, I2) and AG. Add codes here to include them.
RU_NOTE = ("Nearest zone is rural countryside (RU): homes are permitted but the lot may have no "
           "house. Counted anyway (conservative).")

# --- Thresholds: Ontario guideline D-6 -------------------------------------------
# (recommended minimum separation distance m, potential influence area m) by facility class.
# D-6 says no incompatible development should occur inside the minimum separation distance
# even with mitigation, and adverse effects may be felt inside the influence area. The class
# of a data centre is a judgement: D-6 does not list data centres. Class I (daytime only,
# sound not audible off site) cannot fit a 24/7 facility with outdoor plant; Class III is the
# conservative default.
D6_DISTANCES_M = {"I": (20.0, 70.0), "II": (70.0, 300.0), "III": (300.0, 1000.0)}
DEFAULT_FACILITY_CLASS = "III"
FLAG_NEAR, FLAG_CAUTION, FLAG_CLEAR = "within_min_separation", "within_influence_area", "clear"
# What each flag means for the cooling choice. One fixed statement per flag.
FLAG_STATEMENTS = {   # {cls}, {near} and {caution} are filled in
    FLAG_NEAR: "Residential zoning is within {near:g} m, the D-6 Class {cls} minimum separation "
               "distance: the guideline treats this as incompatible even with mitigation. Noise "
               "should drive the cooling choice; favour enclosed or low-noise cooling.",
    FLAG_CAUTION: "Residential zoning is within {caution:g} m, the D-6 Class {cls} potential "
                  "influence area: adverse noise effects may occur. Include noise in the cooling "
                  "design and check outdoor chillers and fans against noise limits (NPC-300).",
    FLAG_CLEAR: "No residential zoning within {caution:g} m (D-6 Class {cls} influence area): "
                "noise is unlikely to limit the cooling choice (local noise limits still apply).",
}


def thresholds_for(facility_class: str) -> tuple[float, float]:
    try:
        return D6_DISTANCES_M[facility_class.upper()]
    except KeyError:
        raise ValueError(f"facility_class must be one of {sorted(D6_DISTANCES_M)}, got {facility_class!r}")


def statement_for(flag: str, facility_class: str = DEFAULT_FACILITY_CLASS) -> str:
    near, caution = thresholds_for(facility_class)
    return FLAG_STATEMENTS[flag].format(cls=facility_class.upper(), near=near, caution=caution)


def zone_group(family: Optional[str]) -> Optional[str]:
    for group, codes in ZONE_GROUPS.items():
        if family in codes:
            return group
    return None


_MERCATOR_R = 6378137.0
_WGS84_E2 = 0.00669438
_ZONE_CODE_RE = re.compile(r"^[A-Z0-9]{1,4}$")


class ZoningError(Exception):
    """Base class for errors raised by this module."""


class OutOfExtentError(ZoningError):
    """The point lies outside the zoning layer's extent."""


class ServiceError(ZoningError):
    """HTTP or ArcGIS-level failure when talking to the map service."""


class NoZoningDataError(ZoningError):
    """No zoning polygons of any kind near the point (outside the City's zoning coverage)."""


# --- Projection ------------------------------------------------------------
def to_web_mercator(lat: float, lon: float) -> tuple[float, float]:
    if not (-90 < lat < 90 and -180 <= lon <= 180):
        raise ValueError(f"Invalid WGS84 coordinates: lat={lat}, lon={lon}")
    return (_MERCATOR_R * math.radians(lon),
            _MERCATOR_R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def from_web_mercator(x: float, y: float) -> tuple[float, float]:
    """Web Mercator metres to (lat, lon) degrees."""
    return (math.degrees(2 * math.atan(math.exp(y / _MERCATOR_R)) - math.pi / 2),
            math.degrees(x / _MERCATOR_R))


def local_xy(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Metres east/north of (lat0, lon0) on the local tangent plane (WGS84 curvature radii).

    Web Mercator metres are stretched by 1/cos(lat) (~1.4x in Ottawa), so distances are
    never measured there. Over the few km used here this plane is accurate to well under 1 m.
    """
    s = math.sin(math.radians(lat0))
    w = math.sqrt(1 - _WGS84_E2 * s * s)
    meridian = 6378137.0 * (1 - _WGS84_E2) / w ** 3
    prime_vertical = 6378137.0 / w
    return (math.radians(lon - lon0) * prime_vertical * math.cos(math.radians(lat0)),
            math.radians(lat - lat0) * meridian)


def point_segment_distance(ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from the origin to the segment (ax, ay)-(bx, by)."""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
    return math.hypot(ax + t * dx, ay + t * dy)


def _origin_in_rings(rings: list) -> bool:
    """Even-odd test: is the origin inside this polygon (outer ring minus holes)?"""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            (xi, yi), (xj, yj) = ring[i], ring[j]
            if (yi > 0) != (yj > 0) and 0 < (xj - xi) * (0 - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def polygon_distance_m(lat: float, lon: float, mercator_rings: list) -> float:
    """Shortest distance in metres from the site to a polygon (0 if the site is inside it)."""
    rings = [[local_xy(lat, lon, *from_web_mercator(x, y)) for x, y in ring] for ring in mercator_rings]
    if _origin_in_rings(rings):
        return 0.0
    return min(point_segment_distance(*ring[i], *ring[(i + 1) % len(ring)])
               for ring in rings for i in range(len(ring)))


# --- Service access --------------------------------------------------------
def _query(params: dict, session: Optional[requests.Session], url: str) -> dict:
    """POST a query (GET URLs overflow when many object IDs are listed)."""
    poster = (session or requests).post
    try:
        resp = poster(f"{url}/{LAYER_ID}/query", data=params, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise ServiceError(f"Request to {url}/{LAYER_ID}/query failed: {exc}") from exc
    except ValueError as exc:
        raise ServiceError("Service returned a non-JSON response") from exc
    if "error" in payload:
        err = payload["error"]
        raise ServiceError(f"ArcGIS error {err.get('code')}: {err.get('message')}")
    return payload


def _spatial_params(lat: float, lon: float, radius_m: float) -> dict:
    return {"f": "json", "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "distance": radius_m, "units": "esriSRUnit_Meter"}


def where_clause(zones: Iterable[str]) -> str:
    zones = list(zones)
    if not zones or not all(_ZONE_CODE_RE.match(z) for z in zones):
        raise ValueError(f"Invalid zone codes: {zones}")
    return f"{ZONE_FIELD} IN ({','.join(repr(z) for z in zones)})"


def fetch_zone_polygons(lat: float, lon: float, radius_m: float, zones: Iterable[str],
                        session: Optional[requests.Session] = None,
                        url: str = SERVICE_URL) -> list[dict]:
    """All polygons of the given zone families within ``radius_m``.

    Two steps: the matching object IDs (no limit on count), then the geometries by ID in
    chunks. If the service returns fewer features than IDs asked for (transfer limit), the
    chunk is split and retried, so the result is complete or an error, never silently short.
    """
    id_params = {**_spatial_params(lat, lon, radius_m), "where": where_clause(zones),
                 "returnIdsOnly": "true"}
    ids = _query(id_params, session, url).get("objectIds") or []

    def fetch(chunk: list) -> list[dict]:
        payload = _query({"f": "json", "objectIds": ",".join(str(i) for i in chunk), "outSR": 3857,
                        "outFields": f"{ZONE_CODE_FIELD},{ZONE_FIELD}", "returnGeometry": "true"},
                       session, url)
        found = [{"zone_code": f.get("attributes", {}).get(ZONE_CODE_FIELD),
                  "zone_family": f.get("attributes", {}).get(ZONE_FIELD),
                  "rings": f["geometry"]["rings"]}
                 for f in payload.get("features", []) if (f.get("geometry") or {}).get("rings")]
        if len(found) < len(chunk):
            if len(chunk) == 1:
                raise ServiceError(f"Service returned no geometry for object {chunk[0]}.")
            half = len(chunk) // 2
            return fetch(chunk[:half]) + fetch(chunk[half:])
        return found

    polygons = []
    for i in range(0, len(ids), ID_CHUNK_SIZE):
        polygons += fetch(ids[i:i + ID_CHUNK_SIZE])
    return polygons


def any_zoning_nearby(lat: float, lon: float, radius_m: float,
                      session: Optional[requests.Session] = None, url: str = SERVICE_URL) -> bool:
    """True if any zoning polygon (any zone) lies within the radius, i.e. the area is covered."""
    payload = _query({**_spatial_params(lat, lon, radius_m), "where": "1=1", "returnCountOnly": "true"},
                   session, url)
    return payload.get("count", 0) > 0


# --- Assessment ------------------------------------------------------------
def flag_for(distance_m: Optional[float], near_m: float, caution_m: float) -> str:
    if distance_m is None:
        return FLAG_CLEAR
    if distance_m <= near_m:
        return FLAG_NEAR
    return FLAG_CAUTION if distance_m <= caution_m else FLAG_CLEAR


def nearest_residential(lat: float, lon: float, facility_class: str = DEFAULT_FACILITY_CLASS,
                        radius_m: Optional[float] = None, zones: Iterable[str] = RESIDENTIAL_ZONES,
                        near_m: Optional[float] = None, caution_m: Optional[float] = None,
                        session: Optional[requests.Session] = None) -> dict:
    """Distance to the nearest residential zone and a noise-gate flag.

    The thresholds are the D-6 minimum separation distance and influence area for
    ``facility_class`` (override with ``near_m`` / ``caution_m``). The search radius defaults
    to the influence area and may not be smaller, so "clear" always means nothing residential
    within the influence area.

    Raises OutOfExtentError (no network call) for points outside the layer extent, and
    NoZoningDataError if there is no zoning at all near the point (so "none within radius"
    always means the area is mapped and has no residential zone in range).
    """
    default_near, default_caution = thresholds_for(facility_class)
    near_m = default_near if near_m is None else near_m
    caution_m = default_caution if caution_m is None else caution_m
    radius_m = caution_m if radius_m is None else radius_m
    if radius_m < caution_m:
        raise ValueError(f"search radius ({radius_m:g} m) must cover the influence area ({caution_m:g} m)")
    x, y = to_web_mercator(lat, lon)
    if not (LAYER_EXTENT[0] <= x <= LAYER_EXTENT[2] and LAYER_EXTENT[1] <= y <= LAYER_EXTENT[3]):
        raise OutOfExtentError(f"({lat}, {lon}) is outside the Ottawa zoning layer extent.")
    zones = tuple(zones)
    polygons = fetch_zone_polygons(lat, lon, radius_m, zones, session)
    base = {"lat": lat, "lon": lon, "bylaw": BYLAW, "facility_class": facility_class.upper(),
            "minimum_separation_m": near_m, "influence_area_m": caution_m, "search_radius_m": radius_m,
            "zones_considered": list(zones), "residential_polygons_found": len(polygons)}
    if not polygons:
        if not any_zoning_nearby(lat, lon, radius_m, session):
            raise NoZoningDataError(
                f"No zoning polygons within {radius_m:g} m of ({lat}, {lon}); "
                "the point is probably outside the City of Ottawa's zoning coverage.")
        return {**base, "distance_m": None, "nearest_zone_code": None, "nearest_zone_family": None,
                "nearest_zone_group": None, "within_min_separation": False,
                "within_influence_area": False, "flag": FLAG_CLEAR,
                "statement": statement_for(FLAG_CLEAR, facility_class),
                "message": f"none within radius ({radius_m:g} m)"}
    distance, poly = min(((polygon_distance_m(lat, lon, p["rings"]), p) for p in polygons),
                         key=lambda t: t[0])
    flag = flag_for(distance, near_m, caution_m)
    group = zone_group(poly["zone_family"])
    result = {**base, "distance_m": round(distance, 1), "nearest_zone_code": poly["zone_code"],
              "nearest_zone_family": poly["zone_family"], "nearest_zone_group": group,
              "within_min_separation": distance <= near_m, "within_influence_area": distance <= caution_m,
              "flag": flag, "statement": statement_for(flag, facility_class),
              "message": f"nearest residential zone {poly['zone_code']} at {distance:.0f} m"}
    if group == "rural countryside":
        result["note"] = RU_NOTE
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Distance to the nearest residential zone (Ottawa).")
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--facility-class", default=DEFAULT_FACILITY_CLASS, choices=sorted(D6_DISTANCES_M),
                   help="D-6 industrial facility class (sets the distances); default is the most conservative")
    p.add_argument("--radius", type=float, help="search radius in metres (default: the influence area)")
    args = p.parse_args(argv)
    try:
        print(json.dumps(nearest_residential(args.lat, args.lon, args.facility_class, args.radius), indent=2))
    except (ZoningError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Urban-heat-island (UHI) stress score for a site in Ottawa.

Data: City of Ottawa Climate_Resiliency MapServer, layer 2 ("Urban Heat Island
2023"), a Landsat-derived surface-temperature raster (deg C, snapshot of
2023-07-05). The raster is queried through the ArcGIS ``identify`` endpoint.

Usage:  python -m src.uhi 45.35 -75.91 [--radius 500]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

import requests

# --- Service ---------------------------------------------------------------
SERVICE_URL = "https://maps.ottawa.ca/arcgis/rest/services/Climate_Resiliency/MapServer"
LAYER_ID = 2                    # default layer: latest (2023)
REQUEST_TIMEOUT_S = 30
# Layer extents in EPSG:3857 (from layer metadata): xmin, ymin, xmax, ymax.
_EXTENT_2021_23 = (-8501693.68, 5621163.73, -8374163.68, 5709723.73)
LAYERS = {
    0: {"year": 2019, "extent": (-8501853.53, 5611297.47, -8372643.53, 5710897.47)},
    1: {"year": 2021, "extent": _EXTENT_2021_23},
    2: {"year": 2023, "extent": _EXTENT_2021_23},
}
SERVICE_EXTENT = LAYERS[LAYER_ID]["extent"]

# --- Sampling --------------------------------------------------------------
DEFAULT_RADIUS_M = 500.0
BUFFER_GRID_SIZE = 5            # NxN lattice over the buffer's bounding square
CITY_GRID_SIZE = 30             # NxN lattice over the service extent
CITY_GRID_WORKERS = 8
CACHE_DIR = Path(__file__).resolve().parent.parent / "data"

def cache_path_for(layer_id: int) -> Path:
    return CACHE_DIR / f"uhi_city_grid_layer{layer_id}.json"


# --- Banding ---------------------------------------------------------------
# Percentile cut points (0-100): below LOW_MAX = low, below MODERATE_MAX = moderate,
# otherwise high (top third of the city for that year).
BAND_LOW_MAX_PCT = 100 / 3
BAND_MODERATE_MAX_PCT = 200 / 3

_EARTH_R = 6378137.0


class UHIError(Exception):
    """Base class for errors raised by this module."""


class OutOfExtentError(UHIError):
    """The requested point lies outside the service's raster extent."""


class NoDataError(UHIError):
    """No valid pixel values were found (water, unmapped area, etc.)."""


class ServiceError(UHIError):
    """HTTP or ArcGIS-level failure when talking to the map service."""


# --- Projection ------------------------------------------------------------
def to_web_mercator(lat: float, lon: float) -> tuple[float, float]:
    if not (-90 < lat < 90 and -180 <= lon <= 180):
        raise ValueError(f"Invalid WGS84 coordinates: lat={lat}, lon={lon}")
    x = _EARTH_R * math.radians(lon)
    y = _EARTH_R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def in_extent(x: float, y: float, extent=SERVICE_EXTENT) -> bool:
    return extent[0] <= x <= extent[2] and extent[1] <= y <= extent[3]


# --- Service access --------------------------------------------------------
def identify_pixel(x: float, y: float, session: Optional[requests.Session] = None,
                   url: str = SERVICE_URL, layer_id: int = LAYER_ID) -> Optional[float]:
    """Return the pixel value at a Web Mercator point, or None for NoData / outside raster."""
    params = {
        "f": "json",
        "geometry": f"{x},{y}",
        "geometryType": "esriGeometryPoint",
        "sr": 3857,
        "layers": f"all:{layer_id}",
        "tolerance": 1,
        "mapExtent": f"{x - 100},{y - 100},{x + 100},{y + 100}",
        "imageDisplay": "400,400,96",
        "returnGeometry": "false",
    }
    getter = (session or requests).get
    try:
        resp = getter(f"{url}/identify", params=params, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise ServiceError(f"Request to {url}/identify failed: {exc}") from exc
    except ValueError as exc:
        raise ServiceError("Service returned a non-JSON response") from exc

    if "error" in payload:  # ArcGIS reports many failures as HTTP 200 + error body
        err = payload["error"]
        raise ServiceError(f"ArcGIS error {err.get('code')}: {err.get('message')}")

    for result in payload.get("results", []):
        raw = result.get("attributes", {}).get("Pixel Value")
        try:
            value = float(raw)
        except (TypeError, ValueError):  # "NoData" or missing
            continue
        if math.isfinite(value):
            return value
    return None


# --- Buffer mean -----------------------------------------------------------
def buffer_points(x: float, y: float, radius_m: float, grid_size: int,
                  lat: float) -> list[tuple[float, float]]:
    """Grid points (Web Mercator) that fall inside the circular buffer.

    Web Mercator metres are stretched by 1/cos(lat), so the ground radius is
    scaled up to get the correct footprint.
    """
    if radius_m <= 0 or grid_size < 1:
        raise ValueError("radius_m must be > 0 and grid_size >= 1")
    r = radius_m / math.cos(math.radians(lat))
    if grid_size == 1:
        return [(x, y)]
    step = 2 * r / (grid_size - 1)
    pts = []
    for i in range(grid_size):
        for j in range(grid_size):
            dx, dy = -r + i * step, -r + j * step
            if dx * dx + dy * dy <= r * r * (1 + 1e-9):
                pts.append((x + dx, y + dy))
    return pts


def buffer_mean(lat: float, lon: float, radius_m: float = DEFAULT_RADIUS_M,
                grid_size: int = BUFFER_GRID_SIZE,
                session: Optional[requests.Session] = None,
                layer_id: int = LAYER_ID) -> tuple[float, int, int]:
    """Mean valid pixel value in the buffer. Returns (mean, n_valid, n_sampled)."""
    x, y = to_web_mercator(lat, lon)
    if not in_extent(x, y, LAYERS[layer_id]["extent"]):
        raise OutOfExtentError(
            f"({lat}, {lon}) is outside the Ottawa UHI raster extent; "
            "this tool only covers the City of Ottawa.")
    pts = buffer_points(x, y, radius_m, grid_size, lat)
    values = [v for v in (identify_pixel(px, py, session, layer_id=layer_id) for px, py in pts)
              if v is not None]
    if not values:
        raise NoDataError(
            f"No valid UHI pixels within {radius_m:g} m of ({lat}, {lon}) "
            "(e.g. open water or unmapped area).")
    return sum(values) / len(values), len(values), len(pts)


# --- Citywide distribution -------------------------------------------------
def sample_city_grid(grid_size: int = CITY_GRID_SIZE, workers: int = CITY_GRID_WORKERS,
                     layer_id: int = LAYER_ID) -> list[float]:
    """Sample a layer's whole extent on a coarse lattice; NoData is dropped."""
    xmin, ymin, xmax, ymax = LAYERS[layer_id]["extent"]
    xs = [xmin + (xmax - xmin) * (i + 0.5) / grid_size for i in range(grid_size)]
    ys = [ymin + (ymax - ymin) * (j + 0.5) / grid_size for j in range(grid_size)]
    pts = [(px, py) for px in xs for py in ys]
    with requests.Session() as session, ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda p: identify_pixel(p[0], p[1], session, layer_id=layer_id), pts)
        return sorted(v for v in results if v is not None)


def load_city_distribution(cache_path: Optional[Path] = None, grid_size: int = CITY_GRID_SIZE,
                           refresh: bool = False, layer_id: int = LAYER_ID) -> list[float]:
    """Return the sorted citywide values for a layer, sampling once and caching to JSON."""
    cache_path = cache_path or cache_path_for(layer_id)
    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("grid_size") == grid_size and cached.get("values"):
                return cached["values"]
        except (OSError, ValueError):
            pass  # corrupt cache: resample
    values = sample_city_grid(grid_size, layer_id=layer_id)
    if not values:
        raise NoDataError(f"Citywide sampling of layer {layer_id} returned no valid pixels.")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"layer": layer_id, "grid_size": grid_size, "values": values}),
        encoding="utf-8")
    return values


# --- Assessment ------------------------------------------------------------
def percentile_of(value: float, sorted_values: Iterable[float]) -> float:
    """Mid-rank percentile (0-100) of ``value`` within ``sorted_values``."""
    vals = list(sorted_values)
    if not vals:
        raise ValueError("empty distribution")
    below, upto = bisect_left(vals, value), bisect_right(vals, value)
    return 100.0 * (below + upto) / 2 / len(vals)


def band_for(pct: float, low_max: float = BAND_LOW_MAX_PCT,
             moderate_max: float = BAND_MODERATE_MAX_PCT) -> str:
    if pct < low_max:
        return "low"
    return "moderate" if pct < moderate_max else "high"


def year_assessment(lat: float, lon: float, radius_m: float = DEFAULT_RADIUS_M,
                    grid_size: int = BUFFER_GRID_SIZE, refresh_cache: bool = False,
                    cache_path: Optional[Path] = None, layer_id: int = LAYER_ID) -> dict:
    """Where the site's buffer sits within one year's citywide surface-temperature map."""
    mean, n_valid, n_sampled = buffer_mean(lat, lon, radius_m, grid_size, layer_id=layer_id)
    dist = load_city_distribution(cache_path, refresh=refresh_cache, layer_id=layer_id)
    pct = percentile_of(mean, dist)
    city_median = statistics.median(dist)
    return {
        "year": LAYERS[layer_id]["year"],
        "buffer_mean_c": round(mean, 2),
        "city_median_c": round(city_median, 2),
        "above_city_median_c": round(mean - city_median, 2),
        "percentile": round(pct, 1),
        "band": band_for(pct),
        "samples_valid": n_valid,
        "samples_total": n_sampled,
    }


def assess_site(lat: float, lon: float, radius_m: float = DEFAULT_RADIUS_M,
                grid_size: int = BUFFER_GRID_SIZE, refresh_cache: bool = False) -> dict:
    """Assess the site in every available year and check whether the result persists.

    ``assessment``:
      persistent_hotspot - top third of the city in every year checked (needs >= 2 years)
      intermittent       - top third in some years only (or only one year was available)
      not_hotspot        - never in the top third
    A year with no data at the site (NoData, or outside that layer's extent) is skipped
    and listed under ``skipped``.
    """
    years, skipped = {}, {}
    for layer_id, info in LAYERS.items():
        try:
            years[info["year"]] = year_assessment(lat, lon, radius_m, grid_size, refresh_cache,
                                                  layer_id=layer_id)
        except (OutOfExtentError, NoDataError) as exc:
            skipped[info["year"]] = str(exc)
    if not years:
        raise NoDataError(f"No year has valid UHI data for ({lat}, {lon}): {skipped}")
    n, n_high = len(years), sum(r["band"] == "high" for r in years.values())
    if n_high == 0:
        label = "not_hotspot"
    elif n_high == n and n >= 2:
        label = "persistent_hotspot"
    else:
        label = "intermittent"
    return {
        "lat": lat,
        "lon": lon,
        "radius_m": radius_m,
        "assessment": label,
        "years_in_top_third": f"{n_high}/{n}",
        "years": years,
        "skipped": skipped,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Heat-island assessment for an Ottawa site.")
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M, help="buffer radius in metres")
    p.add_argument("--grid", type=int, default=BUFFER_GRID_SIZE, help="NxN sample lattice in the buffer")
    p.add_argument("--refresh-cache", action="store_true", help="resample the citywide grids")
    p.add_argument("--layer", type=int, choices=sorted(LAYERS),
                   help="assess one year only (0=2019, 1=2021, 2=2023); default is all years")
    args = p.parse_args(argv)
    try:
        if args.layer is None:
            result = assess_site(args.lat, args.lon, args.radius, args.grid, args.refresh_cache)
        else:
            result = year_assessment(args.lat, args.lon, args.radius, args.grid, args.refresh_cache,
                                     layer_id=args.layer)
        print(json.dumps(result, indent=2))
    except (UHIError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

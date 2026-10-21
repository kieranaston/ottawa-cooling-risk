"""Ottawa free-cooling baseline from the Ottawa Intl Airport typical year.

Source: NRCan CWEC 2023 hourly typical year for OTTAWA INTL A (Climate ID 6106001).
The file is downloaded once, checked against a pinned SHA-256 and cached in ``data/``.
Dry-bulb is read from the file; wet-bulb is calculated per hour from dry-bulb, humidity
and pressure (ASHRAE psychrometric equations). Results are plain counts of those hours.

Usage:  python -m src.freecooling [--cutoff 18] [--wb-cutoff 10] [--stress 27]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import requests

EPW_URL = ("https://ftp.maps.canada.ca/pub/nrcan_rncan/Solar-energy_Energie-solaire/"
           "CWEC_2023/ON/6106001/CAN_ON_OTTAWA-INTL-A_6106001_CWEC.epw")
EPW_SHA256 = "5565397a8343528cd28a5ac5f575c1024107c4e4c72e76966436cf372c6ea58b"
EPW_CACHE = Path(__file__).resolve().parent.parent / "data" / "CAN_ON_OTTAWA-INTL-A_6106001_CWEC.epw"
REQUEST_TIMEOUT_S = 90

# Thresholds, deg C. A free-cooling hour has outdoor temperature <= cutoff.
DEFAULT_CUTOFFS_C = (18.0, 24.0)       # dry-bulb: air-side free cooling
DEFAULT_WB_CUTOFFS_C = (10.0, 15.0)    # wet-bulb: water-side / evaporative free cooling
DEFAULT_STRESS_C = (27.0, 30.0)        # dry-bulb heat-stress hours

EPW_HEADER_LINES = 8
EPW_HOURS = 8760
RH_MAX_VALID_PCT = 105.0   # a few hours read 101-104% RH; treat as saturated. EPW missing = 999.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class WeatherDataError(Exception):
    """The weather file could not be fetched, verified or parsed."""


# --- Wet-bulb (ASHRAE Fundamentals, SI) ------------------------------------
def saturation_pressure_pa(t_c: float) -> float:
    """Saturation vapour pressure over water (t >= 0) or ice (t < 0), Pa."""
    t = t_c + 273.15
    if t_c >= 0:
        ln_p = (-5.8002206e3 / t + 1.3914993 - 4.8640239e-2 * t + 4.1764768e-5 * t ** 2
                - 1.4452093e-8 * t ** 3 + 6.5459673 * math.log(t))
    else:
        ln_p = (-5.6745359e3 / t + 6.3925247 - 9.677843e-3 * t + 6.2215701e-7 * t ** 2
                + 2.0747825e-9 * t ** 3 - 9.484024e-13 * t ** 4 + 4.1635019 * math.log(t))
    return math.exp(ln_p)


def wet_bulb_c(t_c: float, rh_pct: float, p_pa: float) -> float:
    """Thermodynamic wet-bulb temperature, solved by bisection."""
    pw = rh_pct / 100.0 * saturation_pressure_pa(t_c)
    w = 0.621945 * pw / (p_pa - pw)          # humidity ratio of the actual air

    def w_at(tw: float) -> float:            # humidity ratio implied by a trial wet-bulb
        pws = saturation_pressure_pa(tw)
        ws = 0.621945 * pws / (p_pa - pws)
        if tw >= 0:
            return ((2501 - 2.326 * tw) * ws - 1.006 * (t_c - tw)) / (2501 + 1.86 * t_c - 4.186 * tw)
        return ((2830 - 0.24 * tw) * ws - 1.006 * (t_c - tw)) / (2830 + 1.86 * t_c - 2.1 * tw)

    lo, hi = t_c - 60.0, t_c
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if w_at(mid) < w else (lo, mid)
    return (lo + hi) / 2


# --- Weather file ----------------------------------------------------------
def ensure_epw(path: Path = EPW_CACHE, url: str = EPW_URL, sha256: str | None = EPW_SHA256) -> Path:
    """Return the cached EPW, downloading and verifying it on first use."""
    if path.exists():
        return path
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise WeatherDataError(f"Could not download weather file from {url}: {exc}") from exc
    if sha256 and hashlib.sha256(resp.content).hexdigest() != sha256:
        raise WeatherDataError("Downloaded weather file fails the pinned checksum; refusing to use it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return path


def read_epw(path: Path) -> list[tuple[int, int, float, float]]:
    """Parse an EPW into one (month, day, dry_bulb_C, wet_bulb_C) tuple per hour."""
    hours = []
    try:
        with open(path, encoding="latin-1") as f:
            for n, line in enumerate(f, 1):
                if n <= EPW_HEADER_LINES or not line.strip():
                    continue
                p = line.split(",")
                db, rh, pressure = float(p[6]), float(p[8]), float(p[9])
                if not (0 <= rh <= RH_MAX_VALID_PCT and 30000 < pressure < 120000):
                    raise ValueError(f"missing or invalid humidity/pressure on line {n}")
                hours.append((int(p[1]), int(p[2]), db, wet_bulb_c(db, min(rh, 100.0), pressure)))
    except (OSError, ValueError, IndexError) as exc:
        raise WeatherDataError(f"Could not parse weather file {path}: {exc}") from exc
    if len(hours) != EPW_HOURS:
        raise WeatherDataError(f"Expected {EPW_HOURS} hourly records, got {len(hours)}.")
    return hours


# --- Summary ---------------------------------------------------------------
def _range(v: list[float]) -> dict:
    return {"min": round(min(v), 1), "mean": round(sum(v) / len(v), 1), "max": round(max(v), 1)}


def _free_cooling(temps: list[float], days: list[tuple], cutoffs: Iterable[float]) -> dict:
    """Hours at or below each cutoff, and days where every hour is."""
    daily_max = defaultdict(lambda: -1e9)
    for key, t in zip(days, temps):
        daily_max[key] = max(daily_max[key], t)
    return {f"{c:g}C": {"hours": sum(t <= c for t in temps),
                        "pct_of_year": round(100 * sum(t <= c for t in temps) / len(temps), 1),
                        "full_days": sum(m <= c for m in daily_max.values()),
                        "total_days": len(daily_max)}
            for c in cutoffs}


def summarize(hours: list[tuple[int, int, float, float]],
              cutoffs_c: Iterable[float] = DEFAULT_CUTOFFS_C,
              wb_cutoffs_c: Iterable[float] = DEFAULT_WB_CUTOFFS_C,
              stress_c: Iterable[float] = DEFAULT_STRESS_C) -> dict:
    db = [h[2] for h in hours]
    wb = [h[3] for h in hours]
    days = [(h[0], h[1]) for h in hours]
    monthly = defaultdict(list)
    for h in hours:
        monthly[h[0]].append(h[2])
    return {
        "source": "Ottawa Intl A (6106001), NRCan CWEC 2023 typical year",
        "hours": len(hours),
        "dry_bulb_c": {"annual": _range(db),
                       "monthly": {MONTHS[m - 1]: _range(v) for m, v in sorted(monthly.items())}},
        "wet_bulb_c": {"annual": _range(wb)},
        "free_cooling_dry_bulb": _free_cooling(db, days, cutoffs_c),
        "free_cooling_wet_bulb": _free_cooling(wb, days, wb_cutoffs_c),
        "stress_hours_dry_bulb": {f"above_{s:g}C": sum(t > s for t in db) for s in stress_c},
    }


def ottawa_baseline(cutoffs_c: Iterable[float] = DEFAULT_CUTOFFS_C,
                    wb_cutoffs_c: Iterable[float] = DEFAULT_WB_CUTOFFS_C,
                    stress_c: Iterable[float] = DEFAULT_STRESS_C,
                    epw_path: Path = EPW_CACHE) -> dict:
    return summarize(read_epw(ensure_epw(epw_path)), cutoffs_c, wb_cutoffs_c, stress_c)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ottawa free-cooling baseline (airport typical year).")
    p.add_argument("--cutoff", type=float, action="append", help="dry-bulb cutoff, C (repeatable)")
    p.add_argument("--wb-cutoff", type=float, action="append", help="wet-bulb cutoff, C (repeatable)")
    p.add_argument("--stress", type=float, action="append", help="dry-bulb stress threshold, C (repeatable)")
    args = p.parse_args(argv)
    try:
        result = ottawa_baseline(args.cutoff or DEFAULT_CUTOFFS_C,
                                 args.wb_cutoff or DEFAULT_WB_CUTOFFS_C,
                                 args.stress or DEFAULT_STRESS_C)
    except WeatherDataError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Poster figures for ottawa-cooling-risk (separate from src/: needs matplotlib).

Run from the project root:   python -m figures.make_figures [1] [2] [3]

  1  Site profile comparison (table / heat strip, 3 sites x 4 indicators)
  2  Monthly free-cooling hours (dry-bulb and wet-bulb, Ottawa airport typical year)
  3  Heat-island persistence map (years in the hottest third, 2019/2021/2023)

Output: figures/out/*.png at 300 dpi, 14 x 6 in, white background, all text >= 24 pt, with
Title / Source / Description written into each PNG's metadata for the captions.
Figures 1 and 3 query live services (a minute or so); the sites are constants below.
"""

from __future__ import annotations

import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from src import freecooling, pttw, uhi, zoning

OUT_DIR = Path(__file__).resolve().parent / "out"
FIG_W_IN, FIG_H_IN, DPI = 14.0, 6.0, 300
MIN_FONT_PT = 24

# Palette: teal and orange, validated for colour-blind separation on white (validate_palette.py).
TEAL, ORANGE = "#00958F", "#E8590C"
ORANGE_LIGHT = "#F8C9A8"        # middle band: light tint of the orange
INK, INK_MUTED, GRID, SURFACE = "#1F2937", "#4B5563", "#E5E7EB", "#FFFFFF"
# Heat-map persistence ramp, one hue (orange), light to dark: 0, 1, 2, 3 years in the hottest third.
PERSISTENCE_RAMP = ("#EEF0F2", "#F8C9A8", "#F08A4B", "#C2410C")

# Poster sites (name, lat, lon, label letter). Change these to the sites on the poster.
SITES = (
    ("Downtown core", 45.4250, -75.6930, "A"),
    ("Kanata North", 45.3500, -75.9100, "B"),
    ("Carp (rural)", 45.3490, -76.0390, "C"),
)
FACILITY_CLASS = "III"          # D-6 class used for the noise column (most conservative)
WATER_RADIUS_M = 5000.0
HEAT_MAP_SPACING_M = 1500.0

SOURCES = {
    1: ("Sources: NRCan CWEC 2023 typical year, Ottawa Intl A (free cooling); City of Ottawa Urban "
        "Heat Island 2019, 2021, 2023 (heat island); Ontario Permit To Take Water and Water Taking "
        "2022 (water demand, groundwater within 5 km); City of Ottawa Zoning By-law 2008-250 with "
        f"Ontario guideline D-6 Class {FACILITY_CLASS} distances (nearest homes)."),
    2: ("Source: NRCan CWEC 2023 typical year, Ottawa Intl A (6106001), 8,760 hours. Wet-bulb "
        "calculated from dry-bulb, humidity and pressure (ASHRAE psychrometric equations)."),
    3: ("Source: City of Ottawa Urban Heat Island 2019, 2021, 2023 (Climate_Resiliency MapServer "
        f"layers 0-2), sampled on a {HEAT_MAP_SPACING_M / 1000:g} km grid inside the City boundary "
        "(Wards layer). Hottest third = top third of each year's citywide sample."),
}


def setup_style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": MIN_FONT_PT,
        "axes.edgecolor": INK_MUTED, "axes.labelcolor": INK, "xtick.color": INK, "ytick.color": INK,
        "text.color": INK, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    })


def new_figure():
    return plt.figure(figsize=(FIG_W_IN, FIG_H_IN), facecolor=SURFACE)


def save(fig, filename: str, title: str, source: str, description: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / filename
    fig.savefig(path, dpi=DPI, facecolor=SURFACE,
                metadata={"Title": title, "Source": source, "Description": description,
                          "Software": "matplotlib (ottawa-cooling-risk/figures/make_figures.py)"})
    plt.close(fig)
    return path


def check_min_font(fig) -> list[str]:
    """Names of any text objects below the minimum font size."""
    small = []
    for t in fig.findobj(matplotlib.text.Text):
        if t.get_text().strip() and t.get_fontsize() < MIN_FONT_PT - 1e-6:
            small.append(f"{t.get_text()[:30]!r} {t.get_fontsize():.0f} pt")
    return small


# --- Figure 1: site profile comparison ---------------------------------------
LOW, MID, HIGH = "low", "moderate", "high"
FILL = {LOW: (TEAL, "#FFFFFF"), MID: (ORANGE_LIGHT, INK), HIGH: (ORANGE, "#FFFFFF")}
GROUP_SHORT = {"urban residential": "urban homes", "village / rural residential": "village homes",
               "rural countryside": "rural lot"}


def site_profile(name: str, lat: float, lon: float) -> dict:
    heat = uhi.assess_site(lat, lon)
    years = list(heat["years"].values())
    avg_above = sum(y["above_city_median_c"] for y in years) / len(years)
    heat_level = {"persistent_hotspot": HIGH, "intermittent": MID, "not_hotspot": LOW}[heat["assessment"]]
    heat_label = {"persistent_hotspot": "Persistent", "intermittent": "Intermittent",
                  "not_hotspot": "Not a hotspot"}[heat["assessment"]]
    n_high = heat["years_in_top_third"]
    water = pttw.assess_site(lat, lon, radii_m=(WATER_RADIUS_M,))["groundwater_supply_demand"][f"{WATER_RADIUS_M:g}m"]
    noise = zoning.nearest_residential(lat, lon, FACILITY_CLASS)
    noise_level = {zoning.FLAG_NEAR: HIGH, zoning.FLAG_CAUTION: MID, zoning.FLAG_CLEAR: LOW}[noise["flag"]]
    litres = water["reported_l_per_day"]
    return {
        "name": name,
        "heat": (heat_level, heat_label, f"{avg_above:+.0f} °C, {n_high} yrs"),
        "water": (water["band"] if water["band"] in (LOW, MID, HIGH) else MID,
                  water["band"].capitalize(), f"{litres / 1e6:.2f} ML/day" if litres else "none recorded"),
        "noise": (noise_level, f"{noise['distance_m']:.0f} m" if noise["distance_m"] is not None else "> 1 km",
                  GROUP_SHORT.get(noise["nearest_zone_group"], "none in range")),
    }


def figure_site_profiles() -> Path:
    base = freecooling.ottawa_baseline()["free_cooling_dry_bulb"]["18C"]
    profiles = [site_profile(n, la, lo) for n, la, lo, _ in SITES]
    fig = new_figure()
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, FIG_W_IN)
    ax.set_ylim(FIG_H_IN, 0)
    ax.axis("off")
    gap = 0.07                                     # white gap between cells, inches
    x0, name_w, base_w, col_w = 0.10, 3.25, 2.55, 2.70
    xs = [x0, x0 + name_w, x0 + name_w + base_w]
    for _ in range(3):
        xs.append(xs[-1] + col_w)
    headers = [("Free\ncooling", xs[1], base_w), ("Heat\nisland", xs[2], col_w),
               ("Water\ndemand", xs[3], col_w), ("Nearest\nhomes", xs[4], col_w)]
    top, head_h, row_h = 0.10, 1.0, 1.3
    for text, x, w in headers:
        ax.text(x + w / 2, top + head_h / 2, text, ha="center", va="center", fontsize=MIN_FONT_PT,
                fontweight="bold", linespacing=1.1)

    def cell(x, y, w, h, level, line1, line2):
        face, ink = FILL[level]
        ax.add_patch(Rectangle((x + gap, y + gap), w - 2 * gap, h - 2 * gap, facecolor=face, edgecolor="none"))
        ax.text(x + w / 2, y + h * 0.36, line1, ha="center", va="center", fontsize=MIN_FONT_PT,
                fontweight="bold", color=ink)
        ax.text(x + w / 2, y + h * 0.68, line2, ha="center", va="center", fontsize=MIN_FONT_PT, color=ink)

    y0 = top + head_h
    # Shared baseline cell (same for every site), spanning all rows, outlined not filled
    total_h = row_h * len(profiles)
    ax.add_patch(Rectangle((xs[1] + gap, y0 + gap), base_w - 2 * gap, total_h - 2 * gap, facecolor="#FFFFFF",
                           edgecolor=TEAL, linewidth=3))
    for text, dy, bold in (("Ottawa", 0.20, False), ("baseline:", 0.32, False),
                           (f"{base['full_days']} days", 0.50, True), ("at or below", 0.68, False),
                           ("18 °C", 0.80, False)):
        ax.text(xs[1] + base_w / 2, y0 + total_h * dy, text, ha="center", va="center", fontsize=MIN_FONT_PT,
                fontweight="bold" if bold else "normal")
    for i, p in enumerate(profiles):
        y = y0 + i * row_h
        ax.text(xs[0] + name_w - 0.2, y + row_h / 2, p["name"], ha="right", va="center", fontsize=MIN_FONT_PT,
                fontweight="bold")
        cell(xs[2], y, col_w, row_h, *p["heat"])
        cell(xs[3], y, col_w, row_h, *p["water"])
        cell(xs[4], y, col_w, row_h, *p["noise"])
    # Legend: three swatches
    ly = y0 + total_h + 0.45
    lx = xs[0] + 0.1
    for level, label, width in ((LOW, "Lower concern", 4.1), (MID, "Middle", 2.7), (HIGH, "Higher concern", 4.0)):
        ax.add_patch(Rectangle((lx, ly - 0.17), 0.42, 0.34, facecolor=FILL[level][0], edgecolor="none"))
        ax.text(lx + 0.55, ly, label, ha="left", va="center", fontsize=MIN_FONT_PT)
        lx += width
    return finish(fig, "fig1_site_profiles.png", 1, "Site profiles: climate baseline and site indicators",
                  "Three Ottawa sites across four indicators. Free cooling is one city-wide baseline; the other "
                  "three columns are site-specific. Heat island: persistence across 2019/2021/2023 and average "
                  f"degrees above the city median. Water: groundwater supply demand within 5 km (2022 reported). "
                  f"Nearest homes: distance to residential zoning, coloured by D-6 Class {FACILITY_CLASS} "
                  "separation (300 m) and influence area (1 km).")


# --- Figure 2: monthly free-cooling hours --------------------------------------
def figure_monthly() -> Path:
    hours = freecooling.read_epw(freecooling.ensure_epw())
    dry_cut, wet_cut = freecooling.DEFAULT_CUTOFFS_C[0], freecooling.DEFAULT_WB_CUTOFFS_C[0]
    dry = [0] * 12
    wet = [0] * 12
    for month, _, db, wb in hours:
        dry[month - 1] += db <= dry_cut
        wet[month - 1] += wb <= wet_cut
    fig = new_figure()
    ax = fig.add_axes([0.105, 0.14, 0.88, 0.80])
    width = 0.40
    xs = range(12)
    ax.bar([x - width / 2 for x in xs], dry, width, color=TEAL, edgecolor=SURFACE, linewidth=1.5,
           label=f"Dry-bulb ≤ {dry_cut:g} °C")
    ax.bar([x + width / 2 for x in xs], wet, width, color=ORANGE, edgecolor=SURFACE, linewidth=1.5,
           label=f"Wet-bulb ≤ {wet_cut:g} °C")
    ax.set_xticks(list(xs))
    ax.set_xticklabels(freecooling.MONTHS)
    ax.set_ylim(0, 744)
    ax.set_yticks([0, 250, 500, 744])
    ax.set_yticklabels(["0", "250", "500", "744"])
    ax.set_ylabel("Hours in month")
    ax.grid(axis="y", color=GRID, linewidth=1)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="upper center", frameon=False, fontsize=MIN_FONT_PT, handlelength=1.0, borderaxespad=0.2)
    ax.tick_params(length=0)
    return finish(fig, "fig2_monthly_free_cooling.png", 2, "Free-cooling hours by month, Ottawa typical year",
                  f"Hours per month at or below {dry_cut:g} °C dry-bulb (air-side free cooling) and "
                  f"{wet_cut:g} °C wet-bulb (water-side), Ottawa airport typical year. A month has 672-744 hours. "
                  f"Annual totals: dry-bulb {sum(dry)} h, wet-bulb {sum(wet)} h.")


# --- Figure 3: heat-island persistence map ---------------------------------------
def _hot_thresholds() -> dict[int, float]:
    """Per layer: the value above which a point is in the hottest third of that year's city."""
    out = {}
    for layer in uhi.LAYERS:
        dist = uhi.load_city_distribution(layer_id=layer)
        out[layer] = dist[min(len(dist) - 1, int(len(dist) * uhi.BAND_MODERATE_MAX_PCT / 100))]
    return out


def sample_persistence(spacing_m: float = HEAT_MAP_SPACING_M) -> list[tuple[float, float, int, int]]:
    """(lat, lon, years in hottest third, years with data) on a lattice inside the City boundary."""
    import requests
    polygons = pttw.load_boundary()
    pts = pttw.city_lattice(polygons, spacing_m)
    thresholds = _hot_thresholds()
    with requests.Session() as session, ThreadPoolExecutor(max_workers=8) as pool:
        def work(pt):
            lat, lon = pt
            x, y = uhi.to_web_mercator(lat, lon)
            hot = seen = 0
            for layer in uhi.LAYERS:
                v = uhi.identify_pixel(x, y, session, layer_id=layer)
                if v is not None:
                    seen += 1
                    hot += v >= thresholds[layer]
            return lat, lon, hot, seen
        return [r for r in pool.map(work, pts) if r[3] > 0]


def figure_heat_map() -> Path:
    samples = sample_persistence()
    polygons = pttw.load_boundary()
    lat0 = sum(s[0] for s in samples) / len(samples)
    kx = math.cos(math.radians(lat0))               # shrink longitude so the map is not stretched
    fig = new_figure()
    ax = fig.add_axes([0.01, 0.02, 0.58, 0.96])
    for rings in polygons:
        for ring in rings:
            ax.plot([p[0] * kx for p in ring], [p[1] for p in ring], color=INK_MUTED, linewidth=0.8, zorder=1)
    # square markers sized to the lattice spacing
    km_per_deg = 111.32
    xs = [s[1] * kx for s in samples]
    ys = [s[0] for s in samples]
    ax.set_aspect("equal")
    ax.set_xlim(min(xs) - 0.02, max(xs) + 0.02)
    ax.set_ylim(min(ys) - 0.02, max(ys) + 0.02)
    fig.canvas.draw()
    pts_per_deg = ax.transData.transform((0, 1))[1] - ax.transData.transform((0, 0))[1]
    side_pt = (HEAT_MAP_SPACING_M / 1000 / km_per_deg) * pts_per_deg * 72 / fig.dpi   # px -> points
    colors = [PERSISTENCE_RAMP[s[2]] for s in samples]
    ax.scatter(xs, ys, s=side_pt ** 2 * 1.04, c=colors, marker="s", linewidths=0, zorder=2)
    for name, lat, lon, letter in SITES:
        ax.scatter([lon * kx], [lat], s=560, c=SURFACE, edgecolors=INK, linewidths=3, zorder=4)
        ax.text(lon * kx, lat, letter, ha="center", va="center", fontsize=MIN_FONT_PT, fontweight="bold", zorder=5)
    ax.axis("off")
    # legend panel on the right
    lg = fig.add_axes([0.60, 0.05, 0.39, 0.90])
    lg.set_xlim(0, 1)
    lg.set_ylim(0, 1)
    lg.axis("off")
    lg.text(0.0, 0.98, "Years in the hottest\nthird of Ottawa", fontsize=MIN_FONT_PT, fontweight="bold",
            va="top", linespacing=1.1)
    for i, label in enumerate(("0 of 3", "1 of 3", "2 of 3", "3 of 3")):
        y = 0.72 - i * 0.10
        lg.add_patch(Rectangle((0.0, y - 0.038), 0.09, 0.076, facecolor=PERSISTENCE_RAMP[i], edgecolor=INK_MUTED,
                               linewidth=0.8 if i == 0 else 0))
        lg.text(0.13, y, label, fontsize=MIN_FONT_PT, va="center")
    for i, (name, _, _, letter) in enumerate(SITES):
        y = 0.28 - i * 0.105
        lg.scatter([0.045], [y], s=520, c=SURFACE, edgecolors=INK, linewidths=3, clip_on=False)
        lg.text(0.045, y, letter, ha="center", va="center", fontsize=MIN_FONT_PT, fontweight="bold")
        lg.text(0.13, y, name, fontsize=MIN_FONT_PT, va="center")
    return finish(fig, "fig3_heat_persistence.png", 3, "Heat-island persistence across 2019, 2021 and 2023",
                  "Each square is a point inside the City of Ottawa boundary, coloured by how many of the three "
                  "annual heat maps (2019, 2021, 2023) place it in the hottest third of the city that year. "
                  "Letters mark the sample sites. Surface temperature, one clear day per year; not air temperature.")


def finish(fig, filename: str, number: int, title: str, description: str) -> Path:
    small = check_min_font(fig)
    if small:
        print(f"  WARNING fonts below {MIN_FONT_PT} pt: {small}")
    return save(fig, filename, title, SOURCES[number], description)


FIGURES = {1: figure_site_profiles, 2: figure_monthly, 3: figure_heat_map}


def main(argv=None) -> int:
    setup_style()
    wanted = [int(a) for a in (argv if argv is not None else sys.argv[1:])] or sorted(FIGURES)
    for n in wanted:
        print(f"figure {n}: {FIGURES[n].__name__} ...")
        print("  saved", FIGURES[n]())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

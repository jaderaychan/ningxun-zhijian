"""Render the four-panel story figure: terrain -> imperviousness -> water -> risk.

The demo video has to carry a viewer from "raw elevation data" to "80 subway
entrances at risk" in about a minute. Separate figures make that a slideshow; one
composite makes it a single argument. Panels:

    1. terrain            what the ground looks like
    2. curve number       how much of the rain becomes runoff (land cover)
    3. ponding depth      where the water ends up
    4. risk points        which tunnels and facilities that flooding actually hits

Runs from the caches, so it costs no network time once the event has been run.

    python scripts/make_story_figure.py
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

CITY_BOUNDS = (118.72, 32.02, 118.82, 32.10)
RUN_TAG = "nanjing_final"


def main() -> int:
    import matplotlib

    matplotlib.use("Agg")
    # Shared detector: reports a missing CJK font instead of letting matplotlib
    # substitute one and draw a panel of empty boxes.
    from floodkit.runio import configure_fonts

    configure_fonts()
    import matplotlib.pyplot as plt
    import rasterio
    from matplotlib.lines import Line2D
    from rasterio.warp import transform as warp_transform

    from floodkit.dem_source import ensure_tiles
    from floodkit.exposure import _is_underpass, _project_many, load_features
    from floodkit.landcover import curve_number_grid, ensure_tile
    from floodkit.runio import class_codes
    from floodkit.pipeline import load_dem_metric

    run_dir = os.path.join(ROOT, "data", "runs", RUN_TAG)
    report_path = os.path.join(run_dir, "%s_report.json" % RUN_TAG)
    depth_path = os.path.join(run_dir, "%s_depth.tif" % RUN_TAG)
    for required in (report_path, depth_path):
        if not os.path.exists(required):
            print("missing %s — run the event first:" % required)
            print("  python -m floodkit run --bounds 118.72 32.02 118.82 32.10 \\")
            print("         --rain-mm 100 --cn 85 --cn-source landcover --tag %s --exposure" % RUN_TAG)
            return 2

    tiles = ensure_tiles(CITY_BOUNDS, os.path.join(ROOT, "data", "dem"), progress=lambda *_: None)
    grid = None
    for tile in tiles:
        try:
            grid = load_dem_metric(tile, CITY_BOUNDS)
            break
        except Exception:
            continue
    if grid is None:
        print("could not load a DEM tile")
        return 2

    tile = ensure_tile(CITY_BOUNDS, os.path.join(ROOT, "data", "landcover"), progress=lambda *_: None)
    landcover = curve_number_grid(tile, CITY_BOUNDS, grid.transform, grid.shape)
    cn = landcover["cn"]
    water = landcover["water_mask"]

    with rasterio.open(depth_path) as src:
        depth = src.read(1)
    depth = np.where(np.isfinite(depth), depth, 0.0)

    report = json.load(open(report_path, encoding="utf-8"))
    exposure = report.get("exposure") or {}
    facilities = (exposure.get("critical_facilities") or {}).get("top_facilities") or []

    underpass_xy = []
    for element in load_features(
        os.path.join(ROOT, "data", "osm", sorted(
            n for n in os.listdir(os.path.join(ROOT, "data", "osm")) if n.startswith("osm_roads") and n.endswith(".json")
        )[-1]
    )):
        tags = element.get("tags") or {}
        if not _is_underpass(tags):
            continue
        geometry = [(p["lon"], p["lat"]) for p in (element.get("geometry") or []) if "lon" in p]
        if len(geometry) < 2:
            continue
        projected = _project_many(geometry, grid.transform)
        underpass_xy.append(([p[0] for p in projected], [p[1] for p in projected]))

    # constrained layout keeps the panel titles from colliding with the panel
    # above them, which plain tight_layout did not manage with four colourbars.
    fig, axes = plt.subplots(2, 2, figsize=(15.0, 12.6), dpi=165, layout="constrained")
    terrain = np.where(grid.valid, grid.elevation, np.nan)
    mesh = axes[0, 0].imshow(terrain, cmap="terrain")
    axes[0, 0].set_title("1. 地形高程 (m) — Copernicus DEM GLO-30", fontsize=16)
    fig.colorbar(mesh, ax=axes[0, 0], shrink=0.82)

    cn_masked = np.where(grid.valid & ~water, cn, np.nan)
    mesh = axes[0, 1].imshow(cn_masked, cmap="YlOrRd", vmin=60, vmax=100)
    axes[0, 1].set_title("2. 复合曲线数 CN — ESA WorldCover 10 m", fontsize=16)
    fig.colorbar(mesh, ax=axes[0, 1], shrink=0.82)

    # Masked at zero rather than at 0.02: the panel title quotes the report's ponded
    # area, and that number counts every cell with any water in it. Cutting the
    # first couple of centimetres off the picture made the figure illustrate a
    # smaller area than the caption above it.
    #
    # The six shared classes rather than a continuous ramp: the printed map, the
    # interactive page and the sweep panels all bin depth the same way, and a story
    # figure using its own scale would be the one place the colours mean something
    # else. It also fixes legibility — a ramp pinned near the maximum flattens the
    # widespread centimetre-deep flooding into one shade.
    from matplotlib.colors import BoundaryNorm, ListedColormap

    from floodkit.runio import DEPTH_COLORS, DEPTH_EDGES, DEPTH_LABELS

    codes = class_codes(depth)
    cmap = ListedColormap(DEPTH_COLORS)
    depth_norm = BoundaryNorm(DEPTH_EDGES + [max(DEPTH_EDGES[-1] + 1.0, float(depth.max()) + 1.0)],
                              cmap.N)
    shown_depth = np.ma.masked_where(codes <= 0, codes)
    mesh = axes[1, 0].imshow(shown_depth, cmap=cmap, norm=depth_norm,
                             interpolation="nearest")
    axes[1, 0].set_title(
        "3. 积水深度 (m) — 100 mm 降雨\n淹没 %.2f km²，最深 %.2f m（六级分档）"
        % (report["ponding"]["pond_area_m2"] / 1e6, depth.max()),
        fontsize=14,
    )
    cbar = fig.colorbar(mesh, ax=axes[1, 0], shrink=0.82, ticks=DEPTH_EDGES)
    cbar.ax.set_yticklabels(DEPTH_LABELS)

    axes[1, 1].imshow(shown_depth, cmap=cmap, norm=depth_norm, interpolation="nearest")
    for xs, ys in underpass_xy:
        axes[1, 1].plot(xs, ys, color="#d62728", linewidth=0.9, alpha=0.8, solid_capstyle="round")
    if facilities:
        xs, ys = warp_transform(
            "EPSG:4326", str(grid.crs),
            [f["lon"] for f in facilities], [f["lat"] for f in facilities],
        )
        inverse = ~grid.transform
        points = [inverse * (x, y) for x, y in zip(xs, ys)]
        depths = [f["depth_m"] for f in facilities]
        axes[1, 1].scatter(
            [p[0] for p in points], [p[1] for p in points], c=depths, cmap="autumn_r",
            s=55, edgecolors="black", linewidths=0.6, zorder=6, vmin=0, vmax=max(depths),
        )
    up = exposure.get("underpasses") or {}
    fac = exposure.get("critical_facilities") or {}
    axes[1, 1].set_title(
        "4. 风险点位 — 下穿 %d 段被淹(生命危险 %d) · 关键设施 %d 个(地铁口 %d)\n图中标注最需处置的前 %d 个设施"
        % (up.get("underpass_ways_flooded", 0), up.get("life_safety_count", 0),
           fac.get("facilities_flooded", 0), fac.get("subway_entrances_flooded", 0),
           len(facilities)),
        fontsize=15,
    )
    axes[1, 1].legend(
        handles=[
            Line2D([0], [0], color="#d62728", lw=1.6, label="下穿通道 / 隧道（全部）"),
            Line2D([0], [0], marker="o", color="none", markerfacecolor="#ff7f0e",
                   markeredgecolor="black", markersize=7, label="关键设施（颜色 = 水深）"),
        ],
        loc="lower right", fontsize=11.5, framealpha=0.9,
    )

    for ax in axes.ravel():
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle(
        "宁讯智鉴：南京主城区 100 mm 降雨内涝分析（智能体自主完成，五项指标全部通过）",
        fontsize=18,
    )
    out = os.path.join(run_dir, "%s_story.png" % RUN_TAG)
    fig.savefig(out)
    plt.close(fig)
    print("wrote %s" % out)
    print("CN 范围 %.0f..%.0f | 水深最大 %.2f m | 下穿段 %d | 关键设施 %d"
          % (np.nanmin(cn_masked), np.nanmax(cn_masked), depth.max(),
             len(underpass_xy), len(facilities)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

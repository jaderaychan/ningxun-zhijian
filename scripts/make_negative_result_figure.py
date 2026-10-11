"""Draw the negative result: the model has no skill at the official waterlogging list.

This is the figure the project most needs and least wants, so it is built to be
hard to argue with.

The reported result is that the model cannot predict the 27 mapped points from the
Jiangsu Public Security Bureau's 2015 Nanjing list: 3 hits out of 27 against a 70%
bar, and a ranking AUC of 0.5193 with a 95% confidence interval that contains 0.5.
A number in a table invites "maybe the threshold was wrong". Three panels answer
that:

  1. the ROC curve behind the AUC, with the operating point the binary hit rate
     actually uses marked on it — so the reader can see for themselves that no
     threshold on this curve reaches the bar;
  2. the two depth distributions on one axis, official against background, drawn as
     ECDFs. They lie on top of each other. That is what "no signal" looks like;
  3. the official points on the modelled depth field, green where the model got
     them, red where it did not.

**The event is re-run to draw this.** The reported AUC depends on the water mask the
run used — terrain-detected flat water unioned with land cover — and on the requested
bounds, which the metric raster does not round-trip to. Re-running with the config the
report now records is the only way to plot the curve the number came from rather
than a similar-looking one. The script asserts its recomputed hit rate and AUC equal
the report's before it draws anything; a figure that disagrees with the number it
annotates is worse than no figure.

    python scripts/make_negative_result_figure.py [tag] [--from-report]

`--from-report` skips the re-run and draws from the stored report alone, which is
faster but cannot draw the ROC (the samples are not stored), so it omits that panel.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from floodkit.runio import (  # noqa: E402
    artifact,
    class_codes,
    configure_fonts,
    missing_glyphs,
    project_lonlat,
)

ZH = {
    "title": "负结果：模型对官方易涝点清单没有预测能力",
    "subtitle": (
        "江苏省公安厅 2015 年公布的南京易积水路段，%d 处落在研究区内。"
        "二值命中率 %d/%d = %.1f%%（判据 ≥%.0f%%）｜排序检验 AUC %.4f（95%% CI %.3f–%.3f，含 0.5）｜z = %.3f"
    ),
    "roc": "排序检验的 ROC（同一批样本、同一随机种子）",
    "roc_x": "背景点被判为湿的比例（假正率）",
    "roc_y": "官方点被判为湿的比例（真正率）",
    "roc_chance": "无信号（AUC = 0.5）",
    "roc_model": "模型（AUC = %.4f）",
    "roc_op": "报告采用的 0.05 m 阈值落点\n（真正率 %.1f%% = %d/%d，假正率 %.1f%%）",
    "auc_note": "AUC 的 95%% 置信区间 %.3f–%.3f 覆盖 0.5，z = %.3f，未越过 |z| ≥ 1.96 的判据。",
    "ecdf": "两类点位的模拟水深分布（ECDF）",
    "ecdf_x": "模拟水深（米）",
    "ecdf_y": "累计比例",
    "ecdf_official": "官方易涝点（n=%d，中位数 %.2f m）",
    "ecdf_background": "研究区随机背景点（n=%d，中位数 %.2f m）",
    "ecdf_note": "两条曲线几乎重合：官方点位并不比随机点位更湿。",
    "map": "官方易涝点与模拟积水范围",
    "hit": "命中（模拟水深 ≥ 0.05 m）",
    "miss": "未命中（模型判为不积水）",
    "outside": "清单中未能匹配到坐标的点",
    "depth": "模拟积水深度（米）",
    "foot": (
        "地形 Copernicus DEM GLO-30｜土地覆盖 ESA WorldCover 10 m｜易涝点清单 江苏省公安厅（2015）"
        "｜随机背景点 %d 个，固定种子 %d"
    ),
    "why": (
        "这不是模型算错了：该清单记录的是管网瓶颈型易涝点，而本模型只模拟地形驱动的积水。"
        "负结果与产生它的代码一起提交——它同样是证据。"
    ),
    "detected": "地形检测 + 土地覆盖水面掩模已排除 %.1f km² 永久水面",
}

EN = {
    "title": "Negative result: no skill at the official waterlogging list",
    "subtitle": (
        "%d mapped points from the 2015 Nanjing list fall inside the study area. "
        "Binary hit rate %d/%d = %.1f%% (bar %.0f%%) | ranking AUC %.4f (95%% CI %.3f-%.3f, contains 0.5) | z = %.3f"
    ),
    "roc": "ROC of the ranking test (same samples, same seed)",
    "roc_x": "Fraction of background called wet (false positive rate)",
    "roc_y": "Fraction of official points called wet (true positive rate)",
    "roc_chance": "No signal (AUC = 0.5)",
    "roc_model": "Model (AUC = %.4f)",
    "roc_op": "Where the report's 0.05 m threshold lands\n(TPR %.1f%% = %d/%d, FPR %.1f%%)",
    "auc_note": "The 95%% confidence interval %.3f-%.3f covers 0.5; z = %.3f, short of the |z| >= 1.96 bar.",
    "ecdf": "Modelled depth at both point sets (ECDF)",
    "ecdf_x": "Modelled depth (m)",
    "ecdf_y": "Cumulative share",
    "ecdf_official": "Official waterlogging points (n=%d, median %.2f m)",
    "ecdf_background": "Random points in the study area (n=%d, median %.2f m)",
    "ecdf_note": "The curves sit on top of each other: the official points are not wetter than random ones.",
    "map": "Official waterlogging points against the modelled ponding",
    "hit": "Hit (modelled depth >= 0.05 m)",
    "miss": "Missed (model says dry)",
    "outside": "List entry with no matched coordinate",
    "depth": "Modelled ponding depth (m)",
    "foot": (
        "Terrain Copernicus DEM GLO-30 | Land cover ESA WorldCover 10 m | Waterlogging list Jiangsu "
        "Public Security Bureau (2015) | %d random background points, fixed seed %d"
    ),
    "why": (
        "The model is not wrong here: that list records drainage-bottleneck flooding, while this model "
        "simulates terrain-driven ponding only. The negative result ships with the code that produced it."
    ),
    "detected": "terrain-detected flat water plus the land-cover mask removed %.1f km2",
}


def roc_curve(official: np.ndarray, background: np.ndarray):
    """TPR/FPR as the decision threshold sweeps the observed depths.

    Aggregated **per distinct depth**, not per sample. Almost every point in both
    samples sits at exactly 0.0 m, so a per-sample sweep would place all the
    positives before the negatives at that one score and integrate to ~0.93 instead
    of 0.52. A threshold classifies every point at or above it, so the operating
    point is a property of the depth value, not of the row order.

    With ties collapsed this way the trapezoid area equals the Mann-Whitney AUC the
    report quotes, which the caller asserts.
    """
    n1, n2 = official.size, background.size
    scores = np.concatenate([official, background])
    labels = np.concatenate([np.ones(n1), np.zeros(n2)])
    order = np.argsort(scores, kind="stable")
    sorted_scores, sorted_labels = scores[order], labels[order]
    cumulative = np.concatenate([[0.0], np.cumsum(sorted_labels)])

    unique = np.unique(sorted_scores)
    below = np.searchsorted(sorted_scores, unique, side="left")
    positives_below = cumulative[below]
    tpr = (n1 - positives_below) / n1                     # official at or above v
    fpr = (n2 - (below - positives_below)) / n2           # background at or above v

    # `unique` ascending makes fpr/tpr descending; flip for an increasing curve.
    fpr = np.concatenate([[0.0], fpr[::-1]])
    tpr = np.concatenate([[0.0], tpr[::-1]])
    return fpr, tpr, float(np.trapezoid(tpr, fpr))


def ecdf(values: np.ndarray):
    x = np.sort(values)
    y = np.arange(1, x.size + 1) / x.size
    return x, y


def checkpoint_names(path: str) -> list:
    """Street names from the official list, in the order the points were read.

    The stored checkpoint details carry coordinates and depths but no names, so
    every hit would be labelled "(unnamed)" — which tells a reader nothing about
    which street the model got right. Reading the list again in the same order as
    `_load_checkpoints` keeps the two aligned.
    """
    import csv as csvmod

    if not path or not os.path.exists(str(path)):
        return []
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read().strip()
    if not text.startswith("[") and "lon" in text.splitlines()[0].lower():
        rows = list(csvmod.DictReader(text.splitlines()))
        return [(row.get("name") or "(unnamed)").strip()
                for row in rows if (row.get("lon") or "").strip() and (row.get("lat") or "").strip()]
    return []


def load_dem_and_water(bounds, config):
    from floodkit.dem_source import ensure_tiles
    from floodkit.landcover import curve_number_grid, ensure_tile
    from floodkit.pipeline import load_dem_metric

    grid = None
    for tile in ensure_tiles(bounds, os.path.join(ROOT, "data", "dem"), progress=lambda *_: None):
        try:
            grid = load_dem_metric(tile, bounds)
            break
        except Exception:
            continue
    if grid is None:
        raise SystemExit("could not load a DEM tile for %s" % (bounds,))

    water, cn_input = None, float(config.get("cn") or 85.0)
    if config.get("cn_source") == "landcover":
        lc = ensure_tile(bounds, os.path.join(ROOT, "data", "landcover"), progress=lambda *_: None)
        landcover = curve_number_grid(lc, bounds, grid.transform, grid.shape)
        water, cn_input = landcover["water_mask"], landcover["cn"]
    return grid, water, cn_input


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", default="nanjing_wide")
    parser.add_argument("--from-report", action="store_true",
                        help="skip the event re-run; draws no ROC panel")
    parser.add_argument("--formats", default="png,pdf")
    args = parser.parse_args()
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]

    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams["svg.fonttype"] = "path"
    matplotlib.rcParams["pdf.fonttype"] = 42
    font = configure_fonts()
    L = ZH if font else EN
    if font:
        gaps = {key: "".join(sorted(missing_glyphs(text, font)))
                for key, text in L.items() if isinstance(text, str) and missing_glyphs(text, font)
                and "%" not in text}
        if any(gaps.values()):
            print("note: the CJK font cannot draw %s; falling back to English" % gaps)
            L = EN
    print("labels: %s%s" % ("Chinese" if L is ZH else "English",
                            " (%s)" % font if font else ""))

    import matplotlib.pyplot as plt

    tag = args.tag
    run_dir = os.path.dirname(artifact(tag, "_report.json"))
    with open(artifact(tag, "_report.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    config = report.get("run_config") or {}
    if not config:
        raise SystemExit(
            "%s has no run_config block, so the event cannot be reproduced. Re-run it "
            "with `python -m floodkit run ...` on this version." % tag)

    ranking = report.get("checkpoint_ranking") or {}
    points = report.get("checkpoints") or {}
    if not ranking.get("available"):
        raise SystemExit("%s carries no ranking analysis; run it with --checkpoints" % tag)
    if not ranking.get("official_points_inside"):
        raise SystemExit("no official checkpoint falls inside the analysed grid")

    official = background = None
    if not args.from_report:
        print("re-running the event to obtain the exact samples behind AUC %.4f ..."
              % ranking["auc"])
        from floodkit.cli import _load_checkpoints
        from floodkit.pipeline import DEFAULT_ENVELOPE, rank_sample, run_event

        bounds = tuple(config["bounds_wgs84"])
        grid, water, cn_input = load_dem_and_water(bounds, config)

        checkpoints_path = config.get("checkpoints")
        if not checkpoints_path or not os.path.exists(str(checkpoints_path)):
            candidate = os.path.join(ROOT, "data", "checkpoints", "nanjing_official_2015.csv")
            if not os.path.exists(candidate):
                raise SystemExit("checkpoint list not found: %s" % checkpoints_path)
            checkpoints_path = candidate
        points_lonlat = _load_checkpoints(str(checkpoints_path))
        names = checkpoint_names(str(checkpoints_path))

        result = run_event(
            dem=grid, rain_mm=float(config["rain_mm"]), cn=cn_input,
            envelope_m=tuple(config.get("envelope_m") or DEFAULT_ENVELOPE),
            min_depth_m=float(config.get("min_depth_m", 0.05)),
            checkpoints_lonlat=points_lonlat, water_mask=water,
        )
        freshwater = result.get("checkpoints") or {}
        reranked = result.get("checkpoint_ranking") or {}
        print("  recomputed: %d/%d hits, AUC %.12f"
              % (freshwater.get("hits", 0), freshwater.get("points", 0),
                 reranked.get("auc", float("nan"))))
        # The figure must not annotate a number it did not reproduce.
        if freshwater.get("hits") != points.get("hits") or \
                freshwater.get("points") != points.get("points"):
            raise SystemExit("re-run disagrees on the hit rate: %s vs %s"
                             % (freshwater.get("hits"), points.get("hits")))
        if abs(reranked.get("auc", 0.0) - ranking["auc"]) > 1e-12:
            raise SystemExit("re-run disagrees on the AUC: %.12f vs %.12f"
                             % (reranked.get("auc", 0.0), ranking["auc"]))
        print("  the re-run reproduces the report exactly (hit rate and AUC)")

        # The mask the ranking used is the land-cover water **unioned with** the
        # terrain-detected flat water, exactly as run_event builds it. Passing only
        # the land-cover half leaves one extra official point inside the grid and
        # shifts the AUC from 0.5193 to 0.5220 — close enough to look right.
        from floodkit.terrain import detect_flat_water

        detected, _ = detect_flat_water(grid.elevation)
        effective = detected if water is None else (np.asarray(water, dtype=bool) | detected)
        sample = rank_sample(result["depth_m"], points_lonlat, grid.transform,
                             str(grid.crs), exclusion_mask=effective)
        official, background = sample["official"], sample["background"]
        if sample["inside"] != ranking.get("official_points_inside"):
            raise SystemExit(
                "the reconstructed mask admits %d official points, the report has %d — "
                "the samples would not be the ones the AUC came from"
                % (sample["inside"], ranking.get("official_points_inside")))
        if abs(float(official.mean()) - ranking["official_mean_depth_m"]) > 1e-9:
            raise SystemExit("official mean depth %.12f does not match the report's %.12f"
                             % (official.mean(), ranking["official_mean_depth_m"]))
        if abs(float(background.mean()) - ranking["background_mean_depth_m"]) > 1e-9:
            raise SystemExit("background mean depth %.12f does not match the report's %.12f"
                             % (background.mean(), ranking["background_mean_depth_m"]))
        print("  samples verified against the report: %d official (mean %.6f m), "
              "%d background (mean %.6f m)"
              % (official.size, official.mean(), background.size, background.mean()))

        depth = result["depth_m"]
        transform, crs, shape = grid.transform, grid.crs, grid.shape
        water_area_km2 = float(effective.sum()) * grid.cell_area_m2 / 1e6
    else:
        depth = None
        transform, crs, shape = None, None, None
        water_area_km2 = 0.0
        names = []

    if official is not None:
        fpr, tpr, auc_from_curve = roc_curve(official, background)
        print("  AUC from the drawn ROC: %.12f (report %.12f) — two computations, same number"
              % (auc_from_curve, ranking["auc"]))
        if abs(auc_from_curve - ranking["auc"]) > 1e-9:
            raise SystemExit("the drawn ROC integrates to %.12f, not the reported %.12f"
                             % (auc_from_curve, ranking["auc"]))
        threshold = 0.05
        tpr_op = float((official > threshold).mean())
        fpr_op = float((background > threshold).mean())

    # ---- figure -------------------------------------------------------------
    has_roc = official is not None
    fig = plt.figure(figsize=(15.0 if has_roc else 11.0, 12.2), dpi=195)
    if has_roc:
        grid_spec = fig.add_gridspec(2, 2, height_ratios=[1.0, 1.32],
                                     hspace=0.24, wspace=0.22,
                                     left=0.055, right=0.975, top=0.90, bottom=0.075)
        ax_roc, ax_ecdf = fig.add_subplot(grid_spec[0, 0]), fig.add_subplot(grid_spec[0, 1])
        ax_map = fig.add_subplot(grid_spec[1, :])
    else:
        grid_spec = fig.add_gridspec(1, 1, left=0.055, right=0.975, top=0.90, bottom=0.075)
        ax_map = fig.add_subplot(grid_spec[0, 0])

    if has_roc:
        ax_roc.plot([0, 1], [0, 1], color="#9e9e9e", lw=1.4, ls="--", label=L["roc_chance"])
        ax_roc.plot(fpr, tpr, color="#1f5fa9", lw=2.6, label=L["roc_model"] % auc_from_curve)
        ax_roc.scatter([fpr_op], [tpr_op], s=90, c="#c62828", edgecolors="white",
                       linewidths=1.4, zorder=5)
        ax_roc.annotate(L["roc_op"] % (100 * tpr_op, points.get("hits", 0),
                                       points.get("points", 0), 100 * fpr_op),
                        (fpr_op, tpr_op), textcoords="offset points", xytext=(14, -26),
                        fontsize=9.5, color="#c62828",
                        bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#e0b4b4", lw=0.6),
                        arrowprops=dict(arrowstyle="-", color="#c62828", lw=0.8))
        ax_roc.set_xlim(-0.02, 1.02)
        ax_roc.set_ylim(-0.02, 1.02)
        ax_roc.set_xlabel(L["roc_x"], fontsize=12)
        ax_roc.set_ylabel(L["roc_y"], fontsize=12)
        ax_roc.set_title(L["roc"], fontsize=14.5)
        ax_roc.legend(loc="lower right", fontsize=11.5, frameon=False)
        ax_roc.grid(alpha=0.25, lw=0.6)
        lo, hi = ranking["auc_ci95"]
        ax_roc.text(0.02, 0.98, L["auc_note"] % (lo, hi, ranking["z_score"]),
                    transform=ax_roc.transAxes, va="top", fontsize=11.5, color="#37474f",
                    bbox=dict(boxstyle="round,pad=0.35", fc="#f5f8fb", ec="#dbe6f0", lw=0.7))

        for values, colour, key in ((background, "#90a4ae", "ecdf_background"),
                                    (official, "#c62828", "ecdf_official")):
            x, y = ecdf(values)
            ax_ecdf.step(np.concatenate([[0], x]), np.concatenate([[0], y]),
                         where="post", color=colour, lw=2.4,
                         label=L[key] % (values.size, float(np.median(values))))
        ax_ecdf.set_xscale("symlog", linthresh=0.01)
        ax_ecdf.set_xlim(0, 1.0)
        ax_ecdf.set_ylim(0, 1.02)
        ax_ecdf.set_xlabel(L["ecdf_x"], fontsize=12)
        ax_ecdf.set_ylabel(L["ecdf_y"], fontsize=12)
        ax_ecdf.set_title(L["ecdf"], fontsize=14.5)
        ax_ecdf.legend(loc="lower right", fontsize=11, frameon=False)
        ax_ecdf.grid(alpha=0.25, lw=0.6)
        ax_ecdf.text(0.03, 0.72, L["ecdf_note"], transform=ax_ecdf.transAxes,
                     fontsize=11.5, color="#37474f",
                     bbox=dict(boxstyle="round,pad=0.35", fc="#f5f8fc", ec="#dbe6f0", lw=0.7))

    # ---- map ----------------------------------------------------------------
    if depth is not None:
        from matplotlib.colors import BoundaryNorm, ListedColormap

        from floodkit.runio import DEPTH_COLORS, DEPTH_EDGES, DEPTH_LABELS

        # The same classes as every other figure, not a continuous ramp: on a study
        # area this size most of the wet cells are a few centimetres deep, and a ramp
        # pinned to the 6 m maximum renders them as an almost invisible speckle.
        codes = class_codes(depth)
        cmap = ListedColormap(DEPTH_COLORS)
        norm = BoundaryNorm(DEPTH_EDGES + [max(DEPTH_EDGES[-1] + 1.0, float(depth.max()) + 1.0)],
                            cmap.N)
        im = ax_map.imshow(np.ma.masked_where(codes <= 0, codes), cmap=cmap, norm=norm,
                           interpolation="nearest")
        cbar = fig.colorbar(im, ax=ax_map, shrink=0.62, pad=0.012, ticks=DEPTH_EDGES)
        cbar.ax.set_yticklabels(DEPTH_LABELS)
        cbar.set_label(L["depth"], fontsize=12)

        details = points.get("details") or []
        for index, item in enumerate(details):
            col, row = project_lonlat([(item["lon"], item["lat"])], transform, str(crs))[0]
            hit = item.get("depth_m", 0.0) >= 0.05
            ax_map.scatter([col], [row], s=150, marker="o",
                           facecolor="#2e7d32" if hit else "none",
                           edgecolors="#2e7d32" if hit else "#c62828",
                           linewidths=2.2, zorder=6)
            if hit:
                label = names[index] if index < len(names) else (item.get("name") or "(unnamed)")
                ax_map.annotate("%s %.2fm" % (label, item["depth_m"]),
                                (col, row), textcoords="offset points", xytext=(10, 8),
                                fontsize=11.5, zorder=7,
                                bbox=dict(boxstyle="round,pad=0.24", fc="white",
                                          ec="#9e9e9e", lw=0.5, alpha=0.95),
                                arrowprops=dict(arrowstyle="-", color="#616161", lw=0.6))
        ax_map.set_xticks([])
        ax_map.set_yticks([])
        ax_map.set_title(L["map"], fontsize=14.5)
        handles = [
            plt.Line2D([0], [0], marker="o", color="none", markerfacecolor="#2e7d32",
                       markeredgecolor="#2e7d32", markersize=10, label=L["hit"]),
            plt.Line2D([0], [0], marker="o", color="none", markerfacecolor="none",
                       markeredgecolor="#c62828", markeredgewidth=2, markersize=10,
                       label=L["miss"]),
        ]
        # Upper left, not upper right: the northern edge is where the hit labels sit,
        # and a legend that lands on top of the one label a reader needs is worse
        # than a legend in a corner with no data in it.
        ax_map.legend(handles=handles, loc="upper left", fontsize=11.5, framealpha=0.94)

    fig.suptitle(L["title"], fontsize=20, fontweight="bold", y=0.978)
    fig.text(0.5, 0.945, L["subtitle"] % (
        points.get("points", 0), points.get("hits", 0), points.get("points", 0),
        100 * points.get("hit_rate", 0.0), 100 * points.get("tolerance", 0.7),
        ranking["auc"], ranking["auc_ci95"][0], ranking["auc_ci95"][1], ranking["z_score"]),
        ha="center", va="center", fontsize=12, color="#37474f")
    fig.text(0.5, 0.047, L["why"], ha="center", fontsize=11.5, color="#c62828")
    fig.text(0.5, 0.030, L["detected"] % water_area_km2 if water_area_km2 else "",
             ha="center", fontsize=10.2, color="#6a4fc0")
    fig.text(0.5, 0.014,
             L["foot"] % (ranking.get("background_cells", 0), 20261007),
             ha="center", fontsize=10, color="#757575")

    written = []
    for fmt in formats:
        path = artifact(tag, "_negative_result.%s" % fmt)
        fig.savefig(path, bbox_inches="tight", facecolor="white", format=fmt)
        written.append((path, os.path.getsize(path)))
    plt.close(fig)

    sidecar = artifact(tag, "_negative_result.json")
    with open(sidecar, "w", encoding="utf-8") as fh:
        json.dump({
            "tag": tag,
            "points": points.get("points"),
            "hits": points.get("hits"),
            "hit_rate": points.get("hit_rate"),
            "tolerance": points.get("tolerance"),
            "auc": ranking["auc"],
            "auc_ci95": ranking["auc_ci95"],
            "z_score": ranking["z_score"],
            "auc_from_drawn_roc": (auc_from_curve if official is not None else None),
            "official_points_in_grid": ranking.get("official_points_inside"),
            "background_cells": ranking.get("background_cells"),
            "rerun_reproduced_report": official is not None,
            "panels": ["roc", "ecdf", "map"] if official is not None else ["map"],
            "note": "auc_from_drawn_roc is integrated from the plotted curve; it must equal auc.",
        }, fh, ensure_ascii=False, indent=2)

    for path, size in written:
        print("wrote %s (%.2f MB)" % (path, size / 1e6))
    print("wrote %s" % sidecar)
    print("命中 %d/%d = %.1f%%，AUC %.4f（图中曲线积分 %.4f）"
          % (points.get("hits", 0), points.get("points", 0),
             100 * points.get("hit_rate", 0.0), ranking["auc"],
             auc_from_curve if official is not None else float("nan")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

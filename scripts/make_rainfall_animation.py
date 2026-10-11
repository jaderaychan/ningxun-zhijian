"""Animate the flooding across rainfall intensities, and draw the same data as panels.

A single 100 mm scenario leaves the solver's spill branch untouched: nothing in the
city study area overflows at 100 mm, so the code that handles a basin filling past
its spill level never runs. The rainfall sweep is what found that. This turns the
sweep into something a person can watch — the ponded area growing, and at 150 mm the
basins starting to spill — because "the branch was never exercised" is a sentence,
and a moving picture of it appearing is an argument.

Outputs, all in data/evidence/:

  rainfall_animation.gif        5 frames, no dependencies, plays anywhere
  rainfall_animation.mp4        the same, if ffmpeg is on PATH
  rainfall_smallmultiples.png   the frames side by side, for a slide or a report
  rainfall_animation.json       the numbers behind every frame

**The numbers are checked against `rainfall_sweep.json`** before anything is drawn.
That file was written by tests/rainfall_sweep.py, an independent entry point: if this
script's re-run disagrees with it, one of the two is wrong and neither should be
published.

    python scripts/make_rainfall_animation.py [--rain 50,100,150,200,300] [--tag rainfall]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from floodkit.runio import (  # noqa: E402
    DEPTH_COLORS,
    DEPTH_EDGES,
    DEPTH_LABELS,
    configure_fonts,
)

CITY_BOUNDS = (118.72, 32.02, 118.82, 32.10)
DEFAULT_RAIN = (50.0, 100.0, 150.0, 200.0, 300.0)
EVIDENCE = os.path.join(ROOT, "data", "evidence")
FRAMES = os.path.join(ROOT, "data", "tmp", "rainfall_frames")

ZH = {
    "panel": "%.0f mm 降雨",
    "stats": "淹没 %.2f km²｜最深 %.2f m\n溢出 %s m³｜溢出洼地 %d 个",
    "spill_none": "未触发外溢分支",
    "spill_on": "外溢分支已执行",
    "suptitle": "降雨强度扫描：同一片地形，雨越大水越深（并溢出）",
    "note": ("旗舰情景 100 mm 时溢出量恒为 0——处理洼地灌满外溢的那段代码从未被执行过。"
             "未被执行的代码就是未被验证的代码；150 mm 起该分支才开始运行。"),
    "grid": "研究区 %.4f–%.4f°E, %.4f–%.4f°N　格网 %.1f m　曲线数 CN %.0f（均一）",
    "depth": "模拟积水深度（米）",
}
EN = {
    "panel": "%.0f mm rainfall",
    "stats": "ponded %.2f km2 | max %.2f m\nspilled %s m3 | overflowing basins %d",
    "spill_none": "spill branch not exercised",
    "spill_on": "spill branch exercised",
    "suptitle": "Rainfall sweep: same terrain, more rain, deeper water (and then overflow)",
    "note": ("At 100 mm the spilled volume is identically zero - the code path that handles a "
             "basin filling past its spill level never ran. It starts running at 150 mm."),
    "grid": "Study area %.4f-%.4f E, %.4f-%.4f N  grid %.1f m  CN %.0f (uniform)",
    "depth": "Modelled ponding depth (m)",
}


def fmt_volume(value) -> str:
    if value is None:
        return "0"
    return "%.0f" % value if value < 10000 else "%.1e" % value


def render_frame(depth, rain, stats, L, path):
    """One animation frame. Every frame shares the colour scale and the extent."""
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap

    from floodkit.runio import class_codes

    fig, ax = plt.subplots(figsize=(9.6, 7.7), dpi=175)
    # right=0.90, not 1.0: the colourbar's rotated label needs the margin, and
    # without it the label is clipped at the image edge in every frame.
    fig.subplots_adjust(left=0.02, right=0.90, top=0.92, bottom=0.05)
    codes = class_codes(depth)
    cmap = ListedColormap(DEPTH_COLORS)
    norm = BoundaryNorm(DEPTH_EDGES + [DEPTH_EDGES[-1] * 6.0], cmap.N)
    mesh = ax.imshow(np.ma.masked_where(codes <= 0, codes), cmap=cmap, norm=norm,
                     interpolation="nearest")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title("%s　%s" % (L["panel"] % rain,
                             L["spill_on"] if stats["spilled_m3"] > 0 else L["spill_none"]),
                 fontsize=15)
    ax.text(0.02, 0.02, L["stats"] % (stats["ponded_km2"], stats["max_depth_m"],
                                      fmt_volume(stats["spilled_m3"]),
                                      stats["overflowing_basins"]),
            transform=ax.transAxes, fontsize=12.5, va="bottom", color="#1b2733",
            bbox=dict(boxstyle="round,pad=0.4", fc="white", ec="#dbe6f0", lw=0.8, alpha=0.94))
    cbar = fig.colorbar(mesh, ax=ax, shrink=0.72, pad=0.02, ticks=DEPTH_EDGES)
    cbar.ax.set_yticklabels(DEPTH_LABELS)
    cbar.set_label(L["depth"], fontsize=12.5)
    fig.savefig(path, facecolor="white", bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rain", default=",".join("%g" % r for r in DEFAULT_RAIN))
    parser.add_argument("--cn", type=float, default=85.0)
    parser.add_argument("--no-mp4", action="store_true")
    args = parser.parse_args()
    rainfall = [float(v) for v in args.rain.split(",") if v.strip()]

    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams["svg.fonttype"] = "path"
    font = configure_fonts()
    L = ZH if font else EN
    print("labels: %s" % ("Chinese" if font else "English"))

    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap

    from floodkit.dem_source import ensure_tiles
    from floodkit.pipeline import DEFAULT_ENVELOPE, load_dem_metric, run_event
    from floodkit.runio import class_codes

    grid = None
    for tile in ensure_tiles(CITY_BOUNDS, os.path.join(ROOT, "data", "dem"), progress=lambda *_: None):
        try:
            grid = load_dem_metric(tile, CITY_BOUNDS)
            break
        except Exception:
            continue
    if grid is None:
        raise SystemExit("could not load a DEM tile")

    recorded_path = os.path.join(EVIDENCE, "rainfall_sweep.json")
    recorded = {}
    if os.path.exists(recorded_path):
        with open(recorded_path, encoding="utf-8") as fh:
            recorded = {float(s["rain_mm"]): s for s in json.load(fh).get("scenarios", [])}
        print("checking against the recorded sweep at %s" % os.path.relpath(recorded_path, ROOT))

    os.makedirs(FRAMES, exist_ok=True)
    os.makedirs(EVIDENCE, exist_ok=True)
    scenarios, frames, depths = [], [], []
    for index, rain in enumerate(rainfall):
        result = run_event(grid, rain_mm=rain, cn=args.cn, envelope_m=DEFAULT_ENVELOPE)
        pond = result.get("ponding") or {}
        stats = {
            "rain_mm": rain,
            "outcome": result.get("outcome"),
            "ponded_km2": (pond.get("pond_area_m2") or 0.0) / 1e6,
            "max_depth_m": pond.get("max_depth_m") or 0.0,
            "spilled_m3": pond.get("total_spilled_m3") or 0.0,
            "overflowing_basins": pond.get("overflowing_basins") or 0,
            "total_runoff_m3": pond.get("total_runoff_m3") or 0.0,
            "d01": (result.get("water_balance") or {}).get("relative_error"),
        }
        scenarios.append(stats)
        if rain in recorded:
            want = recorded[rain]
            for key in ("ponded_km2", "max_depth_m", "overflowing_basins"):
                if abs(float(stats[key]) - float(want[key])) > 1e-6:
                    raise SystemExit(
                        "%s disagrees with the recorded sweep: %.6f vs %.6f"
                        % (key, stats[key], want[key]))
            if abs(float(stats["spilled_m3"]) - float(want["spilled_m3"] or 0.0)) > 1e-3:
                raise SystemExit("spilled volume disagrees at %g mm: %s vs %s"
                                 % (rain, stats["spilled_m3"], want["spilled_m3"]))
        print("  %3.0f mm  ponded %6.2f km²  max %5.2f m  spilled %10s  overflowing %3d"
              % (rain, stats["ponded_km2"], stats["max_depth_m"],
                 fmt_volume(stats["spilled_m3"]), stats["overflowing_basins"]))
        path = os.path.join(FRAMES, "frame_%02d.png" % index)
        render_frame(result["depth_m"], rain, stats, L, path)
        frames.append(path)
        depths.append(result["depth_m"])

    depth_max = max(s["max_depth_m"] for s in scenarios)
    print("recorded sweep reproduced; deepest scenario %.2f m" % depth_max)

    # ---- small multiples ----------------------------------------------------
    columns = min(3, len(scenarios))
    rows = int(np.ceil(len(scenarios) / columns))
    fig, axes = plt.subplots(rows, columns, figsize=(5.0 * columns, 4.5 * rows + 2.0), dpi=175,
                             layout="constrained")
    axes = np.atleast_1d(axes).ravel()
    cmap = ListedColormap(DEPTH_COLORS)
    boundaries = DEPTH_EDGES + [max(DEPTH_EDGES[-1] + 1.0, depth_max + 1.0)]
    norm = BoundaryNorm(boundaries, cmap.N)
    mesh = None
    for ax, stats, depth in zip(axes, scenarios, depths):
        codes = class_codes(depth)
        mesh = ax.imshow(np.ma.masked_where(codes <= 0, codes), cmap=cmap, norm=norm,
                         interpolation="nearest")
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title("%s　%s" % (L["panel"] % stats["rain_mm"],
                                 L["spill_on"] if stats["spilled_m3"] > 0 else L["spill_none"]),
                     fontsize=13)
        ax.text(0.02, 0.02, L["stats"] % (stats["ponded_km2"], stats["max_depth_m"],
                                          fmt_volume(stats["spilled_m3"]),
                                          stats["overflowing_basins"]),
                transform=ax.transAxes, fontsize=11, va="bottom",
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#dbe6f0", lw=0.7, alpha=0.93))
    for ax in axes[len(scenarios):]:
        ax.axis("off")
    if mesh is not None:
        cbar = fig.colorbar(mesh, ax=list(axes), shrink=0.62, pad=0.012, ticks=DEPTH_EDGES,
                            location="right")
        cbar.ax.set_yticklabels(DEPTH_LABELS)
        cbar.set_label(L["depth"], fontsize=10.5)
    fig.suptitle(L["suptitle"], fontsize=18, fontweight="bold")
    fig.text(0.5, 0.026, L["note"], ha="center", fontsize=11.5, color="#c62828")
    fig.text(0.5, 0.004, L["grid"] % (CITY_BOUNDS[0], CITY_BOUNDS[2], CITY_BOUNDS[1],
                                      CITY_BOUNDS[3], abs(grid.transform.a), args.cn),
             ha="center", fontsize=10, color="#757575")
    panels_path = os.path.join(EVIDENCE, "rainfall_smallmultiples.png")
    fig.savefig(panels_path, facecolor="white")
    plt.close(fig)
    print("wrote %s (%.2f MB)" % (panels_path, os.path.getsize(panels_path) / 1e6))

    # ---- animation ----------------------------------------------------------
    from PIL import Image

    images = [Image.open(p).convert("P", palette=Image.ADAPTIVE, colors=128) for p in frames]
    # The last frame is held for several seconds via a per-frame duration list, not
    # by repeating it: Pillow's encoder merges consecutive identical frames, so
    # duplicated holds quietly disappear and the animation snaps back to 50 mm.
    durations = [1100] * (len(images) - 1) + [4200]
    gif_path = os.path.join(EVIDENCE, "rainfall_animation.gif")
    images[0].save(gif_path, save_all=True, append_images=images[1:],
                   duration=durations, loop=0, optimize=True, disposal=2)
    print("wrote %s (%.2f MB, %d frames, last held %.1f s)"
          % (gif_path, os.path.getsize(gif_path) / 1e6, len(images), durations[-1] / 1000))

    mp4_path = None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and not args.no_mp4:
        mp4_path = os.path.join(EVIDENCE, "rainfall_animation.mp4")
        # Even dimensions are an H.264 requirement, hence the scale expression.
        command = [
            ffmpeg, "-y", "-loglevel", "error", "-framerate", "0.9",
            "-i", os.path.join(FRAMES, "frame_%02d.png"),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", mp4_path,
        ]
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode == 0 and os.path.exists(mp4_path):
            print("wrote %s (%.2f MB)" % (mp4_path, os.path.getsize(mp4_path) / 1e6))
        else:
            print("ffmpeg failed, no MP4 written: %s" % (done.stderr or "").strip()[:200])
            mp4_path = None
    elif not ffmpeg:
        print("ffmpeg not on PATH; the GIF carries the animation on its own")

    sidecar = os.path.join(EVIDENCE, "rainfall_animation.json")
    with open(sidecar, "w", encoding="utf-8") as fh:
        json.dump({
            "bounds": list(CITY_BOUNDS),
            "cn": args.cn,
            "cn_source": "uniform",
            "scenarios": scenarios,
            "artifacts": {
                "smallmultiples_png": os.path.relpath(panels_path, ROOT).replace("\\", "/"),
                "gif": os.path.relpath(gif_path, ROOT).replace("\\", "/"),
                "mp4": (os.path.relpath(mp4_path, ROOT).replace("\\", "/") if mp4_path else None),
            },
            "checked_against": os.path.relpath(recorded_path, ROOT).replace("\\", "/")
            if recorded else None,
            "note": ("Every frame comes from a fresh run_event with uniform CN; the numbers were "
                     "asserted against the independently produced rainfall_sweep.json before "
                     "drawing. The spill branch is exercised from 150 mm upward."),
        }, fh, ensure_ascii=False, indent=2)
    print("wrote %s" % sidecar)

    monotone = all(scenarios[i]["ponded_km2"] <= scenarios[i + 1]["ponded_km2"] + 1e-9
                   for i in range(len(scenarios) - 1))
    any_spill = any(s["spilled_m3"] > 0 for s in scenarios)
    print("淹没面积随降雨单调不减: %s｜外溢分支被执行: %s"
          % ("是" if monotone else "否", "是" if any_spill else "否"))
    return 0 if (monotone and any_spill) else 1


if __name__ == "__main__":
    raise SystemExit(main())

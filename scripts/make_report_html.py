"""Render a self-contained interactive HTML report for one finished run.

Why this exists
---------------
The run already produces a good report-grade map (`_report_map.png`), a risk plot,
a story figure, a GeoTIFF and two reports. What none of them do is let a reader
*interrogate* the result: find the deepest cell, read a depth at a point, ask
"what does the terrain look like underneath this underpass", or check which
indicator passed and where its number comes from.

This writes one HTML file that answers those questions and opens by double-click.

Design constraints, in order of importance
------------------------------------------
1. **Fully offline.** No CDN, no tile server, no webfont, no fetch(). The basemap
   is baked to PNG and inlined as base64. A judge on a laptop in airplane mode, or
   a demo venue with hostile wifi, must still see the map.
2. **Zero new dependencies.** numpy / matplotlib / rasterio / Pillow are already
   installed by scripts/setup.ps1. folium and plotly are deliberately avoided:
   they pull a CDN by default and would add transitive dependencies to setup.
3. **Single source of truth.** Every number on the page is read from the run's
   report JSON or its depth raster. Nothing is retyped. The underpass and facility
   counts are additionally *recomputed* from the cached OSM geometries with the
   same functions the analysis used, and the script fails if they disagree with
   the report — a renderer that silently disagrees with the report is worse than
   no renderer.

    python scripts/make_report_html.py [tag] [--scale 5] [--out path]

Default tag is nanjing_final. Writes data/runs/<tag>/<tag>_report.html.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys

# The dependency path is bootstrapped before the first third-party import: the
# project keeps numpy/scipy/matplotlib/rasterio in .pylibs rather than in the
# interpreter, so importing numpy first would fail on a bare interpreter.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

# matplotlib rebuilds a font cache on first use and writes it to ~/.matplotlib by
# default. Under a restricted sandbox that write is denied, which is noisy but
# worse, it makes the first run of this script behave differently from the
# second. Point it at a directory inside the project instead.
os.environ.setdefault("MPLCONFIGDIR", os.path.join(ROOT, ".mplconfig"))

import numpy as np  # noqa: E402

# Elevation is only needed for the terrain profile, at 10 cm resolution.
ELEV_SCALE = 10.0
ELEV_INVALID = 65535

# Douglas-Peucker tolerance, in grid cells, for the vector overlays. 0.15 cell is
# well below the 28 m data resolution, so this removes coordinate noise rather
# than shape.
SIMPLIFY_TOL = 0.15

BG = "#f7f7f5"
ROAD_STYLE = {
    "motorway": (1.5 * 1.35, "#9e9e9e"),
    "trunk": (1.5 * 1.35, "#9e9e9e"),
    "primary": (1.1 * 1.35, "#ababab"),
    "secondary": (0.8 * 1.35, "#bdbdbd"),
    "tertiary": (0.8 * 1.35, "#bdbdbd"),
}
ROAD_DEFAULT = (0.5 * 1.35, "#d5d5d5")


# --------------------------------------------------------------------------- io
# Reading a finished run lives in floodkit.runio so that the printed map, the
# interactive page and the GeoJSON export cannot disagree about which OSM cache
# file a run used or where its footprint is.

from floodkit.runio import (  # noqa: E402
    DEPTH_COLORS,
    DEPTH_CODE_CEILING,
    DEPTH_EDGES,
    DEPTH_LABELS,
    DEPTH_SCALE,
    artifact,
    bounds_wgs84,
    code_class_table,
    encode_depth,
    load_run,
    load_water_mask,
    lonlat_lookup,
    select_osm,
    simplify,
)

# ------------------------------------------------------------------- elevation


def elevation_on_grid(bounds, transform, crs, shape):
    """Mosaic every DEM tile onto the run's exact grid.

    Two things this has to get right, both learned by testing a wider run:

    * **All tiles, not the first one.** A study area can straddle a tile edge; the
      first tile that happens to load can cover a 35-row sliver of it, which
      silently turns the terrain profile into a flat line over most of the map.
      Each tile is reprojected in turn and only fills cells still empty.
    * **The run's own grid, not a recomputed one.** A half-cell offset would put
      the terrain profile under the wrong water. Returns None when no DEM is
      available, and reports how much of the grid ended up covered.
    """
    try:
        from rasterio.warp import Resampling, reproject

        from floodkit.dem_source import ensure_tiles
        from floodkit.pipeline import load_dem_metric
    except Exception as exc:  # noqa: BLE001
        print("elevation layer skipped (import): %s" % exc)
        return None

    try:
        tiles = ensure_tiles(bounds, os.path.join(ROOT, "data", "dem"), progress=lambda *_: None)
    except Exception as exc:  # noqa: BLE001 - offline runs must still render
        print("elevation layer skipped (no DEM cache and no network): %s" % exc)
        return None

    rows, cols = shape
    dst = np.full((rows, cols), np.nan, dtype="float64")
    used = 0
    for tile in tiles:
        try:
            grid = load_dem_metric(tile, bounds)
        except Exception:
            continue
        same_grid = grid.shape == (rows, cols) and np.allclose(
            [grid.transform.a, grid.transform.b, grid.transform.c,
             grid.transform.d, grid.transform.e, grid.transform.f],
            [transform.a, transform.b, transform.c, transform.d, transform.e, transform.f],
        )
        if same_grid:
            layer = grid.elevation
        else:
            layer = np.full((rows, cols), np.nan, dtype="float64")
            reproject(
                source=grid.elevation,
                destination=layer,
                src_transform=grid.transform,
                src_crs=grid.crs,
                dst_transform=transform,
                dst_crs=crs,
                src_nodata=grid.nodata,
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
        fill = ~np.isfinite(dst) & np.isfinite(layer)
        if fill.any():
            dst[fill] = layer[fill]
            used += 1
        if np.isfinite(dst).all():
            break

    if used == 0:
        print("elevation layer skipped: no DEM tile overlaps the study area")
        return None
    coverage = float(np.isfinite(dst).mean())
    print("  elevation: %d/%d tiles mosaicked, %.1f%% of the grid covered"
          % (used, len(tiles), 100 * coverage))
    return dst


# ------------------------------------------------------------------- basemap


def bake_layers(bounds, transform, crs, shape, scale, water, roads_path, buildings_path):
    """Bake two transparent PNGs that line up one-to-one with the data grid.

    The axes span exactly [0, cols] x [rows, 0], so grid coordinate (c, r) maps
    to image pixel (c*scale, r*scale) with no offset to get wrong later.
    """
    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    import matplotlib.pyplot as plt

    from floodkit.exposure import load_features

    rows, cols = shape
    dpi = 100
    lons, lats = [], []

    def to_grid(points):
        from rasterio.warp import transform as warp_transform

        lons[:] = [p["lon"] for p in points]
        lats[:] = [p["lat"] for p in points]
        xs, ys = warp_transform("EPSG:4326", str(crs), lons, lats)
        inverse = ~transform
        return [inverse * (x, y) for x, y in zip(xs, ys)]

    def new_fig():
        fig = plt.figure(figsize=(cols * scale / dpi, rows * scale / dpi), dpi=dpi)
        fig.patch.set_alpha(0.0)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, cols)
        ax.set_ylim(rows, 0)
        ax.set_axis_off()
        ax.patch.set_alpha(0.0)
        return fig, ax

    def to_png(fig):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", transparent=True, dpi=dpi)
        plt.close(fig)
        return buf.getvalue()

    # ---- layer 1: water and buildings ---------------------------------------
    fig, ax = new_fig()
    if water is not None and water.any():
        rgba = np.zeros(water.shape + (4,), dtype="float32")
        rgba[..., 0], rgba[..., 1], rgba[..., 2] = 0.66, 0.81, 0.90
        rgba[..., 3] = water.astype("float32")
        ax.imshow(rgba, extent=(0, cols, rows, 0), zorder=1)
    if buildings_path:
        for el in load_features(buildings_path):
            geom = el.get("geometry") or []
            if len(geom) < 4:
                continue
            pts = to_grid([p for p in geom if "lon" in p])
            if len(pts) < 3:
                continue
            ax.fill([p[0] for p in pts], [p[1] for p in pts],
                    color="#efece6", linewidth=0, zorder=2)
    base_png = to_png(fig)

    # ---- layer 2: roads -----------------------------------------------------
    fig, ax = new_fig()
    if roads_path:
        by_style = {}
        for el in load_features(roads_path):
            geom = el.get("geometry") or []
            if len(geom) < 2:
                continue
            pts = to_grid([p for p in geom if "lon" in p])
            if len(pts) < 2:
                continue
            highway = (el.get("tags") or {}).get("highway")
            by_style.setdefault(ROAD_STYLE.get(highway, ROAD_DEFAULT), []).append(pts)
        for (lw, colour), lines in sorted(by_style.items(), key=lambda kv: kv[0][0]):
            for pts in lines:
                ax.plot([p[0] for p in pts], [p[1] for p in pts], color=colour,
                        linewidth=lw, solid_capstyle="round", zorder=3)
    roads_png = to_png(fig)
    return base_png, roads_png


def quantise_png(data: bytes):
    """Reduce a flat-colour line-art PNG to a small palette.

    The basemap is a handful of solid tones; truecolour PNG spends three bytes
    per pixel to say so. Quantising cuts the largest payload in the file by
    roughly an order of magnitude with no visible difference at these colours.
    """
    try:
        from PIL import Image
    except Exception:
        return data
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    alpha = img.getchannel("A")
    quant = img.convert("RGB").quantize(colors=32, method=Image.MEDIANCUT, dither=Image.NONE)
    quant = quant.convert("RGBA")
    quant.putalpha(alpha.point(lambda v: 255 if v > 8 else 0))
    # A hard alpha cut keeps the quantiser from inventing a halo of near-zero
    # alpha pixels, which would both inflate the file and grey the map edges.
    out = io.BytesIO()
    quant.save(out, format="PNG", optimize=True)
    return out.getvalue() if out.tell() < len(data) else data


# -------------------------------------------------------------------- vectors



FOOT_TYPES = ("footway", "pedestrian", "path", "steps", "cycleway")


def build_vectors(report, depth, transform, crs, shape, bounds, osm_paths):
    """Assemble the drawable layers, checking every count against the report.

    The layer data comes from floodkit.exposure rather than from a second copy of
    its sampling rules. An earlier version reimplemented the walk here to reach the
    sections the report drops, which left two implementations of "how deep is this
    underpass" that could only be kept in step by hand.

    The report keeps the top 30 underpass sections and the top 30 facilities; the
    map has to show all 420 and all 146. Asking for the whole layer and *asserting*
    the counts against the report is what makes drawing the full set safe: a
    renderer that quietly disagrees with the JSON is worse than no renderer.
    """
    from floodkit.exposure import (
        critical_facility_impact,
        load_features,
        underpass_segments,
    )

    min_depth = 0.05  # the default attach_exposure uses
    notes = []
    vectors = {"underpasses": [], "facilities": [], "basins": []}
    exposure = report.get("exposure") or {}

    # ---- underpasses: every section, flooded or not --------------------------
    roads_path = osm_paths.get("roads")
    if roads_path:
        raw = underpass_segments(
            depth, load_features(roads_path), transform, abs(transform.a), min_depth,
            include_unflooded=True, include_geometry=True,
        )
        # exposure returns source order so its totals reproduce the report bit for
        # bit; a decision list needs severity order instead.
        segs = []
        for entry in raw:
            pts = to_grid(entry["geometry_lonlat"], transform)
            segs.append({
                "id": entry["osm_id"],
                "pts": [[round(x, 2), round(y, 2)] for x, y in simplify(pts, SIMPLIFY_TOL)],
                "name": entry["name"],
                "highway": entry["highway"],
                "layer": entry["layer"],
                "flooded": entry["flooded_length_m"] > 0,
                "fd": round(entry["flooded_length_m"], 1),
                "d": round(entry["max_depth_m"], 2),
                "risk": entry["risk"],
                "vehicle": (entry["highway"] or "") not in FOOT_TYPES,
            })
        segs.sort(key=lambda s: (-s["d"], -s["fd"]))
        want = exposure.get("underpasses") or {}
        if want.get("underpass_ways") is not None and want["underpass_ways"] != len(segs):
            raise SystemExit("underpass segment count disagrees with the report: %d vs %d"
                             % (len(segs), want["underpass_ways"]))
        flooded = sum(1 for s in segs if s["flooded"])
        if (want.get("underpass_ways_flooded") is not None
                and want["underpass_ways_flooded"] != flooded):
            raise SystemExit(
                "underpass recomputation disagrees with the report: %d vs %d flooded sections. "
                "The page must not publish a map that contradicts the report."
                % (flooded, want["underpass_ways_flooded"]))
        vectors["underpasses"] = segs
    else:
        notes.append("OSM \u8def\u7f51\u7f13\u5b58\u7f3a\u5931\uff0c\u4e0b\u7a7f\u96a7\u9053\u56fe\u5c42\u672a\u7ed8\u5236")

    # ---- critical facilities -------------------------------------------------
    critical_path = osm_paths.get("critical")
    if critical_path:
        impact = critical_facility_impact(
            depth, load_features(critical_path), transform, abs(transform.a), min_depth,
            top_n=None,
        )
        facilities = []
        for fac in impact["top_facilities"]:
            col, row = project_one(fac["lon"], fac["lat"], transform)
            facilities.append({
                "c": round(col, 2), "r": round(row, 2),
                "lon": fac["lon"], "lat": fac["lat"],
                "name": fac["name"],
                "kind": fac["kind"],
                "d": round(float(fac["depth_m"]), 3),
                "id": fac["osm_id"],
            })
        facilities.sort(key=lambda f: -f["d"])
        want_f = (exposure.get("critical_facilities") or {}).get("facilities_flooded")
        if want_f is not None and want_f != len(facilities):
            raise SystemExit("facility recomputation disagrees with the report: %d vs %d"
                             % (len(facilities), want_f))
        vectors["facilities"] = facilities
    else:
        notes.append("OSM \u5173\u952e\u8bbe\u65bd\u7f13\u5b58\u7f3a\u5931\uff0c\u8bbe\u65bd\u56fe\u5c42\u672a\u7ed8\u5236")

    # ---- basins the model itself flags for a site visit ----------------------
    basins = []
    for basin in report.get("basins") or []:
        if not (basin.get("verify_required") or basin.get("outside_validity")):
            continue
        col, row = project_one(basin["lon"], basin["lat"], transform)
        basins.append({
            "c": round(col, 2), "r": round(row, 2),
            "d": round(float(basin.get("max_depth_m", 0.0)), 2),
            "area": round(float(basin.get("pond_area_m2", 0.0))),
            "verify": bool(basin.get("verify_required")),
            "outside": bool(basin.get("outside_validity")),
            "lon": basin["lon"], "lat": basin["lat"],
        })
    basins.sort(key=lambda b: -b["d"])
    vectors["basins"] = basins
    return vectors, notes


def to_grid(points, transform):
    """WGS84 -> grid coordinates for a list of (lon, lat) pairs."""
    from rasterio.warp import transform as warp_transform

    if not points:
        return []
    xs, ys = warp_transform("EPSG:4326", "EPSG:32650",
                            [p[0] for p in points], [p[1] for p in points])
    inverse = ~transform
    return [inverse * (x, y) for x, y in zip(xs, ys)]


def project_one(lon, lat, transform):
    (point,) = to_grid([(lon, lat)], transform)
    return point


# --------------------------------------------------------------------- payload


def encode_arrays(depth, elevation, shape):
    """Pack the two rasters the browser needs into base64 bytes."""
    codes = encode_depth(depth)
    payload = {"depth": base64.b64encode(codes.tobytes()).decode("ascii")}
    if elevation is not None:
        valid = np.isfinite(elevation)
        lo = float(elevation[valid].min()) if valid.any() else 0.0
        codes_e = np.full(shape, ELEV_INVALID, dtype="<u2")
        scaled = np.clip(np.round((elevation[valid] - lo) * ELEV_SCALE), 0, ELEV_INVALID - 1)
        if scaled.max() >= ELEV_INVALID:
            raise SystemExit("elevation range too wide for the 10 cm encoding")
        codes_e[valid] = scaled.astype("<u2")
        payload["elev"] = base64.b64encode(codes_e.tobytes()).decode("ascii")
        payload["elev_offset"] = lo
    else:
        payload["elev"] = None
        payload["elev_offset"] = 0.0
    return payload


def report_subset(report, tag, cell_m, shape, bounds):
    """The part of the report the page shows, kept verbatim."""
    exposure = report.get("exposure") or {}
    up = exposure.get("underpasses") or {}
    crit = exposure.get("critical_facilities") or {}
    roads = exposure.get("roads") or {}
    buildings = exposure.get("buildings") or {}

    cn_source = "landcover" if report.get("landcover") else "uniform"
    command = (
        "python -X utf8 -m floodkit run --bounds %.4f %.4f %.4f %.4f "
        "--rain-mm %g --cn %g --cn-source %s --tag %s%s"
        % (bounds[0], bounds[1], bounds[2], bounds[3],
           report.get("rainfall_mm", 0.0), round(report.get("curve_number_mean", 0.0)),
           cn_source, tag, " --exposure" if exposure.get("available") else "")
    )

    return {
        "tag": tag,
        "outcome": report.get("outcome"),
        "status": report.get("status"),
        "rainfall_mm": report.get("rainfall_mm"),
        "cn_mean": report.get("curve_number_mean"),
        "domain": report.get("domain"),
        "terrain": report.get("terrain"),
        "runoff": report.get("runoff"),
        "ponding": report.get("ponding"),
        "water_balance": report.get("water_balance"),
        "checks": report.get("checks"),
        "quality": report.get("quality"),
        "basin_qc": report.get("basin_qc"),
        "landcover": report.get("landcover"),
        "sensitivity": report.get("sensitivity"),
        "exposure_available": bool(exposure.get("available")),
        "roads": roads,
        "buildings": buildings,
        "underpass_summary": up,
        "critical_summary": crit,
        "cell_m": cell_m,
        "rows": shape[0],
        "cols": shape[1],
        "bounds": [round(b, 6) for b in bounds],
        "reproduce": command,
    }


# ------------------------------------------------------------------------ html


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --ink:#1b2733; --ink2:#4a5b6b; --ink3:#7b8b99; --line:#dfe4e8;
  --bg:#f7f7f5; --panel:#ffffff; --accent:#1f5fa9; --ok:#1b7f4b; --bad:#c62828;
  --warn:#b26a00; --chip:#eef3f8;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{
  font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei","Hiragino Sans GB",
       "Source Han Sans SC","Noto Sans CJK SC",sans-serif;
  color:var(--ink);background:#eef1f3;-webkit-font-smoothing:antialiased;
}
#app{display:flex;height:100vh;overflow:hidden}
#side{
  width:396px;min-width:396px;height:100%;overflow-y:auto;
  background:var(--panel);border-right:1px solid var(--line);
}
#main{flex:1;position:relative;min-width:0}
#map{display:block;width:100%;height:100%;background:var(--bg);cursor:crosshair}
#map.panning{cursor:grabbing}
h1{font-size:16px;margin:0;letter-spacing:.2px}
.sec{padding:14px 18px;border-bottom:1px solid var(--line)}
.sec h2{
  font-size:11px;text-transform:uppercase;letter-spacing:.11em;color:var(--ink3);
  margin:0 0 9px;font-weight:600;
}
.head{padding:16px 18px 14px;border-bottom:1px solid var(--line)}
.head .sub{color:var(--ink2);font-size:12px;margin-top:5px}
.head .tag{
  display:inline-block;font:11px ui-monospace,Consolas,monospace;color:var(--ink2);
  background:var(--chip);border:1px solid var(--line);border-radius:4px;
  padding:1px 6px;margin-top:7px;
}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.kpi{background:#fafbfc;border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.kpi b{display:block;font-size:17px;font-weight:650;letter-spacing:-.2px}
.kpi span{font-size:11px;color:var(--ink3)}
.kpi.wide{grid-column:1/-1}
table.ind{width:100%;border-collapse:collapse;font-size:12px}
table.ind td{padding:5px 0;border-bottom:1px dashed var(--line);vertical-align:top}
table.ind tr:last-child td{border-bottom:none}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.pass{background:var(--ok)}.fail{background:var(--bad)}.na{background:#b9c2c9}
.mono{font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:11px}
.val{text-align:right;white-space:nowrap;color:var(--ink2)}
.note{font-size:11.5px;color:var(--ink3);margin-top:8px;line-height:1.6}
.note code{background:var(--chip);padding:1px 4px;border-radius:3px;font-size:10.5px}
.row{
  display:flex;justify-content:space-between;gap:10px;padding:6px 8px;border-radius:5px;
  cursor:pointer;border:1px solid transparent;font-size:12.5px;
}
.row:hover{background:#f2f6fa;border-color:#dbe6f0}
.row.sel{background:#e8f1fa;border-color:#b8d4ec}
.row .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .dp{font-family:ui-monospace,Consolas,monospace;color:var(--ink2);white-space:nowrap}
.pill{
  display:inline-block;font-size:10px;padding:0 5px;border-radius:8px;margin-left:6px;
  border:1px solid currentColor;line-height:15px;vertical-align:1px;
}
.p-life{color:#c62828}.p-watch{color:#b26a00}.p-dry{color:#8a949c}
.p-out{color:#6a4fc0}
.ctl{display:flex;flex-wrap:wrap;gap:6px;padding:10px 18px;border-bottom:1px solid var(--line);
     background:#fcfdfe;position:sticky;top:0;z-index:5}
button{
  font:12px inherit;color:var(--ink);background:#fff;border:1px solid var(--line);
  border-radius:5px;padding:5px 9px;cursor:pointer;
}
button:hover{border-color:#b9c6d2;background:#f7fafc}
button.on{background:var(--accent);border-color:var(--accent);color:#fff}
button.mini{padding:3px 7px;font-size:11px}
label.sw{display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer;
         padding:4px 8px;border:1px solid var(--line);border-radius:5px;background:#fff}
label.sw input{margin:0}
#tip{
  position:absolute;pointer-events:none;background:rgba(20,30,40,.93);color:#fff;
  font:12px/1.5 inherit;padding:6px 9px;border-radius:5px;white-space:nowrap;
  transform:translate(12px,12px);display:none;z-index:20
}
#tip .m{font-family:ui-monospace,Consolas,monospace;color:#cfe3ff}
#hud{
  position:absolute;left:12px;bottom:12px;background:rgba(255,255,255,.94);
  border:1px solid var(--line);border-radius:6px;padding:7px 10px;
  font:11.5px/1.6 ui-monospace,Consolas,monospace;color:var(--ink2);z-index:10
}
#legend{
  position:absolute;right:12px;top:12px;background:rgba(255,255,255,.95);
  border:1px solid var(--line);border-radius:6px;padding:9px 11px;font-size:11.5px;
  min-width:150px;z-index:10
}
#legend .t{font-weight:600;margin-bottom:6px;font-size:11px;letter-spacing:.06em;color:var(--ink3)}
#legend .li{display:flex;align-items:center;gap:6px;margin:3px 0}
#legend i{width:15px;height:10px;border-radius:2px;display:inline-block}
#legend .ln{width:15px;height:0;border-top-width:2.5px;border-top-style:solid}
#profileWrap{border-top:1px solid var(--line);padding:10px 14px 14px;display:none}
#profileWrap.on{display:block}
#profile{width:100%;height:196px;display:block}
#detail{font-size:12.5px}
#detail .k{color:var(--ink3);font-size:11px}
.empty{color:var(--ink3);font-size:12px;font-style:normal}
a{color:var(--accent)}
.foot{padding:12px 18px 26px;color:var(--ink3);font-size:11px;line-height:1.65}
.bar{
  position:absolute;left:50%;bottom:14px;transform:translateX(-50%);
  background:rgba(30,40,50,.9);color:#fff;padding:6px 12px;border-radius:16px;
  font-size:12px;z-index:30;display:none
}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <div class="head">
      <h1>宁讯智鉴 · 交互式内涝报告</h1>
      <div class="sub" id="subtitle"></div>
      <div class="tag" id="tag"></div>
    </div>

    <div class="ctl" id="ctl"></div>

    <div class="sec">
      <h2>关键结论</h2>
      <div class="kpis" id="kpis"></div>
    </div>

    <div class="sec">
      <h2>机器可判指标</h2>
      <table class="ind" id="indicators"></table>
      <div class="note" id="indNote"></div>
    </div>

    <div class="sec" id="riskSec">
      <h2>最需处置：下穿隧道 / 地下通道</h2>
      <div id="riskUnder"></div>
      <h2 style="margin-top:14px">最需处置：关键设施</h2>
      <div id="riskFac"></div>
      <div class="note">
        列表与地图联动：点击任一项定位到该点位。完整清单见同目录
        <span class="mono">_report.json</span>（<span class="mono">top_segments</span> /
        <span class="mono">top_facilities</span>）。
      </div>
    </div>

    <div class="sec">
      <h2>点位详情</h2>
      <div id="detail" class="empty">把鼠标移到地图上读取该点水深与高程；点击橙色设施或红色下穿段查看详情。</div>
    </div>

    <div class="sec">
      <h2>地形 / 水面剖面</h2>
      <div class="note" id="profHint">
        打开上方「剖面」后，在地图上点两个点，即可切出该线段的地形高程与水面线。
      </div>
    </div>

    <div id="profileWrap"><canvas id="profile"></canvas></div>

    <div class="sec">
      <h2>输入质量</h2>
      <div id="quality"></div>
    </div>

    <div class="sec">
      <h2>复算方式</h2>
      <div class="mono" style="word-break:break-all;background:var(--chip);padding:8px;border-radius:5px"
        id="reproduce"></div>
      <div class="note">
        本页所有数字均由上述运行产物的 <span class="mono">report.json</span> 与
        <span class="mono">depth.tif</span> 直接读取，未手工录入；
        下穿段与设施图层由同一套 <span class="mono">floodkit.exposure</span> 函数用缓存 OSM
        数据重新计算，数量与报告不一致时本页拒绝生成。
      </div>
    </div>

    <div class="foot" id="foot"></div>
  </aside>

  <main id="main">
    <canvas id="map"></canvas>
    <div id="tip"></div>
    <div id="hud"></div>
    <div id="legend"></div>
  </main>
</div>

<script type="application/json" id="data">__DATA__</script>
<script>
__SCRIPT__
</script>
</body>
</html>
"""


SCRIPT_TEMPLATE = r"""
/* A page whose script dies leaves an empty shell that looks like "no flooding"
   rather than like a broken report. Catch it and say so on the page. */
(function () {
  var box = document.createElement('div');
  box.id = 'fatal';
  box.style.cssText = 'position:fixed;inset:auto 12px 12px 12px;z-index:99;background:#fff3f3;' +
    'border:1px solid #e0b4b4;color:#8a1f1f;padding:10px 12px;border-radius:6px;' +
    'font:12px/1.6 ui-monospace,Consolas,monospace;white-space:pre-wrap;display:none';
  window.addEventListener('error', function (e) {
    box.style.display = 'block';
    box.textContent = '本页脚本执行失败，地图与数字未渲染：' + (e.message || e.error) +
      '\n' + (e.filename || '') + ':' + (e.lineno || '') +
      '\n请把这条信息连同文件名一起反馈。';
    document.body.appendChild(box);
  });
})();

(function () {
"use strict";

/* ---------------------------------------------------------------- payload */
const D = JSON.parse(document.getElementById('data').textContent);
const M = D.meta, R = D.report, V = D.vectors;
const COLS = R.cols, ROWS = R.rows, CELL_M = R.cell_m;
const COLORS = M.colors, LABELS = M.labels;
const DEPTH_SCALE = M.depth_scale, ELEV_SCALE = M.elev_scale;
const ELEV_INVALID = 65535, ELEV_OFFSET = M.elev_offset;

function b64bytes(b64) {
  const bin = atob(b64), n = bin.length, out = new Uint8Array(n);
  for (let i = 0; i < n; i++) out[i] = bin.charCodeAt(i);
  return out;
}
const DEPTH = b64bytes(D.depth);
const ELEV = D.elev ? b64bytes(D.elev) : null;
const ELEV_VIEW = ELEV ? new DataView(ELEV.buffer) : null;

/* Reading elevation through an explicit little-endian DataView rather than an
   Int16Array: the file is written by numpy as '<u2', and silently interpreting
   it with the host's byte order would turn a terrain profile into noise on any
   big-endian machine. */
function elevAt(c, r) {
  if (!ELEV_VIEW) return NaN;
  const i = (r * COLS + c) * 2;
  const v = ELEV_VIEW.getUint16(i, true);
  return v === ELEV_INVALID ? NaN : v / ELEV_SCALE + ELEV_OFFSET;
}
/* Decoding matches floodkit.runio.code_to_depth: code 0 is dry, code 1 is the
   trace band and decodes to its midpoint, codes 2+ take the bottom of their step.
   The max() is what expresses that, and it is the only place the two sides have
   to agree beyond the class table itself. */
function depthAt(c, r) {
  if (c < 0 || r < 0 || c >= COLS || r >= ROWS) return 0;
  const code = DEPTH[r * COLS + c];
  return code === 0 ? 0 : Math.max(code - 1, 0.5) / DEPTH_SCALE;
}
const NCLASS = COLORS.length;
/* The depth classification is a table shipped from Python (floodkit.runio's
   code_class_table), not a formula written again here. An earlier version
   re-derived the class with "first break greater than the value", which shifted
   every band by one and painted 0.1 m cells with the 0.2–0.5 m colour: invisible
   on screen, because both are blue. Entry 0 means "below the threshold". */
const CLASS_OF_CODE = M.class_of_code;
function classOfCode(code) { return CLASS_OF_CODE[code]; }
const HEX = COLORS.map(h => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]);

/* --------------------------------------------------------------- geo lookup */
function lonlatAt(c, r) {
  const L = M.lonlat, cs = L.cols, rs = L.rows;
  let i = 0; while (i < cs.length - 2 && cs[i + 1] <= c) i++;
  let j = 0; while (j < rs.length - 2 && rs[j + 1] <= r) j++;
  const tx = Math.min(1, Math.max(0, (c - cs[i]) / Math.max(1e-9, cs[i + 1] - cs[i])));
  const ty = Math.min(1, Math.max(0, (r - rs[j]) / Math.max(1e-9, rs[j + 1] - rs[j])));
  const at = (jj, ii) => jj * cs.length + ii;
  const mix = (a, b, t) => a + (b - a) * t;
  const lon = mix(mix(L.lon[at(j, i)], L.lon[at(j, i + 1)], tx),
                  mix(L.lon[at(j + 1, i)], L.lon[at(j + 1, i + 1)], tx), ty);
  const lat = mix(mix(L.lat[at(j, i)], L.lat[at(j, i + 1)], tx),
                  mix(L.lat[at(j + 1, i)], L.lat[at(j + 1, i + 1)], tx), ty);
  return [lon, lat];
}

/* ------------------------------------------------------------------- canvas */
const canvas = document.getElementById('map');
const ctx = canvas.getContext('2d');
const tip = document.getElementById('tip');
const hud = document.getElementById('hud');
let DPR = window.devicePixelRatio || 1;
const view = { s: 2, ox: 0, oy: 0, min: 0.6, max: 40 };
/* The basemap is a raster baked at M.base_scale image pixels per model cell. Zooming
   past that asks the browser to invent pixels, which is exactly the "the map goes
   blurry when I zoom in" complaint. The depth layer is drawn from raw class codes
   and stays crisp at any zoom; only the baked motorway/building layer has a floor. */
const MAX_ZOOM = Math.max(6, M.base_scale);
const layers = { base: true, roads: true, depth: true, under: true, fac: true, dry: false,
                 basin: true, labels: true };
let depthAlpha = 0.88;
let selected = null;
let profileMode = false, profilePts = [];

const IMG_BASE = new Image(), IMG_ROADS = new Image();
IMG_BASE.src = 'data:image/png;base64,' + D.base_png;
IMG_ROADS.src = 'data:image/png;base64,' + D.roads_png;

/* The depth layer is composited in the browser from the raw class codes rather
   than baked as a third PNG, so the opacity slider and the class legend stay
   honest: they both read from the same array the hover readout does. */
const depthCanvas = document.createElement('canvas');
depthCanvas.width = COLS; depthCanvas.height = ROWS;
(function paintDepth() {
  const id = depthCanvas.getContext('2d').createImageData(COLS, ROWS);
  const px = id.data;
  for (let i = 0; i < COLS * ROWS; i++) {
    const o = i * 4;
    const cls = classOfCode(DEPTH[i]);
    if (!cls) { px[o + 3] = 0; continue; }
    const rgb = HEX[cls - 1];
    px[o] = rgb[0]; px[o + 1] = rgb[1]; px[o + 2] = rgb[2]; px[o + 3] = 255;
  }
  depthCanvas.getContext('2d').putImageData(id, 0, 0);
})();

function resize() {
  DPR = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = Math.round(w * DPR);
  canvas.height = Math.round(h * DPR);
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  draw();
}
function fit() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const s = Math.min(w / COLS, h / ROWS) * 0.96;
  view.s = s;
  view.min = s * 0.5;
  view.max = MAX_ZOOM;
  view.ox = (w - COLS * s) / 2;
  view.oy = (h - ROWS * s) / 2;
  draw();
}
const toScreen = (c, r) => [view.ox + c * view.s, view.oy + r * view.s];
const toGrid = (x, y) => [(x - view.ox) / view.s, (y - view.oy) / view.s];

/* ------------------------------------------------------------------ drawing */
function draw() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = M.bg; ctx.fillRect(0, 0, w, h);

  const dw = COLS * view.s, dh = ROWS * view.s;
  if (layers.base && IMG_BASE.complete) {
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(IMG_BASE, view.ox, view.oy, dw, dh);
  }
  if (layers.roads && IMG_ROADS.complete) {
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(IMG_ROADS, view.ox, view.oy, dw, dh);
  }
  if (layers.depth) {
    /* Nearest-neighbour on purpose: every visible square is one 28 m model
       cell. A smoothed edge would suggest a spatial resolution the model does
       not have. */
    ctx.imageSmoothingEnabled = false;
    ctx.globalAlpha = depthAlpha;
    ctx.drawImage(depthCanvas, view.ox, view.oy, dw, dh);
    ctx.globalAlpha = 1;
  }

  if (layers.dry) drawUnderpasses(false);
  if (layers.under) drawUnderpasses(true);
  if (layers.basin) drawBasins();
  if (layers.fac) drawFacilities();
  drawProfilePts();
  drawScaleBar();
}

function lineWidth(px) { return Math.max(0.6, Math.min(4, px * Math.max(0.55, Math.min(2, view.s / 2.2)))); }

function drawUnderpasses(floodedPass) {
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  for (const s of V.underpasses) {
    if (floodedPass ? !s.flooded : s.flooded) continue;
    if (s.flooded && selected && selected.type === 'under' && selected.o.id === s.id) continue;
    ctx.beginPath();
    for (let i = 0; i < s.pts.length; i++) {
      const p = toScreen(s.pts[i][0], s.pts[i][1]);
      i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]);
    }
    if (s.flooded) {
      ctx.strokeStyle = s.risk === 'life-safety' ? '#c62828' : '#e07b0a';
      ctx.globalAlpha = 0.92;
      ctx.lineWidth = lineWidth(s.risk === 'life-safety' ? 2.2 : 1.6);
    } else {
      ctx.strokeStyle = '#9aa6b0'; ctx.globalAlpha = 0.55; ctx.lineWidth = lineWidth(0.9);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }
  const sel = selected && selected.type === 'under' ? selected.o : null;
  if (sel) {
    ctx.beginPath();
    for (let i = 0; i < sel.pts.length; i++) {
      const p = toScreen(sel.pts[i][0], sel.pts[i][1]);
      i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]);
    }
    ctx.strokeStyle = '#111'; ctx.lineWidth = lineWidth(4.5); ctx.globalAlpha = .35; ctx.stroke();
    ctx.strokeStyle = '#ffd54f'; ctx.lineWidth = lineWidth(2.2); ctx.globalAlpha = 1; ctx.stroke();
  }
}

const FAC_COLORS = [[255, 213, 79], [255, 152, 0], [244, 81, 30], [198, 40, 40], [136, 14, 79]];
function facColor(d) {
  const maxD = M.fac_max || 3;
  const t = Math.max(0, Math.min(1, d / maxD)) * (FAC_COLORS.length - 1);
  const i = Math.min(FAC_COLORS.length - 2, Math.floor(t)), f = t - i;
  const a = FAC_COLORS[i], b = FAC_COLORS[i + 1];
  return 'rgb(' + Math.round(a[0] + (b[0] - a[0]) * f) + ',' +
                  Math.round(a[1] + (b[1] - a[1]) * f) + ',' +
                  Math.round(a[2] + (b[2] - a[2]) * f) + ')';
}
function facRadius(d) { return Math.max(3.2, Math.min(11, 3.2 + d * 2.2)); }

function drawFacilities() {
  const showLabels = layers.labels && view.s > 1.6;
  for (const f of V.facilities) {
    const p = toScreen(f.c, f.r);
    if (p[0] < -30 || p[1] < -30 || p[0] > canvas.clientWidth + 30 || p[1] > canvas.clientHeight + 30) continue;
    const r = facRadius(f.d);
    ctx.beginPath();
    ctx.arc(p[0], p[1], r, 0, Math.PI * 2);
    ctx.fillStyle = facColor(f.d);
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(20,25,30,.75)';
    ctx.stroke();
  }
  /* Labels are placed greedily and dropped on collision, worst point first, so
     what survives is always the most severe set rather than the leftmost. */
  if (showLabels) {
    const taken = [];
    const byDepth = V.facilities.slice().sort((a, b) => b.d - a.d);
    const limit = view.s > 3 ? 16 : 9;
    ctx.font = '11px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif';
    ctx.textBaseline = 'middle';
    let drawn = 0;
    for (const f of byDepth) {
      if (drawn >= limit) break;
      const p = toScreen(f.c, f.r);
      const wpx = ctx.measureText(f.name).width;
      const right = p[0] < canvas.clientWidth * 0.72;
      let bx = right ? p[0] + facRadius(f.d) + 4 : p[0] - facRadius(f.d) - 4 - wpx - 10;
      /* Keep the plate inside the canvas: a label that runs off the edge is worse
         than no label, because it looks like the facility has no name. */
      bx = Math.max(3, Math.min(bx, canvas.clientWidth - wpx - 9));
      const by = p[1] - 9;
      let clash = false;
      for (const t of taken) {
        if (!(bx + wpx + 10 < t[0] || bx > t[0] + t[2] + 10 || by + 15 < t[1] || by > t[1] + 15)) { clash = true; break; }
      }
      if (clash) continue;
      taken.push([bx, by, wpx]);
      ctx.fillStyle = 'rgba(255,255,255,.93)';
      ctx.fillRect(bx - 2, by, wpx + 6, 14);
      ctx.strokeStyle = 'rgba(150,160,170,.55)'; ctx.lineWidth = .6;
      ctx.strokeRect(bx - 2, by, wpx + 6, 14);
      ctx.fillStyle = '#20303f';
      ctx.fillText(f.name, bx + 1, by + 7.5);
      drawn++;
    }
  }
  if (selected && selected.type === 'fac') {
    const p = toScreen(selected.o.c, selected.o.r);
    ctx.beginPath(); ctx.arc(p[0], p[1], facRadius(selected.o.d) + 5, 0, Math.PI * 2);
    ctx.strokeStyle = '#111'; ctx.lineWidth = 2; ctx.stroke();
    ctx.beginPath(); ctx.arc(p[0], p[1], facRadius(selected.o.d) + 7.5, 0, Math.PI * 2);
    ctx.strokeStyle = '#ffd54f'; ctx.lineWidth = 2; ctx.stroke();
  }
}

function drawBasins() {
  for (const b of V.basins) {
    const p = toScreen(b.c, b.r);
    if (p[0] < -20 || p[1] < -20 || p[0] > canvas.clientWidth + 20 || p[1] > canvas.clientHeight + 20) continue;
    if (!b.verify && !b.outside) continue;
    const col = b.outside ? '#6a4fc0' : '#8e24aa';
    ctx.beginPath();
    ctx.moveTo(p[0], p[1] - 6); ctx.lineTo(p[0] + 6, p[1]);
    ctx.lineTo(p[0], p[1] + 6); ctx.lineTo(p[0] - 6, p[1]);
    ctx.closePath();
    ctx.fillStyle = col; ctx.globalAlpha = .85; ctx.fill(); ctx.globalAlpha = 1;
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.2; ctx.stroke();
  }
}

function drawProfilePts() {
  profilePts.forEach((p, i) => {
    const s = toScreen(p[0], p[1]);
    ctx.beginPath(); ctx.arc(s[0], s[1], 5, 0, Math.PI * 2);
    ctx.fillStyle = '#111'; ctx.fill();
    ctx.fillStyle = '#fff'; ctx.font = 'bold 10px sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(String(i + 1), s[0], s[1] + .5);
  });
  if (profilePts.length === 2) {
    const a = toScreen(profilePts[0][0], profilePts[0][1]);
    const b = toScreen(profilePts[1][0], profilePts[1][1]);
    ctx.save();
    ctx.setLineDash([6, 4]); ctx.strokeStyle = '#111'; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    ctx.restore();
  }
  ctx.textAlign = 'start'; ctx.textBaseline = 'alphabetic';
}

function drawScaleBar() {
  const targetM = CELL_M * 70;  // aim for a bar around 2 km
  const nice = niceDistance(targetM);
  const px = nice / CELL_M * view.s;
  const x0 = 20, y0 = canvas.clientHeight - 26;
  ctx.fillStyle = 'rgba(255,255,255,.9)';
  ctx.fillRect(x0 - 6, y0 - 16, px + 70, 26);
  ctx.strokeStyle = '#212121'; ctx.lineWidth = 2.4;
  ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x0 + px, y0); ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x0, y0 - 4); ctx.lineTo(x0, y0); ctx.lineTo(x0 + px, y0); ctx.lineTo(x0 + px, y0 - 4);
  ctx.stroke();
  ctx.fillStyle = '#212121';
  ctx.font = '11.5px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif';
  ctx.textBaseline = 'alphabetic';
  ctx.fillText(nice >= 1000 ? (nice / 1000) + ' km' : nice + ' m', x0 + px + 8, y0 + 4);
}
function niceDistance(m) {
  const steps = [100, 200, 500, 1000, 2000, 5000, 10000, 20000];
  for (const s of steps) if (m <= s) return s;
  return 50000;
}

/* -------------------------------------------------------------------- input */
let dragging = false, moved = false, last = [0, 0];

function pointerPos(e) {
  const r = canvas.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}
canvas.addEventListener('pointerdown', e => {
  /* Synthetic and some embedded contexts have no active pointer to capture, and
     an exception here would abort the handler before panning ever starts. */
  try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* not capturable */ }
  dragging = true; moved = false; last = pointerPos(e);
  canvas.classList.add('panning');
});
canvas.addEventListener('pointermove', e => {
  const p = pointerPos(e);
  if (dragging) {
    view.ox += p[0] - last[0];
    view.oy += p[1] - last[1];
    last = p; moved = true;
    draw();
  }
  hover(p);
});
canvas.addEventListener('pointerup', e => {
  dragging = false;
  canvas.classList.remove('panning');
  if (moved) return;
  const p = pointerPos(e);
  const g = toGrid(p[0], p[1]);
  if (profileMode) {
    if (profilePts.length >= 2) profilePts = [];
    profilePts.push(g);
    draw();
    if (profilePts.length === 2) renderProfile();
    return;
  }
  const hit = pickFacility(p);
  if (hit) select({ type: 'fac', o: hit });
  else {
    const uh = pickUnderpass(g);
    if (uh) select({ type: 'under', o: uh });
    else select(null);
  }
});
canvas.addEventListener('pointerleave', () => { tip.style.display = 'none'; });
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const p = pointerPos(e);
  const k = Math.exp(-e.deltaY * 0.0016);
  const ns = Math.max(view.min, Math.min(view.max, view.s * k));
  view.ox = p[0] - (p[0] - view.ox) * (ns / view.s);
  view.oy = p[1] - (p[1] - view.oy) * (ns / view.s);
  view.s = ns;
  draw(); hover(p);
}, { passive: false });

function pickFacility(p) {
  let best = null, bestD = 14;
  for (const f of V.facilities) {
    const s = toScreen(f.c, f.r);
    const d = Math.hypot(s[0] - p[0], s[1] - p[1]);
    if (d < bestD) { bestD = d; best = f; }
  }
  return best;
}
function pickUnderpass(g) {
  let best = null, bestD = 0.8;  // within ~22 m of the line
  for (const s of V.underpasses) {
    if (!s.flooded) continue;
    for (let i = 0; i < s.pts.length - 1; i++) {
      const d = segDist(g[0], g[1], s.pts[i][0], s.pts[i][1], s.pts[i + 1][0], s.pts[i + 1][1]);
      if (d < bestD) { bestD = d; best = s; }
    }
  }
  return best;
}
function segDist(px, py, x0, y0, x1, y1) {
  const dx = x1 - x0, dy = y1 - y0;
  const L = dx * dx + dy * dy;
  let t = L ? ((px - x0) * dx + (py - y0) * dy) / L : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (x0 + t * dx), py - (y0 + t * dy));
}

const KIND_CN = { subway_entrance: '地铁出入口', school: '学校', kindergarten: '幼儿园',
                  hospital: '医院', fire_station: '消防站', ambulance_station: '急救站',
                  police: '公安', clinic: '诊所', university: '高校', college: '高校' };

function hover(p) {
  const g = toGrid(p[0], p[1]);
  const c = Math.floor(g[0]), r = Math.floor(g[1]);
  const inside = c >= 0 && r >= 0 && c < COLS && r < ROWS;
  const d = inside ? depthAt(c, r) : NaN;
  const el = inside ? elevAt(c, r) : NaN;
  const ll = inside ? lonlatAt(g[0], g[1]) : null;

  hud.textContent = inside
    ? '水深 ' + d.toFixed(2) + ' m' + (isNaN(el) ? '' : '　地面 ' + el.toFixed(1) + ' m') +
      '　' + ll[0].toFixed(5) + '°E ' + ll[1].toFixed(5) + '°N'
    : '';

  const f = pickFacility(p);
  if (f) {
    tip.innerHTML = '<b>' + esc(f.name) + '</b>　' + (KIND_CN[f.kind] || f.kind) +
      '<br>水深 <span class="m">' + f.d.toFixed(2) + ' m</span>' +
      '<br><span class="m">' + f.lon.toFixed(5) + ', ' + f.lat.toFixed(5) + '</span>';
    tip.style.display = 'block';
    tip.style.left = p[0] + 'px';
    tip.style.top = p[1] + 'px';
    return;
  }
  const u = pickUnderpass(g);
  if (u) {
    tip.innerHTML = '<b>' + esc(u.name) + '</b>　' + (u.highway || '') +
      '<br>最大水深 <span class="m">' + u.d.toFixed(2) + ' m</span>　淹没长度 ' +
      u.fd.toFixed(0) + ' m' + (u.layer ? '　layer ' + u.layer : '');
    tip.style.display = 'block';
    tip.style.left = p[0] + 'px';
    tip.style.top = p[1] + 'px';
    return;
  }
  tip.style.display = 'none';
}
function esc(s) { return String(s).replace(/[&<>]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[m])); }

/* ------------------------------------------------------------------ profile */
function renderProfile() {
  const wrap = document.getElementById('profileWrap');
  wrap.classList.add('on');
  const cv = document.getElementById('profile');
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const a = profilePts[0], b = profilePts[1];
  const N = 220;
  const dist = Math.hypot(b[0] - a[0], b[1] - a[1]) * CELL_M;
  const samples = [];
  for (let i = 0; i < N; i++) {
    const t = i / (N - 1);
    const c = a[0] + (b[0] - a[0]) * t, r = a[1] + (b[1] - a[1]) * t;
    samples.push({
      x: t * dist,
      e: elevAt(Math.min(COLS - 1, Math.max(0, Math.floor(c))), Math.min(ROWS - 1, Math.max(0, Math.floor(r)))),
      d: depthAt(Math.floor(c), Math.floor(r)),
    });
  }
  const haveElev = ELEV && samples.some(s => !isNaN(s.e));
  const padL = 34, padR = 10, padT = 22, padB = 30;
  const stripH = 30, gap = 15;
  const plotH = h - padT - padB - stripH - gap;
  const plotW = w - padL - padR;

  const els = samples.map(s => s.e).filter(v => !isNaN(v)).concat(samples.map(s => s.e + s.d).filter(v => !isNaN(v)));
  let lo = Math.min(...els), hi = Math.max(...els);
  if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
  const pad = Math.max(0.6, (hi - lo) * 0.16); lo -= pad; hi += pad;
  const maxD = Math.max(0.3, ...samples.map(s => s.d)) * 1.2;

  const X = x => padL + (x / Math.max(1, dist)) * plotW;
  const Y = v => padT + plotH - ((v - lo) / (hi - lo)) * plotH;

  g.fillStyle = '#fbfcfd'; g.fillRect(padL, padT, plotW, plotH);
  g.strokeStyle = '#e3e8ec'; g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const yy = padT + (plotH / 4) * i;
    g.beginPath(); g.moveTo(padL, yy); g.lineTo(padL + plotW, yy); g.stroke();
  }
  g.fillStyle = '#7b8b99'; g.font = '10px ui-monospace,Consolas,monospace';
  g.textAlign = 'right'; g.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const v = hi - ((hi - lo) / 4) * i;
    g.fillText(v.toFixed(0), padL - 5, padT + (plotH / 4) * i);
  }
  g.save();
  g.translate(11, padT + plotH / 2); g.rotate(-Math.PI / 2);
  g.textAlign = 'center'; g.fillStyle = '#4a5b6b';
  g.fillText('高程 (m)', 0, 0);
  g.restore();

  if (haveElev) {
    g.beginPath();
    let started = false;
    for (const s of samples) {
      if (isNaN(s.e)) { started = false; continue; }
      const px = X(s.x), py = Y(s.e);
      started ? g.lineTo(px, py) : g.moveTo(px, py);
      started = true;
    }
    g.strokeStyle = '#8d6e63'; g.lineWidth = 1.8; g.stroke();

    const filled = samples.filter(s => !isNaN(s.e) && s.d > 0.02);
    if (filled.length > 1) {
      g.beginPath();
      g.moveTo(X(filled[0].x), Y(filled[0].e));
      for (const s of filled) g.lineTo(X(s.x), Y(s.e + s.d));
      for (let i = filled.length - 1; i >= 0; i--) g.lineTo(X(filled[i].x), Y(filled[i].e));
      g.closePath();
      g.fillStyle = 'rgba(70,140,215,.62)'; g.fill();
      g.beginPath();
      let st = false;
      for (const s of filled) {
        const px = X(s.x), py = Y(s.e + s.d);
        st ? g.lineTo(px, py) : g.moveTo(px, py);
        st = true;
      }
      g.strokeStyle = '#1f5fa9'; g.lineWidth = 2; g.stroke();
    }
  }

  /* Second strip: depth on its own scale with the 0.3 m life-safety threshold.
     On the shared elevation axis a 0.3 m difference is invisible, which is how
     a "shallow" reading ends up looking harmless. */
  const sy = padT + plotH + gap;
  g.fillStyle = '#fbfcfd'; g.fillRect(padL, sy, plotW, stripH);
  g.strokeStyle = '#e3e8ec'; g.strokeRect(padL, sy, plotW, stripH);
  const DY = v => sy + stripH - (v / maxD) * stripH;
  g.beginPath();
  g.moveTo(X(0), DY(0));
  for (const s of samples) g.lineTo(X(s.x), DY(s.d));
  g.lineTo(X(dist), DY(0)); g.closePath();
  g.fillStyle = 'rgba(70,140,215,.45)'; g.fill();
  const th = DY(0.3);
  if (th > sy && th < sy + stripH) {
    g.save(); g.setLineDash([4, 3]);
    g.strokeStyle = '#c62828'; g.lineWidth = 1.4;
    g.beginPath(); g.moveTo(padL, th); g.lineTo(padL + plotW, th); g.stroke();
    g.restore();
    g.fillStyle = '#c62828';
    g.font = '10px -apple-system,"Microsoft YaHei",sans-serif';
    /* textAlign is inherited from whatever drew last — the restored elevation
       axis label leaves it on 'right', which pushes this caption off the left
       edge of the canvas. Every label here states its own alignment. */
    g.textAlign = 'left'; g.textBaseline = 'middle';
    // Keep the caption inside the strip: on a shallow profile the threshold sits
    // near the bottom and an unclamped label would spill over the axis title.
    const ty = Math.max(sy + 8, Math.min(th - 6, sy + stripH - 4));
    g.fillText('0.3 m 生命危险阈值', padL + 4, ty);
  }
  g.fillStyle = '#4a5b6b';
  g.textAlign = 'right'; g.textBaseline = 'middle';
  g.font = '10px ui-monospace,Consolas,monospace';
  g.fillText(maxD.toFixed(1), padL - 5, sy + 8);
  g.font = '10px -apple-system,"Microsoft YaHei",sans-serif';
  g.fillText('水深 (m)', padL + plotW, sy - 6);

  const peak = samples.reduce((m, s) => Math.max(m, s.d), 0);
  const wetLen = samples.filter(s => s.d > 0.02).length / N * dist;
  g.fillStyle = '#1b2733'; g.textAlign = 'left'; g.textBaseline = 'alphabetic';
  g.font = '11.5px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif';
  g.fillText('剖面长 ' + (dist / 1000).toFixed(2) + ' km　最深 ' + peak.toFixed(2) +
             ' m　积水段 ' + (wetLen / 1000).toFixed(2) + ' km', padL, padT - 8);
  g.fillStyle = '#7b8b99'; g.textAlign = 'right';
  g.fillText('沿剖面距离', padL + plotW, h - 8);
  g.textAlign = 'left'; g.textBaseline = 'alphabetic';

  document.getElementById('profHint').innerHTML =
    '剖面已生成。再点两个点可换一条线；关闭「剖面」按钮退出该模式。' +
    (haveElev ? '' : ' <b>高程数据不可用</b>，只显示水面。');
}

/* ------------------------------------------------------------------- panels */
function select(item) {
  selected = item;
  const box = document.getElementById('detail');
  if (!item) {
    box.className = 'empty';
    box.textContent = '把鼠标移到地图上读取该点水深与高程；点击橙色设施或红色下穿段查看详情。';
  } else if (item.type === 'fac') {
    const f = item.o;
    box.className = '';
    box.innerHTML =
      '<div style="font-weight:600;margin-bottom:4px">' + esc(f.name) + '</div>' +
      '<div class="k">类型</div>' + (KIND_CN[f.kind] || esc(f.kind)) +
      '<div class="k" style="margin-top:6px">模拟水深</div><b>' + f.d.toFixed(2) + ' m</b>' +
      '<div class="k" style="margin-top:6px">经纬度</div><span class="mono">' +
      f.lon.toFixed(5) + ', ' + f.lat.toFixed(5) + '</span>' +
      '<div class="k" style="margin-top:6px">OpenStreetMap</div>' +
      '<a href="https://www.openstreetmap.org/' + (String(f.id).startsWith('n') ? 'node/' : 'node/') +
      String(f.id).replace(/^[nw]/, '') + '" target="_blank" rel="noopener">' + esc(String(f.id)) + '</a>' +
      '<div class="note">水深取自该点 3×3 邻域的最大值（与报告口径一致）。</div>';
  } else {
    const u = item.o;
    box.className = '';
    box.innerHTML =
      '<div style="font-weight:600;margin-bottom:4px">' + esc(u.name) + '</div>' +
      '<div class="k">道路类型</div>' + esc(u.highway || '-') +
      '　<span class="k">layer</span> ' + esc(u.layer || '-') +
      '<div class="k" style="margin-top:6px">最大水深</div><b>' + u.d.toFixed(2) + ' m</b>' +
      '　<span class="pill ' + (u.risk === 'life-safety' ? 'p-life' : 'p-watch') + '">' +
      (u.risk === 'life-safety' ? '生命危险级' : '需关注') + '</span>' +
      '<div class="k" style="margin-top:6px">淹没长度</div>' + u.fd.toFixed(0) + ' m' +
      (u.vehicle ? '　<span class="mono">车行</span>' : '　<span class="mono">人行</span>') +
      '<div class="note">下穿段从两端灌水且没有逃生路线；0.3 m 是报告采用的生命危险阈值。</div>';
  }
  draw();
  if (item) {
    const g = item.type === 'fac' ? [item.o.c, item.o.r]
                                  : item.o.pts[Math.floor(item.o.pts.length / 2)];
    flyTo(g[0], g[1], item.type === 'fac' ? 7 : 4.5);
  }
  highlightRows();
}

function flyTo(c, r, s) {
  view.s = Math.max(view.min, Math.min(view.max, s));
  view.ox = canvas.clientWidth / 2 - c * view.s;
  view.oy = canvas.clientHeight / 2 - r * view.s;
  draw();
}

function highlightRows() {
  document.querySelectorAll('.row').forEach(el => el.classList.remove('sel'));
  if (!selected) return;
  const key = selected.type + ':' + (selected.o.id !== undefined ? selected.o.id : selected.o.name);
  const el = document.querySelector('.row[data-key="' + CSS.escape(key) + '"]');
  if (el) el.classList.add('sel');
}

/* ------------------------------------------------------------------- build */
function fmt(v, n) { return (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(n); }

function buildHead() {
  const p = R.ponding || {}, d = R.domain || {};
  document.getElementById('subtitle').innerHTML =
    '南京主城区 · ' + fmt(R.rainfall_mm, 0) + ' mm 降雨情景 · 研究区 ' +
    fmt(d.area_km2, 2) + ' km² · 格网 ' + fmt(R.cell_m, 1) + ' m';
  document.getElementById('tag').textContent = R.tag + '　outcome: ' + (R.outcome || '?') +
    (R.status && R.status !== R.outcome ? '　quality: ' + R.status : '');
}

function buildKpis() {
  const p = R.ponding || {}, d = R.domain || {}, up = R.underpass_summary || {},
        cr = R.critical_summary || {}, rd = R.roads || {}, bd = R.buildings || {};
  const area = (p.pond_area_m2 || 0) / 1e6;
  const cells = [
    ['淹没面积', area.toFixed(2) + ' km²', '占研究区 ' + (100 * area / (d.area_km2 || 1)).toFixed(1) + '%'],
    ['最深积水', fmt(p.max_depth_m, 2) + ' m', '色标分 5 级，见右上图例'],
    ['下穿 / 隧道被淹', up.underpass_ways_flooded + ' / ' + up.underpass_ways,
      '生命危险级 ' + up.life_safety_count + ' 段，其中车行 ' + up.vehicle_life_safety_count + ' 段'],
    ['关键设施被淹', cr.facilities_flooded + ' / ' + cr.facilities_total,
      '地铁出入口 ' + cr.subway_entrances_flooded + ' 个'],
    ['受影响道路', rd.ways_flooded + ' / ' + rd.ways_total,
      '淹没长度 ' + Math.round(rd.flooded_length_m || 0) + ' m'],
    ['受影响建筑', bd.buildings_affected + ' / ' + bd.buildings_total,
      '占地 ' + Math.round((bd.affected_footprint_area_m2 || 0) / 1e4) + ' 万 m²'],
  ];
  document.getElementById('kpis').innerHTML = cells.map(c =>
    '<div class="kpi"><b>' + c[1] + '</b><span>' + c[0] + '</span>' +
    '<div style="font-size:10.5px;color:var(--ink3);margin-top:3px">' + c[2] + '</div></div>').join('');
}

function buildIndicators() {
  const checks = R.checks || {};
  const rows = [];
  const order = ['D01_water_balance', 'D01b_raster_vs_solver', 'D04_sensitivity'];
  for (const key of order) {
    const c = checks[key];
    if (!c) continue;
    const pass = c.passed === true;
    const value = c.relative_error !== undefined ? c.relative_error
                : c.max_relative_area_change !== undefined ? c.max_relative_area_change
                : c.hit_rate !== undefined ? c.hit_rate : null;
    const tol = c.tolerance;
    const shown = (value !== null && Math.abs(value) < 1e-3 && value !== 0)
      ? value.toExponential(1) : fmt(value, 4);
    rows.push('<tr><td><span class="dot ' + (pass ? 'pass' : 'fail') + '"></span>' +
      '<b>' + key.split('_')[0] + '</b> <span style="color:var(--ink3)">' +
      { D01: '全域水量平衡', D01b: '栅格 vs 解算器', D04: '参数敏感性' }[key.split('_')[0]] +
      '</span><div class="mono" style="color:var(--ink3);margin-top:2px">checks.' + key + '</div></td>' +
      '<td class="val"><b>' + shown + '</b><br><span style="color:var(--ink3)">容差 ' + tol +
      '</span></td></tr>');
  }
  if (checks.D02 || checks.D2) {
    const c = checks.D02;
    rows.push('<tr><td><span class="dot ' + (c.passed ? 'pass' : 'fail') + '"></span><b>D02</b> ' +
      '<span style="color:var(--ink3)">官方易涝点核对</span></td><td class="val">' +
      fmt(c.hit_rate, 4) + '</td></tr>');
  }
  document.getElementById('indicators').innerHTML = rows.join('');

  /* The honesty note is not decoration: a page that shows three green dots and
     says nothing about the indicators living outside this artifact invites the
     reader to assume they all passed. */
  const missing = [];
  if (!checks.D02 && !checks.D2) missing.push('D02 / D02b（官方易涝点核对与排序检验）未在本 run 启用，需要 <code>--checkpoints</code>');
  missing.push('D03（跨语言独立复算）由 AGH 工具 <code>flood_crosslang_check</code> 在工具链内执行，不在报告 JSON 中');
  missing.push('解析校核与回归测试不在本页：<code>python -X utf8 -m floodkit selftest</code>、<code>python tests\\test_floodkit.py</code>');
  document.getElementById('indNote').innerHTML =
    '<b>本页未包含的指标（不要当作已通过）：</b><br>' + missing.map(m => '· ' + m).join('<br>');
}

function rowHtml(key, name, depth, tag, cls) {
  return '<div class="row" data-key="' + esc(key) + '"><span class="nm">' + esc(name) +
    (tag ? '<span class="pill ' + cls + '">' + tag + '</span>' : '') +
    '</span><span class="dp">' + depth.toFixed(2) + ' m</span></div>';
}

function buildLists() {
  const unders = V.underpasses.filter(s => s.flooded)
    .sort((a, b) => (b.d - a.d) || (b.fd - a.fd)).slice(0, 14);
  document.getElementById('riskUnder').innerHTML = unders.length ? unders.map(s =>
    rowHtml('under:' + s.id, s.name === '(unnamed)' ? '(未命名 ' + (s.highway || '') + ')' : s.name,
            s.d, s.risk === 'life-safety' ? '生命危险' : '关注',
            s.risk === 'life-safety' ? 'p-life' : 'p-watch')).join('')
    : '<div class="empty">本情景无被淹下穿段。</div>';

  const facs = V.facilities.slice(0, 14);
  document.getElementById('riskFac').innerHTML = facs.length ? facs.map(f =>
    rowHtml('fac:' + f.id, f.name + '　' + (KIND_CN[f.kind] || f.kind), f.d, '', '')).join('')
    : '<div class="empty">本情景无被淹关键设施。</div>';

  document.querySelectorAll('.row').forEach(el => {
    el.addEventListener('click', () => {
      const key = el.getAttribute('data-key');
      if (key.startsWith('under:')) {
        const id = Number(key.slice(6));
        const s = V.underpasses.find(x => x.id === id);
        if (s) select({ type: 'under', o: s });
      } else {
        const id = Number(key.slice(4));
        const f = V.facilities.find(x => x.id === id);
        if (f) select({ type: 'fac', o: f });
      }
    });
  });
}

function buildControls() {
  const ctl = document.getElementById('ctl');
  const defs = [
    ['base', '水体 / 建筑'], ['roads', '路网'], ['depth', '水深'],
    ['under', '下穿（被淹）'], ['dry', '下穿（未淹）'], ['fac', '风险设施'],
    ['basin', '需核验洼地'], ['labels', '标注'],
  ];
  ctl.innerHTML = defs.map(d =>
    '<label class="sw"><input type="checkbox" data-layer="' + d[0] + '"' +
    (layers[d[0]] ? ' checked' : '') + '>' + d[1] + '</label>').join('') +
    '<button id="btnProfile" class="mini">剖面</button>' +
    '<button id="btnFit" class="mini">重置视图</button>' +
    '<button id="btnPng" class="mini">导出 PNG</button>' +
    '<label class="sw" style="flex:1 1 100%">水深透明度' +
    '<input type="range" id="alpha" min="25" max="100" value="' + Math.round(depthAlpha * 100) +
    '" style="flex:1"></label>';

  ctl.querySelectorAll('input[data-layer]').forEach(inp => {
    inp.addEventListener('change', () => { layers[inp.dataset.layer] = inp.checked; draw(); });
  });
  document.getElementById('alpha').addEventListener('input', e => {
    depthAlpha = Number(e.target.value) / 100; draw();
  });
  const bp = document.getElementById('btnProfile');
  bp.addEventListener('click', () => {
    profileMode = !profileMode;
    bp.classList.toggle('on', profileMode);
    profilePts = [];
    document.getElementById('profileWrap').classList.toggle('on', false);
    document.getElementById('profHint').innerHTML = profileMode
      ? '<b>剖面模式：</b>在地图上点两个点。拖动可平移，滚轮缩放。'
      : '打开上方「剖面」后，在地图上点两个点，即可切出该线段的地形高程与水面线。';
    draw();
  });
  document.getElementById('btnFit').addEventListener('click', fit);
  document.getElementById('btnPng').addEventListener('click', () => {
    canvas.toBlob(b => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(b);
      a.download = R.tag + '_interactive_view.png';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    });
  });
}

function buildLegend() {
  const items = LABELS.map((l, i) =>
    '<div class="li"><i style="background:' + COLORS[i] + '"></i>' + l + ' m</div>').join('');
  document.getElementById('legend').innerHTML =
    '<div class="t">模拟积水深度（米）</div>' + items +
    '<div style="height:7px"></div>' +
    '<div class="li"><i class="ln" style="border-color:#c62828"></i>生命危险下穿</div>' +
    '<div class="li"><i class="ln" style="border-color:#e07b0a"></i>被淹·需关注</div>' +
    '<div class="li"><i class="ln" style="border-color:#9aa6b0"></i>下穿（未淹）</div>' +
    '<div class="li"><i style="background:#8e24aa;border-radius:50%;width:10px;height:10px"></i>需现场核验洼地</div>' +
    '<div class="li"><i style="background:#6a4fc0;border-radius:50%;width:10px;height:10px"></i>适用域外洼地</div>' +
    '<div class="li"><i style="background:#ff6f00;border-radius:50%;width:10px;height:10px"></i>关键设施（色=水深）</div>';
}

function buildQuality() {
  const q = R.quality || {}, issues = q.issues || [];
  const bq = R.basin_qc || {};
  let html = '';
  if (!issues.length) html += '<div class="empty">输入质量门未报告任何问题。</div>';
  else html += issues.map(i => {
    const col = i.severity === 'error' ? 'var(--bad)' : i.severity === 'warning' ? 'var(--warn)' : 'var(--ink3)';
    return '<div style="margin-bottom:7px"><span class="dot" style="background:' + col + '"></span>' +
      '<span class="mono" style="color:' + col + '">' + esc(i.code) + '</span><br>' +
      '<span style="font-size:12px;color:var(--ink2)">' + esc(i.message) + '</span></div>';
  }).join('');
  if (bq.deep_and_tiny_count !== undefined) {
    html += '<div class="note">另有 <b>' + bq.deep_and_tiny_count + '</b> 个「深而小」洼地' +
      '（疑为 DEM 伪影）与 <b>' + bq.outside_validity_count + '</b> 个「陡坡汇水」洼地' +
      '（超出洼地蓄水模型的适用域）被单独列出，未混入上方结论。' +
      '地图上的菱形标记即这两类点位，最深的 ' + fmt(bq.screened_max_depth_m, 2) +
      ' m 属于其中之一。</div>';
  }
  document.getElementById('quality').innerHTML = html;
}

function buildFoot() {
  const b = R.bounds || [];
  document.getElementById('foot').innerHTML =
    '地形 Copernicus DEM GLO-30｜土地覆盖 ESA WorldCover 10 m｜承灾体 OpenStreetMap（ODbL）<br>' +
    '<b>静态洼地蓄水模型，不含排水管网，结果为水深上限</b>——本模型回答「地形上哪里会存水、存多深」，' +
    '不回答「实际这场雨哪里会淹」。<br>' +
    '研究区 ' + b[0].toFixed(4) + '–' + b[2].toFixed(4) + '°E, ' +
    b[1].toFixed(4) + '–' + b[3].toFixed(4) + '°N　格网 ' + fmt(R.cell_m, 1) + ' m　栅格 ' +
    R.cols + '×' + R.rows;
  document.getElementById('reproduce').textContent = R.reproduce;
}

window.addEventListener('resize', resize);
buildHead(); buildKpis(); buildIndicators(); buildLists(); buildControls();
buildLegend(); buildQuality(); buildFoot();
resize(); fit();
})();
"""


def build_data(report, tag, vectors, payload, bounds, cell_m, shape, meta_extra):
    base = {
        "meta": {
            "bg": BG,
            "edges": DEPTH_EDGES,
            "colors": DEPTH_COLORS,
            "labels": DEPTH_LABELS,
            # Generated once, in Python. The page is not allowed to re-derive the
            # binning: see the note on class_codes in floodkit/runio.py.
            "class_of_code": code_class_table(),
            "depth_scale": DEPTH_SCALE,
            "base_scale": float(meta_extra["scale"]),
            "elev_scale": ELEV_SCALE,
            "elev_offset": payload["elev_offset"],
            "cols": shape[1],
            "rows": shape[0],
            "lonlat": meta_extra["lonlat"],
            "fac_max": meta_extra["fac_max"],
        },
        "report": report,
        "vectors": vectors,
        "base_png": base64.b64encode(meta_extra["base_png"]).decode("ascii"),
        "roads_png": base64.b64encode(meta_extra["roads_png"]).decode("ascii"),
        "depth": payload["depth"],
        "elev": payload["elev"],
    }
    return base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", default="nanjing_final")
    parser.add_argument("--scale", type=float, default=8.0,
                        help="basemap image pixels per grid cell (default 8). This is the "
                             "zoom ceiling: past it the browser is enlarging a fixed raster "
                             "and the basemap goes soft. Cost grows with the square of the "
                             "ratio.")
    parser.add_argument("--out", default="")
    parser.add_argument("--max-mb", type=float, default=8.0,
                        help="fail if the page exceeds this size (default 8 MB)")
    args = parser.parse_args()

    tag = args.tag
    report, depth, transform, crs, shape, cell_m = load_run(tag)
    bounds = bounds_wgs84(transform, crs, shape)
    print("run %s: %d x %d cells, %.1f m, bounds %.4f %.4f %.4f %.4f"
          % (tag, shape[1], shape[0], cell_m, *bounds))

    # ---- vector layers, recomputed and cross-checked against the report -----
    print("selecting the OSM extracts this run used ...")
    osm_paths = select_osm(report, bounds)

    print("recomputing exposure vectors from the cached OSM data ...")
    try:
        vectors, notes = build_vectors(report, depth, transform, crs, shape, bounds, osm_paths)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a missed layer must not lose the page
        print("vector layers skipped: %s: %s" % (type(exc).__name__, exc))
        vectors = {"underpasses": [], "facilities": [], "basins": []}
        notes = ["OSM 图层重建失败：%s" % exc]
    print("  underpasses: %d segments, %d flooded"
          % (len(vectors["underpasses"]), sum(1 for s in vectors["underpasses"] if s["flooded"])))
    print("  facilities: %d flooded" % len(vectors["facilities"]))
    print("  basins flagged for verification: %d" % len(vectors["basins"]))

    # ---- rasters ------------------------------------------------------------
    print("loading elevation from the DEM cache ...")
    elevation = elevation_on_grid(bounds, transform, crs, shape)
    payload = encode_arrays(depth, elevation, shape)
    if depth.max() > DEPTH_CODE_CEILING:
        print("warning: max depth %.2f m exceeds the %.2f m encoding ceiling and was clamped"
              % (depth.max(), DEPTH_CODE_CEILING))

    # ---- basemap layers -----------------------------------------------------
    water = None
    try:
        water = load_water_mask(tag, bounds, transform, crs, shape)
    except Exception as exc:  # noqa: BLE001
        print("water layer skipped: %s" % exc)

    print("baking basemap layers at %.1f px/cell ..." % args.scale)
    base_png, roads_png = bake_layers(
        bounds, transform, crs, shape, args.scale, water,
        osm_paths.get("roads"), osm_paths.get("buildings"))
    raw_size = len(base_png) + len(roads_png)
    base_png, roads_png = quantise_png(base_png), quantise_png(roads_png)
    print("  basemap %.2f MB -> %.2f MB after palette quantisation"
          % (raw_size / 1e6, (len(base_png) + len(roads_png)) / 1e6))

    # ---- assemble -----------------------------------------------------------
    lonlat = lonlat_lookup(transform, crs, shape)
    fac_max = max([f["d"] for f in vectors["facilities"]] or [1.0])
    report_view = report_subset(report, tag, cell_m, shape, bounds)
    data = build_data(report_view, tag, vectors, payload, bounds, cell_m, shape,
                      {"lonlat": lonlat, "fac_max": fac_max, "scale": args.scale,
                       "base_png": base_png, "roads_png": roads_png})

    # "</" inside a JSON string would close the <script> tag early.
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = "宁讯智鉴 · %s 内涝分析交互报告" % tag
    html = (HTML_TEMPLATE
            .replace("__TITLE__", title)
            .replace("__DATA__", blob)
            .replace("__SCRIPT__", SCRIPT_TEMPLATE))

    out = args.out or artifact(tag, "_report.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    size_mb = os.path.getsize(out) / 1e6
    print("wrote %s (%.2f MB)" % (out, size_mb))
    for note in notes:
        print("  note: %s" % note)
    if vectors["underpasses"]:
        print("  分层：下穿 %d 段（被淹 %d）｜设施 %d 个｜需核验洼地 %d 个"
              % (len(vectors["underpasses"]),
                 sum(1 for s in vectors["underpasses"] if s["flooded"]),
                 len(vectors["facilities"]), len(vectors["basins"])))
    if size_mb > args.max_mb:
        raise SystemExit("page is %.2f MB, above the %.1f MB budget — lower --scale"
                         % (size_mb, args.max_mb))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

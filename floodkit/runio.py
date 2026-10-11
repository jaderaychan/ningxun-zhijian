"""Read a finished run back off disk.

Everything that renders or exports from a completed run needs the same four
things, and getting any of them subtly wrong produces a figure that looks right
and is not:

1. the report JSON and the depth raster, with the -9999 no-data value masked out
   rather than masked *incidentally* by a 0.05 m threshold;
2. the run's own footprint. It is not stored in the report, because the requested
   bounds and the reprojected grid footprint differ by a cell or two — so it is
   derived from the raster's transform and CRS, never from a hardcoded box;
3. **the right OSM cache file.** A cache directory accumulates overlapping
   extracts, and the raster footprint does not round-trip to the requested bounds,
   so geometry alone picks the wrong one: on this project it silently produced a
   map of 868 underpass sections for a report that counted 420. The report's own
   counts are the only reliable selector, so the file that reproduces them wins;
4. the permanent-water mask, which is expensive to rebuild and is a derived cache.

Keeping this here rather than in each script is the point: three copies of the
"which cache file" rule is three chances to disagree with the report.
"""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEPTH_NODATA = -9999.0

OSM_KINDS = ("roads", "buildings", "critical")

# The depth classification every output shares: the printed map, the interactive
# page, the GeoJSON export and the story figure must not disagree about what
# "0.5 m" looks like.
#
# The first edge is 0.0 and the first band is therefore (0, 0.05) — every cell the
# model put any water in at all. That band is not decoration: `pond_area_m2` in the
# report counts cells with depth strictly greater than zero, so a map that starts
# at 0.05 m draws 6.5% less water than the headline it prints underneath itself.
# The gap was 0.506 km2 on the reference run and went unnoticed until the GeoJSON
# area total was reconciled against the report.
DEPTH_EDGES = [0.0, 0.05, 0.20, 0.50, 1.00, 2.00]
DEPTH_LABELS = ["< 0.05", "0.05–0.2", "0.2–0.5", "0.5–1.0", "1.0–2.0", "> 2.0"]
# Ramp from pale to navy. The two shallowest steps used to be #eef6fe and #d9ecff,
# which on the map's #f7f7f5 background are indistinguishable from "dry" once the
# figure is scaled to fit a screen — and the trace step is the one that makes the
# drawn water add up to the headline area, so it cannot be invisible.
DEPTH_COLORS = ["#d5ebfc", "#a6d2f5", "#6cb0e6", "#3a80c6", "#1c56a3", "#0a2c63"]

# The threshold the *exposure* analysis uses. Deliberately not the same thing as
# the ponding classes: a centimetre of water on a road is sheet flow, a centimetre
# at a subway entrance is not a life-safety event, and the report's facility counts
# are built on this number.
EXPOSURE_MIN_DEPTH_M = 0.05


def class_index(value: float) -> int:
    """0-based depth class of one value in metres, or -1 at or below zero.

    Band k covers [DEPTH_EDGES[k], DEPTH_EDGES[k+1]) with the last band open
    ended, so band 0 is the (0, 0.05) trace band. Written as "count the edges this
    value reaches" rather than "find the first upper bound it is under" because the
    two are only equal when the edge list carries a trailing sentinel, and getting
    that backwards shifts every class by one — which is exactly what happened to
    the interactive page, where 0.1 m was painted with the 0.2–0.5 m colour and
    nothing raised.
    """
    if value <= 0:
        return -1
    index = 0
    for i in range(1, len(DEPTH_EDGES)):
        if value >= DEPTH_EDGES[i]:
            index = i
    return index


def class_codes(depth):
    """Depth raster -> integer class per cell, 0 meaning "no water at all".

    int16 so it can go straight into rasterio.features.shapes, which needs a
    discrete raster to vectorise.
    """
    import numpy as np

    codes = np.zeros(depth.shape, dtype="int16")
    wet = depth > 0
    for index in range(len(DEPTH_EDGES)):
        lower = DEPTH_EDGES[index]
        upper = DEPTH_EDGES[index + 1] if index + 1 < len(DEPTH_EDGES) else None
        band = depth >= lower
        if upper is not None:
            band &= depth < upper
        codes[band & wet] = index + 1
    return codes


#: Byte encoding used by the interactive page: one byte per cell, 0 meaning dry.
#: Code 1 is the trace band and codes 2..255 are the wet classes at 5 cm steps,
#: so depth = (code - 1) / DEPTH_SCALE for code >= 1.
DEPTH_SCALE = 20.0
DEPTH_CODE_CEILING = (255 - 1) / DEPTH_SCALE


def encode_depth(depth):
    """Depth raster -> one byte per cell, dry cells zero."""
    import numpy as np

    codes = np.zeros(depth.shape, dtype="uint8")
    wet = depth > 0
    scaled = np.floor(np.clip(depth[wet], 0.0, DEPTH_CODE_CEILING) * DEPTH_SCALE).astype("int64") + 1
    codes[wet] = np.clip(scaled, 1, 255).astype("uint8")
    return codes


def code_to_depth(code: int) -> float | None:
    """Representative depth of one byte code, or None for code 0 (dry).

    Code 1 exists to separate "wet, under 5 cm" from "no water at all", which the
    5 cm quantisation cannot otherwise express — both would land on 0. It decodes
    to the middle of the trace band. Codes 2 and up decode to the bottom of their
    step.
    """
    if code <= 0:
        return None
    if code == 1:
        return DEPTH_EDGES[1] / 2.0
    return (code - 1) / DEPTH_SCALE


def code_class_table() -> list:
    """Class for every byte code, 0 meaning dry.

    Generated here and shipped as a lookup table rather than reimplemented as a
    formula in JavaScript: two implementations of the same binning is two chances
    to disagree, and the disagreement is invisible because both produce a colour.
    """
    table = []
    for code in range(256):
        depth = code_to_depth(code)
        table.append(0 if depth is None else class_index(depth) + 1)
    return table


def class_label(code: int) -> str:
    return DEPTH_LABELS[code - 1] if 1 <= code <= len(DEPTH_LABELS) else "dry"


# ------------------------------------------------------------------- fonts

#: Fonts that can draw Chinese labels, most likely first.
CJK_FONT_CANDIDATES = (
    "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC",
    "WenQuanYi Zen Hei", "PingFang SC", "Hiragino Sans GB",
)

_WARNED_ABOUT_FONTS = False


def cjk_font_name():
    """Name of an installed CJK-capable font, or None."""
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in CJK_FONT_CANDIDATES:
        if name in installed:
            return name
    return None


_FONT_CMAP_CACHE: dict = {}


def _font_cmap(family: str) -> set:
    """The set of code points a font can draw, read once and cached."""
    if family in _FONT_CMAP_CACHE:
        return _FONT_CMAP_CACHE[family]
    points: set = set()
    try:
        from matplotlib import font_manager
        from fontTools.ttLib import TTFont

        path = font_manager.findfont(font_manager.FontProperties(family=family),
                                     fallback_to_default=False)
        with TTFont(path, fontNumber=0, lazy=True) as font:
            for table in font["cmap"].tables:
                points |= set(table.cmap.keys())
    except Exception:  # noqa: BLE001 - an unreadable font is not a hard failure
        points = set()
    _FONT_CMAP_CACHE[family] = points
    return points


def glyph_is_available(char: str, family=None) -> bool:
    """Whether the chosen font actually has this character.

    Worth asking, because matplotlib does not: it warns to stderr and draws an
    empty box. A marker chosen for looks can silently become a row of boxes in the
    one figure nobody re-reads. `▸` (U+25B8) is missing from Microsoft YaHei, which
    is how this function came to exist.
    """
    family = family or cjk_font_name()
    return bool(family) and ord(char) in _font_cmap(family)


def missing_glyphs(text: str, family=None) -> set:
    """Every character in `text` the font cannot draw."""
    family = family or cjk_font_name()
    if not family:
        return set(text)
    cmap = _font_cmap(family)
    return {ch for ch in set(text) if ch not in ("\n", "\r", "\t") and ord(ch) not in cmap}


def configure_fonts(verbose: bool = True):
    """Point matplotlib at a CJK font, or say clearly that there isn't one.

    matplotlib does not fail when it cannot draw a glyph: it substitutes a font
    without the character and renders an empty box. A figure full of boxes looks
    like a rendering glitch rather than a missing dependency, and it happens to
    anyone running this on a machine without a Chinese font — which is most Linux
    CI images. Every figure in this project goes through here so the same warning
    appears wherever the problem does.
    """
    global _WARNED_ABOUT_FONTS
    import matplotlib

    name = cjk_font_name()
    if name:
        matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False
        return name
    matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    if verbose and not _WARNED_ABOUT_FONTS:
        _WARNED_ABOUT_FONTS = True
        print("warning: no CJK font installed (tried %s)." % ", ".join(CJK_FONT_CANDIDATES))
        print("         Chinese labels will render as empty boxes; install one of the")
        print("         above, or expect the fallback language where a figure offers it.")
    return None


def data_dir() -> str:
    return os.path.join(ROOT, "data")


def run_dir(tag: str) -> str:
    return os.path.join(data_dir(), "runs", tag)


def artifact(tag: str, suffix: str) -> str:
    return os.path.join(run_dir(tag), "%s%s" % (tag, suffix))


def load_run(tag: str):
    """Report JSON plus the depth raster of one run, on the grid it was built on.

    The raster stores -9999 for cells that carry no data. Relying on that to fall
    below a 0.05 m threshold happens to work and is exactly the kind of accident
    that stops working the moment a threshold changes, so it is masked explicitly
    here.
    """
    import numpy as np
    import rasterio

    report_path = artifact(tag, "_report.json")
    depth_path = artifact(tag, "_depth.tif")
    for required in (report_path, depth_path):
        if not os.path.exists(required):
            raise FileNotFoundError("missing %s — run the event first" % required)

    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)

    with rasterio.open(depth_path) as src:
        raw = src.read(1)
        transform, crs = src.transform, src.crs
        shape = (src.height, src.width)
        nodata = src.nodata
        cell_m = abs(transform.a)

    depth = raw.astype("float64")
    depth[depth == (nodata if nodata is not None else DEPTH_NODATA)] = 0.0
    depth[~np.isfinite(depth)] = 0.0
    depth[depth < 0] = 0.0
    return report, depth, transform, crs, shape, cell_m


def bounds_wgs84(transform, crs, shape):
    """The run's own footprint in WGS84 — never a hardcoded bounding box."""
    from rasterio.transform import array_bounds
    from rasterio.warp import transform_bounds

    west, south, east, north = array_bounds(shape[0], shape[1], transform)
    return transform_bounds(crs, "EPSG:4326", west, south, east, north, densify_pts=21)


# --------------------------------------------------------------- OSM selection


def parse_osm_name(name: str):
    """Read kind and bounding box out of an OSM cache file name.

    `fetch_osm` writes `osm_<kind>_<w>_<s>_<e>_<n>.json`, so the box is
    recoverable without opening a 29 MB file.
    """
    if not name.endswith(".json"):
        return None, None
    parts = name[:-5].split("_")
    if len(parts) != 6 or parts[0] != "osm":
        return None, None
    try:
        return parts[1], tuple(float(v) for v in parts[2:6])
    except ValueError:
        return None, None


def osm_variants(kind: str):
    folder = os.path.join(data_dir(), "osm")
    if not os.path.isdir(folder):
        return []
    out = []
    for name in os.listdir(folder):
        parsed_kind, box = parse_osm_name(name)
        if parsed_kind != kind:
            continue
        path = os.path.join(folder, name)
        out.append({
            "path": path,
            "box": box,
            "area": (box[2] - box[0]) * (box[3] - box[1]),
            "bytes": os.path.getsize(path),
        })
    out.sort(key=lambda c: c["area"])
    return out


def osm_elements(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("elements", [])


def pick_covering(kind: str, bounds):
    """Best-effort cached extract for a report that carries no count to match on.

    Only a fallback: when the report has a count, `pick_by_count` is used instead,
    because geometry alone cannot answer this question. The raster grid is aligned in
    UTM, so its footprint always exceeds the lon/lat box the run requested by up to a
    cell — which means an extract fetched for the requested box never *covers* the
    footprint, and a strict cover test matches nothing at all.

    With nothing covering, the largest variant is the safe choice, not the smallest:
    a superset is harmless when the layer is only drawn, because the axes clip it,
    while a subset silently blanks part of the map. This used to return the smallest
    variant, which is how the 405 km² run quietly came back with the 76 km²
    buildings layer.
    """
    variants = osm_variants(kind)
    if not variants:
        return None
    covering = [c for c in variants
                if all(c["box"][i] <= bounds[i] + 1e-4 for i in (0, 1))
                and all(c["box"][i] >= bounds[i] - 1e-4 for i in (2, 3))]
    if covering:
        return covering[0]["path"]        # osm_variants is sorted by area, smallest first
    return variants[-1]["path"]           # largest: a superset, never a silent subset


def building_total_count(path: str) -> int:
    """Reproduce `exposure.building_impact`'s `buildings_total` for a cached extract.

    The same predicate the pipeline applies: an element counts when it has a ring of
    at least three lon/lat vertices and a non-degenerate bounding box. The projection
    is skipped deliberately — a footprint with zero extent in lon/lat has zero extent
    in UTM too — and the count is taken over the whole file, because that is what the
    report records: a property of the extract, not of the grid.
    """
    total = 0
    for element in osm_elements(path):
        ring = [(pt["lon"], pt["lat"]) for pt in (element.get("geometry") or [])
                if "lon" in pt and "lat" in pt]
        if len(ring) < 3:
            continue
        lons = [point[0] for point in ring]
        lats = [point[1] for point in ring]
        if max(lons) - min(lons) <= 0 or max(lats) - min(lats) <= 0:
            continue
        total += 1
    return total


def pick_by_count(kind: str, expected: int, count_fn):
    """Pick the cached extract a run actually used, by reproducing a count.

    The run's requested bounds are not stored in the report, and the raster
    footprint does not round-trip to them, so geometry alone picks the wrong file
    when several overlapping extracts are cached. Counting is cheap and the report
    is authoritative, so the extract that reproduces the report's own number is
    the one that was used.
    """
    variants = osm_variants(kind)
    if not variants:
        return None
    matches = []
    for cand in variants:
        try:
            if count_fn(cand["path"]) == expected:
                matches.append(cand)
        except Exception as exc:  # noqa: BLE001 - one bad cache file must not stop the run
            print("  candidate %s unreadable: %s" % (os.path.basename(cand["path"]), exc))
    if not matches:
        detail = ", ".join("%s (%d bytes)" % (os.path.basename(c["path"]), c["bytes"])
                           for c in variants)
        raise SystemExit(
            "no cached %s extract reproduces the report's count of %d. Candidates: %s. "
            "Regenerate the OSM cache for this run rather than drawing a subset."
            % (kind, expected, detail)
        )
    return matches[0]["path"]


def underpass_way_count(path: str) -> int:
    from .exposure import _is_underpass

    return sum(1 for el in osm_elements(path)
               if _is_underpass(el.get("tags") or {}) and len(el.get("geometry") or []) >= 2)


def facility_total_count(path: str) -> int:
    """Reproduce exposure.critical_facility_impact's facilities_total.

    That counter includes every element carrying coordinates, whether or not it
    lands inside the grid, so the same rule is applied here.
    """
    total = 0
    for el in osm_elements(path):
        if el.get("type") == "node":
            ok = el.get("lon") is not None and el.get("lat") is not None
        else:
            center = el.get("center") or {}
            ok = center.get("lon") is not None and center.get("lat") is not None
        total += 1 if ok else 0
    return total


def select_osm(report: dict, bounds, verbose: bool = True):
    """Resolve the three OSM layers this run used. Returns {kind: path or None}.

    Every layer is resolved by reproducing a number the report already carries, not
    by comparing boxes. That is not a stylistic choice: the raster footprint always
    overhangs the requested box, so a geometric match cannot identify the extract a
    run used, and getting it wrong is silent — the wrong buildings layer draws a map
    that looks complete and covers half the study area.
    """
    exposure = report.get("exposure") or {}
    underpass_total = (exposure.get("underpasses") or {}).get("underpass_ways")
    facility_total = (exposure.get("critical_facilities") or {}).get("facilities_total")
    building_total = (exposure.get("buildings") or {}).get("buildings_total")
    chosen = {
        "roads": (pick_by_count("roads", underpass_total, underpass_way_count)
                  if underpass_total else pick_covering("roads", bounds)),
        "critical": (pick_by_count("critical", facility_total, facility_total_count)
                     if facility_total else pick_covering("critical", bounds)),
        "buildings": (pick_by_count("buildings", building_total, building_total_count)
                      if building_total else pick_covering("buildings", bounds)),
    }
    if verbose:
        for kind in OSM_KINDS:
            path = chosen.get(kind)
            print("  %s: %s" % (kind, os.path.basename(path) if path else "(none cached)"))
    return chosen


# ------------------------------------------------------------------- landcover


def load_water_mask(tag: str, bounds, transform, crs, shape):
    """Permanent water from ESA WorldCover, cached outside the submission.

    The cache key carries the grid shape and the bounding box, not just the tag: a
    mask computed for one study area and reused for another would recolour the map
    without any error being raised.
    """
    import numpy as np

    cache = os.path.join(
        data_dir(), "tmp",
        "%s_%dx%d_%.4f_%.4f_watermask.npy" % (tag, shape[0], shape[1], bounds[0], bounds[1]),
    )
    if os.path.exists(cache):
        return np.load(cache)
    from .landcover import curve_number_grid, ensure_tile

    tile = ensure_tile(bounds, os.path.join(data_dir(), "landcover"), progress=lambda *_: None)
    water = curve_number_grid(tile, bounds, transform, shape)["water_mask"]
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    np.save(cache, water)
    return water


def lonlat_lookup(transform, crs, shape, step: int = 16):
    """Coarse lon/lat lattice so a page can report coordinates under the cursor.

    UTM 50N -> WGS84 is not affine, but over a 10 km box the departure from
    bilinear interpolation on a 16-cell lattice is far below one metre.
    """
    import numpy as np
    from rasterio.warp import transform as warp_transform

    rows, cols = shape
    rs = sorted(set(list(range(0, rows, step)) + [rows - 1]))
    cs = sorted(set(list(range(0, cols, step)) + [cols - 1]))
    cc, rr = np.meshgrid(np.array(cs, dtype="float64"), np.array(rs, dtype="float64"))
    xs, ys = transform * (cc.ravel(), rr.ravel())
    lons, lats = warp_transform(crs, "EPSG:4326", list(xs), list(ys))
    return {
        "step": step,
        "rows": rs,
        "cols": cs,
        "lon": [round(v, 7) for v in lons],
        "lat": [round(v, 7) for v in lats],
    }


def project_lonlat(points, transform, target_crs="EPSG:32650") -> list:
    """Batch WGS84 -> grid coordinates. One CRS setup for the whole list."""
    from rasterio.warp import transform as warp_transform

    if not points:
        return []
    xs, ys = warp_transform("EPSG:4326", target_crs,
                            [p[0] for p in points], [p[1] for p in points])
    inverse = ~transform
    return [inverse * (x, y) for x, y in zip(xs, ys)]


def to_lonlat(points, transform, source_crs="EPSG:32650") -> list:
    """Batch grid coordinates -> WGS84, for vector export.

    The affine multiply needs arrays, not lists: with a list the left operand ends
    up being the sequence and the operation raises instead of broadcasting.
    """
    import numpy as np
    from rasterio.warp import transform as warp_transform

    if not points:
        return []
    cols = np.array([p[0] for p in points], dtype="float64")
    rows = np.array([p[1] for p in points], dtype="float64")
    xs, ys = transform * (cols, rows)
    lons, lats = warp_transform(source_crs, "EPSG:4326", list(xs), list(ys))
    return list(zip(lons, lats))


def simplify(points, tol=0.15) -> list:
    """Douglas-Peucker, iterative: OSM ways can carry thousands of vertices."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        x0, y0 = points[first]
        x1, y1 = points[last]
        dx, dy = x1 - x0, y1 - y0
        norm = (dx * dx + dy * dy) ** 0.5
        worst, worst_i = -1.0, -1
        for i in range(first + 1, last):
            px, py = points[i]
            if norm < 1e-12:
                dist = ((px - x0) ** 2 + (py - y0) ** 2) ** 0.5
            else:
                dist = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / norm
            if dist > worst:
                worst, worst_i = dist, i
        if worst > tol:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))
    return [p for p, k in zip(points, keep) if k]

"""Exposure analysis: turn a depth raster into a decision list.

A depth raster is not an answer. "Which road segments flood, how deep, and how
many buildings are affected" is. This module pulls real OpenStreetMap features
for the study area, rasterises them onto the model grid with the same transform,
and reports per-feature impact so a non-specialist can act on the result.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
import urllib.request

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

ROAD_QUERY = """
[out:json][timeout:{timeout}];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|pedestrian|footway)$"]({south},{west},{north},{east});
);
out geom tags;
"""

BUILDING_QUERY = """
[out:json][timeout:{timeout}];
(
  way["building"]({south},{west},{north},{east});
);
out geom tags;
"""

# Life-safety-critical POIs. An inundation map that says "1300 roads wet" is
# information; one that says "this hospital entrance and this subway exit are
# 0.6 m under water" is a decision.
POI_QUERY = """
[out:json][timeout:{timeout}];
(
  node["railway"="subway_entrance"]({south},{west},{north},{east});
  node["amenity"~"^(hospital|school|kindergarten|fire_station)$"]({south},{west},{north},{east});
  node["emergency"~"^(ambulance_station|fire_station)$"]({south},{west},{north},{east});
  way["amenity"~"^(hospital|school)$"]({south},{west},{north},{east});
);
out center tags;
"""

QUERIES = {"roads": ROAD_QUERY, "buildings": BUILDING_QUERY, "critical": POI_QUERY}

# Step used when walking a line across the depth grid, in PIXELS.
#
# This must not be derived from the cell size in metres: the walk happens in
# pixel space, so a 14-"metre" step is 14 pixels and is larger than most OSM
# segments. Densification then adds nothing and every road segment contributes a
# single depth sample taken at its start node, which quietly turns a "flooded
# length" statistic into "did this segment begin in water".
DENSIFY_STEP_PX = 0.5


def fetch_osm(
    bounds_wgs84: tuple[float, float, float, float],
    cache_dir: str,
    kind: str = "roads",
    timeout: int = 180,
    progress=print,
) -> str:
    """Download an Overpass extract, cached on disk so re-runs are offline-safe."""
    os.makedirs(cache_dir, exist_ok=True)
    west, south, east, north = bounds_wgs84
    dest = os.path.join(cache_dir, "osm_%s_%.4f_%.4f_%.4f_%.4f.json" % (kind, west, south, east, north))
    if os.path.exists(dest) and os.path.getsize(dest) > 512:
        progress("cached  %s" % os.path.basename(dest))
        return dest

    template = QUERIES.get(kind)
    if template is None:
        raise ValueError("unknown OSM query kind: %s (expected one of %s)" % (kind, ", ".join(QUERIES)))
    query = template.format(west=west, south=south, east=east, north=north, timeout=timeout)
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        started = time.time()
        try:
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={"User-Agent": "floodkit/0.1 (urban ponding research)", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=timeout + 30) as response:
                payload = response.read()
            parsed = json.loads(payload.decode("utf-8"))
            elements = parsed.get("elements", [])
            if not elements:
                raise ValueError("Overpass returned no elements for %s" % kind)
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, dest)
            progress("fetched %s: %d elements in %.1fs" % (kind, len(elements), time.time() - started))
            return dest
        except Exception as exc:  # try the next mirror, report if all fail
            last_error = exc
            progress("overpass mirror failed (%s): %s" % (endpoint, exc))
    raise RuntimeError("all Overpass mirrors failed for %s: %s" % (kind, last_error))


def load_features(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("elements", [])


def _project(lon: float, lat: float, transform):
    """WGS84 -> grid coordinates using the grid's own transform.

    Note the direction: ``transform * (col, row)`` maps pixel -> world, so
    converting world coordinates into grid positions needs the inverse. Getting
    this backwards silently places every feature outside the raster, which shows
    up as "no roads flooded" rather than as an error.
    """
    from rasterio.warp import transform as warp_transform

    xs, ys = warp_transform("EPSG:4326", "EPSG:32650", [lon], [lat])
    return ~transform * (xs[0], ys[0])


def _project_many(points: list[tuple[float, float]], transform) -> list[tuple[float, float]]:
    """Batch WGS84 -> grid projection.

    PROJ setup dominates the cost of a single transform call, so transforming
    thousands of vertices one at a time makes the exposure step unusable; this
    projects the whole batch through one CRS pair.
    """
    if not points:
        return []
    from rasterio.warp import transform as warp_transform

    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    xs, ys = warp_transform("EPSG:4326", "EPSG:32650", lons, lats)
    inverse = ~transform
    return [inverse * (x, y) for x, y in zip(xs, ys)]


def _densify(points: list[tuple[float, float]], step_m: float) -> list[tuple[float, float]]:
    """Insert intermediate points so no cell is stepped over."""
    if len(points) < 2:
        return points
    out = [points[0]]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        distance = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(distance / max(step_m, 1e-6)))
        for i in range(1, steps + 1):
            t = i / steps
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return out


def road_impact(
    depth_m,
    elements: list[dict],
    transform,
    cell_size_m: float,
    min_depth_m: float = 0.05,
    top_n: int | None = 40,
    include_geometry: bool = False,
) -> dict:
    """Per-way inundation statistics for OSM roads.

    The result table is capped for the JSON report, but the renderers and the
    GeoJSON export need every flooded way. Asking this function for them keeps one
    implementation of the sampling rule instead of three that drift: `top_n=None`
    returns all of them, and `include_geometry` attaches the lon/lat ring that a
    vector export needs and a JSON report does not.
    """
    import numpy as np

    rows, cols = depth_m.shape
    segments = []
    type_counts: dict[str, int] = {}
    flooded_type_counts: dict[str, int] = {}
    total_length = 0.0
    flooded_length = 0.0

    for element in elements:
        geometry = element.get("geometry") or []
        if len(geometry) < 2:
            continue
        raw = [(pt["lon"], pt["lat"]) for pt in geometry if "lon" in pt and "lat" in pt]
        if len(raw) < 2:
            continue
        grid = _project_many(raw, transform)
        dense = _densify(grid, DENSIFY_STEP_PX)

        length = 0.0
        flooded = 0.0
        max_depth = 0.0
        for (x0, y0), (x1, y1) in zip(dense, dense[1:]):
            r0, c0 = int(y0), int(x0)
            step = math.hypot(x1 - x0, y1 - y0)
            length += step
            if 0 <= r0 < rows and 0 <= c0 < cols:
                d = float(depth_m[r0, c0])
                if d > min_depth_m:
                    flooded += step
                    max_depth = max(max_depth, d)

        if length <= 0:
            continue
        tags = element.get("tags", {})
        highway = tags.get("highway", "unknown")
        type_counts[highway] = type_counts.get(highway, 0) + 1
        total_length += length
        flooded_length += flooded
        if flooded > 0:
            flooded_type_counts[highway] = flooded_type_counts.get(highway, 0) + 1
            segment = {
                "osm_id": element.get("id"),
                "name": tags.get("name") or tags.get("ref") or "(unnamed)",
                "highway": highway,
                "length_m": length,
                "flooded_length_m": flooded,
                "max_depth_m": max_depth,
                "lanes": tags.get("lanes"),
            }
            if include_geometry:
                segment["geometry_lonlat"] = raw
            segments.append(segment)

    segments.sort(key=lambda s: -s["flooded_length_m"])
    return {
        "ways_total": sum(type_counts.values()),
        "ways_flooded": len(segments),
        "total_length_m": total_length,
        "flooded_length_m": flooded_length,
        "flooded_length_fraction": (flooded_length / total_length) if total_length else 0.0,
        "by_highway": type_counts,
        "flooded_by_highway": flooded_type_counts,
        "top_segments": segments if top_n is None else segments[:top_n],
    }


def building_impact(
    depth_m,
    elements: list[dict],
    transform,
    cell_size_m: float,
    min_depth_m: float = 0.05,
    sample_step_px: float = 0.5,
    max_samples: int = 40_000,
    top_n: int | None = 25,
    include_geometry: bool = False,
) -> dict:
    """Building footprint exposure, measured on the rasterised footprint.

    The footprint interior is sampled on a lattice rather than tested at the
    centroid, because a centroid test systematically misses long thin buildings
    and under-counts large ones. The lattice step is in *pixels* — mixing pixel
    and metre units here silently samples almost nothing and reports zero
    affected buildings.
    """
    import numpy as np

    rows, cols = depth_m.shape
    total = 0
    affected = 0
    affected_area = 0.0
    deepest = []
    cell_area = cell_size_m * cell_size_m

    for element in elements:
        geometry = element.get("geometry") or []
        ring = [(pt["lon"], pt["lat"]) for pt in geometry if "lon" in pt and "lat" in pt]
        if len(ring) < 3:
            continue
        grid = np.array(_project_many(ring, transform), dtype="float64")
        x_min, y_min = grid.min(axis=0)
        x_max, y_max = grid.max(axis=0)
        if x_max - x_min <= 0 or y_max - y_min <= 0:
            continue
        total += 1

        step = sample_step_px
        span_x = max(x_max - x_min, step)
        span_y = max(y_max - y_min, step)
        if (span_x / step) * (span_y / step) > max_samples:
            step = float(np.sqrt(span_x * span_y / max_samples))

        xs = np.arange(x_min, x_max + step, step)
        ys = np.arange(y_min, y_max + step, step)
        grid_x, grid_y = np.meshgrid(xs, ys)
        inside = _points_in_polygon(grid_x.ravel(), grid_y.ravel(), grid)
        if not inside.any():
            continue
        px = grid_x.ravel()[inside]
        py = grid_y.ravel()[inside]
        rr = py.astype("int64")
        cc = px.astype("int64")
        valid = (rr >= 0) & (rr < rows) & (cc >= 0) & (cc < cols)
        if not valid.any():
            continue
        rr = rr[valid]
        cc = cc[valid]
        depths = depth_m[rr, cc]
        wet = depths > min_depth_m
        footprint_area = float(inside.sum()) * (step * step) * cell_area
        if wet.any():
            affected += 1
            affected_area += footprint_area * float(wet.mean())
            entry = {
                "osm_id": element.get("id"),
                "name": (element.get("tags") or {}).get("name", "(unnamed)"),
                "footprint_area_m2": footprint_area,
                "max_depth_m": float(depths.max()),
                "flooded_fraction": float(wet.mean()),
            }
            if include_geometry:
                entry["geometry_lonlat"] = ring
            deepest.append(entry)

    deepest.sort(key=lambda b: -b["max_depth_m"])
    return {
        "buildings_total": total,
        "buildings_affected": affected,
        "affected_fraction": (affected / total) if total else 0.0,
        "affected_footprint_area_m2": affected_area,
        "top_buildings": deepest if top_n is None else deepest[:top_n],
    }


def _is_underpass(tags: dict) -> bool:
    """Road running below grade.

    Flooding here is a life-safety event, not an inconvenience: a subgrade road
    fills from both ends with no escape route. Treating these identically to a
    wet kerb is how the ranking of "worst" spots ends up wrong.
    """
    if tags.get("tunnel") in ("yes", "building_passage", "culvert", "avalanche_protector"):
        return True
    if tags.get("covered") == "yes":
        return True
    layer = tags.get("layer")
    try:
        return layer is not None and float(layer) < 0
    except (TypeError, ValueError):
        return False


def _way_depth_profile(element, depth_m, transform, cell_size_m, min_depth_m):
    """Walk one way's geometry, returning (length, flooded_length, max_depth)."""
    geometry = element.get("geometry") or []
    if len(geometry) < 2:
        return None
    raw = [(pt["lon"], pt["lat"]) for pt in geometry if "lon" in pt and "lat" in pt]
    if len(raw) < 2:
        return None
    dense = _densify(_project_many(raw, transform), DENSIFY_STEP_PX)
    rows, cols = depth_m.shape
    length = 0.0
    flooded = 0.0
    max_depth = 0.0
    for (x0, y0), (x1, y1) in zip(dense, dense[1:]):
        r, c = int(y0), int(x0)
        step = math.hypot(x1 - x0, y1 - y0)
        length += step
        if 0 <= r < rows and 0 <= c < cols:
            d = float(depth_m[r, c])
            if d > min_depth_m:
                flooded += step
                max_depth = max(max_depth, d)
    return length, flooded, max_depth


def underpass_segments(
    depth_m,
    elements: list[dict],
    transform,
    cell_size_m: float,
    min_depth_m: float = 0.05,
    include_unflooded: bool = True,
    include_geometry: bool = False,
) -> list[dict]:
    """Every below-grade section with its own flooding statistics.

    This is the drawing view, not the reporting view: it keeps the sections that
    stayed dry, because a map that only draws the flooded ones cannot show the
    reader what "not flooded" looks like — 303 of the 420 sections in the
    reference run fall in that category. `underpass_impact` slices this list for
    the report, so the sampling rule exists once.

    Returned in OSM source order, deliberately unsorted, and the totals below are
    accumulated with `+=` rather than `sum()`. Two separate traps, both of which
    move the last bits of a reported total:

    * floating-point addition is not associative, so summing a length-sorted list
      changes the result;
    * since Python 3.12 `sum()` applies Neumaier compensation to floats, so it is
      *more* accurate than a plain accumulation and therefore does not reproduce
      one. "The numbers are reproducible" is a claim this project makes in
      writing, so the accumulation matches the report's, not the most accurate one.

    Callers that want a decision list sort by `max_depth_m` themselves.
    """
    segments = []
    for element in elements:
        tags = element.get("tags") or {}
        if not _is_underpass(tags):
            continue
        stats = _way_depth_profile(element, depth_m, transform, cell_size_m, min_depth_m)
        if stats is None:
            continue
        length, flooded, max_depth = stats
        if flooded <= 0 and not include_unflooded:
            continue
        entry = {
            "osm_id": element.get("id"),
            "name": tags.get("name") or tags.get("ref") or "(unnamed)",
            "highway": tags.get("highway"),
            "tunnel": tags.get("tunnel"),
            "layer": tags.get("layer"),
            "length_m": length,
            "flooded_length_m": flooded,
            "max_depth_m": max_depth,
            "risk": "life-safety" if max_depth >= 0.3 else ("watch" if flooded > 0 else "dry"),
        }
        if include_geometry:
            entry["geometry_lonlat"] = [
                (pt["lon"], pt["lat"]) for pt in (element.get("geometry") or []) if "lon" in pt
            ]
        segments.append(entry)
    return segments


def underpass_impact(
    depth_m,
    elements: list[dict],
    transform,
    cell_size_m: float,
    min_depth_m: float = 0.05,
    top_n: int = 30,
    include_geometry: bool = False,
) -> dict:
    """Flooding of below-grade road sections: tunnels, underpasses, covered ways."""
    all_segments = underpass_segments(
        depth_m, elements, transform, cell_size_m, min_depth_m,
        include_unflooded=True, include_geometry=include_geometry,
    )
    # Accumulated with `+=` in source order, not with `sum()`: see the note on
    # underpass_segments for why both choices matter to the last bit.
    total = len(all_segments)
    total_length = 0.0
    flooded_length = 0.0
    segments = []
    for seg in all_segments:
        total_length += seg["length_m"]
        if seg["flooded_length_m"] > 0:
            flooded_length += seg["flooded_length_m"]
            segments.append(seg)
    segments.sort(key=lambda s: (-s["max_depth_m"], -s["flooded_length_m"]))
    # Split vehicle tunnels from pedestrian underpasses. Both are life-safety
    # relevant, but a decision list dominated by unnamed footways buries the
    # vehicle tunnels a response plan actually has to close.
    foot_types = ("footway", "pedestrian", "path", "steps", "cycleway")
    by_highway: dict[str, int] = {}
    for seg in segments:
        key = seg["highway"] or "unknown"
        by_highway[key] = by_highway.get(key, 0) + 1
    return {
        "underpass_ways": total,
        "underpass_ways_flooded": len(segments),
        "underpass_total_length_m": total_length,
        "underpass_flooded_length_m": flooded_length,
        "life_safety_count": sum(1 for s in segments if s["risk"] == "life-safety"),
        "vehicle_life_safety_count": sum(
            1 for s in segments
            if s["risk"] == "life-safety" and (s["highway"] or "") not in foot_types
        ),
        "flooded_by_highway": by_highway,
        "top_segments": segments[:top_n],
        "top_vehicle_segments": [s for s in segments if (s["highway"] or "") not in foot_types][:20],
    }


def critical_facility_impact(
    depth_m,
    elements: list[dict],
    transform,
    cell_size_m: float,
    min_depth_m: float = 0.05,
    top_n: int | None = 30,
) -> dict:
    """Point facilities: subway entrances, hospitals, schools, fire stations.

    Point features are sampled over a 3x3 neighbourhood rather than at one cell:
    a facility is an area, not a mathematical point, and a coordinate that lands
    on a cell boundary would otherwise be decided by floating-point truncation —
    a projected point can come back as 4.999999999985 and be read as the cell
    above. Subway entrances matter most here: a flooded entrance is an ingress
    path into the underground network.
    """
    import numpy as np

    rows, cols = depth_m.shape
    facilities = []
    by_kind: dict[str, int] = {}
    flooded_by_kind: dict[str, int] = {}

    for element in elements:
        tags = element.get("tags") or {}
        if element.get("type") == "node":
            lon, lat = element.get("lon"), element.get("lat")
        else:
            center = element.get("center") or {}
            lon, lat = center.get("lon"), center.get("lat")
        if lon is None or lat is None:
            continue
        kind = tags.get("railway") or tags.get("amenity") or tags.get("emergency") or "other"
        by_kind[kind] = by_kind.get(kind, 0) + 1

        (x, y), = _project_many([(lon, lat)], transform)
        r, c = int(y), int(x)
        if not (0 <= r < rows and 0 <= c < cols):
            continue
        window = depth_m[max(0, r - 1) : r + 2, max(0, c - 1) : c + 2]
        d = float(np.nanmax(window)) if window.size else 0.0
        if not np.isfinite(d) or d <= min_depth_m:
            continue
        flooded_by_kind[kind] = flooded_by_kind.get(kind, 0) + 1
        facilities.append(
            {
                "osm_id": element.get("id"),
                "kind": kind,
                "name": tags.get("name") or "(unnamed)",
                "lon": lon,
                "lat": lat,
                "depth_m": d,
            }
        )

    facilities.sort(key=lambda f: -f["depth_m"])
    return {
        "facilities_total": sum(by_kind.values()),
        "facilities_flooded": len(facilities),
        "by_kind": by_kind,
        "flooded_by_kind": flooded_by_kind,
        "subway_entrances_flooded": flooded_by_kind.get("subway_entrance", 0),
        "top_facilities": facilities if top_n is None else facilities[:top_n],
    }


def _points_in_polygon(x, y, polygon) -> "np.ndarray":
    """Vectorised even-odd rule; polygon is an (N,2) array of grid coordinates."""
    import numpy as np

    inside = np.zeros(x.shape, dtype=bool)
    n = polygon.shape[0]
    for i in range(n):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % n]
        if y0 == y1:
            continue
        cond = ((y0 > y) != (y1 > y))
        with np.errstate(divide="ignore", invalid="ignore"):
            x_cross = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
        inside ^= cond & (x < x_cross)
    return inside

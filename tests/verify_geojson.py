"""Verify the GeoJSON exports against the run they came from.

A vector export is the one deliverable a reader can check without trusting any of
this project's code: open it in QGIS, click a polygon, read the attribute table.
That only works if the file is well formed and if its numbers are the report's
numbers, so both are checked here.

The load-bearing check is the area reconciliation. The reported ponded area counts
every cell with any water at all, while every map used to start at 0.05 m and
therefore drew 6.5% less than the headline it printed. Exporting the trace band and
summing the polygons back is what caught it; this test is what stops it coming back.

    python tests/verify_geojson.py [tag ...]
"""

from __future__ import annotations

import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

FAILURES: list[str] = []

LAYER_GEOMETRY = {
    "ponding_depth_class": {"Polygon", "MultiPolygon"},
    "underpass_section": {"LineString", "MultiLineString"},
    "flooded_road": {"LineString", "MultiLineString"},
    "critical_facility": {"Point"},
    "affected_building": {"Polygon", "MultiPolygon"},
}

AREA_TOLERANCE = 1e-3   # 0.1%: the polygons are written with 6-decimal coordinates


def check(ok: bool, msg: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        FAILURES.append(msg)


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def ring_area(ring) -> float:
    """Shoelace area of a ring given in projected metres."""
    x = np.array([p[0] for p in ring], dtype="float64")
    y = np.array([p[1] for p in ring], dtype="float64")
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def metric_ring_areas(features: list) -> list:
    """Area of every polygon in the file, measured in metres.

    The file is in degrees, where a shoelace area is a number in square degrees —
    meaningless and, at these magnitudes, indistinguishable from zero. Every vertex
    is reprojected in one batch and the areas are taken in the metric CRS the model
    actually computed in.
    """
    from rasterio.warp import transform as warp_transform

    flat, polygons = [], []
    for feature in features:
        for rings in geom_rings(feature["geometry"]):
            spans = []
            for ring in rings:
                spans.append((len(flat), len(ring)))
                flat.extend(ring)
            polygons.append(spans)
    xs, ys = warp_transform("EPSG:4326", "EPSG:32650",
                            [p[0] for p in flat], [p[1] for p in flat])
    metric = list(zip(xs, ys))

    areas = []
    for spans in polygons:
        outer = metric[spans[0][0]:spans[0][0] + spans[0][1]]
        area = ring_area(outer)
        for start, length in spans[1:]:
            area -= ring_area(metric[start:start + length])
        areas.append(area)
    return areas


def geom_rings(geom) -> list:
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return list(geom["coordinates"])
    return []


def all_points(geom) -> list:
    kind = geom["type"]
    if kind == "Point":
        return [geom["coordinates"]]
    if kind == "LineString":
        return list(geom["coordinates"])
    if kind == "MultiLineString":
        return [p for line in geom["coordinates"] for p in line]
    return [p for rings in geom_rings(geom) for ring in rings for p in ring]


def check_collection(doc: dict, name: str) -> None:
    check(doc.get("type") == "FeatureCollection", "%s is a FeatureCollection" % name)
    check(doc.get("crs", {}).get("properties", {}).get("name", "").endswith("CRS84"),
          "%s declares CRS84 (lon/lat) in its header" % name)
    check(isinstance(doc.get("features"), list) and doc["features"],
          "%s carries features" % name)
    check(bool(doc.get("metadata", {}).get("note")),
          "%s states what it contains and what it excludes" % name)


def main() -> int:
    tags = sys.argv[1:] or ["nanjing_final"]
    for tag in tags:
        print("=" * 72)
        print("verifying the GeoJSON exports of %s" % tag)
        from floodkit.runio import EXPOSURE_MIN_DEPTH_M, bounds_wgs84, load_run

        report, depth, transform, crs, shape, cell_m = load_run(tag)
        west, south, east, north = bounds_wgs84(transform, crs, shape)
        pad = 0.01  # ~1 km, enough for a simplified footprint to stay inside

        # ---- ponding --------------------------------------------------------
        path = os.path.join(ROOT, "data", "runs", tag, "%s_ponding.geojson" % tag)
        doc = load(path)
        check_collection(doc, "ponding.geojson")
        features = doc["features"]
        print("  %d depth polygons" % len(features))

        bad_geom = [f for f in features if f["geometry"]["type"] not in LAYER_GEOMETRY["ponding_depth_class"]]
        check(not bad_geom, "every feature is a polygon")
        bad_layer = [f for f in features if f["properties"].get("layer") != "ponding_depth_class"]
        check(not bad_layer, "every feature carries the ponding layer tag")

        outside = 0
        unclosed = 0
        for feature in features:
            for rings in geom_rings(feature["geometry"]):
                for ring in rings:
                    if ring[0] != ring[-1]:
                        unclosed += 1
                    for lon, lat in ring:
                        if not (west - pad <= lon <= east + pad and south - pad <= lat <= north + pad):
                            outside += 1
        check(outside == 0, "every vertex lies inside the run's footprint (%d outside)" % outside)
        check(unclosed == 0, "every polygon ring is closed (%d unclosed)" % unclosed)

        codes = sorted({f["properties"]["class_code"] for f in features})
        check(all(1 <= c <= 6 for c in codes),
              "class codes stay inside the shared 1..6 bands (%s)" % codes)

        stated = sum(f["properties"]["area_m2"] for f in features)
        measured = sum(metric_ring_areas(features))
        check(abs(measured - stated) / max(stated, 1) < AREA_TOLERANCE,
              "the stated area matches the geometry in metres to %.4f%%"
              % (100 * abs(measured - stated) / max(stated, 1)))

        reported = report["ponding"]["pond_area_m2"]
        check(abs(measured - reported) / reported < AREA_TOLERANCE,
              "polygon area %.0f m² reconciles with the reported ponded area %.0f m² (%+.4f%%)"
              % (measured, reported, 100 * (measured - reported) / reported))

        # The trace band is the whole reason the total reconciles; if it ever goes
        # missing the sum drops by 6% and this catches it by name.
        trace = [f for f in features if f["properties"]["class_code"] == 1]
        trace_area = sum(f["properties"]["area_m2"] for f in trace)
        wet_cells = int((depth > 0).sum())
        byte_cells = int(((depth > 0) & (depth < 0.05)).sum())
        check(abs(trace_area - byte_cells * abs(transform.a * transform.e))
              / max(trace_area, 1) < AREA_TOLERANCE,
              "the trace band holds the %d cells under %.2f m (%.4f km²)"
              % (byte_cells, 0.05, trace_area / 1e6))
        check(wet_cells * abs(transform.a * transform.e) > trace_area,
              "the trace band is a real part of the %d wet cells" % wet_cells)

        # ---- exposure -------------------------------------------------------
        path = os.path.join(ROOT, "data", "runs", tag, "%s_exposure.geojson" % tag)
        doc = load(path)
        check_collection(doc, "exposure.geojson")
        features = doc["features"]
        by_layer: dict[str, list] = {}
        for feature in features:
            by_layer.setdefault(feature["properties"]["layer"], []).append(feature)
        unknown = set(by_layer) - set(LAYER_GEOMETRY)
        check(not unknown, "no unexpected layer values (%s)" % (unknown or "none"))

        exposure = report.get("exposure") or {}
        expected = {
            "underpass_section": (exposure.get("underpasses") or {}).get("underpass_ways"),
            "critical_facility": (exposure.get("critical_facilities") or {}).get("facilities_flooded"),
            "flooded_road": (exposure.get("roads") or {}).get("ways_flooded"),
            "affected_building": (exposure.get("buildings") or {}).get("buildings_affected"),
        }
        for layer, want in expected.items():
            got = len(by_layer.get(layer, []))
            check(want is not None and got == want,
                  "%s: %d features = the report's %s" % (layer, got, want))

        for layer, features_ in by_layer.items():
            allowed = LAYER_GEOMETRY[layer]
            wrong = [f for f in features_ if f["geometry"]["type"] not in allowed]
            check(not wrong, "%s geometries are %s" % (layer, "/".join(sorted(allowed))))

        # OSM ways come back from Overpass as complete geometries, so a road that
        # crosses the study area keeps its vertices on both sides of it. The
        # analysis sampled only the cells inside the grid; the export does not clip,
        # because a half-road is less useful in a GIS than the whole road with its
        # flooded length as an attribute. What is checked here is that nothing is
        # wildly displaced — a leaked CRS or a swapped lon/lat would show up as
        # vertices hundreds of kilometres away.
        points = [p for f in features for p in all_points(f["geometry"])]
        far = [p for p in points
               if not (west - 0.5 <= p[0] <= east + 0.5 and south - 0.5 <= p[1] <= north + 0.5)]
        inside = sum(1 for p in points
                     if west - pad <= p[0] <= east + pad and south - pad <= p[1] <= north + pad)
        check(not far, "no vertex is displaced beyond 0.5 deg of the footprint (%d far)" % len(far))
        check(inside / max(len(points), 1) > 0.7,
              "%.1f%% of the %d exposure vertices fall inside the footprint (the rest belong "
              "to OSM ways that run past it and are exported unclipped)" %
              (100 * inside / max(len(points), 1), len(points)))

        lines = by_layer.get("underpass_section", [])
        life = [f for f in lines if f["properties"]["risk"] == "life-safety"]
        check(len(life) == (exposure.get("underpasses") or {}).get("life_safety_count"),
              "life-safety sections: %d = the report's %s"
              % (len(life), (exposure.get("underpasses") or {}).get("life_safety_count")))
        flooded = [f for f in lines if f["properties"]["flooded"]]
        check(len(flooded) == (exposure.get("underpasses") or {}).get("underpass_ways_flooded"),
              "flooded sections: %d = the report's %s"
              % (len(flooded), (exposure.get("underpasses") or {}).get("underpass_ways_flooded")))
        check(len(lines) - len(flooded) > 0,
              "the dry sections are exported too, not only the flooded %d" % len(flooded))

        deep_facilities = [f for f in by_layer.get("critical_facility", [])
                           if f["properties"]["depth_m"] > EXPOSURE_MIN_DEPTH_M]
        check(len(deep_facilities) == len(by_layer.get("critical_facility", [])),
              "every exported facility exceeds the %.2f m exposure threshold"
              % EXPOSURE_MIN_DEPTH_M)

        for layer in ("underpass_section", "flooded_road", "critical_facility", "affected_building"):
            feats = by_layer.get(layer, [])
            check(all(isinstance(f["properties"].get("osm_id"), int) for f in feats),
                  "%s features carry an OSM id that can be looked up" % layer)

        lonlat_ok = all(
            -180 <= p[0] <= 180 and -90 <= p[1] <= 90
            for f in features for p in all_points(f["geometry"])
        )
        check(lonlat_ok, "coordinates are degrees, not projected metres")

        sizes = {os.path.basename(p): os.path.getsize(p) / 1e6
                 for p in (os.path.join(ROOT, "data", "runs", tag, "%s_%s.geojson" % (tag, s))
                           for s in ("ponding", "exposure"))}
        print("  sizes: " + ", ".join("%s %.2f MB" % kv for kv in sorted(sizes.items())))
        _ = (math, np)

    print("=" * 72)
    if FAILURES:
        print("FAILED: %d check(s)" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("GeoJSON exports match the runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

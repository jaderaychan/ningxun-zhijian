"""Confirm the exposure refactor did not move a single reported number.

`floodkit.exposure` gained `top_n` / `include_geometry` / `include_unflooded`
parameters so the renderers and the GeoJSON export can ask for the whole layer
instead of reimplementing the sampling rule three times. Every default is
supposed to reproduce the old output exactly, and that claim is only worth
something if it is checked.

One deliberate asymmetry: the depth raster is float32 on disk while a run works
in float64, so anything read back off the GeoTIFF is compared to float32
precision. Everything driven by a *comparison* rather than a value — the flooded
lengths, the counts, the totals — is compared exactly, because those are the
numbers that appear in the report and in the documentation.

    python tests/check_exposure_refactor.py [tag]
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

FAILURES: list[str] = []

# float32 carries ~7 significant decimal digits; 1e-5 relative leaves an order of
# magnitude of headroom without letting a real change through.
REL = 1e-5


def check(ok: bool, msg: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        FAILURES.append(msg)


def close(a, b) -> bool:
    scale = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / scale < REL


def same_segment(a: dict, b: dict) -> bool:
    """Lengths exactly (they are comparison-driven), depths to float32 precision."""
    for key in set(a) | set(b):
        if key not in a or key not in b:
            return False
        if key in ("max_depth_m", "depth_m"):
            if not close(float(a[key]), float(b[key])):
                return False
        elif isinstance(a[key], float):
            if abs(a[key] - b[key]) > 1e-9:
                return False
        elif a[key] != b[key]:
            return False
    return True


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "nanjing_final"
    run_dir = os.path.join(ROOT, "data", "runs", tag)
    report = json.load(open(os.path.join(run_dir, "%s_report.json" % tag), encoding="utf-8"))

    import rasterio

    from floodkit.exposure import (
        critical_facility_impact,
        load_features,
        road_impact,
        underpass_impact,
        underpass_segments,
    )

    from floodkit.runio import bounds_wgs84, load_run, select_osm

    # Read the run the same way every output tool does, so this test exercises the
    # shared path (including the OSM cache selection) rather than its own copy.
    report, depth, transform, crs, shape, cell_m = load_run(tag)
    osm_paths = select_osm(report, bounds_wgs84(transform, crs, shape), verbose=False)
    roads = load_features(osm_paths["roads"])
    critical = load_features(osm_paths["critical"])

    print("[%s] underpass_impact defaults reproduce the report" % tag)
    got = underpass_impact(depth, roads, transform, cell_m, 0.05)
    want = report["exposure"]["underpasses"]
    check(set(got) == set(want), "same keys (%s)" % (set(got) ^ set(want) or "identical"))
    for key in ("underpass_ways", "underpass_ways_flooded", "life_safety_count",
                "vehicle_life_safety_count", "flooded_by_highway"):
        check(got[key] == want[key], "%s = %s" % (key, want[key]))
    # These two are sums of step lengths, so they are driven by which cells exceed
    # the threshold, not by the depth values. They must match to the last bit.
    check(got["underpass_total_length_m"] == want["underpass_total_length_m"],
          "underpass_total_length_m = %.12f" % want["underpass_total_length_m"])
    check(got["underpass_flooded_length_m"] == want["underpass_flooded_length_m"],
          "underpass_flooded_length_m = %.12f" % want["underpass_flooded_length_m"])
    for key in ("top_segments", "top_vehicle_segments"):
        check(len(got[key]) == len(want[key]), "%s length %d" % (key, len(want[key])))
        check(all(same_segment(a, b) for a, b in zip(got[key], want[key])),
              "%s values and order identical" % key)

    print("[%s] critical_facility_impact defaults reproduce the report" % tag)
    got = critical_facility_impact(depth, critical, transform, cell_m, 0.05)
    want = report["exposure"]["critical_facilities"]
    check(got["facilities_total"] == want["facilities_total"],
          "facilities_total = %s" % want["facilities_total"])
    check(got["facilities_flooded"] == want["facilities_flooded"],
          "facilities_flooded = %s" % want["facilities_flooded"])
    check(got["subway_entrances_flooded"] == want["subway_entrances_flooded"],
          "subway_entrances_flooded = %s" % want["subway_entrances_flooded"])
    check(got["flooded_by_kind"] == want["flooded_by_kind"], "flooded_by_kind identical")
    check(len(got["top_facilities"]) == len(want["top_facilities"]),
          "top_facilities length %d" % len(want["top_facilities"]))
    check(all(same_segment(a, b) for a, b in zip(got["top_facilities"], want["top_facilities"])),
          "top_facilities values and order identical")

    print("[%s] road_impact defaults reproduce the report" % tag)
    got = road_impact(depth, roads, transform, cell_m, 0.05)
    want = report["exposure"]["roads"]
    check(got["ways_total"] == want["ways_total"], "ways_total = %s" % want["ways_total"])
    check(got["ways_flooded"] == want["ways_flooded"], "ways_flooded = %s" % want["ways_flooded"])
    check(got["total_length_m"] == want["total_length_m"],
          "total_length_m = %.12f" % want["total_length_m"])
    check(got["flooded_length_m"] == want["flooded_length_m"],
          "flooded_length_m = %.12f" % want["flooded_length_m"])
    check(len(got["top_segments"]) == len(want["top_segments"]),
          "top_segments length %d" % len(want["top_segments"]))
    check(all(same_segment(a, b) for a, b in zip(got["top_segments"], want["top_segments"])),
          "top_segments values and order identical")

    print("[%s] the uncapped views expose strictly more, never different" % tag)
    capped = underpass_impact(depth, roads, transform, cell_m, 0.05)
    full = underpass_impact(depth, roads, transform, cell_m, 0.05, top_n=None)
    check(len(full["top_segments"]) == full["underpass_ways_flooded"],
          "top_n=None returns all %d flooded sections" % full["underpass_ways_flooded"])
    check([s["osm_id"] for s in full["top_segments"][:30]]
          == [s["osm_id"] for s in capped["top_segments"]],
          "the first 30 of the full list are the capped list, in the same order")
    check(all(full[k] == capped[k] for k in capped if not k.startswith("top_")),
          "every summary count is unchanged by top_n")

    capped_f = critical_facility_impact(depth, critical, transform, cell_m, 0.05)
    full_f = critical_facility_impact(depth, critical, transform, cell_m, 0.05, top_n=None)
    check(len(full_f["top_facilities"]) == full_f["facilities_flooded"],
          "top_n=None returns all %d flooded facilities" % full_f["facilities_flooded"])
    check([f["osm_id"] for f in full_f["top_facilities"][:30]]
          == [f["osm_id"] for f in capped_f["top_facilities"]],
          "the first 30 of the full facility list are the capped list")

    segs = underpass_segments(depth, roads, transform, cell_m, 0.05, include_unflooded=True)
    check(len(segs) == capped["underpass_ways"],
          "the drawing view carries all %d sections, flooded and dry" % capped["underpass_ways"])
    check(sum(1 for s in segs if s["flooded_length_m"] > 0) == capped["underpass_ways_flooded"],
          "of which %d are flooded" % capped["underpass_ways_flooded"])
    check(sum(1 for s in segs if s["risk"] == "dry") == len(segs) - capped["underpass_ways_flooded"],
          "every non-flooded section is labelled dry")
    check(sum(s["length_m"] for s in segs) == capped["underpass_total_length_m"]
          or abs(sum(s["length_m"] for s in segs) - capped["underpass_total_length_m"]) < 1e-9,
          "the drawing view sums to the reported total")
    with_geom = underpass_segments(depth, roads, transform, cell_m, 0.05, include_geometry=True)
    check(all(len(s["geometry_lonlat"]) >= 2 for s in with_geom),
          "include_geometry attaches a usable lon/lat line to every section")

    print("=" * 60)
    if FAILURES:
        print("FAILED: %d" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("exposure refactor is output-preserving")
    _ = np
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

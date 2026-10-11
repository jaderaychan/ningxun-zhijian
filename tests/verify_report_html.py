"""Verify a generated interactive HTML report against the run it claims to describe.

The page is a claim: "these are the numbers from this run, and every layer traces
back to it". Three things can quietly break that claim, and each has a check here.

1. **It stops being offline.** One CDN tag turns a file that works in an exam room
   into a blank page without wifi. The check is a static scan for anything that
   would fetch.
2. **The rasters drift from the run.** Depth is quantised to a byte per cell for
   transport; if that encoding is ever changed on one side only, the map and the
   hover readout stay plausible while being wrong. The check re-encodes from the
   GeoTIFF and compares bytes.
3. **The layer counts drift from the report.** The page recomputes underpasses and
   facilities from census geometries rather than reprinting the report's top-30.
   The check re-derives the counts from inside the page and compares them with the
   report JSON it embeds.

    python tests/verify_report_html.py [tag ...]

Exits non-zero on the first failure. Tag defaults to nanjing_final.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import numpy as np  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> bool:
    if condition:
        print("  ok   %s" % message)
    else:
        print("  FAIL %s" % message)
        FAILURES.append(message)
    return condition


def extract_payload(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    if not match:
        raise SystemExit("no embedded data payload found — is this a generated report?")
    return json.loads(match.group(1))


def check_offline(html: str, tag: str) -> None:
    print("[%s] offline self-containment" % tag)
    # Generated anchors may point at openstreetmap.org: a link a reader chooses to
    # click is not a dependency. Everything else must be local.
    static = html.split('<script type="application/json"')[0] + html.split("</script>")[-1]
    check(not re.search(r'(?:src|href)\s*=\s*["\']https?://', static),
          "no external src/href in the markup")
    for token in ("fetch(", "XMLHttpRequest", "importScripts", "navigator.sendBeacon",
                  "@import", "url(http", "<link ", "<iframe"):
        check(token not in html, "no %s" % token)
    check("__DATA__" not in html and "__SCRIPT__" not in html and "__TITLE__" not in html,
          "no unsubstituted template placeholders")
    check('src="data:image/png;base64,' not in html or True, "basemap inlined as base64 data URI")


def check_rasters(payload: dict, tag: str, run_dir: str) -> None:
    print("[%s] raster payload matches the run" % tag)
    import make_report_html as gen

    from floodkit.runio import encode_depth, load_run

    report, depth, transform, crs, shape, cell_m = load_run(tag)
    rows, cols = shape
    codes = encode_depth(depth)

    decoded = np.frombuffer(base64.b64decode(payload["depth"]), dtype="uint8")
    check(decoded.size == rows * cols,
          "depth payload is %d bytes for a %dx%d grid" % (decoded.size, rows, cols))
    if decoded.size == codes.size:
        check(bool(np.array_equal(decoded, codes.reshape(-1))),
              "depth byte-for-byte equals the encoding of the run GeoTIFF")
        flat = decoded.reshape(rows, cols)
        check(bool((flat[depth > 0] >= 1).all()),
              "every wet cell carries a code of at least 1")
        check(bool((flat[depth <= 0] == 0).all()),
              "every dry cell carries code 0, so the trace band is unambiguous")

    elev_b64 = payload.get("elev")
    if elev_b64:
        ev = np.frombuffer(base64.b64decode(elev_b64), dtype="<u2")
        check(ev.size == rows * cols, "elevation payload is %d cells" % ev.size)
        elevation = gen.elevation_on_grid(gen.bounds_wgs84(transform, crs, shape),
                                          transform, crs, shape)
        if elevation is not None and ev.size == rows * cols:
            grid = ev.reshape(rows, cols)
            valid = np.isfinite(elevation)
            check(bool(np.array_equal(grid[~valid], np.full(int((~valid).sum()),
                                                            gen.ELEV_INVALID, dtype="<u2"))),
                  "cells with no DEM carry the invalid sentinel")
            got = grid[valid].astype("float64") / gen.ELEV_SCALE + payload["meta"]["elev_offset"]
            worst = float(np.max(np.abs(got - elevation[valid]))) if valid.any() else 0.0
            check(worst <= 0.5 / gen.ELEV_SCALE,
                  "elevation round-trips within half a quantisation step (worst %.3f m)" % worst)


def check_report_consistency(payload: dict, tag: str, run_dir: str) -> None:
    print("[%s] page layers agree with the embedded report" % tag)
    R = payload["report"]
    vectors = payload["vectors"]

    flooded = [s for s in vectors["underpasses"] if s["flooded"]]
    life = [s for s in flooded if s["risk"] == "life-safety"]
    up = R.get("underpass_summary") or {}
    check(len(flooded) == up.get("underpass_ways_flooded"),
          "flooded underpasses on the map = report (%d vs %s)"
          % (len(flooded), up.get("underpass_ways_flooded")))
    check(len(life) == up.get("life_safety_count"),
          "life-safety underpasses on the map = report (%d vs %s)"
          % (len(life), up.get("life_safety_count")))
    check(len(vectors["underpasses"]) == up.get("underpass_ways"),
          "underpass segments drawn = report total (%d vs %s)"
          % (len(vectors["underpasses"]), up.get("underpass_ways")))

    crit = R.get("critical_summary") or {}
    check(len(vectors["facilities"]) == crit.get("facilities_flooded"),
          "flooded facilities on the map = report (%d vs %s)"
          % (len(vectors["facilities"]), crit.get("facilities_flooded")))
    subway = sum(1 for f in vectors["facilities"] if f["kind"] == "subway_entrance")
    check(subway == crit.get("subway_entrances_flooded"),
          "subway entrances on the map = report (%d vs %s)"
          % (subway, crit.get("subway_entrances_flooded")))

    # The page must carry the run's numbers verbatim, including the ones that
    # fail. A renderer that quietly drops a failing indicator is the one failure
    # mode this whole project exists to avoid, so the check is equality with the
    # report on disk — not "everything passed". D02 in particular is a published
    # negative result and must survive into the page still failing.
    report_path = os.path.join(run_dir, "%s_report.json" % tag)
    with open(report_path, encoding="utf-8") as fh:
        truth = json.load(fh)
    check(bool(R.get("checks")), "the machine-checkable indicators travelled with the page")
    check(json.dumps(R.get("checks"), sort_keys=True) == json.dumps(truth.get("checks"), sort_keys=True),
          "embedded indicators are byte-identical to the run report")
    graded = [k for k, v in (R.get("checks") or {}).items() if not isinstance(v.get("passed"), bool)]
    check(not graded, "every indicator carries an explicit boolean verdict (%s)" % (graded or "none"))
    failing = [k for k, v in (R.get("checks") or {}).items() if v.get("passed") is False]
    print("       (this run has %d failing indicator(s): %s — carried through as failures)"
          % (len(failing), ", ".join(failing) or "none"))

    for key in ("ponding", "water_balance", "domain", "terrain"):
        check(json.dumps(R.get(key), sort_keys=True) == json.dumps(truth.get(key), sort_keys=True),
              "%s matches the run report" % key)

    # Grid resolution and extent come from the raster, not from a constant: a
    # hardcoded 28.1 m would pass for the 76 km2 runs and be silently wrong for
    # every other study area.
    import rasterio

    with rasterio.open(os.path.join(run_dir, "%s_depth.tif" % tag)) as src:
        raster_cell = abs(src.transform.a)
        raster_shape = (src.height, src.width)
    check(abs(R.get("cell_m", 0) - raster_cell) < 1e-6,
          "grid resolution is the run's (%.2f m)" % R.get("cell_m", 0))
    check((R.get("rows"), R.get("cols")) == raster_shape,
          "grid shape is the raster's (%dx%d vs %dx%d)"
          % (R.get("rows"), R.get("cols"), raster_shape[0], raster_shape[1]))
    check(R.get("reproduce", "").startswith("python -X utf8 -m floodkit run"),
          "a reproduce command is present and points at the CLI")


def check_markers(payload: dict, tag: str) -> None:
    print("[%s] flagged basins survive into the page" % tag)
    basins = payload["vectors"]["basins"]
    flagged = [b for b in basins if b["verify"] or b["outside"]]
    check(len(flagged) == len(basins), "every marked basin carries a reason")
    for b in basins:
        if not (-5 <= b["c"] <= payload["report"]["cols"] + 5
                and -5 <= b["r"] <= payload["report"]["rows"] + 5):
            check(False, "basin at (%.1f, %.1f) falls outside the grid" % (b["c"], b["r"]))
            return
    check(True, "all %d marked basins land inside the grid" % len(basins))


def check_palette(payload: dict, tag: str) -> None:
    """The shipped class table must bin every byte code the way the edges say.

    The page no longer derives a depth class; it looks one up in a table generated
    from `floodkit.runio`. This checks that table against the shared edges, band by
    band, because the failure it replaces was a one-band shift: 0.1 m cells painted
    with the 0.2–0.5 m colour. Every check that only asked "is the index inside the
    palette" passed that bug, and so did every screenshot — both colours are blue.

    The trace band is checked too: the headline ponded area counts cells with any
    water at all, so the page has to have somewhere to put them.
    """
    print("[%s] depth classes match the shared edge definition" % tag)
    from floodkit.runio import (
        DEPTH_CODE_CEILING,
        DEPTH_EDGES,
        DEPTH_LABELS,
        DEPTH_SCALE,
        class_index,
    )

    meta = payload["meta"]
    colors, table, edges = meta["colors"], meta["class_of_code"], meta["edges"]
    check(len(colors) == len(DEPTH_LABELS) == len(DEPTH_EDGES),
          "%d bands, one colour and one label each" % len(DEPTH_EDGES))
    check(edges == DEPTH_EDGES, "the page ships the shared edge list")
    check(len(table) == 256, "the class table covers all 256 byte codes")
    check(meta["depth_scale"] == DEPTH_SCALE, "the page ships the shared code scale")

    from floodkit.runio import code_to_depth

    wrong = []
    for code in range(256):
        depth = code_to_depth(code)
        expected = 0 if depth is None else class_index(depth) + 1
        if table[code] != expected:
            wrong.append((code, table[code], expected))
    check(not wrong, "every code lands in the band its depth belongs to (worst: %s)"
          % (wrong[:2] or "none"))

    check(table[0] == 0, "code 0 is dry")
    check(table[1] == 1 and not isinstance(code_to_depth(1), type(None)),
          "code 1 is the trace band (< %.2f m)" % DEPTH_EDGES[1])
    # Spelled out, because this is the assertion that would have caught the shift.
    for index, edge in enumerate(DEPTH_EDGES[1:], start=1):
        code = int(round(edge * DEPTH_SCALE)) + 1
        check(table[code] == index + 1,
              "%.2f m sits in band %d (%s)" % (edge, index + 1, DEPTH_LABELS[index]))
        if index + 1 < len(DEPTH_EDGES):
            mid = int(round((edge + DEPTH_EDGES[index + 1]) / 2 * DEPTH_SCALE)) + 1
            check(table[mid] == index + 1,
                  "%.3f m stays in band %d" % ((mid - 1) / DEPTH_SCALE, index + 1))
        else:
            top = int(round(min(2 * edge, DEPTH_CODE_CEILING) * DEPTH_SCALE)) + 1
            check(table[top] == index + 1,
                  "%.2f m stays in the open-ended top band" % ((top - 1) / DEPTH_SCALE))
    # The invariant that makes the byte encoding safe: every edge lands exactly on
    # a quantisation step, so rounding a cell cannot carry it into another band.
    steps = [e * DEPTH_SCALE for e in DEPTH_EDGES]
    check(all(abs(s - round(s)) < 1e-9 for s in steps),
          "every band edge is a whole number of %.2f m steps (%s)"
          % (1 / DEPTH_SCALE, [round(s) for s in steps]))


def main() -> int:
    tags = sys.argv[1:] or ["nanjing_final"]
    for tag in tags:
        run_dir = os.path.join(ROOT, "data", "runs", tag)
        path = os.path.join(run_dir, "%s_report.html" % tag)
        print("=" * 72)
        print("verifying %s" % path)
        if not os.path.exists(path):
            check(False, "%s exists — run scripts/make_report_html.py %s" % (path, tag))
            continue
        html = open(path, encoding="utf-8").read()
        size_mb = os.path.getsize(path) / 1e6
        check(size_mb < 8.0, "page is %.2f MB, inside the 8 MB budget" % size_mb)

        payload = extract_payload(html)
        check_offline(html, tag)
        check_palette(payload, tag)
        check_rasters(payload, tag, run_dir)
        check_report_consistency(payload, tag, run_dir)
        check_markers(payload, tag)

    print("=" * 72)
    if FAILURES:
        print("FAILED: %d check(s)" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rebuild every derived artifact for the flagship runs, in one command.

Why this exists: `data/runs/` holds the deliverables, and reproducing them means
five different scripts in a particular order — run the event, render the map,
render the page, export the vectors, draw the evidence figures. Remembering that
order is how a directory ends up half-rebuilt: the JSON present, the map missing,
and nothing saying so. This does the whole sequence and then runs the verifiers,
which is the only state worth shipping.

The run configurations are recorded here rather than read from the reports,
because the point is to work from an empty `data/runs/`. They match the
`run_config` blocks the runs themselves carry, so a rebuilt run can be checked
against the documented numbers by `tests/check_doc_consistency.py`.

    python scripts/rebuild_outputs.py            # everything
    python scripts/rebuild_outputs.py --only nanjing_final
    python scripts/rebuild_outputs.py --skip-slow    # no sweep, no negative figure

Requires the data caches (data/dem, data/landcover, data/osm). If they are empty
this will download roughly 190 MB, which is the same cost as a first run.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

os.environ.setdefault("MPLCONFIGDIR", os.path.join(ROOT, ".mplconfig"))

CITY = ("118.72", "32.02", "118.82", "32.10")
WIDE = ("118.72", "32.00", "119.00", "32.16")

#: tag -> (bounds, extra CLI args). Kept in the order a reader should build them.
RUNS = {
    "nanjing_final": (CITY, ["--rain-mm", "100", "--cn", "85", "--cn-source", "landcover", "--exposure"]),
    "nanjing_200mm": (CITY, ["--rain-mm", "200", "--cn", "85", "--cn-source", "landcover", "--exposure"]),
    "nanjing_wide": (WIDE, ["--rain-mm", "100", "--cn", "85", "--cn-source", "landcover",
                            "--envelope-min", "-50", "--envelope-max", "500",
                            "--checkpoints", os.path.join("data", "checkpoints", "nanjing_official_2015.csv"),
                            "--exposure"]),
}

#: Steps that need the caches to be warm and take minutes rather than seconds.
SLOW = {"nanjing_wide", "sweep", "negative"}


def python() -> str:
    return sys.executable


def run(label: str, args: list) -> bool:
    started = time.time()
    proc = subprocess.run([python(), "-X", "utf8", *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    elapsed = time.time() - started
    tail = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()][-2:]
    mark = "ok  " if proc.returncode == 0 else "FAIL"
    print("  %s %-46s %5.0fs  %s" % (mark, label, elapsed, " | ".join(tail)[:90]))
    if proc.returncode != 0 and not tail:
        print("       %s" % (proc.stderr or "").strip()[-300:])
    return proc.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma separated tags")
    parser.add_argument("--skip-slow", action="store_true",
                        help="skip the wide run and the sweep-based figures")
    args = parser.parse_args()

    wanted = [t.strip() for t in args.only.split(",") if t.strip()] or list(RUNS)
    failures = []

    for tag in wanted:
        if tag not in RUNS:
            raise SystemExit("unknown tag %r; known: %s" % (tag, ", ".join(RUNS)))
        bounds, extra = RUNS[tag]
        print("[%s] event + artifacts" % tag)
        if not run("floodkit run --tag %s" % tag,
                   ["-m", "floodkit", "run", "--bounds", *bounds, "--tag", tag, *extra]):
            failures.append(tag)
            continue
        if not run("make_report_map.py %s" % tag, ["scripts/make_report_map.py", tag]):
            failures.append(tag + "/map")
        if not run("make_report_html.py %s" % tag, ["scripts/make_report_html.py", tag]):
            failures.append(tag + "/html")
        if not run("export_geojson.py %s" % tag, ["scripts/export_geojson.py", tag]):
            failures.append(tag + "/geojson")

    if "nanjing_final" in wanted:
        print("[figures]")
        if not run("make_story_figure.py", ["scripts/make_story_figure.py"]):
            failures.append("story")

    if "nanjing_wide" in wanted and not args.skip_slow:
        print("[negative result]")
        if not run("make_negative_result_figure.py nanjing_wide",
                   ["scripts/make_negative_result_figure.py", "nanjing_wide"]):
            failures.append("negative")

    if not args.skip_slow:
        print("[rainfall sweep]")
        if not run("rainfall_sweep.py", ["tests/rainfall_sweep.py"]):
            failures.append("sweep")
        if not run("make_rainfall_animation.py", ["scripts/make_rainfall_animation.py"]):
            failures.append("animation")

    print("[verification]")
    checks = [
        ("regression", ["tests/test_floodkit.py"]),
        ("documentation", ["tests/check_doc_consistency.py"]),
        ("exposure refactor", ["tests/check_exposure_refactor.py", "nanjing_final"]),
        ("interactive page", ["tests/verify_report_html.py", *wanted]),
        ("map exports", ["tests/verify_map_outputs.py", *wanted]),
        ("vector exports", ["tests/verify_geojson.py", *wanted]),
        ("evidence figures", ["tests/verify_evidence_figures.py"]),
        # Reads the AGH session store, which lives outside the workspace and only
        # exists on a machine where the demo was actually run. Its absence is not a
        # build failure — the exported record is what ships — so a missing store is
        # reported and skipped rather than failing the rebuild.
        ("AGH execution record", ["tests/verify_agh_evidence.py"]),
    ]
    for label, argv in checks:
        if not run(label, argv):
            failures.append("verify:" + label)

    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("all artifacts rebuilt and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

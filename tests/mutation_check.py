"""Mutation check: do the tests actually detect broken behaviour?

A test suite that passes is not evidence that it tests anything. The failure mode
this project hit repeatedly was a test that kept passing after the code changed
shape underneath it — so instead of reading the tests and hoping, this breaks the
source on purpose and asserts the suite notices.

Every mutation below is a plausible typo or a previous real bug:

* dropping the initial abstraction term,
* forgetting to subtract 254 in the retention formula,
* returning the fill surface instead of solving for a stage,
* using the forward affine transform where the inverse belongs (this was a real
  shipped bug that silently put every OSM feature off the raster),
* removing the elevation envelope check.

A mutation the suite does NOT catch is a survivor, and a survivor is an untested
behaviour — that is the point of the exercise.

    python tests/mutation_check.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.path.join(ROOT, "_mutation")
DEPS = os.path.join(ROOT, ".pylibs")

# (file, literal, replacement, label). The literal must appear exactly once.
MUTATIONS = [
    (
        "hydro.py",
        "    s = 25400.0 / cn_safe - 254.0\n",
        "    s = 25400.0 / cn_safe\n",
        "CN: forget to subtract 254 in the retention formula",
    ),
    (
        "hydro.py",
        "Q = (P - Ia)^2 / (P - Ia + S)  for P > Ia, else 0.",
        "Q = P^2 / (P + S)  for all P.",
        "docstring only (control: must NOT be caught)",
    ),
    (
        "terrain.py",
        "    if target_volume >= volumes[-1]:\n        return float(elevations[-1]), float(volumes[-1])\n",
        "    if target_volume >= volumes[-1]:\n        return float(elevations[0]), float(volumes[-1])\n",
        "stage-volume: return the floor stage once the basin is full",
    ),
    (
        "terrain.py",
        "    if target_volume <= 0.0:\n        return float(elevations[0]), 0.0\n",
        "    if target_volume <= 0.0:\n        return float(elevations[-1]), 0.0\n",
        "stage-volume: return the spill stage for an empty basin",
    ),
    (
        "exposure.py",
        "    return [inverse * (x, y) for x, y in zip(xs, ys)]",
        "    return [transform * (x, y) for x, y in zip(xs, ys)]",
        "exposure: forward transform instead of the inverse (a real shipped bug)",
    ),
    (
        "quality.py",
        "    if hi > envelope_m[1] or lo < envelope_m[0]:",
        "    if False:",
        "quality: disable the elevation envelope",
    ),
    (
        "hydro.py",
        "    cn_safe = np.clip(cn_arr, 1.0, 100.0)\n",
        "    cn_safe = np.clip(cn_arr, 0.0, 1000.0)\n",
        "hydro: stop clamping the curve number to 1..100",
    ),
    # The three below reintroduce bugs that were found and fixed during
    # development. If a fix is not held in place by a test, any later edit can
    # undo it and nobody will notice.
    (
        "hydro.py",
        "        ground = dem.ravel()[pond_cells]\n",
        "        ground = filled.ravel()[pond_cells]\n",
        "hydro: build the stage-volume curve from the FILLED surface (bug #3)",
    ),
    (
        "hydro.py",
        "        pond_cells = cells[cap > 0.0]\n",
        "        pond_cells = cells[cap > min_depth_m]\n",
        "hydro: count a basin only if it is deeper than the reporting threshold",
    ),
    (
        "hydro.py",
        "        water = np.clip(stage - dem.ravel()[pond_cells], 0.0, None)\n"
        "        if not water.any():\n"
        "            continue\n"
        "        depth_out.ravel()[pond_cells] = np.maximum(depth_out.ravel()[pond_cells], water)\n",
        "        water = np.clip(stage - dem.ravel()[cells], 0.0, None)\n"
        "        if not water.any():\n"
        "            continue\n"
        "        depth_out.ravel()[cells] = np.maximum(depth_out.ravel()[cells], water)\n",
        "hydro: put water on the whole catchment instead of the fill footprint (bug #13)",
    ),
    (
        "exposure.py",
        "    dense = _densify(_project_many(raw, transform), DENSIFY_STEP_PX)\n",
        "    dense = _densify(_project_many(raw, transform), 14.0)\n",
        "exposure: densify by metres where pixels are expected (bug #12)",
    ),
    # Scope guards. These are the cheapest mistakes to make and the hardest to
    # notice: nothing crashes, the answer just quietly covers ground the project
    # never validated, or refuses a day it should have accepted.
    (
        "places.py",
        "    if east < ew or west > ee or north < es or south > en:\n",
        "    if False:\n",
        "places: drop the Nanjing-only bounds guard (any city would be accepted)",
    ),
    (
        "places.py",
        "        \"bounds\": [118.72, 32.02, 118.82, 32.10],\n",
        "        \"bounds\": [118.62, 32.02, 118.82, 32.10],\n",
        "places: shift the validated Nanjing box (numbers stop matching the reports)",
    ),
    (
        "weather.py",
        "SCOPE_DAYS_AHEAD = 15\n",
        "SCOPE_DAYS_AHEAD = 90\n",
        "weather: widen the project window from 15 days to 90",
    ),
    (
        "weather.py",
        "    if moment > latest:\n",
        "    if moment >= latest:\n",
        "weather: make the 15-day boundary exclusive (refuses the last legal day)",
    ),
    (
        "weather.py",
        "    refusal = check_time_scope(start, now_local)\n    if refusal is not None:\n        return refusal\n",
        "    refusal = check_time_scope(start, now_local)\n    if False:\n        return refusal\n",
        "weather: keep check_time_scope but stop the entry point enforcing it",
    ),
]


def run_suite(label: str) -> tuple[bool, str]:
    """Return (suite_failed, tail_of_output)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([DEPS, SCRATCH, ROOT])
    env["MPLCONFIGDIR"] = os.path.join(ROOT, ".mplconfig")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.join(SCRATCH, "tests", "test_floodkit.py")],
        cwd=SCRATCH,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-3:])
    if proc.returncode != 0 and not tail:
        tail = (proc.stderr or "").strip()[-300:]
    return proc.returncode != 0, tail


def reset_scratch() -> None:
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(os.path.join(SCRATCH, "data", "tmp"), exist_ok=True)
    shutil.copytree(os.path.join(ROOT, "floodkit"), os.path.join(SCRATCH, "floodkit"))
    shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(SCRATCH, "tests"))
    for sub in ("fixtures", "checkpoints"):
        src = os.path.join(ROOT, "data", sub)
        if os.path.exists(src):
            shutil.copytree(src, os.path.join(SCRATCH, "data", sub))
    # The encoding test asserts on the launcher scripts, so the scratch copy needs
    # them too. Without this the baseline fails on a missing file, which looks
    # like a broken suite rather than a missing fixture.
    scripts = os.path.join(ROOT, "scripts")
    if os.path.exists(scripts):
        shutil.copytree(scripts, os.path.join(SCRATCH, "scripts"))
        # __pycache__ inside scripts is noise; keep the scratch tree clean.
        shutil.rmtree(os.path.join(SCRATCH, "scripts", "__pycache__"), ignore_errors=True)
    for name in ("启动AGH.cmd", "安装插件.cmd"):
        src = os.path.join(ROOT, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(SCRATCH, name))


def main() -> int:
    reset_scratch()
    baseline_failed, baseline_tail = run_suite("baseline")
    if baseline_failed:
        print("BASELINE FAILS — fix the suite before mutating anything")
        print(baseline_tail)
        return 2
    print("baseline: suite passes (a mutation must break it)")
    print()

    survivors = []
    not_found = []
    controls_wrong = []
    print("%-6s %s" % ("result", "mutation"))
    for filename, literal, replacement, label in MUTATIONS:
        reset_scratch()
        path = os.path.join(SCRATCH, "floodkit", filename)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        occurrences = text.count(literal)
        if occurrences != 1:
            not_found.append((label, occurrences))
            print("%-6s %s  [pattern found %d times]" % ("SKIP", label, occurrences))
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace(literal, replacement))
        failed, tail = run_suite(label)
        is_control = "control" in label
        if is_control:
            # a docstring edit must NOT be caught; if it is, the suite is flaky
            if failed:
                controls_wrong.append(label)
                print("%-6s %s" % ("BADCTL", label))
            else:
                print("%-6s %s" % ("ok", label))
            continue
        if failed:
            print("%-6s %s" % ("caught", label))
        else:
            survivors.append(label)
            print("%-6s %s   <-- SURVIVOR" % ("MISSED", label))

    shutil.rmtree(SCRATCH, ignore_errors=True)
    print()
    print("caught   : %d" % (len(MUTATIONS) - len(survivors) - len(not_found) - 1 - len(controls_wrong)))
    print("survivors: %d" % len(survivors))
    for label in survivors:
        print("   - %s" % label)
    if not_found:
        print("patterns not found (mutation harness needs updating, not the code):")
        for label, count in not_found:
            print("   - %s (%d matches)" % (label, count))
    if controls_wrong:
        print("control mutations were caught, which means the suite is flaky: %s" % controls_wrong)
    _ = re
    return 1 if (survivors or not_found or controls_wrong) else 0


if __name__ == "__main__":
    raise SystemExit(main())

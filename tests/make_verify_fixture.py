"""Export cross-language verification fixtures.

MATLAB is unavailable on the target machine (expired licence), so the second
independent implementation lives in JavaScript instead — see
`agh-plugin/verify-core.mjs`. Cross-language beats cross-tool here: the checker
runs inside the agent's own tool chain, so it re-verifies every result the agent
produces rather than being a one-off script someone ran once.

The fixture deliberately exports *inputs and Python's answers*, never the method:
the JS side rebuilds the stage-volume curve and the SCS-CN terms from scratch.

The basin section is a matrix rather than a single case. One case proves the two
implementations agree at one point in the input space; the interesting failures
live at the edges — an empty basin, a basin filled exactly to its spill level, a
target beyond capacity, and flat floors where many cells share an elevation.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

from floodkit.hydro import run_ponding, scs_cn_retention, scs_cn_runoff  # noqa: E402
from floodkit.terrain import (  # noqa: E402
    depression_depth,
    fill_depressions,
    flow_sinks,
    stage_from_volume,
)

OUT = os.path.join(ROOT, "data", "fixtures", "verify_case.json")


def basin_case(name: str, ground: np.ndarray, spill: float, cell_area: float, target: float) -> dict:
    """Build one fixture entry and Python's answer for it."""
    ground_sorted = np.sort(np.asarray(ground, dtype="float64"))
    k = np.arange(1, ground_sorted.size + 1, dtype="float64")
    volumes = cell_area * (k * ground_sorted - np.cumsum(ground_sorted))
    capacity_m3 = cell_area * float(np.sum(np.maximum(spill - ground_sorted, 0.0)))
    e_curve = np.concatenate([ground_sorted, [spill]])
    v_curve = np.concatenate([volumes, [capacity_m3]])
    stage, _ = stage_from_volume(e_curve, v_curve, target)
    stored = cell_area * float(np.sum(np.maximum(stage - ground_sorted, 0.0)))
    return {
        "name": name,
        "ground_elevations_m": [float(v) for v in ground_sorted],
        "spill_elevation_m": float(spill),
        "cell_area_m2": float(cell_area),
        "target_volume_m3": float(target),
        "capacity_m3": capacity_m3,
        "python_stage_m": float(stage),
        "python_stored_m3": float(stored),
        "clamped_to_capacity": bool(target > capacity_m3),
    }


def build_basins() -> list[dict]:
    n = 60
    cell_area = 900.0
    yy, xx = np.mgrid[0:n, 0:n]

    # A: the original bowl, driven by a real run_ponding result
    bowl = 30.0 - np.exp(-(((yy - 30.0) ** 2 + (xx - 30.0) ** 2) / (2 * 8.0 ** 2))) * 10.0
    filled = fill_depressions(bowl)
    capacity = depression_depth(bowl, filled)
    sinks = flow_sinks(bowl)
    result = run_ponding(bowl, filled, capacity, sinks, np.full(bowl.shape, 40.0), cell_area)
    biggest = max(result.basins, key=lambda b: b.stored_m3)
    footprint = capacity.ravel() > 0.05
    ground_bowl = bowl.ravel()[footprint]
    spill_bowl = float(filled.ravel()[footprint].max())
    capacity_bowl = cell_area * float(np.sum(np.maximum(spill_bowl - np.sort(ground_bowl), 0.0)))

    # B: a V-shaped valley cross-section (long, thin, few distinct elevations)
    valley = 20.0 + 0.02 * np.abs(np.arange(n) - n / 2.0)
    valley = np.repeat(valley[None, :], n, axis=0) + 0.0 * yy
    ground_valley = (valley - valley.min()).ravel() + 1.0

    # C: a flat-bottomed pit — many equal elevations, which is where tie handling
    #    in the interpolation either works or quietly picks a different branch
    flat = np.full((n, n), 5.0)
    flat[28:33, 28:33] = 3.0
    filled_flat = fill_depressions(flat)
    ground_flat = np.maximum(filled_flat - flat, 0.0).ravel()

    cases = [
        basin_case("bowl_mid", ground_bowl, spill_bowl, cell_area, float(biggest.inflow_m3)),
        basin_case("bowl_empty", ground_bowl, spill_bowl, cell_area, 0.0),
        basin_case("bowl_exactly_full", ground_bowl, spill_bowl, cell_area, capacity_bowl),
        basin_case("bowl_over_capacity", ground_bowl, spill_bowl, cell_area, capacity_bowl * 1.5),
        basin_case("bowl_tiny", ground_bowl, spill_bowl, cell_area, 1.0),
        basin_case(
            "valley_mid",
            ground_valley,
            float(ground_valley.max()) + 0.5,
            cell_area,
            cell_area * float(np.sum(np.maximum(ground_valley.max() + 0.25 - ground_valley, 0.0))),
        ),
        basin_case(
            "flat_floor_half",
            ground_flat,
            float(ground_flat.max()) * 0.5 + 1.0,
            cell_area,
            cell_area * float(np.sum(np.maximum(ground_flat.max() * 0.5 + 1.0 - ground_flat, 0.0))) * 0.5,
        ),
        basin_case("single_cell", np.array([12.5]), 14.0, cell_area, cell_area * 0.75),
        basin_case("small_cell_area", ground_bowl, spill_bowl, 1.0, capacity_bowl / 900.0 / 2.0),
        {
            "name": "degenerate_zero_capacity",
            "ground_elevations_m": [7.0, 7.0, 7.0],
            "spill_elevation_m": 7.0,
            "cell_area_m2": cell_area,
            "target_volume_m3": 0.0,
            "capacity_m3": 0.0,
            "python_stage_m": 7.0,
            "python_stored_m3": 0.0,
            "clamped_to_capacity": False,
        },
    ]
    return cases


def main() -> int:
    # ---- 1. scalar SCS-CN samples -------------------------------------
    scs_samples = []
    for p in (0.0, 5.0, 12.5, 40.0, 100.0, 250.0):
        for cn in (60.0, 75.0, 85.0, 95.0):
            scs_samples.append(
                {
                    "p_mm": p,
                    "cn": cn,
                    "q_mm": float(scs_cn_runoff(p, cn)),
                    "f_mm": float(scs_cn_retention(p, cn)),
                }
            )

    basins = build_basins()

    # the round-trip curve check keeps a raw curve in the fixture
    n = 60
    yy, xx = np.mgrid[0:n, 0:n]
    dem = 30.0 - np.exp(-(((yy - 30.0) ** 2 + (xx - 30.0) ** 2) / (2 * 8.0 ** 2))) * 10.0
    filled = fill_depressions(dem)
    capacity = depression_depth(dem, filled)
    footprint = capacity.ravel() > 0.05
    ground = np.sort(dem.ravel()[footprint])
    spill = float(filled.ravel()[footprint].max())
    cell_area = 900.0
    k = np.arange(1, ground.size + 1, dtype="float64")
    volumes = cell_area * (k * ground - np.cumsum(ground))
    capacity_m3 = cell_area * float(np.sum(np.maximum(spill - ground, 0.0)))

    payload = {
        "generated_by": "python floodkit",
        "note": "JavaScript re-derives every number below from the raw inputs.",
        "scs_samples": scs_samples,
        "stage_volume": {
            "elevations_m": [float(v) for v in np.concatenate([ground, [spill]])],
            "volumes_m3": [float(v) for v in np.concatenate([volumes, [capacity_m3]])],
            "cell_area_m2": cell_area,
        },
        "basins": basins,
        # kept so an older reader still finds a single basin to check
        "basin": basins[0],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print("wrote %s" % OUT)
    print("scs samples: %d | basin cases: %d" % (len(scs_samples), len(basins)))
    for case in basins:
        print("   %-24s cells=%-5d capacity=%-12.1f target=%-12.1f stage=%.6f"
              % (case["name"], len(case["ground_elevations_m"]), case["capacity_m3"],
                 case["target_volume_m3"], case["python_stage_m"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Rainfall sweep: exercise the solver across intensities, not just one scenario.

A single 100 mm scenario leaves the spill branch almost untouched — in the city
study area nothing overflows, so the code that handles a basin filling past its
spill level never runs. Untested branches are where wrong answers hide, so this
sweeps the rainfall depth and reports how the indicators behave, including how
much water actually spills once basins start filling.

    python tests/rainfall_sweep.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

from floodkit.pipeline import DEFAULT_ENVELOPE, load_dem_metric, run_event  # noqa: E402

CITY_BOUNDS = (118.72, 32.02, 118.82, 32.10)
RAINFALL_MM = (50.0, 100.0, 150.0, 200.0, 300.0)


def main() -> int:
    from floodkit.dem_source import ensure_tiles

    tiles = ensure_tiles(CITY_BOUNDS, os.path.join(ROOT, "data", "dem"), progress=lambda *_: None)
    grid = None
    for tile in tiles:
        try:
            grid = load_dem_metric(tile, CITY_BOUNDS)
            break
        except Exception:
            continue
    if grid is None:
        print("could not load a DEM tile")
        return 2

    print("%-8s %-9s %-10s %-11s %-11s %-10s %-9s %s" % (
        "rain", "outcome", "D01", "D01b", "ponded_km2", "spilled", "overflow", "max_depth"))
    rows = []
    for rain in RAINFALL_MM:
        result = run_event(grid, rain_mm=rain, cn=85.0, envelope_m=DEFAULT_ENVELOPE)
        wb = result.get("water_balance") or {}
        pond = result.get("ponding") or {}
        checks = result.get("checks") or {}
        row = {
            "rain_mm": rain,
            "outcome": result.get("outcome"),
            "d01": wb.get("relative_error"),
            "d01b": wb.get("raster_vs_solver_relative_error"),
            "ponded_km2": (pond.get("pond_area_m2") or 0.0) / 1e6,
            "spilled_m3": pond.get("total_spilled_m3"),
            "overflowing_basins": pond.get("overflowing_basins"),
            "basin_gap_m3": pond.get("basin_accounting_gap_m3"),
            "max_depth_m": pond.get("max_depth_m"),
            "d04_change": (checks.get("D04_sensitivity") or {}).get("max_relative_area_change"),
        }
        rows.append(row)
        print("%-8.0f %-9s %-10.2e %-11.2e %-11.3f %-10.0f %-9s %.2f" % (
            row["rain_mm"], row["outcome"], row["d01"] or 0.0, row["d01b"] or 0.0,
            row["ponded_km2"], row["spilled_m3"] or 0.0, row["overflowing_basins"], row["max_depth_m"] or 0.0))

    monotone = all(rows[i]["ponded_km2"] <= rows[i + 1]["ponded_km2"] + 1e-9 for i in range(len(rows) - 1))
    all_balanced = all((r["outcome"] in ("ok", "degraded")) for r in rows)
    all_exact = all((r["d01"] or 0.0) <= 0.01 for r in rows)
    any_spill = any((r["spilled_m3"] or 0.0) > 0 for r in rows)

    summary = {
        "bounds": list(CITY_BOUNDS),
        "scenarios": rows,
        "checks": {
            "every_scenario_completed": all_balanced,
            "water_balance_holds_at_every_intensity": all_exact,
            "ponded_area_monotone_in_rainfall": monotone,
            "spill_branch_exercised": any_spill,
        },
        "note": (
            "Monotonicity in rainfall is the structural check: more rain can never pond less "
            "water. Whether any basin spills is reported rather than asserted, because a sweep "
            "that never overflows leaves that branch untested."
        ),
    }
    out_dir = os.path.join(ROOT, "data", "evidence")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "rainfall_sweep.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)
    print()
    for key, value in summary["checks"].items():
        print("%-44s %s" % (key, "PASS" if value else "FAIL"))
    print("wrote", path)
    return 0 if all(summary["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

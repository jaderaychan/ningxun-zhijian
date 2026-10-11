"""Hydrological core: SCS-CN runoff generation and depression-storage ponding.

Model statement (honest scope)
------------------------------
This is a *simplified hydrological-terrain* model, not a 2-D hydrodynamic
solver. It answers: "for this rainfall event, which depressions pond water,
how deep, and how much of the rainfall is accounted for". It does NOT resolve
flow velocity, drainage-network capacity, river backwater or time of
concentration. Those limits are reported with the results, not hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .terrain import stage_from_volume

# SCS curve numbers for the standard land-cover classes used by this project.
# Source: USDA TR-55 (urban hydrology) values for hydrologic soil group C,
# rescaled by the caller when the soil group differs.
CN_TABLE_HSG_C = {
    "impervious": 98.0,
    "commercial": 94.0,
    "industrial": 93.0,
    "residential_high": 90.0,
    "residential_medium": 85.0,
    "residential_low": 80.0,
    "open_space": 74.0,
    "paved_road": 98.0,
    "bare_soil": 88.0,
    "grass": 74.0,
    "forest": 70.0,
    "cropland": 78.0,
    "water": 100.0,
}

# hydrologic soil group adjustment (TR-55 approximate offset in CN points,
# applied as a shift and clipped to the physical range)
HSG_SHIFT = {"A": -8.0, "B": -4.0, "C": 0.0, "D": +4.0}


def potential_retention(cn: np.ndarray | float) -> np.ndarray | float:
    """S in millimetres for a curve number (TR-55)."""
    cn_arr = np.asarray(cn, dtype="float64")
    cn_safe = np.clip(cn_arr, 1.0, 100.0)
    s = 25400.0 / cn_safe - 254.0
    return float(s) if np.isscalar(cn) or cn_arr.ndim == 0 else s


def initial_abstraction(cn: np.ndarray | float, ia_ratio: float = 0.2):
    """Ia in millimetres."""
    return ia_ratio * potential_retention(cn)


def scs_cn_runoff(rain_mm: np.ndarray | float, cn: np.ndarray | float, ia_ratio: float = 0.2):
    """SCS-CN direct runoff depth in millimetres.

    Q = (P - Ia)^2 / (P - Ia + S)  for P > Ia, else 0.
    Vectorised; P and CN broadcast together.
    """
    p = np.asarray(rain_mm, dtype="float64")
    s = np.asarray(potential_retention(cn), dtype="float64")
    ia = ia_ratio * s
    effective = p - ia
    q = np.zeros_like(effective, dtype="float64")
    positive = effective > 0.0
    denom = effective + s
    with np.errstate(divide="ignore", invalid="ignore"):
        q[positive] = (effective[positive] ** 2) / denom[positive]
    # never let runoff exceed the rainfall that generated it
    q = np.minimum(q, np.maximum(p, 0.0))
    return float(q) if q.ndim == 0 else q


def scs_cn_retention(rain_mm: np.ndarray | float, cn: np.ndarray | float, ia_ratio: float = 0.2):
    """Continuing retention F in millimetres.

    F = S * (P - Ia) / (P - Ia + S).  Together with the runoff this closes the
    SCS water balance  P = Ia + F + Q, which is what makes the whole-domain mass
    balance in `water_balance` a real check rather than a restatement: F is
    evaluated from the retention relation, never as P - Ia - Q.
    """
    p = np.asarray(rain_mm, dtype="float64")
    s = np.asarray(potential_retention(cn), dtype="float64")
    ia = ia_ratio * s
    effective = np.maximum(p - ia, 0.0)
    denom = effective + s
    f = np.zeros_like(effective, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(denom > 0, s * effective / denom, 0.0)
    return float(f) if f.ndim == 0 else f


@dataclass
class BasinResult:
    basin_id: int
    sink_cell: int
    row: int
    col: int
    catchment_cells: int
    pond_cells: int
    catchment_area_m2: float
    pond_area_m2: float
    inflow_m3: float
    stored_m3: float
    spilled_m3: float
    stage_m: float
    max_depth_m: float
    bottom_elev_m: float
    spill_elev_m: float
    x_m: float | None = None
    y_m: float | None = None


@dataclass
class PondingResult:
    depth_m: np.ndarray
    basins: list[BasinResult] = field(default_factory=list)
    total_rain_m3: float = 0.0
    total_runoff_m3: float = 0.0
    total_stored_m3: float = 0.0
    total_spilled_m3: float = 0.0
    routed_inflow_m3: float = 0.0
    outflow_m3: float = 0.0
    pond_cell_count: int = 0
    pond_area_m2: float = 0.0
    max_depth_m: float = 0.0
    skipped_basins: int = 0
    skipped_small: int = 0
    basin_accounting_gap_m3: float = 0.0
    overflowing_basins: int = 0
    unrouted_runoff_m3: float = 0.0

    def as_dict(self) -> dict:
        return {
            "total_rain_m3": self.total_rain_m3,
            "total_runoff_m3": self.total_runoff_m3,
            "routed_inflow_m3": self.routed_inflow_m3,
            "total_stored_m3": self.total_stored_m3,
            "total_spilled_m3": self.total_spilled_m3,
            "outflow_m3": self.outflow_m3,
            "routed_fraction": (self.routed_inflow_m3 / self.total_runoff_m3) if self.total_runoff_m3 > 0 else 0.0,
            "pond_area_m2": self.pond_area_m2,
            "pond_cells": int(self.pond_cell_count),
            "max_depth_m": self.max_depth_m,
            "basin_count": len(self.basins),
            "skipped_basins": self.skipped_basins,
            "skipped_small": self.skipped_small,
            "basin_accounting_gap_m3": self.basin_accounting_gap_m3,
            "overflowing_basins": self.overflowing_basins,
            "unrouted_runoff_m3": self.unrouted_runoff_m3,
        }


def run_ponding(
    dem: np.ndarray,
    filled: np.ndarray,
    depth_capacity: np.ndarray,
    sinks: np.ndarray,
    runoff_mm: np.ndarray,
    cell_area_m2: float,
    min_depth_m: float = 0.05,
    min_pond_cells: int = 4,
    transform=None,
    max_basins: int = 50000,
    exclude_mask: np.ndarray | None = None,
) -> PondingResult:
    """Route generated runoff into depression basins and solve water levels.

    Every cell drains (via D8 on the original surface) to a terminal local
    minimum. All cells sharing a terminal form one ponding unit: the water that
    unit receives is the runoff generated on its catchment, and the water level
    is solved from the unit's own stage-volume curve.
    """
    dem = np.asarray(dem, dtype="float64")
    filled = np.asarray(filled, dtype="float64")
    depth_capacity = np.asarray(depth_capacity, dtype="float64")
    runoff_mm = np.asarray(runoff_mm, dtype="float64")
    rows, cols = dem.shape

    depth_out = np.zeros_like(dem)
    result = PondingResult(depth_m=depth_out)

    flat_runoff = runoff_mm.ravel()
    flat_sinks = sinks.ravel()
    if exclude_mask is not None:
        # Permanent water is not a place that can "flood": keeping it in the
        # basin footprint would let a lake fill up and dominate every depth
        # statistic, which is exactly the error this parameter exists to stop.
        flat_sinks = np.where(np.asarray(exclude_mask).ravel(), -1, flat_sinks)
    order = np.argsort(flat_sinks, kind="stable")
    sorted_sinks = flat_sinks[order]
    boundaries = np.nonzero(np.diff(sorted_sinks))[0] + 1
    groups = np.split(order, boundaries)

    populated = [(g[0], g) for g in groups if g.size > 0]
    populated.sort(key=lambda item: -item[1].size)
    if len(populated) > max_basins:
        dropped = populated[max_basins:]
        result.skipped_basins = len(dropped)
        result.unrouted_runoff_m3 += float(
            sum(flat_runoff[cells].sum() for _, cells in dropped)
        ) / 1000.0 * cell_area_m2
        populated = populated[:max_basins]
    inv_area = 1.0 / cell_area_m2 if cell_area_m2 > 0 else 0.0

    for basin_id, (terminal, cells) in enumerate(populated, start=1):
        if terminal < 0:
            continue  # excluded permanent-water cells form their own bucket
        cap = depth_capacity.ravel()[cells]
        # `min_depth_m` decides which basins are worth *reporting*; the volume
        # curve must describe the whole depression (cap > 0). Building the curve
        # from only the deep cells while spreading water over the full footprint
        # stored more water than was routed in — exactly the residual that the
        # D01 balance reported as 2.2% on the larger study area.
        pond_cells = cells[cap > 0.0]
        if pond_cells.size < min_pond_cells:
            result.skipped_small += 1
            result.unrouted_runoff_m3 += float(flat_runoff[cells].sum()) / 1000.0 * cell_area_m2
            continue
        # the depression floor has zero storage capacity but is where water is
        # deepest, so it must be part of the stage-volume footprint
        floor = int(terminal)
        if not np.isin(floor, pond_cells):
            pond_cells = np.append(pond_cells, floor)

        inflow_m3 = float(flat_runoff[cells].sum()) / 1000.0 * cell_area_m2
        if inflow_m3 <= 0.0:
            continue

        # The stage-volume curve must use the ORIGINAL ground surface: on the
        # filled surface a depression is nearly flat, which would collapse the
        # curve to zero storage and push every stage straight to the spill level.
        ground = dem.ravel()[pond_cells]
        spill_elev = float(filled.ravel()[pond_cells].max())
        order_e = np.argsort(ground, kind="stable")
        e_sorted = ground[order_e]
        n = e_sorted.size
        k = np.arange(1, n + 1, dtype="float64")
        volumes = cell_area_m2 * (k * e_sorted - np.cumsum(e_sorted))
        capacity_m3 = cell_area_m2 * float(np.sum(np.maximum(spill_elev - e_sorted, 0.0)))
        if capacity_m3 < volumes[-1]:
            capacity_m3 = float(volumes[-1])
        e_curve = np.concatenate([e_sorted, [spill_elev]])
        v_curve = np.concatenate([volumes, [capacity_m3]])

        target = min(inflow_m3, capacity_m3)
        spilled = inflow_m3 - target
        stage, _stored_curve = stage_from_volume(e_curve, v_curve, target)

        # Depth applies only to the cells the fill surface puts inside this
        # depression. A cell whose D8 path happens to end at the floor but whose
        # fill level equals its own ground can drain away by another route; the
        # raster used to give it water anyway, which stored more than the routed
        # volume and produced the entire D01 residual.
        water = np.clip(stage - dem.ravel()[pond_cells], 0.0, None)
        if not water.any():
            continue
        depth_out.ravel()[pond_cells] = np.maximum(depth_out.ravel()[pond_cells], water)

        wet = water > 0.0
        stored_raster = float(water.sum()) * cell_area_m2
        r0, c0 = divmod(int(terminal), cols)
        x_m = y_m = None
        if transform is not None:
            x_m, y_m = transform * (c0 + 0.5, r0 + 0.5)

        result.basins.append(
            BasinResult(
                basin_id=basin_id,
                sink_cell=int(terminal),
                row=int(r0),
                col=int(c0),
                catchment_cells=int(cells.size),
                pond_cells=int(wet.sum()),
                catchment_area_m2=float(cells.size) * cell_area_m2,
                pond_area_m2=float(wet.sum()) * cell_area_m2,
                inflow_m3=inflow_m3,
                stored_m3=stored_raster,
                spilled_m3=spilled,
                stage_m=float(stage),
                max_depth_m=float(water.max()) if water.size else 0.0,
                bottom_elev_m=float(e_sorted[0]),
                spill_elev_m=spill_elev,
                x_m=float(x_m) if x_m is not None else None,
                y_m=float(y_m) if y_m is not None else None,
            )
        )
        _ = inv_area  # kept explicit: areas are computed from cell counts

    result.total_runoff_m3 = float(runoff_mm.sum()) * cell_area_m2 / 1000.0
    result.total_stored_m3 = float(depth_out.sum()) * cell_area_m2
    result.total_spilled_m3 = float(sum(b.spilled_m3 for b in result.basins))
    result.routed_inflow_m3 = float(sum(b.inflow_m3 for b in result.basins))
    # Per-basin accounting check: if stored + spilled does not equal the inflow a
    # basin received, the stage-volume footprint and the water actually placed on
    # the raster are describing different areas. Reported here so the global
    # balance can be attributed instead of just observed.
    result.basin_accounting_gap_m3 = float(
        sum(b.stored_m3 + b.spilled_m3 - b.inflow_m3 for b in result.basins)
    )
    result.overflowing_basins = int(sum(1 for b in result.basins if b.spilled_m3 > 0))
    # Measured independently by summing the runoff of every catchment whose
    # basin was not reported, instead of being defined as "runoff minus routed".
    # Defined as a residual, the term made D01 an identity that could never fail;
    # measured, D01 becomes a genuine partition check that catches runoff which
    # is double-counted across basins or silently dropped.
    result.outflow_m3 = result.unrouted_runoff_m3
    result.pond_cell_count = int((depth_out > 0).sum())
    result.pond_area_m2 = float(result.pond_cell_count) * cell_area_m2
    result.max_depth_m = float(depth_out.max()) if depth_out.size else 0.0
    return result


def water_balance(
    rain_mm: np.ndarray,
    cn: np.ndarray,
    runoff_mm: np.ndarray,
    stored_m3: float,
    spilled_m3: float,
    cell_area_m2: float,
    outflow_m3: float = 0.0,
    stored_solver_m3: float | None = None,
    ia_ratio: float = 0.2,
    tolerance: float = 0.01,
) -> dict:
    """Independent whole-domain mass balance.

        rainfall = initial abstraction + continuing retention + stored
                   + spilled + outflow + residual

    `residual` collects every place the accounting could leak: a unit error, a
    wrong cell area, a routing bug, runoff that vanished. It is reported, never
    silently absorbed. `F` comes from the SCS retention relation and the stored
    volume from the *produced raster*, so the chain rain -> abstraction ->
    retention -> runoff -> routed storage -> artifact is verified end to end.
    A second check compares that raster storage against the solver's own basin
    totals, which catches aggregation errors the global balance alone would hide.
    """
    rain_mm = np.asarray(rain_mm, dtype="float64")
    cn = np.asarray(cn, dtype="float64")
    runoff_mm = np.asarray(runoff_mm, dtype="float64")

    rain_vol = float(rain_mm.sum()) * cell_area_m2 / 1000.0
    ia_mm = np.asarray(initial_abstraction(cn, ia_ratio), dtype="float64")
    ia_vol = float(np.minimum(ia_mm, np.maximum(rain_mm, 0.0)).sum()) * cell_area_m2 / 1000.0
    f_vol = float(np.asarray(scs_cn_retention(rain_mm, cn, ia_ratio)).sum()) * cell_area_m2 / 1000.0
    runoff_vol = float(runoff_mm.sum()) * cell_area_m2 / 1000.0
    scs_gap = rain_vol - ia_vol - f_vol - runoff_vol

    accounted = ia_vol + f_vol + float(stored_m3) + float(spilled_m3) + float(outflow_m3)
    residual = rain_vol - accounted
    denom = rain_vol if rain_vol > 0 else 1.0
    rel_error = abs(residual) / denom

    routing_rel = None
    if stored_solver_m3 is not None and float(stored_m3) > 0:
        routing_rel = abs(float(stored_m3) - float(stored_solver_m3)) / float(stored_m3)

    return {
        "rain_volume_m3": rain_vol,
        "initial_abstraction_m3": ia_vol,
        "continuing_retention_m3": f_vol,
        "generated_runoff_m3": runoff_vol,
        "scs_algebra_gap_m3": scs_gap,
        "stored_m3": float(stored_m3),
        "spilled_m3": float(spilled_m3),
        "outflow_m3": float(outflow_m3),
        "residual_m3": residual,
        "relative_error": rel_error,
        "raster_vs_solver_relative_error": routing_rel,
        "routed_fraction": (float(stored_m3) + float(spilled_m3)) / runoff_vol if runoff_vol > 0 else 0.0,
        "tolerance": tolerance,
        "passed": bool(rel_error <= tolerance),
    }

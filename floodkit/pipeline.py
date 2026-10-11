"""End-to-end event pipeline: DEM in, verified inundation evidence out."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np

from . import quality
from .hydro import run_ponding, scs_cn_runoff, water_balance
from .terrain import (
    depression_depth,
    detect_flat_water,
    fill_depressions,
    flow_sinks,
    slope_degrees,
)

METRIC_CRS = "EPSG:32650"  # UTM zone 50N covers Jiangsu

# Plausibility envelope for elevation in this study area (Jiangsu is a lowland
# plain). This is a study-area parameter, not a universal constant: it catches
# the feet/metre class of error only when the mistake pushes values outside it.
# Declared once here so the CLI and the library cannot drift apart — they did,
# and a feet-valued DEM sailed through with a 800 m ceiling.
DEFAULT_ENVELOPE = (-50.0, 400.0)

# Result keys that must never be serialised into the JSON report.
EXCLUDED_FROM_REPORT = ("depth_m",)


@dataclass
class DemGrid:
    elevation: np.ndarray
    transform: object
    crs: object
    nodata: float | None
    cell_area_m2: float
    res_x: float
    res_y: float
    source: str

    @property
    def shape(self):
        return self.elevation.shape

    @property
    def valid(self) -> np.ndarray:
        arr = self.elevation
        mask = np.isfinite(arr)
        if self.nodata is not None:
            mask &= arr != self.nodata
        return mask


def load_dem_metric(
    path: str,
    bounds_wgs84: tuple[float, float, float, float],
    target_crs: str = METRIC_CRS,
) -> DemGrid:
    """Clip a geographic DEM to `bounds_wgs84` and reproject to a metric CRS.

    Reprojection is not cosmetic: in EPSG:4326 a "30 m" cell is ~30 m only in
    one direction, and its area shrinks with latitude. Every volume in this
    project depends on cell area, so the model works on a metric grid.
    """
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject
    from rasterio.transform import array_bounds
    from rasterio.windows import from_bounds

    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError("source DEM has no CRS: %s" % path)
        window = from_bounds(*bounds_wgs84, transform=src.transform)
        window = window.round_offsets().round_lengths()
        if window.width < 2 or window.height < 2:
            raise ValueError("requested bounds fall outside the DEM extent: %s" % path)
        data = src.read(1, window=window).astype("float64")
        src_transform = src.window_transform(window)
        src_crs = src.crs
        src_nodata = src.nodata

    invalid = ~np.isfinite(data)
    if src_nodata is not None:
        invalid |= data == src_nodata
    sentinel = -9999.0
    data[invalid] = sentinel

    src_bounds = array_bounds(data.shape[0], data.shape[1], src_transform)
    dst_transform, width, height = calculate_default_transform(
        src_crs, target_crs, data.shape[1], data.shape[0], *src_bounds
    )
    dst = np.full((height, width), np.nan, dtype="float64")
    reproject(
        source=data,
        destination=dst,
        src_transform=src_transform,
        src_crs=src_crs,
        src_nodata=sentinel,
        dst_transform=dst_transform,
        dst_crs=target_crs,
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )

    cell_area = abs(float(dst_transform.a) * float(dst_transform.e))
    return DemGrid(
        elevation=dst,
        transform=dst_transform,
        crs=target_crs,
        nodata=None,
        cell_area_m2=cell_area,
        res_x=float(dst_transform.a),
        res_y=float(dst_transform.e),
        source=path,
    )


def run_event(
    dem: DemGrid,
    rain_mm: float | np.ndarray,
    cn: float | np.ndarray,
    envelope_m: tuple[float, float] = DEFAULT_ENVELOPE,
    min_depth_m: float = 0.05,
    min_pond_cells: int = 4,
    checkpoints_lonlat: list[tuple[float, float]] | None = None,
    checkpoint_names: list[str] | None = None,
    checkpoint_depth_m: float = 0.05,
    sensitivity_fraction: float = 0.2,
    rain_units: str | None = None,
    water_mask: np.ndarray | None = None,
    auto_water_mask: bool = True,
) -> dict:
    """Run one rainfall event and return the full evidence bundle."""
    report = quality.QualityReport()
    dem_arr = np.asarray(dem.elevation, dtype="float64")

    quality.check_domain(dem_arr, report)
    quality.check_georeference(dem.crs, dem.transform, report)
    quality.check_elevation(dem_arr, None, report, envelope_m=envelope_m, units=rain_units,
                            reference_envelope=DEFAULT_ENVELOPE)
    quality.check_nodata_holes(dem_arr, None, report)

    shape = dem_arr.shape
    rain_grid = np.asarray(rain_mm, dtype="float64")
    if rain_grid.ndim == 0:
        rain_grid = np.full(shape, float(rain_grid), dtype="float64")
    cn_grid = np.asarray(cn, dtype="float64")
    if cn_grid.ndim == 0:
        cn_grid = np.full(shape, float(cn_grid), dtype="float64")

    quality.check_rainfall(rain_grid, report)
    quality.check_curve_number(cn_grid, report)

    # Fail fast. Once the quality gate rejects the input there is nothing
    # meaningful left to compute, and continuing means later steps run against
    # data the model has already declared unusable — for a missing CRS that used
    # to crash the run inside a coordinate transform instead of reporting a
    # clean rejection.
    if report.status == "rejected":
        return {
            "status": report.status,
            "quality": report.as_dict(),
            "outcome": "rejected",
            "reason": [i.message for i in report.fatal],
            "rainfall_mm": float(np.nanmean(rain_grid)),
            "curve_number_mean": float(np.nanmean(cn_grid)),
            "domain": {
                "rows": int(shape[0]),
                "cols": int(shape[1]),
                "cells": int(dem_arr.size),
                "valid_cells": int(dem.valid.sum()),
                "cell_area_m2": dem.cell_area_m2,
                "crs": str(dem.crs),
                "resolution_m": [dem.res_x, abs(dem.res_y)],
            },
            "depth_m": None,
        }

    # ---- permanent water ------------------------------------------------
    # Two independent sources are merged rather than preferred: a land-cover
    # water class and the flat-surface detector each miss different things
    # (irrigation ponds vs. flat hardstanding), and a false water cell only
    # removes a cell from the ponding analysis while a missed one can create a
    # fake lake that dominates every depth statistic.
    water_components: list[dict] = []
    if auto_water_mask:
        detected, water_components = detect_flat_water(dem_arr)
        water_mask = detected if water_mask is None else (np.asarray(water_mask, dtype=bool) | detected)
    if water_mask is not None:
        water_mask = np.asarray(water_mask, dtype=bool)
        report.add(
            "WATER_MASK",
            quality.INFO,
            "Excluded %.3f km2 of permanent water surface from the ponding analysis."
            % (float(water_mask.sum()) * dem.cell_area_m2 / 1e6),
            water_cells=int(water_mask.sum()),
            water_area_km2=float(water_mask.sum()) * dem.cell_area_m2 / 1e6,
            detected_components=len(water_components),
        )

    valid = dem.valid
    if water_mask is not None:
        valid = valid & ~water_mask

    result: dict = {
        "status": report.status,
        "quality": report.as_dict(),
        "domain": {
            "rows": int(shape[0]),
            "cols": int(shape[1]),
            "cells": int(dem_arr.size),
            "valid_cells": int(valid.sum()),
            "water_cells": int(water_mask.sum()) if water_mask is not None else 0,
            "water_area_km2": float(water_mask.sum()) * dem.cell_area_m2 / 1e6 if water_mask is not None else 0.0,
            "cell_area_m2": dem.cell_area_m2,
            "crs": str(dem.crs),
            "resolution_m": [dem.res_x, abs(dem.res_y)],
            "area_km2": float(valid.sum()) * dem.cell_area_m2 / 1e6,
        },
        "rainfall_mm": float(np.nanmean(rain_grid)),
        "curve_number_mean": float(np.nanmean(cn_grid)),
    }
    if water_components:
        result["water_bodies"] = _locate(water_components, dem.transform, str(dem.crs))

    # ---- terrain ---------------------------------------------------------
    filled = fill_depressions(dem_arr, nodata=None)
    depth_capacity = depression_depth(dem_arr, filled, nodata=None)
    sinks = flow_sinks(dem_arr)
    slope = slope_degrees(np.nan_to_num(dem_arr, nan=0.0), dem.res_x, abs(dem.res_y))
    result["terrain"] = {
        "filled_min_m": float(np.nanmin(filled)),
        "filled_max_m": float(np.nanmax(filled)),
        "max_depression_depth_m": float(depth_capacity.max()),
        "depression_cells": int((depth_capacity > min_depth_m).sum()),
        "mean_slope_deg": float(np.nanmean(slope[valid])) if valid.any() else 0.0,
        "max_slope_deg": float(np.nanmax(slope[valid])) if valid.any() else 0.0,
    }

    # ---- runoff ----------------------------------------------------------
    rain_eff = np.where(valid, rain_grid, 0.0)
    cn_eff = np.where(valid, cn_grid, 0.0)
    runoff = scs_cn_runoff(rain_eff, cn_eff)
    runoff = np.where(valid, runoff, 0.0)
    result["runoff"] = {
        "mean_runoff_mm": float(runoff[valid].mean()) if valid.any() else 0.0,
        "max_runoff_mm": float(runoff.max()),
        "runoff_coefficient": float(runoff[valid].sum() / rain_eff[valid].sum())
        if valid.any() and rain_eff[valid].sum() > 0
        else 0.0,
    }

    # ---- ponding ---------------------------------------------------------
    ponding = run_ponding(
        dem=dem_arr,
        filled=filled,
        depth_capacity=depth_capacity,
        sinks=sinks,
        runoff_mm=runoff,
        cell_area_m2=dem.cell_area_m2,
        min_depth_m=min_depth_m,
        min_pond_cells=min_pond_cells,
        transform=dem.transform,
        exclude_mask=water_mask,
    )
    result["ponding"] = ponding.as_dict()

    balance = water_balance(
        rain_mm=rain_eff,
        cn=cn_eff,
        runoff_mm=runoff,
        stored_m3=ponding.total_stored_m3,
        spilled_m3=ponding.total_spilled_m3,
        outflow_m3=ponding.outflow_m3,
        stored_solver_m3=float(sum(b.stored_m3 for b in ponding.basins)),
        cell_area_m2=dem.cell_area_m2,
    )
    result["water_balance"] = balance

    # ---- sensitivity (D04) ----------------------------------------------
    result["sensitivity"] = _sensitivity(
        dem, filled, depth_capacity, sinks, rain_grid, cn_grid, valid, ponding,
        sensitivity_fraction, min_depth_m, water_mask,
    )

    # ---- checkpoints (D02) ----------------------------------------------
    if checkpoints_lonlat:
        result["checkpoints"] = hit_rate(
            checkpoints_lonlat, ponding.depth_m, dem.transform, checkpoint_depth_m
        )
        result["checkpoint_ranking"] = point_rank_analysis(
            ponding.depth_m, checkpoints_lonlat, dem.transform, str(dem.crs), exclusion_mask=water_mask
        )
        result["checkpoint_mechanism"] = classify_checkpoint_mechanism(
            ponding.depth_m,
            depth_capacity,
            checkpoints_lonlat,
            dem.transform,
            str(dem.crs),
            names=checkpoint_names,
        )

    basin_table, basin_qc = _basin_table(ponding, dem, slope=slope)
    result["basins"] = basin_table
    result["basin_qc"] = basin_qc

    checks = {
        "D01_water_balance": {
            "relative_error": balance["relative_error"],
            "tolerance": balance["tolerance"],
            "passed": balance["passed"],
        },
        "D01b_raster_vs_solver": {
            "relative_error": balance.get("raster_vs_solver_relative_error"),
            "tolerance": 1e-6,
            "passed": bool(
                balance.get("raster_vs_solver_relative_error") is None
                or balance["raster_vs_solver_relative_error"] <= 1e-6
            ),
        },
        "D04_sensitivity": result["sensitivity"],
    }
    if "checkpoints" in result:
        checks["D02_checkpoint_hit_rate"] = result["checkpoints"]
    result["checks"] = checks
    structural = [
        checks["D01_water_balance"],
        checks["D01b_raster_vs_solver"],
        checks["D04_sensitivity"],
    ]
    if "checkpoints" in result:
        structural.append(checks["D02_checkpoint_hit_rate"])
    result["outcome"] = "ok" if all(c.get("passed", True) for c in structural) else "degraded"
    result["depth_m"] = ponding.depth_m
    return result


def _sensitivity(
    dem, filled, depth_capacity, sinks, rain_grid, cn_grid, valid, baseline, fraction, min_depth_m,
    exclude_mask=None,
) -> dict:
    """Curve-number sweep: structural monotonicity plus a bounded response.

    Structural check (the one that actually catches numerical instability): more
    runoff must never produce less ponded area. The reported tolerance is derived
    from the hydrology rather than picked for convenience: in SCS-CN a +/-20% CN
    change moves the runoff coefficient by roughly 50%, so a pond-area response
    within +/-45% is consistent with the model, while an inversion or a blow-up
    is not.
    """
    factors = [1.0 - fraction, 1.0 - fraction / 2.0, 1.0, 1.0 + fraction / 2.0, 1.0 + fraction]
    variants = []
    areas = []
    runoffs = []
    for factor in factors:
        cn_p = np.clip(cn_grid * factor, 30.0, 100.0)
        runoff_p = scs_cn_runoff(np.where(valid, rain_grid, 0.0), np.where(valid, cn_p, 0.0))
        runoff_p = np.where(valid, runoff_p, 0.0)
        res = run_ponding(
            dem=dem.elevation,
            filled=filled,
            depth_capacity=depth_capacity,
            sinks=sinks,
            runoff_mm=runoff_p,
            cell_area_m2=dem.cell_area_m2,
            min_depth_m=min_depth_m,
            exclude_mask=exclude_mask,
        )
        areas.append(float(res.pond_area_m2))
        runoffs.append(float(runoff_p[valid].mean()) if valid.any() else 0.0)
        variants.append({"cn_factor": factor, "pond_area_m2": float(res.pond_area_m2), "mean_runoff_mm": runoffs[-1]})

    baseline_area = areas[factors.index(1.0)]
    epsilon = 1e-6 * max(1.0, baseline_area)
    monotone = all(areas[i] <= areas[i + 1] + epsilon for i in range(len(areas) - 1))
    max_change = max((abs(a - baseline_area) / baseline_area) for a in areas) if baseline_area > 0 else 0.0
    tolerance = 0.45
    return {
        "fraction": fraction,
        "baseline_area_m2": baseline_area,
        "variants": variants,
        "monotone": bool(monotone),
        "max_relative_area_change": float(max_change),
        "tolerance": tolerance,
        "passed": bool(monotone and max_change <= tolerance),
    }


def _basin_table(
    ponding,
    dem,
    slope: np.ndarray | None = None,
    limit: int = 200,
    deep_m: float = 2.0,
    small_m2: float = 10_000.0,
    steep_deg: float = 10.0,
):
    """Build the reviewer-facing basin table with WGS84 coordinates and QC flags.

    Two independent reasons a basin is called out rather than headlined:

    * `verify_required` — deep but tiny. At 30 m resolution a pond 2 m deep and
      under a hectare wide is more likely a DEM artefact or an excavated pit than
      a rainfall puddle.
    * `outside_validity` — the surrounding terrain is steeper than `steep_deg`.
      Depression *storage* is the wrong concept on a hillslope: that water keeps
      moving downslope instead of ponding, so the depth is not a flood depth.
    """
    from rasterio.warp import transform as warp_transform

    basins = sorted(ponding.basins, key=lambda b: -b.max_depth_m)
    xs = [b.x_m if b.x_m is not None else 0.0 for b in basins]
    ys = [b.y_m if b.y_m is not None else 0.0 for b in basins]
    if xs:
        lon, lat = warp_transform(str(dem.crs), "EPSG:4326", xs, ys)
    else:
        lon, lat = [], []

    table = []
    qc = {
        "deep_and_tiny_count": 0,
        "outside_validity_count": 0,
        "deep_threshold_m": deep_m,
        "small_threshold_m2": small_m2,
        "steep_threshold_deg": steep_deg,
        "note": (
            "Deep tiny ponds and steep-catchment basins are reported separately: the first are "
            "candidate DEM artefacts, the second lie outside the validity domain of a "
            "depression-storage model."
        ),
    }
    screened_area = 0.0
    screened_max = 0.0
    screened_count = 0

    for index, b in enumerate(basins):
        local_slope = None
        if slope is not None and b.row >= 0 and b.col >= 0:
            r0, r1 = max(0, b.row - 2), min(slope.shape[0], b.row + 3)
            c0, c1 = max(0, b.col - 2), min(slope.shape[1], b.col + 3)
            if r1 > r0 and c1 > c0:
                local_slope = float(np.nanmean(slope[r0:r1, c0:c1]))
        tiny_deep = b.max_depth_m >= deep_m and b.pond_area_m2 <= small_m2
        too_steep = local_slope is not None and local_slope > steep_deg
        if tiny_deep:
            qc["deep_and_tiny_count"] += 1
        if too_steep:
            qc["outside_validity_count"] += 1
        if not too_steep:
            screened_area += b.pond_area_m2
            screened_max = max(screened_max, b.max_depth_m)
            screened_count += 1
        if index >= limit:
            continue
        table.append(
            {
                "basin_id": b.basin_id,
                "row": b.row,
                "col": b.col,
                "lon": float(lon[index]) if index < len(lon) else None,
                "lat": float(lat[index]) if index < len(lat) else None,
                "catchment_cells": b.catchment_cells,
                "pond_cells": b.pond_cells,
                "catchment_area_m2": b.catchment_area_m2,
                "pond_area_m2": b.pond_area_m2,
                "inflow_m3": b.inflow_m3,
                "stored_m3": b.stored_m3,
                "spilled_m3": b.spilled_m3,
                "stage_m": b.stage_m,
                "max_depth_m": b.max_depth_m,
                "local_slope_deg": local_slope,
                "verify_required": bool(tiny_deep),
                "outside_validity": bool(too_steep),
            }
        )
    qc["screened_pond_area_m2"] = screened_area
    qc["screened_max_depth_m"] = screened_max
    qc["screened_basin_count"] = screened_count
    return table, qc


def _locate(components: list[dict], transform, grid_crs=METRIC_CRS) -> list[dict]:
    """Attach WGS84 coordinates to grid-space components so a reviewer can look them up."""
    from rasterio.warp import transform as warp_transform

    xs, ys = [], []
    for comp in components:
        x, y = transform * (comp["col"] + 0.5, comp["row"] + 0.5)
        xs.append(x)
        ys.append(y)
    lon, lat = warp_transform(grid_crs, "EPSG:4326", xs, ys)
    out = []
    for comp, lo, la in zip(components, lon, lat):
        item = dict(comp)
        item["lon"] = float(lo)
        item["lat"] = float(la)
        out.append(item)
    return sorted(out, key=lambda c: -c["cells"])


def hit_rate(
    points_lonlat: list[tuple[float, float]],
    depth_m: np.ndarray,
    transform,
    min_depth_m: float = 0.05,
) -> dict:
    """Fraction of known waterlogging points that the model floods.

    Coordinates are transformed from the metric grid back to WGS84 through the
    grid's own transform, so the comparison uses the same georeferencing as the
    raster. Points outside the domain are counted as misses and reported.
    """
    from rasterio.warp import transform as warp_transform

    rows, cols = depth_m.shape
    xs, ys = [], []
    for lon, lat in points_lonlat:
        xs.append(lon)
        ys.append(lat)
    gx, gy = warp_transform("EPSG:4326", "EPSG:32650", xs, ys)

    inv = ~transform
    hits, misses, outside = 0, 0, 0
    details = []
    for (lon, lat), x, y in zip(points_lonlat, gx, gy):
        col, row = inv * (x, y)
        r, c = int(row), int(col)
        if r < 0 or r >= rows or c < 0 or c >= cols:
            outside += 1
            details.append({"lon": lon, "lat": lat, "status": "outside_domain"})
            continue
        d = float(depth_m[r, c])
        if d > min_depth_m:
            hits += 1
            details.append({"lon": lon, "lat": lat, "status": "flooded", "depth_m": d})
        else:
            misses += 1
            details.append({"lon": lon, "lat": lat, "status": "dry", "depth_m": d})
    total = len(points_lonlat)
    return {
        "points": total,
        "hits": hits,
        "misses": misses,
        "outside_domain": outside,
        "hit_rate": (hits / total) if total else 0.0,
        "tolerance": 0.70,
        "passed": bool(total > 0 and hits / total >= 0.70),
        "details": details,
    }


def attach_exposure(result: dict, dem: DemGrid, bounds_wgs84, cache_dir: str, min_depth_m: float = 0.05) -> dict:
    """Attach OSM road and building exposure to a finished run.

    Kept separate from the numerical core so a network failure degrades the
    deliverable (no exposure section) instead of invalidating a valid hydraulic
    result. The failure reason is recorded, never swallowed.
    """
    from . import exposure as exposure_mod

    depth = result.get("depth_m")
    if depth is None:
        result["exposure"] = {"available": False, "reason": "run produced no depth raster"}
        return result
    try:
        roads_path = exposure_mod.fetch_osm(bounds_wgs84, cache_dir, "roads", progress=lambda *_: None)
        road_elements = exposure_mod.load_features(roads_path)
        roads = exposure_mod.road_impact(depth, road_elements, dem.transform, dem.res_x, min_depth_m)
        underpasses = exposure_mod.underpass_impact(
            depth, road_elements, dem.transform, dem.res_x, min_depth_m
        )

        buildings_path = exposure_mod.fetch_osm(bounds_wgs84, cache_dir, "buildings", progress=lambda *_: None)
        buildings = exposure_mod.building_impact(
            depth, exposure_mod.load_features(buildings_path), dem.transform, dem.res_x, min_depth_m
        )

        critical = {"available": False, "reason": "not fetched"}
        try:
            critical_path = exposure_mod.fetch_osm(bounds_wgs84, cache_dir, "critical", progress=lambda *_: None)
            critical = exposure_mod.critical_facility_impact(
                depth, exposure_mod.load_features(critical_path), dem.transform, dem.res_x, min_depth_m
            )
            critical["available"] = True
        except Exception as exc:  # noqa: BLE001 - optional layer, reported not hidden
            critical = {"available": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        result["exposure"] = {"available": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
        return result
    result["exposure"] = {
        "available": True,
        "source": "OpenStreetMap via Overpass API",
        "roads": roads,
        "underpasses": underpasses,
        "buildings": buildings,
        "critical_facilities": critical,
    }
    # Kept for plotting only; the JSON writer drops every key starting with "_"
    # so raw OSM geometry never bloats the report.
    result["_osm_elements"] = {
        "roads": road_elements,
        "critical": exposure_mod.load_features(critical_path) if critical.get("available") else [],
    }
    return result


def rank_sample(
    depth_m,
    points_lonlat,
    transform,
    grid_crs=METRIC_CRS,
    exclusion_mask=None,
    n_background: int = 20000,
    seed: int = 20261007,
) -> dict:
    """The two samples the ranking test compares: official depths, background depths.

    Split out of `point_rank_analysis` so a figure can plot the very curve behind
    the reported AUC instead of re-deriving it. The seed is fixed, so the background
    draw is reproducible and the ROC a reader sees is the one the number came from.
    """
    import numpy as np
    from rasterio.warp import transform as warp_transform

    rows, cols = depth_m.shape
    field = np.array(depth_m, dtype="float64", copy=True)
    field[~np.isfinite(field)] = 0.0
    eligible = np.ones(field.shape, dtype=bool)
    if exclusion_mask is not None:
        eligible &= ~np.asarray(exclusion_mask, dtype=bool)

    lons = [p[0] for p in points_lonlat]
    lats = [p[1] for p in points_lonlat]
    gx, gy = warp_transform("EPSG:4326", str(grid_crs), lons, lats)
    inverse = ~transform
    official = []
    inside = 0
    for x, y in zip(gx, gy):
        c, r = inverse * (x, y)
        ri, ci = int(r), int(c)
        if 0 <= ri < rows and 0 <= ci < cols and eligible[ri, ci]:
            official.append(float(field[ri, ci]))
            inside += 1
    if not official:
        return {"available": False, "reason": "no official point falls inside the analysed grid"}

    rng = np.random.default_rng(seed)
    flat_eligible = np.flatnonzero(eligible.ravel())
    if flat_eligible.size == 0:
        return {"available": False, "reason": "no eligible background cells"}
    pick = rng.choice(flat_eligible, size=min(n_background, flat_eligible.size), replace=False)
    background = field.ravel()[pick]

    return {
        "available": True,
        "official": np.asarray(official, dtype="float64"),
        "background": background,
        "inside": inside,
    }


def point_rank_analysis(
    depth_m,
    points_lonlat,
    transform,
    grid_crs=METRIC_CRS,
    exclusion_mask=None,
    n_background: int = 20000,
    seed: int = 20261007,
) -> dict:
    """Does the model carry any signal about these points, even if it misses them?

    A binary hit rate answers one question at one depth threshold and throws the
    rest away. If the official points are missed because the threshold is wrong
    for a 100 mm scenario, the ranking test still shows it: we compare the modelled
    depth at the official points against the depth at random points drawn from the
    same study area and report the AUC (probability that a random official point
    ranks above a random background point).

    AUC 0.5 means the model is blind to these locations; clearly above 0.5 means it
    ranks them as wetter than average even when they sit under the reporting
    threshold. Both outcomes are reported as they come out.
    """
    import numpy as np

    sample = rank_sample(depth_m, points_lonlat, transform, grid_crs,
                         exclusion_mask, n_background, seed)
    if not sample.get("available"):
        return sample
    official_arr = sample["official"]
    background = sample["background"]
    inside = sample["inside"]

    # Mann-Whitney U -> AUC, computed without scipy's rankdata dependency chain
    both = np.concatenate([official_arr, background])
    order = np.argsort(both, kind="stable")
    ranks = np.empty_like(order, dtype="float64")
    ranks[order] = np.arange(1, both.size + 1, dtype="float64")
    # average ranks for ties so ties cannot inflate the statistic
    sorted_both = both[order]
    i = 0
    while i < sorted_both.size:
        j = i
        while j + 1 < sorted_both.size and sorted_both[j + 1] == sorted_both[i]:
            j += 1
        if j > i:
            ranks[order[i : j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    rank_sum = float(ranks[: official_arr.size].sum())
    n1 = official_arr.size
    n2 = background.size
    u = rank_sum - n1 * (n1 + 1) / 2.0
    auc = u / (n1 * n2) if n1 and n2 else float("nan")

    # Hanley-McNeil standard error. Without it, "AUC is about 0.5" is an
    # eyeball judgement; with it the report can say whether the model's
    # apparent skill is distinguishable from chance at all.
    q1 = auc / (2.0 - auc) if auc < 2.0 else 0.0
    q2 = (2.0 * auc * auc) / (1.0 + auc) if auc > -1.0 else 0.0
    variance = (
        auc * (1.0 - auc)
        + (n1 - 1) * (q1 - auc * auc)
        + (n2 - 1) * (q2 - auc * auc)
    ) / (n1 * n2)
    std_error = float(np.sqrt(max(variance, 0.0)))
    z_score = float((auc - 0.5) / std_error) if std_error > 0 else 0.0

    return {
        "available": True,
        "official_points_inside": inside,
        "official_mean_depth_m": float(official_arr.mean()),
        "official_median_depth_m": float(np.median(official_arr)),
        "official_max_depth_m": float(official_arr.max()),
        "background_cells": int(n2),
        "background_mean_depth_m": float(background.mean()),
        "background_median_depth_m": float(np.median(background)),
        "auc": float(auc),
        "auc_std_error": std_error,
        "auc_ci95": [float(auc - 1.96 * std_error), float(auc + 1.96 * std_error)],
        "z_score": z_score,
        "distinguishable_from_chance": bool(abs(z_score) >= 1.96),
        "interpretation": (
            "AUC is the probability that a random official waterlogging point ranks wetter than a "
            "random point in the same study area. 0.5 = no signal; |z| >= 1.96 is the usual bar for "
            "calling it distinguishable from chance."
        ),
        "passed": bool(abs(z_score) >= 1.96),
        "tolerance_note": "reported as evidence, not as a pass/fail threshold",
    }


def classify_checkpoint_mechanism(
    depth_m,
    capacity_m,
    points_lonlat,
    transform,
    grid_crs=METRIC_CRS,
    names=None,
    min_depth_m: float = 0.05,
) -> dict:
    """Classify each reported waterlogging point by likely mechanism.

    The model cannot predict drainage-deficiency flooding, but that failure is
    itself useful: for a list of places a city already knows flood, it can say
    which ones are explained by topography and which are not.

    * `terrain_ponding`     - the point floods in this scenario; topography explains it.
    * `terrain_storage_dry` - the point sits in a real depression that did not fill at
                              this rainfall. Worth re-running at a larger event.
    * `no_terrain_storage`  - there is no depression here at all, so topography cannot
                              explain the reported flooding. The likely cause is the
                              drainage system: undersized pipes, blocked inlets,
                              or a culvert that cannot pass the flow.

    That distinction is actionable: the first group responds to storage and landscape
    measures, the third to pipe and inlet work.
    """
    import numpy as np
    from rasterio.warp import transform as warp_transform

    rows, cols = depth_m.shape
    depth_field = np.nan_to_num(np.asarray(depth_m, dtype="float64"))
    capacity_field = np.nan_to_num(np.asarray(capacity_m, dtype="float64"))
    names = names or ["(unnamed)"] * len(points_lonlat)

    lons = [p[0] for p in points_lonlat]
    lats = [p[1] for p in points_lonlat]
    gx, gy = warp_transform("EPSG:4326", str(grid_crs), lons, lats)
    inverse = ~transform

    classified = []
    counts = {"terrain_ponding": 0, "terrain_storage_dry": 0, "no_terrain_storage": 0, "outside_domain": 0}
    for name, x, y in zip(names, gx, gy):
        c, r = inverse * (x, y)
        ri, ci = int(r), int(c)
        if not (0 <= ri < rows and 0 <= ci < cols):
            counts["outside_domain"] += 1
            classified.append({"name": name, "mechanism": "outside_domain"})
            continue
        # a small neighbourhood, for the same reason the facility check uses one:
        # a reported point is a place, and a coordinate can land on a cell edge
        window_d = depth_field[max(0, ri - 1) : ri + 2, max(0, ci - 1) : ci + 2]
        window_c = capacity_field[max(0, ri - 1) : ri + 2, max(0, ci - 1) : ci + 2]
        d = float(window_d.max()) if window_d.size else 0.0
        cap = float(window_c.max()) if window_c.size else 0.0
        if d > min_depth_m:
            mechanism = "terrain_ponding"
        elif cap > min_depth_m:
            mechanism = "terrain_storage_dry"
        else:
            mechanism = "no_terrain_storage"
        counts[mechanism] += 1
        classified.append(
            {
                "name": name,
                "mechanism": mechanism,
                "modelled_depth_m": round(d, 3),
                "depression_capacity_m": round(cap, 3),
            }
        )

    total = len(classified)
    return {
        "points": total,
        "counts": counts,
        "terrain_explained": counts["terrain_ponding"] + counts["terrain_storage_dry"],
        "terrain_explained_fraction": (counts["terrain_ponding"] + counts["terrain_storage_dry"]) / total
        if total
        else 0.0,
        "interpretation": (
            "no_terrain_storage means topography cannot explain the reported flooding at this "
            "location; the drainage system is the likely cause. This is a mechanism diagnosis, "
            "not a prediction of flooding."
        ),
        "classified": classified,
    }


def write_outputs(result: dict, dem: DemGrid, out_dir: str, tag: str = "run") -> dict:
    """Persist the depth raster, the basin table, the report and a map."""
    import matplotlib

    matplotlib.use("Agg")
    # Chinese labels render as empty boxes without a CJK-capable family, and the
    # plots carry real place names. floodkit.runio reports the missing font instead
    # of letting matplotlib substitute one silently.
    from .runio import configure_fonts

    configure_fonts()
    import matplotlib.pyplot as plt
    import rasterio

    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    depth = result.get("depth_m")
    if depth is None:
        paths["report_json"] = _write_json(result, out_dir, tag)
        return paths

    tif = os.path.join(out_dir, "%s_depth.tif" % tag)
    profile = {
        "driver": "GTiff",
        "height": depth.shape[0],
        "width": depth.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": dem.crs,
        "transform": dem.transform,
        "compress": "lzw",
        "nodata": -9999.0,
    }
    with rasterio.open(tif, "w", **profile) as dst:
        dst.write(np.where(np.isfinite(depth), depth, -9999.0).astype("float32"), 1)
    paths["depth_tif"] = tif

    png = os.path.join(out_dir, "%s_map.png" % tag)
    elevation = dem.elevation
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5), dpi=170)
    im0 = axes[0].imshow(elevation, cmap="terrain")
    axes[0].set_title("DEM (m)")
    fig.colorbar(im0, ax=axes[0], shrink=0.8)
    masked = np.ma.masked_where(depth <= 0, depth)
    im1 = axes[1].imshow(elevation, cmap="gray")
    im2 = axes[1].imshow(masked, cmap="Blues", vmin=0, vmax=max(0.5, float(np.nanmax(depth)) if depth.size else 1.0))
    axes[1].set_title("modelled ponding depth (m)")
    fig.colorbar(im2, ax=axes[1], shrink=0.8)
    for ax in axes:
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle("%s | rain %.1f mm | CN %.0f | ponded %.2f km2 | max %.2f m"
                 % (tag, result["rainfall_mm"], result["curve_number_mean"],
                    result["ponding"]["pond_area_m2"] / 1e6, result["ponding"]["max_depth_m"]))
    fig.tight_layout()
    fig.savefig(png)
    plt.close(fig)
    paths["map_png"] = png

    critical_png = _plot_critical(result, dem, out_dir, tag)
    if critical_png:
        paths["critical_png"] = critical_png

    paths["report_json"] = _write_json(result, out_dir, tag)
    paths["report_md"] = write_markdown(result, out_dir, tag)
    return paths


def _plot_critical(result: dict, dem: DemGrid, out_dir: str, tag: str):
    """Map the life-safety picture: where the underpasses and facilities are.

    A depth raster answers "how much water". This answers "where should someone
    go first", which is the question the numbers 118 flooded underpasses and 77
    flooded subway entrances actually raise.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    exposure = result.get("exposure") or {}
    elements = result.get("_osm_elements") or {}
    depth = result.get("depth_m")
    if not exposure.get("available") or depth is None or not elements:
        return None

    from .exposure import _is_underpass, _project_many

    underpass_xy = []
    for element in elements.get("roads", []):
        if not _is_underpass(element.get("tags") or {}):
            continue
        geometry = [(pt["lon"], pt["lat"]) for pt in (element.get("geometry") or []) if "lon" in pt]
        if len(geometry) < 2:
            continue
        grid = _project_many(geometry, dem.transform)
        underpass_xy.append(([p[0] for p in grid], [p[1] for p in grid]))

    facilities = (exposure.get("critical_facilities") or {})
    top = facilities.get("top_facilities") or []

    fig, axes = plt.subplots(1, 2, figsize=(14, 6), dpi=170)
    masked = np.ma.masked_where(depth <= 0, depth)
    vmax = max(0.5, float(np.nanmax(depth)) if depth.size else 1.0)

    for ax in axes:
        ax.imshow(masked, cmap="Blues", vmin=0, vmax=vmax)
        for xs, ys in underpass_xy:
            ax.plot(xs, ys, color="#d62728", linewidth=1.1, alpha=0.85, solid_capstyle="round")
        if top:
            lons = [f["lon"] for f in top]
            lats = [f["lat"] for f in top]
            import rasterio.warp as warp

            xs, ys = warp.transform("EPSG:4326", str(dem.crs), lons, lats)
            inv = ~dem.transform
            grid_points = [inv * (x, y) for x, y in zip(xs, ys)]
            cols = [pt[0] for pt in grid_points]
            rows = [pt[1] for pt in grid_points]
            depths = [f["depth_m"] for f in top]
            ax.scatter(cols, rows, c=depths, cmap="autumn_r", s=46, edgecolors="black",
                       linewidths=0.6, zorder=5, vmin=0, vmax=max(depths) if depths else 1.0)
        ax.set_xticks([])
        ax.set_yticks([])

    axes[0].set_title("full study area")
    # Zoom on the worst cluster: the deepest critical facility plus surroundings.
    if top:
        r0, c0 = rows[0], cols[0]
        half = 40  # ~1.1 km: enough to see the street pattern around the point
        axes[1].set_xlim(max(0, c0 - half), c0 + half)
        axes[1].set_ylim(r0 + half, max(0, r0 - half))
        axes[1].set_title("zoom: deepest critical point (%s, %.2f m)" % (top[0]["name"], top[0]["depth_m"]))
    else:
        axes[1].set_title("zoom: no flooded facility")

    handles = [
        Line2D([0], [0], color="#d62728", lw=1.6, label="underpass / tunnel sections"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#ff7f0e",
               markeredgecolor="black", markersize=7, label="critical facility (colour = depth)"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False)
    up = exposure.get("underpasses") or {}
    fig.suptitle("%s | %d underpass sections flooded (%d life-safety) | %d critical facilities flooded (%d subway entrances)"
                 % (tag, up.get("underpass_ways_flooded", 0), up.get("life_safety_count", 0),
                    facilities.get("facilities_flooded", 0), facilities.get("subway_entrances_flooded", 0)))
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    path = os.path.join(out_dir, "%s_critical.png" % tag)
    fig.savefig(path)
    plt.close(fig)
    return path


def _write_json(result: dict, out_dir: str, tag: str) -> str:
    path = os.path.join(out_dir, "%s_report.json" % tag)
    # Underscore-prefixed keys are in-memory working data, and the depth raster
    # belongs in the GeoTIFF: serialising it as a nested list added 680 KB to
    # every report. Both are excluded explicitly so neither rule can displace
    # the other again.
    payload = {
        k: v
        for k, v in result.items()
        if not k.startswith("_") and k not in EXCLUDED_FROM_REPORT
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=_default)
    return path


def write_markdown(report: dict, out_dir: str, tag: str) -> str:
    """Render the Markdown report from a report dict plus whatever is on disk.

    Reads the output directory rather than a dict of paths, because the good figures
    are produced by separate scripts that run *after* the event: the report-grade
    map, the story figure, the interactive page and the GeoJSON exports. The
    pipeline writes this file immediately after a run, and
    scripts/make_report_map.py calls it again so the artifact list reflects what a
    reader actually has in front of them.

    This also fixes what the first version embedded: it linked `_map.png`, the
    diagnostic plot with the black background, as the report's illustration, and
    stayed that way after a proper map became available.
    """
    path = os.path.join(out_dir, "%s_report.md" % tag)
    lines = ["# Event report: %s" % tag, ""]
    lines.append("- outcome: **%s**" % report.get("outcome", "unknown"))
    if report.get("reason"):
        lines.append("- rejection reason: %s" % "; ".join(report["reason"]))
    dom = report.get("domain", {})
    lines.append("- domain: %s x %s cells, %.2f km2, %.1f m grid" %
                 (dom.get("rows"), dom.get("cols"), dom.get("area_km2", 0.0),
                  (dom.get("resolution_m") or [0])[0]))
    lines.append("- rainfall: %.1f mm, mean CN %.0f" % (report.get("rainfall_mm", 0.0), report.get("curve_number_mean", 0.0)))
    if "runoff" in report:
        lines.append("- runoff: mean %.1f mm, coefficient %.3f" %
                     (report["runoff"]["mean_runoff_mm"], report["runoff"]["runoff_coefficient"]))
    if "ponding" in report:
        p = report["ponding"]
        lines.append("- ponding: %.3f km2, max depth %.2f m, basins %d" %
                     (p["pond_area_m2"] / 1e6, p["max_depth_m"], p["basin_count"]))
    lines.append("")
    lines.append("## Machine-checkable indicators")
    lines.append("")
    lines.append("| indicator | value | tolerance | passed |")
    lines.append("|---|---|---|---|")
    for name, check in (report.get("checks") or {}).items():
        if "relative_error" in check:
            lines.append("| %s | %s | %s | %s |" % (
                name, _fmt(check["relative_error"]), _fmt(check["tolerance"]), check["passed"]))
        elif "max_relative_area_change" in check:
            lines.append("| %s | %s | %s | %s |" % (
                name, _fmt(check["max_relative_area_change"]), _fmt(check["tolerance"]), check["passed"]))
        elif "hit_rate" in check:
            lines.append("| %s | %s | %s | %s |" % (
                name, _fmt(check["hit_rate"]), _fmt(check["tolerance"]), check["passed"]))
    lines.append("")
    if report.get("quality", {}).get("issues"):
        lines.append("## Input quality")
        lines.append("")
        for issue in report["quality"]["issues"]:
            lines.append("- [%s] %s: %s" % (issue["severity"], issue["code"], issue["message"]))
        lines.append("")
    exposure = report.get("exposure")
    if exposure:
        lines.append("## Exposure (OpenStreetMap)")
        lines.append("")
        if not exposure.get("available"):
            lines.append("- not available: %s" % exposure.get("reason"))
        else:
            roads = exposure["roads"]
            buildings = exposure["buildings"]
            underpasses = exposure.get("underpasses") or {}
            critical = exposure.get("critical_facilities") or {}

            if underpasses:
                lines.append("### 下穿通道与隧道（生命危险级）")
                lines.append("")
                lines.append("- 下穿/隧道段共 %d 段，被淹 **%d 段**，其中生命危险级（水深 ≥0.3 m）**%d 段**"
                             % (underpasses["underpass_ways"], underpasses["underpass_ways_flooded"],
                                underpasses["life_safety_count"]))
                top = underpasses.get("top_segments") or []
                if top:
                    lines.append("")
                    lines.append("| 名称 | 类型 | layer | 淹没长度 (m) | 最大水深 (m) | 风险 |")
                    lines.append("|---|---|---|---|---|---|")
                    for seg in top[:10]:
                        lines.append("| %s | %s | %s | %.0f | %.2f | %s |"
                                     % (seg["name"], seg.get("highway") or "-", seg.get("layer") or "-",
                                        seg["flooded_length_m"], seg["max_depth_m"], seg["risk"]))
                lines.append("")

            if critical.get("available"):
                lines.append("### 关键设施（地铁口 / 医院 / 学校 / 消防）")
                lines.append("")
                lines.append("- 设施共 %d 个，被淹 **%d 个**；其中**地铁口被淹 %d 个**"
                             % (critical["facilities_total"], critical["facilities_flooded"],
                                critical["subway_entrances_flooded"]))
                flooded_kinds = critical.get("flooded_by_kind") or {}
                if flooded_kinds:
                    lines.append("- 被淹分类：%s"
                                 % "，".join("%s %d 个" % (k, v) for k, v in
                                             sorted(flooded_kinds.items(), key=lambda kv: -kv[1])))
                top = critical.get("top_facilities") or []
                if top:
                    lines.append("")
                    lines.append("| 类型 | 名称 | 水深 (m) |")
                    lines.append("|---|---|---|")
                    for fac in top[:10]:
                        lines.append("| %s | %s | %.2f |" % (fac["kind"], fac["name"], fac["depth_m"]))
                lines.append("")

            lines.append("### 路网与建筑")
            lines.append("")
            lines.append("- road ways flooded: %d / %d, flooded length %.0f m of %.0f m"
                         % (roads["ways_flooded"], roads["ways_total"],
                            roads["flooded_length_m"], roads["total_length_m"]))
            lines.append("- buildings affected: %d / %d (%.1f%%), footprint %.0f m2"
                         % (buildings["buildings_affected"], buildings["buildings_total"],
                            100 * buildings["affected_fraction"], buildings["affected_footprint_area_m2"]))
            if roads.get("top_segments"):
                lines.append("")
                lines.append("| road | type | flooded length (m) | max depth (m) |")
                lines.append("|---|---|---|---|")
                for seg in roads["top_segments"][:10]:
                    lines.append("| %s | %s | %.0f | %.2f |"
                                 % (seg["name"], seg["highway"], seg["flooded_length_m"], seg["max_depth_m"]))
        lines.append("")

    # ---- figures ------------------------------------------------------------
    lines.append("## 图")
    lines.append("")
    figures = [s for s in FIGURES if os.path.exists(os.path.join(out_dir, tag + s[0]))]
    if not figures:
        lines.append("- 尚未生成图形（先跑 `python scripts/make_report_map.py %s`）" % tag)
    for suffix, caption, _note in figures:
        lines.append("### %s" % caption)
        lines.append("")
        lines.append("![%s](%s)" % (caption, tag + suffix))
        lines.append("")

    # ---- artifacts ----------------------------------------------------------
    lines.append("## 产物清单")
    lines.append("")
    lines.append("| 文件 | 大小 | 说明 |")
    lines.append("|---|---|---|")
    present = 0
    for suffix, _caption, note in ARTIFACTS:
        full = os.path.join(out_dir, tag + suffix)
        if not os.path.exists(full):
            continue
        present += 1
        lines.append("| `%s` | %.2f MB | %s |"
                     % (tag + suffix, os.path.getsize(full) / 1e6, note))
    lines.append("")
    lines.append("共 %d 个产物文件。" % present)
    lines.append("")

    # ---- reproduce ----------------------------------------------------------
    command = _reproduce_command(report, out_dir, tag)
    if command:
        lines.append("## 复算")
        lines.append("")
        lines.append("```")
        lines.append(command)
        lines.append("```")
        lines.append("")
        lines.append("指标数值取自 `%s_report.json`；本页每个数字都由该文件生成，未手工录入。"
                     % tag)
        lines.append("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


#: Figures written by scripts/, in the order a reader should meet them. The
#: diagnostic `_map.png` is deliberately last: it is the pipeline's own plot, it
#: exists before any of the others, and the first version of this report embedded
#: it as *the* illustration and never moved on.
FIGURES = [
    ("_report_map.png", "报告级风险地图（OpenStreetMap 底图 + 分级水深 + 风险点位）",
     "scripts/make_report_map.py"),
    ("_negative_result.png", "负结果：官方易涝点核对（ROC / 水深分布 / 点位）",
     "scripts/make_negative_result_figure.py"),
    ("_critical.png", "下穿隧道与关键设施风险点位", "pipeline（随运行生成）"),
    ("_story.png", "四联故事图：地形 → 曲线数 → 水深 → 风险", "scripts/make_story_figure.py"),
    ("_map.png", "诊断图（DEM 与水深）", "pipeline（随运行生成）"),
]

ARTIFACTS = [
    ("_report.json", "", "机器可判指标的完整数值；本报告与交互页面的唯一数据来源"),
    ("_report.md", "", "本文件"),
    ("_depth.tif", "", "逐格淹没深度栅格（GeoTIFF，EPSG:32650，LZW 压缩）"),
    ("_report_map.png", "", "报告级风险地图（PNG）"),
    ("_report_map.pdf", "", "同上，矢量、字体内嵌，可印刷放大"),
    ("_report_map.svg", "", "同上，矢量、文字已转曲，不依赖任何字体"),
    ("_negative_result.png", "", "官方易涝点核对的负结果图（ROC / 分布 / 点位）"),
    ("_negative_result.pdf", "", "同上，矢量、字体内嵌"),
    ("_negative_result.json", "", "负结果图上的每个数字，及其与报告的逐位对账结果"),
    ("_critical.png", "", "风险点位图（下穿 / 设施）"),
    ("_map.png", "", "诊断图（DEM + 水深），调试用"),
    ("_story.png", "", "四联故事图（地形 → CN → 水深 → 风险）"),
    ("_report.html", "", "单文件离线交互式报告（双击打开，无需联网）"),
    ("_ponding.geojson", "", "水深分级多边形，可直接拖进 QGIS / ArcGIS"),
    ("_exposure.geojson", "", "承灾体矢量：下穿段、关键设施、淹没道路、受影响建筑"),
]


def _reproduce_command(report: dict, out_dir: str, tag: str) -> str:
    """Rebuild the CLI call that produced this run, from the run itself.

    The bounding box is not stored in the report, so it is read back off the depth
    raster. Without that the command would need a hardcoded box, which is the exact
    mistake floodkit.runio exists to avoid.
    """
    depth_path = os.path.join(out_dir, "%s_depth.tif" % tag)
    if not os.path.exists(depth_path):
        return ""
    try:
        import rasterio

        from . import runio

        with rasterio.open(depth_path) as src:
            bounds = runio.bounds_wgs84(src.transform, src.crs, (src.height, src.width))
    except Exception:  # noqa: BLE001 - a missing raster must not lose the report
        return ""
    exposure = report.get("exposure") or {}
    return (
        "python -X utf8 -m floodkit run --bounds %.4f %.4f %.4f %.4f "
        "--rain-mm %g --cn %g --cn-source %s --tag %s%s"
        % (bounds[0], bounds[1], bounds[2], bounds[3],
           report.get("rainfall_mm", 0.0), round(report.get("curve_number_mean", 0.0)),
           "landcover" if report.get("landcover") else "uniform", tag,
           " --exposure" if exposure.get("available") else "")
    )


def _write_markdown(result: dict, out_dir: str, tag: str, paths: dict = None) -> str:
    """Backwards-compatible shim; the paths argument is no longer used."""
    _ = paths
    return write_markdown(result, out_dir, tag)


def _fmt(value) -> str:
    """Format an indicator for the report; a missing value is 'n/a', never a crash.

    Zero-storage runs legitimately have no raster-vs-solver ratio, and a report
    generator that throws on that turns an honest boundary case into a failure.
    """
    if value is None:
        return "n/a"
    try:
        return "%.4f" % float(value)
    except (TypeError, ValueError):
        return str(value)


def _default(obj):
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)

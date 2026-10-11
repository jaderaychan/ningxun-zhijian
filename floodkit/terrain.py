"""Terrain processing for the urban-ponding model.

Pure numpy + scipy, no GDAL geometry dependency beyond reading the raster.

Pipeline
--------
1. `fill_depressions`  : priority-flood depression filling (Barnes et al. 2014)
2. `depression_depth`  : filled - original -> ponding depth capacity
3. `label_basins`      : connected components of significant depressions
4. `flow_sinks`        : D8 steepest-descent routing on the ORIGINAL surface,
                         each cell's terminal sink -> sink-watershed catchments
5. `catchment_area`    : contributing cell count per terminal sink
"""

from __future__ import annotations

import heapq

import numpy as np
from scipy import ndimage

NODATA = -9999.0

# 8-neighbourhood, ordered so that index 0 is East and neighbours go clockwise.
NEIGHBOURS = np.array(
    [
        (0, 1),    # E
        (1, 1),    # SE
        (1, 0),    # S
        (1, -1),   # SW
        (0, -1),   # W
        (-1, -1),  # NW
        (-1, 0),   # N
        (-1, 1),   # NE
    ],
    dtype=np.int64,
)
# horizontal distance multiplier: 1 for cardinal, sqrt(2) for diagonal
NEIGHBOUR_DIST = np.array([1.0, np.sqrt(2.0), 1.0, np.sqrt(2.0), 1.0, np.sqrt(2.0), 1.0, np.sqrt(2.0)])


def as_float(array) -> np.ndarray:
    return np.asarray(array, dtype="float64")


def fill_depressions(dem: np.ndarray, nodata: float | None = None) -> np.ndarray:
    """Return the depression-filled surface using priority-flood.

    Invalid cells (`nodata` or non-finite) must never enter the priority queue:
    a NaN key silently destroys the heap order and every downstream depth. They
    are instead excluded from processing and used as *open boundaries* — the
    valid cells touching a missing cell are seeded, so unknown ground can drain
    a basin but can never dam one. That is the conservative direction for flood
    mapping: it under- rather than over-predicts ponding.
    """
    dem = as_float(dem)
    rows, cols = dem.shape
    filled = dem.copy()

    invalid = ~np.isfinite(dem)
    if nodata is not None:
        invalid |= dem == nodata
    if invalid.all():
        return filled

    closed = invalid.copy()  # invalid cells are never queued or processed
    heap: list[tuple[float, int, int]] = []

    # domain border
    for r in range(rows):
        for c in (0, cols - 1):
            _push(heap, filled, closed, r, c)
    for c in range(cols):
        for r in (0, rows - 1):
            _push(heap, filled, closed, r, c)

    # valid cells adjacent to a hole are also open boundaries
    if invalid.any():
        growth = ndimage.binary_dilation(invalid, structure=np.ones((3, 3), dtype=bool))
        ring = growth & ~invalid
        for r, c in zip(*np.nonzero(ring)):
            _push(heap, filled, closed, int(r), int(c))

    while heap:
        elev, r, c = heapq.heappop(heap)
        for dr, dc in NEIGHBOURS:
            nr, nc = r + dr, c + dc
            if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                continue
            if closed[nr, nc]:
                continue
            closed[nr, nc] = True
            if filled[nr, nc] < elev:
                filled[nr, nc] = elev
            heapq.heappush(heap, (float(filled[nr, nc]), nr, nc))
    return filled


def _push(heap, filled, closed, r, c):
    if closed[r, c]:
        return
    closed[r, c] = True
    heapq.heappush(heap, (float(filled[r, c]), int(r), int(c)))


def depression_depth(dem: np.ndarray, filled: np.ndarray, nodata: float | None = None) -> np.ndarray:
    """Ponding capacity per cell (metres), zero outside depressions."""
    depth = as_float(filled) - as_float(dem)
    depth[~np.isfinite(depth)] = 0.0
    depth[depth < 0] = 0.0
    if nodata is not None:
        depth[as_float(dem) == nodata] = 0.0
    return depth


def label_basins(
    depth: np.ndarray,
    min_depth: float = 0.05,
    min_cells: int = 4,
) -> tuple[np.ndarray, int]:
    """Label depression basins (8-connected) that are deep/wide enough to matter.

    Returns (labels, count) where labels is int32, 0 = no basin.
    """
    mask = np.asarray(depth) > float(min_depth)
    structure = np.ones((3, 3), dtype=bool)
    labels, count = ndimage.label(mask, structure=structure)
    if count == 0:
        return labels.astype("int32"), 0

    keep = np.zeros(count + 1, dtype=bool)
    sizes = ndimage.sum_labels(np.ones_like(labels, dtype="float64"), labels, index=np.arange(1, count + 1))
    maxdepth = ndimage.maximum(depth, labels=labels, index=np.arange(1, count + 1))
    for i in range(count):
        if sizes[i] >= min_cells and maxdepth[i] >= min_depth:
            keep[i + 1] = True
    if not keep[1:].any():
        return np.zeros_like(labels, dtype="int32"), 0

    remap = np.zeros(count + 1, dtype="int32")
    remap[keep] = np.arange(1, int(keep.sum()) + 1, dtype="int32")
    return remap[labels], int(keep.sum())


def flow_sinks(dem: np.ndarray) -> np.ndarray:
    """D8 steepest-descent routing on the original surface.

    Returns an int64 array of flat indices naming the terminal cell each cell
    drains to. A terminal cell drains to itself (local minimum, domain boundary,
    or a cell whose neighbours are all higher). Ties are broken by neighbour
    order, and flow only moves to a strictly lower neighbour, so the pointer
    graph is acyclic by construction.
    """
    dem = as_float(dem)
    rows, cols = dem.shape
    flat = dem.ravel()
    n = flat.size

    best_slope = np.full(n, -np.inf)
    target = np.arange(n, dtype=np.int64)  # default: self (terminal)

    idx = np.arange(n)
    r = idx // cols
    c = idx % cols
    for (dr, dc), dist in zip(NEIGHBOURS, NEIGHBOUR_DIST):
        nr = r + dr
        nc = c + dc
        ok = (nr >= 0) & (nr < rows) & (nc >= 0) & (nc < cols)
        if not ok.any():
            continue
        src = idx[ok]
        dst = (nr[ok] * cols + nc[ok])
        drop = flat[src] - flat[dst]
        slope = drop / dist
        better = slope > 0.0
        if not better.any():
            continue
        s = src[better]
        d = dst[better]
        v = slope[better]
        upd = v > best_slope[s]
        s = s[upd]
        d = d[upd]
        v = v[upd]
        best_slope[s] = v
        target[s] = d

    # path compression: repeatedly follow pointers until they stop changing
    for _ in range(64):
        nxt = target[target]
        if np.array_equal(nxt, target):
            break
        target = nxt
    return target


def catchment_area(dem: np.ndarray, sinks: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Count how many cells drain to each terminal cell.

    Returns (sink_cell_index, cell_count) for every terminal cell that receives
    at least one cell, sorted by descending count.
    """
    counts = np.bincount(sinks, minlength=dem.size)
    nonzero = np.nonzero(counts)[0]
    order = np.argsort(-counts[nonzero])
    return nonzero[order].astype("int64"), counts[nonzero][order].astype("int64")


def slope_degrees(dem: np.ndarray, cell_size_x: float, cell_size_y: float) -> np.ndarray:
    """Horn slope in degrees."""
    dem = as_float(dem)
    dzdy, dzdx = np.gradient(dem, abs(cell_size_y), abs(cell_size_x))
    slope = np.arctan(np.hypot(dzdx, dzdy))
    return np.degrees(slope)


def detect_flat_water(
    elevation: np.ndarray,
    max_range_m: float = 0.6,
    window: int = 5,
    min_cells: int = 200,
    min_depression_m: float = 1.0,
) -> tuple[np.ndarray, list[dict]]:
    """Detect standing water surfaces (lakes, wide rivers, reservoirs).

    Two conditions must hold together:

    1. **Flatness** — the local elevation range inside a `window` is tiny. A DEM
       carries the water *surface* over a lake, and that surface is flat.
    2. **Sits below its surroundings** — the patch is lower than the ring around
       it. Flatness alone also matches car parks, airports and reclaimed plains,
       which are land and must stay in the analysis.

    Returns (mask, components) where each component records its centroid and the
    evidence used, so a reviewer can check the call rather than trust it.
    """
    dem = as_float(elevation)
    invalid = ~np.isfinite(dem)
    structure = np.ones((3, 3), dtype=bool)
    if invalid.all():
        return np.zeros(dem.shape, dtype=bool), []

    high = np.where(invalid, -np.inf, dem)
    low = np.where(invalid, np.inf, dem)
    local_min = ndimage.minimum_filter(low, size=window, mode="nearest")
    local_max = ndimage.maximum_filter(high, size=window, mode="nearest")
    flat = np.isfinite(local_min) & np.isfinite(local_max) & ((local_max - local_min) <= max_range_m)
    flat &= ~invalid

    labels, count = ndimage.label(flat, structure=structure)
    if count == 0:
        return np.zeros(dem.shape, dtype=bool), []

    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    ring = ndimage.binary_dilation(flat, structure=structure, iterations=window)
    components: list[dict] = []
    mask = np.zeros(dem.shape, dtype=bool)

    for label_id in range(1, count + 1):
        if sizes[label_id] < min_cells:
            continue
        region = labels == label_id
        region_mean = float(dem[region & ~invalid].mean())
        around = ring & ~region & ~invalid
        if not around.any():
            continue
        ring_mean = float(dem[around].mean())
        drop = ring_mean - region_mean
        if drop < min_depression_m:
            continue
        rr, cc = np.nonzero(region)
        mask |= region
        components.append(
            {
                "label": int(label_id),
                "cells": int(sizes[label_id]),
                "surface_elev_m": region_mean,
                "surrounding_mean_m": ring_mean,
                "drop_m": drop,
                "row": int(rr.mean()),
                "col": int(cc.mean()),
            }
        )
    return mask, components


def stage_from_volume(
    elevations: np.ndarray,
    volumes: np.ndarray,
    target_volume: float,
) -> tuple[float, float]:
    """Invert a monotone stage-volume curve by exact linear interpolation.

    `elevations` must be sorted ascending with matching cumulative `volumes`.
    Returns (stage, stored_volume).
    """
    elevations = np.asarray(elevations, dtype="float64")
    volumes = np.asarray(volumes, dtype="float64")
    if elevations.size == 0:
        return float("nan"), 0.0
    if target_volume <= 0.0:
        return float(elevations[0]), 0.0
    if target_volume >= volumes[-1]:
        return float(elevations[-1]), float(volumes[-1])
    k = int(np.searchsorted(volumes, target_volume, side="left"))
    if k == 0:
        return float(elevations[0]), 0.0
    v0, v1 = volumes[k - 1], volumes[k]
    e0, e1 = elevations[k - 1], elevations[k]
    if v1 <= v0:
        return float(e1), float(v1)
    frac = (target_volume - v0) / (v1 - v0)
    return float(e0 + frac * (e1 - e0)), float(target_volume)

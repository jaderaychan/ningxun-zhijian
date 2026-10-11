"""Land cover driven curve numbers (ESA WorldCover 10 m).

Replaces the uniform CN assumption, which was the single largest simplification
in the model: asphalt (CN 98) and grass (CN 74) behave nothing alike, and a city
is a patchwork of both.

Method (TR-55 composite curve number):
    CN = impervious_fraction * CN_impervious
       + (1 - impervious_fraction) * CN_pervious(dominant natural class)

`impervious_fraction` is the share of 10 m WorldCover sub-cells classified as
built-up inside each model cell; the pervious term comes from the dominant
non-built class. Both are produced by reprojecting the 10 m source onto the model
grid — fraction via average resampling, dominant class via mode resampling — so
the statistics are computed on real sub-cell data rather than on a point sample.
"""

from __future__ import annotations

import os
import time
from urllib.request import Request, urlopen

# ESA WorldCover v200 (2021), 3x3 degree tiles, EPSG:4326, ~10 m.
WORLDCOVER_BASE = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
WORLDCOVER_VERSION = "v200/2021/map"

# Class -> curve number for hydrologic soil group C (TR-55 style values).
CN_BY_CLASS = {
    10: 70.0,   # tree cover
    20: 74.0,   # shrubland
    30: 74.0,   # grassland
    40: 78.0,   # cropland
    50: 98.0,   # built-up  (impervious; handled through the fraction term)
    60: 88.0,   # bare / sparse
    70: 95.0,   # snow and ice (not expected in Jiangsu)
    80: 100.0,  # permanent water (excluded from the analysis)
    90: 85.0,   # herbaceous wetland
    95: 85.0,   # mangroves
    100: 74.0,  # moss and lichen
}
WATER_CLASS = 80
BUILT_CLASS = 50
CN_IMPERVIOUS = 98.0

CLASS_NAMES = {
    10: "tree cover", 20: "shrubland", 30: "grassland", 40: "cropland",
    50: "built-up", 60: "bare/sparse", 70: "snow/ice", 80: "water",
    90: "wetland", 95: "mangroves", 100: "moss/lichen",
}


def tile_name(bounds_wgs84: tuple[float, float, float, float]) -> str:
    """WorldCover tile covering the bounds (3-degree grid, SW corner naming)."""
    west, south, east, north = bounds_wgs84
    lon = int(west // 3) * 3
    lat = int(south // 3) * 3
    ns = "N%02d" % lat if lat >= 0 else "S%02d" % abs(lat)
    ew = "E%03d" % lon if lon >= 0 else "W%03d" % abs(lon)
    return "ESA_WorldCover_10m_2021_v200_%s%s_Map" % (ns, ew)


def tile_url(name: str) -> str:
    return "%s/%s/%s.tif" % (WORLDCOVER_BASE, WORLDCOVER_VERSION, name)


def ensure_tile(bounds_wgs84, cache_dir: str, timeout: int = 900, progress=print) -> str:
    """Download the covering WorldCover tile once and cache it (about 130 MB)."""
    name = tile_name(bounds_wgs84)
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, name + ".tif")
    if os.path.exists(dest) and os.path.getsize(dest) > 10_000_000:
        progress("cached  %s  %.1f MB" % (name, os.path.getsize(dest) / 1048576.0))
        return dest
    url = tile_url(name)
    started = time.time()
    tmp = dest + ".part"
    request = Request(url, headers={"User-Agent": "floodkit/0.1"})
    with urlopen(request, timeout=timeout) as response, open(tmp, "wb") as fh:
        total = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    os.replace(tmp, dest)
    progress("fetched %s  %.1f MB in %.0fs" % (name, total / 1048576.0, time.time() - started))
    return dest


def curve_number_grid(
    tile_path: str,
    bounds_wgs84,
    transform,
    shape: tuple[int, int],
    target_crs: str = "EPSG:32650",
    hsg_shift: float = 0.0,
) -> dict:
    """Build a CN grid and a water mask on the model grid from WorldCover.

    Returns a dict with `cn`, `water_mask`, `impervious_fraction` and class
    statistics, so the report can show *why* the curve numbers came out as they
    did instead of asking a reviewer to trust a number.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject
    from rasterio.windows import from_bounds

    with rasterio.open(tile_path) as src:
        window = from_bounds(*bounds_wgs84, transform=src.transform)
        window = window.round_offsets().round_lengths()
        # pad by a few cells so reprojection edge effects stay outside the AOI
        pad = 4
        col0 = max(0, int(window.col_off) - pad)
        row0 = max(0, int(window.row_off) - pad)
        col1 = min(src.width, int(window.col_off + window.width) + pad)
        row1 = min(src.height, int(window.row_off + window.height) + pad)
        window = rasterio.windows.Window(col0, row0, col1 - col0, row1 - row0)
        classes = src.read(1, window=window)
        src_transform = src.window_transform(window)
        src_crs = src.crs

    classes = classes.astype("float32")
    classes[classes <= 0] = np.nan
    built = np.where(np.isnan(classes), np.nan, (classes == BUILT_CLASS).astype("float32"))

    dominant = np.zeros(shape, dtype="float32")
    reproject(
        source=np.nan_to_num(classes, nan=0.0),
        destination=dominant,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=transform,
        dst_crs=target_crs,
        resampling=Resampling.mode,
    )
    fraction = np.zeros(shape, dtype="float32")
    reproject(
        source=np.nan_to_num(built, nan=0.0),
        destination=fraction,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=transform,
        dst_crs=target_crs,
        resampling=Resampling.average,
    )
    fraction = np.clip(fraction, 0.0, 1.0)

    pervious = np.full(shape, 85.0, dtype="float64")
    for klass, cn in CN_BY_CLASS.items():
        if klass == BUILT_CLASS:
            continue
        pervious[dominant.astype("int32") == klass] = cn

    cn = fraction.astype("float64") * CN_IMPERVIOUS + (1.0 - fraction.astype("float64")) * pervious
    cn = np.clip(cn + hsg_shift, 30.0, 100.0)

    water_mask = dominant.astype("int32") == WATER_CLASS

    unique, counts = np.unique(dominant.astype("int32"), return_counts=True)
    total = float(dominant.size) if dominant.size else 1.0
    class_stats = [
        {
            "class": int(k),
            "name": CLASS_NAMES.get(int(k), "unknown"),
            "cells": int(c),
            "fraction": float(c) / total,
            "cn": CN_BY_CLASS.get(int(k)),
        }
        for k, c in sorted(zip(unique.tolist(), counts.tolist()), key=lambda t: -t[1])
        if k != 0
    ]

    return {
        "cn": cn,
        "water_mask": water_mask,
        "impervious_fraction": fraction,
        "dominant_class": dominant.astype("int32"),
        "class_stats": class_stats,
        "mean_impervious_fraction": float(fraction.mean()),
        "cn_min": float(cn.min()),
        "cn_max": float(cn.max()),
        "cn_mean": float(cn.mean()),
        "source": "ESA WorldCover 10m v200 (2021), TR-55 composite CN, HSG C",
        "tile": os.path.basename(tile_path),
    }

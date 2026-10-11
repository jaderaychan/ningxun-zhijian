"""Copernicus DEM GLO-30 tile discovery and caching (AWS open data, no key)."""

from __future__ import annotations

import os
import time
from urllib.request import Request, urlopen

BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"


def tile_name(lat_floor: int, lon_floor: int) -> str:
    """Name of the 1x1 degree tile whose SW corner is (lat_floor, lon_floor)."""
    ns = "N%02d_00" % lat_floor if lat_floor >= 0 else "S%02d_00" % abs(lat_floor)
    ew = "E%03d_00" % lon_floor if lon_floor >= 0 else "W%03d_00" % abs(lon_floor)
    base = "Copernicus_DSM_COG_10_%s_%s_DEM" % (ns, ew)
    return base


def tile_url(name: str) -> str:
    return "%s/%s/%s.tif" % (BUCKET, name, name)


def tiles_for_bounds(bounds_wgs84: tuple[float, float, float, float]) -> list[str]:
    west, south, east, north = bounds_wgs84
    lats = range(int(np_floor(south)), int(np_floor(north)) + 1)
    lons = range(int(np_floor(west)), int(np_floor(east)) + 1)
    names = []
    for la in lats:
        for lo in lons:
            names.append(tile_name(la, lo))
    return names


def np_floor(value: float) -> int:
    import math

    return math.floor(value)


def ensure_tiles(
    bounds_wgs84: tuple[float, float, float, float],
    cache_dir: str,
    timeout: int = 120,
    progress=print,
) -> list[str]:
    """Download any missing tiles and return their local paths."""
    os.makedirs(cache_dir, exist_ok=True)
    paths = []
    for name in tiles_for_bounds(bounds_wgs84):
        dest = os.path.join(cache_dir, name + ".tif")
        if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
            progress("cached  %s  %.1f MB" % (name, os.path.getsize(dest) / 1048576.0))
            paths.append(dest)
            continue
        url = tile_url(name)
        req = Request(url, headers={"User-Agent": "floodkit/0.1"})
        started = time.time()
        tmp = dest + ".part"
        with urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
            total = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
        os.replace(tmp, dest)
        progress("fetched %s  %.1f MB in %.1fs" % (name, total / 1048576.0, time.time() - started))
        paths.append(dest)
    return paths


def local_tile_path(name: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, name + ".tif")

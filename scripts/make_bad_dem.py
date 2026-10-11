"""Create the deliberately broken DEMs used for failure-mode demonstrations.

The demo claims "feed it bad data and it refuses instead of inventing a result".
That claim is only worth anything if the bad data is a real file anyone can feed
in, so these are generated from the same good tile the normal runs use.

Produces, under data/fixtures/:
    bad_dem_units.tif    elevation delivered in feet (x3.28084)
    bad_dem_no_crs.tif   same ground surface, coordinate reference system removed
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.windows import from_bounds  # noqa: E402

BOUNDS = (118.72, 32.02, 118.82, 32.10)
DEM_DIR = os.path.join(ROOT, "data", "dem")
OUT_DIR = os.path.join(ROOT, "data", "fixtures")
FEET_PER_METRE = 3.28084


def find_source_tile() -> str:
    for name in sorted(os.listdir(DEM_DIR)):
        if name.lower().endswith(".tif"):
            return os.path.join(DEM_DIR, name)
    raise SystemExit("no DEM tile in %s — run `python -m floodkit fetch-dem --bounds ...` first" % DEM_DIR)


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    source = find_source_tile()
    print("source tile:", os.path.basename(source))

    with rasterio.open(source) as src:
        window = from_bounds(*BOUNDS, transform=src.transform).round_offsets().round_lengths()
        data = src.read(1, window=window).astype("float32")
        transform = src.window_transform(window)
        profile = src.profile.copy()
        nodata = src.nodata

    # The source profile carries the FULL tile's width/height/transform. Writing
    # the clipped window while keeping them declares a 3600x3600 raster whose
    # georeferencing does not match its contents: every later read is silently
    # misaligned and the "bad" file quietly stops being bad. Override all three.
    profile.update(
        width=data.shape[1],
        height=data.shape[0],
        transform=transform,
    )

    valid = np.isfinite(data)
    if nodata is not None:
        valid &= data != nodata

    # ---- 1. unit error: metres written as feet -------------------------
    feet = data.copy()
    feet[valid] = data[valid] * FEET_PER_METRE
    units_path = os.path.join(OUT_DIR, "bad_dem_units.tif")
    with rasterio.open(units_path, "w", **profile) as dst:
        dst.write(feet, 1)
    print("wrote %s  (elevation range %.1f .. %.1f, i.e. metres x %.5f)"
          % (os.path.basename(units_path), float(feet[valid].min()), float(feet[valid].max()), FEET_PER_METRE))

    # ---- 2. missing georeference ---------------------------------------
    noc_rs_path = os.path.join(OUT_DIR, "bad_dem_no_crs.tif")
    profile_no_crs = profile.copy()
    profile_no_crs.update(crs=None)
    with rasterio.open(noc_rs_path, "w", **profile_no_crs) as dst:
        dst.write(data, 1)
    with rasterio.open(noc_rs_path) as check:
        print("wrote %s  (crs=%s)" % (os.path.basename(noc_rs_path), check.crs))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

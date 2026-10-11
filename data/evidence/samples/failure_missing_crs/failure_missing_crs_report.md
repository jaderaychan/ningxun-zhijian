# Event report: failure_missing_crs

- outcome: **ok**
- domain: 321 x 341 cells, 81.73 km2, 28.1 m grid
- rainfall: 100.0 mm, mean CN 85
- runoff: mean 61.0 mm, coefficient 0.610
- ponding: 7.619 km2, max depth 4.08 m, basins 2471

## Machine-checkable indicators

| indicator | value | tolerance | passed |
|---|---|---|---|
| D01_water_balance | 0.0033 | 0.0100 | True |
| D01b_raster_vs_solver | 0.0000 | 0.0000 | True |
| D04_sensitivity | 0.2914 | 0.4500 | True |

## Input quality

- [warning] DEM_HOLES: DEM has 3.08% missing cells (largest contiguous hole: 919 cells); holes are treated as open drains.
- [info] WATER_MASK: Excluded 2.049 km2 of permanent water surface from the ponding analysis.

![map](failure_missing_crs_map.png)

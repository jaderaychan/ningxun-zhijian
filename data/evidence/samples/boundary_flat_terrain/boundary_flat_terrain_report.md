# Event report: boundary_flat_terrain

- outcome: **ok**
- domain: 321 x 341 cells, 78.83 km2, 28.1 m grid
- rainfall: 100.0 mm, mean CN 92
- runoff: mean 78.2 mm, coefficient 0.782
- ponding: 0.000 km2, max depth 0.00 m, basins 0

## Machine-checkable indicators

| indicator | value | tolerance | passed |
|---|---|---|---|
| D01_water_balance | 0.0000 | 0.0100 | True |
| D01b_raster_vs_solver | n/a | 0.0000 | True |
| D04_sensitivity | 0.0000 | 0.4500 | True |

## Input quality

- [warning] DEM_FLAT: DEM is perfectly flat; ponding is undefined and results will be reported as not applicable.
- [info] DEM_NO_HOLES: DEM has no missing cells.
- [info] WATER_MASK: Excluded 7.614 km2 of permanent water surface from the ponding analysis.

## 图

### 诊断图（DEM 与水深）

![诊断图（DEM 与水深）](boundary_flat_terrain_map.png)

## 产物清单

| 文件 | 大小 | 说明 |
|---|---|---|
| `boundary_flat_terrain_report.json` | 0.01 MB | 机器可判指标的完整数值；本报告与交互页面的唯一数据来源 |
| `boundary_flat_terrain_report.md` | 0.00 MB | 本文件 |
| `boundary_flat_terrain_depth.tif` | 0.01 MB | 逐格淹没深度栅格（GeoTIFF，EPSG:32650，LZW 压缩） |
| `boundary_flat_terrain_map.png` | 1.02 MB | 诊断图（DEM + 水深），调试用 |

共 4 个产物文件。

## 复算

```
python -X utf8 -m floodkit run --bounds 118.7184 32.0188 118.8214 32.1015 --rain-mm 100 --cn 92 --cn-source uniform --tag boundary_flat_terrain
```

指标数值取自 `boundary_flat_terrain_report.json`；本页每个数字都由该文件生成，未手工录入。

# Event report: boundary_zero_rain

- outcome: **ok**
- domain: 321 x 341 cells, 76.32 km2, 28.1 m grid
- rainfall: 0.0 mm, mean CN 92
- runoff: mean 0.0 mm, coefficient 0.000
- ponding: 0.000 km2, max depth 0.00 m, basins 0

## Machine-checkable indicators

| indicator | value | tolerance | passed |
|---|---|---|---|
| D01_water_balance | 0.0000 | 0.0100 | True |
| D01b_raster_vs_solver | n/a | 0.0000 | True |
| D04_sensitivity | 0.0000 | 0.4500 | True |

## Input quality

- [warning] DEM_HOLES: DEM has 3.08% missing cells (largest contiguous hole: 919 cells); holes are treated as open drains.
- [warning] RAIN_ZERO: Rainfall scenario is empty; the model will report zero ponding as a boundary case, not a failure.
- [info] WATER_MASK: Excluded 7.614 km2 of permanent water surface from the ponding analysis.

## 图

### 诊断图（DEM 与水深）

![诊断图（DEM 与水深）](boundary_zero_rain_map.png)

## 产物清单

| 文件 | 大小 | 说明 |
|---|---|---|
| `boundary_zero_rain_report.json` | 0.01 MB | 机器可判指标的完整数值；本报告与交互页面的唯一数据来源 |
| `boundary_zero_rain_report.md` | 0.00 MB | 本文件 |
| `boundary_zero_rain_depth.tif` | 0.01 MB | 逐格淹没深度栅格（GeoTIFF，EPSG:32650，LZW 压缩） |
| `boundary_zero_rain_map.png` | 1.02 MB | 诊断图（DEM + 水深），调试用 |

共 4 个产物文件。

## 复算

```
python -X utf8 -m floodkit run --bounds 118.7184 32.0188 118.8214 32.1015 --rain-mm 0 --cn 92 --cn-source uniform --tag boundary_zero_rain
```

指标数值取自 `boundary_zero_rain_report.json`；本页每个数字都由该文件生成，未手工录入。

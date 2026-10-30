# 集成标准

[English](INTEGRATION.md) · [架构说明](ARCHITECTURE.zh-CN.md) · [项目总览](../README.zh-CN.md)

本文定义：当另一个系统集成 `floodkit` 或 Agnes Harness 插件时，**可以依赖什么**、
**绝不能破坏什么**。文中"必须/不得/应当/可以"按 RFC 2119 的通常含义使用。

整份标准只想守住一句话：**下游消费者必须能分辨"真结果"和"看起来像结果的东西"。**
下面每一条规则都是为了保住这个能力。

---

## 1. 可依赖的保证

下列每一条都有自动化检查覆盖，集成方**可以**依赖。

| 保证 | 由谁检查 |
|---|---|
| 输入出现 `fatal` 级问题时**不产生水深栅格** | `tests/test_floodkit.py`、`floodkit samples` |
| 每一次运行都产出 `report.json`，包括被拒绝的运行 | `tests/verify_package.py` |
| `report.json` 里的每个数字都能从该次运行的输入与栅格重新推导 | `flood_verify_report`（JavaScript）、`D01b` |
| 文档里的数字与报告 JSON 一致 | `tests/check_doc_consistency.py` |
| 交互式页面不发起任何网络请求 | `tests/verify_report_html.py` |
| 矢量产物与报告对账 | `tests/verify_geojson.py` |

集成方**不得**依赖：精度类指标、流速与到达时间（不存在）、排水行为（未建模）、
南京以外的覆盖范围。

---

## 2. 接口面

只有四个。除此之外都不是受支持的接口。

### 2.1 命令行

```powershell
python -X utf8 -m floodkit run       --bounds W S E N --rain-mm R --tag TAG [--exposure]
python -X utf8 -m floodkit selftest
python -X utf8 -m floodkit samples   [--cn-source landcover]
python -X utf8 -m floodkit resolve   --name 南京
python -X utf8 -m floodkit weather   --bounds W S E N
python -X utf8 -m floodkit verify    --tag TAG
```

**契约：**

- 每个子命令都向 **stdout 写一个 JSON 对象**，诊断信息走 stderr。
  集成方**必须**解析 stdout，不得抓取日志文本。
- 退出码属于契约的一部分：

| 退出码 | 含义 | 调用方应当 |
|---|---|---|
| `0` | 成功，`outcome` 为 `ok` 或 `degraded` | 使用结果，但仍需检查 `quality.issues` |
| `2` | 用法或输入错误（缺参数、值无法解析） | 修正调用；原样重试没有意义 |
| `3` | 无法解析或被拒绝——系统拒绝产出结果 | 把 `code` 呈现给用户；**不要**盲重试 |
| `4` | 验证失败（`selftest`、`verify`） | 视为安装已损坏 |
| `5` | `samples` 有案例与它声明的期望不符 | 视为安装已损坏 |

- 非零退出码**必须**理解为"没有结果"，而不是"部分结果"。

### 2.2 报告 JSON（`data/runs/<tag>/<tag>_report.json`）

稳定的顶层键。集成方**必须**容忍新增键。

| 键 | 内容 |
|---|---|
| `outcome` | `ok` \| `degraded` \| `rejected` |
| `status` | 与 `quality.status` 一致 |
| `quality` | `{status, issues[]}`，每条 issue 为 `{code, severity, message, detail}` |
| `domain` | `rows, cols, cells, valid_cells, cell_area_m2, resolution_m[], crs, area_km2` |
| `run_config` | `bounds_wgs84[4], rain_mm, cn, cn_source, min_depth_m, envelope_m[2], checkpoints, exposure` |
| `water_balance` | `rain/initial_abstraction/continuing_retention/generated_runoff/stored/spilled/outflow/residual`、`relative_error`、`raster_vs_solver_relative_error`、`tolerance`、`passed` |
| `ponding` | 面积、水深、洼地计数、`total_spilled_m3`、`overflowing_basins` |
| `terrain`、`runoff`、`landcover` | **实际用到的输入**，使运行可被重新推导 |
| `sensitivity` | `fraction, variants[], monotone, max_relative_area_change, tolerance, passed` |
| `checks` | 各项指标的机器可读判定 |
| `exposure` | `available`、`source`、`roads`、`underpasses`、`buildings`、`critical_facilities` |

**规则：**

- `quality.issues[].severity` **必须**是 `fatal`、`warning`、`info` 之一。
- `fatal` 级问题**必须**伴随 `outcome: "rejected"`，**并且不存在水深栅格**。
  一个同时发现栅格与 `fatal` 问题的消费者，**必须**判定该次运行无效。
- 体积单位为立方米，水深为米，面积为平方米——除非键名另有所指。
  集成方**不得**靠猜测重新换算单位。
- `water_balance.relative_error` 是**无量纲比值**；报告里显示的 `0.000000`
  在 JSON 中是 `2.4e-16`。应当按机器精度理解，不得当成精确的零。

### 2.3 Harness 工具

共 8 个，由 `agh-plugin/index.mjs` 注册。元数据是规范性的：

| 工具 | `isReadOnly` | `requiresApproval` | `replay` |
|---|---|---|---|
| `flood_selftest` | true | `never` | `safe` |
| `flood_resolve_place` | true | `never` | `safe` |
| `flood_verify_report` | true | `never` | `safe` |
| `flood_crosslang_check` | true | `never` | `safe` |
| `flood_weather` | false（写缓存） | `never` | `safe` |
| `flood_publish_report` | false（写派生页面） | `never` | `safe` |
| `flood_fetch_dem` | false | `always` | `never` |
| `flood_run_event` | false | `always` | `never` |

**规则：**

- 每个工具定义**必须**声明**全部 8 个**元数据键。缺键会让扩展工厂以
  `E_TOOLDEF_META` 失败，插件永远停在 `starting`。
- 参数 schema **必须**设置 `additionalProperties: false`。
  集成方**不得**在调用工具前绕过 schema 校验。
- 会写文件的工具**必须**声明 `replay: "never"`，避免崩溃后悄悄重跑并合成一个未知结果。
- `requiresApproval: "always"` **不得**为了让自动化演示更顺而放宽。
  写操作上的人工在环是这个系统被提交时的一项性质。

### 2.4 产物

给定运行标签 `TAG`，在 `data/runs/TAG/` 下产出：

```
TAG_report.json        机器可读结果（总是产出，即使被拒绝）
TAG_report.md          人类可读摘要
TAG_depth.tif          水深栅格（outcome 为 rejected 时不存在）
TAG_map.png            DEM + 水深诊断图
TAG_critical.png       关键点位图
TAG_report.html        单文件离线交互式报告
TAG_ponding.geojson    水深多边形
TAG_exposure.geojson   受影响的承灾体
TAG_report_map.png/.pdf/.svg   印刷级地图（按需生成）
```

集成方**不得**假设除 `_report.json` 之外的任何产物一定存在。
其余每个文件是否存在，取决于 `outcome` 以及跑了哪条命令。

---

## 3. 错误码分类

机器可读的 `code` 取值。集成方**应当**把 `fatal` 码直接呈现给用户，
而不是另编一句文案——每个码的 message 都是写给人读的。

**输入质量（含严重度）**

| 码 | 严重度 | 含义 |
|---|---|---|
| `CRS_MISSING`、`TRANSFORM_MISSING`、`TRANSFORM_DEGENERATE`、`TRANSFORM_ROTATED`、`TRANSFORM_TINY_CELL` | fatal | 栅格无法定位，或仿射参数退化 |
| `DEM_ALL_NODATA` | fatal | 完全没有有效高程 |
| `DEM_UNIT_DECLARED_NON_METRIC` | fatal | 单位是英尺之类 |
| `DEM_OUT_OF_ENVELOPE` | fatal | 高程范围对该研究区不合理（疑单位、基准或取错瓦片） |
| `DEM_HOLES` | warning | 有缺失格，按开放排水口处理——报告出来，不隐藏 |
| `DEM_FLAT` | warning | 地形平坦，积水结果没有意义 |
| `DEM_NO_HOLES` | info | 无缺失格 |
| `RAIN_NON_FINITE`、`RAIN_NEGATIVE`、`RAIN_IMPLAUSIBLE` | fatal | 降雨输入非法 |
| `RAIN_ZERO` | info | 零降雨事件——降级运行，仍可评估 |
| `CN_NON_FINITE`、`CN_OUT_OF_RANGE` | fatal | 曲线数超出 1–100 没有物理意义 |
| `DOMAIN_TOO_SMALL` | fatal | 研究区小于可分析的最小尺寸 |

**调用**

| 码 | 含义 |
|---|---|
| `NO_NAME`、`NO_PLACE` | 缺少位置参数 |
| `PLACE_UNRESOLVED` | 地名不在内置研究区内（只认南京） |
| `BOUNDS_REJECTED` | 范围非法，或超出已验证的适用域 |

**预报**

| 码 | 含义 |
|---|---|
| `TIME_OUT_OF_SCOPE`、`OUT_OF_HORIZON` | 请求时间超出 15 天预报窗口 |
| `BAD_TIME` | 时间无法解析 |
| `FORECAST_UNAVAILABLE`、`EMPTY_FORECAST` | 上游模式没有可用结果 |

---

## 4. 集成方不得破坏的不变量

1. **`fatal` 问题不得被处理成成功。** 不得把一次 `rejected` 运行后处理成一张地图。
2. **不得复制第二份数据定义。** 深度分级、配色、范围、承灾体计数**必须**来自
   `floodkit.runio`，不得在消费方重新推导。
3. **不得手写数字进生成物。** 页面、地图、报告里的每个数字**必须**从该次运行的
   `report.json` / `depth.tif` 读取。
4. **失败的指标必须保持失败。** 把红色指标变绿的渲染器**必须**拒绝产出。
5. **审批不可绕过。** 写工具保持 `requiresApproval: "always"`。
6. **范围保持封闭。** 南京以外、15 天以外的请求**必须**失败，
   不得退回到默认区域或最近值。
7. **不得声称精确零。** 机器精度的残差必须按机器精度表述，
   不得四舍五入成"0"而暗示精确。

---

## 5. 版本与兼容性

| 项 | 要求 |
|---|---|
| Python | **3.12**。1e-16 量级的残差依赖 3.12 的补偿求和；量级跨版本稳定，末位不稳定 |
| 依赖 | `numpy`、`scipy`、`matplotlib`、`rasterio`、`Pillow`（见 `requirements.txt`，是下限而非锁版本） |
| Node.js | 任意支持 ESM 与 `node:http` 的版本。插件没有运行时依赖 |
| 运行底座 | 具备 §2.3 所述扩展工具 API 的 Agnes Harness |
| 报告 schema | 只做增量变更。删除或改变 §2.2 所列键的类型属于破坏性变更 |
| 工具名 | 稳定。`flood_*` 名称被文档、测试与执行记录同时引用 |

集成方**应当**把它验证过的 floodkit 提交号钉下来，并与任何派生结果一起记录。

---

## 6. 新增一个工具，或一个城市

**新增工具**——六步都必需：

1. 定义 schema，`additionalProperties: false`，八个元数据键齐全。
2. 诚实选择元数据：`isReadOnly` 的含义是**完全没有写入**；
   只要写缓存就是 false，哪怕不具破坏性。
3. 在 `agh-plugin/test-plugin.mjs` 里加契约测试，**必须**包含一道应当失败的负向对照。
4. 同步更新两个 README 与 `docs/ARCHITECTURE*.md` 里的工具表。
5. 更新文档中所有工具计数——这个数是被断言的，过时值在
   `tests/check_doc_consistency.py` 的 `STALE` 列表里。
6. 重跑 `node agh-plugin/test-plugin.mjs` 并确认总数。

**新增城市**——这是研究工作，不是改配置：

1. 重新推导土地覆盖 → 曲线数的映射，并在当地标定。
2. 重新推导合理高程包络，并记录理由。
3. 校验 OSM 图层的完整性；图层不全就会低估影响。
4. 如果存在公开易涝点清单，跑一次外部核对。若能达到的最好 AUC 接近 0.5，
   把它作为负结果公布，而不是省略。
5. 完成第 4 步之后，才把条目加进 `places.py`。在此之前，
   模型在该城市的诚实状态是"未验证"，质量门**必须**继续拒绝。

---

## 7. 集成方必须跑通的验收测试

```powershell
python -X utf8 -m floodkit selftest            # 解析校核，不联网
python -X utf8 tests/test_floodkit.py          # 回归测试
python -X utf8 -m floodkit samples --cn-source landcover   # 正常 / 边界 / 失败
node agh-plugin/test-plugin.mjs                # 工具契约，含负向对照
python -X utf8 tests/check_doc_consistency.py  # 文档 vs 报告 JSON
python -X utf8 tests/verify_report_html.py TAG # 页面离线且忠实于报告
python -X utf8 tests/verify_geojson.py TAG     # 矢量与报告对账
```

**全部**在目标机器上通过，集成才算被接受。
在作者机器上通过，不构成关于目标机器的任何证据。

---

## 8. 反模式

| 反模式 | 为什么它破坏了保证 |
|---|---|
| 解析人类可读的 `.md` 而不是 JSON | 散文会自由变化，JSON 才是契约 |
| 在消费方重新推导深度分级 | 同一个水深会在两个地方有两种颜色 |
| 对 `exit 3` 自动换参数重试 | `3` 表示系统出于原则拒绝，不是运气不好 |
| 为了演示放宽 `requiresApproval` | 去掉了可审计的人工在环性质 |
| 把 `0.000000` 当成精确零 | 它是 `2.4e-16`；声称精确会招来一次必然失败的核对 |
| 只报告通过的指标 | 报告的信息量恰恰来自那些没通过的 |
| 认为 `rejected` 之后还有栅格 | 按设计就是没有——这正是质量门的意义 |

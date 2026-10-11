# 不用 Agnes 也能跑：给你的协作者 / 朋友

> **一句话**：这个项目是两层的。`floodkit/` 是纯 Python 科学内核，**不需要任何模型、
> 不需要 AGH、不需要 API Key**；Agnes 只是"调度层"，负责用自然语言决定调哪个工具。
> 你朋友要复现的全部计算与全部证据，都在不需要模型的那一层里。

本文档是**唯一**面向"没有 Agnes"的读者的入口。仓库其余文档默认你能跑 AGH。

---

## 0. 先看结果：零安装

clone 下来后直接**双击打开**这个文件即可（它把底图、指标、清单全部内嵌，不联网）：

```
data/runs/nanjing_final/nanjing_final_report.html
```

这是那个交互式报告：鼠标悬停读水深、勾选图层、点两个点看地形剖面。
还有一张四联故事图 `data/runs/nanjing_final/nanjing_final_story.png`。

**这一层不需要装任何东西**——HTML 与图片已经随仓库提供。

---

## 1. 装环境（一个模型都不需要）

需要 **Python 3.12**（3.10+ 基本可用，见下）。

```bash
git clone https://github.com/jaderaychan/ningxun-zhijian.git
cd ningxun-zhijian
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows 用户也可以直接用仓库自带脚本，它会把依赖装进项目的 `.pylibs/` 并自动找 Python：

```powershell
pwsh -File scripts\setup.ps1          # 不传 -AghRepo 就只装 Python 依赖，不碰 AGH
```

> `setup.ps1` 是 Windows 专用，而且**只装依赖、不复制插件**这一点要注意：
> 参数 `-SkipPython` 是"跳过 Python"，不是"跳过 AGH"；不传 `-AghRepo` 时它自然跳过 AGH。

跑起来需要 `PYTHONPATH` 指到仓库根目录（`.pylibs` 若存在也一并加上）：

```bash
# macOS/Linux
export PYTHONPATH="$PWD:$PWD/.pylibs"
# Windows PowerShell
$env:PYTHONPATH = "$PWD;$PWD\.pylibs"
```

---

## 2. 完全离线就能跑的三件事

这三步**不下载任何数据**，只用到仓库里已有的文件。

```bash
# ① 解析校核：填洼、水位—库容反演、SCS-CN 闭式解，与解析解逐位比对
python -X utf8 -m floodkit selftest                    # 期望 5/5

# ② 回归测试：每一个修过的真 bug 都锁了一条
python -X utf8 tests/test_floodkit.py                  # 期望 31/31

# ③ 用仓库里的报告复核全部产物（报告、地图、矢量、证据图、AGH 执行记录）
python -X utf8 tests/check_doc_consistency.py          # 文档与报告一致
python -X utf8 tests/verify_map_outputs.py nanjing_final nanjing_200mm nanjing_wide
python -X utf8 tests/verify_geojson.py nanjing_final nanjing_200mm nanjing_wide
python -X utf8 tests/verify_report_html.py nanjing_final nanjing_200mm nanjing_wide
python -X utf8 tests/verify_evidence_figures.py
python -X utf8 tests/verify_agh_evidence.py
```

③ 之所以能离线跑，是因为 `data/runs/` 与 `data/evidence/` 随仓库提供；
这些脚本是**独立重算**报告里的数字，而不是信任报告。

---

## 3. 自己算一场雨（需要联网，首次约 186 MB）

```bash
python -X utf8 -m floodkit run \
  --bounds 118.72 32.02 118.82 32.10 \
  --rain-mm 100 --cn 85 --cn-source landcover \
  --tag my_run --exposure
```

- `--bounds` 是经纬度 `西 南 东 北`。**当前只支持南京**（曲线数、高程包络、承灾体图层
  都只在南京验证过），换城市会被质量门拒绝——这是刻意的，不是 bug。
- 首次运行会下载 Copernicus DEM、ESA WorldCover、OSM 承灾体，**实测冷启动 107 秒 / 约 186 MB**
  （缓存在 `data/dem`、`data/landcover`、`data/osm`）；之后再跑约 16 秒。
- 结果写到 `data/runs/my_run/`：报告 JSON + Markdown、水深 GeoTIFF、两张图。

再把它渲染成可分享的东西：

```bash
python -X utf8 scripts/make_report_map.py my_run     # 印刷级风险地图 PNG/PDF（含矢量 SVG）
python -X utf8 scripts/make_report_html.py my_run    # 单文件离线交互式报告
python -X utf8 scripts/export_geojson.py my_run      # 水深多边形 + 承灾体矢量
```

想从零把**所有**产物重建并自动自检：

```bash
python -X utf8 scripts/rebuild_outputs.py            # 约 10 分钟
```

---

## 4. 想连"工具链"也一起看？不需要模型，只需要 Node

AGH 插件是**普通 ES 模块**。彩排脚本直接调用每个工具的 `execute()`，用假上下文，
**不启动 AGH、不调用任何模型**，却走的是与智能体完全相同的参数：

```bash
# 需要 Node.js 20+，不需要 Agnes、不需要 API Key
node tests/rehearse-agent-path.mjs        # 期望 23/23，约 50 秒
node agh-plugin/test-plugin.mjs           # 期望 42/42（工具契约）
```

这一步会真的跑一次事件、渲染交互页、并**真的 fetch 它返回的网址**——
所以它是"没有模型也能验证整条链"的最强证据。

> **非 Windows 用户注意**：插件按此顺序找 Python —— `FLOODKIT_PYTHON` 环境变量 →
> `.floodkit.json` 里的 `python` → `.venv/Scripts/python.exe`（Windows）、
> `.venv/bin/python`（macOS/Linux）→ 最后退回 PATH 上的 `python`。
> 如果依赖装在别处，显式指定即可（避免它默默用了系统的 python）：
>
> ```bash
> # macOS/Linux
> export FLOODKIT_PYTHON="$PWD/.venv/bin/python"
> # Windows PowerShell
> $env:FLOODKIT_PYTHON = "$PWD\.venv\Scripts\python.exe"
> ```

---

## 5. 那么，什么才真的需要 Agnes？

**只有一件事**：在工作台对话框里用自然语言提问，让模型自己决定调哪个工具。

```bash
# 以下步骤需要 Agnes API Key 与 AGH，见 README「AGH 集成」一节
pwsh -File 启动AGH.cmd
```

不需要模型时，你依然可以：检查内核（①）、跑全部回归与验证（②③）、
自己算并渲染（④）、复现完整的工具调用链（⑤）。**科学结论与全部证据都不依赖模型。**

---

## 6. 已知限制（先看，免得白跑）

| 限制 | 说明 |
|---|---|
| **只认南京** | 曲线数、高程包络、承灾体图层只在南京验证过；换范围会被质量门拒绝 |
| **15 天时间窗** | `flood_weather` 只查未来 15 天；超范围明确拒绝，不会猜 |
| **不含排水管网** | 模型假设水只靠地形消失，**因此倾向于高估**积水范围。这是最大的简化 |
| **高程包络** | 默认 `-50..400 m`。若你的范围真的含山地，用 `--envelope-min/--envelope-max` 覆盖——**但不要猜**，猜窄了会把好数据拒掉（质量门会说明原因） |
| **中文字体** | 缺 CJK 字体时渲染器**打印警告并切英文标签**，不会渲染成方框 |
| **Python 版本** | 3.12 能逐位复现文档里的数字（D01 残差 2.44e-16 依赖 3.12 的补偿求和） |

## 7. 常见问题

**`ModuleNotFoundError: floodkit`** → `PYTHONPATH` 没设，见第 1 节。

**`rasterio` 装不上** → 用官方轮子：`python -m pip install --only-binary :all: rasterio`；
仍失败就装 [conda-forge 的 rasterio](https://anaconda.org/conda-forge/rasterio)。

**跑出来全是方框** → 装一个中文字体（如 Noto Sans CJK / 微软雅黑），
或直接接受英文标签：渲染器会在缺字体时自动切。

**首次运行很慢** → 在下载数据，见第 3 节的实测数字。缓存之后就快了。

**想要我朋友拿到的是"结果"而不是"代码"** → 仓库里已经有全部结果：
`data/runs/*/`（报告、地图、交互页、矢量）与 `data/evidence/*`（样例证据包、
降雨扫描、动画）。第 0 节可以零安装直接看。

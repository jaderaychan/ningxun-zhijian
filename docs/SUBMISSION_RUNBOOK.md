# 提交日运行手册

> 提交前请确认包内 `docs/JUDGE_SUMMARY.md`（一页纸评审速览）存在——评委的时间有限。

**这份手册的目标：把已经做完的东西变成提交。** 每一步都给出确切命令和应该看到的结果，
以及失败时怎么办。预计需要 **90–120 分钟**（不含报名审核等待）。

> 所有命令都在项目根目录 `C:\Users\14820\Desktop\江苏省ai+黑客松比赛` 下执行。

---

## 0. 先看清两个死线

| 事项 | 截止 | 谁来做 |
|---|---|---|
| **报名** | **2026-10-10 12:00** | **只有你能做** |
| 提交 | 2026-10-15 12:00 | 报名后 |

**报名是唯一真正的阻塞点。** 手册剩下的部分都在报名之后。

---

## 1. 报名（你做）

1. 按比赛通知的报名入口填写，组别选 **本科生组**，题号 **T1**。
2. 队伍名称建议：**宁讯智鉴**。
3. 作品名称建议：《**宁讯智鉴：城市内涝「感知—建模—验证」自主智能体**》。
4. 报名成功后你会得到 **参赛编号** 和 **Agnes API Key**（通常通过赛事群发放）。

**拿到后请把这三样告诉我**：参赛编号、队长姓名、Agnes API Key。
（Key 也可以你自己填，见第 2 步。）

---

## 2. 配置 Agnes 模型（5 分钟）

1. 启动 AGH（见第 3 步），浏览器会打开 http://127.0.0.1:4177/
2. 首次打开会弹出设置面板（没有模型时它自动弹出）。
3. 填入：
   - 服务地址：`https://api.agnes-ai.cn/v1`
   - API Key：赛事发放的那个
   - 模型：`agnes-3.0-flash`（**免费**；`agnes-2.5-pro` 是付费的，不要选错）
4. 保存后右侧提示应变为可以开始任务。

> **不要**使用任何非 Agnes 模型。队长承诺书里写明了「模型仅限 Agnes」。

---

## 3. 从普通会话启动 AGH（重要）

**双击 `启动AGH.cmd` 即可。**

> ⚠️ 不要用管理员/提权窗口启动。之前有一次 AGH 是在提权会话里跑起来的，
> 结果**其他进程无法通过命名管道连上它**（原生管道身份校验过不去），
> 插件安装和 SDK 行为都会变得诡异。用普通窗口最稳。

脚本会做的事：检查入口 → 检查 node → 探测 4177 是否已在运行 → 启动 `agnes serve` → 打开浏览器。

**停止服务**（在另一个窗口执行）：
```cmd
node "C:\Users\14820\Desktop\江苏省ai+黑客松比赛\_agh_src\agnes-harness-main\packages\cli\dist\local\agnes.mjs" daemon stop
```

---

## 4. 安装我们的插件（10 分钟）

### 路径 A（推荐）：双击 `安装插件.cmd`

**直接双击项目根目录的 `安装插件.cmd`**，它会：

1. 把 `agh-plugin\` 的 4 个文件同步到 `_agh_src\agnes-harness-main\flood-tools\`
2. 执行 `package inspect` 并打印 id / integrity / capabilityHash
3. 执行 `package add` —— **这里会出现一个确认提示，输入 `y` 回车**
4. 自动执行 `package trust` 和 `package enable`
5. 执行 `package status` 并核对 `desired=enabled` 与 `trusted=true`

你只需要**按一次 `y`**。

> **为什么不能做成全自动？** 因为 AGH 的 `confirmPackageInstall` 要求
> stdin/stdout 都是真实终端，否则直接返回 false —— 而且**没有任何命令行开关可以绕过**。
> 这是刻意的安全设计（不变量 INV-33：**未被回答的确认提示，绝不能当作同意**）。
> 双击 `.cmd` 打开的是真实控制台，所以能过这一关；从脚本或管道里调用则不行。

### 路径 B：Web 工作台

1. 右上角 **设置（Settings）** → 左侧 **插件管理（Plugin management）**
2. **添加插件** → 源类型选 **本地文件**，源填：`file:./flood-tools`
3. **检查（inspect）** → 确认没有 blockers → **安装** → **信任** → **启用**

### 装好之后

回到任务界面，工具列表里应出现这 8 个：

| 工具 | 作用 | 审批 |
|---|---|---|
| `flood_selftest` | 解析校核（5 项） | 只读，免审批 |
| `flood_resolve_place` | 地名 → 研究区范围（只认南京） | 只读，免审批 |
| `flood_weather` | 查预报（多模式区间，未来 15 天） | 只写缓存，免审批 |
| `flood_fetch_dem` | 下载 DEM 瓦片 | 需审批 |
| `flood_run_event` | 跑一次内涝事件 | 需审批 |
| `flood_publish_report` | 渲染交互式报告并给出网址，供用户自己查看 | 写派生页面，免审批 |
| `flood_verify_report` | 校验报告一致性 | 只读，免审批 |
| `flood_crosslang_check` | 跨语言独立复算 | 只读，免审批 |

### ⚠️ 装了插件之后**必须重启 AGH**，光开新对话不够

这一条是踩出来的，**录视频前务必记住**：

插件被**停用或卸载**时，AGH 会撤销该扩展（审计日志里的 `extension.revoked`）。
之后重新启用，daemon 会记录 `extension.registered` + `extension.loaded`，
`agnes-lock.json` 里也是 `enabled: true`——**一切看起来都正常**，
但**运行中的会话拿不到工具**，智能体会回答"当前会话里没有可用的洪水计算能力"。

**新对话解决不了**，因为它复用的是同一个 daemon 进程的扩展状态。

**正确做法**：关闭启动 AGH 的那个命令行窗口（关闭窗口即停止服务），
重新双击 `启动AGH.cmd`，让 daemon 从 `agnes-lock.json` 干净加载。

**验证方法**（2 秒，只读免审批）：

```
请调用 flood_selftest 工具，把结果告诉我
```

返回 5 项校核即为正常。**别用"列出你的工具"来验证**——模型会编一份看起来合理的清单。

**常见故障**：

| 现象 | 原因 | 对策 |
|---|---|---|
| 助手提示「当前不是真实终端」 | 从脚本/管道调用 | 双击 `安装插件.cmd` |
| inspect 报 `Expected union value` | 源路径格式不对 | 必须是 `file:./flood-tools`（有 `./`，不含 `..`） |
| 提示 `Installation cancelled` | 确认提示没被回答 | 重跑，在提示处输入 `y` |
| 安装后工具列表没有 flood_ | 没启用或没信任 | `package status` 看 desired/actual/trusted |
| **启用成功但智能体说没有工具** | **热插拔后 daemon 状态没刷新** | **重启 AGH（见上一节）** |
| 智能体说"研究区超出范围" | 地名不在南京，或时间超 15 天 | 这是**设计如此**，不是故障 |

---

## 5. 跑演示任务（15 分钟）

提示词见 [DEMO_BRIEF.md](DEMO_BRIEF.md) 的「演示任务书」。核心一句是：

> 帮我在南京主城区跑一次 100 毫米降雨的内涝分析，用真实地形与 OpenStreetMap 数据，
> 找出受影响的下穿隧道和地铁口，并证明你的结果没有算错。

模型应该依次调用：`flood_run_event` → `flood_verify_report` → `flood_crosslang_check`。

**跑完应该看到的关键数字**（与报告 JSON 一致，可当场核对）：

| 项 | 值 |
|---|---|
| 研究区 | 76.3 km²（有效高程格面积） |
| 淹没面积 | 7.81 km² |
| 被淹下穿隧道 | 117 / 420 段（生命危险级 102 段，其中车行隧道 44 段） |
| 被淹关键设施 | 146 / 292 个（含地铁口 80 个） |
| 受影响道路 | 1722 / 4751 条 |
| 受影响建筑 | 1131 / 12080 栋 |
| 最危险点位 | 南京五十中 2.55 m、新街口地铁口 #2/#13 2.47 m |

若不一致，说明报告被别的参数跑过——**以报告 JSON 为准**，并重跑一次第 5 步。

---

## 6. 采集 AGH 执行记录（5 分钟）

这是必交项「AGH 执行记录」。两个来源都要留：

1. **工作台轨迹视图**：把会话页面截图（含模型调用、工具调用、审批记录）。
2. **审计日志**：`%USERPROFILE%\.agh\data\audit\host.jsonl`

复制一份到证据目录：

```powershell
$ws = "C:\Users\14820\Desktop\江苏省ai+黑客松比赛"
Set-Location $ws
python -X utf8 scripts\export_agh_evidence.py      # 导出轨迹 + 摘要 + 审计日志
python -X utf8 tests\verify_agh_evidence.py        # 与包内报告逐项核对
```

> ⚠️ **不要只复制 `host.jsonl`。** 本版 AGH 的主机审计日志只记录**扩展生命周期**
> （`extension.registered` / `loaded` / `revoked`），**不记录任何一次工具调用**——
> 只凭它无法证明"模型自主调用了工具"，而那正是这一项要证明的事。
> `export_agh_evidence.py` 从 AGH 会话库 `sessions.db` 导出真实的 `tool/call`、
> `tool/result` 与审批事件。

> ⚠️ **诚信提醒**：`SUBMISSION_PACKET.md` 的 **§2 与 §5.3 目前是占位内容**，
> 写明了"待 AGH 实录"。**务必把它们替换成真实捕获的记录**，
> 不要保留设计稿、也不要照抄。提交的材料里出现未经实测的"证据"，一旦被核对就是硬伤。
> §2 里已经写好了替换模板，照着填即可。

**要能证明的三件事**（缺一不可）：

- [ ] 模型**自主**调用了工具（不是我手动跑的）
- [ ] 至少有 **1 条完整工具调用链**（含参数与返回）
- [ ] 用的是 **Agnes 模型**（轨迹视图里能看到模型名）

---

## 7. 复跑验证证据（10 分钟）

```powershell
$ws = "C:\Users\14820\Desktop\江苏省ai+黑客松比赛"
$py = "C:\Users\14820\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "$ws\.pylibs;$ws"; $env:MPLCONFIGDIR = "$ws\.mplconfig"
Set-Location $ws

python -X utf8 -m floodkit selftest                      # 期望 5/5，ok: true
python -X utf8 tests\test_floodkit.py                    # 期望 31/31 passed
python -X utf8 tests\mutation_check.py                   # 期望 caught 15, survivors 0
python -X utf8 tests\rainfall_sweep.py                   # 期望 4/4 PASS
python -X utf8 -m floodkit samples --bounds 118.72 32.02 118.82 32.10 `
       --cn-source landcover --out "$ws\data\evidence\samples"   # 期望 8/8
python -X utf8 tests\check_doc_consistency.py            # 期望 documentation matches the reports
node agh-plugin\test-plugin.mjs                          # 期望 42/42（需能调起 python）
python -X utf8 scripts\make_story_figure.py                # 生成视频用四联图
```

**全部通过**说明作品可运行、可验证。任何一条不过，先看 [PLAN.md](PLAN.md) 的 bug 记录。

---

## 7.5 演示彩排（**录视频前必做**，20 秒）

在打开摄像头之前，先证明"智能体要走的路径"确实能产出文档里的数字：

```powershell
$ws = "C:\Users\14820\Desktop\江苏省ai+黑客松比赛"
$env:FLOODKIT_PYTHON = "<python 绝对路径>"
$env:FLOODKIT_ROOT = $ws
node "$ws\tests\rehearse-agent-path.mjs"
```

它**不调用模型**，而是用与智能体完全相同的参数直接驱动插件，逐项核对：

| 核对项 | 期望 |
|---|---|
| 研究区 | 76.3 km²（有效高程格面积） |
| 淹没面积 | 7.81 km² |
| 下穿隧道 | 117 / 420（生命危险 102，车行 44） |
| 关键设施 | 146 / 292（地铁口 80） |
| 道路 / 建筑 | 1722 / 4751 · 1131 / 12080 |
| D01 水量平衡 | ≈ 2.44e-16 |
| D04 敏感性 | 0.3004 |
| 报告校验 / 跨语言 | fatal=0 |
| **故障注入** | outcome=rejected，不产出栅格 |

**实测 23/23 通过**（缓存命中，含渲染交互页）。如果这里有任何一项 FAIL，
**先别录**——说明工具路径和文档已经不一致，录出来会前后矛盾。

> 这一步存在的理由：文档里的数字全部是用**命令行**手工跑出来的，
> 而"工具能不能产出这些数字"是另一个问题——它曾经是"不能"，
> 因为 `flood_run_event` 当时既不支持土地覆盖 CN，也不做承灾体分析。

## 8. 录演示视频（3–5 分钟，40 分钟）

分镜与旁白见 [DEMO_BRIEF.md](DEMO_BRIEF.md)。

**必须出现的内容**（评审要核）：

- [ ] **录制前已跑通彩排 23/23**（见 7.5）
- [ ] AGH 工作台界面 + 模型名（证明用 AGH + Agnes）
- [ ] 智能体自主调用工具的过程
- [ ] **四联故事图**（`data/runs/nanjing_final/nanjing_final_story.png`）——一张图讲完
      地形 → 下垫面 → 积水 → 风险点位，最适合做视频中段的定场画面
- [ ] 淹没图 + 关键点位图（`nanjing_final_map.png` / `nanjing_final_critical.png`）
- [ ] 五项指标里至少 D01 / D01b / D03 / D04
- [ ] 三类测试样例 8/8
- [ ] **诚实说明**：模型对官方易涝点清单 AUC 0.5193，测的是地形致涝而非管网致涝

> 最后一条别删。**敢说自己边界的作品，比只会说"我们全对"的作品可信得多。**

链接（B站/Youku 皆可）拿到后填进提交表。

---

## 8.5 答辩准备（建议提交后做）

现场问辩的准备见 [DEFENSE_QA.md](DEFENSE_QA.md)：13 个可能被问到的问题、
每题的一句话回答与支撑证据，以及一份**不要说的话**清单（过度声称是最容易失分的地方）。

## 9. 成员声明与公开内容（20 分钟）

1. **成员分工声明**：打开 [SUBMISSION_PACKET.md](SUBMISSION_PACKET.md)，
   把占位处替换成真实姓名、学校、年级、具体分工。
   **分工要写具体**（谁写的哪个模块、谁跑的哪组实验），不要写「共同完成」。
2. **公开内容**：至少 1 条。可选知乎/CSDN/B站专栏/公众号，
   内容草稿在 [SUBMISSION_PACKET.md](SUBMISSION_PACKET.md) 的「公开内容」一节。
   发布后**把链接填进提交表**，并截图留证。

---

## 10. 打包提交（10 分钟）

```powershell
$ws = "C:\Users\14820\Desktop\江苏省ai+黑客松比赛"
Set-Location $ws
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\package-submission.ps1 `
    -Group "本科生组" `
    -EntryNumber "U247" `
    -TeamName "宁讯智鉴" `
    -CaptainName "陈琪睿"
```

脚本会输出文件数与压缩后大小，并检查是否 ≤200MB。
产物在 `dist\` 下，命名格式为 `组别-参赛编号-队伍名称-队长姓名.zip`。

### 打包后必做：包体检查（10 秒）

```powershell
python -X utf8 tests\verify_package.py
```

它把刚打好的 ZIP **解压到临时目录**，从"评委打开压缩包"的视角复核：

| 检查 | 期望 |
|---|---|
| 排除项未泄漏 | `_agh_src`（约 1GB）、`_mutation`、`.pylibs`、`data/tmp` 等都不在包内 |
| 文档引用的证据都在包内 | 除 `data/evidence/agh/`（录制时生成）外无不一致 |
| 解压后解析校核 | 通过 |
| 解压后回归测试 | 31/31 |
| 解压后文档一致性 | matches the reports |

**为什么必须做这一步**：工作区里缓存、依赖、探索文件全都在，
但评委看不到这个目录——**他们只打开 ZIP**。如果打包漏了文件，
或者文档指向一个从没被包含进来的东西，**这个问题只在评委那里出现**。

**提交前最后核对**（对照比赛通知的必交项）：

- [ ] 基本信息（组别 / 编号 / 队名 / 成员）
- [ ] 技术信息（作品名、方向、技术方案）
- [ ] 可运行作品（ZIP 内代码 + 复现说明）
- [ ] 演示视频链接（3–5 分钟）
- [ ] 运行证据。**提交前这三个文件必须真实存在**（录制时生成，不是占位）：
      - [ ] `data/evidence/agh/host.jsonl` —— AGH 审计日志
      - [ ] `data/evidence/agh/flood_tool_calls.jsonl` —— 其中 `flood_` 相关行
      - [ ] 工作台轨迹视图截图（能看到模型名与工具调用）
      ```powershell
      Test-Path "$ws\data\evidence\agh\host.jsonl"            # 期望 True
      Test-Path "$ws\data\evidence\agh\flood_tool_calls.jsonl" # 期望 True
      ```
- [ ] 测试样例（正常 / 边界 / 失败，`data/evidence/samples/`）
- [ ] 成员声明
- [ ] 公开内容链接

---

## 11. 故障排查

| 现象 | 原因 | 对策 |
|---|---|---|
| 工作台打不开 | 服务没起来 | 看 `启动AGH.cmd` 窗口里的报错；确认 `_agh_src\agnes-harness-main\packages\cli\dist\local\agnes.mjs` 存在 |
| 设置里保存模型报错 | Key 或地址不对 | 地址必须是 `https://api.agnes-ai.cn/v1`（带 `/v1`） |
| 插件 inspect 报 `Expected union value` | 源路径格式不对 | 必须是 `file:./flood-tools` |
| 安装点了没反应 | AGH 跑在提权会话里 | 关掉，用普通窗口重新双击 `启动AGH.cmd` |
| 智能体说找不到工具 | 插件没启用 | 插件管理里确认 desired=enabled actual=running trusted=true |
| `flood_run_event` 卡住 | 首次要下载 DEM/WorldCover/OSM | 属正常。**实测冷启动 107 秒**（下载约 186 MB）；缓存命中则约 16 秒 |
| `node agh-plugin\test-plugin.mjs` 失败 | 子进程调不起 python | 设置 `$env:FLOODKIT_PYTHON` 为 python 绝对路径 |
| 打包报「受限目录」 | 有残留临时目录 | 删除 `data\tmp` 后重试 |

---

## 12. 如果时间不够，优先级排序

**必交且无法省略**（按重要性）：

1. 报名（过了死线就全都没了）
2. 可运行作品 ZIP（已经做好，打包即可）
3. AGH 执行记录（必须是 AGH + Agnes 真实跑出来的）
4. 三类测试样例（已经做好，`data/evidence/samples/`）
5. 演示视频链接
6. 成员声明 + 公开内容

**如果只剩 2 小时**：报名 → 打包 → 录一段 3 分钟的屏（哪怕不剪辑）→ 填表 → 提交。

**作品本身已经完成且验证过**（28 项回归测试、15 项变异测试、5 项指标），
剩下的全部是**流程性工作**。

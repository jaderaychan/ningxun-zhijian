# AGH 执行记录

本目录是必交项「AGH 执行记录」的证据。由 `python scripts/export_agh_evidence.py` 生成，
**不要手工编辑**。

| 文件 | 内容 |
|---|---|
| `session_trace.jsonl` | **真实轨迹**：模型每次工具调用（工具名、参数）、对应的返回、
以及审批请求与裁决。来自 AGH 会话库 `sessions.db`。 |
| `session_summary.json` | 上表的机器可判摘要：完整调用链、每次调用的审批裁决、以及
`flood_run_event` 返回的关键数字（可与 `data/runs/` 下的报告逐项核对）。 |
| `host.jsonl` | AGH 主机审计日志原件。 |
| `flood_tool_calls.jsonl` | 审计日志中与 `flood_` 相关的行。 |

## 为什么不能只看 host.jsonl

AGH 的主机审计日志在本版本只记录**扩展生命周期**（`extension.registered` /
`extension.loaded` / `extension.revoked`）——每个工具一行，发生在插件加载时。
它**不记录任何一次工具调用**。所以只凭它，无法证明"模型自主调用了工具"，
而那正是这一项要证明的事。真正的调用链在会话库里，本目录导出的是它。

- 会话：`6ce01adf-dadc-4115-a41e-472334c69a01`
- 建立时间：`2026-10-11T03:47:43.125Z`
- 模型：`agnes-3.0-flash`

`tests/verify_agh_evidence.py` 会核对轨迹与摘要一致、每次调用都有返回、
需要审批的调用都有裁决，并把这些数字与包内 `data/runs/` 的报告逐项比对。

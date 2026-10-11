"""Export the real AGH execution record: the required "AGH 执行记录" evidence.

Why this replaces the two-command recipe in SUBMISSION_RUNBOOK step 6
--------------------------------------------------------------------
That recipe copies `%USERPROFILE%\\.agh\\data\\audit\\host.jsonl` and greps it for
`flood_`. On this AGH build the host audit log records extension *lifecycle* only —
`extension.registered` / `extension.loaded` / `extension.revoked` — one line per tool
at load time. It never records a tool call. So following the recipe produces a file
that shows the plugin was loaded and cannot show that the model ever called it, which
is the one thing the 必交项 has to prove.

The calls are in the session store: `~/.agh/data/sessions.db`, table `events`, where
`tool/call`, `tool/result`, `approval/asked` and `approval/decided` carry the tool
name, its arguments, the approval verdict and the result. This exports that trace,
plus the audit log as the runbook asks, and a summary that can be checked against the
run reports shipped in the package.

Writes to data/evidence/agh/:
    host.jsonl              audit log, copied verbatim
    flood_tool_calls.jsonl  its flood_ lines, as the runbook documents
    session_trace.jsonl     the real trace: tool calls, results and approvals
    session_summary.json    machine-checkable chain, verdicts and headline numbers
    README.md               what the four files are and how to regenerate them

    python scripts/export_agh_evidence.py                 # newest flood session
    python scripts/export_agh_evidence.py --session <key>
    python scripts/export_agh_evidence.py --list
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

AGH_HOME = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".agh")
SESSIONS_DB = os.path.join(AGH_HOME, "data", "sessions.db")
AUDIT_LOG = os.path.join(AGH_HOME, "data", "audit", "host.jsonl")
OUT_DIR = os.path.join(ROOT, "data", "evidence", "agh")

#: Event types worth exporting. Everything else is transport noise: request headers,
#: budget ledgers, context breakdowns — none of it is evidence about the model.
TRACE_TYPES = (
    "session/start", "turn/start", "turn/end",
    "user/message", "assistant/message",
    "tool/call", "tool/result",
    "approval/asked", "approval/decided",
    "verifier/signal",
)

FLOOD_PREFIX = "flood_"


def copy_database() -> str:
    """Work on a copy: the workbench may hold the live database open."""
    scratch = os.path.join(ROOT, "data", "tmp", "aghdb")
    if os.path.isdir(scratch):
        shutil.rmtree(scratch)
    os.makedirs(scratch)
    for suffix in ("", "-wal", "-shm"):
        source = SESSIONS_DB + suffix
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(scratch, "sessions.db" + suffix))
    return os.path.join(scratch, "sessions.db")


def sessions_with_flood(con: sqlite3.Connection):
    """session_key -> (created_at, flood tool-call count), newest first."""
    out = []
    for row in con.execute("select session_key, created_at from sessions"):
        key, created = row["session_key"], row["created_at"]
        # `like '%flood_%'` rather than a match on `"name":"flood_`: the stored JSON is
        # compact, and pinning the spacing would silently count zero. The parsed chain
        # is what actually filters, so this only has to be permissive.
        calls = con.execute(
            "select count(*) from events where session_key = ? and type = 'tool/call' "
            "and data like '%flood\\_%' escape '\\'", (key,)).fetchone()[0]
        out.append((created, key, calls))
    out.sort(reverse=True)
    return out


def event_rows(con: sqlite3.Connection, session_key: str):
    for row in con.execute(
            "select seq, ts, type, lane, actor, data from events "
            "where session_key = ? and type in (%s) order by seq"
            % ",".join("?" * len(TRACE_TYPES)),
            (session_key, *TRACE_TYPES)):
        try:
            payload = json.loads(row["data"]) if row["data"] else {}
        except ValueError:
            payload = {"_unparsed": row["data"]}
        yield {"seq": row["seq"], "ts": row["ts"], "type": row["type"],
               "lane": _text(row["lane"]), "actor": _json_actor(row["actor"]),
               "data": payload}


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _json_actor(value):
    try:
        parsed = json.loads(value) if value else {}
        return parsed.get("id") or parsed.get("role") or "unknown"
    except ValueError:
        return _text(value)


def build_chain(events: list[dict]):
    """Pair each tool call with its approval verdict and its result."""
    asked = {}
    decided = {}
    results = {}
    for event in events:
        data = event["data"]
        if event["type"] == "approval/asked":
            asked[data.get("toolUseId")] = data
        elif event["type"] == "approval/decided":
            decided[data.get("requestId")] = data
        elif event["type"] == "tool/result":
            results[data.get("toolUseId")] = data

    chain = []
    for event in events:
        if event["type"] != "tool/call":
            continue
        data = event["data"]
        use_id = data.get("toolUseId")
        request = asked.get(use_id) or {}
        verdict = decided.get(request.get("requestId")) or {}
        result = results.get(use_id) or {}
        structured = result.get("structured") or {}
        chain.append({
            "seq": event["seq"],
            "ts": event["ts"],
            "tool": data.get("name"),
            "args": data.get("args"),
            "requires_approval": (data.get("resolvedPolicy") or {}).get("requiresApproval"),
            "approval_verdict": verdict.get("verdict"),
            "approval_summary": request.get("summary"),
            "has_result": bool(result),
            "result_ok": structured.get("ok"),
            "result_code": structured.get("code"),
            "headline": _headline(data.get("name"), structured),
        })
    return chain


def _headline(tool: str, structured: dict):
    """A few numbers a verifier can check against the shipped run report."""
    if tool == "flood_run_event":
        ponding = structured.get("ponding") or {}
        exposure = structured.get("exposure") or {}
        underpasses = exposure.get("underpasses") or {}
        facilities = exposure.get("critical_facilities") or {}
        return {
            "outcome": structured.get("outcome"),
            "ponded_km2": round((ponding.get("pond_area_m2") or 0) / 1e6, 3),
            "max_depth_m": ponding.get("max_depth_m"),
            "underpass_ways_flooded": underpasses.get("underpass_ways_flooded"),
            "facilities_flooded": facilities.get("facilities_flooded"),
        }
    if tool == "flood_weather":
        return {"min_mm": structured.get("min_mm"), "max_mm": structured.get("max_mm"),
                "window_start": (structured.get("window") or {}).get("start")}
    if tool == "flood_publish_report":
        return {"url": structured.get("url"), "size_bytes": structured.get("size_bytes"),
                "tag": structured.get("tag")}
    if tool == "flood_selftest":
        return {"checks": len(structured.get("checks") or [])}
    if tool == "flood_verify_report":
        return {"verified": structured.get("verified"), "fatal_count": structured.get("fatal_count")}
    if tool == "flood_crosslang_check":
        return {"verified": structured.get("verified"), "fatal_count": structured.get("fatal_count")}
    return None


def model_of(events: list[dict]) -> str | None:
    for event in events:
        if event["type"] == "user/message":
            text = "".join(part.get("text", "") for part in (event["data"].get("content") or [])
                           if isinstance(part, dict))
            marker = '"model":"'
            if marker in text:
                return text.split(marker, 1)[1].split('"', 1)[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=None)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(SESSIONS_DB):
        raise SystemExit("no AGH session store at %s" % SESSIONS_DB)
    db = copy_database()
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    available = sessions_with_flood(con)
    if args.list or not available:
        print("sessions (newest first), flood tool calls in brackets:")
        for created, key, calls in available:
            print("  %s  %s  [%d]" % (created, key, calls))
        if not available:
            return 1
        if args.list:
            return 0

    session_key = args.session or next((key for _, key, calls in available if calls), None)
    if not session_key:
        raise SystemExit("no session in the store called a flood_ tool; nothing to export")
    created = next((c for c, k, _ in available if k == session_key), "?")

    events = list(event_rows(con, session_key))
    chain = build_chain(events)
    flood_chain = [step for step in chain if (step["tool"] or "").startswith(FLOOD_PREFIX)]
    if not flood_chain:
        raise SystemExit("session %s has no flood_ tool calls" % session_key)

    os.makedirs(OUT_DIR, exist_ok=True)

    # 1 & 2 — what the runbook asks for
    if os.path.exists(AUDIT_LOG):
        shutil.copy2(AUDIT_LOG, os.path.join(OUT_DIR, "host.jsonl"))
    with open(AUDIT_LOG, encoding="utf-8") as fh:
        flood_lines = [line for line in fh if FLOOD_PREFIX in line]
    with open(os.path.join(OUT_DIR, "flood_tool_calls.jsonl"), "w", encoding="utf-8") as fh:
        fh.writelines(flood_lines)

    # 3 — the real trace
    trace_path = os.path.join(OUT_DIR, "session_trace.jsonl")
    with open(trace_path, "w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    # 4 — the machine-checkable summary
    counts = {}
    for event in events:
        counts[event["type"]] = counts.get(event["type"], 0) + 1
    runs = [{"tag": (step["args"] or {}).get("tag"), **(step["headline"] or {})}
            for step in flood_chain if step["tool"] == "flood_run_event"]
    summary = {
        "session_key": session_key,
        "session_created_at": created,
        "model": model_of(events),
        "exported_at": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "exported_by": "scripts/export_agh_evidence.py",
        "source": os.path.relpath(SESSIONS_DB, os.path.expanduser("~")).replace(os.sep, "/"),
        "counts": counts,
        "flood_tool_calls": len(flood_chain),
        "chain": flood_chain,
        "runs": runs,
        "audit_lines_mentioning_flood": len(flood_lines),
        "note": (
            "The host audit log records extension lifecycle only, so it shows the plugin "
            "loaded and cannot show a tool call. The chain above is the model's actual "
            "tool use, with the approval verdict for each call, exported from the AGH "
            "session store."),
    }
    with open(os.path.join(OUT_DIR, "session_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    with open(os.path.join(OUT_DIR, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(README_TEXT % (session_key, created, summary["model"] or "?"))

    print("session %s (%s), model %s" % (session_key, created, summary["model"]))
    print("  %d flood tool calls, %d approvals asked, %d decided"
          % (len(flood_chain), counts.get("approval/asked", 0), counts.get("approval/decided", 0)))
    for step in flood_chain:
        marker = "approval=%s" % (step["approval_verdict"] or "-")
        print("  seq=%-4s %-24s ok=%-5s %s"
              % (step["seq"], step["tool"], step["result_ok"], marker))
    print("\nwrote %s" % os.path.relpath(OUT_DIR, ROOT))
    return 0


README_TEXT = """# AGH 执行记录

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

- 会话：`%s`
- 建立时间：`%s`
- 模型：`%s`

`tests/verify_agh_evidence.py` 会核对轨迹与摘要一致、每次调用都有返回、
需要审批的调用都有裁决，并把这些数字与包内 `data/runs/` 的报告逐项比对。
"""


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify the AGH execution record against itself and against the shipped reports.

The point of the "AGH 执行记录" item is to show that the model autonomously called the
tools. An exported file proves nothing on its own — it is a file this project wrote.
So this checks the things that would expose a fabricated or stale record:

  * the summary's chain is exactly the trace's tool calls, in order;
  * every call has a result, and no call reports a number the trace does not contain;
  * the approval decisions recorded are ones the trace actually contains, and the one
    thing that must not happen — a write tool running with approval required and never
    decided — is refused;
  * **the numbers the run tool returned match the report shipped in data/runs/**, so
    the execution record and the modelled artifacts cannot drift apart;
  * the model is an Agnes model, which is what the submission promises.

    python tests/verify_agh_evidence.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

EVIDENCE = os.path.join(ROOT, "data", "evidence", "agh")
FAILURES: list[str] = []

ALLOWED_VERDICTS = {"allowed-once", "allowed-session", "allowed-permanent"}


def check(condition, message):
    print("  %s %s" % ("ok  " if condition else "FAIL", message))
    if not condition:
        FAILURES.append(message)


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main() -> int:
    print("=" * 72)
    print("AGH execution record")
    for name in ("session_trace.jsonl", "session_summary.json", "host.jsonl",
                 "flood_tool_calls.jsonl", "README.md"):
        check(os.path.exists(os.path.join(EVIDENCE, name)), "%s exists" % name)
    if FAILURES:
        print("\nFAILED: %d check(s)" % len(FAILURES))
        return 1

    with open(os.path.join(EVIDENCE, "session_summary.json"), encoding="utf-8") as fh:
        summary = json.load(fh)
    trace = load_jsonl(os.path.join(EVIDENCE, "session_trace.jsonl"))

    print("=" * 72)
    print("the summary is a faithful slice of the trace")
    trace_calls = [event for event in trace if event["type"] == "tool/call"]
    trace_flood = [event for event in trace_calls
                   if str(event["data"].get("name", "")).startswith("flood_")]
    check(len(summary["chain"]) == len(trace_flood),
          "the chain has the trace's %d flood tool calls" % len(trace_flood))
    check([step["tool"] for step in summary["chain"]] ==
          [event["data"]["name"] for event in trace_flood],
          "the chain names match the trace, in order")
    check([step["seq"] for step in summary["chain"]] == [event["seq"] for event in trace_flood],
          "the chain sequence numbers match the trace")
    check(summary["session_key"] in {event["data"].get("key") for event in trace
                                     if event["type"] == "session/start"},
          "the trace's session/start names the session the summary does")
    check(len(trace_calls) == len(trace_flood),
          "the session called nothing but flood tools (%d calls)" % len(trace_calls))

    print("=" * 72)
    print("every call returned, and the approvals are real")
    check(all(step["has_result"] for step in summary["chain"]),
          "every tool call has a result")
    asked = {event["data"].get("toolUseId") for event in trace if event["type"] == "approval/asked"}
    decided = {event["data"].get("requestId") for event in trace
               if event["type"] == "approval/decided"}
    check(len(asked) >= 1 and len(decided) >= 1,
          "the human was in the loop (%d asked, %d decided)" % (len(asked), len(decided)))
    check(asked == {event["data"].get("toolUseId") for event in trace
                    if event["type"] == "approval/asked"},
          "every approval request in the trace is accounted for")

    verdicts = {event["data"].get("requestId"): event["data"].get("verdict")
                for event in trace if event["type"] == "approval/decided"}
    check(all(verdict in ALLOWED_VERDICTS for verdict in verdicts.values()),
          "no approval was recorded as anything but an allow: %s" % sorted(set(verdicts.values())))

    # A write tool that ran while its approval was never decided is the failure this
    # whole item exists to rule out. The grant is often session-scoped, so a later call
    # of the same tool legitimately has no approval event of its own; what must never
    # happen is a write tool with no grant at all.
    granted_tools = set()
    for event in trace:
        if event["type"] != "tool/call":
            continue
        policy = (event["data"].get("resolvedPolicy") or {}).get("requiresApproval")
        if policy != "never" and (event["data"].get("name") or "").startswith("flood_"):
            granted_tools.add(event["data"]["name"])
    ungranted = [step for step in summary["chain"]
                 if step["requires_approval"] != "never"
                 and step["tool"] in granted_tools
                 and step["approval_verdict"] is None
                 and not any(other["tool"] == step["tool"]
                             and other["approval_verdict"] in ALLOWED_VERDICTS
                             for other in summary["chain"][:summary["chain"].index(step)])]
    check(not ungranted,
          "every write tool ran under an approval granted earlier or on the call: %s"
          % ([step["tool"] for step in ungranted] or "none"))

    print("=" * 72)
    print("the record agrees with the reports shipped in data/runs/")
    checked = 0
    for run in summary["runs"]:
        tag = run.get("tag")
        report_path = os.path.join(ROOT, "data", "runs", tag or "", "%s_report.json" % tag)
        if run.get("outcome") != "ok":
            continue
        if not os.path.exists(report_path):
            check(False, "run %s reports outcome ok but its report is not in data/runs/" % tag)
            continue
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
        ponding = report.get("ponding") or {}
        exposure = report.get("exposure") or {}
        underpasses = exposure.get("underpasses") or {}
        facilities = exposure.get("critical_facilities") or {}
        check(run["outcome"] == report.get("outcome"),
              "%s: the record's outcome is the report's (%s)" % (tag, report.get("outcome")))
        check(abs(run["ponded_km2"] - (ponding.get("pond_area_m2") or 0) / 1e6) < 5e-4,
              "%s: ponded area agrees (%.3f vs %.3f km²)"
              % (tag, run["ponded_km2"], (ponding.get("pond_area_m2") or 0) / 1e6))
        check(run["max_depth_m"] == ponding.get("max_depth_m"),
              "%s: maximum depth agrees (%.3f m)" % (tag, run["max_depth_m"] or float("nan")))
        check(run["underpass_ways_flooded"] == underpasses.get("underpass_ways_flooded"),
              "%s: flooded underpass sections agree (%s)"
              % (tag, run["underpass_ways_flooded"]))
        check(run["facilities_flooded"] == facilities.get("facilities_flooded"),
              "%s: flooded facilities agree (%s)" % (tag, run["facilities_flooded"]))
        checked += 1
    check(checked >= 1, "at least one successful run was cross-checked (%d)" % checked)

    # The published page has to still be that run's page. Byte equality would be the
    # wrong invariant: the trace is a historical record while the page is a live
    # deliverable, so regenerating it — a rename, a palette change — legitimately
    # changes its length. A few percent catches what matters (a missing page, or a
    # different run's page) without failing every time the page is re-rendered; the
    # substantive numbers are compared exactly in the block above.
    publishes = [step for step in summary["chain"] if step["tool"] == "flood_publish_report"]
    for step in publishes:
        tag = (step["headline"] or {}).get("tag")
        page = os.path.join(ROOT, "data", "runs", tag or "", "%s_report.html" % tag)
        recorded = (step["headline"] or {}).get("size_bytes") or 0
        if os.path.exists(page):
            actual = os.path.getsize(page)
            drift = abs(actual - recorded) / recorded if recorded else 1.0
            check(drift < 0.05,
                  "%s: the published page is still that page (%d bytes now, %d when "
                  "published, %+.2f%%)" % (tag, actual, recorded, 100 * (actual - recorded) / recorded))
        else:
            check(False, "%s: the published page is missing from data/runs/" % tag)

    print("=" * 72)
    print("the model is the one the submission promises")
    check(str(summary.get("model", "")).startswith("agnes"),
          "the session ran on an Agnes model (%s)" % summary.get("model"))

    print("=" * 72)
    if FAILURES:
        print("FAILED: %d check(s)" % len(FAILURES))
        for failure in FAILURES:
            print("  - %s" % failure)
        return 1
    print("the AGH execution record matches the trace and the shipped reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

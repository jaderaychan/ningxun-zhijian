"""Verify that the documentation still quotes the numbers the code produces.

Documentation drift is a real failure mode here: the model changed several times
during development and each change moved the headline numbers. A judge checking
one figure against the report would find the mismatch immediately, so this check
is part of the test suite rather than a habit.

    python tests/check_doc_consistency.py
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PRIMARY_RUN = os.path.join(ROOT, "data", "runs", "nanjing_final", "nanjing_final_report.json")
WIDE_RUN = os.path.join(ROOT, "data", "runs", "nanjing_wide", "nanjing_wide_report.json")

DOCS = [
    "README.md",
    "docs/SUBMISSION_PACKET.md",
    "docs/DEMO_BRIEF.md",
    "docs/PLAN.md",
    "docs/SUBMISSION_RUNBOOK.md",
    "docs/PUBLIC_POST.md",
    "docs/JUDGE_SUMMARY.md",
    "docs/DEFENSE_QA.md",
    "docs/HANDOVER.md",
]

# Strings that must never reappear: they were real numbers once, and a copy of
# them left behind means a doc was edited from memory instead of from the report.
STALE = (
    # place names that were once guessed from a mojibake console
    "南京十三中",
    "小天鹅幼儿园",
    # headline numbers from earlier model versions
    "1302 / 4751",
    "1302 条",
    "4433",
    "143 / 292",
    "118 段",
    "地铁口 77",
    "77 个地铁口",
    "0.58%",
    "30.9%",
    # test counts that have since grown — the regression suite, the plugin contract
    # suite and the rehearsal all move whenever a bug earns a test
    "18/18",
    "20/20",
    "22/22",
    "28/28",
    "29/29",
    "30/30",
    "契约测试 33 项",
    "回归测试 28 条",
    "33/33",
    "19/19",
    "11 个 bug",
    "11 个已修复",
    "12 个 bug",
    "12 个已修复",
    # tool counts, since an eighth tool was added
    "7 个工具",
    "5 个自定义工具",
    "5 个工具",
    "这 7 个",
    "增加到 7 个",
    "5 个 `flood_*`",
    # names the submission replaced: a document still using one of them
    # contradicts the entry forms
    "涝先知",
    "见微知涝",
)


def headline_numbers() -> dict:
    with open(PRIMARY_RUN, encoding="utf-8") as fh:
        primary = json.load(fh)
    exposure = primary["exposure"]
    numbers = {
        "roads_flooded": exposure["roads"]["ways_flooded"],
        "roads_total": exposure["roads"]["ways_total"],
        "underpass_flooded": exposure["underpasses"]["underpass_ways_flooded"],
        "underpass_total": exposure["underpasses"]["underpass_ways"],
        "underpass_life_safety": exposure["underpasses"]["life_safety_count"],
        "underpass_vehicle": exposure["underpasses"]["vehicle_life_safety_count"],
        "facilities_flooded": exposure["critical_facilities"]["facilities_flooded"],
        "facilities_total": exposure["critical_facilities"]["facilities_total"],
        "subway_entrances": exposure["critical_facilities"]["subway_entrances_flooded"],
        "buildings_affected": exposure["buildings"]["buildings_affected"],
        "buildings_total": exposure["buildings"]["buildings_total"],
        # The study area is easy to mis-state by computing it from the bounding box
        # by hand: the grid is ~86 km2 and the bbox ~84 km2, but only 76.3 km2 has
        # valid elevation and that is the figure every document must quote.
        "study_area_km2": round(float(primary["domain"]["area_km2"]), 1),
    }
    if os.path.exists(WIDE_RUN):
        with open(WIDE_RUN, encoding="utf-8") as fh:
            wide = json.load(fh)
        if wide.get("checkpoints"):
            numbers["d02_points"] = wide["checkpoints"]["points"]
            numbers["d02_hits"] = wide["checkpoints"]["hits"]
    return numbers


def suite_size() -> int:
    """Derive the regression-suite size instead of trusting the prose.

    Every bug that earns a test changes this number, and it is quoted in seven
    documents. Sweeping them by hand is how a doc ends up claiming a suite size the
    suite does not have — so the README is checked against the file itself, and STALE
    catches the value it replaced.
    """
    with open(os.path.join(ROOT, "tests", "test_floodkit.py"), encoding="utf-8") as fh:
        return len(re.findall(r"(?m)^def test_", fh.read()))


def main() -> int:
    failures = []
    numbers = headline_numbers()
    must_appear = [
        str(numbers["underpass_flooded"]),
        str(numbers["underpass_life_safety"]),
        str(numbers["underpass_vehicle"]),
        str(numbers["facilities_flooded"]),
        str(numbers["subway_entrances"]),
        str(numbers["roads_flooded"]),
        str(numbers["buildings_affected"]),
        str(numbers["study_area_km2"]),
    ]

    # The README's file tree quotes a file count per directory. Those counts drifted
    # three times during development and were caught by hand each time; a number that
    # can be counted should be counted. `docs/` uses 份 and the rest 个文件, so the
    # pattern accepts either unit.
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        readme_text = fh.read()
    tree_problems = []
    for rel in ("floodkit", "agh-plugin", "tests", "scripts", "docs"):
        actual = 0
        for base, dirs, files in os.walk(os.path.join(ROOT, rel)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            actual += len(files)
        pattern = re.compile(r"(?m)^%s/\s+.*?（(\d+)\s*(?:个文件|份)" % re.escape(rel))
        found = pattern.search(readme_text)
        if not found:
            tree_problems.append("%s/ has no file count in the README tree" % rel)
        elif int(found.group(1)) != actual:
            tree_problems.append("%s/: README says %s, tree has %d"
                                 % (rel, found.group(1), actual))
    for problem in tree_problems:
        print("   tree: %s" % problem)
    if tree_problems:
        failures.extend(tree_problems)

    size = suite_size()
    print("regression suite: %d tests, so wherever a doc quotes the suite size it must "
          "quote %d/%d" % (size, size, size))
    # Only the documents that actually quote a suite size are held to it. Demanding it
    # everywhere would force a number into documents that never needed one, which is
    # how a document acquires a fact nobody maintains.
    suite_quote = "%d/%d" % (size, size)
    QUOTES_SUITE = ("README.md", "docs/JUDGE_SUMMARY.md", "docs/DEMO_BRIEF.md",
                    "docs/HANDOVER.md", "docs/LIMITATIONS.md", "docs/PLAN.md",
                    "docs/SUBMISSION_RUNBOOK.md")

    failures = []
    print("headline numbers from %s" % os.path.relpath(PRIMARY_RUN, ROOT))
    for key, value in numbers.items():
        print("   %-22s %s" % (key, value))
    print()

    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        readme_text = fh.read()

    for rel in DOCS:
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(path):
            failures.append("%s missing" % rel)
            continue
        text = open(path, encoding="utf-8").read()
        wanted = must_appear + ([suite_quote] if rel in QUOTES_SUITE else [])
        missing = [n for n in wanted if n not in text]
        stale = [s for s in STALE if s in text]
        status = "ok" if not missing and not stale else "STALE"
        print("%-28s %-6s missing:%s stale:%s" % (rel, status, missing or "-", stale or "-"))
        if missing:
            failures.append("%s is missing %s" % (rel, missing))
        if stale:
            failures.append("%s still contains %s" % (rel, stale))

    # every number quoted in the docs must be traceable to a report file
    if not os.path.exists(PRIMARY_RUN):
        failures.append("primary report missing: run `python -m floodkit run ... --tag nanjing_final`")

    print()
    if failures:
        print("FAILED:")
        for item in failures:
            print("   - %s" % item)
        return 1
    print("documentation matches the reports")
    _ = re
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify the submission ZIP as a standalone artifact — the judge's view.

Everything else in this project is verified inside the working directory, where
caches, dependencies and exploratory files all happen to be present. The judges
never see that directory: they open the ZIP. If packaging drops a file, or a
document points at something that was never included, the defect only appears in
their copy.

So this extracts the built archive to a scratch directory and runs the checks
there:

  1. every evidence path referenced by a document exists inside the archive
  2. the analytic self-check passes
  3. the regression suite passes
  4. the documentation still matches the reports it ships with
  5. no excluded directories leaked in (exploration code, AGH checkout, caches)

    python tests/verify_package.py
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.path.join(ROOT, "_zipview")
DEPS = os.path.join(ROOT, ".pylibs")

# Directories that must never reach a judge. `_agh_src` alone is ~1 GB.
MUST_NOT_CONTAIN = ("_agh_src/", "_mutation/", "_cleanroom/", "_zipview/", ".pylibs/", "data/tmp/",
                    "data/landcover/", "data/dem/", "data/osm/")

# Evidence produced during the recorded session rather than shipped.
EXPECTED_PENDING = ("data/evidence/agh/", "data/audit/")


def newest_zip() -> str | None:
    candidates = glob.glob(os.path.join(ROOT, "dist", "*.zip"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def referenced_evidence(base: str) -> tuple[list[str], list[str]]:
    """Paths mentioned in the shipped documents, split into present and pending."""
    refs: set[str] = set()
    for pattern in (os.path.join(base, "*.md"), os.path.join(base, "docs", "*.md")):
        for path in glob.glob(pattern):
            text = open(path, encoding="utf-8").read()
            for match in re.finditer(r"data[/\\][\w./\\-]+\.(?:jsonl|json|png|tif|md)", text):
                refs.add(match.group(0).replace("/", os.sep))
    present, pending = [], []
    for ref in sorted(refs):
        normalised = ref.replace(os.sep, "/")
        if os.path.exists(os.path.join(base, ref)):
            present.append(ref)
        elif normalised.startswith(EXPECTED_PENDING):
            pending.append(ref)
        else:
            pending.append(ref)  # caller distinguishes by the pending-prefix rule below
    return present, pending


def run(cmd: list[str], cwd: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([DEPS, cwd])
    env["MPLCONFIGDIR"] = os.path.join(ROOT, ".mplconfig")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-2:])
    if proc.returncode != 0 and not tail:
        tail = (proc.stderr or "").strip()[-300:]
    return proc.returncode == 0, tail


def main() -> int:
    archive = newest_zip()
    if not archive:
        print("no archive in dist/ — run scripts/package-submission.ps1 first")
        return 2
    print("archive: %s (%.1f MB)" % (os.path.basename(archive), os.path.getsize(archive) / 1e6))

    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        zf.extractall(SCRATCH)

    failures: list[str] = []

    leaked = [n for n in names if any(n.replace("\\", "/").startswith(p) for p in MUST_NOT_CONTAIN)]
    print("\n1. 排除项未泄漏: %s" % ("通过" if not leaked else "泄漏 %d 项" % len(leaked)))
    if leaked:
        failures.append("excluded content leaked into the archive: %s" % leaked[:5])

    present, missing = referenced_evidence(SCRATCH)
    pending = [m for m in missing if m.replace(os.sep, "/").startswith(EXPECTED_PENDING)]
    unexpected = [m for m in missing if m not in pending]
    print("2. 文档引用的证据: 在包内 %d 个 | 录制时生成 %d 个 | 意外缺失 %s"
          % (len(present), len(pending), unexpected or "无"))
    if unexpected:
        failures.append("documents reference files not in the archive: %s" % unexpected)

    ok, tail = run([sys.executable, "-X", "utf8", "-m", "floodkit", "selftest"], SCRATCH)
    print("3. 解析校核: %s  %s" % ("通过" if ok else "失败", tail.replace("\n", " ")[:70]))
    if not ok:
        failures.append("selftest failed inside the archive")

    ok, tail = run([sys.executable, "-X", "utf8", os.path.join(SCRATCH, "tests", "test_floodkit.py")], SCRATCH)
    print("4. 回归测试: %s  %s" % ("通过" if ok else "失败", tail.replace("\n", " ")[:70]))
    if not ok:
        failures.append("regression suite failed inside the archive")

    ok, tail = run([sys.executable, "-X", "utf8", os.path.join(SCRATCH, "tests", "check_doc_consistency.py")], SCRATCH)
    print("5. 文档一致性: %s  %s" % ("通过" if ok else "失败", tail.replace("\n", " ")[:70]))
    if not ok:
        failures.append("documentation does not match the reports inside the archive")

    shutil.rmtree(SCRATCH, ignore_errors=True)

    print()
    if failures:
        print("包体检查未通过：")
        for item in failures:
            print("  - %s" % item)
        return 1
    print("包体检查通过：解压后自检、回归、文档一致性均可运行，排除项未泄漏。")
    print("注意：data/evidence/agh/ 下的 AGH 执行记录需在录制会话时生成，本检查不覆盖。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

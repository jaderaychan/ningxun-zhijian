"""Verify the two evidence figures that carry the project's hardest claims.

`rainfall_animation.*` and `negative_result.*` are not decoration: the first is the
evidence that the spill branch runs at all, the second is the published negative
result. Both are checked against the JSON reports they were drawn from, because a
figure that disagrees with the file beside it is the failure this whole project is
organised against.

What is checked:

* the rainfall sidecar's numbers equal the independently produced
  `rainfall_sweep.json`, the ponded area is monotone in rainfall, and the spill
  branch is exercised at the top of the range but not at 100 mm;
* the GIF and MP4 exist, are non-trivial, and the GIF declares the expected number
  of frames;
* the negative-result sidecar's hit rate and AUC equal the run report's, and the
  AUC integrated from the plotted ROC equals the AUC computed by rank statistics —
  two independent computations of one number.

    python tests/verify_evidence_figures.py
"""

from __future__ import annotations

import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

FAILURES: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        FAILURES.append(msg)


def gif_frames(path: str) -> int:
    """Count frames by walking the GIF's frame list.

    Not by counting 0x2c bytes in the file: that separator also occurs inside the
    LZW-compressed image data, so byte counting reported 750 frames for an 8-frame
    animation and the check passed while verifying nothing.
    """
    from PIL import Image

    with Image.open(path) as image:
        count = 1
        try:
            while True:
                image.seek(image.tell() + 1)
                count += 1
        except EOFError:
            pass
        return count


def png_size(path: str):
    with open(path, "rb") as fh:
        head = fh.read(24)
    return struct.unpack(">II", head[16:24]) if head[:8] == b"\x89PNG\r\n\x1a\n" else None


def main() -> int:
    evidence = os.path.join(ROOT, "data", "evidence")

    # ---------------------------------------------------------------- rainfall
    print("=" * 72)
    print("rainfall sweep animation")
    sweep_path = os.path.join(evidence, "rainfall_sweep.json")
    sidecar_path = os.path.join(evidence, "rainfall_animation.json")
    check(os.path.exists(sweep_path), "the independent sweep record exists")
    check(os.path.exists(sidecar_path), "the animation sidecar exists")
    if os.path.exists(sweep_path) and os.path.exists(sidecar_path):
        with open(sweep_path, encoding="utf-8") as fh:
            sweep = json.load(fh)
        with open(sidecar_path, encoding="utf-8") as fh:
            sidecar = json.load(fh)
        recorded = {float(s["rain_mm"]): s for s in sweep["scenarios"]}
        drawn = {float(s["rain_mm"]): s for s in sidecar["scenarios"]}

        check(set(recorded) == set(drawn),
              "the same rainfall levels appear in both (%s)" % sorted(drawn))
        worst = []
        for rain in sorted(set(recorded) & set(drawn)):
            for key in ("ponded_km2", "max_depth_m", "overflowing_basins"):
                a, b = float(drawn[rain][key]), float(recorded[rain][key])
                if abs(a - b) > 1e-6:
                    worst.append((rain, key, a, b))
        check(not worst, "every drawn number equals the independent sweep (diff: %s)"
              % (worst[:3] or "none"))

        levels = [drawn[r]["ponded_km2"] for r in sorted(drawn)]
        check(all(levels[i] <= levels[i + 1] + 1e-9 for i in range(len(levels) - 1)),
              "ponded area is monotone in rainfall (%s)"
              % " -> ".join("%.2f" % v for v in levels))
        low = drawn.get(100.0, {})
        check(low.get("spilled_m3", 0.0) == 0.0,
              "the flagship 100 mm scenario spills nothing")
        top = drawn[max(drawn)]
        check(top["spilled_m3"] > 0 and top["overflowing_basins"] > 0,
              "the top of the range exercises the spill branch (%s m3 over %d basins)"
              % ("%.0f" % top["spilled_m3"], top["overflowing_basins"]))
        check(sidecar.get("checked_against"),
              "the sidecar records what its numbers were checked against")

    gif = os.path.join(evidence, "rainfall_animation.gif")
    check(os.path.exists(gif), "the GIF exists")
    if os.path.exists(gif):
        frames = gif_frames(gif)
        check(frames == len(drawn),
              "the GIF carries %d frames, one per scenario" % frames)
        check(os.path.getsize(gif) > 50_000,
              "the GIF is %.2f MB, not a blank animation" % (os.path.getsize(gif) / 1e6))
        # The hold on the last frame is a per-frame duration, and Pillow will merge
        # repeated identical frames — so this is the only way to know the 300 mm
        # state is actually readable before the loop restarts.
        from PIL import Image

        with Image.open(gif) as image:
            durations = []
            for index in range(frames):
                image.seek(index)
                durations.append(image.info.get("duration", 0))
        check(len(durations) == frames and durations[-1] > 2 * max(durations[:-1] or [1]),
              "the last frame is held %.1f s against %.1f s for the others"
              % (durations[-1] / 1000, max(durations[:-1] or [0]) / 1000))

    mp4 = os.path.join(evidence, "rainfall_animation.mp4")
    if os.path.exists(mp4):
        with open(mp4, "rb") as fh:
            head = fh.read(16)
        check(b"ftyp" in head, "the MP4 has a valid file-type box")
        check(os.path.getsize(mp4) > 50_000,
              "the MP4 is %.2f MB" % (os.path.getsize(mp4) / 1e6))
    else:
        print("  --   no MP4 (ffmpeg not on PATH at build time); the GIF covers it")

    panels = os.path.join(evidence, "rainfall_smallmultiples.png")
    check(os.path.exists(panels), "the small-multiples PNG exists")
    if os.path.exists(panels):
        size = png_size(panels)
        check(size is not None and size[0] > 1200,
              "the panels are %s px wide" % (size[0] if size else "?"))

    # ---------------------------------------------------------- negative result
    print("=" * 72)
    print("negative result figure")
    tag = "nanjing_wide"
    report_path = os.path.join(ROOT, "data", "runs", tag, "%s_report.json" % tag)
    side_path = os.path.join(ROOT, "data", "runs", tag, "%s_negative_result.json" % tag)
    if not os.path.exists(report_path):
        # Runs get cleared for disk space, and that is a legitimate state for a
        # working copy. Saying so loudly beats a red failure that means "you did
        # housekeeping" — and beats silence, because this figure is a required
        # deliverable and verify_package.py will refuse a submission that references
        # a file the archive does not contain.
        print("  --   SKIPPED: no %s run in data/runs." % tag)
        print("       The negative result is a required deliverable; rebuild it with")
        print("         python -X utf8 scripts/rebuild_outputs.py --only %s" % tag)
        print("       then rerun this check.")
    else:
        check(os.path.exists(side_path), "the run report and the figure sidecar both exist")
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
        with open(side_path, encoding="utf-8") as fh:
            side = json.load(fh)
        ranking = report["checkpoint_ranking"]
        points = report["checkpoints"]

        check(side["auc"] == ranking["auc"],
              "the figure's AUC is the report's, to the last bit (%.12f)" % side["auc"])
        check(side["auc_ci95"] == ranking["auc_ci95"], "the confidence interval matches")
        check(side["hits"] == points["hits"] and side["points"] == points["points"],
              "the hit rate on the figure is the report's (%d/%d)"
              % (side["hits"], side["points"]))
        check(side["tolerance"] == points["tolerance"],
              "the %.0f%% bar on the figure is the report's" % (100 * side["tolerance"]))
        check(side["official_points_in_grid"] == ranking["official_points_inside"],
              "the sample size matches (%d points in the grid)"
              % side["official_points_in_grid"])
        check(side["rerun_reproduced_report"] is True,
              "the event was re-run and reproduced the report before drawing")
        # The two computations of the AUC: rank statistics in the pipeline, and the
        # area under the curve this script plotted. They must agree.
        check(side["auc_from_drawn_roc"] is not None
              and abs(side["auc_from_drawn_roc"] - side["auc"]) < 1e-9,
              "the plotted ROC integrates to the reported AUC (%.12f vs %.12f)"
              % (side["auc_from_drawn_roc"], side["auc"]))
        check("roc" in side["panels"] and "ecdf" in side["panels"],
              "the figure carries the ROC and the depth comparison")

    if os.path.exists(report_path):
        for suffix in ("_negative_result.png", "_negative_result.pdf"):
            path = os.path.join(ROOT, "data", "runs", tag, tag + suffix)
            check(os.path.exists(path), "%s exists" % os.path.basename(path))
            if os.path.exists(path) and path.endswith(".pdf"):
                raw = open(path, "rb").read()
                check(raw[:5] == b"%PDF-" and raw.rstrip().endswith(b"%%EOF"),
                      "the negative-result PDF is complete")
                check(b"/FontFile" in raw, "its labels are embedded")

    print("=" * 72)
    if FAILURES:
        print("FAILED: %d check(s)" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("evidence figures match the reports they annotate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

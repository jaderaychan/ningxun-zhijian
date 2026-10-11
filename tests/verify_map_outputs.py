"""Verify the exported map files are what they claim to be.

Three claims are made about these files, and each one has a cheap check:

* the PNG is a real image of the expected size (not a truncated write);
* the PDF is a complete one-page document with its fonts **embedded**, which is
  what lets a judge on a machine with no Chinese font open it and read the labels;
* when an SVG is written, its text is stored as outlines and it references no font
  at all — the portability guarantee, checked by looking for the references rather
  than taken on trust.

What this cannot check is whether the picture looks right; that needs eyes. It does
check the failure that has no visual symptom: a figure that renders perfectly on the
machine that made it and as a grid of empty boxes anywhere else.

    python tests/verify_map_outputs.py [tag ...]
"""

from __future__ import annotations

import os
import re
import struct
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILURES: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        FAILURES.append(msg)


def png_size(path: str):
    """Width and height straight out of the IHDR chunk."""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", head[16:24])


def check_labels_drawable() -> None:
    """Every character the map prints must exist in the chosen font.

    matplotlib substitutes a font rather than failing, so a marker picked for looks
    becomes an empty box and nothing raises. `▸` is missing from Microsoft YaHei;
    that is how this check came to exist.
    """
    print("[labels] every character the map prints can be drawn")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import make_report_map as mapmod

    from floodkit.runio import cjk_font_name, missing_glyphs

    family = cjk_font_name()
    if not family:
        print("  --   no CJK font installed; the map falls back to English labels")
        return
    absent: dict = {}
    for table_name, table in (("ZH", mapmod.ZH), ("EN", mapmod.EN)):
        for key, text in table.items():
            if not isinstance(text, str):
                continue
            gaps = missing_glyphs(text, family)
            if gaps:
                absent["%s.%s" % (table_name, key)] = "".join(sorted(gaps))
    check(not absent,
          "%s can draw every label character (missing: %s)" % (family, absent or "none"))


def main() -> int:
    check_labels_drawable()
    tags = sys.argv[1:] or ["nanjing_final"]
    for tag in tags:
        run_dir = os.path.join(ROOT, "data", "runs", tag)
        print("=" * 72)
        print("verifying the map exports of %s" % tag)

        png = os.path.join(run_dir, "%s_report_map.png" % tag)
        check(os.path.exists(png), "the report map PNG exists")
        if os.path.exists(png):
            size = png_size(png)
            check(size is not None, "the PNG header parses")
            if size:
                check(size[0] > 1200 and size[1] > 900,
                      "the PNG is %dx%d, big enough to read at print size" % size)
            check(os.path.getsize(png) > 200_000,
                  "the PNG is %.2f MB, so it is not a blank canvas"
                  % (os.path.getsize(png) / 1e6))

        pdf = os.path.join(run_dir, "%s_report_map.pdf" % tag)
        check(os.path.exists(pdf), "the report map PDF exists")
        if os.path.exists(pdf):
            raw = open(pdf, "rb").read()
            check(raw[:5] == b"%PDF-", "the PDF header is present")
            check(raw.rstrip().endswith(b"%%EOF"), "the PDF ends with its EOF marker")
            pages = len(re.findall(rb"/Type\s*/Page[^s]", raw))
            check(pages == 1, "the PDF has exactly one page (%d found)" % pages)
            fonts = len(re.findall(rb"/FontFile\d?", raw))
            check(fonts >= 1,
                  "%d font programs are embedded, so the labels render on a machine "
                  "without the font" % fonts)
            check(b"/Image" in raw or b"/XObject" in raw,
                  "the raster layers are carried in the page")

        svg = os.path.join(run_dir, "%s_report_map.svg" % tag)
        if not os.path.exists(svg):
            print("  --   no SVG written for this run (pass --formats png,pdf,svg)")
        else:
            with open(svg, "rb") as fh:
                raw = fh.read()
            root = ET.fromstring(raw)
            ns = {"s": "http://www.w3.org/2000/svg"}
            texts = root.findall(".//s:text", ns)
            paths = root.findall(".//s:path", ns)
            check(not texts,
                  "every label in the SVG is an outline, not a <text> element (%d found)"
                  % len(texts))
            check(not re.search(rb"font-family", raw),
                  "the SVG references no font at all")
            check(len(paths) > 1000, "the SVG carries %d vector paths" % len(paths))
            check(os.path.getsize(svg) > 100_000, "the SVG is not empty")

        tif = os.path.join(run_dir, "%s_depth.tif" % tag)
        check(os.path.exists(tif), "the depth raster is beside the figures")

    print("=" * 72)
    if FAILURES:
        print("FAILED: %d check(s)" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("map exports are well formed and font-independent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

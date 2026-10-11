"""Three-class test samples required by the competition, run as automated checks.

The submission asks for 正常 / 边界 / 失败 samples. Making them a *script* rather
than three screenshots means anyone can re-run them on their own machine and get
the same verdicts, and it stops the failure cases from rotting as the code moves.

Each case declares its expectation up front. The runner asserts it and writes an
evidence bundle, so "handled correctly" is a machine verdict, not a claim.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field

import numpy as np

from .pipeline import DemGrid, run_event, write_outputs

# A study-area envelope is a plausibility gate, not a universal constant: it says
# "elevations this region cannot have". It catches the feet/metre class of error
# only when the mistake pushes values outside the envelope; see LIMITATIONS.md.
DEFAULT_ENVELOPE = (-50.0, 400.0)


@dataclass
class Sample:
    key: str
    kind: str  # normal | boundary | failure
    title: str
    expectation: str
    run: object = None
    check: object = None
    tags: list[str] = field(default_factory=list)


_UNSET = object()


def _event(dem, rain_mm, cn, envelope, cn_grid=None, water_mask=None, **kwargs):
    """Run one sample on the configuration under test.

    Passing the same CN grid and water mask the flagship run uses keeps the
    three-class suite honest: a failure case that exercises a uniform CN while
    the product ships land-cover-driven CN is not testing the product.
    """
    return run_event(
        dem,
        rain_mm=rain_mm,
        cn=cn_grid if cn_grid is not None else cn,
        envelope_m=envelope,
        water_mask=water_mask,
        **kwargs,
    )


def _clone(dem: DemGrid, elevation=_UNSET, crs=_UNSET, transform=_UNSET) -> DemGrid:
    """Copy a grid, overriding only the fields that are explicitly passed.

    `None` cannot be the "not passed" sentinel here: removing the CRS *is* the
    point of the missing-CRS failure case, and treating None as "keep the
    original" made that case silently analyse the real grid. The suite caught it.
    """
    return DemGrid(
        elevation=np.array(dem.elevation if elevation is _UNSET else elevation, dtype="float64", copy=True),
        transform=dem.transform if transform is _UNSET else transform,
        crs=dem.crs if crs is _UNSET else crs,
        nodata=dem.nodata,
        cell_area_m2=dem.cell_area_m2,
        res_x=dem.res_x,
        res_y=dem.res_y,
        source=dem.source,
    )


def _feet(dem: DemGrid) -> DemGrid:
    """The classic unit error: the same surface expressed in feet."""
    return _clone(dem, elevation=np.asarray(dem.elevation) * 3.28084)


def _holey(dem: DemGrid, block: int = 20, keep: int = 8) -> DemGrid:
    """Punch a regular pattern of missing cells, the way a masked DEM arrives.

    The default removes ~12% of the domain: enough to be a real data-quality
    finding, small enough that the run must still complete.
    """
    elevation = np.array(dem.elevation, dtype="float64", copy=True)
    rows, cols = elevation.shape
    for r in range(0, rows, block):
        for c in range(0, cols, block):
            elevation[r : r + max(keep - 1, 1), c : c + max(keep - 1, 1)] = np.nan
    return _clone(dem, elevation=elevation)


def _flat(dem: DemGrid) -> DemGrid:
    """A perfectly flat surface: ponding is undefined, and must be said so."""
    return _clone(dem, elevation=np.full(dem.shape, 15.0))


def build_samples(
    dem: DemGrid,
    rain_mm: float = 100.0,
    cn: float = 85.0,
    cn_grid=None,
    water_mask=None,
) -> list[Sample]:
    samples: list[Sample] = []

    # ---- 正常：完整数据 + 标准降雨事件 --------------------------------
    samples.append(
        Sample(
            key="normal_standard_event",
            kind="normal",
            title="Complete DEM with a standard 100 mm event",
            expectation="outcome ok; every machine-checkable indicator passes; a depth raster is produced",
            run=lambda: _event(dem, rain_mm, cn, DEFAULT_ENVELOPE, cn_grid, water_mask),
            check=_expect_ok,
            tags=["D01", "D01b", "D04"],
        )
    )

    # ---- 边界 1：零降雨 ------------------------------------------------
    samples.append(
        Sample(
            key="boundary_zero_rain",
            kind="boundary",
            title="Zero rainfall",
            expectation="not a failure: quality degrades, zero ponding is reported, indicators still evaluate",
            run=lambda: _event(dem, 0.0, cn, DEFAULT_ENVELOPE, cn_grid, water_mask),
            check=lambda r: _expect(
                r,
                r["quality"]["status"] == "degraded"
                and r["ponding"]["pond_area_m2"] == 0.0
                and r["outcome"] in ("ok", "degraded"),
                "degraded quality, zero ponded area, run still evaluates",
            ),
            tags=["D05"],
        )
    )

    # ---- 边界 2：DEM 有空洞 --------------------------------------------
    samples.append(
        Sample(
            key="boundary_dem_holes",
            kind="boundary",
            title="DEM with a regular pattern of missing cells",
            expectation="holes are reported and treated as drains; the run completes without inventing ponds in holes",
            run=lambda: _event(_holey(dem), rain_mm, cn, DEFAULT_ENVELOPE, cn_grid, water_mask),
            check=lambda r: _expect(
                r,
                r["outcome"] in ("ok", "degraded")
                and any(i["code"] == "DEM_HOLES" for i in r["quality"]["issues"])
                and r["depth_m"] is not None,
                "holes reported, run completed, no fake ponding",
            ),
            tags=["D05"],
        )
    )

    # ---- 边界 3：完全平坦 ----------------------------------------------
    samples.append(
        Sample(
            key="boundary_flat_terrain",
            kind="boundary",
            title="Perfectly flat terrain",
            expectation="flagged as flat; the model must not claim meaningful ponding on an undefined surface",
            run=lambda: _event(_flat(dem), rain_mm, cn, DEFAULT_ENVELOPE, cn_grid, water_mask),
            check=lambda r: _expect(
                r,
                any(i["code"] == "DEM_FLAT" for i in r["quality"]["issues"]),
                "flatness surfaced as a warning",
            ),
            tags=["D05"],
        )
    )

    # ---- 失败 1：缺 CRS -------------------------------------------------
    samples.append(
        Sample(
            key="failure_missing_crs",
            kind="failure",
            title="DEM without a coordinate reference system",
            expectation="fail-closed: rejected, no depth raster, an explicit reason",
            run=lambda: _event(_clone(dem, crs=None), rain_mm, cn, DEFAULT_ENVELOPE, cn_grid, water_mask),
            check=_expect_rejected("CRS_MISSING"),
            tags=["D05", "fail-closed"],
        )
    )

    # ---- 失败 2：单位错误（英尺当成米）--------------------------------
    samples.append(
        Sample(
            key="failure_imperial_units",
            kind="failure",
            title="Elevation delivered in feet",
            expectation="fail-closed: the study-area envelope rejects the surface instead of modelling 3.28x relief",
            run=lambda: _event(_feet(dem), rain_mm, cn, DEFAULT_ENVELOPE, cn_grid, water_mask),
            check=_expect_rejected("DEM_OUT_OF_ENVELOPE"),
            tags=["D05", "fail-closed"],
        )
    )

    # ---- 失败 3：降雨数据损坏 + 曲线号越界 ------------------------------
    def broken_inputs():
        bad_rain = np.full(dem.shape, -5.0)
        return _event(dem, bad_rain, cn, DEFAULT_ENVELOPE, cn_grid, water_mask)

    samples.append(
        Sample(
            key="failure_corrupt_rainfall",
            kind="failure",
            title="Rainfall file with negative depths",
            expectation="fail-closed: rejected before any modelling",
            run=broken_inputs,
            check=_expect_rejected("RAIN_NEGATIVE"),
            tags=["D05", "fail-closed"],
        )
    )

    def bad_cn():
        # The fault must be injected into whatever CN is actually in use. Setting
        # only the scalar argument silently did nothing once a land-cover CN grid
        # was supplied, so the "out of range" failure case ran as a normal
        # analysis and passed — a test that had stopped testing anything.
        if cn_grid is not None:
            broken = np.array(cn_grid, dtype="float64", copy=True)
            broken[broken > 0.0] = 150.0
            return _event(dem, rain_mm, cn, DEFAULT_ENVELOPE, broken, water_mask)
        return _event(dem, rain_mm, 150.0, DEFAULT_ENVELOPE, None, water_mask)

    samples.append(
        Sample(
            key="failure_curve_number_out_of_range",
            kind="failure",
            title="Curve number outside the physical range",
            expectation="fail-closed: CN 150 has no physical meaning and must be refused",
            run=bad_cn,
            check=_expect_rejected("CN_OUT_OF_RANGE"),
            tags=["D05", "fail-closed"],
        )
    )

    return samples


def _expect(_result: dict, condition: bool, message: str) -> tuple[bool, str]:
    return bool(condition), message


def _expect_ok(result: dict) -> tuple[bool, str]:
    if result.get("outcome") != "ok":
        return False, "expected outcome ok, got %s" % result.get("outcome")
    failed = [k for k, v in (result.get("checks") or {}).items() if not v.get("passed", False)]
    if failed:
        return False, "indicators failed: %s" % ", ".join(failed)
    if result.get("depth_m") is None:
        return False, "no depth raster produced"
    return True, "ok, all indicators passed"


def _expect_rejected(code: str):
    def check(result: dict) -> tuple[bool, str]:
        if result.get("outcome") != "rejected":
            return False, "expected a rejection, got %s" % result.get("outcome")
        if result.get("depth_m") is not None:
            return False, "a rejected run still produced a depth raster"
        codes = [i["code"] for i in result.get("quality", {}).get("issues", [])]
        if code not in codes:
            return False, "missing expected issue %s (saw %s)" % (code, codes)
        return True, "rejected with %s, no raster" % code

    return check


def run_samples(
    dem: DemGrid,
    out_dir: str,
    rain_mm: float = 100.0,
    cn: float = 85.0,
    cn_grid=None,
    water_mask=None,
) -> dict:
    """Run every sample, write an evidence bundle, and return the summary."""
    os.makedirs(out_dir, exist_ok=True)
    samples = build_samples(dem, rain_mm=rain_mm, cn=cn, cn_grid=cn_grid, water_mask=water_mask)
    records = []

    for sample in samples:
        result = sample.run()
        passed, detail = sample.check(result)
        record = {
            "key": sample.key,
            "kind": sample.kind,
            "title": sample.title,
            "expectation": sample.expectation,
            "outcome": result.get("outcome"),
            "quality_status": (result.get("quality") or {}).get("status"),
            "issues": [i["code"] for i in (result.get("quality") or {}).get("issues", [])],
            "checks": {k: v.get("passed") for k, v in (result.get("checks") or {}).items()},
            "produced_raster": result.get("depth_m") is not None,
            "expectation_met": passed,
            "verdict_detail": detail,
            "tags": sample.tags,
        }
        if result.get("depth_m") is not None:
            write_outputs(result, dem, os.path.join(out_dir, sample.key), tag=sample.key)
            record["artifacts_written"] = True
        else:
            report = {k: v for k, v in result.items() if k != "depth_m"}
            path = os.path.join(out_dir, "%s_report.json" % sample.key)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
            record["artifacts_written"] = False
            record["report"] = path
        records.append(record)

    summary = {
        "rain_mm": rain_mm,
        "curve_number": cn,
        "total": len(records),
        "passed": sum(1 for r in records if r["expectation_met"]),
        "failed": sum(1 for r in records if not r["expectation_met"]),
        "by_kind": {
            kind: {
                "count": sum(1 for r in records if r["kind"] == kind),
                "passed": sum(1 for r in records if r["kind"] == kind and r["expectation_met"]),
            }
            for kind in ("normal", "boundary", "failure")
        },
        "records": records,
    }

    with open(os.path.join(out_dir, "samples_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(out_dir, "SUMMARY.md"), "w", encoding="utf-8") as fh:
        fh.write(_render(summary))
    return summary


def _render(summary: dict) -> str:
    lines = [
        "# Test samples: normal / boundary / failure",
        "",
        "Generated by `python -m floodkit samples`. Every row is a machine verdict, not a claim:",
        "the case declares what must happen, the runner asserts it, and a mismatch fails the run.",
        "",
        "Rainfall %.0f mm, curve number %.0f." % (summary["rain_mm"], summary["curve_number"]),
        "",
        "**%d / %d expectations met.**" % (summary["passed"], summary["total"]),
        "",
        "| case | kind | expected behaviour | observed | verdict |",
        "|---|---|---|---|---|",
    ]
    for r in summary["records"]:
        lines.append(
            "| `%s` | %s | %s | outcome=%s, quality=%s, raster=%s | %s |"
            % (
                r["key"],
                r["kind"],
                r["title"],
                r["outcome"],
                r["quality_status"],
                "yes" if r["produced_raster"] else "no",
                "PASS" if r["expectation_met"] else "FAIL",
            )
        )
    lines.append("")
    lines.append("## Detail")
    lines.append("")
    for r in summary["records"]:
        lines.append("### %s (%s)" % (r["key"], r["kind"]))
        lines.append("")
        lines.append("- expectation: %s" % r["expectation"])
        lines.append("- verdict: %s -- %s" % ("PASS" if r["expectation_met"] else "FAIL", r["verdict_detail"]))
        lines.append("- quality issues: %s" % (", ".join(r["issues"]) or "none"))
        lines.append("- indicators: %s" % (", ".join("%s=%s" % (k, v) for k, v in r["checks"].items()) or "n/a"))
        lines.append("")
    return "\n".join(lines)

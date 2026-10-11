"""Input quality gates.

Design rule: the pipeline must never turn broken input into a plausible-looking
map. Every check returns an issue with an explicit severity, and any `fatal`
issue makes the run `rejected` — the caller is then required to stop and report
instead of producing a depth raster.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

FATAL = "fatal"
WARNING = "warning"
INFO = "info"


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class QualityReport:
    issues: list[Issue] = field(default_factory=list)

    def add(self, code, severity, message, **detail):
        self.issues.append(Issue(code=code, severity=severity, message=message, detail=detail))

    @property
    def fatal(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == FATAL]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def status(self) -> str:
        if self.fatal:
            return "rejected"
        if self.warnings:
            return "degraded"
        return "ok"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "issues": [
                {"code": i.code, "severity": i.severity, "message": i.message, "detail": i.detail}
                for i in self.issues
            ],
        }


def check_georeference(crs, transform, report: QualityReport) -> None:
    if crs is None:
        report.add(
            "CRS_MISSING",
            FATAL,
            "DEM has no coordinate reference system; cell areas and distances would be meaningless.",
        )
    if transform is None:
        report.add("TRANSFORM_MISSING", FATAL, "DEM has no geotransform.")
        return
    a, b, c, d, e, f = (float(v) for v in transform[:6])
    if a == 0 or e == 0:
        report.add("TRANSFORM_DEGENERATE", FATAL, "DEM geotransform has a zero pixel size.")
    if abs(b) > 1e-12 or abs(d) > 1e-12:
        report.add(
            "TRANSFORM_ROTATED",
            WARNING,
            "DEM geotransform is rotated; cell-area calculation assumes an axis-aligned grid.",
        )
    if abs(a) < 1e-6 and abs(e) < 1e-6:
        report.add("TRANSFORM_TINY_CELL", FATAL, "DEM pixel size is effectively zero.")


def check_elevation(
    dem: np.ndarray,
    nodata: float | None,
    report: QualityReport,
    envelope_m: tuple[float, float] = (-30.0, 800.0),
    units: str | None = None,
    reference_envelope: tuple[float, float] | None = None,
) -> None:
    """Range and integrity checks against a configurable regional envelope.

    The envelope is a plausibility gate, not a magic unit detector: Jiangsu is a
    lowland plain, so a DEM claiming 2000 m there is wrong (wrong units, wrong
    vertical datum or wrong tile). Override it per study area.

    `reference_envelope` is the validated default for the study area. When the
    caller has overridden it, the failure message says so: an envelope that is too
    narrow is the one cause on that list the caller can fix by itself, and without
    this the gate names four possibilities and the agent asks the user which one it
    is instead of retrying.
    """
    arr = np.asarray(dem, dtype="float64")
    valid = np.isfinite(arr)
    if nodata is not None:
        valid &= arr != nodata
    n_valid = int(valid.sum())
    if n_valid == 0:
        report.add("DEM_ALL_NODATA", FATAL, "DEM contains no valid elevation cells.")
        return

    vals = arr[valid]
    lo, hi = float(vals.min()), float(vals.max())
    if units is not None and str(units).strip().lower() not in ("", "m", "metre", "meter", "metres", "meters"):
        report.add(
            "DEM_UNIT_DECLARED_NON_METRIC",
            FATAL,
            f"DEM declares vertical units '{units}'; the model requires metres.",
            declared_units=str(units),
        )
    if hi > envelope_m[1] or lo < envelope_m[0]:
        message = (
            "DEM elevation range %.1f..%.1f m is outside the plausible envelope %.1f..%.1f m "
            "for this study area (suspect units, vertical datum, or wrong tile)."
            % (lo, hi, envelope_m[0], envelope_m[1])
        )
        detail: dict = {"min_m": lo, "max_m": hi, "envelope": list(envelope_m)}
        if reference_envelope is not None and tuple(envelope_m) != tuple(reference_envelope):
            message += (
                " This envelope was supplied by the caller, not the validated default for this "
                "study area (%.1f..%.1f m): if it was not measured, drop the override and re-run."
                % (reference_envelope[0], reference_envelope[1])
            )
            detail["envelope_overridden"] = True
            detail["validated_default_envelope"] = list(reference_envelope)
        report.add("DEM_OUT_OF_ENVELOPE", FATAL, message, **detail)
    if float(vals.std()) < 1e-6:
        report.add(
            "DEM_FLAT",
            WARNING,
            "DEM is perfectly flat; ponding is undefined and results will be reported as not applicable.",
        )


def check_nodata_holes(dem: np.ndarray, nodata: float | None, report: QualityReport) -> None:
    arr = np.asarray(dem, dtype="float64")
    invalid = ~np.isfinite(arr)
    if nodata is not None:
        invalid |= arr == nodata
    frac = float(invalid.mean())
    if frac == 0.0:
        report.add("DEM_NO_HOLES", INFO, "DEM has no missing cells.")
        return
    labels, count = ndimage.label(invalid, structure=np.ones((3, 3), dtype=bool))
    largest = 0
    if count:
        largest = int(np.bincount(labels.ravel())[1:].max())
    report.add(
        "DEM_HOLES",
        WARNING if frac < 0.2 else FATAL,
        "DEM has %.2f%% missing cells (largest contiguous hole: %d cells); holes are treated as open drains."
        % (frac * 100.0, largest),
        missing_fraction=frac,
        missing_cells=int(invalid.sum()),
        largest_hole_cells=largest,
    )


def check_rainfall(rain_mm: np.ndarray, report: QualityReport) -> None:
    arr = np.asarray(rain_mm, dtype="float64")
    if not np.isfinite(arr).all():
        report.add("RAIN_NON_FINITE", FATAL, "Rainfall input contains NaN or infinite values.")
        return
    if (arr < 0).any():
        report.add("RAIN_NEGATIVE", FATAL, "Rainfall input contains negative depths.")
    total = float(arr.mean())
    if total <= 0.0:
        report.add(
            "RAIN_ZERO",
            WARNING,
            "Rainfall scenario is empty; the model will report zero ponding as a boundary case, not a failure.",
        )
    if arr.max() > 2000.0:
        report.add(
            "RAIN_IMPLAUSIBLE",
            FATAL,
            "Rainfall depth %.1f mm exceeds any plausible single-event value (suspect unit error, e.g. mm vs 0.1 mm)."
            % float(arr.max()),
        )


def check_curve_number(cn: np.ndarray, report: QualityReport) -> None:
    arr = np.asarray(cn, dtype="float64")
    if not np.isfinite(arr).all():
        report.add("CN_NON_FINITE", FATAL, "Curve number grid contains NaN.")
        return
    if (arr < 30.0).any() or (arr > 100.0).any():
        report.add(
            "CN_OUT_OF_RANGE",
            FATAL,
            "Curve numbers outside 30..100 are not physically meaningful (min %.1f, max %.1f)."
            % (float(arr.min()), float(arr.max())),
        )


def check_domain(dem: np.ndarray, report: QualityReport, min_cells: int = 100) -> None:
    if dem.size < min_cells:
        report.add(
            "DOMAIN_TOO_SMALL",
            FATAL,
            f"Study area has only {dem.size} cells; at least {min_cells} are required.",
        )

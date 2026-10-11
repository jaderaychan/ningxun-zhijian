"""Scenario definition: rainfall input and curve-number assignment."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field

import numpy as np

from .hydro import CN_TABLE_HSG_C, HSG_SHIFT


@dataclass
class RainfallScenario:
    name: str
    total_mm: float
    duration_h: float
    shape: np.ndarray | None = None
    source: str = "scenario assumption"
    note: str = ""

    def grid(self, shape: tuple[int, int]) -> np.ndarray:
        """Rainfall depth per cell in millimetres."""
        if self.shape is not None:
            if self.shape.shape != shape:
                raise ValueError("rainfall shape %s does not match domain %s" % (self.shape.shape, shape))
            return self.shape.astype("float64")
        return np.full(shape, float(self.total_mm), dtype="float64")


def uniform_rainfall(total_mm: float, duration_h: float = 6.0, name: str = "uniform") -> RainfallScenario:
    """A flat rainfall scenario.

    This is an explicit *scenario assumption*, not an official design storm.
    The competition workflow only needs the number to be declared and traceable,
    so the scenario is always reported with its source field.
    """
    return RainfallScenario(
        name=name,
        total_mm=float(total_mm),
        duration_h=float(duration_h),
        source="scenario assumption (flat depth over the study area)",
        note="Replace with an official design storm or gauge hyetograph for engineering use.",
    )


def from_csv(path: str, name: str | None = None) -> RainfallScenario:
    """Load a hyetograph (`minutes,depth_mm`) or a gridded depth raster (.npy)."""
    if path.lower().endswith(".npy"):
        arr = np.load(path).astype("float64")
        return RainfallScenario(
            name=name or "gridded",
            total_mm=float(arr.mean()),
            duration_h=float("nan"),
            shape=arr,
            source="file:%s" % path,
        )
    depths = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            key = None
            for candidate in ("depth_mm", "rain_mm", "depth", "rain", "precip_mm"):
                if candidate in row:
                    key = candidate
                    break
            if key is None:
                raise ValueError("CSV needs a depth column (depth_mm / rain_mm / depth / rain)")
            depths.append(float(row[key]))
    arr = np.asarray(depths, dtype="float64")
    if arr.size == 0:
        raise ValueError("empty hyetograph: %s" % path)
    if (arr < 0).any():
        raise ValueError("hyetograph contains negative depths: %s" % path)
    return RainfallScenario(
        name=name or "hyetograph",
        total_mm=float(arr.sum()),
        duration_h=float(arr.size) / 60.0,
        source="file:%s" % path,
    )


@dataclass
class LandCoverScenario:
    classes: np.ndarray
    hsg: str = "C"
    mapping: dict = field(default_factory=lambda: dict(CN_TABLE_HSG_C))
    source: str = "built-in TR-55 (HSG C) table"

    def curve_number(self) -> np.ndarray:
        shift = HSG_SHIFT.get(self.hsg.upper(), 0.0)
        names = np.asarray([str(v) for v in self.classes.ravel()], dtype=object)
        cn = np.full(names.size, np.nan, dtype="float64")
        for key, value in self.mapping.items():
            cn[names == key] = value
        cn = cn.reshape(self.classes.shape)
        missing = int(np.isnan(cn).sum())
        if missing:
            raise ValueError(
                "%d cells have a land-cover class absent from the CN mapping; "
                "add it explicitly instead of guessing a value" % missing
            )
        return np.clip(cn + shift, 30.0, 100.0)


def uniform_curve_number(shape: tuple[int, int], cn: float) -> np.ndarray:
    return np.full(shape, float(cn), dtype="float64")

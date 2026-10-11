"""Study areas for this project.

Scope decision: **Nanjing only.** The model's parameters (curve numbers, the
elevation envelope, the OSM exposure layers) were all validated against Nanjing,
and the two boxes below are the ones real runs used. Offering other cities would
mean shipping boxes nobody checked, so the resolver refuses them instead of
guessing -- a wrong box silently produces a confident, wrong map.

What it does accept:

  * a named Nanjing study area (built-in, exact, reproducible), or
  * explicit bounds the caller supplies, as long as they fall inside the Nanjing
    envelope and are large enough to be a real study area.

Anything else is refused with a message naming what is supported. No network.
"""

from __future__ import annotations

# Boxes that validated runs actually used. Keep in sync with data/runs/ and with
# the figures quoted in README.md and docs/.
KNOWN_AREAS: dict[str, dict] = {
    "南京主城区": {
        "bounds": [118.72, 32.02, 118.82, 32.10],
        "area_note": "已验证：报告与文档中的全部数字出自这个范围（有效分析面积 76.3 km²）",
        "aliases": ["南京", "南京市", "南京主城", "南京市区", "南京中心城区", "主城区",
                    "nanjing", "nanjing main city", "nanjing city"],
    },
    "南京（扩大）": {
        "bounds": [118.72, 32.00, 119.00, 32.16],
        "area_note": "已验证：用于和 2015 年官方易涝点清单做外部对照（440 km²，含紫金山）",
        "aliases": ["南京全域", "南京市域", "南京全市域", "南京扩大", "nanjing wide", "nanjing greater"],
    },
}

# The whole municipality, used only to reject out-of-scope requests. Generous on
# purpose: it is a scope guard, not a boundary anyone surveyed.
NANJING_ENVELOPE = (118.20, 31.10, 119.40, 32.70)

# A study area smaller than this cannot hold a meaningful depression inventory.
MIN_SPAN_DEG = 0.01  # ~1 km


class PlaceError(RuntimeError):
    """Raised when a study area cannot be determined. Callers must not invent one."""


def _normalise(name: str) -> str:
    return str(name).strip().lower().replace(" ", "").replace("　", "")


def known_areas() -> list[str]:
    return sorted(KNOWN_AREAS)


def supported_note() -> str:
    lines = ["本项目只覆盖南京市。可用的命名研究区："]
    for name, entry in KNOWN_AREAS.items():
        west, south, east, north = entry["bounds"]
        lines.append("  · %s  bounds=[%.2f, %.2f, %.2f, %.2f]（%.1f–%.1f°E, %.1f–%.1f°N）"
                     % (name, west, south, east, north, west, east, south, north))
        lines.append("      %s" % entry["area_note"])
    lines.append("如需其他区域，请直接给出经纬度范围（必须落在南京市境内）。")
    return "\n".join(lines)


def validate_bounds(bounds, source: str = "caller") -> list[float]:
    """Check caller-supplied bounds. Raises PlaceError when they are unusable."""
    try:
        west, south, east, north = (float(v) for v in bounds)
    except Exception as error:
        raise PlaceError("范围必须是四个数：西、南、东、北（WGS84 经纬度）。") from error
    if not (east > west and north > south):
        raise PlaceError("范围不合法：东界必须大于西界，北界必须大于南界。")
    if east - west < MIN_SPAN_DEG or north - south < MIN_SPAN_DEG:
        raise PlaceError("范围太小（不足约 1 公里），无法做有意义的分析。")
    ew, es, ee, en = NANJING_ENVELOPE
    if east < ew or west > ee or north < es or south > en:
        raise PlaceError("范围不在南京市境内。本项目只覆盖南京。\n" + supported_note())
    return [west, south, east, north]


def resolve(name: str) -> dict:
    """Resolve a named Nanjing study area.

    Returns bounds in WGS84 degrees plus provenance. Raises PlaceError for
    anything outside Nanjing -- the caller must ask rather than assume.
    """
    key = _normalise(name)
    if not key:
        raise PlaceError("地名是空的。\n" + supported_note())

    for canonical, entry in KNOWN_AREAS.items():
        if _normalise(canonical) == key or key in {_normalise(a) for a in entry["aliases"]}:
            west, south, east, north = entry["bounds"]
            return {
                "query": name,
                "name": canonical,
                "bounds": list(entry["bounds"]),
                "center": [round((west + east) / 2.0, 4), round((south + north) / 2.0, 4)],
                "approximate": False,
                "source": "built-in validated study area",
                "note": entry["area_note"],
            }

    raise PlaceError(
        "认不出研究区 %r。本项目只覆盖南京，不做其他城市。\n%s" % (name, supported_note()))

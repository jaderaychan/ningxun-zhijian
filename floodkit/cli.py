"""Command line entry point for the flood analysis pipeline.

Every subcommand prints a single JSON object on stdout so the harness (AGH)
can parse tool results without screen-scraping.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEM_CACHE = os.path.join(ROOT, "data", "dem")


def _envelope(args) -> tuple[float, float]:
    """Use the study-area constant unless the caller overrode BOTH bounds."""
    from .pipeline import DEFAULT_ENVELOPE

    if args.envelope_min is None or args.envelope_max is None:
        return DEFAULT_ENVELOPE
    return (float(args.envelope_min), float(args.envelope_max))


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def cmd_fetch_dem(args) -> int:
    from .dem_source import ensure_tiles

    bounds = tuple(args.bounds)
    cache = args.cache or DEFAULT_DEM_CACHE
    try:
        paths = ensure_tiles(bounds, cache)
    except Exception as exc:  # network or naming failure must be visible, not silent
        _emit({"ok": False, "error": "dem_fetch_failed", "message": str(exc), "bounds": list(bounds)})
        return 2
    _emit({"ok": True, "bounds": list(bounds), "cache_dir": cache, "tiles": paths})
    return 0


def cmd_run(args) -> int:
    from .pipeline import load_dem_metric, run_event, write_outputs
    from .dem_source import ensure_tiles

    bounds = tuple(args.bounds)
    cache = args.dem_cache or DEFAULT_DEM_CACHE
    if args.dem_file:
        # Fault injection needs to point the run at a specific (possibly broken)
        # file instead of whatever the tile cache holds.
        tiles = [args.dem_file]
        if not os.path.exists(args.dem_file):
            _emit({"ok": False, "error": "dem_file_missing", "message": args.dem_file})
            return 2
    else:
        try:
            tiles = ensure_tiles(bounds, cache, progress=lambda *_: None)
        except Exception as exc:
            _emit({"ok": False, "error": "dem_fetch_failed", "message": str(exc)})
            return 2

    grid = None
    last_error = None
    for tile in tiles:
        try:
            grid = load_dem_metric(tile, bounds)
            break
        except Exception as exc:  # try the next tile that overlaps the window
            last_error = exc
    if grid is None:
        _emit({"ok": False, "error": "dem_load_failed", "message": str(last_error)})
        return 2

    checkpoints = None
    checkpoint_names = None
    if args.checkpoints:
        _CHECKPOINT_NAMES.clear()
        checkpoints = _load_checkpoints(args.checkpoints)
        checkpoint_names = list(_CHECKPOINT_NAMES) or None

    cn_input = args.cn
    water_mask = None
    landcover_stats = None
    if args.cn_source == "landcover":
        from .landcover import curve_number_grid, ensure_tile

        lc_cache = args.landcover_cache or os.path.join(os.path.dirname(DEFAULT_DEM_CACHE), "landcover")
        try:
            tile = ensure_tile(bounds, lc_cache, progress=lambda *_: None)
            landcover = curve_number_grid(tile, bounds, grid.transform, grid.shape)
        except Exception as exc:
            _emit({"ok": False, "error": "landcover_failed", "message": "%s: %s" % (type(exc).__name__, exc)})
            return 2
        cn_input = landcover["cn"]
        water_mask = landcover["water_mask"]
        landcover_stats = {k: v for k, v in landcover.items() if not hasattr(v, "shape")}

    result = run_event(
        dem=grid,
        rain_mm=args.rain_mm,
        cn=cn_input,
        envelope_m=_envelope(args),
        min_depth_m=args.min_depth,
        checkpoints_lonlat=checkpoints,
        checkpoint_names=checkpoint_names,
        water_mask=water_mask,
    )
    # The requested bounds are not recoverable from the raster: the metric grid
    # covers a slightly larger footprint than the window asked for, and the
    # difference is a cell or two — enough that recomputing a mask or re-selecting a
    # cached OSM extract from the raster's own bounds picks a different input and
    # quietly changes a number. Recorded here so any later tool can reproduce this
    # run instead of guessing at it.
    result["run_config"] = {
        "bounds_wgs84": [float(v) for v in bounds],
        "rain_mm": float(args.rain_mm),
        "cn": float(args.cn),
        "cn_source": args.cn_source,
        "min_depth_m": float(args.min_depth),
        "envelope_m": [float(v) for v in _envelope(args)],
        "checkpoints": args.checkpoints,
        "exposure": bool(args.exposure),
    }
    if landcover_stats is not None:
        result["landcover"] = landcover_stats

    if args.exposure:
        from .pipeline import attach_exposure

        osm_cache = args.osm_cache or os.path.join(os.path.dirname(DEFAULT_DEM_CACHE), "osm")
        attach_exposure(result, grid, bounds, osm_cache, min_depth_m=args.min_depth)

    out_dir = args.out or os.path.join(os.path.dirname(DEFAULT_DEM_CACHE), "runs", args.tag)
    paths = write_outputs(result, grid, out_dir, tag=args.tag)

    payload = {k: v for k, v in result.items() if k != "depth_m"}
    payload["ok"] = True
    payload["artifacts"] = paths
    _emit(payload)
    return 0 if result.get("outcome") in ("ok", "degraded") else 3


_CHECKPOINT_NAMES: list[str] = []


def _load_checkpoints(path: str):
    """Read checkpoint coordinates from JSON or CSV.

    A header row with `lon`/`lat` columns is honoured, because the official
    waterlogging list carries a name and a match-quality column first; assuming
    positional `lon,lat` would silently read the *name* as a longitude.
    """
    points = []
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read().strip()
    if text.startswith("["):
        for item in json.loads(text):
            if isinstance(item, dict):
                points.append((float(item["lon"]), float(item["lat"])))
            else:
                points.append((float(item[0]), float(item[1])))
        return points

    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if lines and "lon" in lines[0].lower() and "lat" in lines[0].lower():
        reader = csv.DictReader(lines)
        for row in reader:
            lon = (row.get("lon") or "").strip()
            lat = (row.get("lat") or "").strip()
            if not lon or not lat:
                continue  # unmatched entries are reported by the builder, not guessed here
            points.append((float(lon), float(lat)))
            _CHECKPOINT_NAMES.append((row.get("name") or "(unnamed)").strip())
        return points

    for line in lines:
        parts = line.replace(",", " ").split()
        if len(parts) >= 2:
            points.append((float(parts[0]), float(parts[1])))
    return points


def cmd_samples(args) -> int:
    """Run the normal / boundary / failure sample suite and write the evidence bundle."""
    from .dem_source import ensure_tiles
    from .pipeline import load_dem_metric
    from .samples import run_samples

    bounds = tuple(args.bounds)
    cache = args.dem_cache or DEFAULT_DEM_CACHE
    try:
        tiles = ensure_tiles(bounds, cache, progress=lambda *_: None)
    except Exception as exc:
        _emit({"ok": False, "error": "dem_fetch_failed", "message": str(exc)})
        return 2

    grid = None
    last_error = None
    for tile in tiles:
        try:
            grid = load_dem_metric(tile, bounds)
            break
        except Exception as exc:
            last_error = exc
    if grid is None:
        _emit({"ok": False, "error": "dem_load_failed", "message": str(last_error)})
        return 2

    out_dir = args.out or os.path.join(
        os.path.dirname(DEFAULT_DEM_CACHE), "evidence", "samples"
    )

    cn_grid = None
    water_mask = None
    landcover_stats = None
    if args.cn_source == "landcover":
        from .landcover import curve_number_grid, ensure_tile

        lc_cache = args.landcover_cache or os.path.join(os.path.dirname(DEFAULT_DEM_CACHE), "landcover")
        try:
            tile = ensure_tile(bounds, lc_cache, progress=lambda *_: None)
            landcover = curve_number_grid(tile, bounds, grid.transform, grid.shape)
        except Exception as exc:
            _emit({"ok": False, "error": "landcover_failed", "message": "%s: %s" % (type(exc).__name__, exc)})
            return 2
        cn_grid = landcover["cn"]
        water_mask = landcover["water_mask"]
        landcover_stats = {k: v for k, v in landcover.items() if not hasattr(v, "shape")}

    summary = run_samples(
        grid, out_dir, rain_mm=args.rain_mm, cn=args.cn, cn_grid=cn_grid, water_mask=water_mask
    )
    summary["cn_source"] = args.cn_source
    if landcover_stats is not None:
        summary["landcover"] = landcover_stats
    # rewrite the bundle so the summary on disk records which configuration was tested
    with open(os.path.join(out_dir, "samples_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)
    _emit({"ok": summary["failed"] == 0, "out_dir": out_dir, "summary": summary})
    return 0 if summary["failed"] == 0 else 5


def cmd_selftest(args) -> int:
    """Analytic and synthetic checks that do not need network access."""
    import numpy as np

    from .terrain import fill_depressions, depression_depth, flow_sinks, stage_from_volume

    checks = []

    # 1. depression filling on a known pit
    dem = np.full((9, 9), 10.0)
    dem[4, 4] = 5.0
    filled = fill_depressions(dem)
    checks.append({
        "name": "fill_single_pit",
        "expected": 10.0,
        "observed": float(filled[4, 4]),
        "passed": bool(abs(filled[4, 4] - 10.0) < 1e-9),
    })

    # 2. depression depth equals the known pit depth
    depth = depression_depth(dem, filled)
    checks.append({
        "name": "pit_depth",
        "expected": 5.0,
        "observed": float(depth[4, 4]),
        "passed": bool(abs(depth[4, 4] - 5.0) < 1e-9),
    })

    # 3. stage-volume inversion is exact for a linear basin
    elevations = np.array([0.0, 1.0, 2.0, 3.0])
    volumes = np.array([0.0, 1.0, 4.0, 9.0])  # A=1: V_k = k*e_k - sum(e)
    stage, stored = stage_from_volume(elevations, volumes, 4.0)
    checks.append({
        "name": "stage_volume_inverse",
        "expected": 2.0,
        "observed": float(stage),
        "passed": bool(abs(stage - 2.0) < 1e-9 and abs(stored - 4.0) < 1e-9),
    })

    # 4. D8 routing: a tilted plane must drain every cell to one boundary sink
    plane = np.tile(np.arange(20, dtype="float64"), (20, 1))[::-1]
    sinks = flow_sinks(plane)
    terminals = set(sinks.ravel().tolist())
    checks.append({
        "name": "flow_sinks_tilted_plane",
        "expected": "few terminals",
        "observed": len(terminals),
        "passed": bool(len(terminals) <= 20),
    })

    # 5. SCS-CN against the published closed form
    from .hydro import scs_cn_runoff

    cn = 80.0
    s = 25400.0 / cn - 254.0
    ia = 0.2 * s
    p = 100.0
    expected = (p - ia) ** 2 / (p - ia + s)
    observed = float(scs_cn_runoff(p, cn))
    checks.append({
        "name": "scs_cn_closed_form",
        "expected": expected,
        "observed": observed,
        "passed": bool(abs(expected - observed) < 1e-9),
    })

    passed = all(c["passed"] for c in checks)
    _emit({"ok": passed, "checks": checks})
    return 0 if passed else 4


def cmd_places(args) -> int:
    """Resolve a place name to a study box. Never guesses: unknown means unknown."""
    from .places import PlaceError, known_areas, resolve

    if args.list:
        _emit({"ok": True, "known_areas": known_areas(),
               "note": "这些是内置的、已验证过的研究区；其他地名会走地理编码。"})
        return 0
    if not args.name:
        _emit({"ok": False, "code": "NO_NAME", "message": "请给出 --name，或用 --list 看内置研究区。"})
        return 2
    try:
        result = resolve(args.name)
    except PlaceError as error:
        _emit({"ok": False, "code": "PLACE_UNRESOLVED", "query": args.name, "message": str(error)})
        return 3
    _emit({"ok": True, **result})
    return 0


def cmd_forecast(args) -> int:
    """Forecast rainfall over a window, reported as a range across models."""
    from .places import PlaceError, resolve, validate_bounds
    from .weather import window_forecast

    if args.bounds:
        try:
            west, south, east, north = validate_bounds(args.bounds)
        except PlaceError as error:
            _emit({"ok": False, "code": "BOUNDS_REJECTED", "message": str(error)})
            return 3
        lat, lon = (south + north) / 2.0, (west + east) / 2.0
        place = {
            "name": args.name or "给定范围的中心",
            "bounds": [west, south, east, north],
            "approximate": False,
            "source": "caller-supplied bounds (inside Nanjing)",
        }
    else:
        if not args.place:
            _emit({"ok": False, "code": "NO_PLACE", "message": "请给出 --place 或 --bounds。"})
            return 2
        try:
            place = resolve(args.place)
        except PlaceError as error:
            _emit({"ok": False, "code": "PLACE_UNRESOLVED", "message": str(error)})
            return 3
        lon, lat = float(place["center"][0]), float(place["center"][1])

    result = window_forecast(lat, lon, args.at, ROOT,
                             window_hours=args.window_hours,
                             forecast_days=args.forecast_days,
                             past_days=args.past_days)
    result["place"] = place
    if result.get("ok"):
        result["interpretation"] = (
            "这是%s在 %s 起 %d 小时的预报区间（%s）。"
            "它是外部预报，不是用户说的雨量；两者不一致时必须如实告知用户，"
            "不要用其中之一覆盖另一个。"
            % (place.get("name"), result["window"]["start"], result["window"]["hours"],
               result.get("spread_source"))
        )
    _emit(result)
    return 0 if result.get("ok") else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="floodkit", description="Urban ponding analysis toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch-dem", help="download Copernicus DEM tiles for a bounding box")
    p_fetch.add_argument("--bounds", nargs=4, type=float, required=True,
                         metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    p_fetch.add_argument("--cache", default=None)
    p_fetch.set_defaults(func=cmd_fetch_dem)

    p_run = sub.add_parser("run", help="run one rainfall event end to end")
    p_run.add_argument("--bounds", nargs=4, type=float, required=True,
                       metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    p_run.add_argument("--rain-mm", type=float, required=True, help="event rainfall depth (mm)")
    p_run.add_argument("--cn", type=float, required=True, help="curve number (uniform) or grid file path")
    p_run.add_argument("--cn-source", choices=("uniform", "landcover"), default="uniform",
                       help="uniform: use --cn everywhere; landcover: derive CN from ESA WorldCover 10m")
    p_run.add_argument("--landcover-cache", default=None, help="WorldCover tile cache directory")
    p_run.add_argument("--tag", default="run")
    p_run.add_argument("--out", default=None, help="output directory")
    p_run.add_argument("--dem-cache", default=None)
    p_run.add_argument("--dem-file", default=None,
                       help="use this specific DEM file instead of the tile cache (for fault injection)")
    p_run.add_argument("--min-depth", type=float, default=0.05)
    p_run.add_argument("--envelope-min", type=float, default=None,
                       help="lower plausibility bound for elevation (default: study-area constant)")
    p_run.add_argument("--envelope-max", type=float, default=None,
                       help="upper plausibility bound for elevation (default: study-area constant)")
    p_run.add_argument("--checkpoints", default=None,
                       help="CSV/JSON of lon,lat waterlogging points for the hit-rate check")
    p_run.add_argument("--exposure", action="store_true",
                       help="also analyse OpenStreetMap road and building exposure (needs network)")
    p_run.add_argument("--osm-cache", default=None, help="OSM extract cache directory")
    p_run.set_defaults(func=cmd_run)

    p_self = sub.add_parser("selftest", help="analytic checks that need no network")
    p_self.set_defaults(func=cmd_selftest)

    p_samples = sub.add_parser("samples", help="run the normal/boundary/failure sample suite")
    p_samples.add_argument("--bounds", nargs=4, type=float, required=True,
                           metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    p_samples.add_argument("--rain-mm", type=float, default=100.0)
    p_samples.add_argument("--cn", type=float, default=85.0)
    p_samples.add_argument("--out", default=None)
    p_samples.add_argument("--dem-cache", default=None)
    p_samples.add_argument("--cn-source", choices=("uniform", "landcover"), default="uniform",
                           help="uniform: one CN everywhere; landcover: the shipped configuration")
    p_samples.add_argument("--landcover-cache", default=None)
    p_samples.set_defaults(func=cmd_samples)

    p_places = sub.add_parser("places", help="resolve a Nanjing study area by name")
    p_places.add_argument("--name", default=None, help="e.g. 南京主城区")
    p_places.add_argument("--list", action="store_true", help="list the built-in study areas")
    p_places.set_defaults(func=cmd_places)

    p_fc = sub.add_parser("forecast", help="forecast rainfall over a window, as a range across models")
    p_fc.add_argument("--place", default=None, help="Nanjing study area name, e.g. 南京主城区")
    p_fc.add_argument("--bounds", nargs=4, type=float, default=None,
                      help="explicit west south east north (must be inside Nanjing)")
    p_fc.add_argument("--at", required=True, help="local start time, e.g. 2026-10-11T12:00")
    p_fc.add_argument("--window-hours", type=int, default=1, dest="window_hours")
    p_fc.add_argument("--forecast-days", type=int, default=16, dest="forecast_days",
                      help="Open-Meteo serves at most 16 days")
    p_fc.add_argument("--past-days", type=int, default=1, dest="past_days")
    p_fc.set_defaults(func=cmd_forecast)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

"""Regression and analytic tests for floodkit.

Runs without pytest:
    python tests/test_floodkit.py
and is also pytest-compatible (`pytest tests/test_floodkit.py`).

Every test here exists because a real bug or a real verification requirement
put it there; the docstring of each says which.
"""

from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, ".pylibs"))
sys.path.insert(0, ROOT)

from floodkit import quality  # noqa: E402
from floodkit.hydro import (  # noqa: E402
    initial_abstraction,
    potential_retention,
    run_ponding,
    scs_cn_retention,
    scs_cn_runoff,
    water_balance,
)
from floodkit.pipeline import DemGrid, run_event  # noqa: E402
from floodkit.terrain import (  # noqa: E402
    depression_depth,
    fill_depressions,
    flow_sinks,
    stage_from_volume,
)


def _grid(elevation, crs="EPSG:32650", res=30.0):
    from affine import Affine

    transform = Affine(res, 0.0, 500000.0, 0.0, -res, 3600000.0)
    return DemGrid(
        elevation=np.asarray(elevation, dtype="float64"),
        transform=transform,
        crs=crs,
        nodata=None,
        cell_area_m2=res * res,
        res_x=res,
        res_y=-res,
        source="synthetic",
    )


def test_fill_single_pit():
    """A 5 m pit in a flat 10 m plane must fill to exactly the spill level."""
    dem = np.full((9, 9), 10.0)
    dem[4, 4] = 5.0
    filled = fill_depressions(dem)
    assert abs(filled[4, 4] - 10.0) < 1e-9


def test_fill_ignores_missing_cells():
    """Regression: NaN cells used to poison the priority-flood heap.

    A missing cell must drain a basin, never dam one, and must never leak a NaN
    key into the heap. Before the fix this produced 75 m of fake ponding and a
    98% 'depression' fraction across the whole domain.
    """
    dem = np.full((40, 40), 12.0)
    dem[18:22, 18:22] = 11.5
    dem[20, 20] = np.nan
    filled = fill_depressions(dem)
    assert np.isnan(filled[20, 20])
    assert np.isfinite(filled).all() or np.isnan(filled).sum() == 1
    depth = depression_depth(dem, filled)
    assert np.nanmax(depth) < 1.0, "a missing cell must not create deep fake ponding"

    # a hole on the rim of a real pit must drain it towards that boundary
    dem2 = np.full((30, 30), 20.0)
    dem2[14:17, 14:17] = 12.0
    dem2[13, 15] = np.nan
    filled2 = fill_depressions(dem2)
    assert np.nanmax(depression_depth(dem2, filled2)) <= 8.0 + 1e-9


def test_stage_volume_exact_and_monotone():
    """The stage-volume inversion must be exact and monotone."""
    elevations = np.array([0.0, 1.0, 2.0, 3.0])
    volumes = np.array([0.0, 1.0, 4.0, 9.0])
    stage, stored = stage_from_volume(elevations, volumes, 4.0)
    assert abs(stage - 2.0) < 1e-9
    assert abs(stored - 4.0) < 1e-9

    prev = -1.0
    for target in np.linspace(0.0, 9.0, 25):
        s, _ = stage_from_volume(elevations, volumes, float(target))
        assert s >= prev - 1e-12
        prev = s


def test_scs_identity_includes_retention():
    """Regression: the first mass balance forgot SCS continuing retention F.

    P = Ia + F + Q must hold analytically; omitting F left a 29.7% residual that
    looked like a routing bug but was a broken accounting identity.
    """
    for cn in (60.0, 75.0, 85.0, 95.0):
        for p in (0.0, 5.0, 40.0, 100.0, 250.0):
            s = float(potential_retention(cn))
            ia = float(initial_abstraction(cn))
            q = float(scs_cn_runoff(p, cn))
            f = float(scs_cn_retention(p, cn))
            if p <= ia:
                assert q == 0.0 and f == 0.0
                continue
            assert abs(p - ia - f - q) < 1e-9 * max(1.0, p)


def test_ponding_conserves_volume_on_a_known_basin():
    """A single synthetic basin: stored volume must equal routed inflow."""
    n = 60
    dem = np.full((n, n), 30.0)
    yy, xx = np.mgrid[0:n, 0:n]
    pit = np.exp(-(((yy - 30.0) ** 2 + (xx - 30.0) ** 2) / (2 * 8.0 ** 2))) * 10.0
    dem = dem - pit
    filled = fill_depressions(dem)
    cap = depression_depth(dem, filled)
    sinks = flow_sinks(dem)
    runoff_mm = np.full(dem.shape, 40.0)
    res = run_ponding(dem, filled, cap, sinks, runoff_mm, 900.0)

    assert res.total_stored_m3 > 0.0
    routed = res.routed_inflow_m3
    assert abs(res.total_stored_m3 + res.total_spilled_m3 - routed) / routed < 0.01
    assert res.max_depth_m < 10.0


def test_quality_rejects_missing_crs():
    """Fail-closed: no CRS means no cell area, so the run must be rejected."""
    report = quality.QualityReport()
    quality.check_georeference(None, (1, 0, 0, 0, -1, 0), report)
    assert report.status == "rejected"
    assert any(i.code == "CRS_MISSING" for i in report.fatal)


def test_quality_rejects_imperial_units():
    """A DEM converted to feet blows the regional envelope and must be refused."""
    dem = np.full((30, 30), 50.0)
    dem[10, 10] = 500.0  # 500 m in a plain
    report = quality.QualityReport()
    quality.check_elevation(dem, None, report, envelope_m=(-30.0, 300.0))
    assert report.status == "rejected"
    assert any(i.code == "DEM_OUT_OF_ENVELOPE" for i in report.fatal)


def test_envelope_failure_names_a_caller_supplied_override():
    """A too-narrow override must say so, because it is the caller's to fix.

    Seen in the field: an agent asked to run a rain event on the study area guessed
    a "plausible" 5..70 m band for Nanjing, the gate rejected a perfectly good DEM
    whose true range is 0.7..125.3 m, and the agent — told only "suspect units,
    vertical datum, or wrong tile" — asked the user which of the three it was. The
    one cause it could have fixed itself was the one the message did not mention.
    """
    dem = np.full((30, 30), 50.0)
    dem[0, 0] = 125.3
    dem[0, 1] = 0.7

    overridden = quality.QualityReport()
    quality.check_elevation(dem, None, overridden, envelope_m=(5.0, 70.0),
                            reference_envelope=(-50.0, 400.0))
    issue = next(i for i in overridden.fatal if i.code == "DEM_OUT_OF_ENVELOPE")
    assert "supplied by the caller" in issue.message
    assert "-50.0..400.0" in issue.message
    assert "drop the override" in issue.message
    assert issue.detail["envelope_overridden"] is True
    assert issue.detail["validated_default_envelope"] == [-50.0, 400.0]

    # The default envelope is not an override, so the hint must not appear: it would
    # point the caller at a flag they never set and send them looking for a bug there.
    # This needs a DEM that violates the *default* band — the 0.7..125.3 m one above
    # sits inside it and would never be rejected at all.
    alpine = np.full((30, 30), 50.0)
    alpine[0, 0] = 900.0
    default = quality.QualityReport()
    quality.check_elevation(alpine, None, default, envelope_m=(-50.0, 400.0),
                            reference_envelope=(-50.0, 400.0))
    plain = next(i for i in default.fatal if i.code == "DEM_OUT_OF_ENVELOPE")
    assert "caller" not in plain.message
    assert "envelope_overridden" not in plain.detail


def test_quality_rejects_broken_rainfall():
    report = quality.QualityReport()
    quality.check_rainfall(np.array([[10.0, -1.0]]), report)
    assert report.status == "rejected"

    report2 = quality.QualityReport()
    quality.check_rainfall(np.array([[np.nan, 10.0]]), report2)
    assert report2.status == "rejected"

    report3 = quality.QualityReport()
    quality.check_rainfall(np.array([[0.0, 0.0]]), report3)
    assert report3.status == "degraded" and not report3.fatal


def test_pipeline_fails_closed_without_crs():
    """End to end: broken georeferencing must produce a rejection, not a map."""
    dem = np.full((40, 40), 20.0)
    grid = _grid(dem, crs=None)
    result = run_event(grid, rain_mm=50.0, cn=85.0)
    assert result["outcome"] == "rejected"
    assert result["depth_m"] is None
    assert result["reason"]


def test_pipeline_zero_rain_is_boundary_not_failure():
    dem = np.full((40, 40), 20.0)
    dem[20, 20] = 18.0
    grid = _grid(dem)
    result = run_event(grid, rain_mm=0.0, cn=85.0)
    assert result["outcome"] in ("ok", "degraded")
    assert result["quality"]["status"] == "degraded"  # reported, not hidden
    assert result["ponding"]["pond_area_m2"] == 0.0


def test_water_balance_flags_unit_error():
    """A 1000x cell-area mistake must break the balance, not pass silently."""
    shape = (20, 20)
    rain = np.full(shape, 100.0)
    cn = np.full(shape, 85.0)
    q = scs_cn_runoff(rain, cn)
    # solver thinks the cell area is 1000x larger than it is
    stored = float(q.sum()) * 900.0 * 1000.0 / 1000.0
    bad = water_balance(rain, cn, q, stored_m3=stored, spilled_m3=0.0,
                        outflow_m3=0.0, cell_area_m2=900.0)
    assert not bad["passed"]
    good = water_balance(rain, cn, q, stored_m3=float(q.sum()) * 900.0 / 1000.0,
                         spilled_m3=0.0, outflow_m3=0.0, cell_area_m2=900.0)
    assert good["passed"]


def test_exposure_projection_direction():
    """Regression: world->grid must use the INVERSE affine.

    ``transform * (col, row)`` maps pixel -> world. Using it the other way round
    put every OSM feature tens of millions of pixels off the raster, which the
    report then showed as "0 roads flooded" instead of as an error.
    """
    from affine import Affine

    from floodkit.exposure import _project_many
    from rasterio.warp import transform as wt

    transform = Affine(30.0, 0.0, 500000.0, 0.0, -30.0, 3600000.0)
    x, y = 500030.0, 3599970.0  # exactly pixel centre offset -> (col, row) == (1, 1)
    lon, lat = wt("EPSG:32650", "EPSG:4326", [x], [y])
    grid = _project_many([(lon[0], lat[0])], transform)
    assert abs(grid[0][0] - 1.0) < 1e-6, grid[0]
    assert abs(grid[0][1] - 1.0) < 1e-6, grid[0]


def test_building_impact_uses_pixel_step():
    """Regression: the footprint lattice step is in pixels, not metres.

    A metre-sized step sampled almost nothing inside small urban footprints and
    reported zero affected buildings even where streets were clearly flooded.
    """
    from affine import Affine

    from floodkit.exposure import building_impact
    from rasterio.warp import transform as wt

    transform = Affine(30.0, 0.0, 500000.0, 0.0, -30.0, 3600000.0)
    depth = np.zeros((20, 20))
    depth[10, 10] = 0.5  # one flooded cell, near the corner of a 3x3 cell building

    corners = [(9, 9), (12, 9), (12, 12), (9, 12)]
    xs = [500000.0 + 30.0 * c for c, _ in corners]
    ys = [3600000.0 - 30.0 * r for _, r in corners]
    lon, lat = wt("EPSG:32650", "EPSG:4326", xs, ys)
    elements = [{"id": 1, "tags": {"building": "yes"}, "geometry": [{"lon": a, "lat": b} for a, b in zip(lon, lat)]}]

    res = building_impact(depth, elements, transform, 30.0)
    assert res["buildings_total"] == 1
    assert res["buildings_affected"] == 1, res


def test_water_mask_excludes_lake_from_ponding():
    """A flat surface sitting below its surroundings must not be flooded."""
    n = 60
    dem = np.full((n, n), 40.0)
    dem[20:40, 20:40] = 12.0  # a flat, low, lake-like patch
    dem[15, 15] = 30.0        # a genuine small pit elsewhere
    grid = _grid(dem)
    masked = run_event(grid, rain_mm=120.0, cn=90.0, auto_water_mask=True)
    unmasked = run_event(grid, rain_mm=120.0, cn=90.0, auto_water_mask=False)
    assert masked["domain"]["water_cells"] > 0
    assert masked["domain"]["area_km2"] < unmasked["domain"]["area_km2"]
    # the lake is excluded, so it cannot dominate the depth statistics
    assert masked["ponding"]["max_depth_m"] <= unmasked["ponding"]["max_depth_m"] + 1e-9


def test_landcover_composite_curve_number():
    """Composite CN must follow land cover, not a global constant.

    Regression for the largest modelling simplification in the project: a
    uniform CN treats asphalt and grass as the same surface. Here the west half
    of the tile is built-up (CN 98) and the east half is grassland (CN 74); after
    reprojection onto the model grid the two halves must still be distinguishable.
    """
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import calculate_default_transform

    from floodkit.landcover import curve_number_grid

    west, north = 118.780, 32.060
    res = 1.0 / 12000.0  # ~9.3 m, WorldCover's native grid
    rows, cols = 60, 120
    src_transform = from_origin(west, north, res, res)
    data = np.zeros((rows, cols), dtype="uint8")
    data[:, : cols // 2] = 50  # built-up
    data[:, cols // 2 :] = 30  # grassland

    # Plain os.makedirs inside the project: tempfile.mkdtemp creates a 0700
    # directory whose restrictive DACL the sandboxed process cannot write into.
    scratch = os.path.join(ROOT, "data", "tmp", "landcover-test")
    os.makedirs(scratch, exist_ok=True)
    path = os.path.join(scratch, "worldcover.tif")
    with rasterio.open(
        path, "w", driver="GTiff", height=rows, width=cols, count=1, dtype="uint8",
        crs="EPSG:4326", transform=src_transform,
    ) as dst:
        dst.write(data, 1)

    bounds = (west, north - rows * res, west + cols * res, north)
    dst_transform, width, height = calculate_default_transform(
        "EPSG:4326", "EPSG:32650", cols, rows, *bounds, resolution=30.0
    )
    out = curve_number_grid(path, bounds, dst_transform, (height, width), target_crs="EPSG:32650")

    assert out["water_mask"].sum() == 0
    cn = out["cn"]
    assert cn[:, : width // 4].mean() > 90.0, "built-up half must stay near CN 98"
    assert cn[:, -width // 4 :].mean() < 80.0, "grassland half must stay near CN 74"
    assert out["impervious_fraction"][:, : width // 4].mean() > 0.5
    assert out["impervious_fraction"][:, -width // 4 :].mean() < 0.1


def test_pipeline_rejects_feet_dem():
    """A DEM whose elevations are 3.28x too large must be refused, not modelled.

    Regression for a silent-wrongness bug: the CLI shipped its own envelope
    defaults (-30..800) which overrode the library's tightened study-area
    envelope (-50..400), so a feet-valued DEM sailed through the quality gate
    and produced a full flood map with 3.28x the real relief.
    """
    n = 40
    dem = np.full((n, n), 30.0)
    dem[10:30, 10:30] = 150.0
    feet = dem * 3.28084  # 492 m in "metres" -> outside any Jiangsu envelope
    grid = _grid(feet)
    result = run_event(grid, rain_mm=100.0, cn=85.0)
    assert result["outcome"] == "rejected"
    assert result["depth_m"] is None
    assert any(i["code"] == "DEM_OUT_OF_ENVELOPE" for i in result["quality"]["issues"])


def test_cli_envelope_matches_library_default():
    """Regression: the two envelope defaults must not be able to drift apart."""
    from argparse import Namespace

    from floodkit.cli import _envelope
    from floodkit.pipeline import DEFAULT_ENVELOPE

    assert _envelope(Namespace(envelope_min=None, envelope_max=None)) == DEFAULT_ENVELOPE
    assert _envelope(Namespace(envelope_min=-10.0, envelope_max=250.0)) == (-10.0, 250.0)


def test_underpass_and_critical_facility_impact():
    """Below-grade roads and life-safety facilities must be identified separately.

    An inundation map that only says "some roads are wet" understates the risk:
    a flooded underpass fills from both ends with no escape, and a flooded subway
    entrance is an ingress path into the underground network. Both need their own
    classification, not a place in the generic road list.
    """
    from affine import Affine

    from floodkit.exposure import critical_facility_impact, underpass_impact
    from rasterio.warp import transform as wt

    transform = Affine(30.0, 0.0, 500000.0, 0.0, -30.0, 3600000.0)
    depth = np.zeros((20, 20))
    depth[5, 5] = 0.8

    def to_ring(pixel_points):
        xs = [500000.0 + 30.0 * c for c, _ in pixel_points]
        ys = [3600000.0 - 30.0 * r for _, r in pixel_points]
        lon, lat = wt("EPSG:32650", "EPSG:4326", xs, ys)
        return [{"lon": a, "lat": b} for a, b in zip(lon, lat)]

    # an underpass crossing the flooded cell (geometry at the cell centre, which
    # is where real OSM points fall; cell corners are the float-truncation edge)
    underpass = {
        "id": 1,
        "tags": {"highway": "primary", "name": "测试隧道", "tunnel": "yes", "layer": "-1"},
        "geometry": to_ring([(3, 5.5), (8, 5.5)]),
    }
    surface = {
        "id": 2,
        "tags": {"highway": "primary", "name": "地面路"},
        "geometry": to_ring([(15, 15.5), (18, 18.5)]),
    }
    result = underpass_impact(depth, [underpass, surface], transform, 30.0)
    assert result["underpass_ways"] == 1, "only the tunnel-tagged way is below grade"
    assert result["underpass_ways_flooded"] == 1
    assert result["life_safety_count"] == 1
    assert result["top_segments"][0]["name"] == "测试隧道"

    # a subway entrance inside the flooded cell vs a hospital outside it
    def node(element_id, row, col, tags):
        lon, lat = wt("EPSG:32650", "EPSG:4326", [500000.0 + 30.0 * col], [3600000.0 - 30.0 * row])
        return {"type": "node", "id": element_id, "lon": lon[0], "lat": lat[0], "tags": tags}

    facilities = [
        node(10, 5, 5, {"railway": "subway_entrance", "name": "测试站#1"}),
        node(11, 15, 15, {"amenity": "hospital", "name": "测试医院"}),
    ]
    fac = critical_facility_impact(depth, facilities, transform, 30.0)
    assert fac["facilities_total"] == 2
    assert fac["facilities_flooded"] == 1
    assert fac["subway_entrances_flooded"] == 1
    assert fac["top_facilities"][0]["name"] == "测试站#1"


def test_report_excludes_rasters_and_private_keys():
    """The JSON report must carry evidence, not payloads.

    Regression: replacing the old `k != "depth_m"` rule with "drop underscore
    keys" silently started serialising the whole depth raster as a nested list,
    growing every report from ~156 KB to ~1.59 MB. Both exclusions now apply.
    """
    import json
    import os
    import tempfile

    from floodkit.pipeline import EXCLUDED_FROM_REPORT, _write_json

    result = {
        "outcome": "ok",
        "depth_m": np.zeros((5, 5)),
        "_osm_elements": {"roads": [{"big": "object"}]},
        "ponding": {"pond_area_m2": 1.0},
    }
    assert "depth_m" in EXCLUDED_FROM_REPORT

    scratch = os.path.join(ROOT, "data", "tmp", "report-test")
    os.makedirs(scratch, exist_ok=True)
    path = _write_json(result, scratch, "unit")
    written = json.loads(open(path, encoding="utf-8").read())
    assert "depth_m" not in written, "the raster must live in the GeoTIFF, not the JSON"
    assert not [k for k in written if k.startswith("_")], "private working keys must not serialise"
    assert written["outcome"] == "ok"
    _ = tempfile


def test_basin_spills_when_inflow_exceeds_capacity():
    """The spill branch must balance: stored + spilled == routed inflow.

    At the flagship 100 mm scenario nothing overflows, so the code handling a
    depression that fills past its spill level never ran; a rainfall sweep showed
    the branch only wakes up at 150 mm and above.

    The inputs are built directly rather than derived from synthetic terrain.
    Deriving them is a trap: any depression whose drainage ends inside the grid is
    a *closed* bowl, so its capacity is the volume of the whole bowl and no
    plausible rainfall ever overflows it — a case that looks like a spill test and
    silently is not one. Building the stage-volume inputs explicitly tests the
    unit that actually owns the behaviour.
    """
    n, cell_area = 40, 900.0
    dem = 100.0 - 0.25 * np.arange(n)[None, :].repeat(n, axis=0)  # slopes down to the outlet
    filled = dem.copy()
    capacity = np.zeros_like(dem)
    capacity[19:22, 19:22] = 2.0  # one 3x3 depression, 2 m of storage, nothing else
    sinks = np.full(dem.shape, 20 * n + 10, dtype=np.int64)  # every cell belongs to it

    runoff_mm = np.full(dem.shape, 200.0)
    expected_inflow = runoff_mm.sum() / 1000.0 * cell_area
    result = run_ponding(dem, filled, capacity, sinks, runoff_mm, cell_area)

    assert abs(result.routed_inflow_m3 - expected_inflow) / expected_inflow < 1e-9
    assert result.total_spilled_m3 > 0.0, "this case is supposed to overflow"
    assert result.overflowing_basins >= 1
    assert abs(result.total_stored_m3 + result.total_spilled_m3 - result.routed_inflow_m3) < 1e-6
    assert abs(result.basin_accounting_gap_m3) < 1e-6


def test_scs_retention_is_pinned_by_physical_anchors():
    """The 254 mm offset is the whole point of the TR-55 retention formula.

    The identity test above is self-referential: it derives its expected Ia from
    `potential_retention` itself, so dropping the `- 254` moves both sides together
    and the test still passes. Mutation testing found exactly that survivor. These
    anchors are physical facts instead, so no implementation can satisfy them by
    being consistently wrong.

    CN 100 is a fully impervious surface: no retention, and every millimetre of
    rain above the initial abstraction runs off.
    """
    assert abs(float(potential_retention(100.0)) - 0.0) < 1e-9
    assert abs(float(potential_retention(50.0)) - 254.0) < 1e-9  # 25400/50 - 254
    assert abs(float(initial_abstraction(100.0)) - 0.0) < 1e-9

    for p in (1.0, 25.0, 50.0, 120.0):
        assert abs(float(scs_cn_runoff(p, 100.0)) - p) < 1e-9, p
        assert abs(float(scs_cn_retention(p, 100.0))) < 1e-9, p


def test_curve_number_is_clamped_into_range():
    """An out-of-range CN must not leak inf or negative retention into a run.

    Mutation testing found this untested: widening the clamp to 0..1000 changed no
    result the suite checked, because nothing exercised a CN outside 1..100.
    """
    assert float(potential_retention(0.0)) == float(potential_retention(1.0))
    assert float(potential_retention(-5.0)) == float(potential_retention(1.0))
    assert float(potential_retention(150.0)) == float(potential_retention(100.0))
    assert np.isfinite(potential_retention(np.array([-1.0, 0.0, 200.0, 1e6]))).all()
    assert float(scs_cn_runoff(100.0, 0.0)) == float(scs_cn_runoff(100.0, 1.0))
    assert float(scs_cn_runoff(100.0, 1e6)) == float(scs_cn_runoff(100.0, 100.0))


def test_shallow_and_mixed_depressions_still_balance():
    """Two fixes that no test held in place until mutation testing found them.

    1. A basin whose storage is only 3 cm deep must still be reported. Measuring
       "deep enough to count" with the same threshold used to *decide* a basin is
       worth reporting quietly deletes wide, shallow depressions — which are the
       common case in a city, not the exotic one.

    2. Water is placed on the fill-defined footprint, not on every cell the D8
       routing assigned to the catchment. A cell whose flow path ends at the floor
       but whose fill level equals its own ground can drain away by another route;
       giving it water stored more than was routed in and produced the whole 2.5%
       water-balance residual on the larger study area.
    """
    # (1) a 2x2 basin holding 3 cm of water, below the 5 cm reporting threshold
    n = 12
    dem = np.full((n, n), 10.0)
    dem[5:7, 5:7] = 10.0 - 0.03
    filled = np.full((n, n), 10.0)
    capacity = np.zeros((n, n))
    capacity[5:7, 5:7] = 0.03
    sinks = np.full((n, n), 5 * n + 5, dtype=np.int64)
    runoff_mm = np.full((n, n), 1.0)
    result = run_ponding(dem, filled, capacity, sinks, runoff_mm, 900.0)

    assert len(result.basins) == 1, "a 3 cm deep basin must still be reported"
    assert result.basins[0].stored_m3 > 0.0
    assert result.skipped_small == 0

    # (2) a group mixing real storage with a cap==0 cell sitting below the stage.
    #     Cell 4 is the awkward one: its flow path ends at the floor, but its fill
    #     level equals its own ground, so it can drain away by another route.
    n2 = 10
    dem2 = np.array([0.0, 1.0, 2.0, 3.0, 1.0, 5.0, 6.0, 7.0, 8.0, 9.0])
    capacity2 = np.array([2.0, 0.5, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    filled2 = dem2 + capacity2
    sinks2 = np.zeros(n2, dtype=np.int64)  # one catchment for all of them
    runoff2 = np.full(n2, 2000.0)          # far more water than the 4 cells can hold
    result2 = run_ponding(
        dem2.reshape(1, n2), filled2.reshape(1, n2), capacity2.reshape(1, n2),
        sinks2.reshape(1, n2), runoff2.reshape(1, n2), 900.0,
    )
    assert len(result2.basins) == 1
    assert result2.total_spilled_m3 > 0.0
    gap = result2.total_stored_m3 + result2.total_spilled_m3 - result2.routed_inflow_m3
    assert abs(gap) < 1e-6 * max(1.0, result2.routed_inflow_m3), gap
    assert abs(result2.basin_accounting_gap_m3) < 1e-6


def test_place_resolution_is_nanjing_only():
    """A place name must resolve to a validated box, or to nothing at all.

    The dangerous outcome is not a refusal, it is a confident box for a city the
    model was never calibrated on: curve numbers, the elevation envelope and the
    exposure layers are all Nanjing-specific, so an unchecked box would produce a
    full-looking report with no valid basis. Every case here is offline.
    """
    from floodkit import places

    flagship = places.resolve("南京主城区")
    assert flagship["bounds"] == [118.72, 32.02, 118.82, 32.10]
    assert flagship["approximate"] is False
    # An alias must land on the same box, or "南京" and "南京主城区" would give
    # different answers to the same question.
    for alias in ("南京", "南京市", "南京市区", "主城区"):
        assert places.resolve(alias)["bounds"] == flagship["bounds"], alias

    for foreign in ("北京", "上海", "广州", "Nanjing is not this", "火星"):
        try:
            places.resolve(foreign)
        except places.PlaceError:
            continue
        raise AssertionError("%r should not resolve to a study area" % foreign)


def test_place_rejects_out_of_scope_bounds():
    """Explicit bounds are a deliberate choice, but still have to be in Nanjing."""
    from floodkit import places

    assert places.validate_bounds([118.75, 32.03, 118.80, 32.07]) == [118.75, 32.03, 118.80, 32.07]
    for bad, why in (
        ([116.35, 39.86, 116.45, 39.95], "outside Nanjing"),
        ([118.82, 32.10, 118.72, 32.02], "east/south inverted"),
        ([118.770, 32.060, 118.775, 32.065], "too small to analyse"),
        ([118.0, 32.0, 118.0, 32.1], "zero width"),
    ):
        try:
            places.validate_bounds(bad)
        except places.PlaceError:
            continue
        raise AssertionError("%s: %r should be rejected" % (why, bad))


def test_weather_scope_is_fifteen_days():
    """The project window is 15 days; the boundary must be inclusive.

    An off-by-one here refuses the last legitimate day, and because the limit is
    the only refusal reason (the scope sits inside the 16-day model horizon) a
    wrong edge would look exactly like normal operation. Pure function, no network.
    """
    from datetime import datetime, timedelta

    from floodkit import weather

    now = datetime(2026, 10, 10, 14, 0)
    assert weather.SCOPE_DAYS_AHEAD == 15
    assert weather.check_time_scope(now + timedelta(days=15), now) is None
    assert weather.check_time_scope(now + timedelta(hours=1), now) is None
    # A time earlier today is still reviewable; last week is not.
    assert weather.check_time_scope(now - timedelta(hours=12), now) is None

    for moment in (now + timedelta(days=15, hours=1), now + timedelta(days=16),
                   now + timedelta(days=400), now - timedelta(days=2), now - timedelta(days=200)):
        refusal = weather.check_time_scope(moment, now)
        assert refusal is not None, moment
        assert refusal["code"] == "TIME_OUT_OF_SCOPE"
        # The refusal has to name the window, or the caller can only say "no".
        assert refusal["scope"]["latest"] and refusal["scope"]["earliest"]

    # The scope has to be enforced by the entry point, not merely available as a
    # helper: a correct check_time_scope that window_forecast never calls would
    # leave every out-of-scope request reaching the network. Testing the helper
    # alone cannot see that, so drive the real entry point. This stays offline
    # because the scope test runs before any request is made.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = weather.window_forecast(32.06, 118.7778,
                                  (now + timedelta(days=16)).strftime("%Y-%m-%dT12:00"), root, now=now)
    assert out["ok"] is False, out
    assert out["code"] == "TIME_OUT_OF_SCOPE", out
    assert "per_model_mm" not in out


def test_weather_rejects_bad_time_without_network():
    """A malformed time must fail on parsing, before any request is made."""
    from floodkit import weather

    out = weather.window_forecast(32.06, 118.7778, "明天中午", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert out["ok"] is False
    assert out["code"] == "BAD_TIME"
    assert "min_mm" not in out


def test_weather_snaps_a_time_with_minutes_to_its_hour():
    """A time carrying minutes must be served by the containing hour, not refused.

    Seen in the field: the hourly series is keyed on the hour ("2026-10-26T10:00"),
    while the requested window was built minute-accurately ("2026-10-26T10:14"), so
    no stamp ever matched. Every model came back None and the request was reported as
    OUT_OF_HORIZON — whose message then compared 10:14 against a data horizon of
    23:00 and contradicted itself. A time that was in range and fully served read as
    beyond the forecast. Offline: fetch_hourly is replaced, so no request is made.
    """
    from datetime import datetime

    from floodkit import weather

    now = datetime(2026, 10, 10, 14, 0)
    stamps = ["2026-10-10T%02d:00" % hour for hour in range(24)]
    stamps += ["2026-10-11T%02d:00" % hour for hour in range(24)]
    payload = {"hourly": {"time": stamps}}
    for name in weather.DEFAULT_MODELS:
        payload["hourly"][weather._key(name)] = [1.0] * len(stamps)

    original = weather.fetch_hourly
    weather.fetch_hourly = lambda *a, **k: {
        "ok": True, "payload": payload, "source": "fixture", "retrieved_at": "2026-10-10T14:00",
    }
    try:
        out = weather.window_forecast(32.06, 118.7778, "2026-10-11T09:14",
                                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      now=now)
    finally:
        weather.fetch_hourly = original

    assert out["ok"] is True, out
    assert out["window"]["start"] == "2026-10-11T09:00", out["window"]
    # The caller has to be able to say which hour it actually got.
    assert out["window"]["snapped_from"] == "2026-10-11T09:14", out["window"]
    assert out["min_mm"] == 1.0 and out["max_mm"] == 1.0, out

    # An on-the-hour request is untouched, and must not claim to have been snapped:
    # an always-present key would make the field useless as a signal.
    weather.fetch_hourly = lambda *a, **k: {
        "ok": True, "payload": payload, "source": "fixture", "retrieved_at": "2026-10-10T14:00",
    }
    try:
        exact = weather.window_forecast(32.06, 118.7778, "2026-10-11T09:00",
                                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        now=now)
    finally:
        weather.fetch_hourly = original
    assert exact["ok"] is True, exact
    assert exact["window"]["start"] == "2026-10-11T09:00"
    assert exact["window"]["snapped_from"] is None


def test_osm_extract_is_chosen_by_reproducing_the_report_count():
    """Selection must be by count, because geometry alone cannot identify the extract.

    Two real caches are present (a 76 km² city box and a 405 km² wide box) and the
    grid is aligned in UTM, so its footprint overhangs the requested lon/lat box by up
    to a cell. A cover test therefore matches *nothing*, and the old fallback took the
    smallest variant — so the wide run silently drew the city buildings layer. The
    exporter's count check caught it; the map and the interactive page have no such
    check, which is what makes this worth a test.

    Built from the caches actually on disk: with fewer than two variants the test
    cannot distinguish a correct rule from a constant, so it says so instead of
    passing vacuously.
    """
    from floodkit import runio

    variants = runio.osm_variants("buildings")
    if len(variants) < 2:
        print("     (skipped: needs two cached buildings extracts, found %d)" % len(variants))
        return

    by_area = sorted(variants, key=lambda c: c["area"])
    small, large = by_area[0], by_area[-1]

    # The predicate has to reproduce a count the pipeline itself computes, otherwise
    # "match the report" selects nothing and the run stops rather than drawing wrong.
    counts = {c["path"]: runio.building_total_count(c["path"]) for c in variants}
    assert len(set(counts.values())) == len(variants), counts

    # A report whose buildings_total came from the large extract must pick it, even
    # though its bounds are the ones the small extract was fetched for plus an overhang.
    fake = {"exposure": {"buildings": {"buildings_total": counts[large["path"]]}}}
    assert runio.select_osm(fake, small["box"], verbose=False)["buildings"] == large["path"]

    fake_small = {"exposure": {"buildings": {"buildings_total": counts[small["path"]]}}}
    assert runio.select_osm(fake_small, large["box"], verbose=False)["buildings"] == small["path"]

    # With no count to match on, the fallback must choose the largest extract: a
    # superset is clipped by the axes, a subset blanks part of the map silently.
    assert runio.pick_covering("buildings", large["box"]) == large["path"]

    # And a count that matches no cache must stop the run, not fall back to a subset.
    try:
        runio.pick_by_count("buildings", counts[large["path"]] + 1, runio.building_total_count)
    except SystemExit as exc:
        assert "reproduces the report's count" in str(exc), exc
    else:
        raise AssertionError("an unmatched count must fail closed")


def test_launcher_scripts_keep_their_required_encoding():
    """The two encodings that silently brick a Windows script.

    1. A .ps1 containing non-ASCII MUST start with a UTF-8 BOM. Windows
       PowerShell 5.1 reads a BOM-less file as ANSI, so a Chinese string or a
       checkmark becomes mojibake and the parser dies with "The string is
       missing the terminator" far from the real cause. This happened: editing
       the file with a tool that does not preserve the BOM broke
       scripts/package-submission.ps1 while every other check still passed.

    2. A .cmd MUST be pure ASCII with CRLF endings. cmd.exe mis-parses an
       LF-only batch file, splitting lines and running fragments as commands --
       also observed here, and equally hard to read from the symptom.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    problems = []

    for name in ("setup.ps1", "package-submission.ps1"):
        path = os.path.join(root, "scripts", name)
        if not os.path.exists(path):
            problems.append("%s is missing" % name)
            continue
        raw = open(path, "rb").read()
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append("%s is not valid UTF-8" % name)
            continue
        if not all(b < 128 for b in raw) and not raw.startswith(b"\xef\xbb\xbf"):
            problems.append("%s has non-ASCII text but no UTF-8 BOM" % name)

    for name in ("启动AGH.cmd", "安装插件.cmd"):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            problems.append("%s is missing" % name)
            continue
        raw = open(path, "rb").read()
        if not all(b < 128 for b in raw):
            problems.append("%s must stay pure ASCII (the console code page would garble it)" % name)
        if raw.count(b"\n") != raw.count(b"\r\n"):
            problems.append("%s has bare LF endings; cmd.exe mis-parses those" % name)

    assert not problems, "; ".join(problems)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main() -> int:
    failures = []
    for fn in TESTS:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except AssertionError as exc:
            failures.append((fn.__name__, str(exc) or "assertion failed"))
            print("FAIL %s -- %s" % (fn.__name__, exc))
        except Exception as exc:  # noqa: BLE001
            failures.append((fn.__name__, "%s: %s" % (type(exc).__name__, exc)))
            print("ERROR %s -- %s: %s" % (fn.__name__, type(exc).__name__, exc))
    print("\n%d/%d passed" % (len(TESTS) - len(failures), len(TESTS)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

# Architecture

[中文版](ARCHITECTURE.zh-CN.md) · [Integration standard](INTEGRATION.md) · [Project overview](../README.md)

The system is built as **three layers with one rule between them**: the model may
decide *what* to compute, but it never decides *what is correct*. Correctness is
computed twice, in two languages.

```
              user task  (a place + a rainfall scenario + a decision question)
                                    │
                 ┌──────────────────▼───────────────────┐
                 │  Layer 3 — agent (Agnes Harness)     │
                 │  plans → calls tools → reads results │
                 │  → validates → retries on failure    │
                 └──────────────────┬───────────────────┘
                                    │  8 tools, schema-checked,
                                    │  writes require human approval
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
 flood_selftest              flood_run_event            flood_verify_report
 flood_resolve_place         flood_fetch_dem            flood_publish_report
 flood_weather               flood_crosslang_check
        │                           │                           │
        └─────────────┬─────────────┴─────────────┬─────────────┘
                      ▼                           ▼
        Layer 1 — floodkit (Python)   Layer 2 — verify-core.mjs (JavaScript)
        deterministic hydrology       independent re-derivation
```

## Layer 1 — the deterministic core (`floodkit/`)

Pure computation. No model, no network call in the arithmetic path, no UI. Running
`python -m floodkit run …` reproduces every number in the shipped reports without
Agnes, without an API key, and without the agent.

| Module | Responsibility |
|---|---|
| `dem_source.py` | Discover, download and cache Copernicus GLO-30 tiles |
| `places.py` | Place name → study-area bounding box. Nanjing only; it refuses rather than guesses |
| `weather.py` | Forecast rainfall as a range across three independent models; 15-day horizon |
| `quality.py` | Input quality gate. Severity `fatal` means **no raster is produced** |
| `terrain.py` | Priority-Flood fill, basin labelling, D8 flow, stage–volume inversion |
| `landcover.py` | ESA WorldCover 10 m → composite curve number (TR-55, hydrologic soil group C) |
| `hydro.py` | SCS-CN runoff, per-basin water-level equilibrium, domain water balance |
| `scenario.py` | Rainfall scenario definition |
| `exposure.py` | OSM impact analysis: roads, buildings, underpasses, subway entrances, schools, hospitals |
| `runio.py` | **Single source of truth** for reading a completed run: raster, bounds, OSM cache selection, depth classes |
| `pipeline.py` | End-to-end orchestration and report assembly |
| `cli.py` | Command-line entry point. Every subcommand emits JSON on stdout |
| `samples.py` | Three classes of test case: normal / boundary / failure |

### The physical chain

```
DEM (GLO-30, 30 m)
  → metric projection to UTM 50N (EPSG:32650)
      geographic cells shrink with latitude; areas and flow direction are wrong
      until the grid is metric
  → quality gate (CRS, geotransform, units, envelope, holes, rain, CN, domain)
  → Priority-Flood fill          removes noise pits that would break flow routing
  → D8 flow routing              each cell drains to its lowest of 8 neighbours
  → basin units                  low point + every cell that feeds it
  → ESA WorldCover 10 m          → composite curve number CN
  → SCS-CN runoff                P = Ia + F + Q
  → stage–volume balance         inflow = stored + spilled + outflow, per basin
  → per-cell depth               water level − terrain
  → verification                 five indicators, computed independently
  → OSM exposure                 which named road / building / underpass / entrance
```

Measured shape of the shipped Nanjing run: **341 × 321 cells**, cell area
**789.7 m²** (28.1 m grid), 96,644 valid cells, **76.32 km²** study area,
**2,320** basin units after screening (5,877 sub-threshold depressions still
accounted for in the balance), **7.81 km²** ponded after a 100 mm event.

## Layer 2 — the independent verifier (`agh-plugin/verify-core.mjs`)

A second implementation of the same physics, written from scratch in JavaScript.
It re-derives SCS-CN over **24 cases** and the stage–volume inversion over **10
cases** (empty basin, exactly full, over-capacity truncation, flat tied
elevations, single cell, zero capacity). Worst water-level disagreement:
**8.9e-16 m**.

Why a second language rather than a second function:

- **The same code run twice proves nothing.** It demonstrates repeatability, not
  correctness. A second implementation must independently reach the same answer.
- **It is not a one-off script.** It runs inside the agent's own tool chain
  (`flood_crosslang_check`), so every result is re-derived at the moment it is
  produced rather than at some earlier date.
- **It found three real divergences**, including a flat-tied-elevation case that
  only an edge case exposes.

Its limit is stated plainly: if both implementations share a *misunderstanding*,
cross-language checking cannot see it. Analytic checks cover that gap, which is
why the selftest compares against closed-form solutions rather than against each
other.

## Layer 3 — the agent (`agh-plugin/index.mjs`)

Eight tools registered on the harness. The metadata is not decoration: it is what
makes the agent's autonomy auditable.

| Tool | Purpose | Side effects |
|---|---|---|
| `flood_selftest` | Analytic checks before computing anything | read-only |
| `flood_resolve_place` | Place name → study area. Nanjing only, refuses otherwise | read-only |
| `flood_weather` | Forecast as a range across three independent models | network + cache |
| `flood_fetch_dem` | Fetch DEM tiles for a bounding box | network + cache, needs approval |
| `flood_run_event` | Run one rainfall event end to end | writes artifacts, needs approval |
| `flood_publish_report` | Render the interactive page and serve it on loopback | writes a derived page |
| `flood_verify_report` | Re-derive the mass balance from the report, in JavaScript | read-only |
| `flood_crosslang_check` | Re-derive the solver itself, in JavaScript | read-only |

Two of those exist so the agent can question its **input**, not only its output:
`flood_resolve_place` stops it inventing a study area, and `flood_weather` lets it
notice that the rainfall the user asserted is not what the models forecast.
Checking the arithmetic was never the weakness; checking the premise is.

## Where verification lives

The five indicators are not appendices to the report — each has an owner, a
tolerance and a place where it can be independently recomputed.

| Indicator | Tolerance | Lives in | Recomputable by |
|---|---|---|---|
| D01 domain water balance | ≤ 1 % | `report.json` → `water_balance` | `flood_verify_report` (JS) |
| D01b raster vs solver | ≤ 1e-6 | `report.json` → `water_balance` | re-integrating the delivered GeoTIFF |
| D02 external checkpoints | ≥ 70 % | `report.json` → `checkpoints` | the published point list |
| D02b ranking (AUC + CI) | reported | `report.json` → `checkpoint_ranking` | resampling the same points |
| D03 cross-language | ≤ 0.05 m | agent tool chain, not the JSON | `flood_crosslang_check` (JS) |
| D04 sensitivity | monotone, ≤ 45 % | `report.json` → `sensitivity` | re-running with perturbed CN |

D03 is deliberately absent from `report.json`: it is produced *inside the tool
chain* on every run, so it cannot go stale the way a stored result can.

## Design decisions and their costs

| Decision | Reason | What it costs |
|---|---|---|
| Static storage balance, not a hydrodynamic model | A hydrodynamic model needs Manning coefficients and conduit roughness, which public data cannot supply. Unrated parameters produce numbers that *look* validated | no velocity, no travel time, no river backwater |
| No drainage network | The network is not public at street resolution | results **over-estimate**: water that would drain is treated as ponded |
| UTM 50N metric grid | Geographic cells change area with latitude | one projection is valid for eastern China, not globally |
| 28.1 m cells | Follows from GLO-30 reprojection | street-level curbs and inlets are not resolvable |
| `runio.py` as the only reader | Depth classes, colours, bounds and exposure counts must agree across map, HTML, GeoJSON and figures | a new consumer must go through it rather than parse a file directly |
| Refuse instead of guess | A plausible wrong answer is worse than a refusal | the agent fails on requests that a sloppier system would answer |

## Failure philosophy

Three rules, each enforced in code rather than in prose:

1. **A fatal quality issue means no raster.** The run returns `outcome:
   "rejected"` with a machine-readable reason. It does not downgrade silently.
2. **A failed indicator is shown as failed.** Renderers assert that the page and
   the map agree with `report.json` field by field; a renderer that turns a red
   indicator green refuses to produce output.
3. **Unvalidated scope is refused.** The study area is Nanjing; the forecast
   horizon is 15 days. Both are checked, and both fail closed.

## Reproducibility, as measured rather than claimed

- A clean copy of the submitted source tree re-runs the whole pipeline.
- A cold start (no cache) downloads ~186 MB and takes ~107 s; a warm run takes
  ~16 s. The numbers are the same either way.
- `python scripts/rebuild_outputs.py` rebuilds every artifact and then runs the
  full verification chain, including the agent's execution record.
- `tests/check_doc_consistency.py` compares the numbers quoted in the
  documentation against the report JSON, so a document cannot drift from the run
  it describes.

# Integration standard

[中文版](INTEGRATION.zh-CN.md) · [Architecture](ARCHITECTURE.md) · [Project overview](../README.md)

This document defines what another system may rely on when integrating `floodkit`
or the Agnes Harness plug-in, and what an integrator **must not** break. The
keywords MUST, MUST NOT, SHOULD and MAY are used in the usual RFC 2119 sense.

The whole point of this standard is one sentence: **a downstream consumer must be
able to tell a real result from a plausible one.** Every rule below exists to
preserve that ability.

---

## 1. Guarantees

An integrator MAY rely on the following. Each is covered by an automated check.

| Guarantee | Checked by |
|---|---|
| A `fatal` input problem produces **no depth raster** | `tests/test_floodkit.py`, `floodkit samples` |
| `report.json` is emitted for every run, including rejected ones | `tests/verify_package.py` |
| Every number in `report.json` is derivable from the run's own inputs and raster | `flood_verify_report` (JavaScript), `D01b` |
| Documentation figures agree with the report JSON | `tests/check_doc_consistency.py` |
| The interactive page contains no network request | `tests/verify_report_html.py` |
| Vector outputs reconcile with the report | `tests/verify_geojson.py` |

An integrator MUST NOT rely on: accuracy, velocity or timing fields (they do not
exist), drainage behaviour (not modelled), or coverage outside Nanjing.

---

## 2. Interface surfaces

There are exactly four. Nothing else is a supported interface.

### 2.1 Command line

```powershell
python -X utf8 -m floodkit run       --bounds W S E N --rain-mm R --tag TAG [--exposure]
python -X utf8 -m floodkit selftest
python -X utf8 -m floodkit samples   [--cn-source landcover]
python -X utf8 -m floodkit resolve   --name 南京
python -X utf8 -m floodkit weather   --bounds W S E N
python -X utf8 -m floodkit verify    --tag TAG
```

**Contract:**

- Every subcommand writes a single JSON object to **stdout**. Diagnostics go to
  stderr. An integrator MUST parse stdout, not scrape log text.
- Exit codes are part of the contract:

| Exit | Meaning | Caller should |
|---|---|---|
| `0` | Success — `outcome` is `ok` or `degraded` | consume the result, but check `quality.issues` |
| `2` | Usage or input error (missing args, unparsable value) | fix the call; retrying unchanged cannot help |
| `3` | Unresolved or rejected — the run refused to produce a result | surface the `code` to the user; do **not** retry blindly |
| `4` | Verification failed (`selftest`, `verify`) | treat the installation as broken |
| `5` | One or more `samples` cases did not match its declared expectation | treat the installation as broken |

- A non-zero exit MUST be treated as "no result", never as "partial result".

### 2.2 Report JSON (`data/runs/<tag>/<tag>_report.json`)

Stable top-level keys. An integrator MUST tolerate additional keys.

| Key | Content |
|---|---|
| `outcome` | `ok` \| `degraded` \| `rejected` |
| `status` | Mirrors `quality.status` |
| `quality` | `{status, issues[]}` — each issue `{code, severity, message, detail}` |
| `domain` | `rows, cols, cells, valid_cells, cell_area_m2, resolution_m[], crs, area_km2` |
| `run_config` | `bounds_wgs84[4], rain_mm, cn, cn_source, min_depth_m, envelope_m[2], checkpoints, exposure` |
| `water_balance` | `rain/initial_abstraction/continuing_retention/generated_runoff/stored/spilled/outflow/residual`, `relative_error`, `raster_vs_solver_relative_error`, `tolerance`, `passed` |
| `ponding` | Areas, depths, basin counts, `total_spilled_m3`, `overflowing_basins` |
| `terrain`, `runoff`, `landcover` | Inputs actually used, so a run can be re-derived |
| `sensitivity` | `fraction, variants[], monotone, max_relative_area_change, tolerance, passed` |
| `checks` | Machine-readable verdicts per indicator |
| `exposure` | `available`, `source`, `roads`, `underpasses`, `buildings`, `critical_facilities` |

**Rules:**

- `quality.issues[].severity` MUST be one of `fatal`, `warning`, `info`.
- A `fatal` issue MUST be accompanied by `outcome: "rejected"` **and the absence of
  a depth raster**. A consumer that finds both a raster and a fatal issue MUST
  treat the run as invalid.
- Volume fields are cubic metres; depths are metres; areas are square metres
  unless the key name says otherwise. An integrator MUST NOT re-scale values by
  guessing units.
- `water_balance.relative_error` is a *dimensionless fraction*, and a value of
  `0.000000` in the rendered report is `2.4e-16` in the JSON. Treat it as machine
  precision, not as an exact zero.

### 2.3 Harness tools

Eight tools, registered by `agh-plugin/index.mjs`. Metadata is normative:

| Tool | `isReadOnly` | `requiresApproval` | `replay` |
|---|---|---|---|
| `flood_selftest` | true | `never` | `safe` |
| `flood_resolve_place` | true | `never` | `safe` |
| `flood_verify_report` | true | `never` | `safe` |
| `flood_crosslang_check` | true | `never` | `safe` |
| `flood_weather` | false (writes cache) | `never` | `safe` |
| `flood_publish_report` | false (writes derived page) | `never` | `safe` |
| `flood_fetch_dem` | false | `always` | `never` |
| `flood_run_event` | false | `always` | `never` |

**Rules:**

- Every tool definition MUST declare **all eight** metadata keys. A missing key
  fails the extension factory with `E_TOOLDEF_META` and the plug-in never leaves
  `starting`.
- Parameter schemas MUST set `additionalProperties: false`. An integrator MUST NOT
  bypass schema validation before calling a tool.
- A tool that writes files MUST declare `replay: "never"` so a crash cannot
  silently re-run it and synthesise an unknown outcome.
- `requiresApproval: "always"` MUST NOT be relaxed to make an automated demo
  pass. Human-in-the-loop on write operations is a submitted property of the
  system.

### 2.4 Artifacts

Given a run tag `TAG`, the following are produced in `data/runs/TAG/`:

```
TAG_report.json        machine-readable result (always, even when rejected)
TAG_report.md          human-readable summary
TAG_depth.tif          depth raster (never when outcome is rejected)
TAG_map.png            DEM + depth diagnostic
TAG_critical.png       risk-point map
TAG_report.html        single-file offline interactive report
TAG_ponding.geojson    depth polygons
TAG_exposure.geojson   exposed assets
TAG_report_map.png/.pdf/.svg   print-grade map (on demand)
```

An integrator MUST NOT assume any given artifact exists except `_report.json`.
Existence of every other file is conditional on `outcome` and on which command was
run.

---

## 3. Error taxonomy

Machine-readable `code` values. An integrator SHOULD map `fatal` codes to a user
message verbatim rather than inventing one — they are written to be read.

**Input quality (severity)**

| Code | Severity | Meaning |
|---|---|---|
| `CRS_MISSING`, `TRANSFORM_MISSING`, `TRANSFORM_DEGENERATE`, `TRANSFORM_ROTATED`, `TRANSFORM_TINY_CELL` | fatal | The raster cannot be placed or is degenerate |
| `DEM_ALL_NODATA` | fatal | No valid elevation at all |
| `DEM_UNIT_DECLARED_NON_METRIC` | fatal | Units are feet or similar |
| `DEM_OUT_OF_ENVELOPE` | fatal | Elevation range is implausible for the study area (suspect units, datum or wrong tile) |
| `DEM_HOLES` | warning | Missing cells, treated as open drains — reported, not hidden |
| `DEM_FLAT` | warning | Terrain is flat; ponding results are not meaningful |
| `DEM_NO_HOLES` | info | No missing cells |
| `RAIN_NON_FINITE`, `RAIN_NEGATIVE`, `RAIN_IMPLAUSIBLE` | fatal | Rainfall input invalid |
| `RAIN_ZERO` | info | Zero event — degraded run, still evaluable |
| `CN_NON_FINITE`, `CN_OUT_OF_RANGE` | fatal | Curve number outside 1–100 has no physical meaning |
| `DOMAIN_TOO_SMALL` | fatal | Study area below the minimum analysable size |

**Invocation**

| Code | Meaning |
|---|---|
| `NO_NAME`, `NO_PLACE` | Required location argument missing |
| `PLACE_UNRESOLVED` | Place name not in the built-in study areas (Nanjing only) |
| `BOUNDS_REJECTED` | Bounding box invalid or outside the validated domain |

**Forecast**

| Code | Meaning |
|---|---|
| `TIME_OUT_OF_SCOPE`, `OUT_OF_HORIZON` | Requested time beyond the 15-day forecast window |
| `BAD_TIME` | Unparsable time |
| `FORECAST_UNAVAILABLE`, `EMPTY_FORECAST` | Upstream model returned nothing usable |

---

## 4. Invariants an integrator MUST NOT break

1. **No silent success on a fatal issue.** Never post-process a `rejected` run into
   a rendered map.
2. **No single source of truth duplication.** Depth classes, colours, bounds and
   exposure counts MUST come from `floodkit.runio`, not be re-derived in the
   consumer.
3. **No hand-typed numbers in generated output.** Every figure in a page, map or
   report MUST be read from that run's `report.json` / `depth.tif`.
4. **A failed indicator stays failed.** A renderer that greens a red indicator MUST
   refuse to produce output.
5. **Approval cannot be bypassed.** Write tools keep `requiresApproval: "always"`.
6. **Scope stays closed.** Requests outside Nanjing or beyond 15 days MUST fail,
   not fall back to a default area or a nearest value.
7. **Exact-zero is not claimed.** Machine-precision residuals must be reported as
   such, not rounded to "0" in a way that implies exactness.

---

## 5. Versioning and compatibility

| Item | Requirement |
|---|---|
| Python | **3.12**. Residuals at the 1e-16 level depend on 3.12's compensated `sum()`; the magnitude is stable across versions, the last digits are not |
| Dependencies | `numpy`, `scipy`, `matplotlib`, `rasterio`, `Pillow` (see `requirements.txt`; floors, not pins) |
| Node.js | Any version supporting ESM and `node:http`. The plug-in has no runtime dependencies |
| Harness | Agnes Harness with the extension tool API described in §2.3 |
| Report schema | Additive changes only. Removing or re-typing a key listed in §2.2 is a breaking change |
| Tool names | Stable. `flood_*` names are referenced by documentation, tests and the execution record |

An integrator SHOULD pin the floodkit commit it validated against and record it
alongside any derived result.

---

## 6. Adding a tool, or a city

**Adding a tool** — all six steps are required:

1. Define the schema with `additionalProperties: false` and all eight metadata keys.
2. Choose metadata honestly: `isReadOnly` means *no writes at all*; a cache write
   makes it false even though nothing is destroyed.
3. Add a contract test in `agh-plugin/test-plugin.mjs`, including a negative
   control that must fail.
4. Update the tool table in both READMEs and in `docs/ARCHITECTURE*.md`.
5. Update every tool count quoted in documentation — the count is asserted, and
   stale values are in the `STALE` list of `tests/check_doc_consistency.py`.
6. Re-run `node agh-plugin/test-plugin.mjs` and confirm the total.

**Adding a city** — this is a research task, not a configuration change:

1. Re-derive the land-cover → curve-number mapping and calibrate it locally.
2. Re-derive the plausible elevation envelope and record why.
3. Validate the OSM layers for completeness; an incomplete layer under-reports impact.
4. Run the external checkpoint check if any published flooding list exists. If the
   best achievable AUC is near 0.5, publish that as a negative result rather than
   omitting it.
5. Only then add the entry to `places.py`. Until step 4 is done, the honest state
   of the model is "unvalidated here", and the gate MUST keep refusing.

---

## 7. Acceptance tests an integrator MUST run

```powershell
python -X utf8 -m floodkit selftest            # analytic checks, no network
python -X utf8 tests/test_floodkit.py          # regression suite
python -X utf8 -m floodkit samples --cn-source landcover   # normal / boundary / failure
node agh-plugin/test-plugin.mjs                # tool contract, incl. negative controls
python -X utf8 tests/check_doc_consistency.py  # docs vs report JSON
python -X utf8 tests/verify_report_html.py TAG # page is offline and faithful
python -X utf8 tests/verify_geojson.py TAG     # vectors reconcile with the report
```

An integration is **not** accepted until all of these pass on the target machine.
Passing on the author's machine is not evidence about the target machine.

---

## 8. Anti-patterns

| Anti-pattern | Why it breaks the guarantee |
|---|---|
| Parsing the human-readable `.md` instead of the JSON | Prose changes freely; the JSON is the contract |
| Re-deriving depth classes in the consumer | The same depth gets two colours in two places |
| Retrying `exit 3` with different arguments automatically | `3` means the system refused on principle, not that it was unlucky |
| Relaxing `requiresApproval` for a demo | Removes the auditable human-in-the-loop property |
| Treating `0.000000` as an exact zero | It is `2.4e-16`; claiming exactness invites a check that fails |
| Reporting only the passing indicators | The report's information content comes from the ones that fail |
| Assuming a raster exists after `rejected` | By construction it does not — that is the point of the gate |

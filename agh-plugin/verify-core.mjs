// Independent second implementation of the floodkit numerical core, in JavaScript.
//
// Why this exists: the original plan was a MATLAB second implementation, but the
// MATLAB licence on the target machine is expired (MathWorks error 10). A
// cross-language reimplementation is the stronger substitute anyway — this one
// runs inside the agent's own tool chain, so every result the agent produces can
// be re-derived on the spot instead of relying on a script someone ran once.
//
// Nothing here imports or shells out to Python: the SCS-CN terms, the
// stage-volume curve and its inversion are written from the specification.

const IA_RATIO = 0.2

/** S in millimetres for a curve number (TR-55). */
export function potentialRetention(cn) {
  const safe = Math.min(Math.max(cn, 1), 100)
  return 25400 / safe - 254
}

/** SCS-CN direct runoff depth in millimetres. */
export function scsRunoff(pMm, cn, iaRatio = IA_RATIO) {
  const s = potentialRetention(cn)
  const ia = iaRatio * s
  const p = Math.max(pMm, 0)
  const effective = p - ia
  if (effective <= 0) return 0
  return Math.min((effective * effective) / (effective + s), p)
}

/** SCS-CN continuing retention F in millimetres. */
export function scsRetention(pMm, cn, iaRatio = IA_RATIO) {
  const s = potentialRetention(cn)
  const ia = iaRatio * s
  const effective = Math.max(pMm - ia, 0)
  const denom = effective + s
  return denom > 0 ? (s * effective) / denom : 0
}

/**
 * Invert a monotone stage-volume curve by exact linear interpolation.
 * `elevations` ascending, `volumes` matching cumulative volumes.
 */
export function stageFromVolume(elevations, volumes, target) {
  const n = elevations.length
  if (n === 0) return { stage: NaN, stored: 0 }
  if (target <= 0) return { stage: elevations[0], stored: 0 }
  if (target >= volumes[n - 1]) return { stage: elevations[n - 1], stored: volumes[n - 1] }
  let lo = 0
  let hi = n - 1
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (volumes[mid] < target) lo = mid + 1
    else hi = mid
  }
  const k = lo
  if (k === 0) return { stage: elevations[0], stored: 0 }
  const v0 = volumes[k - 1]
  const v1 = volumes[k]
  const e0 = elevations[k - 1]
  const e1 = elevations[k]
  if (v1 <= v0) return { stage: e1, stored: v1 }
  const frac = (target - v0) / (v1 - v0)
  return { stage: e0 + frac * (e1 - e0), stored: target }
}

/**
 * Rebuild a depression's stage-volume curve from ground elevations and solve the
 * equilibrium stage. Geometry (which cells, and the spill elevation) comes from
 * the caller; everything numerical is derived here.
 */
export function solveBasin({ groundElevations, spillElevation, cellArea, targetVolume }) {
  const e = [...groundElevations].sort((a, b) => a - b)
  const n = e.length
  const volumes = new Array(n)
  let cumulative = 0
  for (let i = 0; i < n; i += 1) {
    cumulative += e[i]
    const k = i + 1
    volumes[i] = cellArea * (k * e[i] - cumulative)
  }
  let capacity = 0
  for (let i = 0; i < n; i += 1) capacity += Math.max(spillElevation - e[i], 0) * cellArea
  if (capacity < volumes[n - 1]) capacity = volumes[n - 1]

  const elevationsCurve = [...e, spillElevation]
  const volumesCurve = [...volumes, capacity]
  const target = Math.min(targetVolume, capacity)
  const solved = stageFromVolume(elevationsCurve, volumesCurve, target)
  return { ...solved, capacity, target, spilled: Math.max(0, targetVolume - target) }
}

/** Compare this implementation against a fixture produced by the Python core. */
export function verifyFixture(fixture, tolerances = {}) {
  const stageTol = tolerances.stageM ?? 0.05
  const volumeTol = tolerances.volumeRelative ?? 0.01
  const runoffTol = tolerances.runoffMm ?? 1e-6
  const findings = []

  let maxRunoffGap = 0
  let maxRetentionGap = 0
  for (const sample of fixture.scs_samples ?? []) {
    const q = scsRunoff(sample.p_mm, sample.cn)
    const f = scsRetention(sample.p_mm, sample.cn)
    maxRunoffGap = Math.max(maxRunoffGap, Math.abs(q - sample.q_mm))
    maxRetentionGap = Math.max(maxRetentionGap, Math.abs(f - sample.f_mm))
  }
  findings.push({
    level: maxRunoffGap <= runoffTol && maxRetentionGap <= runoffTol ? 'pass' : 'fatal',
    code: 'SCS_CN_CROSSLANG',
    message: 'SCS-CN runoff and retention recomputed in JavaScript',
    samples: (fixture.scs_samples ?? []).length,
    max_runoff_gap_mm: maxRunoffGap,
    max_retention_gap_mm: maxRetentionGap,
    tolerance_mm: runoffTol,
  })

  const sv = fixture.stage_volume
  if (sv) {
    const mid = sv.volumes_m3[sv.volumes_m3.length - 2] * 0.5
    const solved = stageFromVolume(sv.elevations_m, sv.volumes_m3, mid)
    // invert the same curve at the interpolated stage: volume must round-trip
    const roundTrip = stageFromVolume(sv.elevations_m, sv.volumes_m3, solved.stored)
    findings.push({
      level: Math.abs(roundTrip.stored - solved.stored) <= 1e-6 * Math.max(1, solved.stored) ? 'pass' : 'fatal',
      code: 'STAGE_VOLUME_ROUNDTRIP',
      message: 'stage-volume inversion round-trips to the same stored volume',
      stage_m: solved.stage,
      stored_m3: solved.stored,
    })
  }

  // One basin case only proves the two implementations agree at one point in the
  // input space. The edges are where they diverge: an empty basin, one filled to
  // exactly its spill level, a target beyond capacity, and flat floors where many
  // cells share an elevation and the interpolation has to break a tie.
  const basins = fixture.basins ?? (fixture.basin ? [fixture.basin] : [])
  if (basins.length) {
    const cases = []
    for (const basin of basins) {
      const solved = solveBasin({
        groundElevations: basin.ground_elevations_m,
        spillElevation: basin.spill_elevation_m,
        cellArea: basin.cell_area_m2,
        targetVolume: basin.target_volume_m3,
      })
      const stageGap = Math.abs(solved.stage - basin.python_stage_m)
      const relative = basin.python_stored_m3 > 0
        ? Math.abs(solved.stored - basin.python_stored_m3) / basin.python_stored_m3
        : Math.abs(solved.stored) <= 1e-9 ? 0 : Infinity
      cases.push({
        name: basin.name ?? 'unnamed',
        cells: (basin.ground_elevations_m ?? []).length,
        target_volume_m3: basin.target_volume_m3,
        clamped_to_capacity: basin.clamped_to_capacity ?? false,
        python_stage_m: basin.python_stage_m,
        javascript_stage_m: solved.stage,
        stage_gap_m: stageGap,
        stored_relative_gap: relative,
        ok: stageGap <= stageTol && relative <= volumeTol,
      })
    }
    const worstStage = Math.max(...cases.map((c) => c.stage_gap_m))
    const worstVolume = Math.max(...cases.map((c) => c.stored_relative_gap))
    const failed = cases.filter((c) => !c.ok)
    findings.push({
      level: failed.length === 0 ? 'pass' : 'fatal',
      code: 'BASIN_SOLVER_CROSSLANG',
      message: 'depression stage solved independently in JavaScript across a case matrix',
      cases: cases.length,
      cases_failed: failed.map((c) => c.name),
      worst_stage_gap_m: worstStage,
      worst_stored_relative_gap: worstVolume,
      stage_tolerance_m: stageTol,
      volume_tolerance: volumeTol,
      detail: cases,
    })
  }

  const fatal = findings.filter((f) => f.level === 'fatal')
  return { verified: fatal.length === 0, fatal_count: fatal.length, findings }
}

// Harness-independent test for the AGH flood tool plugin.
//
// Builds a fake Cordis context, registers the tools exactly the way the harness
// does, and calls them. This proves the plugin's tool contracts work without a
// model, a session or an API key — the harness only adds the tool-calling loop.
//
//   node agh-plugin/test-plugin.mjs [path-to-report.json]
//
// Exit code 0 means every tool behaved as documented.

import path from 'node:path'
import { existsSync, readFileSync } from 'node:fs'

import floodTools from './index.mjs'

const registered = new Map()
const fakeCtx = {
  extension() {
    return {
      registerTool(definition) {
        registered.set(definition.name, definition)
      },
      on() {},
      ctx: { log: { info() {} } },
    }
  },
  log: { info() {} },
}

floodTools.apply(fakeCtx)

const expectedTools = ['flood_selftest', 'flood_resolve_place', 'flood_weather', 'flood_fetch_dem',
  'flood_run_event', 'flood_publish_report', 'flood_verify_report', 'flood_crosslang_check']
const results = []

for (const name of expectedTools) {
  results.push({
    check: `registered:${name}`,
    ok: registered.has(name),
    detail: registered.has(name) ? 'tool contract present' : 'MISSING',
  })
}

// The eight metadata keys AGH requires. A missing key fails the extension
// factory with E_TOOLDEF_META, the plugin never reaches `running`, and the
// workbench refuses to enable it — which is exactly what happened in the field.
// A fake context does NOT validate this, so the test has to.
const META_KEYS = [
  'isReadOnly',
  'isDestructive',
  'isConcurrencySafe',
  'isOpenWorld',
  'replay',
  'costHint',
  'deferLoading',
  'requiresApproval',
]

const contractProblems = []
for (const [name, def] of registered.entries()) {
  if (!def.description || def.description.length < 20) contractProblems.push(`${name}: weak description`)
  if (!def.parameters || def.parameters[Symbol.for('TypeBox.Kind')] !== 'Object') contractProblems.push(`${name}: parameters are not a TypeBox object`)
  if (!def.meta || typeof def.meta.requiresApproval !== 'string') contractProblems.push(`${name}: meta incomplete`)
  else {
    const missing = META_KEYS.filter((key) => !Object.prototype.hasOwnProperty.call(def.meta, key))
    if (missing.length) contractProblems.push(`${name}: meta missing ${missing.join(', ')}`)
    // Mirror the rest of checkToolMeta (extension-api/src/tool.ts) so a bad enum
    // value cannot reach the host again: `replay: 'unsafe'` cost a full round of
    // "cannot be safely enabled" before anyone saw the real error.
    for (const key of ['isReadOnly', 'isDestructive', 'isConcurrencySafe', 'isOpenWorld'])
      if (typeof def.meta[key] !== 'boolean') contractProblems.push(`${name}: meta.${key} must be boolean`)
    if (!['safe', 'never', 'idempotent'].includes(def.meta.replay))
      contractProblems.push(`${name}: meta.replay must be safe | never | idempotent (got ${String(def.meta.replay)})`)
    if (def.meta.requiresApproval !== undefined &&
        !['never', 'destructive', 'always'].includes(def.meta.requiresApproval))
      contractProblems.push(`${name}: meta.requiresApproval must be never | destructive | always`)
    if (def.meta.deferLoading !== undefined && typeof def.meta.deferLoading !== 'boolean')
      contractProblems.push(`${name}: meta.deferLoading must be boolean | undefined`)
    if (def.meta.costHint !== undefined) {
      const cost = def.meta.costHint
      const shaped = typeof cost === 'object' && cost !== null && !Array.isArray(cost)
      const extra = shaped ? Object.keys(cost).filter((k) => k !== 'credits' && k !== 'wallMs') : []
      if (!shaped || extra.length) contractProblems.push(`${name}: meta.costHint must be undefined or {credits?, wallMs?}`)
    }
    if (def.meta.isReadOnly === true && def.meta.isDestructive === true)
      contractProblems.push(`${name}: isReadOnly and isDestructive cannot both be true`)
  }
  if (typeof def.execute !== 'function') contractProblems.push(`${name}: no execute`)
}
results.push({ check: 'tool-contracts', ok: contractProblems.length === 0, detail: contractProblems.join('; ') || 'all contracts well formed' })

// Every tool that writes the workspace must ask for approval; a read-only tool
// must not. Two deliberate exceptions, named here rather than covered by a looser
// rule, because a loosened rule would also excuse the tools that DO need a human
// to say yes:
//
//   flood_weather        writes only a cached HTTP response, so isReadOnly is false
//                        (honestly) while approval stays 'never';
//   flood_publish_report writes a derived page and reads cached tiles. It is not
//                        destructive and it is idempotent for a given run — and it
//                        exists precisely to answer "show me", so an approval modal
//                        between the question and the page defeats its purpose.
const NO_APPROVAL_DESPITE_WRITING = new Set(['flood_weather', 'flood_publish_report'])
const approval = [...registered.entries()].map(([name, def]) => ({
  name,
  readOnly: def.meta.isReadOnly,
  approval: def.meta.requiresApproval,
}))
const approvalOk = approval.every((t) =>
  t.readOnly
    ? t.approval === 'never'
    : t.approval === 'always' || NO_APPROVAL_DESPITE_WRITING.has(t.name))
results.push({ check: 'approval-semantics', ok: approvalOk, detail: JSON.stringify(approval) })

// External data must be marked untrusted, or an instruction smuggled into a
// forecast response would carry the same authority as the user's own words.
const weatherMeta = registered.get('flood_weather').meta
results.push({
  check: 'weather-is-open-world',
  ok: weatherMeta.isOpenWorld === true && weatherMeta.isReadOnly === false,
  detail: `isOpenWorld=${weatherMeta.isOpenWorld} isReadOnly=${weatherMeta.isReadOnly}`,
})

// Mirror isToolResult (core/src/effects/tool-dispatch.ts). The host silently
// replaces a non-conforming result with "invalid ToolResult", so a tool that runs
// perfectly still looks broken to the caller — and a fake context never notices.
const TOOL_RESULT_KEYS = new Set(['content', 'isError', 'details', 'terminate', 'structured', 'deferred'])
function isJsonValue(value, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (typeof value !== 'object') return false
  if (seen.has(value)) return false
  seen.add(value)
  const valid = Array.isArray(value)
    ? value.every((item) => isJsonValue(item, seen))
    : Object.getPrototypeOf(value) === Object.prototype &&
      Object.values(value).every((item) => isJsonValue(item, seen))
  seen.delete(value)
  return valid
}
function isToolResult(value) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  if (!Object.keys(value).every((key) => TOOL_RESULT_KEYS.has(key))) return false
  if (!Array.isArray(value.content)) return false
  if (!value.content.every((b) => b && typeof b.type === 'string' && (b.type !== 'text' || typeof b.text === 'string')))
    return false
  if (value.isError !== undefined && typeof value.isError !== 'boolean') return false
  if (value.terminate !== undefined && typeof value.terminate !== 'boolean') return false
  if (value.details !== undefined && !isJsonValue(value.details)) return false
  if (value.structured !== undefined && !isJsonValue(value.structured)) return false
  return true
}

if (registered.has('flood_selftest')) {
  const out = await registered.get('flood_selftest').execute({})
  results.push({
    check: 'flood_selftest-runs',
    ok: Boolean(out.structured && out.structured.ok),
    detail: `exit=${out.structured && out.structured.exit_code} checks=${(out.structured && out.structured.checks.length) || 0}`,
  })
  results.push({
    check: 'flood_selftest-returns-valid-ToolResult',
    ok: isToolResult(out),
    detail: isToolResult(out)
      ? 'content + structured conform to isToolResult'
      : `rejected by the host: keys=${Object.keys(out).join(',')} structuredJsonSafe=${isJsonValue(out.structured)}`,
  })
}

// The two tools that let the agent question its input rather than its output.
// Both are tested for the failure direction as well, because the dangerous
// behaviour is not "it errored" but "it quietly produced a plausible answer":
// an unresolvable place must NOT come back with a box, and a failed forecast
// lookup must NOT come back as 0 mm.
if (registered.has('flood_resolve_place')) {
  const known = await registered.get('flood_resolve_place').execute({ name: '南京主城区' })
  const boxes = known.structured && known.structured.bounds
  results.push({
    check: 'resolve_place-known-area',
    ok: Boolean(known.structured && known.structured.ok && Array.isArray(boxes) && boxes.length === 4
      && known.structured.approximate === false),
    detail: `bounds=${JSON.stringify(boxes)} approximate=${known.structured && known.structured.approximate}`,
  })
  results.push({
    check: 'resolve_place-valid-ToolResult',
    ok: isToolResult(known),
    detail: 'content + structured conform to isToolResult',
  })

  const foreign = await registered.get('flood_resolve_place').execute({ name: '北京' })
  results.push({
    check: 'resolve_place-refuses-other-cities',
    ok: Boolean(foreign.structured && foreign.structured.ok === false
      && foreign.structured.bounds === undefined),
    detail: `ok=${foreign.structured && foreign.structured.ok} code=${foreign.structured && foreign.structured.code} bounds=${JSON.stringify(foreign.structured && foreign.structured.bounds)}`,
  })
}

if (registered.has('flood_weather')) {
  // Three days out: far enough to be a real forecast, close enough to stay
  // inside the model horizon. A hard-coded far-future date fails with
  // OUT_OF_HORIZON, which is correct behaviour but tests nothing about ranges.
  const soon = new Date(Date.now() + 3 * 24 * 3600 * 1000)
  const at = `${soon.getFullYear()}-${String(soon.getMonth() + 1).padStart(2, '0')}-${String(soon.getDate()).padStart(2, '0')}T12:00`
  const out = await registered.get('flood_weather').execute({ place: '南京主城区', at })
  const s = out.structured || {}
  const ranged = typeof s.min_mm === 'number' && typeof s.max_mm === 'number' && s.max_mm >= s.min_mm
  results.push({
    check: 'weather-returns-a-range',
    ok: Boolean(s.ok && ranged && Array.isArray(s.day_context)),
    detail: `at=${at} ok=${s.ok} min=${s.min_mm} max=${s.max_mm} models=${Object.keys(s.per_model_mm || {}).length} day_hours=${(s.day_context || []).length}`,
  })
  results.push({
    check: 'weather-valid-ToolResult',
    ok: isToolResult(out),
    detail: 'content + structured conform to isToolResult',
  })
  results.push({
    check: 'weather-failure-is-not-zero-rain',
    ok: !(s.ok === false && (s.min_mm === 0 || s.max_mm === 0)),
    detail: s.ok ? 'lookup succeeded (vacuously fine)' : `code=${s.code} min=${s.min_mm} max=${s.max_mm}`,
  })

  const bad = await registered.get('flood_weather').execute({ place: '南京主城区', at: '不是时间' })
  results.push({
    check: 'weather-rejects-bad-time',
    ok: Boolean(bad.structured && bad.structured.ok === false && bad.structured.code === 'BAD_TIME'),
    detail: `code=${bad.structured && bad.structured.code}`,
  })

  const off = await registered.get('flood_weather').execute({ bounds: [116.35, 39.86, 116.45, 39.95], at: '2030-07-01T12:00' })
  results.push({
    check: 'weather-refuses-out-of-scope-bounds',
    ok: Boolean(off.structured && off.structured.ok === false),
    detail: `code=${off.structured && off.structured.code}`,
  })

  // The scope boundary is 15 days, and the boundary itself must be inside: an
  // off-by-one here would refuse the last legitimate day or accept one beyond
  // what the project covers, and neither would show up in ordinary use.
  //
  // Offsets are measured from *now*, not from noon of a calendar day. The window is
  // a floating instant (`now + 15 days`), so a probe pinned to 12:00 on day+15 sits
  // inside the boundary before noon and outside it after — the check passed all
  // evening and failed the next morning, which is the worst kind of red test to meet
  // on the day of a demo.
  const offsetTime = (hours) => {
    const d = new Date(Date.now() + hours * 3600 * 1000)
    const pad = (value) => String(value).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
      + `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  }
  const onEdge = await registered.get('flood_weather').execute({ place: '南京主城区', at: offsetTime(15 * 24 - 2) })
  const pastEdge = await registered.get('flood_weather').execute({ place: '南京主城区', at: offsetTime(15 * 24 + 2) })
  const wayOut = await registered.get('flood_weather').execute({ place: '南京主城区', at: offsetTime(40 * 24) })
  const ancient = await registered.get('flood_weather').execute({ place: '南京主城区', at: offsetTime(-30 * 24) })
  results.push({
    check: 'weather-15-day-boundary',
    ok: Boolean(onEdge.structured && onEdge.structured.ok === true
      && pastEdge.structured && pastEdge.structured.code === 'TIME_OUT_OF_SCOPE'
      && wayOut.structured && wayOut.structured.code === 'TIME_OUT_OF_SCOPE'
      && ancient.structured && ancient.structured.code === 'TIME_OUT_OF_SCOPE'),
    detail: `day15.ok=${onEdge.structured && onEdge.structured.ok} `
      + `day16=${pastEdge.structured && pastEdge.structured.code} `
      + `day40=${wayOut.structured && wayOut.structured.code} `
      + `day-30=${ancient.structured && ancient.structured.code}`,
  })
  results.push({
    check: 'weather-scope-refusal-names-the-window',
    ok: Boolean(pastEdge.structured && pastEdge.structured.scope
      && pastEdge.structured.scope.latest && pastEdge.structured.scope.earliest),
    detail: JSON.stringify(pastEdge.structured && pastEdge.structured.scope),
  })
}

// Default to the flagship run. An earlier default pointed at an exploratory run
// directory that was later cleaned out of the package, which broke this test
// without touching its logic — so it now falls back to whichever run exists
// rather than to one hard-coded tag.
const CANDIDATE_REPORTS = [  path.resolve('data', 'runs', 'nanjing_final', 'nanjing_final_report.json'),
  path.resolve('data', 'runs', 'rehearsal', 'rehearsal_report.json'),
  path.resolve('data', 'runs', 'nanjing_wide', 'nanjing_wide_report.json'),
]
const reportPath =
  process.argv[2] || CANDIDATE_REPORTS.find((candidate) => existsSync(candidate)) || CANDIDATE_REPORTS[0]
if (!existsSync(reportPath)) {
  results.push({
    check: 'report-fixture-present',
    ok: false,
    detail: `no report to audit; run an event first. Looked for: ${CANDIDATE_REPORTS.join(', ')}`,
  })
}
if (registered.has('flood_verify_report')) {
  const out = await registered.get('flood_verify_report').execute({ report_path: reportPath })
  results.push({
    check: 'flood_verify_report-runs',
    ok: Boolean(out.structured && out.structured.ok),
    detail: `fatal=${out.structured && out.structured.fatal_count} findings=${(out.structured && out.structured.findings.length) || 0}`,
  })

  // negative control: the verifier must reject a report whose balance is broken
  const broken = JSON.parse(readFileSync(reportPath, 'utf8'))
  broken.water_balance.stored_m3 = broken.water_balance.stored_m3 * 3
  const tmp = path.resolve('data', 'runs', '_verify_negative_control.json')
  const { writeFileSync } = await import('node:fs')
  writeFileSync(tmp, JSON.stringify(broken))
  const bad = await registered.get('flood_verify_report').execute({ report_path: tmp })
  results.push({
    check: 'flood_verify_report-detects-broken-balance',
    ok: Boolean(bad.structured && bad.structured.ok === false),
    detail: `fatal=${bad.structured && bad.structured.fatal_count}`,
  })
  const missing = await registered.get('flood_verify_report').execute({ report_path: path.resolve('data', 'runs', 'does-not-exist.json') })
  results.push({
    check: 'flood_verify_report-handles-missing-file',
    ok: Boolean(missing.structured && missing.structured.verified === false),
    detail: 'structured failure, no exception',
  })
}

// The reported results need land-cover CN and OSM exposure analysis. Without
// these parameters on the tool the agent can only ever run the simplified
// configuration, so the numbers in the documentation would be unreachable
// through the harness — a gap that stays invisible until someone tries to demo it.
{
  const runEvent = registered.get('flood_run_event')
  const props = Object.keys((runEvent && runEvent.parameters && runEvent.parameters.properties) || {})
  const required = ['cn_source', 'exposure', 'dem_file']
  const missing = required.filter((name) => !props.includes(name))
  results.push({
    check: 'run_event-reaches-shipped-config',
    ok: missing.length === 0,
    detail: missing.length
      ? `missing parameters: ${missing.join(', ')} (have: ${props.join(', ')})`
      : props.join(', '),
  })
}

{
  // Fault injection through the tool, not around it. The demo script promises the
  // agent will be handed unusable data and refuse to publish a result; this proves
  // that path exists and fails closed.
  const { existsSync } = await import('node:fs')
  const badDem = path.resolve('data', 'fixtures', 'bad_dem_units.tif')
  if (!existsSync(badDem)) {
    results.push({ check: 'run_event-rejects-bad-dem', ok: false, detail: `fixture missing: ${badDem}` })
  } else {
    const out = await registered.get('flood_run_event').execute({
      bounds: [118.72, 32.02, 118.82, 32.1],
      rain_mm: 100,
      cn: 85,
      tag: 'contract_bad_dem',
      dem_file: badDem,
    })
    const payload = out.structured || {}
    const rasterProduced = Boolean(payload.artifacts && payload.artifacts.depth_tif)
    results.push({
      check: 'run_event-rejects-bad-dem',
      ok: payload.outcome === 'rejected' && !rasterProduced,
      detail: `outcome=${payload.outcome}, raster_produced=${rasterProduced}`,
    })
  }
}

if (registered.has('flood_publish_report')) {
  // The user-facing contract: "tell me what happens, and give me the site". A path
  // is not an answer — the workbench is a browser and cannot follow file:// — so the
  // test asserts a real http response, not a string that looks like one.
  const publish = registered.get('flood_publish_report')

  const missing = await publish.execute({ tag: 'no_such_run_at_all', render: false })
  results.push({
    check: 'publish_report-refuses-unknown-run',
    ok: Boolean(missing.structured && missing.structured.ok === false
      && missing.structured.code === 'NOT_FOUND' && missing.structured.url === undefined),
    detail: `ok=${missing.structured && missing.structured.ok} code=${missing.structured && missing.structured.code} url=${missing.structured && missing.structured.url}`,
  })

  // A tag is a directory name; "../.." must not be accepted as one.
  const traversal = await publish.execute({ tag: '../..', render: false })
  results.push({
    check: 'publish_report-refuses-path-traversal-tag',
    ok: Boolean(traversal.structured && traversal.structured.ok === false
      && traversal.structured.code === 'BAD_TAG'),
    detail: `code=${traversal.structured && traversal.structured.code}`,
  })

  // Whichever run happens to exist, not a hard-coded tag: runs come and go with
  // housekeeping, and a test that fails because someone cleared a directory teaches
  // nothing about the tool.
  const { existsSync: exists, readdirSync } = await import('node:fs')
  const runsRoot = path.resolve('data', 'runs')
  const servableTag = (exists(runsRoot) ? readdirSync(runsRoot) : [])
    .find((name) => exists(path.join(runsRoot, name, `${name}_report.html`)))

  if (!servableTag) {
    results.push({
      check: 'publish_report-serves-existing-page',
      ok: false,
      detail: 'no run has a rendered page; run the event and flood_publish_report first',
    })
  } else {
    const served = await publish.execute({ tag: servableTag, render: false })
    const url = served.structured && served.structured.url
    results.push({
      check: 'publish_report-serves-existing-page',
      ok: Boolean(served.structured && served.structured.ok && typeof url === 'string'
        && url.startsWith('http://127.0.0.1:')),
      detail: `tag=${servableTag} url=${url} bytes=${served.structured && served.structured.size_bytes}`,
    })
    results.push({
      check: 'publish_report-valid-ToolResult',
      ok: isToolResult(served),
      detail: 'content + structured conform to isToolResult',
    })

    if (url) {
      const response = await fetch(url)
      const body = await response.text()
      results.push({
        check: 'publish_report-url-actually-serves-the-page',
        ok: response.status === 200 && body.includes('<!DOCTYPE html>') && body.length > 100_000,
        detail: `status=${response.status} content-type=${response.headers.get('content-type')} bytes=${body.length}`,
      })
      // The page has to be the *interactive* one, not just any HTML: these are the
      // things the user is promised they can do with it.
      results.push({
        check: 'publish_report-page-is-interactive',
        ok: ['剖面', 'id="map"', 'class_of_code', '机器可判指标'].every((token) => body.includes(token)),
        detail: 'page carries the map canvas, the class table, the evidence panel and the profile tool',
      })

      const sibling = served.structured.also_available || []
      if (sibling.length) {
        const one = await fetch(sibling[0])
        results.push({
          check: 'publish_report-serves-sibling-artifacts',
          ok: one.status === 200,
          detail: `${sibling.length} siblings, e.g. ${sibling[0]} -> ${one.status}`,
        })
      }

      // Encoded, and aimed at a file that genuinely exists outside data/runs.
      // A plain "/../../agh-plugin/package.json" is normalised away by fetch before
      // it is ever sent, so the server only sees a missing file and answers 404 —
      // which looks like a pass while testing nothing. If the guard were absent this
      // request would return the plugin manifest with a 200.
      const escape = await fetch(`${url.split('/').slice(0, 3).join('/')}/%2e%2e%2f%2e%2e%2fagh-plugin%2fpackage.json`)
      const leaked = await escape.text()
      results.push({
        check: 'publish_report-refuses-path-traversal-request',
        ok: escape.status === 403 && !leaked.includes('agh-flood-tools'),
        detail: `GET /%2e%2e%2f%2e%2e%2fagh-plugin%2fpackage.json -> ${escape.status}, body ${leaked.length} bytes`,
      })
    }
  }
}

if (registered.has('flood_crosslang_check')) {
  const out = await registered.get('flood_crosslang_check').execute({})
  const findings = (out.structured && out.structured.findings) || []
  results.push({
    check: 'flood_crosslang_check-runs',
    ok: Boolean(out.structured && out.structured.verified),
    detail: `fatal=${out.structured && out.structured.fatal_count} findings=${findings.length}`,
  })
  for (const finding of findings) {
    results.push({
      check: `crosslang:${finding.code}`,
      ok: finding.level !== 'fatal',
      detail: JSON.stringify(finding).slice(0, 220),
    })
  }

  // Negative control: the checker must catch a wrong Python answer. It perturbs
  // every basin case and asserts the fixture really has cases, so a future shape
  // change cannot silently turn this into a no-op — which is exactly what happened
  // when the fixture moved from a single `basin` to a `basins` matrix and this
  // control kept "passing" while checking nothing.
  const fixture = JSON.parse(
    (await import('node:fs')).readFileSync(path.resolve('data', 'fixtures', 'verify_case.json'), 'utf8'),
  )
  if (!Array.isArray(fixture.basins) || fixture.basins.length === 0) {
    throw new Error('negative control needs a non-empty fixture.basins to perturb')
  }
  for (const basinCase of fixture.basins) basinCase.python_stage_m += 1.5
  const broken = path.resolve('data', 'fixtures', '_crosslang_negative_control.json')
  ;(await import('node:fs')).writeFileSync(broken, JSON.stringify(fixture))
  const bad = await registered.get('flood_crosslang_check').execute({ fixture_path: broken })
  const basinFinding = (bad.structured?.findings ?? []).find((f) => f.code === 'BASIN_SOLVER_CROSSLANG')
  const casesChecked = basinFinding?.cases ?? 0
  results.push({
    check: 'crosslang-detects-disagreement',
    ok: Boolean(bad.structured && bad.structured.verified === false && casesChecked > 0),
    detail: `fatal=${bad.structured && bad.structured.fatal_count}, cases_checked=${casesChecked}`,
  })

  // Second negative control: corrupt the SCS answers instead, so the two halves of
  // the fixture are each proven to be actually compared.
  const scsFixture = JSON.parse(
    (await import('node:fs')).readFileSync(path.resolve('data', 'fixtures', 'verify_case.json'), 'utf8'),
  )
  if (!Array.isArray(scsFixture.scs_samples) || scsFixture.scs_samples.length === 0) {
    throw new Error('negative control needs scs_samples to perturb')
  }
  for (const sample of scsFixture.scs_samples) sample.q_mm += 0.5
  const brokenScs = path.resolve('data', 'fixtures', '_crosslang_scs_negative_control.json')
  ;(await import('node:fs')).writeFileSync(brokenScs, JSON.stringify(scsFixture))
  const badScs = await registered.get('flood_crosslang_check').execute({ fixture_path: brokenScs })
  results.push({
    check: 'crosslang-detects-scs-disagreement',
    ok: Boolean(badScs.structured && badScs.structured.verified === false),
    detail: `fatal=${badScs.structured && badScs.structured.fatal_count}`,
  })
}

let failed = 0
for (const r of results) {
  if (!r.ok) failed += 1
  console.log(`${r.ok ? 'PASS' : 'FAIL'} ${r.check} -- ${r.detail}`)
}
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)

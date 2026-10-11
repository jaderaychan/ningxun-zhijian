// Demo rehearsal: drive the plugin exactly the way the agent will, with no model.
//
// test-plugin.mjs checks tool *contracts* and stays fast. This does the opposite:
// it runs the real, slow path the agent will take on demo day and asserts that the
// numbers coming out of the harness are the numbers written in the documentation.
//
// That distinction matters. Every figure in the submission (117 flooded underpass
// sections, 80 subway entrances, 1722 roads) was produced by the command line, by
// hand. Whether the *tool* can produce them is a separate question — and it was
// answered "no" once already, because flood_run_event had no way to request
// land-cover CN or exposure analysis.
//
// It ends with the step a user actually asks for — "what happens, and where do I
// look" — by rendering the interactive page and fetching the URL it hands back.
//
//   node tests/rehearse-agent-path.mjs
//
// Needs FLOODKIT_PYTHON and FLOODKIT_ROOT. Takes a few minutes.

import path from 'node:path'

import floodTools from '../agh-plugin/index.mjs'

const ROOT = process.env.FLOODKIT_ROOT || process.cwd()
const BOUNDS = [118.72, 32.02, 118.82, 32.1]

// The values quoted throughout the submission for this exact scenario.
const EXPECTED = {
  outcome: 'ok',
  pond_area_km2: 7.81,
  underpass_flooded: 117,
  underpass_total: 420,
  underpass_life_safety: 102,
  underpass_vehicle: 44,
  facilities_flooded: 146,
  facilities_total: 292,
  subway_entrances: 80,
  roads_flooded: 1722,
  roads_total: 4751,
  buildings_affected: 1131,
  buildings_total: 12080,
}

const registered = new Map()
floodTools.apply({
  extension() {
    return {
      registerTool: (definition) => registered.set(definition.name, definition),
      on() {},
      ctx: { log: { info() {} } },
    }
  },
  log: { info() {} },
})

const results = []
const check = (name, ok, detail) => {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name} -- ${detail}`)
}

console.log('=== rehearsal: the path the agent will actually take ===\n')

// 1. analytic self-check (no network, no writes)
{
  const out = await registered.get('flood_selftest').execute({})
  const payload = out.structured || {}
  check('flood_selftest', payload.ok === true, `exit=${payload.exit_code} checks=${payload.checks ? Object.keys(payload.checks).length : 0}`)
}

// 2. the real event, with the configuration the reported results use
let reportPath = null
{
  const out = await registered.get('flood_run_event').execute({
    bounds: BOUNDS,
    rain_mm: 100,
    cn: 85,
    tag: 'rehearsal',
    cn_source: 'landcover',
    exposure: true,
  })
  const payload = out.structured || {}
  reportPath = payload.artifacts && payload.artifacts.report_json
  const exposure = payload.exposure || {}
  const ponding = payload.ponding || {}
  const underpasses = exposure.underpasses || {}
  const facilities = exposure.critical_facilities || {}
  const buildings = exposure.buildings || {}

  const got = {
    outcome: payload.outcome,
    pond_area_km2: Math.round((ponding.pond_area_m2 || 0) / 1e6 * 100) / 100,
    underpass_flooded: underpasses.underpass_ways_flooded,
    underpass_total: underpasses.underpass_ways,
    underpass_life_safety: underpasses.life_safety_count,
    underpass_vehicle: underpasses.vehicle_life_safety_count,
    facilities_flooded: facilities.facilities_flooded,
    facilities_total: facilities.facilities_total,
    subway_entrances: facilities.subway_entrances_flooded,
    roads_flooded: exposure.roads && exposure.roads.ways_flooded,
    roads_total: exposure.roads && exposure.roads.ways_total,
    buildings_affected: buildings.buildings_affected,
    buildings_total: buildings.buildings_total,
  }

  for (const [key, want] of Object.entries(EXPECTED)) {
    const have = got[key]
    check(`documented:${key}`, have === want, `documented=${want} harness=${have}`)
  }
  check('water_balance', (payload.water_balance || {}).passed === true,
    `D01=${(payload.water_balance || {}).relative_error}`)
  check('d04', (payload.checks || {}).D04_sensitivity && payload.checks.D04_sensitivity.passed === true,
    `change=${(payload.checks || {}).D04_sensitivity && payload.checks.D04_sensitivity.max_relative_area_change}`)
}

// 3. independent audit of the report the harness just produced
if (reportPath) {
  const out = await registered.get('flood_verify_report').execute({ report_path: reportPath })
  const payload = out.structured || {}
  check('flood_verify_report', payload.verified === true, `fatal=${payload.fatal_count} findings=${(payload.findings || []).length}`)
} else {
  check('flood_verify_report', false, 'no report path returned by the harness')
}

// 4. "tell me what happens, and give me the site"
//
// This is the form the question actually arrives in, and a path is not an answer:
// the workbench is a browser and cannot follow file://. So the rehearsal renders
// the page and *fetches the URL*, because a link that was never opened is a claim,
// not a deliverable.
{
  const publish = registered.get('flood_publish_report')
  if (!publish) {
    check('publish_report', false, 'tool not registered')
  } else {
    const out = await publish.execute({ tag: 'rehearsal' })
    const payload = out.structured || {}
    const url = payload.url
    check('publish_report', payload.ok === true && typeof url === 'string' && url.startsWith('http://127.0.0.1:'),
      `url=${url} bytes=${payload.size_bytes} rendered=${payload.rendered_now}`)

    const headline = payload.headline || {}
    check('publish_report-headline-matches-docs',
      headline.ponded_km2 === EXPECTED.pond_area_km2
      && headline.underpass_ways_flooded === EXPECTED.underpass_flooded
      && headline.subway_entrances_flooded === EXPECTED.subway_entrances,
      `ponded=${headline.ponded_km2} underpass=${headline.underpass_ways_flooded} subway=${headline.subway_entrances_flooded}`)

    if (url) {
      const response = await fetch(url)
      const body = await response.text()
      check('publish_report-url-answers',
        response.status === 200 && body.includes('<!DOCTYPE html>') && body.length > 100_000,
        `status=${response.status} type=${response.headers.get('content-type')} bytes=${body.length}`)
      // The specific things the demo promises the page can do.
      check('publish_report-page-is-the-interactive-one',
        ['id="map"', 'class_of_code', '机器可判指标', '剖面', '下穿'].every((token) => body.includes(token)),
        'map canvas, class table, evidence panel, profile tool and the risk section are all present')
    }
  }
}

// 5. cross-language check on the shipped fixture
{
  const out = await registered.get('flood_crosslang_check').execute({
    fixture_path: path.join(ROOT, 'data', 'fixtures', 'verify_case.json'),
  })
  const payload = out.structured || {}
  check('flood_crosslang_check', payload.verified === true, `fatal=${payload.fatal_count} findings=${(payload.findings || []).length}`)
}

// 6. the fault-injection segment of the demo
{
  const out = await registered.get('flood_run_event').execute({
    bounds: BOUNDS,
    rain_mm: 100,
    cn: 85,
    tag: 'rehearsal_bad',
    dem_file: path.join(ROOT, 'data', 'fixtures', 'bad_dem_units.tif'),
  })
  const payload = out.structured || {}
  const raster = Boolean(payload.artifacts && payload.artifacts.depth_tif)
  check('fault_injection', payload.outcome === 'rejected' && !raster,
    `outcome=${payload.outcome} raster_produced=${raster}`)
}

const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} passed`)
if (failed.length) {
  console.log('\nThe harness path does NOT reproduce the documented numbers:')
  for (const f of failed) console.log(`  - ${f.name}: ${f.detail}`)
  console.log('\nDo not record the demo until this is resolved.')
}
process.exit(failed.length ? 1 : 0)

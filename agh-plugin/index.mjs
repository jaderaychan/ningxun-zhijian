// AGH backend tool plugin for floodkit.
//
// Registers seven tools on the harness so the agent can drive a real
// geospatial/hydrological pipeline instead of describing one:
//
//   flood_selftest        analytic checks (no network, no writes)
//   flood_resolve_place   Nanjing study-area name -> bounding box
//   flood_weather         forecast rainfall as a range across three models
//   flood_fetch_dem       download Copernicus DEM tiles for a bounding box
//   flood_run_event       run one rainfall event end to end and write artifacts
//   flood_publish_report  render the interactive HTML page and serve it locally
//   flood_verify_report   re-derive the indicators from a report, in JavaScript
//   flood_crosslang_check re-derive the solver itself in JavaScript, from a fixture
//
// Two of those exist so the agent can question its input rather than only its
// output: flood_resolve_place stops it inventing a study area, and
// flood_weather lets it notice that the rainfall figure the user asserted is
// not what the models forecast. Checking the arithmetic was never the weakness;
// checking the premise is.
//
// The verifier is deliberately written in a second language: it recomputes the
// mass balance from the raw volumes stored in the report, so a Python-side
// arithmetic or unit mistake cannot hide behind its own author's asserts.
//
// No imports: the installed snapshot needs no dependency resolution.

import { execFile } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { readFile, stat } from 'node:fs/promises'
import { createServer } from 'node:http'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { verifyFixture } from './verify-core.mjs'

const Kind = Symbol.for('TypeBox.Kind')

const obj = (properties, required) => ({
  [Kind]: 'Object',
  type: 'object',
  properties,
  required,
  additionalProperties: false,
})
const str = (extra = {}) => ({ [Kind]: 'String', type: 'string', ...extra })
const num = (extra = {}) => ({ [Kind]: 'Number', type: 'number', ...extra })
const bool = (extra = {}) => ({ [Kind]: 'Boolean', type: 'boolean', ...extra })
const arr = (items, extra = {}) => ({ [Kind]: 'Array', type: 'array', items, ...extra })

const BOUNDS = arr(num(), { minItems: 4, maxItems: 4 })

// AGH requires all EIGHT keys of tool metadata to be explicitly present, and a
// missing key fails the extension factory outright with E_TOOLDEF_META — the
// plugin then never leaves `starting` and the workbench refuses to enable it.
// `costHint` is the one that is easy to forget because the value may be
// undefined; the key itself still has to exist. See
// packages/extension-api/docs/tools-meta.md.
const READ_ONLY = {
  isReadOnly: true,
  isDestructive: false,
  isConcurrencySafe: true,
  isOpenWorld: false,
  replay: 'safe',
  costHint: undefined,
  deferLoading: false,
  requiresApproval: 'never',
}
const WRITES = {
  isReadOnly: false,
  isDestructive: false,
  isConcurrencySafe: false,
  isOpenWorld: true,
  // `replay` accepts only safe | never | idempotent (extension-api/src/tool.ts
  // checkToolMeta). "never" is the honest choice for a tool that writes files:
  // a crash must not silently re-run it and synthesise an unknown outcome.
  replay: 'never',
  costHint: undefined,
  deferLoading: false,
  requiresApproval: 'always',
}
// A tool that only reads from the network, but does cache the response so a
// demo does not hammer a free API. isReadOnly stays false because the tool
// really does write (data/weather/), and the metadata doc defines isReadOnly as
// "the tool declares no writes" -- claiming it here would be a lie the taint and
// sandbox policies are entitled to rely on. It writes nothing destructive, so it
// needs no approval; isOpenWorld marks the payload untrusted, which is what
// makes an injected instruction inside a weather response carry no authority.
const NETWORK_READ = {
  isReadOnly: false,
  isDestructive: false,
  isConcurrencySafe: true,
  isOpenWorld: true,
  replay: 'safe',
  costHint: undefined,
  deferLoading: false,
  requiresApproval: 'never',
}

// Rendering the shareable page writes a derived file and reads cached tiles. It is
// not destructive and it is idempotent for a given run, so it needs no approval —
// asking for one would put a modal dialog between the user's question and the
// answer they asked to see. It may fetch DEM/land-cover tiles if a cache is cold,
// hence isOpenWorld; two renders of the same tag would race on one output file,
// hence not concurrency safe.
const RENDERS = {
  isReadOnly: false,
  isDestructive: false,
  isConcurrencySafe: false,
  isOpenWorld: true,
  replay: 'safe',
  costHint: { wallMs: 120000 },
  deferLoading: false,
  requiresApproval: 'never',
}

// `new URL(import.meta.url).pathname` is percent-encoded: a project sitting at
// "C:\...\江苏省ai+黑客松比赛" resolves to "C:\...\%E6%B1%9F%E8%8B%8F...", which is not a
// directory. That silently broke every Python call this plugin makes -- cwd did not
// exist, so `.floodkit.json`, `.venv` and `.pylibs` were all "missing" and the tools
// fell back to a bare `python`. fileURLToPath decodes and handles the Windows drive
// letter, which is what the string surgery here was trying to do by hand.
function projectRoot() {
  if (process.env.FLOODKIT_ROOT) return process.env.FLOODKIT_ROOT
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
}

// Resolution order keeps the plugin portable: an explicit env var wins, then the
// machine-local config written by scripts/setup.ps1, then a plain `python`.
function localConfig() {
  try {
    const file = path.join(projectRoot(), '.floodkit.json')
    if (!existsSync(file)) return {}
    return JSON.parse(readFileSync(file, 'utf8'))
  } catch {
    return {}
  }
}

function pythonBin() {
  if (process.env.FLOODKIT_PYTHON) return process.env.FLOODKIT_PYTHON
  const cfg = localConfig()
  if (cfg.python) return cfg.python
  // A venv puts its interpreter in Scripts\ on Windows and in bin/ everywhere else.
  // Probing only the Windows path meant a macOS or Linux collaborator silently fell
  // through to whatever `python` happened to be on PATH — typically one without
  // numpy or rasterio — and the failure surfaced as an import error from deep inside
  // a tool call rather than as "your interpreter is wrong".
  for (const candidate of [
    path.join(projectRoot(), '.venv', 'Scripts', 'python.exe'),
    path.join(projectRoot(), '.venv', 'bin', 'python'),
    path.join(projectRoot(), '.venv', 'bin', 'python3'),
  ]) {
    if (existsSync(candidate)) return candidate
  }
  return 'python'
}

function pythonPath() {
  const root = projectRoot()
  const cfg = localConfig()
  const entries = []
  if (cfg.pylibs) entries.push(cfg.pylibs)
  entries.push(path.join(root, '.pylibs'), root)
  if (process.env.PYTHONPATH) entries.push(process.env.PYTHONPATH)
  return entries.filter(Boolean).join(path.delimiter)
}

// `script` runs a file inside the project instead of the `floodkit` CLI. The output
// tools (the report map, the interactive page, the GeoJSON export) are scripts by
// design: they render artifacts, they are not part of the science core the CLI
// exposes. Everything else — interpreter, PYTHONPATH, cwd, utf-8 — stays identical,
// because a second code path here is how a plugin ends up invoking a different
// Python than the one it tested against.
function runPython(args, { timeoutMs = 900_000, script = null } = {}) {
  const root = projectRoot()
  const env = {
    ...process.env,
    PYTHONPATH: pythonPath(),
    PYTHONUTF8: '1',
    MPLCONFIGDIR: process.env.MPLCONFIGDIR || path.join(root, '.mplconfig'),
  }
  const argv = script
    ? ['-X', 'utf8', script, ...args]
    : ['-X', 'utf8', '-m', 'floodkit', ...args]
  return new Promise((resolve) => {
    execFile(
      pythonBin(),
      argv,
      { cwd: root, env, timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024, windowsHide: true },
      (error, stdout, stderr) => {
        let parsed = null
        if (stdout) {
          try {
            parsed = JSON.parse(stdout)
          } catch {
            parsed = null
          }
        }
        // A tool failure must come back as a structured result the agent can
        // reason about, never as an exception that collapses the turn.
        resolve({
          exitCode: error && typeof error.code === 'number' ? error.code : error ? 1 : 0,
          ok: !error,
          result: parsed,
          stdout: parsed ? undefined : String(stdout || '').slice(-4000),
          stderr: String(stderr || '').slice(-4000),
          timedOut: Boolean(error && error.killed),
        })
      },
    )
  })
}

// AGH validates `structured` with isJsonValue (core/src/effects/tool-dispatch.ts):
// only null, string, boolean, finite number, plain object and array are JSON
// values. `undefined` is NOT one, and neither are NaN or Infinity — a payload
// carrying any of them is rejected as an invalid ToolResult, and the caller only
// ever sees "workspace tool returned an invalid ToolResult".
//
// This bit us for real: `stderr: run.stderr || undefined` was harmless while the
// process failed (stderr was a non-empty string) and became an undefined key the
// moment the process succeeded, so the tool broke exactly when it started working.
function jsonSafe(value) {
  if (value === undefined) return undefined // callers drop the key
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (Array.isArray(value)) return value.map((item) => jsonSafe(item) ?? null)
  if (typeof value === 'object') {
    const out = {}
    for (const [key, item] of Object.entries(value)) {
      const cleaned = jsonSafe(item)
      if (cleaned !== undefined) out[key] = cleaned
    }
    return out
  }
  return String(value)
}

function envelope(payload, summary) {
  const clean = jsonSafe(payload) || {}
  return {
    content: [{ type: 'text', text: JSON.stringify(clean) }],
    structured: { ...clean, summary: String(summary) },
  }
}

// --------------------------------------------------------------- preview server
//
// A file:// link is not something the user can be handed: the workbench runs in a
// browser, and a page served over http is not allowed to navigate to file:// — so
// "here is the path" reads as "go find it yourself". A loopback HTTP server solves
// it with no dependency and no daemon:
//
//   * bound to 127.0.0.1, so nothing is exposed to the network;
//   * port 0, so the OS picks a free one and it cannot collide with the workbench;
//   * rooted at data/runs, read-only, with the resolved path checked to stay inside
//     it — a request for ../../.floodkit.json is refused, not resolved;
//   * one server per plugin instance, started on first use and torn down with the
//     harness, which is what keeps it from being an orphaned background process.

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.geojson': 'application/geo+json; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
  '.png': 'image/png',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.pdf': 'application/pdf',
  '.mp4': 'video/mp4',
  '.tif': 'image/tiff',
  '.zip': 'application/zip',
}

let previewServer = null
let previewPort = null

async function ensurePreviewServer() {
  if (previewServer) return previewPort
  const root = path.join(projectRoot(), 'data', 'runs')
  const server = createServer(async (request, response) => {
    const send = (code, body, type = 'text/plain; charset=utf-8') => {
      response.writeHead(code, { 'content-type': type, 'cache-control': 'no-store' })
      response.end(body)
    }
    try {
      const relative = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname)
      const target = path.resolve(root, relative.replace(/^\/+/, ''))
      if (target !== root && !target.startsWith(root + path.sep)) {
        send(403, 'forbidden: the preview server only serves data/runs')
        return
      }
      const info = await stat(target)
      if (!info.isFile()) {
        send(404, 'not found')
        return
      }
      const body = await readFile(target)
      response.writeHead(200, {
        'content-type': MIME[path.extname(target).toLowerCase()] || 'application/octet-stream',
        'content-length': body.length,
        'cache-control': 'no-store',
      })
      response.end(body)
    } catch {
      send(404, 'not found')
    }
  })
  previewPort = await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve(server.address().port))
  })
  previewServer = server
  return previewPort
}

function previewUrl(tag, file) {
  return `http://127.0.0.1:${previewPort}/${encodeURIComponent(tag)}/${encodeURIComponent(file)}`
}

const floodTools = {
  inject: ['extension'],
  apply(ctx) {
    const agnes = ctx.extension()

    agnes.registerTool({
      name: 'flood_selftest',
      description:
        'Run floodkit analytic self-checks (depression filling, stage-volume inversion, SCS-CN closed form). ' +
        'Use before trusting any model output; it needs no network and writes nothing.',
      parameters: obj({}, []),
      meta: READ_ONLY,
      async execute() {
        const run = await runPython(['selftest'])
        const payload = {
          ok: run.ok && Boolean(run.result && run.result.ok),
          exit_code: run.exitCode,
          checks: (run.result && run.result.checks) || [],
          stderr: run.stderr || undefined,
        }
        return envelope(payload, `flood_selftest: ${payload.ok ? 'all analytic checks passed' : 'FAILED'}`)
      },
    })

    agnes.registerTool({
      name: 'flood_fetch_dem',
      description:
        'Download the Copernicus DEM GLO-30 tiles covering a bounding box into the local cache. ' +
        'Bounds are WGS84 degrees: [west, south, east, north]. Returns the tile paths and cache directory.',
      parameters: obj({ bounds: BOUNDS }, ['bounds']),
      meta: WRITES,
      async execute({ bounds }) {
        const run = await runPython(['fetch-dem', '--bounds', ...bounds.map(String)])
        const payload = {
          ok: run.ok && Boolean(run.result && run.result.ok),
          exit_code: run.exitCode,
          bounds,
          tiles: (run.result && run.result.tiles) || [],
          cache_dir: run.result && run.result.cache_dir,
          error: run.result && run.result.message,
          stderr: run.stderr || undefined,
        }
        return envelope(payload, `flood_fetch_dem: ${payload.tiles.length} tile(s) ready`)
      },
    })

    agnes.registerTool({
      name: 'flood_resolve_place',
      description:
        'Turn a Nanjing study-area name (e.g. 南京主城区, 南京, 南京全域) into a bounding box. ' +
        'This project covers Nanjing ONLY; other cities are refused on purpose, because their ' +
        'curve numbers, elevation envelope and exposure layers were never validated. ' +
        'Call this when the user names a place instead of giving coordinates, and then repeat ' +
        'the returned bounds back to the user in your reply so they can object before you run. ' +
        'If it cannot resolve the name, ask the user for coordinates; never invent a box.',
      parameters: obj({ name: str({ minLength: 1, description: 'place name, e.g. 南京主城区' }) }, ['name']),
      meta: READ_ONLY,
      async execute({ name }) {
        const run = await runPython(['places', '--name', String(name)], { timeoutMs: 60_000 })
        const result = run.result || {}
        const payload = {
          ok: run.ok && Boolean(result.ok),
          exit_code: run.exitCode,
          query: name,
          code: result.code,
          message: result.message,
          name: result.name,
          bounds: result.bounds,
          center: result.center,
          approximate: result.approximate,
          source: result.source,
          note: result.note,
          stderr: run.stderr || undefined,
        }
        return envelope(
          payload,
          payload.ok
            ? `flood_resolve_place: ${payload.name} -> [${(payload.bounds || []).join(', ')}]`
            : 'flood_resolve_place: unresolvable — ask the user for coordinates',
        )
      },
    })

    agnes.registerTool({
      name: 'flood_weather',
      description:
        'Look up forecast rainfall for a Nanjing study area, as a RANGE across three independent ' +
        'models (ECMWF, GFS, ICON), not a single number. ' +
        'Use it to check the premise of a rainfall figure the user asserted: if the forecast ' +
        'disagrees with what the user said, say so and offer to run both — do not silently pick one. ' +
        'The forecast is evidence about the premise, NOT the scenario to run: unless the user ' +
        'explicitly asked for the forecast value, keep using the number they gave. ' +
        'A failed lookup means "could not ask", never "no rain". ' +
        'Only times within the next 15 days are in scope; anything further out or already past ' +
        'is refused with TIME_OUT_OF_SCOPE — report that to the user rather than rephrasing the ' +
        'question to fit. ' +
        'Defaults to the 南京主城区 box when no place or bounds are given.',
      parameters: obj(
        {
          at: str({
            minLength: 10,
            description: 'local start time, e.g. 2026-10-11T12:00 (Asia/Shanghai). Compute this from the current date.',
          }),
          place: str({ description: 'Nanjing study-area name; defaults to 南京主城区' }),
          bounds: BOUNDS,
          window_hours: num({ minimum: 1, maximum: 24, description: 'default 1' }),
        },
        ['at'],
      ),
      meta: NETWORK_READ,
      async execute({ at, place, bounds, window_hours } = {}) {
        const cli = ['forecast', '--at', String(at)]
        if (Array.isArray(bounds) && bounds.length === 4) {
          cli.push('--bounds', ...bounds.map(String))
        } else {
          cli.push('--place', String(place || '南京主城区'))
        }
        if (typeof window_hours === 'number') cli.push('--window-hours', String(Math.round(window_hours)))
        const run = await runPython(cli, { timeoutMs: 90_000 })
        const result = run.result || {}
        const payload = {
          ok: run.ok && Boolean(result.ok),
          exit_code: run.exitCode,
          code: result.code,
          message: result.message,
          // When the asked-for hour is outside the model horizon the caller needs
          // to know what IS covered, or it can only tell the user "no".
          available_range: result.available_range,
          // Same for the project window: a refusal must say which times would
          // have been accepted, so the user can pick one instead of guessing.
          scope: result.scope,
          place: result.place,
          source: result.source,
          retrieved_at: result.retrieved_at,
          from_cache: result.from_cache,
          window: result.window,
          per_model_mm: result.per_model_mm,
          min_mm: result.min_mm,
          max_mm: result.max_mm,
          median_mm: result.median_mm,
          spread_source: result.spread_source,
          day_context: result.day_context,
          interpretation: result.interpretation,
          warning: result.warning,
          warning_past: result.warning_past,
          qweather: result.qweather,
          stderr: run.stderr || undefined,
        }
        return envelope(
          payload,
          payload.ok
            ? `flood_weather: ${payload.min_mm}-${payload.max_mm} mm forecast starting ${(payload.window || {}).start}`
            : `flood_weather: ${payload.code || 'unavailable'} — a lookup failure is not "no rain"`,
        )
      },
    })

    agnes.registerTool({
      name: 'flood_run_event',
      description:
        'Run one rainfall event end to end: clip and reproject the DEM to a metric grid, gate input quality, ' +
        'fill depressions, generate SCS-CN runoff, solve depression ponding, then write a depth raster, a ' +
        'basin table, a JSON report and a map. Returns every machine-checkable indicator. When the input ' +
        'quality gate rejects the data, no depth raster is produced and `outcome` is "rejected" — report that ' +
        'instead of inventing numbers.',
      parameters: obj(
        {
          bounds: BOUNDS,
          rain_mm: num({ exclusiveMinimum: 0, maximum: 2000, description: 'event rainfall depth in millimetres' }),
          cn: num({
            minimum: 30,
            maximum: 100,
            description:
              'SCS curve number. Ignored when cn_source is landcover, which is the configuration the ' +
              'reported results use — do not try to guess a better value for it.',
          }),
          tag: str({ description: 'short run label used in artifact file names' }),
          cn_source: str({
            enum: ['uniform', 'landcover'],
            description:
              'uniform uses the scalar cn everywhere; landcover derives a spatial CN from ESA WorldCover 10 m ' +
              'and is the configuration the reported results use',
          }),
          exposure: bool({
            description:
              'also analyse OpenStreetMap exposure: roads, buildings, underpasses/tunnels and critical ' +
              'facilities such as subway entrances, schools and hospitals',
          }),
          dem_file: str({
            description:
              'optional GeoTIFF to analyse instead of the cached Copernicus tile. Point it at a deliberately ' +
              'bad file to demonstrate that the quality gate rejects unusable input instead of publishing a ' +
              'plausible-looking result',
          }),
          envelope_min: num({
            description:
              'OVERRIDE — leave unset. The validated default for the Nanjing study area is -50..400 m and ' +
              'the quality gate checks the real DEM against it. Do not estimate a "plausible" band: a guessed ' +
              'envelope is narrower than the terrain, so the gate rejects a perfectly good DEM (this happened: ' +
              'a guessed 5..70 m rejected a valid tile whose true range is 0.7..125.3 m). Set it only when you ' +
              'hold external evidence the DEM is right and the default is wrong.',
          }),
          envelope_max: num({
            description:
              'OVERRIDE — leave unset, same reasoning as envelope_min. Only a study area that genuinely ' +
              'includes higher ground needs it (the 405 km² wide box reaches 442.9 m and passes 500).',
          }),
          checkpoints: arr(BOUNDS, {
            description: 'optional [lon, lat] pairs of known waterlogging points for a hit-rate check',
          }),
        },
        ['bounds', 'rain_mm', 'cn', 'tag'],
      ),
      meta: WRITES,
      async execute(args) {
        const cli = [
          'run',
          '--bounds', ...args.bounds.map(String),
          '--rain-mm', String(args.rain_mm),
          '--cn', String(args.cn),
          '--tag', String(args.tag),
        ]
        // Without these the agent can only ever run the simplified configuration:
        // no land-cover CN and no exposure analysis, so none of the reported
        // results would be reachable through the harness.
        if (args.cn_source) cli.push('--cn-source', String(args.cn_source))
        if (args.exposure) cli.push('--exposure')
        if (args.dem_file) cli.push('--dem-file', String(args.dem_file))
        if (typeof args.envelope_min === 'number') cli.push('--envelope-min', String(args.envelope_min))
        if (typeof args.envelope_max === 'number') cli.push('--envelope-max', String(args.envelope_max))
        const run = await runPython(cli)
        if (!run.result) {
          return envelope(
            { ok: false, exit_code: run.exitCode, stdout: run.stdout, stderr: run.stderr, timed_out: run.timedOut },
            'flood_run_event: the analysis process failed before producing a report',
          )
        }
        const r = run.result
        const payload = {
          ok: run.ok,
          exit_code: run.exitCode,
          outcome: r.outcome,
          rejected_reason: r.reason,
          quality: r.quality,
          domain: r.domain,
          terrain: r.terrain,
          runoff: r.runoff,
          ponding: r.ponding,
          water_balance: r.water_balance,
          checks: r.checks,
          basin_qc: r.basin_qc,
          water_bodies: r.water_bodies,
          exposure: r.exposure,
          top_basins: (r.basins || []).slice(0, 12),
          artifacts: r.artifacts,
        }
        return envelope(payload, `flood_run_event: outcome=${r.outcome}, ponded ${((r.ponding || {}).pond_area_m2 / 1e6 || 0).toFixed(3)} km2`)
      },
    })

    agnes.registerTool({
      name: 'flood_publish_report',
      description:
        'Render the interactive HTML report for a finished run and serve it on a local address, then ' +
        'GIVE THE USER THE `url` VERBATIM. Do this whenever the ask is to see the result rather than ' +
        'only read numbers: the page is one self-contained file that opens in a browser with no network, ' +
        'and it carries the risk map, the machine-checkable indicators with their field paths, the ranked ' +
        'underpass and facility lists, hover-to-read depth, layer toggles and a terrain/water profile tool. ' +
        'The path alone is not an answer — the workbench runs in a browser and cannot navigate to file://, ' +
        'which is why this returns an http://127.0.0.1 address. Rendering takes up to two minutes on a ' +
        'large study area; pass render=false to serve an already-rendered page. ' +
        'A tag with no run yet is reported as NOT_FOUND rather than rendered from nothing.',
      parameters: obj(
        {
          tag: str({ description: 'run label; the run must already exist from flood_run_event' }),
          render: bool({
            description:
              're-render the page first (default true). Set false to serve the existing page as-is.',
          }),
          serve: bool({
            description:
              'start the loopback preview server and return an http url (default true). Set false when ' +
              'only the filesystem path is wanted.',
          }),
        },
        ['tag'],
      ),
      meta: RENDERS,
      async execute(args) {
        const tag = String(args.tag || '').trim()
        // The tag becomes a directory name and two file names. Constrain it here as
        // well as in the schema, because a tag like "../.." would otherwise decide
        // which directory gets written to.
        if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(tag)) {
          return envelope(
            { ok: false, code: 'BAD_TAG', tag },
            'flood_publish_report: the tag may only contain letters, digits, dot, dash and underscore',
          )
        }

        const root = projectRoot()
        const runDir = path.join(root, 'data', 'runs', tag)
        const reportPath = path.join(runDir, `${tag}_report.json`)
        const htmlPath = path.join(runDir, `${tag}_report.html`)

        if (!existsSync(reportPath)) {
          return envelope(
            { ok: false, code: 'NOT_FOUND', tag, looked_for: reportPath },
            `flood_publish_report: no run named ${tag} — call flood_run_event first`,
          )
        }

        const wantRender = args.render !== false
        let renderLog = null
        if (wantRender) {
          const run = await runPython([tag], {
            script: path.join('scripts', 'make_report_html.py'),
            timeoutMs: 600_000,
          })
          renderLog = String(run.stdout || run.stderr || '').trim().split('\n').slice(-6).join('\n')
          if (!existsSync(htmlPath)) {
            return envelope(
              { ok: false, code: 'RENDER_FAILED', tag, exit_code: run.exitCode, log: renderLog.slice(-2000) },
              'flood_publish_report: the page was not produced; the log is in the payload',
            )
          }
        } else if (!existsSync(htmlPath)) {
          return envelope(
            { ok: false, code: 'NOT_RENDERED', tag, looked_for: htmlPath },
            'flood_publish_report: no page yet — call again with render=true',
          )
        }

        const sizeBytes = (await stat(htmlPath)).size
        let url = null
        let served = false
        if (args.serve !== false) {
          await ensurePreviewServer()
          url = previewUrl(tag, `${tag}_report.html`)
          served = true
        }

        // The headline numbers travel with the URL so the answer and the page cannot
        // drift apart: whoever reads this payload quotes the same report the page embeds.
        let headline = null
        try {
          const report = JSON.parse(await readFile(reportPath, 'utf8'))
          const ponding = report.ponding || {}
          const exposure = report.exposure || {}
          headline = {
            outcome: report.outcome,
            rainfall_mm: report.rainfall_mm,
            ponded_km2: Number(((ponding.pond_area_m2 || 0) / 1e6).toFixed(2)),
            max_depth_m: ponding.max_depth_m,
            underpass_ways_flooded: (exposure.underpasses || {}).underpass_ways_flooded,
            underpass_life_safety: (exposure.underpasses || {}).life_safety_count,
            facilities_flooded: (exposure.critical_facilities || {}).facilities_flooded,
            subway_entrances_flooded: (exposure.critical_facilities || {}).subway_entrances_flooded,
          }
        } catch {
          headline = null
        }

        const payload = {
          ok: true,
          tag,
          url,
          served,
          file_url: pathToFileURL(htmlPath).href,
          html_path: htmlPath,
          size_bytes: sizeBytes,
          rendered_now: wantRender,
          headline,
          render_log: renderLog,
          // Siblings are reachable on the same server, so the agent can offer the
          // print sheet or the vector export without a second tool call.
          also_available: served
            ? ['_report_map.png', '_report_map.pdf', '_ponding.geojson', '_exposure.geojson', '_report.json']
                .filter((suffix) => existsSync(path.join(runDir, tag + suffix)))
                .map((suffix) => previewUrl(tag, tag + suffix))
            : [],
        }
        return envelope(
          payload,
          url
            ? `flood_publish_report: ready at ${url}`
            : `flood_publish_report: page at ${htmlPath} (${Math.round(sizeBytes / 1024)} KB)`,
        )
      },
    })

    agnes.registerTool({
      name: 'flood_crosslang_check',
      description:
        'Cross-language verification: re-derive the SCS-CN runoff/retention terms, the stage-volume curve ' +
        'inversion and a whole depression water level using an independent JavaScript implementation, then ' +
        'compare against the values the Python core produced. Use it to confirm the numerical core before ' +
        'quoting any depth or volume. Needs no model and no network. ' +
        'The fixture is a separate artifact exported by tests/make_verify_fixture.py — it is NOT the run ' +
        'report produced by flood_run_event. Call this tool with no arguments to use the bundled fixture.',
      parameters: obj(
        {
          fixture_path: str({
            description:
              'optional; omit it to use the bundled fixture at data/fixtures/verify_case.json. Do not pass a ' +
              'flood_run_event report here — reports and fixtures have different shapes.',
          }),
        },
        [],
      ),
      meta: READ_ONLY,
      async execute({ fixture_path } = {}) {
        const bundled = path.join(projectRoot(), 'data', 'fixtures', 'verify_case.json')
        const target = fixture_path || bundled
        if (!existsSync(target)) {
          return envelope(
            {
              ok: false,
              verified: false,
              findings: [
                {
                  level: 'fatal',
                  code: 'FIXTURE_MISSING',
                  message: `run tests/make_verify_fixture.py first; looked for ${target}`,
                },
              ],
            },
            'flood_crosslang_check: no fixture to check',
          )
        }
        let fixture
        try {
          fixture = JSON.parse(readFileSync(target, 'utf8'))
        } catch (error) {
          return envelope(
            { ok: false, verified: false, findings: [{ level: 'fatal', code: 'FIXTURE_UNREADABLE', message: String(error) }] },
            'flood_crosslang_check: fixture is not valid JSON',
          )
        }
        // A model will happily hand us the run report here, because both are "the JSON
        // from the last step". Failing with a raw `groundElevations is not iterable`
        // tells it nothing and costs a round trip, so the shape is checked first and
        // the message says what to pass instead.
        //
        // The check has to be specific: a report ALSO carries a `basins` key, so merely
        // looking for one would wave the report through. The fixture's basin entries
        // carry `ground_elevations_m`, which a report never has.
        const firstBasin = (Array.isArray(fixture.basins) ? fixture.basins[0] : undefined) ?? fixture.basin
        const looksLikeFixture =
          Array.isArray(fixture.scs_samples) &&
          firstBasin !== undefined &&
          Array.isArray(firstBasin.ground_elevations_m)
        if (!looksLikeFixture) {
          const isReport = fixture.water_balance !== undefined || fixture.outcome !== undefined
          return envelope(
            {
              ok: false,
              verified: false,
              fixture: target,
              findings: [
                {
                  level: 'fatal',
                  code: 'FIXTURE_SHAPE',
                  message:
                    (isReport
                      ? `the file at ${target} is a flood_run_event report, not a cross-language fixture. `
                      : `the file at ${target} is not a cross-language fixture. `) +
                    `A fixture needs scs_samples and basins[].ground_elevations_m, and is produced by ` +
                    `tests/make_verify_fixture.py. Call this tool with no arguments to use the bundled ` +
                    `fixture at ${bundled}.`,
                },
              ],
            },
            'flood_crosslang_check: wrong file — pass the fixture, not a run report',
          )
        }
        let outcome
        try {
          outcome = verifyFixture(fixture)
        } catch (error) {
          return envelope(
            {
              ok: false,
              verified: false,
              fixture: target,
              findings: [{ level: 'fatal', code: 'FIXTURE_INVALID', message: String(error) }],
            },
            'flood_crosslang_check: fixture could not be verified',
          )
        }
        return envelope(
          { ok: outcome.verified, fixture: target, ...outcome },
          outcome.verified
            ? 'flood_crosslang_check: Python and JavaScript agree'
            : `flood_crosslang_check: ${outcome.fatal_count} fatal disagreement(s)`,
        )
      },
    })

    agnes.registerTool({
      name: 'flood_verify_report',
      description:
        'Independently audit a floodkit report file. Recomputes the whole-domain mass balance from the raw ' +
        'volumes in the report (in JavaScript, not Python), checks the indicator set is present and passing, ' +
        'and enforces fail-closed behaviour: a rejected run must not have a depth raster. Use this before ' +
        'quoting any number to the user.',
      parameters: obj({ report_path: str({ minLength: 1 }) }, ['report_path']),
      meta: READ_ONLY,
      async execute({ report_path }) {
        const findings = []
        if (!existsSync(report_path)) {
          return envelope(
            { ok: false, verified: false, findings: [{ level: 'fatal', code: 'REPORT_MISSING', message: report_path }] },
            'flood_verify_report: report file not found',
          )
        }
        let report
        try {
          report = JSON.parse(readFileSync(report_path, 'utf8'))
        } catch (error) {
          return envelope(
            { ok: false, verified: false, findings: [{ level: 'fatal', code: 'REPORT_UNREADABLE', message: String(error) }] },
            'flood_verify_report: report is not valid JSON',
          )
        }

        const wb = report.water_balance || {}
        const rain = Number(wb.rain_volume_m3 || 0)
        const accounted =
          Number(wb.initial_abstraction_m3 || 0) +
          Number(wb.continuing_retention_m3 || 0) +
          Number(wb.stored_m3 || 0) +
          Number(wb.spilled_m3 || 0) +
          Number(wb.outflow_m3 || 0)
        const residual = rain - accounted
        const relError = rain > 0 ? Math.abs(residual) / rain : 0
        findings.push({
          level: relError <= 0.01 ? 'pass' : 'fatal',
          code: 'MASS_BALANCE_RECHECK',
          message: 'independent recombination of the reported volumes',
          residual_m3: residual,
          relative_error: relError,
          tolerance: 0.01,
        })

        const checks = report.checks || {}
        const expected = ['D01_water_balance', 'D01b_raster_vs_solver', 'D04_sensitivity']
        for (const name of expected) {
          const present = Object.prototype.hasOwnProperty.call(checks, name)
          findings.push({
            level: present && checks[name].passed ? 'pass' : present ? 'fatal' : 'fatal',
            code: present ? (checks[name].passed ? 'INDICATOR_PASS' : 'INDICATOR_FAIL') : 'INDICATOR_MISSING',
            message: name,
          })
        }

        const qualityStatus = (report.quality || {}).status
        const depthArtifact = report.artifacts && report.artifacts.depth_tif
        if (qualityStatus === 'rejected') {
          findings.push({
            level: report.outcome === 'rejected' && !depthArtifact ? 'pass' : 'fatal',
            code: 'FAIL_CLOSED',
            message: 'a rejected quality gate must not leave a depth raster behind',
          })
        } else {
          findings.push({ level: 'pass', code: 'QUALITY_GATE', message: `quality status ${qualityStatus}` })
        }

        const tiny = (report.basin_qc || {}).deep_and_tiny_count
        if (typeof tiny === 'number' && tiny > 0) {
          findings.push({
            level: 'warn',
            code: 'ARTIFACT_CANDIDATES',
            message: `${tiny} deep-but-tiny ponds need field verification before being quoted as flood depths`,
          })
        }

        const fatal = findings.filter((f) => f.level === 'fatal')
        const payload = {
          ok: fatal.length === 0,
          verified: fatal.length === 0,
          outcome: report.outcome,
          findings,
          fatal_count: fatal.length,
        }
        return envelope(
          payload,
          fatal.length === 0
            ? 'flood_verify_report: report independently verified'
            : `flood_verify_report: ${fatal.length} fatal finding(s)`,
        )
      },
    })
  },
}

export { floodTools }
export default floodTools

// Plugin sync + install preflight for the FloodKit AGH extension.
//
// Why this is not a scripted *installer* any more
// -----------------------------------------------
// `agh package trust <id> <integrity> <capabilityHash>` requires the capability
// hash, and on this AGH build nothing on the command line prints it:
//
//   * `runPackageCommand`'s `inspect` branch calls `formatPreview(preview)`
//     unconditionally and ignores the global `--json` flag, so the output is prose;
//   * `formatPreview` (packages/cli-tui/src/package-admin.ts) prints the id, the
//     integrity, the contributions and the warnings — and no capability hash.
//
// The workbench gets that hash from the daemon response and shows it for the
// operator to confirm, which is exactly the check `trust` exists to enforce. A
// script that skipped it would be lying about what it had verified, so this one
// stops short and hands over.
//
// What it does do, and why that is worth running: sync the plugin files where the
// repo expects them, then run the inspect preflight so a blocker surfaces *before*
// you open the UI — and print the integrity it saw, so you can compare it with the
// hash the workbench displays. If those two differ, something changed in between
// and you should not confirm.
//
// Run it from 安装插件.cmd, or with node directly.

import { spawn } from 'node:child_process'
import { existsSync, readFileSync, readdirSync, copyFileSync, mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const projectRoot = resolve(here, '..')
const repo = join(projectRoot, '_agh_src', 'agnes-harness-main')
const entry = join(repo, 'packages', 'cli', 'dist', 'local', 'agnes.mjs')
const source = join(repo, 'flood-tools')
const pluginDir = join(projectRoot, 'agh-plugin')

const say = (message) => console.log(message)
const fail = (message) => {
  console.error(`\n[失败] ${message}`)
  process.exitCode = 1
}

say('='.repeat(64))
say('  FloodKit AGH 插件：同步 + 安装前预检')
say('='.repeat(64))
say('')

if (!existsSync(entry)) {
  fail(`找不到 AGH 入口：${entry}\n       请先运行 scripts\\setup.ps1，或确认 _agh_src 目录存在。`)
  process.exit()
}

// 1. put the plugin sources where the repo expects them
mkdirSync(source, { recursive: true })
const shipped = readdirSync(pluginDir).filter((name) => !name.startsWith('_'))
for (const name of shipped) {
  copyFileSync(join(pluginDir, name), join(source, name))
}
say(`[1/2] 已同步插件文件到 ${source}`)
say(`      ${shipped.join(', ')}`)
say('')

// 2. preflight: let AGH parse the package and say what it sees
const run = (args) =>
  new Promise((done) => {
    const child = spawn(process.execPath, [entry, ...args], {
      cwd: repo,
      env: { ...process.env, AGNES_PROFILE: process.env.AGNES_PROFILE || 'local-dev' },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let out = ''
    let err = ''
    child.stdout.on('data', (chunk) => (out += chunk))
    child.stderr.on('data', (chunk) => (err += chunk))
    child.on('close', (code) => done({ code, out, err }))
  })

say('[2/2] 预检：让 AGH 解析这个包 …')
const inspected = await run(['package', 'inspect', 'file:./flood-tools'])
const text = `${inspected.out}${inspected.err}`.trim()
say(text.split('\n').map((line) => `      ${line}`).join('\n'))
say('')

if (inspected.code !== 0) {
  fail('AGH 拒绝解析这个包，先解决上面的报错再安装。')
  process.exit()
}

const idMatch = text.match(/^Preview\s+(\S+?)@(\S+)/m)
const integrityMatch = text.match(/^integrity\s+(\S+)/m)
const warningMatch = text.match(/^warnings\s+(.+)$/m)
if (!idMatch || !integrityMatch) {
  fail('预检输出里没有 id 或 integrity，无法核对；把上面的原始输出发我。')
  process.exit()
}
const [, id, version] = idMatch
const integrity = integrityMatch[1]
const warnings = (warningMatch && warningMatch[1]) || 'none'

if (/blocker/i.test(text)) {
  fail(`预检报告了 blockers：\n${text}`)
  process.exit()
}

const tools = (() => {
  try {
    const body = readFileSync(join(source, 'index.mjs'), 'utf8')
    return [...body.matchAll(/name: '(flood_[a-z_]+)'/g)].map((match) => match[1])
  } catch {
    return []
  }
})()

say('预检通过。请在 AGH 工作台完成最后一步（只有该界面能让你核对能力清单）：')
say('')
say(`      插件 id       : ${id}@${version}`)
say(`      期望 integrity: ${integrity}`)
say(`      warnings      : ${warnings}`)
say(`      应注册工具    : ${tools.length} 个`)
say(`                      ${tools.join(', ') || '(未能从源码读出)'}`)
say('')
say('  1. 双击 启动AGH.cmd 启动 AGH')
say('  2. 工作台 → 设置 → 插件管理')
say('  3. 源填 file:./flood-tools')
say('  4. 核对界面显示的 integrity 与上面那行**完全一致**')
say('     —— 不一致就说明这中间文件被改过，不要确认')
say('  5. 确认安装 → 信任 → 启用')
say(`  6. 工具列表里应出现 ${tools.length} 个 flood_ 工具`)
say('')
say('  为什么这一步不能由脚本代做：`agh package trust` 需要的 capabilityHash，')
say('  命令行上没有任何命令会打印它（`package inspect` 只输出人类可读文本，')
say('  且这段文本里没有它）。工作台从 daemon 响应里拿到它再让你确认，')
say('  这正是 trust 要做的核对——由脚本跳过它，等于假装核对过了。')

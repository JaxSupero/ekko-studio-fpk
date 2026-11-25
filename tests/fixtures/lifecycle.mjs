#!/usr/bin/env node
// Run: node tests/fixtures/lifecycle.mjs
import { mkdtempSync, mkdirSync, cpSync, writeFileSync, symlinkSync, existsSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:net'
import assert from 'node:assert/strict'

const pkg = resolve(dirname(fileURLToPath(import.meta.url)), '../../package')
const bin = join(pkg, 'app/bin')
const temp = mkdtempSync(join(tmpdir(), 'ekko-lifecycle-fixture-'))
const app = join(temp, 'target')
const root = join(temp, 'home')
const data = join(root, 'studio-data')
const cmd = join(temp, 'cmd')
const log = join(temp, 'error.log')
const env = { ...process.env, TRIM_APPDEST: app, TRIM_PKGHOME: root, TRIM_TEMP_LOGFILE: log }
let foreign
let probe
const cleanup = []
function call(name, action, expected = 0, extra = {}) {
  const r = spawnSync('bash', [join(cmd, name), ...(action ? [action] : [])], { env: { ...env, ...extra }, encoding: 'utf8', timeout: 45000 })
  assert.equal(r.status, expected, `${name} ${action || ''}: ${r.stderr} ${existsSync(log) ? readFileSync(log, 'utf8') : ''} ${existsSync(join(data, 'server.log')) ? readFileSync(join(data, 'server.log'), 'utf8') : ''}`)
}
function check(name, action) { action(); console.log(`PASS ${name}`) }
try {
  mkdirSync(join(app, 'bin'), { recursive: true })
  mkdirSync(join(app, 'runtime/node/bin'), { recursive: true })
  mkdirSync(join(app, 'studio/dist/server'), { recursive: true })
  mkdirSync(root)
  cpSync(join(pkg, 'cmd'), cmd, { recursive: true })
  const portProbe = createServer()
  await new Promise((ok, no) => { portProbe.once('error', no); portProbe.listen(0, '127.0.0.1', ok) })
  const fixturePort = portProbe.address().port
  await new Promise(ok => portProbe.close(ok))
  const manager = readFileSync(join(bin, 'service.mjs'), 'utf8').replace('const port = 8648', `const port = ${fixturePort}`)
  writeFileSync(join(app, 'bin/service.mjs'), manager)
  symlinkSync(process.execPath, join(app, 'runtime/node/bin/node'))
  writeFileSync(join(app, 'studio/package.json'), '{"type":"module"}')
  writeFileSync(join(app, 'studio/dist/server/index.js'), `import { createServer } from 'node:http';\nconst server = createServer((req, res) => { res.statusCode = req.url === '/health' ? 200 : 404; res.end('ok') });\nserver.listen(Number(process.env.PORT), '127.0.0.1');\nprocess.on('SIGTERM', () => server.close(() => process.exit(0)));\n`)
  check('install prepare + status=3', () => { call('install_callback'); call('main', 'status', 3) })
  const marker = join(data, 'retain.txt')
  writeFileSync(marker, 'persistent')
  check('start + health + status', () => { call('main', 'start'); call('main', 'status') })
  check('idempotent start', () => call('main', 'start'))
  check('stop + status=3', () => { call('main', 'stop'); call('main', 'status', 3) })
  check('dead owner lock safely recovered', () => {
    mkdirSync(join(data, '.service-lock'))
    writeFileSync(join(data, '.service-lock/owner.pid'), '2147483647')
    call('main', 'start'); call('main', 'stop')
  })
  check('live owner lock refused', () => {
    mkdirSync(join(data, '.service-lock'))
    writeFileSync(join(data, '.service-lock/owner.pid'), String(process.pid))
    call('main', 'start', 1)
    rmSync(join(data, '.service-lock'), { recursive: true })
  })
  check('upgrade retains data', () => { call('upgrade_init'); call('upgrade_callback'); assert.equal(readFileSync(marker, 'utf8'), 'persistent') })
  foreign = spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], { stdio: 'ignore' })
  await new Promise((ok, no) => { foreign.once('spawn', ok); foreign.once('error', no) })
  writeFileSync(join(data, 'server.pid'), String(foreign.pid))
  check('foreign PID stop/start refused', () => { call('main', 'stop', 1); call('main', 'start', 1); process.kill(foreign.pid, 0) })
  rmSync(join(data, 'server.pid'))
  probe = createServer()
  await new Promise((ok, no) => { probe.once('error', no); probe.listen(fixturePort, '127.0.0.1', ok) })
  check('port conflict refused without killing owner', () => { call('main', 'start', 1); assert(probe.listening) })
  await new Promise(ok => probe.close(ok))
  probe = null
  check('default uninstall retains data', () => { call('uninstall_init'); call('uninstall_callback'); assert.equal(readFileSync(marker, 'utf8'), 'persistent') })
  check('explicit delete removes guarded app dirs only', () => { call('uninstall_init', null, 0, { wizard_data_action: 'delete' }); assert(existsSync(root)); assert(!existsSync(data)); assert(!existsSync(join(root, 'home'))) })
  console.log('ALL LIFECYCLE FIXTURES PASSED')
} finally {
  try { call('main', 'stop') } catch {}
  if (foreign) foreign.kill('SIGKILL')
  if (probe) probe.close()
  rmSync(temp, { recursive: true, force: true })
}

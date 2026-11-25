#!/usr/bin/env node
import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { existsSync, readFileSync, writeFileSync, renameSync, statSync, unlinkSync, mkdirSync, openSync, closeSync, rmdirSync, realpathSync, lstatSync, rmSync } from 'node:fs'

const appDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const stateRoot = process.env.TRIM_PKGHOME
if (!stateRoot || !stateRoot.startsWith('/')) { fail('TRIM_PKGHOME 未设置或不是绝对路径'); process.exit(1) }
const persistentRoot = resolve(stateRoot)
const home = join(persistentRoot, 'home')
const data = join(persistentRoot, 'studio-data')
const node = join(appDir, 'runtime', 'node', 'bin', 'node')
const server = join(appDir, 'studio', 'dist', 'server', 'index.js')
const pidFile = join(data, 'server.pid')
const logFile = join(data, 'server.log')
const lockDir = join(data, '.service-lock')
const port = 8648
const healthUrl = `http://127.0.0.1:${port}/health`

function fail(message) { console.error(`ekko-studio: ${message}`); process.exitCode = 1 }
function ensureState() {
  if (persistentRoot === '/' || persistentRoot === appDir) throw Error('持久化目录路径无效')
  if (!existsSync(persistentRoot)) throw Error('fnOS 持久目录尚未创建')
  const root = realpathSync(persistentRoot)
  if (root === '/' || root === realpathSync(appDir)) throw Error('持久化目录目标无效')
  for (const dir of [home, data]) {
    if (existsSync(dir) && lstatSync(dir).isSymbolicLink()) throw Error(`拒绝符号链接持久目录: ${dir}`)
    mkdirSync(dir, { recursive: true, mode: 0o700 })
    if (dirname(realpathSync(dir)) !== root) throw Error(`持久目录越界: ${dir}`)
  }
}
function readPid() {
  try {
    const raw = readFileSync(pidFile, 'utf8').trim()
    if (!/^\d+$/.test(raw) || Number(raw) < 2) return null
    return Number(raw)
  } catch { return null }
}
function commandLine(pid) {
  try {
    const args = readFileSync(`/proc/${pid}/cmdline`).toString().split('\0').filter(Boolean)
    return args.length ? args : null
  } catch { return null }
}
function isOurProcess(pid) {
  const args = commandLine(pid)
  if (!args || args.length < 2) return false
  const ownService = resolve(join(appDir, 'bin', 'service.mjs'))
  let index = 1
  while (index < args.length && args[index].startsWith('-')) {
    const flag = args[index++]
    if (['-e', '--eval', '-p', '--print'].includes(flag) || flag.startsWith('--eval=') || flag.startsWith('--print=')) return false
    if (['-r', '--require', '--import', '--loader', '--experimental-loader'].includes(flag)) index++
    if (flag === '--') break
  }
  if (!args[index]) return false
  const script = resolve(args[index])
  return (script === ownService && args[index + 1] === 'run') || script === resolve(server)
}
async function healthy() {
  try { const response = await fetch(healthUrl, { signal: AbortSignal.timeout(1200) }); return response.ok } catch { return false }
}
function rotateLog() {
  try {
    if (statSync(logFile).size < 5 * 1024 * 1024) return
    const old = `${logFile}.1`
    try { unlinkSync(old) } catch {}
    renameSync(logFile, old)
  } catch {}
}
async function assertPortFree() {
  await new Promise((ok, no) => {
    const probe = createServer()
    probe.once('error', no)
    probe.listen(port, '127.0.0.1', () => probe.close(err => err ? no(err) : ok()))
  }).catch(error => { throw Error(`端口 ${port} 已被占用，未终止其他进程 (${error.code || error.message})`) })
}
async function withLock(action) {
  let acquired = false
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      mkdirSync(lockDir, { mode: 0o700 })
      writeFileSync(join(lockDir, 'owner.pid'), `${process.pid}\n`, { mode: 0o600 })
      acquired = true
      break
    } catch (error) {
      if (error.code !== 'EEXIST') throw error
      let owner = null
      try { owner = Number(readFileSync(join(lockDir, 'owner.pid'), 'utf8').trim()) } catch {}
      if (owner && Number.isInteger(owner) && owner > 1) {
        try { process.kill(owner, 0); throw Error(`服务操作锁由进程 ${owner} 持有`) }
        catch (probe) { if (probe.message?.startsWith('服务操作锁')) throw probe; if (probe.code !== 'ESRCH') throw Error(`无法证明锁持有进程 ${owner} 已退出，拒绝回收`) }
        const reclaim = join(data, '.service-reclaim')
        try { mkdirSync(reclaim, { mode: 0o700 }) } catch { throw Error('其他操作正在回收服务锁，请重试') }
        try {
          if (Number(readFileSync(join(lockDir, 'owner.pid'), 'utf8').trim()) !== owner) throw Error('锁所有者已改变')
          unlinkSync(join(lockDir, 'owner.pid')); rmdirSync(lockDir)
        } catch { throw Error('无法安全回收已死进程的服务锁') }
        finally { try { rmdirSync(reclaim) } catch {} }
      } else throw Error('服务锁尚无有效owner记录，拒绝回收；稍后重试')
    }
  }
  if (!acquired) throw Error('无法取得服务操作锁')
  try { return await action() } finally { try { unlinkSync(join(lockDir, 'owner.pid')); rmdirSync(lockDir) } catch {} }
}
async function start() {
  ensureState()
  await withLock(async () => {
    const oldPid = readPid()
    if (oldPid && isOurProcess(oldPid)) {
      if (await healthy()) return
      throw Error(`本应用进程 ${oldPid} 存在但健康检查失败；拒绝启动重复服务`)
    }
    if (oldPid && commandLine(oldPid)) throw Error(`server.pid 指向非本应用进程 ${oldPid}；未触碰该进程或PID文件`)
    if (oldPid) try { unlinkSync(pidFile) } catch {}
    await assertPortFree()
    if (!existsSync(node) || !existsSync(server)) throw Error('未找到随包提供的 Node.js 或官方 Studio server 文件')
    rotateLog()
    const log = openSync(logFile, 'a', 0o600)
    const env = { ...process.env, HOME: home, HERMES_WEB_UI_HOME: data, PORT: String(port), NODE_ENV: 'production' }
    const child = spawn(node, [join(appDir, 'bin', 'service.mjs'), 'run'], { cwd: appDir, detached: true, stdio: ['ignore', log, log], env })
    const spawned = new Promise((ok, no) => { child.once('spawn', ok); child.once('error', no) })
    child.unref()
    closeSync(log)
    await spawned
    if (!child.pid) throw Error('无法创建服务进程')
    await new Promise(resolve => setTimeout(resolve, 80))
    writeFileSync(pidFile, `${child.pid}\n`, { mode: 0o600 })
    const deadline = Date.now() + 30000
    while (Date.now() < deadline) {
      if (!isOurProcess(child.pid)) break
      if (await healthy()) return
      await new Promise(resolve => setTimeout(resolve, 400))
    }
    if (isOurProcess(child.pid)) {
      try { process.kill(child.pid, 'SIGTERM') } catch {}
    }
    throw Error(`服务启动未通过健康检查；日志：${logFile}`)
  })
}
async function stop() {
  ensureState()
  await withLock(async () => {
    const pid = readPid()
    if (!pid) return
    if (!commandLine(pid)) { try { unlinkSync(pidFile) } catch {}; return }
    if (!isOurProcess(pid)) throw Error(`PID ${pid} 不属于本应用；未发送信号且未改动PID文件`)
    try { process.kill(pid, 'SIGTERM') } catch (error) { if (error.code !== 'ESRCH') throw error }
    const deadline = Date.now() + 12000
    while (Date.now() < deadline && commandLine(pid)) await new Promise(resolve => setTimeout(resolve, 250))
    if (commandLine(pid)) {
      if (!isOurProcess(pid)) throw Error(`PID ${pid} 身份发生变化，拒绝发送 KILL`)
      try { process.kill(pid, 'SIGKILL') } catch (error) { if (error.code !== 'ESRCH') throw error }
      const killDeadline = Date.now() + 3000
      while (Date.now() < killDeadline && commandLine(pid)) await new Promise(resolve => setTimeout(resolve, 100))
    }
    if (commandLine(pid)) throw Error(`PID ${pid} 在停止超时后仍存活，停止失败`)
    if (readPid() === pid) try { unlinkSync(pidFile) } catch {}
  })
}
async function purge() {
  if (process.env.wizard_data_action !== 'delete') throw Error('未明确选择删除数据')
  ensureState()
  const pid = readPid()
  if (pid && commandLine(pid)) throw Error('仍存在有效PID，拒绝删除持久数据')
  if (existsSync(lockDir)) throw Error('存在服务操作锁，拒绝删除持久数据')
  // Only these two guarded app-owned directories are removable. Never remove TRIM_PKGHOME itself.
  for (const dir of [home, data]) rmSync(dir, { recursive: true, force: false })
}
async function status() {
  ensureState()
  const pid = readPid()
  if (!pid || !isOurProcess(pid) || !(await healthy())) { process.exitCode = 3; return }
  console.log(`running (${pid})`)
}
async function run() {
  ensureState()
  if (!existsSync(server)) throw Error(`找不到 server: ${server}`)
  process.env.HOME = home
  process.env.HERMES_WEB_UI_HOME = data
  process.env.PORT = String(port)
  process.env.NODE_ENV = 'production'
  process.chdir(join(appDir, 'studio'))
  await import(pathToFileURL(server).href)
}

const command = process.argv[2]
try {
  if (command === 'prepare') ensureState()
  else if (command === 'start') await start()
  else if (command === 'stop') await stop()
  else if (command === 'purge') await purge()
  else if (command === 'status') await status()
  else if (command === 'run') await run()
  else throw Error('用法: service.mjs {start|stop|status|run}')
} catch (error) { fail(error.message) }

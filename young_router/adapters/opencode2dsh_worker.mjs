#!/usr/bin/env node
/**
 * Young Router's process and control surface around dsh-our-free-model. The
 * upstream's public core modules own the OpenCode protocol and wire layer.
 */

import { createServer } from 'node:http'
import { existsSync } from 'node:fs'
import { open, readFile, rename, unlink } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const MAX_BODY_BYTES = 1024 * 1024
const ALLOWED_SETTINGS = new Set([
  'defaultMaxTokens',
  'exposeRegionModels',
  'probeIntervalMinutes',
  'standaloneProbe',
])

function parseArguments(argv) {
  const options = { port: 0, root: '', entry: '', handoff: '', lease: '', settings: {} }
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index]
    const value = argv[index + 1]
    if (flag === '--port') options.port = Number(value ?? 0)
    else if (flag === '--root') options.root = String(value ?? '')
    else if (flag === '--entry') options.entry = String(value ?? '')
    else if (flag === '--handoff') options.handoff = String(value ?? '')
    else if (flag === '--lease') options.lease = String(value ?? '')
    else if (flag === '--setting') {
      const separator = String(value ?? '').indexOf('=')
      const key = String(value ?? '').slice(0, separator)
      if (separator > 0 && ALLOWED_SETTINGS.has(key)) {
        options.settings[key] = String(value).slice(separator + 1)
      }
    } else if (flag === '--help' || flag === '-h') options.help = true
    else throw new Error(`unknown argument: ${flag}`)
    index += 1
  }
  return options
}

function usage() {
  process.stdout.write(
    'usage: opencode2dsh_worker.mjs --port <n> --root <dir> --handoff <fd> --lease <fd> --entry <package.json> [--setting key=value]\n',
  )
}

function booleanSetting(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value).trim().toLowerCase())
}

function integerSetting(value, fallback) {
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback
}

function json(res, status, document) {
  const payload = JSON.stringify(document)
  res.writeHead(status, {
    'content-type': 'application/json',
    'content-length': Buffer.byteLength(payload),
    'cache-control': 'no-store',
  })
  res.end(payload)
}

async function readJson(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) throw Object.assign(new Error('request body too large'), { statusCode: 413 })
    chunks.push(chunk)
  }
  try {
    const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error()
    return parsed
  } catch {
    throw Object.assign(new Error('request body is not a JSON object'), { statusCode: 400 })
  }
}

/** Load the upstream sources from the staged package's root. */
async function loadUpstream(configured) {
  const packageFile = resolve(
    configured || join(dirname(fileURLToPath(import.meta.url)), 'opencode2dsh', 'package.json'),
  )
  const sourceRoot = dirname(packageFile)
  if (!existsSync(packageFile)) throw new Error(`the upstream package.json is missing: ${packageFile}`)
  const { createModelRuntime, createRuntimeStores } = await import(
    pathToFileURL(join(sourceRoot, 'src', 'core', 'runtime.js')).href,
  )
  const { startForwardServer } = await import(
    pathToFileURL(join(sourceRoot, 'src', 'forward.js')).href,
  )
  return { createModelRuntime, createRuntimeStores, startForwardServer, sourceRoot, packageFile }
}

/** Read the public rows from the upstream's loopback gateway. */
async function catalogueDocument(listener, token) {
  const response = await fetch(`http://127.0.0.1:${listener.port}/v1/models`, {
    headers: { authorization: `Bearer ${token}` },
  })
  if (!response.ok) throw new Error(`the upstream model list answered HTTP ${response.status}`)
  const document = await response.json()
  return Array.isArray(document?.data) ? document.data : []
}

async function main(argv) {
  const options = parseArguments(argv)
  if (options.help) {
    usage()
    return
  }
  if (!Number.isInteger(options.port) || options.port < 1 || options.port > 65535) {
    throw new Error('--port must be between 1 and 65535')
  }
  if (!options.root || !resolve(options.root).startsWith('/') && !/^[A-Za-z]:[\\/]/.test(options.root)) {
    throw new Error('--root must be an absolute path')
  }
  const token = process.env.YOUNG_ROUTER_OUR_FREE_MODEL_TOKEN ?? ''
  if (Buffer.byteLength(token) < 16) throw new Error('the worker token is missing')
  const inheritedPath = descriptor => {
    if (!/^\d+$/.test(descriptor)) throw new Error('the worker hand-off descriptor is invalid')
    const candidates = [`/proc/self/fd/${descriptor}`, `/dev/fd/${descriptor}`]
    return candidates.find(existsSync) ?? candidates[0]
  }
  const handoffPath = inheritedPath(options.handoff)
  const leasePath = inheritedPath(options.lease)
  let handoff
  try {
    handoff = await open(handoffPath, 'r+')
  } catch {
    throw new Error('the worker hand-off is missing')
  }
  const handoffStatus = await handoff.stat()
  if (
    process.ppid <= 0
    || !handoffStatus.isFile()
    || !options.lease
    || options.handoff === options.lease
  ) {
    await handoff.close()
    throw new Error('the worker hand-off belongs to another request')
  }
  await handoff.close()
  handoff = undefined
  let lease
  try {
    lease = await open(leasePath, 'r+')
  } catch {
    throw new Error('the free-model worker lock is missing')
  }

  const upstream = await loadUpstream(options.entry)
  const dataDir = resolve(options.root)
  const stores = upstream.createRuntimeStores({ dataDir })
  const defaults = stores.settings.get()
  const settings = {
    defaultMaxTokens: integerSetting(options.settings.defaultMaxTokens, defaults.defaultMaxTokens ?? 32768),
    exposeRegionModels: booleanSetting(
      options.settings.exposeRegionModels ?? String(defaults.exposeRegionModels !== false),
    ),
    probeIntervalMinutes: integerSetting(options.settings.probeIntervalMinutes, defaults.probeIntervalMinutes ?? 15),
    standaloneProbe: booleanSetting(
      options.settings.standaloneProbe ?? String(defaults.standaloneProbe === true),
    ),
  }
  stores.settings.update(settings)
  stores.settings.flush()
  if (stores.settings.writeFailed) throw new Error('the upstream settings could not be persisted')

  const runtime = upstream.createModelRuntime({
    ...stores,
    attributionUserAgent: 'our-free-model',
  })
  let timer
  let refreshInFlight
  let initialRefresh
  let listener
  let closing
  let closed = false
  let control
  let shutdown

  const refreshCatalog = force => {
    if (refreshInFlight) return refreshInFlight
    refreshInFlight = runtime.refreshCatalog({ probe: force })
      .catch(error => console.error(`[our-free-model] catalog refresh failed: ${error?.message ?? error}`))
      .finally(() => {
        refreshInFlight = undefined
        schedule()
      })
    return refreshInFlight
  }
  const schedule = () => {
    clearTimeout(timer)
    if (closed) return
    timer = setTimeout(
      () => { void refreshCatalog(false) },
      Math.max(1, settings.probeIntervalMinutes) * 60_000,
    )
    timer.unref?.()
    initialRefresh ??= refreshCatalog(settings.standaloneProbe)
  }

  listener = await upstream.startForwardServer({
    config: () => ({ host: '127.0.0.1', port: options.port, enabled: true, key: token }),
    complete: runtime.complete,
    modelRows: runtime.publicModelRows,
    health: () => ({ ok: true, service: 'young-router-our-free-model-bridge' }),
    log: message => console.error(`[our-free-model] ${message}`),
  })
  schedule()

  control = createServer(async (req, res) => {
    try {
      const url = new URL(req.url ?? '/', 'http://127.0.0.1')
      if (String(req.headers.authorization ?? '') !== `Bearer ${token}`) {
        json(res, 401, { ok: false, error: 'unauthorized' })
        return
      }
      if (req.method === 'GET' && url.pathname === '/control/status') {
        const packageDocument = JSON.parse(await readFile(upstream.packageFile, 'utf8'))
        json(res, 200, {
          ok: true,
          version: packageDocument.version,
          laneState: 'ready',
          port: listener.port,
        })
        return
      }
      if (req.method === 'GET' && url.pathname === '/control/models') {
        await (initialRefresh ?? Promise.resolve())
        json(res, 200, { ok: true, models: await catalogueDocument(listener, token) })
        return
      }
      if (req.method === 'POST' && url.pathname === '/control/refresh') {
        const body = await readJson(req)
        await refreshCatalog(body.probe === true)
        json(res, 200, { ok: true, models: await catalogueDocument(listener, token) })
        return
      }
      if (req.method === 'POST' && url.pathname === '/control/shutdown') {
        json(res, 200, { ok: true })
        setImmediate(shutdown)
        return
      }
      json(res, 404, { ok: false, error: 'not found' })
    } catch (error) {
      if (!res.headersSent) {
        json(res, Number(error?.statusCode) || 500, {
          ok: false,
          error: String(error?.message ?? error),
        })
      } else res.end()
    }
  })
  await new Promise((resolveListen, reject) => {
    control.once('error', reject)
    control.listen(0, '127.0.0.1', resolveListen)
  })
  const readyPath = join(resolve(options.root), 'online.json')
  const readyStaging = `${readyPath}.${process.pid}.ready`
  const ready = await open(readyStaging, 'w', 0o600)
  await ready.write(JSON.stringify({
    controlPort: control.address().port,
    gatewayPort: listener.port,
  }) + '\n')
  await ready.sync()
  await ready.chmod(0o600)
  await ready.close()
  await rename(readyStaging, readyPath)
  await lease.close()
  await unlink(handoffPath).catch(() => {})
  await unlink(leasePath).catch(() => {})

  const close = async () => {
    if (closing) return closing
    closed = true
    clearTimeout(timer)
    runtime.dispose()
    closing = (async () => {
      await Promise.all([
        new Promise(resolveClose => control.close(resolveClose)),
        listener.close(),
      ])
      stores.dispose()
    })()
    return closing
  }
  shutdown = () => {
    void close().catch(error => {
      console.error(`[our-free-model] shutdown failed: ${error?.message ?? error}`)
      process.exitCode = 1
    })
  }
  process.once('SIGINT', shutdown)
  process.once('SIGTERM', shutdown)
}

main(process.argv.slice(2)).catch(error => {
  console.error(`[our-free-model] ${String(error?.message ?? error)}`)
  process.exitCode = 1
})

#!/usr/bin/env node
/**
 * Node worker that drives the third-party ``dsh-connect-workbuddy`` package.
 *
 * The upstream project (https://github.com/dingminhua/dsh-connect-workbuddy,
 * npm ``dsh-connect-workbuddy``) owns the WorkBuddy product knowledge: the
 * desktop app credential file and its at-rest envelope, the token refresh, the
 * model catalog for both the CN and the international gateway, and the loopback
 * shim that speaks OpenAI Chat Completions to the real upstream.  This worker
 * therefore adds no protocol of its own -- it starts one shim per REGION from
 * the published library, exposes that library's answer on a Core-owned loopback
 * port, and adapts only the two things LiteLLM needs and the plugin never
 * produced (its caller always streams):
 *
 *   * a bearer-guarded control surface the Python Core reads (account status,
 *     live model catalog) and
 *   * a non-streaming chat answer, aggregated from the upstream SSE stream.
 *
 * A region is the library's own unit of separation: the CN gateway and the
 * international one keep separate credentials, separate catalogs and separate
 * credit, and its ``WORKBUDDY_PROVIDERS`` names the route id each registers.
 *
 * The bundle is staged from npm by ``scripts/update_connect_workbuddy.py`` on
 * every artifact build, so this file must not embed product knowledge that the
 * library publishes: no endpoint, header, model list, app path, or credential
 * shape.
 *
 * Protocol (all routes require ``Authorization: Bearer <--token>``):
 *   GET  /control/status                          account state per region
 *   GET  /control/models?provider=<id>[&refresh=1] rich catalog per region
 *   GET  /control/app?provider=<id>            the desktop app that owns the sign-in
 *   POST /control/shutdown                        stop the listener
 *   GET  /<provider-id>/v1/models                 OpenAI model list
 *   POST /<provider-id>/v1/chat/completions       OpenAI chat completions
 *
 * @module young_router/workbuddy_worker
 */

import { createServer } from 'node:http'
import { existsSync, mkdirSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { basename, dirname, join, resolve } from 'node:path'
import { Readable } from 'node:stream'

import { normalizeChatCompletionStreamLine } from './workbuddy_stream.mjs'

const REQUEST_BODY_LIMIT = 64 * 1024 * 1024

function parseArguments(argv) {
  const options = { port: 0, token: '', root: '', entry: '' }
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index]
    const value = argv[index + 1]
    if (flag === '--port') options.port = Number(value ?? 0)
    else if (flag === '--token') options.token = String(value ?? '')
    else if (flag === '--root') options.root = String(value ?? '')
    else if (flag === '--entry') options.entry = String(value ?? '')
    else if (flag === '--help' || flag === '-h') options.help = true
    else throw new Error(`unknown argument: ${flag}`)
    index += 1
  }
  return options
}

function usage() {
  process.stdout.write(
    'usage: workbuddy_worker.mjs --port <n> --token <secret> --root <dir> [--entry <path>]\n',
  )
}

/**
 * The at-rest key provider this build resolves on its own.
 *
 * `dsh-connect-workbuddy` reads the sealed desktop credential through the key
 * the installed app hands out, and it locates that app itself: the region's
 * Electron bundle is probed by `workbuddyAppExecutableCandidates`, confirmed
 * by `isWorkbuddyBundle`, and only then run as plain Node to ask its native
 * binding for the payload.  Nothing here has to inject a key provider, so a
 * build that cannot find the app reports `WORKBUDDY_ENCRYPTED_CREDENTIAL`
 * rather than reading as signed out — see `describeCredential`.
 *
 * The variable is listed here only so this worker can pass one through when
 * the host resolves a non-standard install location.
 */
const APP_EXECUTABLE_ENV = 'WORKBUDDY_APP_EXECUTABLE'

/**
 * The desktop app that owns one region's sign-in, when it is installed.
 *
 * The host needs this only to open the right app for a signed-out card.  The
 * product paths are the library's: `workbuddyAppExecutableCandidates` publishes
 * every bundle the desktop build may be installed as, in its own documented
 * probe order, and `WORKBUDDY_REGIONS` names the two regions — the CN product
 * first, the international one second.  Reading the two lists together is what
 * keeps one region's card from opening the other region's app, which is the
 * defect that shipped when a single-argument lookup answered for both.
 *
 * `WORKBUDDY_APP_EXECUTABLE` pins one binary when the app lives somewhere the
 * probe does not look.  The library reads the same variable when it goes after
 * the app's at-rest key, so a pinned install is the one it opens too.
 *
 * @returns `{executable, bundle}`; `bundle` only on macOS, where the value is
 *   `X.app/Contents/MacOS/<product>` and the bundle sits three levels up.
 */
/**
 * The installed app's own name, read from the path that was found.
 *
 * macOS names an app by its bundle (`WorkBuddy AI.app` -> `WorkBuddy AI`); the
 * other platforms name it by the directory the executable sits in
 * (`...\\WorkBuddy\\WorkBuddy.exe` -> `WorkBuddy`).
 */
function desktopAppName(installed) {
  if (installed === undefined) return undefined
  if (installed.bundle !== undefined) {
    const name = basename(installed.bundle)
    return name.endsWith('.app') ? name.slice(0, -4) : name
  }
  const executable = installed.executable
  if (typeof executable !== 'string' || executable === '') return undefined
  const directory = basename(dirname(executable))
  return directory === '' || directory === '.' ? undefined : directory
}

function installedDesktopApp(library, region) {
  // The library publishes the desktop bundles in a documented probe order and
  // names the two regions in the same order, so the pairing is read from those
  // two lists rather than from an app name written here.  A pinned
  // WORKBUDDY_APP_EXECUTABLE leads the list, which is why the CN slot is taken
  // from it when it is set: the user pointed at one product, and that product
  // is the domestic build the variable documents.
  const pinned = (process.env[APP_EXECUTABLE_ENV] ?? '').trim()
  const candidates = library.workbuddyAppExecutableCandidates()
  const index = library.WORKBUDDY_REGIONS.indexOf(region)
  const executable = pinned !== '' ? pinned : candidates[index]
  if (typeof executable !== 'string' || executable === '') return undefined
  try {
    if (!existsSync(executable)) return undefined
  } catch {
    return undefined
  }
  if (process.platform !== 'darwin') return { executable }
  // `X.app/Contents/MacOS/<product>` puts the bundle three levels up; it is
  // reported only once its Info.plist is really there, so a path that is not a
  // bundle yields no bundle rather than a guessed one.
  const bundle = dirname(dirname(dirname(executable)))
  try {
    if (!existsSync(join(bundle, 'Contents', 'Info.plist'))) return { executable }
  } catch {
    return { executable }
  }
  return { executable, bundle }
}

/** Load the staged third-party library (or a developer-supplied entry). */
async function loadLibrary(entry) {
  const configured = process.env.YOUNG_ROUTER_WORKBUDDY_ENTRY ?? ''
  const candidate = entry || configured || join(dirname(pathToFileURL(import.meta.url).pathname), 'connect-workbuddy', 'index.js')
  const resolved = resolve(candidate)
  try {
    return { library: await import(pathToFileURL(resolved).href), entry: resolved }
  } catch (error) {
    // A development checkout may have the package on its own module path.
    if (process.env.YOUNG_ROUTER_WORKBUDDY_ENTRY || entry) throw error
    return { library: await import('dsh-connect-workbuddy'), entry: 'dsh-connect-workbuddy' }
  }
}

function json(res, status, body) {
  const payload = JSON.stringify(body)
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) })
  res.end(payload)
}

function readBody(req) {
  return new Promise((resolveBody, rejectBody) => {
    const chunks = []
    let size = 0
    req.on('data', chunk => {
      size += chunk.length
      if (size > REQUEST_BODY_LIMIT) {
        rejectBody(new Error('request body too large'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolveBody(Buffer.concat(chunks)))
    req.on('error', rejectBody)
  })
}

/**
 * Merge one upstream SSE stream into a single Chat Completions answer.
 *
 * The library's shim always answers with SSE because the upstream rejects a
 * non-streaming request; LiteLLM still forwards a client's ``stream: false``
 * verbatim, and an SSE body then reads as an invalid JSON answer. Only the
 * fields an OpenAI client needs are merged; unknown delta fields are kept as
 * they arrive so an upstream addition is not silently dropped.
 */
async function aggregateStream(response, model) {
  const decoder = new TextDecoder()
  let buffer = ''
  const message = { role: 'assistant', content: '' }
  const toolCalls = []
  let finishReason = null
  let usage = null
  let id = ''
  let created = 0
  let resolvedModel = model
  let failure = null

  const applyChunk = payload => {
    if (payload.id) id = payload.id
    if (payload.created) created = payload.created
    if (payload.model) resolvedModel = payload.model
    if (payload.usage) usage = payload.usage
    const choice = Array.isArray(payload.choices) ? payload.choices[0] : undefined
    if (!choice) return
    if (choice.finish_reason) finishReason = choice.finish_reason
    const delta = choice.delta ?? choice.message ?? {}
    for (const [key, value] of Object.entries(delta)) {
      if (key === 'role') continue
      if (key === 'tool_calls' && Array.isArray(value)) {
        for (const call of value) {
          const index = Number.isInteger(call.index) ? call.index : toolCalls.length
          const target = toolCalls[index] ?? { id: '', type: 'function', function: { name: '', arguments: '' } }
          if (call.id) target.id = call.id
          if (call.type) target.type = call.type
          if (call.function?.name) target.function.name += call.function.name
          if (call.function?.arguments) target.function.arguments += call.function.arguments
          toolCalls[index] = target
        }
        continue
      }
      if (key === 'function_call' && value && typeof value === 'object') {
        // The upstream repeats an empty legacy ``function_call`` object in
        // every delta; only a value that carries text is a real call.
        if (value.name || value.arguments) {
          message.function_call = message.function_call ?? { name: '', arguments: '' }
          if (value.name) message.function_call.name += value.name
          if (value.arguments) message.function_call.arguments += value.arguments
        }
        continue
      }
      if (typeof value === 'string') message[key] = (message[key] ?? '') + value
      else if (value !== null && value !== undefined && !(key in message)) message[key] = value
    }
  }

  const consumeLine = line => {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith(':')) return
    if (!trimmed.startsWith('data:')) return
    const data = trimmed.slice(5).trim()
    if (data === '[DONE]') return
    try {
      applyChunk(JSON.parse(data))
    } catch {
      failure = failure ?? new Error('workbuddy upstream returned an unreadable stream frame')
    }
  }

  for await (const chunk of response.body) {
    buffer += decoder.decode(chunk, { stream: true })
    let newline = buffer.indexOf('\n')
    while (newline !== -1) {
      consumeLine(buffer.slice(0, newline))
      buffer = buffer.slice(newline + 1)
      newline = buffer.indexOf('\n')
    }
  }
  buffer += decoder.decode()
  if (buffer) consumeLine(buffer)
  if (failure) throw failure
  if (toolCalls.length > 0) message.tool_calls = toolCalls.filter(Boolean)
  return {
    id: id || 'chatcmpl-workbuddy',
    object: 'chat.completion',
    created: created || Math.floor(Date.now() / 1000),
    model: resolvedModel,
    choices: [{ index: 0, message, finish_reason: finishReason ?? 'stop' }],
    usage,
  }
}

/**
 * One WorkBuddy region: the library's store, catalog, and shim.
 *
 * A region is the library's own unit — `WORKBUDDY_REGIONS` names the two
 * upstreams and `WORKBUDDY_PROVIDERS` maps each to the route id it registers.
 * The CN and the international gateway keep separate credentials, separate
 * catalogs and separate credit, so each gets its own store, catalog and shim
 * exactly as the library intends; nothing here merges them.
 */
class RegionRuntime {
  constructor(region, library, root) {
    this.id = library.WORKBUDDY_PROVIDERS[region]
    this.region = region
    this.library = library
    this.client = new library.WorkBuddyUpstreamClient()
    // The store scopes itself to `region`: only credentials whose login domain
    // maps to it are discovered, selected or refreshed, so the two regions'
    // accounts never see each other.  It resolves the at-rest key through the
    // installed desktop app on its own -- there is no key provider to inject.
    this.store = new library.WorkBuddyCredentialStore({
      region,
      refresh: credential => this.client.refreshToken(credential),
      ownPath: join(root, `${this.id}.auth.json`),
    })
    // Seeded from this region's own fallback: a region must never serve the
    // other's roster before its first upstream read.
    this.catalog = new library.WorkBuddyCatalog(region)
    this.shim = library.createWorkBuddyShim({
      store: this.store,
      client: this.client,
      catalog: this.catalog,
      logger: {
        warn: (...args) => console.error('[warn]', this.id, ...args),
        error: (...args) => console.error('[error]', this.id, ...args),
      },
    })
    this.source = 'builtin'
    this.fetchedAtMs = 0
    this.catalogError = ''
    this.credits = null
  }

  /**
   * One region's account state.
   *
   * A credential that exists but cannot be decrypted is not a signed-out
   * account: the library distinguishes them so the UI can say "point the
   * plugin at the app" instead of "sign in again", which cannot help.  The
   * reason travels verbatim to the caller that renders it.
   */
  async status() {
    let status
    try {
      status = await this.store.status()
    } catch (error) {
      status = { state: 'signed-out', reason: String(error?.message ?? error) }
    }
    return {
      ...status,
      provider: this.id,
      region: this.region,
      displayName: this.library.WORKBUDDY_PROVIDER_DISPLAY_NAMES[this.region],
      catalogSource: this.source,
      catalogFetchedAtMs: this.fetchedAtMs || undefined,
      catalogError: this.catalogError || undefined,
      modelCount: this.catalog.current().length,
      credits: this.credits,
    }
  }

  /** Resolve this region's credential, then refresh its catalog. */
  async refresh({ withCredits = true } = {}) {
    const credential = await this.store.resolve()
    this.catalog.setRegionUsable(true)
    try {
      const models = await this.client.fetchModels(credential)
      this.catalog.set(models)
      this.source = 'live'
      this.fetchedAtMs = Date.now()
      this.catalogError = ''
    } catch (error) {
      this.source = 'builtin'
      this.fetchedAtMs = 0
      this.catalogError = String(error?.message ?? error)
    }
    if (withCredits) {
      try {
        this.credits = await this.client.fetchCredits(credential)
      } catch {
        this.credits = null
      }
    }
    return this.catalog.current()
  }

  /** Fetch the catalog when a credential exists; hide the list when it does not. */
  async warm() {
    try {
      await this.refresh({ withCredits: false })
      return { state: 'ready' }
    } catch (error) {
      // "This region has no sign-in" is a state the catalog states on its own,
      // so it is recorded rather than guessed from a blanked list.
      this.catalog.setRegionUsable(false)
      this.source = 'none'
      this.catalogError = String(error?.message ?? error)
      return { state: 'signed-out', reason: this.catalogError }
    }
  }

  /** Forward one OpenAI request to the library's shim. */
  async forward(req, res, url, providerPath) {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    const body = Buffer.concat(chunks)
    const target = `${this.shim.baseUrl()}${url.pathname.slice(providerPath.length) || '/'}${url.search}`
    const upstream = await fetch(target, {
      method: req.method,
      headers: { 'content-type': 'application/json', authorization: `Bearer ${this.shim.token()}` },
      body: req.method === 'POST' && body.length > 0 ? body : undefined,
    })
    if (req.method !== 'POST' || upstream.headers.get('content-type')?.includes('application/json')) {
      const payload = Buffer.from(await upstream.arrayBuffer())
      res.writeHead(upstream.status, {
        'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
        'Content-Length': payload.length,
      })
      res.end(payload)
      return
    }
    if (!upstream.ok) {
      const payload = Buffer.from(await upstream.arrayBuffer())
      res.writeHead(upstream.status, {
        'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
        'Content-Length': payload.length,
      })
      res.end(payload)
      return
    }
    const wantsStream = streamRequested(body)
    if (wantsStream) {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
        'X-Accel-Buffering': 'no',
      })
      // The shim states every delta field it knows, including empty ones, and a
      // reasoning frame therefore carries ``content: ""``.  A client reads that
      // as "the answer started" and closes the thinking block it just opened,
      // so one fragment became one collapsed 深度思考 row; the shaper drops the
      // empty fields and leaves every other frame byte-for-byte.
      const decoder = new TextDecoder()
      let buffer = ''
      for await (const chunk of Readable.fromWeb(upstream.body)) {
        buffer += decoder.decode(chunk, { stream: true })
        let newline = buffer.indexOf('\n')
        while (newline !== -1) {
          res.write(normalizeChatCompletionStreamLine(buffer.slice(0, newline + 1)))
          buffer = buffer.slice(newline + 1)
          newline = buffer.indexOf('\n')
        }
      }
      buffer += decoder.decode()
      if (buffer) res.write(normalizeChatCompletionStreamLine(buffer))
      res.end()
      return
    }
    let model = ''
    try {
      const parsed = JSON.parse(body.toString('utf8') || '{}')
      model = typeof parsed.model === 'string' ? parsed.model : ''
    } catch {
      model = ''
    }
    const aggregated = await aggregateStream(upstream, model)
    json(res, 200, aggregated)
  }
}

function streamRequested(body) {
  try {
    const parsed = JSON.parse(body.toString('utf8') || '{}')
    return parsed.stream === true
  } catch {
    return false
  }
}

async function main() {
  const options = parseArguments(process.argv.slice(2))
  if (options.help) {
    usage()
    return 0
  }
  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535) {
    throw new Error('workbuddy worker requires a valid --port')
  }
  if (!options.token) throw new Error('workbuddy worker requires an inbound --token')
  const root = options.root || process.env.YOUNG_ROUTER_WORKBUDDY_ROOT || ''
  if (!root) throw new Error('workbuddy worker requires a private --root directory')
  mkdirSync(root, { recursive: true, mode: 0o700 })

  const { library, entry } = await loadLibrary(options.entry)
  // A pinned binary is how a non-standard install is reported to the library,
  // which reads the same variable when it goes looking for the app's key.
  const pinnedApp = process.env[APP_EXECUTABLE_ENV] ?? ''
  if (pinnedApp !== '' && !existsSync(pinnedApp)) {
    process.stderr.write(`[warn] workbuddy: ${APP_EXECUTABLE_ENV} names no file: ${pinnedApp}\n`)
  }
  const runtimes = new Map()
  for (const region of library.WORKBUDDY_REGIONS) {
    const runtime = new RegionRuntime(region, library, root)
    runtimes.set(runtime.id, runtime)
  }
  for (const runtime of runtimes.values()) await runtime.shim.ready

  const bearerOk = req => {
    const header = req.headers.authorization
    if (typeof header !== 'string') return false
    const match = /^Bearer\s+(.+)$/i.exec(header.trim())
    return match !== null && match[1] === options.token
  }

  const server = createServer((req, res) => {
    void handle(req, res).catch(error => {
      if (!res.headersSent) json(res, 500, { error: { message: String(error?.message ?? error), type: 'worker_error' } })
      else res.end()
    })
  })

  async function handle(req, res) {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1')
    if (!bearerOk(req)) {
      json(res, 401, { error: { message: 'missing or invalid Authorization bearer', type: 'unauthorized' } })
      return
    }
    if (req.method === 'GET' && url.pathname === '/control/status') {
      const refresh = ['1', 'true', 'yes'].includes((url.searchParams.get('refresh') ?? '').toLowerCase())
      const status = {}
      for (const [id, runtime] of runtimes) {
        if (refresh) {
          // An explicit refresh also re-reads the account's remaining credit:
          // that document is the expensive part, so it rides only this call.
          try {
            await runtime.refresh({ withCredits: true })
          } catch {
            // A signed-out region is reported by status() below.
          }
        }
        status[id] = await runtime.status()
      }
      json(res, 200, { ready: true, entry, providers: status })
      return
    }
    if (req.method === 'GET' && url.pathname === '/control/models') {
      const id = url.searchParams.get('provider') ?? ''
      const runtime = runtimes.get(id)
      if (!runtime) {
        json(res, 404, { error: { message: `unknown provider: ${id}`, type: 'not_found' } })
        return
      }
      const refresh = ['1', 'true', 'yes'].includes((url.searchParams.get('refresh') ?? '').toLowerCase())
      try {
        // Every read refreshes: the roster churns daily and the caller (the
        // app's model picker) is the only consumer. Credits are the expensive
        // extra document, so they ride only an explicit refresh.
        await runtime.refresh({ withCredits: refresh })
      } catch (error) {
        const status = await runtime.status()
        json(res, 200, { provider: id, available: false, detail: String(error?.message ?? error), status, models: [] })
        return
      }
      const status = await runtime.status()
      json(res, 200, {
        provider: id,
        available: true,
        source: runtime.source,
        fetchedAtMs: runtime.fetchedAtMs || undefined,
        detail: runtime.catalogError || undefined,
        status,
        models: runtime.catalog.current().map(model => ({
          id: model.id,
          name: model.name,
          contextWindow: model.contextWindow,
          defaultContextWindow: model.defaultContextWindow,
          supportedContextWindows: model.supportedContextWindows,
          maxTokens: model.maxTokens,
          supportsImages: model.supportsImages === true,
          reasoning: model.reasoning,
          billing: model.billing,
        })),
      })
      return
    }
    if (req.method === 'GET' && url.pathname === '/control/app') {
      // A signed-out card offers to open the app that owns the sign-in, so the
      // host needs to know which one that is.  The library both names the
      // product and finds its installed binary; this file carries neither.
      const id = url.searchParams.get('provider') ?? ''
      const runtime = runtimes.get(id)
      if (runtime === undefined) {
        json(res, 404, { error: { message: `unknown provider: ${id}`, type: 'not_found' } })
        return
      }
      let installed
      try {
        installed = installedDesktopApp(library, runtime.region)
      } catch {
        // No probe answer: the region's display name below still lets the host
        // offer the button rather than not offering it at all.
        installed = undefined
      }
      // The name reported is the app that will actually open, not the region's
      // label.  The host falls back to `open -a <name>` when it has no bundle,
      // and that call only succeeds against the app's real name -- reporting
      // "WorkBuddy Global" would open nothing, because the installed app is
      // named "WorkBuddy AI".  Derived from the path the library found, so this
      // file still embeds no product knowledge; the region label stays in
      // `/control/status` where it names the provider, not an application.
      const appName = desktopAppName(installed)
        ?? library.WORKBUDDY_PROVIDER_DISPLAY_NAMES[runtime.region]
      json(res, 200, {
        provider: id,
        appName,
        ...(installed?.bundle === undefined ? {} : { bundle: installed.bundle }),
      })
      return
    }
    if (req.method === 'POST' && url.pathname === '/control/shutdown') {
      json(res, 200, { ok: true })
      beginShutdown()
      return
    }
    const [first, ...rest] = url.pathname.split('/').filter(segment => segment.length > 0)
    const runtime = runtimes.get(first ?? '')
    if (runtime !== undefined && rest[0] === 'v1') {
      const prefix = `/${first}`
      await runtime.forward(req, res, url, prefix)
      return
    }
    json(res, 404, { error: { message: `no such route: ${req.method} ${url.pathname}`, type: 'not_found' } })
  }

  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(options.port, '127.0.0.1', () => {
      const address = server.address()
      // Announce the bound port before the first catalog fetch: the Core only
      // needs the address to build the provider base URLs, and a slow or
      // unreachable upstream must not delay the gateway's own startup.
      process.stdout.write(`${JSON.stringify({ ready: true, port: address.port, entry, providers: [...runtimes.keys()] })}\n`)
      resolveListen()
    })
  })
  for (const runtime of runtimes.values()) {
    // A signed-out region must not answer with a stale roster, and a warm
    // fetch keeps `/v1/models` honest as soon as the gateway asks for it.
    void runtime.warm().catch(error => console.error('[warm]', runtime.id, String(error?.message ?? error)))
  }

  // Shutdown stops accepting work but lets a turn already in flight finish.
  //
  // This worker is the managed proxy's upstream: a Responses turn streams
  // through it for as long as the model keeps producing.  Tearing those
  // sockets down the moment SIGTERM arrives is what turned every app
  // restart into a cut turn the user had to resend, because the proxy above
  // is still draining and its upstream had already been destroyed.  Close
  // the listener, leave the open responses alone, and exit once they end.
  //
  // The shims are closed only after the drain: they own the real upstream
  // sockets, so closing them first would cut exactly the streams this is
  // waiting for.
  const DRAIN_MAX_SECONDS = 600
  let shuttingDown = false

  function beginShutdown() {
    if (shuttingDown) return
    shuttingDown = true
    server.close(() => finishShutdown())
    // Drop keep-alive sockets that hold nothing: they are not mid-response,
    // and they would otherwise keep the close callback pending forever.
    // In-flight responses keep their sockets.
    server.closeIdleConnections?.()
    // A response that never ends must not pin the previous app forever.
    const limit = setTimeout(() => finishShutdown(), DRAIN_MAX_SECONDS * 1000)
    limit.unref?.()
  }

  let finished = false
  function finishShutdown() {
    if (finished) return
    finished = true
    for (const runtime of runtimes.values()) void runtime.shim.close()
    process.exit(0)
  }

  const shutdown = () => beginShutdown()
  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
  await new Promise(() => {})
  return 0
}

main().catch(error => {
  process.stderr.write(`workbuddy worker failed: ${String(error?.stack ?? error)}\n`)
  process.exit(1)
})

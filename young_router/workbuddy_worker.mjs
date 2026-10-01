#!/usr/bin/env node
/**
 * Node worker that drives the third-party ``dsh-workbuddy-connect`` package.
 *
 * The upstream project (https://github.com/corrinehu/dsh-workbuddy-connect,
 * npm ``dsh-workbuddy-connect``) already owns the WorkBuddy product knowledge:
 * the desktop app credential file and its at-rest envelope, the token refresh,
 * the model catalog for both the CN and the international endpoint, and the
 * loopback shim that speaks OpenAI Chat Completions to the real upstream. This
 * worker therefore adds no protocol of its own -- it starts one shim per
 * variant from the published library, exposes that library's answer on a
 * Core-owned loopback port, and adapts only the two things LiteLLM needs and
 * the plugin never produced (its caller always streams):
 *
 *   * a bearer-guarded control surface the Python Core reads (account status,
 *     live model catalog) and
 *   * a non-streaming chat answer, aggregated from the upstream SSE stream.
 *
 * The bundle is staged from npm by ``scripts/update_workbuddy_connect.py`` on
 * every artifact build, so this file must not embed product knowledge that the
 * library publishes: no endpoint, header, model list, or credential shape.
 *
 * Protocol (all routes require ``Authorization: Bearer <--token>``):
 *   GET  /control/status                          account state per variant
 *   GET  /control/models?provider=<id>[&refresh=1] rich catalog per variant
 *   GET  /control/app?provider=<id>            the desktop app that owns the sign-in
 *   POST /control/shutdown                        stop the listener
 *   GET  /<variant-id>/v1/models                  OpenAI model list
 *   POST /<variant-id>/v1/chat/completions        OpenAI chat completions
 *
 * @module young_router/workbuddy_worker
 */

import { createServer } from 'node:http'
import { mkdirSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { dirname, join, resolve } from 'node:path'
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

/** Load the staged third-party library (or a developer-supplied entry). */
async function loadLibrary(entry) {
  const configured = process.env.YOUNG_ROUTER_WORKBUDDY_ENTRY ?? ''
  const candidate = entry || configured || join(dirname(pathToFileURL(import.meta.url).pathname), 'workbuddy-connect', 'index.js')
  const resolved = resolve(candidate)
  try {
    return { library: await import(pathToFileURL(resolved).href), entry: resolved }
  } catch (error) {
    // A development checkout may have the package on its own module path.
    if (process.env.YOUNG_ROUTER_WORKBUDDY_ENTRY || entry) throw error
    return { library: await import('dsh-workbuddy-connect'), entry: 'dsh-workbuddy-connect' }
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

/** One WorkBuddy variant: the library's store, catalog, and shim. */
class VariantRuntime {
  constructor(id, variant, library, root) {
    this.id = id
    this.variant = variant
    this.library = library
    this.client = new library.WorkBuddyUpstreamClient()
    this.store = new library.WorkBuddyCredentialStore({
      variant,
      refresh: credential => this.client.refreshToken(credential),
      ownPath: join(root, `${id}.auth.json`),
    })
    this.catalog = new library.WorkBuddyCatalog(
      variant.region === 'global' ? library.FALLBACK_WORKBUDDY_AI_MODELS : library.FALLBACK_WORKBUDDY_MODELS,
    )
    this.catalogStore = new library.WorkBuddyCatalogStore(join(root, `${id}.catalog.json`))
    this.shim = library.createWorkBuddyShim({
      store: this.store,
      client: this.client,
      catalog: this.catalog,
      logger: { warn: (...args) => console.error('[warn]', this.id, ...args), error: (...args) => console.error('[error]', this.id, ...args) },
    })
    this.source = 'builtin'
    this.fetchedAtMs = 0
    this.catalogError = ''
    this.credits = null
  }

  async status() {
    let status
    try {
      status = await this.store.status()
    } catch (error) {
      status = { state: 'signed-out', reason: String(error) }
    }
    return {
      ...status,
      provider: this.id,
      displayName: this.variant.displayName,
      appName: this.variant.appName,
      catalogSource: this.source,
      catalogFetchedAtMs: this.fetchedAtMs || undefined,
      catalogError: this.catalogError || undefined,
      modelCount: this.catalog.current().length,
      credits: this.credits,
    }
  }

  /** Resolve the credential, then refresh the variant's catalog. */
  async refresh({ withCredits = true } = {}) {
    const credential = await this.store.resolve()
    this.catalog.setVisible(true)
    try {
      const models = await this.client.fetchModels(credential)
      this.catalog.set(models)
      this.source = 'live'
      this.fetchedAtMs = Date.now()
      this.catalogError = ''
      this.catalogStore.set(accountOf(credential), {
        source: 'live',
        fetchedAtMs: this.fetchedAtMs,
        models: [...models],
      })
    } catch (error) {
      const saved = this.catalogStore.get(accountOf(credential))
      if (saved && Array.isArray(saved.models) && saved.models.length > 0) {
        this.catalog.set(saved.models)
        this.source = 'saved'
        this.fetchedAtMs = saved.fetchedAtMs ?? 0
      } else {
        this.source = 'builtin'
        this.fetchedAtMs = 0
      }
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
      this.catalog.setVisible(false)
      this.source = 'none'
      this.catalogError = String(error?.message ?? error)
      return { state: 'signed-out', reason: this.catalogError }
    }
  }

  /** Forward one OpenAI request to the library's shim. */
  async forward(req, res, url, variantPath) {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    const body = Buffer.concat(chunks)
    const target = `${this.shim.baseUrl()}${url.pathname.slice(variantPath.length) || '/'}${url.search}`
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

function accountOf(credential) {
  return `${credential.uid ?? ''}:${credential.enterpriseId ?? ''}`
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
  const runtimes = new Map()
  for (const variant of library.WORKBUDDY_VARIANTS) {
    const runtime = new VariantRuntime(variant.id, variant, library, root)
    runtimes.set(variant.id, runtime)
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
            // A signed-out variant is reported by status() below.
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
      // The account signs in through the desktop app, so the host needs to
      // know which app that is. Both the name and the resolved bundle come
      // from the upstream library: this file embeds no product knowledge.
      const id = url.searchParams.get('provider') ?? ''
      const variant = library.WORKBUDDY_VARIANTS.find(candidate => candidate.id === id)
      if (!variant) {
        json(res, 404, { error: { message: `unknown provider: ${id}`, type: 'not_found' } })
        return
      }
      let bundle
      let version
      try {
        const installed = await library.installedAppVersion(variant)
        bundle = installed?.bundle
        version = installed?.version
      } catch {
        // An older library build does not report an app path; the name below
        // still lets the host ask the platform to open the app.
        bundle = undefined
      }
      json(res, 200, {
        provider: id,
        appName: variant.appName,
        ...(bundle === undefined ? {} : { bundle }),
        ...(version === undefined ? {} : { version }),
      })
      return
    }
    if (req.method === 'POST' && url.pathname === '/control/shutdown') {
      json(res, 200, { ok: true })
      server.close(() => process.exit(0))
      server.closeAllConnections()
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
    // A signed-out variant must not answer with a stale roster, and a warm
    // fetch keeps `/v1/models` honest as soon as the gateway asks for it.
    void runtime.warm().catch(error => console.error('[warm]', runtime.id, String(error?.message ?? error)))
  }

  const shutdown = () => {
    for (const runtime of runtimes.values()) void runtime.shim.close()
    server.close(() => process.exit(0))
    server.closeAllConnections()
  }
  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
  await new Promise(() => {})
  return 0
}

main().catch(error => {
  process.stderr.write(`workbuddy worker failed: ${String(error?.stack ?? error)}\n`)
  process.exit(1)
})

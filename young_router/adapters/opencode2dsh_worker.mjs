#!/usr/bin/env node
/**
 * Node worker that drives the third-party ``@opencode2dsh/dsh-plugin`` package.
 *
 * The upstream project (https://github.com/FishBottle7/opencode2dsh) already
 * owns the OpenCode Zen anonymous lane: the free-model catalog and its
 * fallback chain, the CLI-identical request disguise (session/request/project
 * ids derived from the conversation), the free-lane body shape, and the
 * ``pi-ai`` wire layer that streams from Zen.  This worker therefore adds no
 * protocol of its own -- it captures the plugin's ``LlmAdapter`` through the
 * package's own ``apply`` entry point, exposes the models that adapter
 * advertises on a Core-owned loopback port, and translates between the two
 * shapes the two sides speak:
 *
 *   * an OpenAI Chat Completions request (what LiteLLM sends) is turned into
 *     the adapter's harness ``GenerateOptions``, and
 *   * the adapter's harness chunk stream is turned back into OpenAI Chat
 *     Completions frames (streaming SSE and a non-streaming aggregate).
 *
 * The bundle is staged from npm by ``scripts/update_opencode2dsh.py`` on every
 * artifact build, so this file must not embed product knowledge the library
 * publishes: no endpoint, header, model list, or free-lane rule lives here.
 *
 * Protocol (all routes require ``Authorization: Bearer <--token>``):
 *   GET  /control/status                          catalog provenance and roster size
 *   GET  /control/models[?refresh=1]              rich catalog with capabilities
 *   POST /control/shutdown                        stop the listener
 *   GET  /v1/models                               OpenAI model list
 *   POST /v1/chat/completions                     OpenAI chat completions
 *
 * @module young_router/opencode2dsh_worker
 */

import { createServer } from 'node:http'
import { createHash } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { pathToFileURL } from 'node:url'
import { join, resolve } from 'node:path'

const REQUEST_BODY_LIMIT = 64 * 1024 * 1024
const PROVIDER_ID = 'opencode2dsh'

function parseArguments(argv) {
  const options = { port: 0, token: '', root: '', entry: '', refreshSeconds: 0 }
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index]
    const value = argv[index + 1]
    if (flag === '--port') options.port = Number(value ?? 0)
    else if (flag === '--token') options.token = String(value ?? '')
    else if (flag === '--root') options.root = String(value ?? '')
    else if (flag === '--entry') options.entry = String(value ?? '')
    else if (flag === '--refresh-seconds') options.refreshSeconds = Number(value ?? 0)
    else if (flag === '--help' || flag === '-h') options.help = true
    else throw new Error(`unknown argument: ${flag}`)
    index += 1
  }
  return options
}

function usage() {
  process.stdout.write(
    'usage: opencode2dsh_worker.mjs --port <n> --token <secret> --root <dir> '
      + '[--entry <path>] [--refresh-seconds <n>]\n',
  )
}

/**
 * Load the staged plugin (or a developer-supplied entry) and capture the
 * ``LlmAdapter`` its own ``apply`` registers.
 *
 * The package publishes no adapter class: the only supported way to reach the
 * adapter is the cordis plugin entry point, which registers it with the host's
 * ``llm`` service.  This worker therefore supplies a minimal host context that
 * captures the registration instead of talking to a real host, and hands back
 * the adapter object the plugin built.  A release that stops registering an
 * adapter (or registers one missing the methods below) fails the worker start
 * rather than serving an endpoint that can never answer.
 */
async function loadAdapter(entry) {
  if (!entry) throw new Error('opencode2dsh worker requires --entry')
  const resolved = resolve(entry)
  const plugin = await import(pathToFileURL(resolved).href)
  if (typeof plugin.apply !== 'function') {
    throw new Error(`the staged ${PROVIDER_ID} plugin publishes no apply() entry point`)
  }
  let captured
  const context = {
    logger: {
      info: (...args) => console.error('[info]', ...args),
      warn: (...args) => console.error('[warn]', ...args),
      error: (...args) => console.error('[error]', ...args),
    },
    llm: {
      registerAdapter(providers, adapter) {
        captured = { providers, adapter }
        return { replace() {} }
      },
    },
  }
  plugin.apply(context, {})
  if (!captured || typeof captured.adapter !== 'object' || captured.adapter === null) {
    throw new Error(`the staged ${PROVIDER_ID} plugin registered no LLM adapter`)
  }
  const adapter = captured.adapter
  for (const method of ['listModels', 'resolveModel', 'prepareCall']) {
    if (typeof adapter[method] !== 'function') {
      throw new Error(`the staged ${PROVIDER_ID} adapter publishes no ${method}()`)
    }
  }
  return { adapter, entry: resolved }
}

/** The provider id the adapter advertises; the plugin's own default. */
function adapterProvider(adapter) {
  try {
    const info = adapter.providerInfo?.(PROVIDER_ID)
    if (info && typeof info.name === 'string' && info.name.length > 0) return info.name
  } catch {
    // A providerInfo that throws is answered by the constant below.
  }
  return PROVIDER_ID
}

/**
 * The adapter's own catalog view, for the control surface.
 *
 * ``listModels`` is the roster the host's picker shows, and ``resolveModel``
 * carries the per-model capability the app's model pane reports.  Both are read
 * here so the control document is the adapter's answer, never a second copy of
 * the free-lane rules.
 */
async function catalogDocument(adapter, provider) {
  const listed = await adapter.listModels(provider)
  const models = []
  const seen = new Set()
  for (const entry of Array.isArray(listed) ? listed : []) {
    const id = typeof entry?.id === 'string' ? entry.id : ''
    if (!id || seen.has(id)) continue
    seen.add(id)
    let resolved = {}
    try {
      resolved = adapter.resolveModel(provider, id) ?? {}
    } catch {
      resolved = {}
    }
    const inputModalities = Array.isArray(entry?.inputModalities)
      ? entry.inputModalities
      : Array.isArray(resolved.inputModalities)
        ? resolved.inputModalities
        : ['text']
    const contextWindow = resolved?.context?.contextWindow
    const maxTokens = resolved?.defaultMaxTokens
    models.push({
      id,
      name: typeof entry?.name === 'string' && entry.name.length > 0 ? entry.name : id,
      ...(Number.isSafeInteger(contextWindow) && contextWindow > 0 ? { contextWindow } : {}),
      ...(Number.isSafeInteger(maxTokens) && maxTokens > 0 ? { maxTokens } : {}),
      supportsImages: inputModalities.includes('image'),
      ...(Array.isArray(resolved?.reasoning?.efforts) && resolved.reasoning.efforts.length > 0
        ? { reasoning: { efforts: resolved.reasoning.efforts.map((effort) => effort.id) } }
        : {}),
    })
  }
  return models
}

/** One string content field out of an OpenAI message's content. */
function textOfContent(content) {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    const parts = []
    for (const part of content) {
      if (part && typeof part === 'object' && part.type === 'text' && typeof part.text === 'string') {
        parts.push(part.text)
      }
    }
    return parts.join('')
  }
  return ''
}

/**
 * The adapter's attachment store root.
 *
 * The staged adapter reads an image block from the DSH attachment store rather
 * than from a ``data:`` URL, so an incoming OpenAI image part has to be written
 * there first.  ``DSH_HOME`` is the store's own root (the adapter mirrors
 * ``dsh-attachment-local``) and the worker sets it to its private root at
 * startup, so nothing outside this worker's directory is written.
 */
function attachmentRoot() {
  const configured = process.env.DSH_HOME?.trim()
  return configured ? resolve(configured) : join(homedir(), '.dsh')
}

/**
 * Materialize one OpenAI image part into the adapter's attachment store and
 * return the harness ``image`` block that points at it.
 *
 * The store is content-addressed: an object lives at
 * ``attachments/v1/objects/<first two hex>/<sha256>`` and the harness block
 * carries ``attachmentId: "sha256:<digest>"``.  Only a base64 ``data:`` URL can
 * be written this way; a remote ``http(s)`` URL is left to the adapter's own
 * text fallback, because fetching it here would add a request the lane never
 * asked for.
 */
function imageBlockFor(part) {
  const image = part?.image_url
  const url = typeof image === 'string' ? image : image?.url
  if (typeof url !== 'string') return undefined
  const match = /^data:([^;,]*);base64,(.*)$/s.exec(url)
  if (!match) return undefined
  const mediaType = match[1] && match[1].length > 0 ? match[1] : 'image/png'
  let bytes
  try {
    bytes = Buffer.from(match[2], 'base64')
  } catch {
    return undefined
  }
  if (bytes.length === 0) return undefined
  const sha = createHash('sha256').update(bytes).digest('hex')
  const directory = join(attachmentRoot(), 'attachments', 'v1', 'objects', sha.slice(0, 2))
  const target = join(directory, sha)
  try {
    mkdirSync(directory, { recursive: true })
    writeFileSync(target, bytes)
  } catch (error) {
    console.error('[image]', String(error?.message ?? error))
    return undefined
  }
  return { type: 'image', attachment: { attachmentId: `sha256:${sha}`, mediaType } }
}

/**
 * Convert one OpenAI Chat Completions request into the adapter's harness
 * ``GenerateOptions``.
 *
 * The two shapes differ only in content enumeration, so the translation is
 * structural: every message keeps its role, text and image parts keep their
 * order, and an assistant turn's ``tool_calls`` become harness ``tool-call``
 * blocks.  A ``tool`` message carries its call id at the message level, which
 * is also the shape the adapter's ``toPiContext`` folds into a tool result.
 */
function toGenerateOptions(body, provider, model) {
  const messages = []
  for (const message of Array.isArray(body.messages) ? body.messages : []) {
    if (!message || typeof message !== 'object') continue
    const role = typeof message.role === 'string' ? message.role : 'user'
    const blocks = []
    if (Array.isArray(message.content)) {
      for (const part of message.content) {
        if (!part || typeof part !== 'object') continue
        if (part.type === 'text' && typeof part.text === 'string' && part.text.length > 0) {
          blocks.push({ type: 'text', text: part.text })
        } else if (part.type === 'image_url' && part.image_url) {
          const block = imageBlockFor(part)
          if (block) blocks.push(block)
        }
      }
    } else if (typeof message.content === 'string' && message.content.length > 0) {
      blocks.push({ type: 'text', text: message.content })
    }
    if (Array.isArray(message.tool_calls)) {
      for (const call of message.tool_calls) {
        if (!call || typeof call !== 'object') continue
        const fn = call.function ?? {}
        blocks.push({
          type: 'tool-call',
          id: typeof call.id === 'string' ? call.id : '',
          name: typeof fn.name === 'string' ? fn.name : '',
          arguments: typeof fn.arguments === 'string' ? fn.arguments : JSON.stringify(fn.arguments ?? {}),
        })
      }
    }
    const entry = { role, content: blocks }
    if (role === 'tool' && typeof message.tool_call_id === 'string') {
      entry.toolCallId = message.tool_call_id
      entry.source = { kind: 'tool', callId: message.tool_call_id }
      if (typeof message.name === 'string') entry.toolName = message.name
    }
    messages.push(entry)
  }
  const options = { provider, model, messages }
  if (typeof body.system === 'string' && body.system.length > 0) options.system = body.system
  if (Array.isArray(body.tools)) {
    options.tools = body.tools
      .filter((tool) => tool && typeof tool === 'object' && tool.type === 'function' && tool.function)
      .map((tool) => ({
        name: typeof tool.function.name === 'string' ? tool.function.name : '',
        description: typeof tool.function.description === 'string' ? tool.function.description : '',
        parameters: tool.function.parameters ?? { type: 'object', properties: {} },
      }))
      .filter((tool) => tool.name.length > 0)
  }
  if (typeof body.max_tokens === 'number') options.maxTokens = body.max_tokens
  if (typeof body.max_completion_tokens === 'number') options.maxTokens = body.max_completion_tokens
  if (typeof body.temperature === 'number') options.temperature = body.temperature
  if (typeof body.reasoning_effort === 'string') options.reasoningEffort = body.reasoning_effort
  else if (typeof body.reasoning === 'string') options.reasoningEffort = body.reasoning
  return options
}

/**
 * Re-encode the adapter's harness chunk stream as OpenAI Chat Completions
 * frames.
 *
 * The harness stream is a block-oriented projection: ``block-start`` /
 * ``text-delta`` / ``block-end`` for text and reasoning, ``tool-call-delta``
 * then ``block-end`` for tool calls, and a terminal ``usage`` + ``finish``.
 * The OpenAI shape is index-oriented deltas, so each block is emitted as one
 * delta carrying its own ``index``, with the tool-call arguments streamed
 * through ``tool_calls`` fragments exactly as an OpenAI client expects.  A
 * terminal failure surfaces as an error frame rather than an empty answer, so
 * a lane failure is never mistaken for a model that said nothing.
 */
async function* toOpenAiFrames(chunks, model) {
  const created = Math.floor(Date.now() / 1000)
  const id = `chatcmpl-opencode2dsh-${created}-${Math.random().toString(16).slice(2, 10)}`
  const blocks = new Map()
  let nextToolIndex = 0
  let finishReason = null
  let usage = null
  const emit = (delta, extra = {}) => ({
    id,
    object: 'chat.completion.chunk',
    created,
    model,
    choices: [{ index: 0, delta, ...extra }],
  })
  yield emit({ role: 'assistant', content: '' })
  for await (const chunk of chunks) {
    if (!chunk || typeof chunk !== 'object') continue
    if (chunk.type === 'usage') {
      usage = {
        prompt_tokens: chunk.usage?.inputTokens ?? 0,
        completion_tokens: chunk.usage?.outputTokens ?? 0,
        total_tokens: (chunk.usage?.inputTokens ?? 0) + (chunk.usage?.outputTokens ?? 0),
      }
      continue
    }
    if (chunk.type === 'finish') {
      const kind = chunk.reason?.kind
      if (kind === 'tool-calls') finishReason = 'tool_calls'
      else if (kind === 'max-tokens') finishReason = 'length'
      else if (kind === 'error' || kind === 'aborted') {
        const message = chunk.reason?.failure?.message ?? 'the OpenCode Zen lane failed'
        throw new Error(message)
      } else finishReason = 'stop'
      continue
    }
    if (chunk.type === 'block-start') {
      const key = typeof chunk.index === 'number' ? chunk.index : blocks.size
      blocks.set(key, { kind: chunk.blockType, toolIndex: null, streamed: '' })
      continue
    }
    if (chunk.type === 'text-delta') {
      if (chunk.text.length > 0) yield emit({ content: chunk.text })
      continue
    }
    if (chunk.type === 'reasoning-delta') {
      if (chunk.text.length > 0) yield emit({ reasoning_content: chunk.text })
      continue
    }
    if (chunk.type === 'tool-call-delta') {
      const key = typeof chunk.index === 'number' ? chunk.index : blocks.size
      const state = blocks.get(key) ?? { kind: 'tool-call', toolIndex: null, streamed: '' }
      if (state.toolIndex === null) state.toolIndex = nextToolIndex++
      // The block-end that follows carries the whole call, so the pieces this
      // frame streams are remembered to emit only what the snapshot still owes.
      state.id = typeof chunk.id === 'string' && chunk.id.length > 0 ? chunk.id : state.id
      state.name = typeof chunk.name === 'string' && chunk.name.length > 0 ? chunk.name : state.name
      state.streamed += typeof chunk.argumentsDelta === 'string' ? chunk.argumentsDelta : ''
      blocks.set(key, state)
      const call = { index: state.toolIndex, type: 'function', function: { name: '', arguments: '' } }
      if (state.id !== undefined && state.idEmitted !== true) {
        call.id = state.id
        state.idEmitted = true
        blocks.set(key, state)
      }
      if (state.name !== undefined && state.nameEmitted !== true) {
        call.function.name = state.name
        state.nameEmitted = true
        blocks.set(key, state)
      }
      if (typeof chunk.argumentsDelta === 'string' && chunk.argumentsDelta.length > 0) {
        call.function.arguments = chunk.argumentsDelta
      }
      yield emit({ tool_calls: [call] })
      continue
    }
    if (chunk.type === 'block-end') {
      const block = chunk.block ?? {}
      if (block.type === 'tool-call') {
        const key = typeof chunk.index === 'number' ? chunk.index : blocks.size
        const state = blocks.get(key) ?? { kind: 'tool-call', toolIndex: null, streamed: '' }
        if (state.toolIndex === null) state.toolIndex = nextToolIndex++
        const full = typeof block.arguments === 'string' ? block.arguments : JSON.stringify(block.arguments ?? {})
        // What the deltas already streamed is a prefix of the snapshot; only
        // the remainder is owed, so a client that assembles by concatenation
        // ends with the upstream's own call and never a doubled one.
        const remaining = full.startsWith(state.streamed) ? full.slice(state.streamed.length) : full
        state.streamed = full
        const call = { index: state.toolIndex, type: 'function', function: { name: '', arguments: remaining } }
        // The id and name are stated once, in the first fragment a client sees;
        // repeating the full value here would be a second copy for any client
        // that concatenates rather than replaces.
        if (state.idEmitted !== true && (typeof block.id === 'string' || typeof block.name === 'string')) {
          if (typeof block.id === 'string' && block.id.length > 0) call.id = block.id
          if (typeof block.name === 'string' && block.name.length > 0) call.function.name = block.name
          state.idEmitted = true
          state.nameEmitted = true
        }
        blocks.set(key, state)
        // Nothing owed at all (a snapshot every delta already delivered) emits
        // no frame: an empty tool_calls fragment would only be noise.
        if (call.id !== undefined || call.function.name.length > 0 || call.function.arguments.length > 0) {
          yield emit({ tool_calls: [call] })
        }
      }
      continue
    }
  }
  const final = { choices: [{ index: 0, delta: {}, finish_reason: finishReason ?? 'stop' }] }
  if (usage) final.usage = usage
  yield { id, object: 'chat.completion.chunk', created, model, ...final }
}

/** Merge the OpenAI frames of one adapter stream into one non-streaming answer. */
async function aggregate(frames) {
  let id = ''
  let created = 0
  let resolvedModel = ''
  let content = ''
  let reasoning = ''
  let finishReason = null
  let usage = null
  const toolCalls = new Map()
  for await (const frame of frames) {
    if (frame.id) id = frame.id
    if (frame.created) created = frame.created
    if (frame.model) resolvedModel = frame.model
    if (frame.usage) usage = frame.usage
    const choice = Array.isArray(frame.choices) ? frame.choices[0] : undefined
    if (!choice) continue
    if (choice.finish_reason) finishReason = choice.finish_reason
    const delta = choice.delta ?? {}
    if (typeof delta.content === 'string') content += delta.content
    if (typeof delta.reasoning_content === 'string') reasoning += delta.reasoning_content
    if (Array.isArray(delta.tool_calls)) {
      for (const call of delta.tool_calls) {
        const index = Number.isInteger(call.index) ? call.index : toolCalls.size
        const target = toolCalls.get(index) ?? { id: '', type: 'function', function: { name: '', arguments: '' } }
        if (call.id) target.id = call.id
        if (call.type) target.type = call.type
        if (call.function?.name) target.function.name += call.function.name
        if (call.function?.arguments) target.function.arguments += call.function.arguments
        toolCalls.set(index, target)
      }
    }
  }
  const message = { role: 'assistant', content }
  if (reasoning.length > 0) message.reasoning_content = reasoning
  if (toolCalls.size > 0) {
    message.content = content.length > 0 ? content : null
    message.tool_calls = [...toolCalls.entries()].sort((a, b) => a[0] - b[0]).map(([, call]) => call)
  }
  const answer = {
    id: id || `chatcmpl-opencode2dsh-${Math.floor(Date.now() / 1000)}`,
    object: 'chat.completion',
    created: created || Math.floor(Date.now() / 1000),
    model: resolvedModel,
    choices: [{ index: 0, message, finish_reason: finishReason ?? 'stop' }],
  }
  if (usage) answer.usage = usage
  return answer
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
    req.on('data', (chunk) => {
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

function errorStatus(message) {
  if (/FREE_TIER_RESTRICTED|FreeTierError|free tier can only be used/i.test(message)) return 403
  if (/REGION_BLOCKED|not available in your country/i.test(message)) return 403
  if (/RATE_LIMIT|429|rate.?limit/i.test(message)) return 429
  if (/AUTH|401|403|API key/i.test(message)) return 401
  if (/INVALID_REQUEST|400/i.test(message)) return 400
  return 502
}

async function main() {
  const options = parseArguments(process.argv.slice(2))
  if (options.help) {
    usage()
    return 0
  }
  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535) {
    throw new Error('opencode2dsh worker requires a valid --port')
  }
  if (!options.token) throw new Error('opencode2dsh worker requires an inbound --token')
  const root = options.root || process.env.YOUNG_ROUTER_OPENCODE2DSH_ROOT || ''
  if (!root) throw new Error('opencode2dsh worker requires a private --root directory')
  mkdirSync(root, { recursive: true, mode: 0o700 })
  // The staged adapter resolves an image block against the DSH attachment
  // store, so the store is pointed at this worker's own private root: an
  // incoming image part is written there, never into a user's real ``~/.dsh``.
  if (!process.env.DSH_HOME || process.env.DSH_HOME.trim() === '') {
    process.env.DSH_HOME = root
  }
  if (process.env.YOUNG_ROUTER_OPENCODE2DSH_CACHE) {
    process.env.YOUNG_ROUTER_OPENCODE2DSH_CACHE = resolve(process.env.YOUNG_ROUTER_OPENCODE2DSH_CACHE)
  }

  const { adapter, entry } = await loadAdapter(options.entry)
  const provider = adapterProvider(adapter)

  const bearerOk = (req) => {
    const header = req.headers.authorization
    if (typeof header !== 'string') return false
    const match = /^Bearer\s+(.+)$/i.exec(header.trim())
    return match !== null && match[1] === options.token
  }

  const server = createServer((req, res) => {
    void handle(req, res).catch((error) => {
      if (!res.headersSent) {
        json(res, errorStatus(String(error?.message ?? error)), {
          error: { message: String(error?.message ?? error), type: 'worker_error' },
        })
      } else {
        res.end()
      }
    })
  })

  async function handle(req, res) {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1')
    if (!bearerOk(req)) {
      json(res, 401, { error: { message: 'missing or invalid Authorization bearer', type: 'unauthorized' } })
      return
    }
    if (req.method === 'GET' && url.pathname === '/control/status') {
      let models = []
      let detail = ''
      try {
        models = await catalogDocument(adapter, provider)
      } catch (error) {
        detail = String(error?.message ?? error)
      }
      json(res, 200, {
        ready: true,
        entry,
        provider,
        available: models.length > 0,
        detail,
        modelCount: models.length,
      })
      return
    }
    if (req.method === 'GET' && url.pathname === '/control/models') {
      let models = []
      let detail = ''
      try {
        models = await catalogDocument(adapter, provider)
      } catch (error) {
        detail = String(error?.message ?? error)
      }
      json(res, 200, {
        provider,
        available: models.length > 0,
        detail,
        source: 'adapter',
        models,
      })
      return
    }
    if (req.method === 'POST' && url.pathname === '/control/shutdown') {
      json(res, 200, { ok: true })
      beginShutdown()
      return
    }
    if (req.method === 'GET' && url.pathname === '/v1/models') {
      const models = await catalogDocument(adapter, provider)
      json(res, 200, {
        object: 'list',
        data: models.map((model) => ({
          id: model.id,
          object: 'model',
          owned_by: provider,
          name: model.name,
        })),
      })
      return
    }
    if (req.method === 'POST' && url.pathname === '/v1/chat/completions') {
      const raw = await readBody(req)
      let body
      try {
        body = JSON.parse(raw.toString('utf8') || '{}')
      } catch {
        json(res, 400, { error: { message: 'invalid JSON body', type: 'invalid_request_error' } })
        return
      }
      const model = typeof body.model === 'string' ? body.model.trim() : ''
      if (!model) {
        json(res, 400, { error: { message: 'a model is required', type: 'invalid_request_error' } })
        return
      }
      const call = await adapter.prepareCall(provider, model)
      const generateOptions = toGenerateOptions(body, provider, model)
      const frames = toOpenAiFrames(call.stream(generateOptions), model)
      if (body.stream === true) {
        res.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
          'X-Accel-Buffering': 'no',
        })
        try {
          for await (const frame of frames) {
            res.write(`data: ${JSON.stringify(frame)}\n\n`)
          }
          res.write('data: [DONE]\n\n')
        } catch (error) {
          const message = String(error?.message ?? error)
          res.write(`data: ${JSON.stringify({ error: { message, type: 'upstream_error' } })}\n\n`)
        }
        res.end()
        return
      }
      let answer
      try {
        answer = await aggregate(frames)
      } catch (error) {
        const message = String(error?.message ?? error)
        json(res, errorStatus(message), { error: { message, type: 'upstream_error' } })
        return
      }
      json(res, 200, answer)
      return
    }
    json(res, 404, { error: { message: `no such route: ${req.method} ${url.pathname}`, type: 'not_found' } })
  }

  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(options.port, '127.0.0.1', () => {
      const address = server.address()
      // Announce the bound port before the catalog warms up: the Core only
      // needs the address to build the provider base URL, and a slow or
      // unreachable upstream must not delay the gateway's own startup.
      process.stdout.write(
        `${JSON.stringify({ ready: true, port: address.port, entry, provider })}\n`,
      )
      resolveListen()
    })
  })

  // Warm the catalog in the background so the first model read is not the one
  // paying for the live Zen directory fetch.
  void catalogDocument(adapter, provider).catch((error) =>
    console.error('[warm]', String(error?.message ?? error)),
  )

  // Shutdown stops accepting work but lets a turn already in flight finish:
  // this worker is the managed proxy's upstream, and tearing those sockets
  // down the moment SIGTERM arrives would cut a turn the proxy is still
  // draining.  Close the listener, leave the open responses alone, and exit
  // once they end.
  const DRAIN_MAX_SECONDS = 600
  let shuttingDown = false

  function beginShutdown() {
    if (shuttingDown) return
    shuttingDown = true
    server.close(() => finishShutdown())
    server.closeIdleConnections?.()
    const limit = setTimeout(() => finishShutdown(), DRAIN_MAX_SECONDS * 1000)
    limit.unref?.()
  }

  let finished = false
  function finishShutdown() {
    if (finished) return
    finished = true
    process.exit(0)
  }

  const shutdown = () => beginShutdown()
  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
  await new Promise(() => {})
  return 0
}

// Run only when executed as the worker, not when imported by a test that drives
// the pure encoders below.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    process.stderr.write(`opencode2dsh worker failed: ${String(error?.stack ?? error)}\n`)
    process.exit(1)
  })
}

export { toOpenAiFrames, aggregate, toGenerateOptions, errorStatus, imageBlockFor }

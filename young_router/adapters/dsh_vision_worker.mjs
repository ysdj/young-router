// The dsh-vision-router bridge: the staged upstream chain, not a copy of it.
//
// Young Router's vision fallback runs inside the Python LiteLLM process, which
// cannot import a Node module.  It therefore calls this worker over stdio, and
// this worker answers with the provider chain that upstream itself computes
// (`scripts/update_dsh_vision_router.py` stages the package the imports here
// name).  Nothing in this file re-derives a model, a default, or an order: a
// release that changes the free OVH chain changes what Core routes to, without
// a change in this repository.
//
// The package's `exports` map does not publish `lib/core-primitives.js`, so the
// entry is resolved by absolute path from the staged bundle.  The staging step
// verifies that this file still exists and still exports these functions, so an
// upstream move fails the build instead of the router at request time.
//
// Protocol: one JSON request per line on stdin, one JSON response per line on
// stdout.  A request is `{"config": {...}}`; a response is
// `{"providers": [...], "source": "upstream"|"unavailable", "error": "..."}`.

import process from 'node:process'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const packageRoot = path.join(here, 'dsh-vision-router')
const entry = path.join(packageRoot, 'lib', 'core-primitives.js')

// The functions the Core's provider chain is built from.  Upstream renamed or
// dropped one of these and the router would silently route nothing, so the
// staging step asserts the same list against the staged file.
const REQUIRED_EXPORTS = [
  'httpProvidersOf',
  'localOllamaProvidersOf',
  'localLmStudioProvidersOf',
]

let upstream

async function loadUpstream() {
  if (upstream !== undefined) return upstream
  try {
    const module = await import(pathToFileURL(entry).href)
    const missing = REQUIRED_EXPORTS.filter((name) => typeof module[name] !== 'function')
    if (missing.length > 0) {
      upstream = { error: `staged dsh-vision-router no longer exports ${missing.join(', ')}` }
      return upstream
    }
    upstream = module
  } catch (error) {
    upstream = { error: String((error && error.message) || error) }
  }
  return upstream
}

// Upstream provider rows are camelCase and carry their own maxTokens.  Core
// owns one snake_case row shape for the whole proxy, so translate here rather
// than teaching every caller both spellings.
function toCoreProvider(provider) {
  if (!provider || typeof provider !== 'object') return null
  const model = typeof provider.model === 'string' ? provider.model : ''
  if (model === '') return null
  const row = {
    name: typeof provider.name === 'string' ? provider.name : 'configured',
    base_url: typeof provider.baseURL === 'string' ? provider.baseURL : '',
    model,
    api_key: '',
    api_key_env: typeof provider.apiKeyEnv === 'string' ? provider.apiKeyEnv : '',
    format: 'openai',
    max_tokens: Number.isInteger(provider.maxTokens) ? provider.maxTokens : null,
    temperature: null,
    top_p: null,
    reasoning_effort:
      typeof provider.reasoningEffort === 'string' && provider.reasoningEffort !== ''
        ? provider.reasoningEffort
        : null,
  }
  if (typeof provider.system === 'string' && provider.system !== '') {
    row.system = provider.system
  }
  return row
}

function chainFor(module, config) {
  const document = config && typeof config === 'object' ? config : {}
  const providers = []
  const seen = new Set()

  const push = (provider) => {
    const row = toCoreProvider(provider)
    if (row === null) return
    // The built-in free chain is several models on one shared endpoint, so the
    // endpoint alone is not an identity: a model that differs is a different
    // provider even when its base URL does not.
    const key = `${row.base_url}|${row.model}|${row.name}`
    if (seen.has(key)) return
    seen.add(key)
    providers.push(row)
  }
  const pushAll = (rows) => {
    for (const provider of asArray(rows)) push(provider)
  }

  const backend = typeof document.backend === 'string' ? document.backend : 'auto'
  // `off` is a real answer, not a missing one: the router disables the whole
  // fallback with it, and returning the free chain here would route images the
  // operator turned vision routing off for.
  if (backend === 'off') return { providers, source: 'upstream' }
  if (backend !== 'api') {
    pushAll(module.localOllamaProvidersOf(document))
    pushAll(module.localLmStudioProvidersOf(document))
  }
  if (backend === 'local') return { providers, source: 'upstream' }

  if (document.freeFallback !== false) {
    pushAll(module.httpProvidersOf(document, true))
  }
  for (const entry of [...asArray(document.httpProviders), ...asArray(document.providers)]) {
    if (!entry || typeof entry !== 'object') continue
    if (typeof entry.baseURL === 'string' || typeof entry.baseUrl === 'string') {
      push({
        name: entry.name,
        baseURL: entry.baseURL ?? entry.baseUrl,
        model: entry.model,
        apiKeyEnv: entry.apiKeyEnv,
        maxTokens: entry.maxTokens,
        reasoningEffort: entry.reasoningEffort,
        system: entry.system,
      })
      continue
    }
    // `{provider, model, fallbacks}` pairs are the compact profile shape.
    for (const name of asArray(entry.fallbacks)) {
      push({ name: entry.provider, baseURL: '', model: name, maxTokens: entry.maxTokens })
    }
    push({ name: entry.provider, baseURL: '', model: entry.model, maxTokens: entry.maxTokens })
  }
  return { providers, source: 'upstream' }
}

function asArray(value) {
  return Array.isArray(value) ? value : []
}

function respond(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`)
}

async function handle(request) {
  const module = await loadUpstream()
  if (module.error) {
    return { providers: [], source: 'unavailable', error: module.error }
  }
  try {
    return chainFor(module, request && request.config)
  } catch (error) {
    return {
      providers: [],
      source: 'unavailable',
      error: String((error && error.message) || error),
    }
  }
}

let buffer = ''
process.stdin.setEncoding('utf8')
process.stdin.on('data', (chunk) => {
  buffer += chunk
  let newline = buffer.indexOf('\n')
  while (newline >= 0) {
    const line = buffer.slice(0, newline).trim()
    buffer = buffer.slice(newline + 1)
    newline = buffer.indexOf('\n')
    if (line === '') continue
    let request = null
    try {
      request = JSON.parse(line)
    } catch (error) {
      respond({ providers: [], source: 'unavailable', error: `bad request: ${error.message}` })
      continue
    }
    handle(request).then(respond, (error) =>
      respond({ providers: [], source: 'unavailable', error: String(error && error.message) })
    )
  }
})
process.stdin.on('end', () => process.exit(0))

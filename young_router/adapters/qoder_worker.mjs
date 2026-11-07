#!/usr/bin/env node
/**
 * Node worker that drives the third-party ``@dmh2002/dsh-connect-qoder``
 * package.
 *
 * The upstream project (https://github.com/dingminhua/dsh-connect-qoder, npm
 * ``@dmh2002/dsh-connect-qoder``) owns the Qoder product knowledge: the cosy
 * signing protocol, the PAT -> jobToken exchange, the live model catalog, the
 * usage read, and the Chat Completions shim it builds for DeepSeek Harness.
 * Its published entry point is a Harness *plugin* (``apply(ctx, config)``), so
 * this worker imports the package's own bundled upstream client instead of
 * re-implementing the protocol -- the staged release check in the artifact
 * build refuses a package whose surface moved.
 *
 * What this file adds is only what a LiteLLM route needs and the plugin does
 * not publish:
 *
 *   * a bearer-guarded control surface the Python Core reads (account state,
 *     live model catalog, PAT sign-in / sign-out), and
 *   * an OpenAI Chat Completions face per region, because Qoder's gateway only
 *     answers an SSE stream and LiteLLM forwards a client's ``stream: false``
 *     verbatim.
 *
 * The two regions are two upstream products behind one protocol: the CN
 * gateway (``gateway.qoder.com.cn``) and the international one
 * (``api1.qoder.sh``) keep separate PATs, separate catalogs and separate
 * credit.  A signed-out region is a normal state, not an error: the worker
 * reports it and that provider stays disabled until a PAT is stored.
 *
 * Protocol (all routes require ``Authorization: Bearer <--token>``):
 *   GET  /control/status                          account state per region
 *   GET  /control/models?provider=<id>[&refresh=1] rich catalog per region
 *   POST /control/login                            store and verify a PAT
 *   POST /control/logout                           forget the stored PAT
 *   POST /control/shutdown                         stop the listener
 *   GET  /<provider-id>/v1/models                 OpenAI model list
 *   POST /<provider-id>/v1/chat/completions       OpenAI chat completions
 *
 * @module young_router/qoder_worker
 */

import { createServer } from 'node:http'
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { basename, dirname, join, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'

import {
  OPENAPI_BY_REGION,
  desktopApp,
  readDesktopSession,
  refreshDesktopSession,
} from './qoder_app_session.mjs'

const REQUEST_BODY_LIMIT = 64 * 1024 * 1024

/**
 * How often a running worker re-reads its parent's identity.
 *
 * The Core is this worker's parent, and nothing signals a child when its
 * parent is killed: the child is reparented and keeps its listener.  Every
 * managed worker in this app watches ``process.ppid`` for that reason (one
 * install path once left seventeen WorkBuddy workers holding ports for hours).
 * ``process.ppid`` is read fresh on every tick because it answers from the live
 * parent record -- a single read at startup reports the original pid long
 * after reparenting.
 */
const ORPHAN_POLL_MS = 2000

/** The two regions, their provider ids, and their display names. */
const REGIONS = Object.freeze([
  { region: 'cn', provider: 'qoder', displayName: 'Qoder' },
  { region: 'global', provider: 'qoder-global', displayName: 'Qoder Global' },
])

/**
 * Environment variables the upstream package documents for a PAT.
 *
 * They are the fallback after the file this worker owns: the file is what the
 * app's own sign-in writes, while the variables are how a host states a PAT
 * without a window (a gateway with a projected config).  The package reads the
 * same names, so a pinned install behaves like the library's own CLI.
 */
const PAT_ENV = Object.freeze({
  cn: 'QODERCN_PERSONAL_ACCESS_TOKEN',
  global: 'QODER_PERSONAL_ACCESS_TOKEN',
})

/**
 * How long one read of the desktop app's sign-in answers later reads.
 *
 * The desktop app rotates its access token when it wakes, and the token lasts
 * a month, so re-reading the Keychain on every request would buy nothing.  A
 * read that turns out to be stale (the app signed out, or the token was
 * revoked) drops the cache and re-reads once — see ``_desktopSession``.
 */
const DESKTOP_SESSION_TTL_MS = 120_000

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
    'usage: qoder_worker.mjs --port <n> --token <secret> --root <dir> [--entry <lib/index.js>]\n',
  )
}

/**
 * Load the upstream client out of the staged package.
 *
 * The published package exports a Harness plugin, not its internals: tsdown
 * bundles the cosy protocol, the upstream client, the account store and the
 * catalog into one shared chunk, and the entry only names the plugin surface.
 * That chunk is still a real ES module inside the published tarball, and it is
 * the only copy of the protocol this app is allowed to run -- re-implementing
 * cosy here would be a second truth that drifts on the next upstream release.
 *
 * The chunk's own file name carries a build hash, so it is found by what it
 * exports rather than by its name: the entry's relative imports are tried
 * first (they are exactly the modules the published entry itself loaded), then
 * the rest of ``lib`` for the case a future bundle stops re-exporting one of
 * them from the entry.  The Chat Completions face is the one with both
 * ``chatStream`` and ``fetchModels`` -- a shape only ``QoderUpstreamClient``
 * has.
 */
async function loadUpstream(entry) {
  const entryPath = resolve(entry || join(dirname(process.argv[1] ?? '.'), 'connect-qoder', 'lib', 'index.js'))
  const libDir = dirname(entryPath)
  if (!existsSync(entryPath)) throw new Error(`staged Qoder entry is missing: ${entryPath}`)

  const candidates = []
  try {
    const source = readFileSync(entryPath, 'utf8').toString()
    for (const match of source.matchAll(/from\s+"(\.\/[^"]+\.js)"/g)) {
      const candidate = join(libDir, basename(match[1]))
      if (!candidates.includes(candidate)) candidates.push(candidate)
    }
  } catch {
    // An unreadable entry is answered by the directory scan below.
  }
  try {
    for (const file of readdirSync(libDir).sort()) {
      if (!file.endsWith('.js') || file === 'client.js' || file === 'bin.js' || file === basename(entryPath)) continue
      const candidate = join(libDir, file)
      if (!candidates.includes(candidate)) candidates.push(candidate)
    }
  } catch {
    // Missing lib directory is reported by the first candidate's import.
  }

  for (const candidate of candidates) {
    let module
    try {
      module = await import(pathToFileURL(candidate).href)
    } catch {
      continue
    }
    for (const value of Object.values(module)) {
      if (
        typeof value === 'function'
        && value.prototype
        && typeof value.prototype.chatStream === 'function'
        && typeof value.prototype.fetchModels === 'function'
      ) {
        return { clientClass: value, module, entry: entryPath }
      }
    }
  }
  throw new Error(
    `staged Qoder package no longer exports the upstream client this worker drives (${entryPath})`,
  )
}

function json(res, status, body, retryAfterMs) {
  const payload = JSON.stringify(body)
  const headers = { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }
  if (typeof retryAfterMs === 'number' && Number.isFinite(retryAfterMs) && retryAfterMs > 0) {
    headers['Retry-After'] = String(Math.ceil(retryAfterMs / 1000))
  }
  res.writeHead(status, headers)
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
 * One upstream failure, in the vocabulary LiteLLM and its clients classify by.
 *
 * A pre-first-byte failure is a non-200 JSON answer: LiteLLM reads
 * ``error.message`` / ``error.code`` from a failed response, and a 200 stream
 * carrying an ``error`` field is not classified at all.  The status and code
 * are the plugin's own mapping (``src/shim.ts``), so the same upstream failure
 * reads the same way whichever surface a client used.
 */
function errorStatus(error) {
  const kind = typeof error?.kind === 'string' ? error.kind : ''
  const mapping = {
    network: [502, 'connection_error'],
    auth: [401, 'invalid_api_key'],
    'rate-limited': [429, 'rate_limit_exceeded'],
    'invalid-request': [400, 'invalid_request_error'],
    'content-policy': [400, 'content_policy_rejected'],
    'quota-exhausted': [429, 'quota_exceeded'],
    transient: [502, 'upstream_error'],
    unknown: [500, 'upstream_error'],
  }
  const [status, code] = mapping[kind] ?? [500, 'upstream_error']
  return { status, code, kind: kind || 'unknown', message: String(error?.message ?? error ?? 'upstream error') }
}

/** The upstream total, read the way the plugin's own credits view reads it. */
function creditsOf(usage) {
  if (!usage || typeof usage !== 'object') return { total: 0, packages: [] }
  const packages = []
  let total = 0
  const add = (packageName, remaining, size) => {
    if (typeof size !== 'number' || size <= 0) return
    total += remaining
    packages.push({ packageName, remain: remaining, size })
  }
  const userQuota = usage.userQuota
  if (userQuota && typeof userQuota.total === 'number') {
    add('base', Math.max(userQuota.total - (userQuota.used ?? 0), 0), userQuota.total)
  }
  const addOnQuota = usage.addOnQuota
  if (addOnQuota && typeof addOnQuota.total === 'number') {
    add('addon', Math.max(addOnQuota.total - (addOnQuota.used ?? 0), 0), addOnQuota.total)
  }
  const org = usage.orgResourcePackage
  if (org && typeof org.cap === 'number') {
    add('org', Math.max(org.cap - (org.used ?? 0), 0), org.cap)
  }
  return {
    total,
    packages,
    ...(typeof usage.expiresAt === 'number' ? { planExpiresAtMs: usage.expiresAt } : {}),
  }
}

/** The default context window one catalog row declares, or the plugin default. */
function contextWindowOf(model) {
  const tiers = contextTiersOf(model)
  const declared = model.context_config
  if (declared && typeof declared === 'object') {
    const entries = Object.values(declared).filter(entry => entry && typeof entry.token_count === 'number')
    const preferred = entries.find(entry => entry.is_default === true)
    if (preferred) return preferred.token_count
    if (entries.length > 0) return Math.min(...entries.map(entry => entry.token_count))
  }
  if (typeof model.max_input_tokens === 'number' && model.max_input_tokens > 0) return model.max_input_tokens
  return 200_000
}

/** The context tiers one catalog row accepts, ascending; absent when none. */
function contextTiersOf(model) {
  const declared = model.context_config
  if (!declared || typeof declared !== 'object') return undefined
  const tiers = Object.values(declared)
    .map(entry => (entry && typeof entry.token_count === 'number' ? entry.token_count : undefined))
    .filter(value => typeof value === 'number' && value > 0)
    .sort((left, right) => left - right)
  return tiers.length > 1 ? [...new Set(tiers)] : undefined
}

/** The thinking effort names one catalog row declares, in strength order. */
function thinkingEffortsOf(model) {
  const enabled = model.thinking_config?.enabled
  const efforts = enabled && typeof enabled === 'object' ? enabled.efforts : undefined
  if (!efforts || typeof efforts !== 'object') return { }
  const order = ['low', 'medium', 'high', 'xhigh', 'max']
  const names = Object.keys(efforts).sort((left, right) => {
    const leftIndex = order.indexOf(left)
    const rightIndex = order.indexOf(right)
    if (leftIndex === -1 && rightIndex === -1) return left.localeCompare(right)
    if (leftIndex === -1) return 1
    if (rightIndex === -1) return -1
    return leftIndex - rightIndex
  })
  const declaredDefault = Object.entries(efforts).find(([, value]) => value?.is_default === true)?.[0]
  return {
    ...(names.length > 0 ? { thinkingEfforts: names } : {}),
    ...(typeof declaredDefault === 'string' && declaredDefault !== '' ? { defaultThinkingEffort: declaredDefault } : {}),
  }
}

/** One upstream catalog row, in the shape the Core's catalog reader consumes. */
function normalizeModel(model) {
  const tiers = contextTiersOf(model)
  const thinking = thinkingEffortsOf(model)
  return {
    // Two names, because they answer two questions: `id` is the key the
    // upstream is called by on the wire (`qmodel_38max`), and `publicName` is
    // what a person picks (`Qwen3.8-Max`) — the name Qoder's own model menu
    // shows.  A pane that displayed the key listed a slug nobody recognises as
    // a model, and a client told to send it could not be matched against
    // Qoder's documentation.
    id: model.key,
    name: model.display_name,
    publicName: model.display_name,
    contextWindow: contextWindowOf(model),
    ...(typeof model.max_output_tokens === 'number' ? { maxTokens: model.max_output_tokens } : {}),
    supportsImages: model.is_vl === true,
    // The upstream's ``is_reasoning`` and its ``thinking_config`` contradict
    // each other in the live catalog (the plugin documents the same rows), and
    // a declared effort set proves the model accepts a strength.  Only the
    // false-to-true direction is taken: a model that states no efforts keeps
    // whatever the flag says.
    reasoning: model.is_reasoning === true || thinking.thinkingEfforts !== undefined,
    // ``price_factor`` is the upstream's own credit rate.  The Core follows it
    // the way a relay group's multiplier is followed, so it travels under the
    // one name every managed catalog uses for a rate.  Absent is not zero: a
    // row that states no rate is one no route may follow.
    ...(typeof model.price_factor === 'number' ? { rate: model.price_factor } : {}),
    isDefault: model.is_default === true,
    isFree: model.is_free === true,
    ...(tiers === undefined ? {} : { contextTiers: tiers }),
    ...thinking,
  }
}

function makeChunk(id, created, model) {
  return {
    id,
    object: 'chat.completion.chunk',
    created,
    model,
    choices: [{ index: 0, delta: {}, finish_reason: null }],
  }
}

/** How one PAT is stored: this worker's own file, never the Core's config. */
function authPath(root, provider) {
  return join(root, `${provider}.auth.json`)
}

function readStoredPat(root, provider) {
  try {
    const parsed = JSON.parse(readFileSync(authPath(root, provider), 'utf8'))
    return typeof parsed?.pat === 'string' ? parsed.pat.trim() : ''
  } catch {
    return ''
  }
}

function writeStoredPat(root, provider, pat) {
  const path = authPath(root, provider)
  const temporary = `${path}.${process.pid}.tmp`
  writeFileSync(temporary, `${JSON.stringify({ pat, storedAtMs: Date.now() }, null, 2)}\n`, { mode: 0o600 })
  renameSync(temporary, path)
}

function removeStoredPat(root, provider) {
  try {
    rmSync(authPath(root, provider))
    return true
  } catch {
    return false
  }
}

function maskPat(pat) {
  if (typeof pat !== 'string' || pat.length === 0) return ''
  if (pat.length <= 10) return '…'
  return `${pat.slice(0, 3)}…${pat.slice(-4)}`
}

/**
 * One Qoder region: the upstream client, its catalog, and its PAT.
 *
 * A region is the upstream package's own unit of separation: the CN gateway
 * and the international one keep separate endpoints, separate PATs and
 * separate credit.  Nothing here merges them.
 */
class RegionRuntime {
  constructor(spec, clientClass, root) {
    this.region = spec.region
    this.id = spec.provider
    this.displayName = spec.displayName
    this.clientClass = clientClass
    this.root = root
    this.client = this._newClient()
    this.models = []
    this.source = 'none'
    this.fetchedAtMs = 0
    this.catalogError = ''
    /** The desktop session ``credential()`` last answered, if any. */
    this._desktopSessionCache = undefined
    /** Set while one client call runs under the desktop session. */
    this._activeDesktop = undefined
  }

  /**
   * The upstream client, with the desktop app's own sign-in spliced in.
   *
   * The package owns the cosy protocol, so this worker does not reimplement
   * any of it.  What the package has no path for is a credential the desktop
   * app already holds: a `dt-` device token signs in through the same exchange
   * the package performs for a PAT, except that the exchange endpoint only
   * accepts a PAT.  Its response is a *job token* — the identity document the
   * cosy handshake then carries — and a device token is exactly that same
   * identity.  So the one exchange call is answered here, from the desktop
   * session, and every byte after it (the RSA/AES handshake, the signature,
   * the wire format) is the package's own.  Nothing else is intercepted: a
   * pasted PAT takes the untouched path.
   *
   * ``fetch`` is the seam: `exchangeJobToken` and the sash exchange are the
   * only two calls answered locally.
   */
  _newClient() {
    return new this.clientClass(this.region, {
      logger: message => console.error('[qoder]', this.id, message),
      fetch: (url, init) => this._upstreamFetch(url, init),
    })
  }

  /** The desktop session this region is signed into, cached briefly. */
  async _desktopSession({ refresh = false } = {}) {
    const now = Date.now()
    if (!refresh && this._desktopSessionCache !== undefined) {
      const cached = this._desktopSessionCache
      if (now - cached.at < DESKTOP_SESSION_TTL_MS) return cached.value
    }
    let value = await readDesktopSession(this.region)
    if (value === undefined) {
      // ``undefined`` is the one answer that says "no session": caching a
      // ``null`` here made ``credential()`` read a session that was not there.
      this._desktopSessionCache = { at: now, value: undefined }
      return undefined
    }
    // A device token lasts a month, and the desktop app refreshes it when it
    // wakes; an expired one is refreshed here rather than reported as a
    // broken account, because the refresh token it came with lasts a year.
    if (value.expiresAt > 0 && value.expiresAt <= now + 60_000) {
      const refreshed = await refreshDesktopSession(value, OPENAPI_BY_REGION[this.region])
      if (refreshed !== undefined) value = refreshed
    }
    this._desktopSessionCache = { at: now, value }
    return value
  }

  /**
   * One upstream request, with the desktop session's two exchange calls
   * answered locally.  Every other request is forwarded byte for byte.
   */
  async _upstreamFetch(url, init) {
    const target = String(url)
    const active = this._activeDesktop
    if (active !== undefined) {
      if (target.includes('/algo/api/v3/user/jobToken')) {
        // The identity document the cosy handshake carries.  `expireTime` is
        // the identity's own expiry, so a stale device token is reported by
        // the upstream rather than cached as valid forever.
        const document = {
          id: active.userId,
          name: active.userName,
          expireTime: active.expiresAt,
          securityOauthToken: active.token,
          refreshToken: active.refreshToken,
          userType: active.plan || 'personal_standard',
        }
        return new Response(JSON.stringify(document), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (target.endsWith('/api/v1/jobToken/exchange')) {
        // The sash face's own token: the device token is accepted as a bearer
        // there, so no second exchange is needed.
        return new Response(JSON.stringify({ token: active.token, expires_at: new Date(active.expiresAt).toISOString() }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
    }
    return fetch(url, init)
  }

  /**
   * The credential this region serves with, and where it came from.
   *
   * A pasted PAT wins over everything: it is the user's explicit statement
   * about this account.  Without one the desktop app's own sign-in is used
   * (which is what makes adding a Qoder provider need no typing at all), and
   * the environment variable stays as the last resort for a host that states
   * a PAT with no window.
   */
  async credential() {
    const stored = readStoredPat(this.root, this.id)
    if (stored !== '') return { pat: stored, source: 'stored' }
    const desktop = await this._desktopSession()
    if (desktop) {
      return { pat: desktop.token, source: 'app', desktop }
    }
    const fromEnv = (process.env[PAT_ENV[this.region]] ?? '').trim()
    if (fromEnv !== '') return { pat: fromEnv, source: 'env' }
    return undefined
  }

  /** Mark which credential the next client call runs under. */
  _useCredential(credential) {
    this._activeDesktop = credential?.source === 'app' ? credential.desktop : undefined
    return credential?.pat
  }

  /**
   * The account this region's PAT belongs to, or signed-out when it has none.
   *
   * ``refresh`` is not a parameter here: every call that reaches this method
   * asked for an account answer, and the only consumer is the control surface,
   * which repeats the worker's own decision rather than inventing one.
   */
  async status({ withCredits = true } = {}) {
    const credential = await this.credential()
    if (credential === undefined) {
      return {
        provider: this.id,
        region: this.region,
        displayName: this.displayName,
        state: 'signed-out',
        reason: 'no_credential',
        signInSource: 'none',
        modelCount: this.models.length,
        catalogSource: this.source,
        ...(this.fetchedAtMs > 0 ? { catalogFetchedAtMs: this.fetchedAtMs } : {}),
        ...(this.catalogError !== '' ? { catalogError: this.catalogError } : {}),
      }
    }
    const token = this._useCredential(credential)
    try {
      const holder = await this.client.ensureSession(token)
      const jobToken = holder?.jobToken ?? {}
      const nickname = typeof jobToken.name === 'string' && jobToken.name.trim() !== ''
        ? jobToken.name.trim()
        : maskPat(token)
      const result = {
        provider: this.id,
        region: this.region,
        displayName: this.displayName,
        state: 'signed-in',
        nickname,
        domain: this.client.region?.gateway ?? '',
        accountId: maskPat(token),
        plan: typeof jobToken.userType === 'string' ? jobToken.userType : undefined,
        signInSource: credential.source,
        // The desktop app is a second reader of the same account, and the pane
        // says so: "signed in through the app" is a different fact from "a
        // token was pasted here", and only the first one self-heals.  The name
        // avoids every secret marker: Core's snapshot redaction rewrites any
        // ``*credential*`` key to ``configured``, and the pane would then be
        // told a source it cannot name.
        ...(credential.source === 'app' ? { appProfile: credential.desktop.profile } : {}),
        modelCount: this.models.length,
        catalogSource: this.source,
        ...(this.fetchedAtMs > 0 ? { catalogFetchedAtMs: this.fetchedAtMs } : {}),
        ...(this.catalogError !== '' ? { catalogError: this.catalogError } : {}),
      }
      if (withCredits) {
        try {
          const usage = await this.client.fetchUsage(token)
          result.credits = creditsOf(usage)
          // The usage document is the account's own statement of its plan; the
          // job-token exchange carries one only for the PAT path, so a session
          // served from the desktop app reads its plan here.
          if (typeof usage?.userType === 'string' && usage.userType !== '') {
            result.plan = usage.userType
          }
        } catch {
          // A credit read is a second, expensive upstream document.  A region
          // that can chat but cannot read its balance is still signed in, and
          // the absence is reported rather than guessed at zero: no `total`
          // field is what tells the pane "not stated", not "no credit".
          result.credits = { unavailable: true }
        }
      }
      return result
    } catch (error) {
      const failure = errorStatus(error)
      if (failure.kind === 'auth' && credential.source === 'app') {
        // The app's own copy moved under us: re-read it once for the next
        // caller instead of caching a dead token for the whole TTL.
        this._desktopSessionCache = undefined
      }
      return {
        provider: this.id,
        region: this.region,
        displayName: this.displayName,
        state: failure.kind === 'auth' ? 'signed-out' : 'error',
        reason: failure.message,
        signInSource: credential.source,
        modelCount: this.models.length,
        catalogSource: this.source,
        ...(this.fetchedAtMs > 0 ? { catalogFetchedAtMs: this.fetchedAtMs } : {}),
        ...(this.catalogError !== '' ? { catalogError: this.catalogError } : {}),
      }
    } finally {
      this._activeDesktop = undefined
    }
  }

  /** Resolve this region's credential and refresh its catalog. */
  async refresh() {
    const credential = await this.credential()
    if (credential === undefined) {
      this.models = []
      this.source = 'none'
      this.fetchedAtMs = 0
      this.catalogError = ''
      throw new Error(
        `Qoder is not signed in for this region: sign in to the Qoder desktop app, or paste a PAT (or set ${PAT_ENV[this.region]})`,
      )
    }
    const token = this._useCredential(credential)
    let models
    try {
      models = await this.client.fetchModels(token)
    } catch (error) {
      if (errorStatus(error).kind === 'auth' && credential.source === 'app') {
        // One retry on a re-read session: the desktop app may have rotated the
        // token between this read and the request above.
        this._desktopSessionCache = undefined
        const retried = await this.credential()
        if (retried?.source === 'app') {
          const retryToken = this._useCredential(retried)
          try {
            models = await this.client.fetchModels(retryToken)
          } finally {
            this._activeDesktop = undefined
          }
        } else {
          this._activeDesktop = undefined
          throw error
        }
      } else {
        this._activeDesktop = undefined
        throw error
      }
    }
    this._activeDesktop = undefined
    this.models = models.map(normalizeModel).filter(model => typeof model.id === 'string' && model.id !== '')
    this.source = 'live'
    this.fetchedAtMs = Date.now()
    this.catalogError = ''
    return this.models
  }

  /** Verify a PAT before storing it, so a wrong paste never looks signed in. */
  async login(pat) {
    const value = typeof pat === 'string' ? pat.trim() : ''
    if (value === '') throw new Error('Qoder PAT is required')
    // The catalog read is the cheapest authenticated call that also proves the
    // account can serve this region: a jobToken exchange alone does not say
    // the token owns the model gateway.
    // A new PAT must not inherit the previous account's cached cosy session:
    // start from a clean client, then keep the session this validation just
    // exchanged so the status read below reuses it.
    this.client = this._newClient()
    this._activeDesktop = undefined
    await this.client.fetchModels(value)
    writeStoredPat(this.root, this.id, value)
    try {
      await this.refresh()
    } catch {
      // The token is valid (the call above answered); a catalog that failed on
      // the second read is reported by its own state instead of undoing a
      // sign-in that succeeded.
    }
    return this.status({ withCredits: true })
  }

  /**
   * Forget this region's pasted PAT.
   *
   * The desktop app's own sign-in is not this app's to end: signing out there
   * is the user's action in that app, so the answer says which of the two
   * holds the account afterwards.  A pane that claimed 已退出 while the app is
   * still signed in would be stating something it cannot make true.
   */
  async logout() {
    const removed = removeStoredPat(this.root, this.id)
    // Drop the session the client cached for the credential just removed, and
    // re-read the desktop app on the next ask: the user may have signed it out.
    this.client = this._newClient()
    this._desktopSessionCache = undefined
    this._activeDesktop = undefined
    this.models = []
    this.source = 'none'
    this.fetchedAtMs = 0
    const appSession = (await this._desktopSession()) !== undefined
    return { provider: this.id, removed, appSession }
  }

  /** Turn one OpenAI body into a Qoder chat call and answer it. */
  async forwardChat(req, res, body) {
    let parsed
    try {
      parsed = JSON.parse(body.toString('utf8') || '{}')
    } catch {
      json(res, 400, { error: { message: 'invalid JSON body', code: 'invalid_request_error', type: 'invalid_request_error' } })
      return
    }
    const messages = Array.isArray(parsed.messages) ? parsed.messages : []
    if (messages.length === 0) {
      json(res, 400, { error: { message: 'messages is required', code: 'invalid_request_error', type: 'invalid_request_error' } })
      return
    }
    const model = typeof parsed.model === 'string' ? parsed.model : ''
    const key = this.models.find(entry => entry.id === model || entry.name === model)?.id ?? model
    const credential = await this.credential()
    if (credential === undefined) {
      json(res, 401, {
        error: {
          message: `Qoder is not signed in for this region: sign in to the Qoder desktop app, or paste a PAT (or set ${PAT_ENV[this.region]})`,
          code: 'invalid_api_key',
          type: 'qoder_error',
        },
      })
      return
    }
    const wantsStream = parsed.stream === true
    const effort = typeof parsed.reasoning_effort === 'string' && parsed.reasoning_effort !== ''
      ? parsed.reasoning_effort
      : undefined
    const toolChoice = parsed.tool_choice
    const tools = toolChoice === 'none' || !Array.isArray(parsed.tools) ? undefined : parsed.tools
    const supportsImages = this.models.find(entry => entry.id === key)?.supportsImages === true
    const hasImages = messages.some(message =>
      Array.isArray(message?.content)
      && message.content.some(part => part?.type === 'image_url'),
    )
    if (hasImages && !supportsImages) {
      json(res, 400, {
        error: {
          message: `model ${model} does not accept image input`,
          code: 'invalid_request_error',
          type: 'invalid_request_error',
        },
      })
      return
    }

    const controller = new AbortController()
    res.on('close', () => controller.abort())
    if (res.destroyed) controller.abort()

    const id = `chatcmpl-${randomUUID().replaceAll('-', '')}`
    const created = Math.floor(Date.now() / 1000)
    let wroteBytes = false
    let finishReason = null
    let inputTokens = 0
    let outputTokens = 0
    let cachedTokens
    let aggregated = ''
    let aggregatedReasoning = ''
    const toolCalls = []

    const openStream = () => {
      if (wroteBytes) return
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
        'X-Accel-Buffering': 'no',
      })
      wroteBytes = true
    }

    const token = this._useCredential(credential)
    try {
      await this.client.chatStream(token, key, messages, {
        ...(tools === undefined ? {} : { tools }),
        // The same rule the plugin's shim applies: an explicit effort turns
        // thinking on (unless it is ``off``), and a client that only asked for
        // reasoning through ``stream_options`` still gets it.
        isReasoning: effort !== 'off' && (
          (effort !== undefined && effort !== '')
          || parsed.stream_options?.include_reasoning === true
        ),
        ...(effort === undefined || effort === 'off' ? {} : { reasoningEffort: effort }),
        isVl: supportsImages,
        signal: controller.signal,
        onDelta: delta => {
          if (delta.inputTokens > 0) inputTokens = delta.inputTokens
          if (delta.outputTokens > 0) outputTokens = delta.outputTokens
          if (delta.cachedTokens !== undefined) cachedTokens = delta.cachedTokens
          if (delta.toolCalls !== undefined) {
            finishReason = 'tool_calls'
            for (const call of delta.toolCalls) {
              const index = Number.isInteger(call?.index) ? call.index : toolCalls.length
              const target = toolCalls[index] ?? { id: '', type: 'function', function: { name: '', arguments: '' } }
              if (call?.id) target.id = call.id
              if (call?.type) target.type = call.type
              if (call?.function?.name) target.function.name += call.function.name
              if (call?.function?.arguments) target.function.arguments += call.function.arguments
              toolCalls[index] = target
            }
          }
          if (wantsStream) {
            openStream()
            const chunk = makeChunk(id, created, model)
            const choice = chunk.choices[0]
            if (delta.role !== '') choice.delta.role = delta.role
            if (delta.content !== '') choice.delta.content = delta.content
            if (delta.reasoningContent !== '') choice.delta.reasoning_content = delta.reasoningContent
            if (delta.toolCalls !== undefined) choice.delta.tool_calls = delta.toolCalls
            res.write(`data: ${JSON.stringify(chunk)}\n\n`)
          } else {
            aggregated += delta.content
            aggregatedReasoning += delta.reasoningContent
          }
        },
      })
      if (wantsStream) {
        const done = makeChunk(id, created, model)
        done.choices[0].finish_reason = finishReason ?? 'stop'
        if (inputTokens > 0 || outputTokens > 0) {
          done.usage = {
            prompt_tokens: inputTokens,
            completion_tokens: outputTokens,
            total_tokens: inputTokens + outputTokens,
            ...(cachedTokens === undefined ? {} : { prompt_tokens_details: { cached_tokens: cachedTokens } }),
          }
        }
        res.write(`data: ${JSON.stringify(done)}\n\n`)
        res.write('data: [DONE]\n\n')
        res.end()
        return
      }
      const message = { role: 'assistant', content: aggregated }
      if (aggregatedReasoning !== '') message.reasoning_content = aggregatedReasoning
      if (toolCalls.length > 0) message.tool_calls = toolCalls.filter(Boolean)
      json(res, 200, {
        id,
        object: 'chat.completion',
        created,
        model,
        choices: [{ index: 0, message, finish_reason: finishReason ?? 'stop' }],
        ...(inputTokens > 0 || outputTokens > 0
          ? {
              usage: {
                prompt_tokens: inputTokens,
                completion_tokens: outputTokens,
                total_tokens: inputTokens + outputTokens,
                ...(cachedTokens === undefined ? {} : { prompt_tokens_details: { cached_tokens: cachedTokens } }),
              },
            }
          : {}),
      })
    } catch (error) {
      const failure = errorStatus(error)
      if (wantsStream && wroteBytes) {
        // Bytes are already committed: the error belongs in the stream, and a
        // replay would show the client two answers.
        const chunk = makeChunk(id, created, model)
        chunk.choices[0].finish_reason = 'error'
        chunk.error = { message: failure.message, code: failure.code, type: 'qoder_error' }
        res.write(`data: ${JSON.stringify(chunk)}\n\n`)
        res.write('data: [DONE]\n\n')
        res.end()
        return
      }
      if (failure.kind === 'auth' && credential.source === 'app') {
        // A token the desktop app rotated between the read and the request:
        // the next caller re-reads it instead of reusing a dead one.
        this._desktopSessionCache = undefined
      }
      json(res, failure.status, {
        error: { message: failure.message, code: failure.code, type: 'qoder_error' },
      }, error?.retryAfterMs)
    } finally {
      this._activeDesktop = undefined
    }
  }
}

async function main() {
  const options = parseArguments(process.argv.slice(2))
  if (options.help) {
    usage()
    return 0
  }
  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535) {
    throw new Error('qoder worker requires a valid --port')
  }
  if (!options.token) throw new Error('qoder worker requires an inbound --token')
  const root = options.root || process.env.YOUNG_ROUTER_QODER_ROOT || ''
  if (!root) throw new Error('qoder worker requires a private --root directory')
  mkdirSync(root, { recursive: true, mode: 0o700 })

  const { clientClass, entry } = await loadUpstream(options.entry)
  const runtimes = new Map()
  for (const spec of REGIONS) {
    runtimes.set(spec.provider, new RegionRuntime(spec, clientClass, root))
  }

  const bearerOk = req => {
    const header = req.headers.authorization
    if (typeof header !== 'string') return false
    const match = /^Bearer\s+(.+)$/i.exec(header.trim())
    return match !== null && match[1] === options.token
  }

  const server = createServer((req, res) => {
    void handle(req, res).catch(error => {
      if (!res.headersSent) {
        const failure = errorStatus(error)
        json(res, failure.status, {
          error: { message: failure.message, code: failure.code, type: 'worker_error' },
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
      const refresh = ['1', 'true', 'yes'].includes((url.searchParams.get('refresh') ?? '').toLowerCase())
      const status = {}
      for (const [id, runtime] of runtimes) {
        if (refresh) {
          try {
            await runtime.refresh()
          } catch {
            // A region with no PAT, or an upstream that refused the catalog,
            // is reported by status() below.
          }
        }
        // The credit is part of the account answer: the pane shows both from
        // one read, and a status call that omitted it would paint an account
        // row with no balance until someone pressed 刷新.  The usage document
        // is one extra upstream call per region, bounded by the client's TTL.
        status[id] = await runtime.status({ withCredits: true })
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
      try {
        await runtime.refresh()
      } catch (error) {
        const status = await runtime.status({ withCredits: false })
        json(res, 200, {
          provider: id,
          available: false,
          detail: String(error?.message ?? error),
          status,
          models: [],
        })
        return
      }
      const status = await runtime.status({ withCredits: false })
      json(res, 200, {
        provider: id,
        available: true,
        source: runtime.source,
        fetchedAtMs: runtime.fetchedAtMs || undefined,
        status,
        models: runtime.models,
      })
      return
    }
    if (req.method === 'POST' && url.pathname === '/control/login') {
      let body
      try {
        body = JSON.parse((await readBody(req)).toString('utf8') || '{}')
      } catch {
        json(res, 400, { error: { message: 'invalid JSON body', type: 'invalid_request_error' } })
        return
      }
      const runtime = runtimes.get(String(body.provider ?? ''))
      if (!runtime) {
        json(res, 404, { error: { message: `unknown provider: ${body.provider}`, type: 'not_found' } })
        return
      }
      try {
        const status = await runtime.login(body.pat)
        json(res, 200, { provider: runtime.id, available: true, status })
      } catch (error) {
        const failure = errorStatus(error)
        json(res, failure.status, {
          error: { message: failure.message, code: failure.code, type: 'qoder_error' },
        })
        return
      }
      return
    }
    if (req.method === 'POST' && url.pathname === '/control/logout') {
      let body
      try {
        body = JSON.parse((await readBody(req)).toString('utf8') || '{}')
      } catch {
        json(res, 400, { error: { message: 'invalid JSON body', type: 'invalid_request_error' } })
        return
      }
      const runtime = runtimes.get(String(body.provider ?? ''))
      if (!runtime) {
        json(res, 404, { error: { message: `unknown provider: ${body.provider}`, type: 'not_found' } })
        return
      }
      const result = await runtime.logout()
      json(res, 200, { ...result, status: await runtime.status({ withCredits: false }) })
      return
    }
    if (req.method === 'GET' && url.pathname === '/control/app') {
      // The app that owns this region's sign-in, so a signed-out pane can hand
      // the user to it instead of only asking for a token.  Absent means the
      // app is not installed here — not an error.
      const id = url.searchParams.get('provider') ?? ''
      const runtime = runtimes.get(id)
      if (!runtime) {
        json(res, 404, { error: { message: `unknown provider: ${id}`, type: 'not_found' } })
        return
      }
      const app = desktopApp(runtime.region)
      json(res, 200, {
        provider: id,
        ...(app === undefined ? {} : { appName: app.appName, bundle: app.bundle }),
        installed: app !== undefined,
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
      if (req.method === 'GET' && rest[1] === 'models') {
        // The callable names, which are the public ones: `forwardChat` accepts
        // either, but a listing that named the wire keys would advertise
        // spellings no model menu shows.
        json(res, 200, {
          object: 'list',
          data: runtime.models.map(model => ({ id: model.publicName || model.id, object: 'model', owned_by: runtime.id })),
        })
        return
      }
      if (req.method === 'POST' && rest[1] === 'chat' && rest[2] === 'completions') {
        const body = await readBody(req)
        await runtime.forwardChat(req, res, body)
        return
      }
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

  // Warm the regions that have a PAT, in the background.  A region with none
  // stays signed out without a logged failure: "no credential yet" is a normal
  // state, not a broken startup.
  for (const runtime of runtimes.values()) {
    if (runtime.credential() === undefined) continue
    void runtime.refresh().catch(error => {
      console.error('[warm]', runtime.id, String(error?.message ?? error))
    })
  }

  // Shutdown stops accepting work but lets a turn already in flight finish:
  // this worker is the managed proxy's upstream, and cutting its sockets on
  // SIGTERM turned every app restart into a turn the user had to resend.
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
  const parent = process.ppid
  const orphanWatch = setInterval(() => {
    if (process.ppid === parent) return
    clearInterval(orphanWatch)
    finishShutdown()
  }, ORPHAN_POLL_MS)
  orphanWatch.unref?.()
  await new Promise(() => {})
  return 0
}

// Importable for the tests below without starting a listener: the entry point
// runs only when this file is the process's main module.
export { loadUpstream, normalizeModel, creditsOf, errorStatus, contextWindowOf }

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch(error => {
    process.stderr.write(`qoder worker failed: ${String(error?.stack ?? error)}\n`)
    process.exit(1)
  })
}

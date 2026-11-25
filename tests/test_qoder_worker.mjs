/**
 * The Qoder worker's published surface, without a Qoder account.
 *
 * The upstream package is staged only at artifact-build time, so this test
 * builds a tiny package with the same shape and proves the worker finds the
 * upstream client by what it exports -- the published chunk carries a build
 * hash in its name, and a release that renames it must still be found by the
 * one shape the worker drives.  The catalog and credit projections are pure
 * functions and are pinned directly.
 */

import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const worker = await import(pathToFileURL(join(import.meta.dirname, '../young_router/adapters/qoder_worker.mjs')).href)

// -- catalog projection -------------------------------------------------------

const model = worker.normalizeModel({
  key: 'qwen3.8-max',
  display_name: 'Qwen3.8-Max',
  enable: true,
  is_default: true,
  is_vl: true,
  is_reasoning: true,
  is_free: false,
  price_factor: 0.5,
  max_input_tokens: 200_000,
  max_output_tokens: 32_000,
  thinking_config: { enabled: { efforts: { xhigh: {}, low: {}, medium: { is_default: true } } } },
  context_config: {
    '200K': { token_count: 200_000, is_default: true },
    '500K': { token_count: 500_000 },
  },
})
assert.equal(model.id, 'qwen3.8-max')
assert.equal(model.name, 'Qwen3.8-Max')
assert.equal(model.contextWindow, 200_000)
assert.equal(model.maxTokens, 32_000)
assert.equal(model.supportsImages, true)
assert.equal(model.reasoning, true)
assert.equal(model.rate, 0.5)
assert.deepEqual(model.contextTiers, [200_000, 500_000])
assert.deepEqual(model.thinkingEfforts, ['low', 'medium', 'xhigh'])
assert.equal(model.defaultThinkingEffort, 'medium')

// A catalog row that states nothing is not given a number: no rate field, and
// the window falls back to the plugin's documented default rather than zero.
const bare = worker.normalizeModel({ key: 'bare', display_name: 'Bare' })
assert.equal('rate' in bare, false)
assert.equal(bare.contextWindow, 200_000)
assert.equal(bare.maxTokens, undefined)

// -- scene catalog -----------------------------------------------------------

// The upstream model list is one array per client surface.  HungerG is only
// in `assistant` — the scene this worker's chat requests are sent as — while
// the package's `fetchModels` reads `chat` alone, so a catalog read that only
// asked the package dropped it.  These rows are the live document's shape.
const hunger = {
  key: 'hmodel',
  display_name: 'HungerG',
  enable: true,
  is_default: true,
  is_vl: true,
  is_reasoning: true,
  is_free: true,
  price_factor: 0,
  tags: ['free'],
  max_input_tokens: 1_000_000,
  context_config: {
    '200K': { token_count: 200_000, is_default: true },
    '400K': { token_count: 400_000 },
    '1M': { token_count: 1_000_000 },
  },
  thinking_config: { enabled: { efforts: { low: {}, high: {}, max: { is_default: true } } } },
}
const fromScene = worker.catalogRowsOf({
  chat: [
    { key: 'auto', display_name: 'Auto' },
    { key: 'qmodel_38max', display_name: 'Qwen3.8-Max' },
  ],
  assistant: [
    { key: 'auto', display_name: 'Auto' },
    { key: 'fast', display_name: 'Fast' },
    hunger,
    { key: 'qmodel_38max', display_name: 'Qwen3.8-Max' },
  ],
})
// Assistant order first (the order Qoder's own menu shows), then a row only
// `chat` lists; the router row is dropped and a duplicate is answered once.
assert.deepEqual(fromScene.map((row) => row.key), ['fast', 'hmodel', 'qmodel_38max'])
assert.equal(fromScene[1].display_name, 'HungerG')
// A document without the scenes is not an empty catalog: the caller falls back.
assert.equal(worker.catalogRowsOf({}), undefined)
assert.equal(worker.catalogRowsOf({ assistant: [], chat: [] }), undefined)
// `chat` alone still answers, so a future release that drops `assistant`
// loses HungerG rather than every model.
assert.deepEqual(
  worker.catalogRowsOf({ chat: [{ key: 'dfmodel', display_name: 'DeepSeek-Flash' }] }).map((row) => row.key),
  ['dfmodel'],
)

const normalizedHunger = worker.normalizeModel(hunger)
assert.equal(normalizedHunger.id, 'hmodel')
assert.equal(normalizedHunger.publicName, 'HungerG')
assert.equal(normalizedHunger.rate, 0)
assert.equal(normalizedHunger.isFree, true)
assert.equal(normalizedHunger.isDefault, true)
assert.equal(normalizedHunger.contextWindow, 200_000)
assert.deepEqual(normalizedHunger.contextTiers, [200_000, 400_000, 1_000_000])
assert.deepEqual(normalizedHunger.thinkingEfforts, ['low', 'high', 'max'])
assert.equal(normalizedHunger.defaultThinkingEffort, 'max')

// -- catalog read -------------------------------------------------------------

const region = { gateway: 'https://gw.example', modelsPath: '/algo/api/v2/model/list?Encode=1' }
{
  let requested = ''
  const rows = await worker.readCatalog({
    region,
    async ensureSession(pat) {
      assert.equal(pat, 'dt-test')
      return { jobToken: {} }
    },
    async bearerGet(_holder, url) {
      requested = url
      return { assistant: [hunger], chat: [] }
    },
    async fetchModels() {
      throw new Error('the scene read answered; fetchModels must not be called')
    },
  }, 'dt-test')
  assert.equal(requested, 'https://gw.example/algo/api/v2/model/list?Encode=1')
  assert.deepEqual(rows.map((row) => row.key), ['hmodel'])
}
// A package that no longer exposes the signed read, or a document whose scene
// moved, answers the package's own catalog instead of no catalog.
for (const client of [
  { region, async fetchModels() { return 'package-catalog' } },
  {
    region,
    async ensureSession() { throw new Error('no holder') },
    async bearerGet() { return { assistant: [hunger] } },
    async fetchModels() { return 'package-catalog' },
  },
  {
    region,
    async ensureSession() { return {} },
    async bearerGet() { return { another_scene: [hunger] } },
    async fetchModels() { return 'package-catalog' },
  },
]) {
  assert.equal(await worker.readCatalog(client, 'dt-test'), 'package-catalog')
}

// -- credits ------------------------------------------------------------------

assert.equal(worker.creditsOf(undefined).total, 0)
assert.equal(
  worker.creditsOf({
    userQuota: { total: 100, used: 8 },
    addOnQuota: { total: 20, used: 0 },
    orgResourcePackage: { cap: 10, used: 10 },
  }).total,
  112,
)

// -- error classification -----------------------------------------------------

assert.equal(worker.errorStatus({ kind: 'auth', message: 'bad token' }).status, 401)
assert.equal(worker.errorStatus({ kind: 'rate-limited', message: 'slow down' }).status, 429)
assert.equal(worker.errorStatus({ kind: 'quota-exhausted', message: 'no credit' }).code, 'quota_exceeded')
assert.equal(worker.errorStatus(new Error('unexpected')).status, 500)

// A queued model is a wait, not a broken connection: the envelope's own HTTP
// 403 was passed through, so Codex reported a dead network and retried every
// 30 s while the proxy read the 403 as a refused credential.  The message now
// names the queue and its stated wait, which is the only channel LiteLLM's
// exception keeps for it; the transport status stays the transient 502 because
// the managed router retries a 429 eight times inside one request.
const queued = worker.errorStatus({
  kind: 'transient',
  message: '上游信封错误: HTTP 403 {"code":"10605"}',
  queue: { isQueued: true, queueCount: 9031, queueType: 'p3', retryAfterSeconds: 30, serviceAvailable: true },
})
assert.equal(queued.status, 502)
assert.match(queued.message, /upstream queued/)
assert.match(queued.message, /retry after 30s/)
assert.doesNotMatch(queued.message, /http 403/i)
// The queue is named but the service itself is refused: that is an upstream
// outage, not a wait on this model, so the message keeps the plugin's words.
assert.doesNotMatch(
  worker.errorStatus({
    kind: 'transient',
    message: 'busy',
    queue: { isQueued: true, queueCount: 0, serviceAvailable: false },
  }).message,
  /upstream queued/,
)
// An empty queue at the upstream is still a wait, and states no delay.
const drained = worker.errorStatus({
  kind: 'transient',
  message: 'busy',
  queue: { isQueued: true, queueCount: 0 },
})
assert.match(drained.message, /upstream queued/)
assert.doesNotMatch(drained.message, /retry after/)

// -- chunk discovery ----------------------------------------------------------

const root = mkdtempSync(join(tmpdir(), 'qoder-worker-test-'))
try {
  const lib = join(root, 'lib')
  mkdirSync(lib)
  // A build-hashed chunk name, an entry that re-exports it under the public
  // plugin surface, and a decoy module that must not be mistaken for a client.
  writeFileSync(
    join(lib, 'chunk-ABC123.js'),
    `export class QoderUpstreamClient {
      constructor(region) { this.region = { gateway: 'https://' + region + '.example' } }
      async chatStream() {}
      async fetchModels() { return [] }
    }
    export const OTHER = Math.PI
    `,
  )
  writeFileSync(
    join(lib, 'bin.js'),
    `process.stdout.write('the CLI binary must never be imported by discovery\\n')\n`,
  )
  writeFileSync(
    join(lib, 'index.js'),
    `export { QoderUpstreamClient as A } from './chunk-ABC123.js'\n`,
  )
  const loaded = await worker.loadUpstream(join(lib, 'index.js'))
  assert.equal(typeof loaded.clientClass, 'function')
  const instance = new loaded.clientClass('cn')
  assert.equal(instance.region.gateway, 'https://cn.example')
  // The discovery order is the entry's own imports first, so a package that
  // renamed its hash keeps working without the worker naming the new file.
  assert.equal(loaded.entry, join(lib, 'index.js'))

  const missing = join(root, 'empty')
  mkdirSync(missing)
  writeFileSync(join(missing, 'index.js'), 'export const apply = () => {}\n')
  await assert.rejects(
    () => worker.loadUpstream(join(missing, 'index.js')),
    /no longer exports the upstream client/,
  )
} finally {
  rmSync(root, { recursive: true, force: true })
}

console.log('Qoder worker tests OK')

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

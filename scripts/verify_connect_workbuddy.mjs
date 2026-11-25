#!/usr/bin/env node
/**
 * Prove a staged `dsh-connect-workbuddy` package still offers the surface the
 * WorkBuddy worker drives, including the catalog read's *return shape*.
 *
 * The package moved `fetchModels` from a bare roster to
 * `{ models, degraded }` in 5.31.0 (#288).  The worker kept handing that
 * envelope straight to `WorkBuddyCatalog.set`, whose own `models.map` threw, so
 * every build after the changepoint reported `models.map is not a function`,
 * fell back to the bundled 11-model roster, and lost every 倍率 with it.  The
 * artifact build verified export names only, so a return-type change could not
 * fail it; the regression surfaced in the pane instead.
 *
 * This script drives the same call the worker makes, with the transport stubbed
 * so it needs no credential and spends no upstream request.  It asserts the
 * shape the worker accepts (the envelope, or the pre-5.31.0 array) and that the
 * parsed entry still carries `creditMultiplier` -- the number the 倍率 column
 * and 跟随倍率 both read.  A release that moves either seam fails the build.
 *
 * Usage: node scripts/verify_connect_workbuddy.mjs <staged>/lib/index.js
 *
 * @module young_router/verify_connect_workbuddy
 */

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { pathToFileURL } from 'node:url'

const entry = process.argv[2] ?? ''
if (entry === '') {
  process.stderr.write('usage: verify_connect_workbuddy.mjs <staged>/lib/index.js\n')
  process.exit(2)
}

function fail(message) {
  process.stderr.write(`${message}\n`)
  process.exit(1)
}

let library
try {
  library = await import(pathToFileURL(entry).href)
} catch (error) {
  fail(`the staged WorkBuddy package cannot be imported: ${error?.message ?? error}`)
}

// The same names the worker imports.  A release that renames one is a build
// failure here rather than a provider that reports itself unavailable later.
for (const name of [
  'WorkBuddyCredentialStore',
  'WorkBuddyUpstreamClient',
  'WorkBuddyCatalog',
  'createWorkBuddyShim',
  'WORKBUDDY_REGIONS',
  'WORKBUDDY_PROVIDERS',
  'WORKBUDDY_PROVIDER_DISPLAY_NAMES',
  'workbuddyAppExecutableCandidates',
  'findWorkbuddyAppExecutable',
]) {
  if (library[name] === undefined) fail(`the staged WorkBuddy package exports no ${name}`)
}

const regions = library.WORKBUDDY_REGIONS
if (!Array.isArray(regions) || regions.length !== 2) {
  fail(`WORKBUDDY_REGIONS is not the two regions: ${JSON.stringify(regions)}`)
}
for (const region of regions) {
  if (typeof library.WORKBUDDY_PROVIDERS[region] !== 'string') {
    fail(`the staged WorkBuddy package names no provider for region: ${region}`)
  }
  if (typeof library.WORKBUDDY_PROVIDER_DISPLAY_NAMES[region] !== 'string') {
    fail(`the staged WorkBuddy package names no display name for region: ${region}`)
  }
}

// One catalog-shaped answer for the CN gateway: a single CLI row priced at
// `x0.42`, which is what the worker's rate hand-off reads.
const answer = {
  code: 0,
  data: {
    models: [{
      id: 'verify-row',
      name: 'Verify row',
      maxInputTokens: 1_000,
      maxOutputTokens: 100,
      credits: 'x0.42 credits',
    }],
    agents: [{ name: 'cli', models: ['verify-row'] }],
  },
}
const originalFetch = globalThis.fetch
globalThis.fetch = async () =>
  new Response(JSON.stringify(answer), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
let read
try {
  read = await new library.WorkBuddyUpstreamClient().fetchModels({
    accessToken: 'verify-token',
    refreshToken: 'verify-refresh',
    expiresAtMs: Date.now() + 3_600_000,
    domain: 'www.codebuddy.cn',
    uid: '',
  })
} catch (error) {
  fail(`the staged WorkBuddy catalog read failed: ${error?.message ?? error}`)
} finally {
  globalThis.fetch = originalFetch
}

const models = Array.isArray(read) ? read : read?.models
if (!Array.isArray(models)) {
  fail('fetchModels no longer answers a roster or { models }: the worker cannot stage it')
}
const row = models.find((model) => model?.id === 'verify-row')
if (row === undefined) fail('the staged WorkBuddy catalog read parsed no verify-row')
if (row.creditMultiplier !== 0.42) {
  fail(`the staged WorkBuddy catalog read no longer publishes creditMultiplier: ${JSON.stringify(row)}`)
}

process.stdout.write(`WorkBuddy staged surface OK (${stagedVersion(entry)})\n`)

/** The version whose release this check just drove, for the build log. */
function stagedVersion(entry) {
  try {
    const manifest = JSON.parse(readFileSync(join(dirname(entry), '..', 'package.json'), 'utf8'))
    return typeof manifest.version === 'string' ? manifest.version : 'unknown'
  } catch {
    return 'unknown'
  }
}

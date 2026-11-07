#!/usr/bin/env node
/**
 * Prove a staged `dsh-connect-qoder` package still offers the surface the
 * Qoder worker drives.
 *
 * The published entry point is a DeepSeek Harness plugin; the cosy protocol and
 * the upstream client live in a bundled chunk whose file name carries a build
 * hash.  The worker therefore finds the client by shape, not by name — a class
 * with both `chatStream` and `fetchModels`.  This script runs the same discovery
 * at artifact-build time, so a release that moved the client fails the build
 * instead of surfacing as "Qoder is unavailable" on the first request.
 *
 * Usage: node scripts/verify_connect_qoder.mjs <staged>/lib/index.js
 *
 * @module young_router/verify_connect_qoder
 */

import { readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { pathToFileURL } from 'node:url'

const entry = process.argv[2] ?? ''
if (entry === '') {
  process.stderr.write('usage: verify_connect_qoder.mjs <staged>/lib/index.js\n')
  process.exit(2)
}

const libDir = dirname(entry)
const entryName = entry.slice(libDir.length + 1)
const candidates = []
for (const file of readdirSync(libDir).sort()) {
  // `bin.js` runs the package's own CLI on import, `client.js` is the browser
  // bundle, and the entry itself is the Harness plugin (its runtime imports are
  // the whole DSH client surface, which this worker never loads).  The worker
  // drives the peer-only chunk, so that is the surface this check imports.
  if (!file.endsWith('.js') || file === 'bin.js' || file === 'client.js' || file === entryName) continue
  candidates.push(join(libDir, file))
}

let client
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
      client = value
      break
    }
  }
  if (client !== undefined) break
}

if (client === undefined) {
  process.stderr.write('the staged Qoder package exports no upstream client (chatStream + fetchModels)\n')
  process.exit(1)
}

// Both regions must still name a gateway: a release that renamed the region
// table would leave one provider pointing at nothing.
for (const region of ['cn', 'global']) {
  const instance = new client(region)
  if (!instance.region || typeof instance.region.gateway !== 'string' || instance.region.gateway === '') {
    process.stderr.write(`the staged Qoder client has no gateway for region: ${region}\n`)
    process.exit(1)
  }
}

process.stdout.write('Qoder staged surface OK\n')

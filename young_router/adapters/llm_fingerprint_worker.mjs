#!/usr/bin/env node
/**
 * The precision probe's worker: one run of llm-fingerprint-detector.
 *
 * llm-fingerprint-detector (https://github.com/ToseaAI/llm-fingerprint-detector,
 * MIT) is a TypeScript library, so it runs on the Node runtime this app already
 * bundles rather than being re-implemented in Python.  This script does only the
 * three things a Core needs around a library: read one job from stdin, call the
 * library, write one JSON result to stdout.
 *
 * Nothing about the measurement lives here.  The probe battery, the
 * reasoning-disable adapters, the Jensen-Shannon statistic, the split-half
 * self-consistency check and the verdict thresholds all come from the staged
 * package - the thresholds in particular are the paper's (arXiv:2607.10252):
 * a same-model cross-provider median distance of ~0.227 against a
 * different-model median of ~0.463, with cut points at 0.25 and 0.35.
 *
 * Two modes:
 *
 *   {"mode": "fingerprint", "base_url", "api_key", "model", ...}
 *       collect one fingerprint and return it verbatim
 *
 *   {"mode": "verify", "reference": {...}, "base_url", "api_key", "model", ...}
 *       collect one fingerprint and compare it against a recorded reference
 *
 * Both answer with the library's own verdict object.  A run that could not
 * measure says so; it never reports an empty comparison as a match.
 */

import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const STAGED_ROOT = process.env.YOUNG_ROUTER_LFD_DIR || new URL('.', import.meta.url).pathname;

/** The staged package's entry point, preferring its build output. */
function resolveModule() {
  const candidates = [`${STAGED_ROOT}/dist/index.js`, `${STAGED_ROOT}/src/index.ts`];
  for (const candidate of candidates) {
    try {
      readFileSync(candidate);
      return pathToFileURL(candidate).href;
    } catch {
      continue;
    }
  }
  throw new Error(
    `llm-fingerprint-detector is not staged at ${STAGED_ROOT}; run scripts/update_llm_fingerprint.py before building`,
  );
}

/**
 * Make the library's requests go out over pooled HTTP/1.1 connections.
 *
 * Measured on api.deepseek.com, this is the difference between a usable probe
 * and an unusable one.  Node's built-in fetch speaks HTTP/2 to that host and
 * multiplexes every request onto one connection; the endpoint serialises them
 * there, so 16 concurrent one-token calls took 18.2 s instead of 0.8 s — a
 * 20x penalty that has nothing to do with the model.  An undici Agent with one
 * connection per in-flight request removes it.
 *
 * The library itself reaches the network through `fetch`, so replacing the
 * global dispatcher is enough and needs no patch to the package.  When undici
 * cannot be loaded the run proceeds on the built-in fetch: slower, but the
 * probe still works, and a missing optimisation must never be a failed check.
 */
async function installPooledDispatcher(concurrency) {
  // Resolve undici beside the staged package, not beside this script: the
  // worker lives in the Core's adapter directory while the dependency is
  // installed with the staged library, and a bare specifier would look only at
  // this file's own ancestors.
  const candidates = [
    `${STAGED_ROOT}/node_modules/undici/index.js`,
    'undici',
  ];
  for (const specifier of candidates) {
    try {
      const { Agent, setGlobalDispatcher } = await import(
        specifier.startsWith('/') ? pathToFileURL(specifier).href : specifier
      );
      setGlobalDispatcher(
        new Agent({ connections: Math.max(1, concurrency), pipelining: 1, keepAliveTimeout: 10_000 }),
      );
      return 'undici';
    } catch {
      continue;
    }
  }
  return 'node-fetch';
}

function readJob() {
  const raw = readFileSync(0, 'utf8');
  if (!raw.trim()) throw new Error('no job on stdin');
  const job = JSON.parse(raw);
  if (!job || typeof job !== 'object') throw new Error('the job is not an object');
  return job;
}

/** The library's own options, taken from the job and nothing else. */
function options(job) {
  const chosen = {};
  // The library's defaults are the standard preset (8 cells, 25 samples).  A
  // caller may ask for a cheaper run, and the result records what it used, so a
  // short run is never mistaken for a full one.
  if (Number.isInteger(job.cells) && job.cells > 0) chosen.cells = job.cells;
  if (Number.isInteger(job.samples_per_cell) && job.samples_per_cell > 0) {
    chosen.samplesPerCell = job.samples_per_cell;
  }
  if (Number.isInteger(job.concurrency) && job.concurrency > 0) chosen.concurrency = job.concurrency;
  if (Number.isInteger(job.timeout_ms) && job.timeout_ms > 0) chosen.timeoutMs = job.timeout_ms;
  if (Number.isInteger(job.max_retries) && job.max_retries >= 0) chosen.maxRetries = job.max_retries;
  return chosen;
}

function endpointOf(job) {
  if (!job.base_url || typeof job.base_url !== 'string') throw new Error('the job has no base_url');
  if (!job.model || typeof job.model !== 'string') throw new Error('the job has no model');
  return {
    baseUrl: job.base_url,
    model: job.model,
    apiKey: typeof job.api_key === 'string' ? job.api_key : undefined,
  };
}

async function main() {
  const job = readJob();
  const library = await import(resolveModule());
  const { fingerprint, compare } = library;

  const endpoint = endpointOf(job);
  const settings = options(job);
  const transport = await installPooledDispatcher(settings.concurrency ?? 8);
  const startedAt = Date.now();
  const run = await fingerprint(endpoint, settings);
  const elapsedMs = Date.now() - startedAt;

  const result = {
    mode: job.mode === 'verify' ? 'verify' : 'fingerprint',
    fingerprint: run.fingerprint,
    // The library's own self-consistency readout: the distance between the two
    // halves of one run.  A high value means the endpoint disagrees with
    // itself, which is the aggregator-rotating-backends signature and makes any
    // comparison against it meaningless.
    split_half_jsd: run.splitHalfJsd ?? null,
    warnings: Array.isArray(run.warnings) ? run.warnings : [],
    post_reasoning: run.fingerprint?.postReasoning ?? false,
    samples_per_cell: run.fingerprint?.samplesPerCell ?? null,
    cells: run.fingerprint?.cells ? Object.keys(run.fingerprint.cells).length : 0,
    // What the run cost, and over which transport: a wall time that blows a
    // caller's budget is a fact about the endpoint, and which dispatcher served
    // it is what makes a slow run diagnosable instead of mysterious.
    elapsed_ms: elapsedMs,
    transport,
  };

  if (result.mode === 'verify') {
    if (!job.reference || typeof job.reference !== 'object') {
      throw new Error('verify mode needs a reference fingerprint');
    }
    // A reference with no overlapping cell is not a comparison: the library
    // throws, and that is the honest answer rather than a fabricated verdict.
    result.comparison = compare(job.reference, run.fingerprint);
  }

  process.stdout.write(JSON.stringify(result));
}

main().catch((error) => {
  process.stdout.write(
    JSON.stringify({
      error: error instanceof Error ? error.message : String(error),
      error_name: error instanceof Error ? error.name : 'Error',
    }),
  );
  process.exitCode = 1;
});

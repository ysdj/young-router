#!/usr/bin/env node
/**
 * Run one TypeScript test file under Node.
 *
 * The test files use the source tree directly.  Source modules import each
 * other without file extensions, which the bundler resolves and Node's type
 * stripping does not.  Build the one entry to a temporary module first, then
 * import it: the test still exercises the same source, and its assertions run
 * as assertions rather than as a second copy of the logic.
 */
import { build } from "esbuild";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const entry = process.argv[2];
if (!entry) {
  console.error("usage: node scripts/run-ts-test.mjs <test-file.mjs>");
  process.exit(2);
}
const directory = await mkdtemp(join(tmpdir(), "young-router-ts-test-"));
const outfile = join(directory, "test.mjs");
try {
  await build({
    entryPoints: [resolve(entry)],
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node22",
    outfile,
    logLevel: "error",
  });
  await import(pathToFileURL(outfile).href);
} finally {
  await rm(directory, { recursive: true, force: true });
}

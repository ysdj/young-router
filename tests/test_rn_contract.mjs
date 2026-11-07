#!/usr/bin/env node
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const rnRoot = path.join(repository, "rn");
const checker = path.join(rnRoot, "scripts", "check-contract.mjs");
const original = fs.readFileSync(path.join(rnRoot, "packages", "shared", "src", "types.ts"), "utf8");
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "young-router-contract-"));

function run(types) {
  const candidate = path.join(temporary, "types.ts");
  fs.writeFileSync(candidate, types, "utf8");
  return spawnSync(process.execPath, [checker], {
    cwd: rnRoot,
    encoding: "utf8",
    env: { ...process.env, YOUNG_ROUTER_CONTRACT_TYPES: candidate },
  });
}

try {
  assert.ok(original.includes('export type ProviderAuthKind =\n'
    + '  | "api_key"\n'
    + '  | "openai_login"\n'
    + '  | "workbuddy_login"\n'
    + '  | "workbuddy_global_login"\n'
    + '  | "qoder_login"\n'
    + '  | "qoder_global_login"\n'
    + '  | "our_free_model_login";'));
  assert.ok(original.includes("auth_status?: ProviderAuthStatus;"));

  const valid = run(original);
  assert.equal(valid.status, 0, valid.stderr);

  const badParams = run(original.replace("snapshot: Record<string, never>;", "snapshot: { stale?: string };"));
  assert.notEqual(badParams.status, 0, "parameter drift must fail the contract check");
  assert.match(badParams.stderr, /IpcParams\.snapshot diverges/u);

  const badResult = run(original.replace("subscribe: { subscription_id: string };", "subscribe: { subscription_id: number };"));
  assert.notEqual(badResult.status, 0, "result drift must fail the contract check");
  assert.match(badResult.stderr, /IpcResults\.subscribe diverges/u);

  // The last member of *this* union, read from its own block rather than
  // pinned to a name: a method added after "import" would otherwise make the
  // replacement a no-op and the drift check would silently stop testing
  // anything.
  const methodBlock = original.slice(
    original.indexOf("export type IpcMethod ="),
    original.indexOf("export type AppRoute ="),
  );
  const lastMethod = [...methodBlock.matchAll(/^  \| "([a-z_]+)";$/gmu)].pop()?.[1];
  assert.ok(lastMethod, "the IpcMethod union must end with a stated member");
  const missingMethod = run(original.replace(`  | "${lastMethod}";`, ";"));
  assert.notEqual(missingMethod.status, 0, "method drift must fail the contract check");
  assert.match(missingMethod.stderr, /IpcMethod diverges/u);
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
}

console.log("RN contract regression tests OK (params, results, and methods)");

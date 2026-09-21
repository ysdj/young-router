#!/usr/bin/env node
/**
 * One-request JSONL worker around the staged upstream TraceOne module.
 *
 * Reads {"id": "...", "text": "<model answer>"} on stdin, runs the upstream
 * classifier from the staged directory (--dir), and prints one result line.
 * Keeping the decision inside the upstream module means a new TraceOne
 * revision only has to be re-staged by scripts/update_traceone.py.
 */
import { readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

function directoryArgument(argv) {
  const index = argv.indexOf("--dir");
  if (index === -1 || index + 1 >= argv.length) {
    throw new Error("--dir <traceone directory> is required");
  }
  return path.resolve(argv[index + 1]);
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

function readRequest(raw) {
  const line = raw.split("\n").find((candidate) => candidate.trim().length > 0);
  if (!line) throw new Error("no request received on stdin");
  const request = JSON.parse(line);
  if (!request || typeof request.text !== "string") {
    throw new Error("request.text must be a string");
  }
  return request;
}

async function readArtifacts(directory) {
  const data = (name) => readFile(path.join(directory, "data", name), "utf8");
  const [bank, adapter, support] = await Promise.all([
    data("unified_bank.json"),
    data("codex_low_v4_adapter_415.json"),
    data("codex_low_v4_support_415.json"),
  ]);
  return { bank: JSON.parse(bank), adapter: JSON.parse(adapter), support: JSON.parse(support) };
}

let requestId = null;
try {
  const directory = directoryArgument(process.argv.slice(2));
  const request = readRequest(await readStdin());
  requestId = request.id ?? null;
  const module = await import(pathToFileURL(path.join(directory, "traceone.js")).href);
  if (typeof module.identifyWithArtifacts !== "function") {
    throw new Error("the staged TraceOne module does not export identifyWithArtifacts");
  }
  const artifacts = await readArtifacts(directory);
  const result = module.identifyWithArtifacts(request.text, artifacts);
  process.stdout.write(`${JSON.stringify({ id: requestId, ok: true, result })}\n`);
} catch (error) {
  process.stdout.write(
    `${JSON.stringify({
      id: requestId,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    })}\n`,
  );
  process.exitCode = 1;
}

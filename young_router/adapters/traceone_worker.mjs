#!/usr/bin/env node
/**
 * One-request JSONL worker around the staged upstream TraceOne module.
 *
 * Reads {"id": "...", "text": "<model answer>"} on stdin, runs the upstream
 * classifier from the staged directory (--dir), and prints one result line.
 * Keeping the decision inside the upstream module means a new TraceOne
 * revision only has to be re-staged by scripts/update_traceone.py.
 */
import { readdir, readFile } from "node:fs/promises";
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

/**
 * Load the classifier documents the staged module reads.
 *
 * A document that declares its own `schema` names its role, so a rename never
 * moves it out of its role; an older document is recognised by the file name
 * spelling it shipped with.  `bank` and `adapter` are required and each has to
 * resolve to exactly one document.  A separate `support` document is optional:
 * the 2026-10 release folded those statistics into the adapter itself, so the
 * engine is complete without one.
 */
async function readArtifacts(directory) {
  const dataDirectory = path.join(directory, "data");
  const names = (await readdir(dataDirectory)).filter((name) => name.endsWith(".json"));
  const documents = await Promise.all(
    names.map(async (name) => {
      const text = await readFile(path.join(dataDirectory, name), "utf8");
      let schema = "";
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed === "object" && typeof parsed.schema === "string") {
          schema = parsed.schema;
        }
      } catch {
        schema = "";
      }
      return { name, text, schema };
    }),
  );
  const roleOf = (document) => {
    if (document.schema === "robust-number-fingerprint-bank") return "bank";
    if (document.schema === "traceone-sequence-adapter-v1") return "adapter";
    if (/unified_bank[^/]*\.json$/.test(document.name)) return "bank";
    if (/_adapter_[^/]*\.json$/.test(document.name)) return "adapter";
    if (/_support_[^/]*\.json$/.test(document.name)) return "support";
    return "";
  };
  const role = (wanted, required) => {
    const matches = documents.filter((document) => roleOf(document) === wanted);
    if (matches.length > 1) {
      throw new Error(`expected exactly one ${wanted} document, found ${matches.length}`);
    }
    if (matches.length === 0) {
      if (required) throw new Error(`expected exactly one ${wanted} document, found 0`);
      return null;
    }
    return matches[0];
  };
  const bank = role("bank", true);
  const adapter = role("adapter", true);
  const support = role("support", false);
  return {
    bank: JSON.parse(bank.text),
    adapter: JSON.parse(adapter.text),
    ...(support ? { support: JSON.parse(support.text) } : {}),
  };
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

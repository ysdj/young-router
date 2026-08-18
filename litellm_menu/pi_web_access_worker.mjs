#!/usr/bin/env node

/**
 * JSONL adapter for pi-web-access.
 *
 * The worker intentionally talks to the extension through Pi's public SDK
 * (DefaultResourceLoader/createAgentSession/extensionRunner) rather than
 * importing pi-web-access internals.  This keeps the adapter compatible with
 * the extension's published package contract.
 *
 * Startup options:
 *   --config-dir <dir>   Directory containing web-search.json
 *   --timeout <seconds>  Per request deadline (default: 60)
 *   --max-results <n>    Maximum search results per query (default: 5)
 *   --entry <path>       pi-web-access/index.ts path (or
 *                        LITELLM_MENU_PI_WEB_ACCESS_ENTRY)
 *
 * LITELLM_MENU_WEB_SEARCH_CONFIG_JSON can be used when the runtime setting is
 * held as JSON rather than a file.  The worker writes that object to
 * <config-dir>/web-search.json before loading the extension.  Secrets should
 * normally be supplied through the runtime config file, not command-line
 * arguments.  `timeoutSeconds`/`timeout` and `maxResults`/`max_results` in
 * that JSON configure the worker deadline/result cap; command-line and
 * environment values take precedence.
 *
 * Each non-empty stdin line is one request object, for example
 * `{ "id": "a1", "action": "search", "query": "..." }`.  Every request
 * produces exactly one stdout JSON line with `id`, `ok`, `text`,
 * `sourceUrls`, and `details` (plus `error` for failures).  Diagnostics go to
 * stderr so stdout remains a machine-readable channel.
 */

import readline from "node:readline";
import process from "node:process";
import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const DEFAULT_TIMEOUT_SECONDS = 60;
const DEFAULT_MAX_RESULTS = 5;
const MAX_PI_RESULTS = 20;
const TOOL_NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;

function argumentValue(name) {
  const prefix = `${name}=`;
  for (let index = 2; index < process.argv.length; index += 1) {
    const argument = process.argv[index];
    if (argument === name) return process.argv[index + 1];
    if (argument.startsWith(prefix)) return argument.slice(prefix.length);
  }
  return undefined;
}

function positiveNumber(value, name, { integer = false } = {}) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0 || (integer && !Number.isInteger(number))) {
    throw new Error(`${name} must be a positive ${integer ? "integer" : "number"}`);
  }
  return number;
}

function boundedInteger(value, name, minimum, maximum) {
  const number = positiveNumber(value, name, { integer: true });
  if (number < minimum || number > maximum) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}`);
  }
  return number;
}

function requestId(request) {
  return request && (typeof request.id === "string" || typeof request.id === "number")
    ? request.id
    : null;
}

function textFromResult(result) {
  return (result?.content ?? [])
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n");
}

function sourceUrls(value) {
  const urls = [];
  const seen = new Set();
  const add = (valueToAdd) => {
    if (typeof valueToAdd !== "string") return;
    const cleaned = valueToAdd.trim().replace(/[),.;:!?]+$/g, "");
    if (!/^https?:\/\//i.test(cleaned) || seen.has(cleaned)) return;
    seen.add(cleaned);
    urls.push(cleaned);
  };
  const visit = (current) => {
    if (typeof current === "string") {
      for (const match of current.matchAll(/https?:\/\/[^\s<>"')\]]+/gi)) add(match[0]);
      return;
    }
    if (Array.isArray(current)) {
      for (const item of current) visit(item);
      return;
    }
    if (!current || typeof current !== "object") return;
    for (const [key, item] of Object.entries(current)) {
      if (/^(?:url|href|sourceUrl|source_url)$/i.test(key) && typeof item === "string") add(item);
      else visit(item);
    }
  };
  visit(value);
  return urls;
}

function payloadFromResult(result, extra = {}) {
  const text = typeof extra.text === "string" ? extra.text : textFromResult(result);
  const resultDetails = result?.details && typeof result.details === "object" ? result.details : {};
  const details = {
    ...resultDetails,
    ...(extra.details && typeof extra.details === "object" ? extra.details : {}),
  };
  const error = typeof details.error === "string" ? details.error : undefined;
  const ok = result?.isError !== true && !error && !/^Error:/i.test(text.trim());
  const payload = {
    ok,
    text,
    sourceUrls: extra.sourceUrls ?? sourceUrls([text, details]),
    details,
  };
  if (!ok && error) payload.error = error;
  return payload;
}

function paginateSearchPayload(payload, page, pageSize) {
  if (page === 1 || !payload.ok || typeof payload.text !== "string") return payload;
  const lines = payload.text.split("\n");
  const sources = [];
  for (let index = 0; index + 1 < lines.length; index += 1) {
    const title = lines[index].match(/^\s*\d+\.\s+(.+?)\s*$/);
    const url = lines[index + 1].match(/^\s+(https?:\/\/\S+)\s*$/i);
    if (!title || !url) continue;
    sources.push({ title: title[1], url: url[1] });
  }
  const start = (page - 1) * pageSize;
  const selected = sources.slice(start, start + pageSize);
  if (selected.length === 0) {
    return {
      ...payload,
      text: "No results found.",
      sourceUrls: [],
      details: { ...payload.details, page, pageSize, totalResults: sources.length },
    };
  }
  const firstSource = lines.findIndex((line, index) => {
    const title = line.match(/^\s*\d+\.\s+(.+?)\s*$/);
    const url = lines[index + 1]?.match(/^\s+(https?:\/\/\S+)\s*$/i);
    return Boolean(title && url);
  });
  const prefix = firstSource >= 0 ? lines.slice(0, firstSource).join("\n").trimEnd() : "";
  const body = selected.map((source, index) => `${index + 1}. ${source.title}\n   ${source.url}`).join("\n\n");
  return {
    ...payload,
    text: prefix ? `${prefix}\n${body}` : body,
    sourceUrls: selected.map((source) => source.url),
    details: { ...payload.details, page, pageSize, totalResults: sources.length },
  };
}

function configToolNames(config) {
  const configured = config?.toolNames;
  if (configured !== undefined && (!configured || typeof configured !== "object" || Array.isArray(configured))) {
    throw new Error("web-search.json toolNames must be an object");
  }
  const names = {
    webSearch: configured?.webSearch ?? "web_search",
    fetchContent: configured?.fetchContent ?? "fetch_content",
    getSearchContent: configured?.getSearchContent ?? "get_search_content",
  };
  for (const [key, value] of Object.entries(names)) {
    if (typeof value !== "string" || !TOOL_NAME_PATTERN.test(value.trim())) {
      throw new Error(`web-search.json toolNames.${key} is invalid`);
    }
    names[key] = value.trim();
  }
  return names;
}

function configDirectory() {
  return resolve(
    argumentValue("--config-dir")
      ?? process.env.LITELLM_MENU_WEB_SEARCH_CONFIG_DIR
      ?? process.env.PI_CODING_AGENT_DIR
      ?? resolve(process.cwd(), ".litellm-runtime", "pi-web-access"),
  );
}

function extensionEntry() {
  const workerDirectory = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    argumentValue("--entry"),
    process.env.LITELLM_MENU_PI_WEB_ACCESS_ENTRY,
    process.env.PI_WEB_ACCESS_ENTRY,
    resolve(workerDirectory, "node_modules", "pi-web-access", "index.ts"),
    // The packaged Core keeps the extension beside this worker.
    resolve(workerDirectory, "pi-web-access", "index.ts"),
    resolve(process.cwd(), "node_modules", "pi-web-access", "index.ts"),
  ].filter((candidate) => typeof candidate === "string" && candidate.length > 0).map((candidate) => resolve(candidate));
  const entry = candidates.find((candidate) => existsSync(candidate));
  if (!entry) {
    throw new Error(
      "pi-web-access/index.ts was not found; set LITELLM_MENU_PI_WEB_ACCESS_ENTRY or pass --entry",
    );
  }
  return entry;
}

async function loadPiSdk(entry) {
  // The build keeps Pi's dependencies below the staged extension directory
  // (for example, <core>/litellm_menu/pi-web-access/node_modules).  Resolve
  // the SDK from that package tree instead of relying on the worker's parent
  // directory or on a global NODE_PATH.
  const packageRequire = createRequire(resolve(dirname(entry), "package.json"));
  let sdkPath;
  try {
    // This works for package layouts that expose a CommonJS-compatible
    // resolver entry.
    sdkPath = packageRequire.resolve("@earendil-works/pi-coding-agent");
  } catch {
    // Pi's published package intentionally exposes only an ESM `import`
    // condition.  `require.resolve` therefore rejects its package exports;
    // use the stable published dist entry from the same staged package tree.
    const packageRoot = resolve(
      dirname(entry),
      "node_modules",
      "@earendil-works",
      "pi-coding-agent",
    );
    const candidate = resolve(packageRoot, "dist", "index.js");
    if (existsSync(candidate)) sdkPath = candidate;
  }
  if (!sdkPath) {
    throw new Error("@earendil-works/pi-coding-agent could not be resolved from the staged pi-web-access package");
  }
  return import(pathToFileURL(sdkPath).href);
}

async function prepareConfig() {
  const directory = configDirectory();
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await chmod(directory, 0o700);
  process.env.PI_CODING_AGENT_DIR = directory;
  const path = resolve(directory, "web-search.json");
  const json = process.env.LITELLM_MENU_WEB_SEARCH_CONFIG_JSON;
  if (json !== undefined) {
    let parsed;
    try {
      parsed = JSON.parse(json);
    } catch (error) {
      throw new Error(`LITELLM_MENU_WEB_SEARCH_CONFIG_JSON is invalid JSON: ${error.message}`);
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("LITELLM_MENU_WEB_SEARCH_CONFIG_JSON must be a JSON object");
    }
    await writeFile(path, `${JSON.stringify(parsed, null, 2)}\n`, { mode: 0o600 });
    await chmod(path, 0o600);
  }

  let config = {};
  try {
    config = JSON.parse(await readFile(path, "utf8"));
  } catch (error) {
    if (error?.code !== "ENOENT") {
      throw new Error(`Unable to read ${path}: ${error.message}`);
    }
  }
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new Error(`${path} must contain a JSON object`);
  }
  return { directory, path, config };
}

async function withDeadline(operation, timeoutMilliseconds) {
  const controller = new AbortController();
  let timeoutHandle;
  const task = Promise.resolve().then(() => operation(controller.signal));
  const deadline = new Promise((_, reject) => {
    timeoutHandle = setTimeout(() => {
      controller.abort();
      reject(new Error(`pi-web-access request timed out after ${timeoutMilliseconds} ms`));
    }, timeoutMilliseconds);
  });
  try {
    return await Promise.race([task, deadline]);
  } finally {
    clearTimeout(timeoutHandle);
  }
}

function normalizeSearchRequest(request, maxResults) {
  const query = typeof request.query === "string" ? request.query.trim() : "";
  const rawQueries = Array.isArray(request.queries) ? request.queries : undefined;
  const queries = rawQueries && rawQueries.length > 0
    ? rawQueries.map((item) => typeof item === "string" ? item.trim() : item)
    : undefined;
  if (!query && (!queries || queries.length === 0)) {
    throw new Error("search requires query or queries");
  }
  if (queries && queries.some((item) => typeof item !== "string" || item.length === 0)) {
    throw new Error("queries must contain non-empty strings");
  }
  const page = request.page === undefined ? 1 : boundedInteger(request.page, "page", 1, Number.MAX_SAFE_INTEGER);
  const requestedResults = request.numResults ?? request.maxResults ?? maxResults;
  const numResults = boundedInteger(requestedResults, "numResults", 1, MAX_PI_RESULTS);
  const params = {
    numResults: Math.min(MAX_PI_RESULTS, numResults * page, maxResults * page),
    workflow: "none",
  };
  if (queries) params.queries = queries;
  else params.query = query;
  for (const key of ["includeContent", "recencyFilter", "domainFilter", "provider"]) {
    if (request[key] !== undefined) params[key] = request[key];
  }
  return { params, page, pageSize: numResults };
}

async function createWorker() {
  const { directory, path, config } = await prepareConfig();
  const entry = extensionEntry();
  const {
    createAgentSession,
    DefaultResourceLoader,
    SessionManager,
  } = await loadPiSdk(entry);
  const names = configToolNames(config);
  const timeoutRaw = argumentValue("--timeout")
    ?? process.env.LITELLM_MENU_WEB_SEARCH_TIMEOUT
    ?? process.env.LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS
    ?? config.timeoutSeconds
    ?? config.timeout
    ?? DEFAULT_TIMEOUT_SECONDS;
  const timeoutSeconds = positiveNumber(timeoutRaw, "timeout");
  const maxResultsRaw = argumentValue("--max-results")
    ?? process.env.LITELLM_MENU_WEB_SEARCH_MAX_RESULTS
    ?? config.maxResults
    ?? config.max_results
    ?? DEFAULT_MAX_RESULTS;
  const maxResults = boundedInteger(maxResultsRaw, "max-results", 1, MAX_PI_RESULTS);

  const loader = new DefaultResourceLoader({
    cwd: process.cwd(),
    agentDir: directory,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    additionalExtensionPaths: [entry],
  });
  await loader.reload();
  const extensionErrors = loader.getExtensions().errors;
  if (extensionErrors.length > 0) {
    throw new Error(extensionErrors.map((error) => `${error.path}: ${error.error}`).join("; "));
  }
  const { session } = await createAgentSession({
    cwd: process.cwd(),
    agentDir: directory,
    resourceLoader: loader,
    sessionManager: SessionManager.inMemory(),
    noTools: "builtin",
  });
  const runner = session.extensionRunner;
  const getTool = (name) => {
    const tool = runner.getToolDefinition(name);
    if (!tool) {
      throw new Error(`pi-web-access tool ${name} is not registered; check web-search.json tools/toolNames`);
    }
    return tool;
  };
  for (const name of new Set([names.webSearch, names.fetchContent, names.getSearchContent])) {
    getTool(name);
  }
  const execute = (name, params, signal) => getTool(name).execute(
    `litellm-menu-${Date.now()}`,
    params,
    signal,
    undefined,
    runner.createContext(),
  );

  const handle = async (request, signal) => {
    const action = request?.action;
    if (action === "search") {
      const normalized = normalizeSearchRequest(request, maxResults);
      const result = await execute(names.webSearch, normalized.params, signal);
      return paginateSearchPayload(payloadFromResult(result), normalized.page, normalized.pageSize);
    }
    if (action === "openPage") {
      if (typeof request.url !== "string" || request.url.trim() === "") {
        throw new Error("openPage requires url");
      }
      const url = request.url.trim();
      const result = await execute(names.fetchContent, { url, mode: "readable" }, signal);
      const payload = payloadFromResult(result, { sourceUrls: [url] });
      if (payload.ok) payload.text = `Retrieved page content for URL: ${url}\n\n${payload.text}`;
      return payload;
    }
    if (action === "findInPage") {
      if (typeof request.url !== "string" || request.url.trim() === "") {
        throw new Error("findInPage requires url");
      }
      const pattern = typeof request.pattern === "string" ? request.pattern.trim() : "";
      if (!pattern) throw new Error("findInPage requires pattern");
      if (pattern.length > 500) throw new Error("pattern exceeds pi-web-access maximum of 500 characters");
      const url = request.url.trim();
      const fetched = await execute(names.fetchContent, { url, mode: "readable" }, signal);
      const fetchedPayload = payloadFromResult(fetched, { sourceUrls: [url] });
      if (!fetchedPayload.ok) return fetchedPayload;
      const responseId = fetched?.details?.responseId;
      if (!responseId) throw new Error("pi-web-access fetch_content did not return responseId; get_search_content cannot run");
      const found = await execute(names.getSearchContent, {
        responseId,
        urlIndex: 0,
        findText: pattern,
        findMode: request.findMode ?? "case-insensitive",
      }, signal);
      const foundPayload = payloadFromResult(found, {
        sourceUrls: [url],
        details: { fetched: fetched.details ?? {} },
      });
      if (!foundPayload.ok) return foundPayload;
      const foundDetails = found?.details && typeof found.details === "object" ? found.details : {};
      const matchCount = Number(foundDetails.matchCount ?? foundDetails.queryResults?.[0]?.matchCount ?? 0);
      if (!Number.isFinite(matchCount) || matchCount <= 0) {
        return payloadFromResult(found, {
          text: `Page text matches for pattern: ${pattern}\nURL: ${url}\n\nNo readable matches for pattern ${JSON.stringify(pattern)}.`,
          sourceUrls: [url],
          details: { fetched: fetched.details ?? {} },
        });
      }
      const foundText = textFromResult(found);
      const separator = foundText.indexOf("\n\n");
      const body = separator >= 0 ? foundText.slice(separator + 2) : foundText;
      return payloadFromResult(found, {
        text: `Page text matches for pattern: ${pattern}\nURL: ${url}\n\n${body}`,
        sourceUrls: [url],
        details: { fetched: fetched.details ?? {} },
      });
    }
    throw new Error(`unknown action: ${String(action)}`);
  };

  return {
    timeoutMilliseconds: timeoutSeconds * 1000,
    handle,
    dispose: () => session.dispose(),
    configPath: path,
    entry,
  };
}

async function main() {
  const worker = await createWorker();
  try {
    const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    for await (const line of input) {
      if (!line.trim()) continue;
      let request;
      try {
        request = JSON.parse(line);
        const payload = await withDeadline(
          (signal) => worker.handle(request, signal),
          worker.timeoutMilliseconds,
        );
        process.stdout.write(`${JSON.stringify({ id: requestId(request), ...payload })}\n`);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        process.stdout.write(`${JSON.stringify({ id: requestId(request), ok: false, error: message, text: `Error: ${message}`, sourceUrls: [], details: {} })}\n`);
      }
    }
  } finally {
    worker.dispose();
  }
}

main().catch((error) => {
  console.error(error?.stack ?? String(error));
  process.exitCode = 1;
});

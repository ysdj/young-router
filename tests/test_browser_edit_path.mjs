// One real edit in the browser page, measured the way a person feels it.
//
// Run by tests/test_browser_edit_path.py against a live Core holding a
// realistic document.  The question this answers is not "did the page
// respond" -- a screenshot answers that -- but "what did the page ask Core
// for, and when did the control the user touched show the answer".
//
// The assertions are the ones the reported defect failed:
//
//  * A settled window applies one control by asking for a small plan, one
//    dispatch and one apply.  A full projection in that window is every
//    settings document over the wire, which is what made a toggle take
//    seconds on a published page and a millisecond on a loopback host.
//  * The control shows the chosen value inside a frame budget a person reads
//    as instant, not after the whole commit has round-tripped.
//
// Usage: node test_browser_edit_path.mjs <page-url> [--json]
// Prints one JSON object on stdout; exit code 0 means every assertion held.

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const pageUrl = process.argv[2];
const jsonOnly = process.argv.includes("--json");
// Optional link shaping, so a local run states the same defect a published page
// shows: `--latency 250 --downstream 125000` is the round trip and the bandwidth
// measured between a home connection and a gateway in another country.
function numericFlag(name) {
  const index = process.argv.indexOf(name);
  if (index < 0) return undefined;
  const value = Number(process.argv[index + 1]);
  return Number.isFinite(value) ? value : undefined;
}
const latencyMs = numericFlag("--latency");
const downstream = numericFlag("--downstream");
const result = { ok: false, reasons: [], measured: {} };

function chromePath() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const candidates = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  ];
  return candidates.find((candidate) => existsSync(candidate));
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    socket.addEventListener("message", (message) => {
      const payload = JSON.parse(message.data);
      if (payload.id !== undefined) {
        const waiter = this.pending.get(payload.id);
        if (!waiter) return;
        this.pending.delete(payload.id);
        if (payload.error) waiter.reject(new Error(JSON.stringify(payload.error)));
        else waiter.resolve(payload.result);
        return;
      }
      for (const listener of this.listeners) listener(payload.method, payload.params);
    });
  }

  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  on(listener) {
    this.listeners.add(listener);
  }
}

async function openTarget(port, url, shaping) {
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  const cdp = new Cdp(socket);
  await cdp.send("Page.enable");
  await cdp.send("Network.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 950, deviceScaleFactor: 1, mobile: false });
  if (shaping) {
    await cdp.send("Network.emulateNetworkConditions", {
      offline: false,
      latency: shaping.latency,
      downloadThroughput: shaping.downstream,
      uploadThroughput: shaping.downstream,
    });
  }
  await cdp.send("Page.navigate", { url });
  return cdp;
}

async function evaluate(cdp, expression) {
  const answer = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (answer.exceptionDetails) throw new Error(JSON.stringify(answer.exceptionDetails));
  return answer.result.value;
}

const ENABLE_STATE = `(() => {
  const boxes = [...document.querySelectorAll('[role="checkbox"]')];
  const box = boxes.find((node) => (node.textContent || "").trim().endsWith("启用") && node.getAttribute("aria-disabled") !== "true");
  if (!box) return null;
  const rect = box.getBoundingClientRect();
  return { checked: box.getAttribute("aria-checked"), x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
})()`;

const STRIP = `(() => document.body.innerText.split("\\n").filter((line) => /就绪|正在保存|已保存|生效|失败|错误/.test(line)).slice(-1)[0] || "")()`;

async function clickAt(cdp, x, y) {
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none", buttons: 0 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
}

/**
 * Clicks the provider's enable box and reports what the page asked Core for,
 * what it downloaded, and how long the box took to show the new value.
 */
async function editOnce(cdp, label, settleMs) {
  const before = await evaluate(cdp, ENABLE_STATE);
  if (!before) throw new Error("no enable checkbox on screen");

  const traffic = [];
  const inFlight = new Map();
  let applied = false;
  const listener = (method, params) => {
    if (method === "Network.requestWillBeSent" && params.request.method === "POST" && /\/ipc(\?|$)/.test(params.request.url)) {
      let body = {};
      try {
        body = JSON.parse(params.request.postData || "{}");
      } catch {
        body = {};
      }
      const entry = { method: body.method, at: Date.now(), bytes: 0 };
      inFlight.set(params.requestId, entry);
      traffic.push(entry);
    }
    if (method === "Network.loadingFinished" && inFlight.has(params.requestId)) {
      const finished = inFlight.get(params.requestId);
      finished.bytes = params.encodedDataLength;
      finished.done = Date.now();
      if (finished.method === "apply" && !finished.failed) applied = true;
    }
    if (method === "Network.loadingFailed" && inFlight.has(params.requestId)) {
      inFlight.get(params.requestId).failed = params.errorText;
      inFlight.get(params.requestId).done = Date.now();
    }
  };
  cdp.on(listener);
  const started = Date.now();
  await clickAt(cdp, before.x, before.y);
  let painted;
  let strip = "";
  while (Date.now() - started < settleMs) {
    await sleep(40);
    const state = await evaluate(cdp, ENABLE_STATE);
    if (painted === undefined && state && state.checked !== before.checked) painted = Date.now() - started;
    strip = await evaluate(cdp, STRIP);
    // The strip still reads 已保存 from the edit before this one for a moment,
    // so the answer this measurement is about is the one whose Apply landed.
    if (applied && painted !== undefined && /已保存|失败|错误/.test(strip)) break;
  }
  await sleep(400);
  return {
    label,
    from: before.checked,
    paintedMs: painted ?? null,
    strip,
    calls: traffic.map((entry) => ({ method: entry.method, bytes: entry.bytes, failed: entry.failed })),
    bytes: traffic.reduce((total, entry) => total + (entry.bytes || 0), 0),
    methods: traffic.map((entry) => entry.method),
  };
}

async function main() {
  if (!pageUrl) throw new Error("usage: test_browser_edit_path.mjs <page-url>");
  const chrome = chromePath();
  if (!chrome) throw new Error("no Chrome or Chromium binary found (set CHROME_PATH)");
  const profile = await mkdtemp(path.join(tmpdir(), "yr-page-test-"));
  const port = 9400 + Math.floor(Math.random() * 400);
  const browser = spawn(chrome, [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "--disable-extensions",
    "about:blank",
  ], { stdio: "ignore" });

  let cdp;
  try {
    let ready = false;
    for (let attempt = 0; attempt < 60 && !ready; attempt += 1) {
      try {
        await fetch(`http://127.0.0.1:${port}/json/version`);
        ready = true;
      } catch {
        await sleep(200);
      }
    }
    if (!ready) throw new Error("the browser did not open its debugging port");

    cdp = await openTarget(port, pageUrl, latencyMs === undefined ? undefined : { latency: latencyMs, downstream: downstream ?? -1 });
    // The shell reads its first state before it can draw a table.
    for (let attempt = 0; attempt < 200; attempt += 1) {
      const state = await evaluate(cdp, ENABLE_STATE);
      if (state) break;
      await sleep(100);
    }
    if (!(await evaluate(cdp, ENABLE_STATE))) throw new Error("the page never drew an enable checkbox");

    // One warm-up edit each way: the first action of a document is the one
    // allowed to read a projection, because the channel has not spoken yet.
    await editOnce(cdp, "warm-up off", 6000);
    await sleep(1200);
    await editOnce(cdp, "warm-up on", 6000);
    await sleep(1200);

    const measured = await editOnce(cdp, "measured", 8000);
    result.measured = measured;

    if (measured.methods.includes("snapshot")) {
      result.reasons.push(`a settled edit read the whole projection (snapshot among ${measured.methods.join(", ")})`);
    }
    if (measured.bytes > 16 * 1024) {
      result.reasons.push(`a settled edit downloaded ${measured.bytes} bytes of /ipc answers`);
    }
    if (measured.paintedMs === null) {
      result.reasons.push("the checkbox never showed the new value");
    } else if (measured.paintedMs > 400) {
      result.reasons.push(`the checkbox showed the new value after ${measured.paintedMs} ms`);
    }
    if (!/已保存/.test(measured.strip)) {
      result.reasons.push(`the status strip never stated the save (last: ${measured.strip})`);
    }
    result.ok = result.reasons.length === 0;
  } finally {
    if (cdp) await cdp.send("Page.close").catch(() => undefined);
    browser.kill("SIGKILL");
    await rm(profile, { recursive: true, force: true }).catch(() => undefined);
  }
}

main()
  .then(() => {
    if (!jsonOnly) console.log(JSON.stringify(result, null, 2));
    else console.log(JSON.stringify(result));
    process.exit(result.ok ? 0 : 1);
  })
  .catch((error) => {
    result.reasons.push(String(error && error.message ? error.message : error));
    console.log(JSON.stringify(result));
    process.exit(1);
  });

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

import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const pageUrl = process.argv[2];
const jsonOnly = process.argv.includes("--json");
const resizeColumnsIndex = process.argv.indexOf("--resize-columns");
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
  if (resizeColumnsIndex >= 0) {
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
      source: `Object.defineProperty(navigator, "languages", { get: () => ["zh-CN"] });`,
    });
  }
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
 * what it downloaded, how long the box took to show the new value, and whether
 * the box ever went back to the value it started from.
 *
 * That last fact is the one a link slower than a control's own patience
 * produced: the press painted, then the control's fixed two-second budget ran
 * out and it fell back to the old value, and the state only arrived after.  A
 * person reads that as a switch that will not stay where it was put, so the
 * driver samples the box for the whole window instead of only looking for the
 * first paint.
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
  let returned = false;
  let settled = false;
  let strip = "";
  while (Date.now() - started < settleMs) {
    await sleep(40);
    const state = await evaluate(cdp, ENABLE_STATE);
    // The strip as the previous sample read it: a press that goes back to its
    // old value while no outcome has been stated is the control arguing with a
    // write nobody has lost yet.  After 失败/错误 the old value is the honest
    // answer and is not counted.
    const stated = /已保存|失败|错误/.test(strip);
    strip = await evaluate(cdp, STRIP);
    if (!state) continue;
    if (painted === undefined && state.checked !== before.checked) painted = Date.now() - started;
    if (painted !== undefined && state.checked === before.checked && !stated) returned = true;
    if (stated && state.checked !== before.checked) settled = true;
    // The strip still reads 已保存 from the edit before this one for a moment,
    // so the answer this measurement is about is the one whose Apply landed.
    if (applied && painted !== undefined && settled && stated) break;
  }
  await sleep(400);
  return {
    label,
    from: before.checked,
    paintedMs: painted ?? null,
    // True when the box went back to the value it started from after showing
    // the press: the one thing a control must not do while a write is open.
    returned,
    strip,
    calls: traffic.map((entry) => ({ method: entry.method, bytes: entry.bytes, failed: entry.failed })),
    bytes: traffic.reduce((total, entry) => total + (entry.bytes || 0), 0),
    methods: traffic.map((entry) => entry.method),
  };
}

/**
 * Waits for the route inspector's enable box to be on screen.
 *
 * The shell reads its first state before it can draw a table, so a page opened
 * on a shaped link answers this in seconds rather than a frame.
 */
async function waitForEnableBox(cdp) {
  for (let attempt = 0; attempt < 300; attempt += 1) {
    const state = await evaluate(cdp, ENABLE_STATE);
    if (state) return state;
    await sleep(100);
  }
  throw new Error("the page never drew an enable checkbox");
}

async function columnGeometry(cdp, label, rowMarker) {
  return evaluate(cdp, `(() => {
    const handle = [...document.querySelectorAll('[role="separator"]')]
      .find((node) => node.getAttribute("aria-label") === ${JSON.stringify(label)});
    if (!handle) return null;
    const header = handle.parentElement;
    const index = [...header.parentElement.children].indexOf(header);
    const row = [...document.querySelectorAll('button[aria-label]')]
      .find((node) => node.getAttribute("aria-label").includes(${JSON.stringify(rowMarker)}));
    const cell = row?.lastElementChild?.children[index];
    if (!cell) return null;
    const grip = handle.getBoundingClientRect();
    const heading = header.getBoundingClientRect();
    const value = cell.getBoundingClientRect();
    return { x: grip.x, y: grip.y, height: grip.height, right: grip.right,
      headerLeft: heading.left, headerRight: heading.right, headerWidth: heading.width,
      cellLeft: value.left, cellRight: value.right, cellWidth: value.width,
      cellTop: value.top, cellHeight: value.height, selected: row.getAttribute("aria-selected") === "true" };
  })()`);
}

async function waitForColumn(cdp, label, rowMarker) {
  for (let attempt = 0; attempt < 150; attempt += 1) {
    const geometry = await columnGeometry(cdp, label, rowMarker);
    if (geometry) return geometry;
    await sleep(100);
  }
  throw new Error(`column ${label} or row ${rowMarker} did not appear`);
}

async function waitForStableColumn(cdp, label, rowMarker) {
  let previous;
  let stable = 0;
  for (let attempt = 0; attempt < 150; attempt += 1) {
    const geometry = await columnGeometry(cdp, label, rowMarker);
    stable = geometry && previous && geometry.headerWidth === previous.headerWidth ? stable + 1 : 0;
    if (stable >= 3) return geometry;
    previous = geometry;
    await sleep(100);
  }
  throw new Error(`column ${label} did not settle`);
}

function assertColumnAligned(geometry) {
  assert.ok(Math.abs(geometry.right - geometry.headerRight) < 1, "resize target is at the column edge");
  assert.ok(Math.abs(geometry.headerWidth - geometry.cellWidth) < 1, "header and row widths match");
  assert.ok(Math.abs(geometry.headerLeft - geometry.cellLeft) < 1, "header and row positions match");
}

async function dragColumn(cdp, geometry, distance) {
  const x = geometry.headerRight - 3;
  const y = geometry.y + geometry.height / 2;
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none", buttons: 0 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x + distance, y, button: "left", buttons: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: x + distance, y, button: "left", buttons: 0, clickCount: 1 });
  await sleep(150);
}

async function checkColumnResizing(cdp) {
  let first = await waitForColumn(cdp, "本地时间", "public-0-0");
  assertColumnAligned(first);
  await dragColumn(cdp, first, 70);
  const widened = await waitForColumn(cdp, "本地时间", "public-0-0");
  assertColumnAligned(widened);
  assert.equal(widened.headerWidth, first.headerWidth + 70);

  const rowPoint = { x: widened.cellLeft + 10, y: widened.cellTop + widened.cellHeight / 2 };
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", ...rowPoint, button: "left", buttons: 1, clickCount: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...rowPoint, button: "left", buttons: 0, clickCount: 1 });
  await sleep(150);
  const selected = await waitForColumn(cdp, "本地时间", "public-0-0");
  assert.ok(selected.selected, "the row is selected");
  assertColumnAligned(selected);

  const python = process.argv[resizeColumnsIndex + 1];
  const logPath = process.argv[resizeColumnsIndex + 2];
  execFileSync(python, ["-c", `import json, sys
from young_router import runtime_store
runtime_store.append_record(sys.argv[1], json.dumps({
    "timestamp": "2026-11-21T10:00:00Z", "request_id": "second",
    "status": "success", "model_group": "public-0-0",
    "usage": {"input_tokens": 1000, "output_tokens": 50, "cache_read_tokens": 500},
}))`, logPath]);
  await waitForColumn(cdp, "缓存命中", "public-0-0");
  await sleep(200);
  const afterUpdate = await waitForColumn(cdp, "本地时间", "public-0-0");
  assertColumnAligned(afterUpdate);
  assert.equal(afterUpdate.headerWidth, widened.headerWidth, "a new optional column keeps the existing width");

  await dragColumn(cdp, afterUpdate, -300);
  const narrowed = await waitForColumn(cdp, "本地时间", "public-0-0");
  assertColumnAligned(narrowed);
  assert.equal(narrowed.headerWidth, 24);
  await dragColumn(cdp, narrowed, 80);
  const restored = await waitForColumn(cdp, "本地时间", "public-0-0");
  assertColumnAligned(restored);
  assert.equal(restored.headerWidth, 104);

  await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });
  const touch = { x: restored.headerRight - 3, y: restored.y + restored.height / 2 };
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [touch] });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ ...touch, x: touch.x + 40 }] });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: false });
  const touched = await waitForColumn(cdp, "本地时间", "public-0-0");
  assertColumnAligned(touched);
  assert.equal(touched.headerWidth, 144);

  const scrollBox = await evaluate(cdp, `(() => {
    let node = [...document.querySelectorAll('[role="separator"]')]
      .find((item) => item.getAttribute("aria-label") === "本地时间");
    while (node && !(getComputedStyle(node).overflowX === "auto" && node.scrollWidth > node.clientWidth)) {
      node = node.parentElement;
    }
    if (!node) return null;
    const bounds = node.getBoundingClientRect();
    return { x: bounds.left + 100, y: bounds.top + 15 };
  })()`);
  assert.ok(scrollBox, "the widened table can scroll horizontally");
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseWheel", ...scrollBox, deltaX: 2000, deltaY: 0 });
  await sleep(200);
  const detail = await waitForColumn(cdp, "详情", "public-0-0");
  assertColumnAligned(detail);
  await dragColumn(cdp, detail, -60);
  const scrolled = await waitForColumn(cdp, "详情", "public-0-0");
  assertColumnAligned(scrolled);
  assert.equal(scrolled.headerWidth, detail.headerWidth - 60);

  await cdp.send("Page.navigate", { url: new URL("#/providers-models", pageUrl).href });
  for (let attempt = 0; attempt < 150; attempt += 1) {
    if (await evaluate(cdp, `document.body.innerText.includes("添加向导")`)) break;
    await sleep(100);
  }
  first = await waitForStableColumn(cdp, "上游模型", "upstream-0-0");
  assertColumnAligned(first);
  await dragColumn(cdp, first, -60);
  const model = await waitForColumn(cdp, "上游模型", "upstream-0-0");
  assertColumnAligned(model);
  assert.equal(model.headerWidth, first.headerWidth - 60, JSON.stringify({ first, model }));
  return { logWidth: widened.headerWidth, minimumWidth: narrowed.headerWidth, modelWidth: model.headerWidth };
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

    if (resizeColumnsIndex >= 0) {
      cdp = await openTarget(port, new URL("#/logs", pageUrl).href);
      result.measured = await checkColumnResizing(cdp);
      result.ok = true;
      return;
    }

    // The slow-link press first, and alone: two windows on one Core share its
    // drafts, so a second page sitting open would have its own auto-apply
    // consume this page's staged edit and report a write this measurement
    // never saw.  One page at a time, then.
    const slowCdp = await openTarget(port, pageUrl, { latency: 2500, downstream: 250000 });
    await waitForEnableBox(slowCdp);
    const slow = await editOnce(slowCdp, "slow link", 45000);
    await slowCdp.send("Page.close").catch(() => undefined);

    cdp = await openTarget(port, pageUrl, latencyMs === undefined ? undefined : { latency: latencyMs, downstream: downstream ?? -1 });
    await waitForEnableBox(cdp);

    // One warm-up edit each way: the first action of a document is the one
    // allowed to read a projection, because the channel has not spoken yet.
    await editOnce(cdp, "warm-up off", 6000);
    await sleep(1200);
    await editOnce(cdp, "warm-up on", 6000);
    await sleep(1200);

    const measured = await editOnce(cdp, "measured", 8000);
    result.measured = { ...measured, slowLink: slow };

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
    if (measured.returned) {
      result.reasons.push("the press went back to its old value before any answer arrived");
    }
    if (slow.paintedMs === null) {
      result.reasons.push("the press on a slow link never showed the chosen value");
    }
    if (slow.returned) {
      result.reasons.push(`the press on a slow link went back to its old value after ${slow.paintedMs} ms, before its write answered`);
    }
    if (!/已保存/.test(slow.strip)) {
      result.reasons.push(`the slow link's edit never stated the save (last: ${slow.strip}, calls: ${slow.methods.join(", ")})`);
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

// One Core call that is never answered, and the presses behind it.
//
// The reported defect was not a wrong value: every checkbox in the published
// page stopped working at once, while the pane still read 就绪 and every
// control still looked pressable.  The page serializes its edits on one queue,
// and a `fetch` that nobody aborts stays pending for as long as the connection
// does -- a gateway replaces the proxy on every Apply, and a POST in flight
// while that happens is answered by nobody.  Every later press queued behind
// that promise and never reached Core.
//
// So this drives the real page and hangs exactly one dispatch the way a lost
// connection hangs: the request is open and only the page's own deadline can
// end it.  What it asserts is the pair of facts that defect failed:
//
//   * the timed-out press is reported in the strip and its echo is withdrawn,
//     instead of the page silently holding a queue nobody can see;
//   * the next press still reaches Core -- the queue moved on.
//
// Usage: node test_browser_hung_call.mjs <page-url> [--json]
// Prints one JSON object on stdout; exit code 0 means every assertion held.

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const pageUrl = process.argv[2];
const jsonOnly = process.argv.includes("--json");
const result = { ok: false, reasons: [], measured: {} };

// The page gives up after 20 s; this budget leaves room for the click, the
// deadline, and the read that follows it on a loaded machine.
const TIMEOUT_BUDGET_MS = 45_000;

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

async function evaluate(cdp, expression) {
  const answer = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (answer.exceptionDetails) throw new Error(JSON.stringify(answer.exceptionDetails));
  return answer.result.value;
}

const CHECKBOX = `(label) => {
  const match = [...document.querySelectorAll('[role="checkbox"]')]
    .find((node) => (node.textContent || "").trim().endsWith(label) && node.getAttribute("aria-disabled") !== "true");
  if (!match) return null;
  const rect = match.getBoundingClientRect();
  return { checked: match.getAttribute("aria-checked"), x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
}`;

const ENABLE_STATE = `(() => (${CHECKBOX})("启用"))()`;
const STRIP = `(() => document.body.innerText.split("\\n").filter((line) => /就绪|保存|回应|失败|错误/.test(line)).slice(-1)[0] || "")()`;

async function clickAt(cdp, x, y) {
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none", buttons: 0 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
}

/**
 * Hang the next dispatch the way a connection with no answer does.
 *
 * The real page's `fetch` would observe the abort its own deadline sends, so
 * the stand-in rejects on `signal` and on nothing else: a page that never
 * cancelled its request would wait here forever, which is the defect.
 */
const HANG_NEXT_DISPATCH = `(() => {
  window.__youngRouterHangNext = true;
  const original = window.fetch;
  window.fetch = function (input, init) {
    const body = init && init.body;
    const dispatch = typeof body === "string" && body.includes('"dispatch"');
    if (window.__youngRouterHangNext && dispatch) {
      window.__youngRouterHangNext = false;
      return new Promise(function (_resolve, reject) {
        const signal = init && init.signal;
        if (!signal) return;
        signal.addEventListener("abort", function () {
          const failure = new Error("aborted");
          failure.name = "AbortError";
          reject(failure);
        });
      });
    }
    return original.apply(this, arguments);
  };
  return true;
})()`;

async function main() {
  if (!pageUrl) throw new Error("usage: test_browser_hung_call.mjs <page-url>");
  const chrome = chromePath();
  if (!chrome) throw new Error("no Chrome or Chromium binary found (set CHROME_PATH)");
  const profile = await mkdtemp(path.join(tmpdir(), "yr-hung-call-"));
  const port = 9800 + Math.floor(Math.random() * 150);
  const browser = spawn(chrome, [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "--disable-extensions",
    // A published page is reached under a name that is not loopback; a manual
    // run maps it here, exactly as the browser that reported the defect did.
    ...(process.env.YR_CHROME_ARGS ? JSON.parse(process.env.YR_CHROME_ARGS) : []),
    "about:blank",
  ], { stdio: "ignore" });

  let cdp;
  try {
    let ready = false;
    for (let attempt = 0; attempt < 80 && !ready; attempt += 1) {
      try {
        await fetch(`http://127.0.0.1:${port}/json/version`);
        ready = true;
      } catch {
        await sleep(200);
      }
    }
    if (!ready) throw new Error("the browser did not open its debugging port");

    const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    cdp = new Cdp(socket);
    await cdp.send("Page.enable");
    await cdp.send("Network.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 950, deviceScaleFactor: 1, mobile: false });
    await cdp.send("Page.navigate", { url: pageUrl });

    // The shell reads its first state before it can draw a table; the routes
    // tab is where the enable checkbox lives.
    const ROUTES_TAB = `(() => {
      const tab = [...document.querySelectorAll("*")].filter((node) => node.children.length === 0 && (node.textContent || "").trim() === "路由").at(-1);
      if (!tab) return null;
      const rect = tab.getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    })()`;
    let tab;
    for (let attempt = 0; attempt < 200; attempt += 1) {
      tab = await evaluate(cdp, ROUTES_TAB);
      if (tab) break;
      await sleep(100);
    }
    if (!tab) throw new Error("the page never drew its routes tab");
    await clickAt(cdp, tab.x, tab.y);
    await sleep(1500);

    let state = await evaluate(cdp, ENABLE_STATE);
    for (let attempt = 0; attempt < 100 && !state; attempt += 1) {
      await sleep(100);
      state = await evaluate(cdp, ENABLE_STATE);
    }
    if (!state) throw new Error("the route inspector never drew an enable checkbox");

    const dispatchRequests = [];
    cdp.on((method, params) => {
      if (method !== "Network.requestWillBeSent" || params.request.method !== "POST" || !/\/ipc(\?|$)/.test(params.request.url)) return;
      let body = {};
      try {
        body = JSON.parse(params.request.postData || "{}");
      } catch {
        body = {};
      }
      if (body.method === "dispatch") dispatchRequests.push(Date.now());
    });

    await evaluate(cdp, HANG_NEXT_DISPATCH);
    const before = state.checked;
    const started = Date.now();
    await clickAt(cdp, state.x, state.y);

    // The press itself must be reported: a deadline that fires in silence is
    // the same dead control with a shorter queue behind it.
    let strip = "";
    let withdrew = false;
    while (Date.now() - started < TIMEOUT_BUDGET_MS) {
      await sleep(250);
      strip = await evaluate(cdp, STRIP);
      const now = await evaluate(cdp, ENABLE_STATE);
      if (now && now.checked === before) withdrew = true;
      if (/回应/.test(strip) && withdrew) break;
    }
    result.measured.timeoutShownMs = Date.now() - started;
    result.measured.strip = strip;
    result.measured.echoWithdrawn = withdrew;
    if (!/回应/.test(strip)) result.reasons.push(`the timed-out press was never reported (last strip: ${strip})`);
    if (!withdrew) result.reasons.push("the timed-out press kept showing the value it never wrote");

    // The next press is the whole point: the queue has to move again.
    const sentBefore = dispatchRequests.length;
    const again = await evaluate(cdp, ENABLE_STATE);
    if (!again) throw new Error("the enable checkbox disappeared after the deadline");
    const nextStarted = Date.now();
    await clickAt(cdp, again.x, again.y);
    let sent = false;
    while (Date.now() - nextStarted < 15_000) {
      await sleep(250);
      if (dispatchRequests.length > sentBefore) {
        sent = true;
        break;
      }
    }
    result.measured.nextPressMs = Date.now() - nextStarted;
    if (!sent) result.reasons.push("the press after the deadline never reached Core: the queue stayed wedged");
    result.ok = result.reasons.length === 0;
  } finally {
    if (cdp) await cdp.send("Page.close").catch(() => undefined);
    browser.kill("SIGKILL");
    await rm(profile, { recursive: true, force: true }).catch(() => undefined);
  }
}

main()
  .then(() => {
    console.log(jsonOnly ? JSON.stringify(result) : JSON.stringify(result, null, 2));
    process.exit(result.ok ? 0 : 1);
  })
  .catch((error) => {
    result.reasons.push(String(error && error.message ? error.message : error));
    console.log(JSON.stringify(result));
    process.exit(1);
  });

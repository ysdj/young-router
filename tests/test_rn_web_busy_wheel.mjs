// How big the page's busy wheel actually is, measured in a browser.
//
// The reported defect was a wait icon that drew larger than its own slot: the
// 中转站关联 account row painted a 56 pt box with a ring inside it where a 24 pt
// control belongs.  Nothing in this repository could fail on it -- `tsc` passes
// with any component inside the slot, the page builds, and a reader of the
// source sees `width: 12` on the container and believes it.
//
// React Native for Web's `ActivityIndicator` is what drew it: a 20 pt system
// ring inside a 12 pt container, so the ring painted 8 pt past the slot the
// button reserved and past every row height that slot decided.  The page now
// draws the app's own wheel (`AppKitBusySpinner`'s twelve graded spokes), and
// this measures the two facts a reader cannot see in the source: the wheel is
// exactly the reserved 12 pt with nothing larger inside it, and a control that
// is working keeps the box it had while idle.
//
// Usage: node test_rn_web_busy_wheel.mjs [--json]
// Prints one JSON object on stdout; exit code 0 means every assertion held.

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const ROOT = path.resolve(import.meta.dirname, "..");
const SHARED = path.join(ROOT, "rn/packages/shared/src");
const WEB_APP = path.join(ROOT, "rn/apps/web");
const jsonOnly = process.argv.includes("--json");
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
    socket.addEventListener("message", (message) => {
      const payload = JSON.parse(message.data);
      if (payload.id === undefined) return;
      const waiter = this.pending.get(payload.id);
      if (!waiter) return;
      this.pending.delete(payload.id);
      if (payload.error) waiter.reject(new Error(JSON.stringify(payload.error)));
      else waiter.resolve(payload.result);
    });
  }

  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
}

/**
 * A page that draws only the shared controls this measurement is about.
 *
 * It is the shared source, not a copy of it: esbuild resolves the same
 * `react-native` shim and the same React that `apps/web/build.mjs` gives the
 * page, so the wheel measured here is the wheel the published page draws.
 */
const ENTRY = `
import * as React from "react";
import { createRoot } from "react-dom/client";
import { View } from "react-native";
import { NativeBusySpinner, NativeButton } from ${JSON.stringify(path.join(SHARED, "ui/NativeControls"))};

function App() {
  return <View style={{ flexDirection: "row", alignItems: "center", gap: 8, padding: 8 }}>
    {/* A caller reserves the slot itself; nothing inside it may be larger. */}
    <View style={{ width: 12, height: 12 }}><NativeBusySpinner label="standalone" /></View>
    {/* The same two controls twice: one working, one idle.  A wait keeps the
        box its idle state had, so a row never re-flows under the pointer. */}
    <NativeButton title="" symbol="refresh" compact busy accessibilityLabel="busy-symbol" />
    <NativeButton title="" symbol="refresh" compact accessibilityLabel="idle-symbol" />
    <NativeButton title="刷新" symbol="refresh" compact busy accessibilityLabel="busy-titled" />
    <NativeButton title="刷新" symbol="refresh" compact accessibilityLabel="idle-titled" />
  </View>;
}

createRoot(document.getElementById("root")!).render(<App />);
`;

const MEASURE = `(() => {
  const box = (node) => {
    const rect = node.getBoundingClientRect();
    return { w: Math.round(rect.width * 100) / 100, h: Math.round(rect.height * 100) / 100 };
  };
  const byLabel = (label) => document.querySelector('[aria-label="' + label + '"]');
  const wheel = document.querySelector('[role="progressbar"]');
  if (!wheel) return { error: "the page drew no busy wheel" };
  // A rotating box reports a larger *painted* rect, so the painted rect cannot
  // answer this: the wheel's own layout boxes are what a row reserves, and the
  // 20 pt system ring this replaced had a 20 pt layout box inside a 12 pt one.
  const descendants = [wheel, ...wheel.querySelectorAll("*")].map((node) => ({ tag: node.tagName, w: node.offsetWidth, h: node.offsetHeight }));
  return {
    wheel: { w: wheel.offsetWidth, h: wheel.offsetHeight },
    spokes: wheel.children.length,
    // A spoke is a rotated wrapper with one bar inside it; a wheel whose spokes
    // lost their bars would still report twelve children.
    drawnSpokes: [...wheel.children].filter((spoke) => spoke.firstElementChild && spoke.firstElementChild.offsetHeight > 0).length,
    descendants,
    animated: getComputedStyle(wheel).animationIterationCount === "infinite" && getComputedStyle(wheel).animationTimingFunction.startsWith("steps(12"),
    busySymbol: box(byLabel("busy-symbol")),
    idleSymbol: box(byLabel("idle-symbol")),
    busyTitled: box(byLabel("busy-titled")),
    idleTitled: box(byLabel("idle-titled")),
  };
})()`;

async function main() {
  const chrome = chromePath();
  if (!chrome) throw new Error("no Chrome or Chromium binary found (set CHROME_PATH)");
  const esbuildPath = path.join(ROOT, "rn/node_modules/esbuild/lib/main.js");
  if (!existsSync(esbuildPath)) throw new Error("esbuild is not installed under rn/; run `pnpm install` there");
  const { build } = await import(pathToFileURL(esbuildPath).href);

  const directory = await mkdtemp(path.join(tmpdir(), "yr-wheel-test-"));
  const profile = await mkdtemp(path.join(tmpdir(), "yr-wheel-profile-"));
  const page = path.join(directory, "index.html");
  await writeFile(path.join(directory, "entry.tsx"), ENTRY, "utf-8");
  await writeFile(page, `<!doctype html><html><body style="margin:0"><div id="root"></div><script src="./bundle.js"></script></body></html>`, "utf-8");
  await build({
    entryPoints: [path.join(directory, "entry.tsx")],
    outfile: path.join(directory, "bundle.js"),
    bundle: true,
    format: "iife",
    platform: "browser",
    target: ["es2022"],
    jsx: "automatic",
    logLevel: "warning",
    loader: { ".png": "dataurl" },
    define: { "process.env.NODE_ENV": '"production"' },
    alias: {
      "react-native": path.join(WEB_APP, "shim.js"),
      react: path.join(WEB_APP, "node_modules/react"),
      "react-dom": path.join(WEB_APP, "node_modules/react-dom"),
      "react-native-web": path.join(WEB_APP, "node_modules/react-native-web"),
    },
  });

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
    const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    cdp = new Cdp(socket);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 420, height: 120, deviceScaleFactor: 2, mobile: false });
    await cdp.send("Page.navigate", { url: pathToFileURL(page).href });

    let measured;
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const answer = await cdp.send("Runtime.evaluate", { expression: MEASURE, returnByValue: true });
      measured = answer.result.value;
      if (measured && !measured.error) break;
      await sleep(100);
    }
    if (!measured || measured.error) throw new Error(measured?.error ?? "the page never drew a busy wheel");
    result.measured = measured;

    if (measured.wheel.w !== 12 || measured.wheel.h !== 12) {
      result.reasons.push(`the wheel's slot is ${measured.wheel.w}x${measured.wheel.h} pt, not the reserved 12x12`);
    }
    // A 20 pt system ring inside this box is exactly the reported defect: the
    // container can still claim 12 while its own child paints past it.
    const oversized = measured.descendants.filter((node) => node.w > 12 || node.h > 12);
    if (oversized.length > 0) {
      result.reasons.push(`the wheel draws ${oversized.length} part(s) larger than its 12 pt slot (${oversized.map((node) => `${node.tag} ${node.w}x${node.h}`).join(", ")})`);
    }
    if (measured.spokes !== 12 || measured.drawnSpokes !== 12) {
      result.reasons.push(`the wheel draws ${measured.drawnSpokes} of 12 spokes`);
    }
    if (!measured.animated) {
      result.reasons.push("the wheel is not turning in the app's twelve steps");
    }
    for (const [busy, idle] of [["busySymbol", "idleSymbol"], ["busyTitled", "idleTitled"]]) {
      if (measured[busy].w !== measured[idle].w || measured[busy].h !== measured[idle].h) {
        result.reasons.push(`a working control changed its box: ${busy} ${measured[busy].w}x${measured[busy].h} vs idle ${measured[idle].w}x${measured[idle].h}`);
      }
    }
    result.ok = result.reasons.length === 0;
  } finally {
    if (cdp) await cdp.send("Page.close").catch(() => undefined);
    browser.kill("SIGKILL");
    await rm(profile, { recursive: true, force: true }).catch(() => undefined);
    await rm(directory, { recursive: true, force: true }).catch(() => undefined);
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

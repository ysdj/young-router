// Where the table's vertical capsule ends, measured in a browser.
//
// The reported defect was a "scroll bar that cannot reach the bottom": a table
// scrolled to its last row still drew its 4 pt capsule one column header short
// of the frame's bottom, so the list looked unfinished at the end and the thumb
// started inside the header at the start.  Nothing in this repository could fail
// on it -- the geometry is a `scrollHeight`/`clientHeight` ratio read at runtime,
// and the source says `top: 2`, which is right for a scroller that owns the
// whole frame and wrong for one that shares it with a header.
//
// The table's frame is taller than its scrolling list: the column header is a
// sibling above the list, inside the horizontal scroller.  So the capsule has to
// be anchored to the list's own top edge and its travel has to be the list's
// height.  This page mounts the shared `NativeTable` -- the same React, the same
// `react-native` shim and the same source `apps/web/build.mjs` bundles -- with
// more rows than fit, and measures both ends of the capsule.
//
// Usage: node test_rn_web_table_scroll_capsule.mjs [--json]
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
 * A table in a pane, with a header and more rows than the pane can show.
 *
 * The frame is fixed, so the list is the frame minus its header and minus the
 * horizontal scroller's own edge -- the two things the capsule's travel must
 * follow.  The rows are the plainest a table draws: a fixed 30 pt each.
 */
const ENTRY = `
import * as React from "react";
import { createRoot } from "react-dom/client";
import { View } from "react-native";
import { NativeTable } from ${JSON.stringify(path.join(SHARED, "ui/NativeControls"))};

const columns = [
  { label: "Upstream", width: 200 },
  { label: "Provider", width: 120 },
  { label: "Key", width: 160 },
  { label: "Order", width: 80 },
];
const rows = Array.from({ length: 20 }, (_, index) => ({
  key: "row-" + index,
  cells: ["model-" + index, "provider-" + index, "key-" + index, String(index)],
}));

function App() {
  return <View style={{ paddingTop: 40, width: 640, height: 280, backgroundColor: "#fff" }}>
    <View style={{ flex: 1, minHeight: 0 }}>
      <NativeTable columns={columns} rows={rows} style={{ flex: 1 }} />
    </View>
  </View>;
}

createRoot(document.getElementById("root")!).render(<App />);
`;

const MEASURE = `(() => {
  const capsule = [...document.querySelectorAll("div")].find((node) => {
    const style = getComputedStyle(node);
    return style.position === "absolute" && style.width === "4px" && style.backgroundColor === "rgba(96, 96, 96, 0.55)";
  });
  if (!capsule) return { error: "the table drew no scroll capsule" };
  const frame = capsule.parentElement;
  const list = [...frame.querySelectorAll("*")].find((node) => {
    const style = getComputedStyle(node);
    return style.overflowY === "auto" && node.scrollHeight > node.clientHeight + 1;
  });
  if (!list) return { error: "the table's rows do not overflow, so there is nothing to measure" };
  const rect = (node) => node.getBoundingClientRect();
  const box = (node) => {
    const r = rect(node);
    return { top: Math.round(r.top * 100) / 100, bottom: Math.round(r.bottom * 100) / 100, height: Math.round(r.height * 100) / 100 };
  };
  const read = () => ({
    list: box(list),
    frame: box(frame),
    capsule: box(capsule),
    scrollTop: Math.round(list.scrollTop * 100) / 100,
    maxScroll: list.scrollHeight - list.clientHeight,
  });
  const atTop = read();
  list.scrollTop = list.scrollHeight - list.clientHeight;
  return new Promise((resolve) => {
    // The capsule moves from the list's own scroll event, which the browser
    // queues: measuring in the same task would read the thumb where it was.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const atEnd = read();
      resolve({
        atTop,
        atEnd,
        rows: list.querySelectorAll("[aria-selected]").length,
        contentHeight: list.scrollHeight,
        listHeight: list.clientHeight,
      });
    }));
  });
})()`;

async function main() {
  const chrome = chromePath();
  if (!chrome) throw new Error("no Chrome or Chromium binary found (set CHROME_PATH)");
  const esbuildPath = path.join(ROOT, "rn/node_modules/esbuild/lib/main.js");
  if (!existsSync(esbuildPath)) throw new Error("esbuild is not installed under rn/; run `pnpm install` there");
  const { build } = await import(pathToFileURL(esbuildPath).href);

  const directory = await mkdtemp(path.join(tmpdir(), "yr-capsule-test-"));
  const profile = await mkdtemp(path.join(tmpdir(), "yr-capsule-profile-"));
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
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 700, height: 400, deviceScaleFactor: 2, mobile: false });
    await cdp.send("Page.navigate", { url: pathToFileURL(page).href });

    let measured;
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const answer = await cdp.send("Runtime.evaluate", { expression: MEASURE, awaitPromise: true, returnByValue: true });
      measured = answer.result.value;
      if (measured && !measured.error) break;
      await sleep(100);
    }
    if (!measured || measured.error) throw new Error(measured?.error ?? "the table never drew a capsule");
    result.measured = measured;

    const headerHeight = Math.round((measured.atTop.list.top - measured.atTop.frame.top) * 100) / 100;
    if (headerHeight < 20) {
      result.reasons.push(`the table drew no header above the list (list starts ${headerHeight} pt below the frame), so this page measures nothing`);
    }
    // At the top the capsule belongs to the list, not to the frame: it starts
    // just inside the list's first row, never over the header.
    const topGap = Math.round((measured.atTop.capsule.top - measured.atTop.list.top) * 100) / 100;
    if (topGap < 0 || topGap > 4) {
      result.reasons.push(`at the top the capsule starts ${topGap} pt from the list's top edge (the header is ${headerHeight} pt), not inside it`);
    }
    // At the end it has to reach the list's bottom, which is what "the scroll
    // bar stops short" is about; the frame's own edge is not where it ends.
    const endGap = Math.round((measured.atEnd.list.bottom - measured.atEnd.capsule.bottom) * 100) / 100;
    if (endGap < 0 || endGap > 4) {
      result.reasons.push(`at the last row the capsule stops ${endGap} pt above the list's bottom edge`);
    }
    if (Math.abs(measured.atEnd.scrollTop - measured.atEnd.maxScroll) > 1) {
      result.reasons.push(`the list did not reach its own end (scrollTop ${measured.atEnd.scrollTop} of ${measured.atEnd.maxScroll})`);
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
    console.log(jsonOnly ? JSON.stringify({ ...result, reasons: [String(error)] }) : JSON.stringify({ ...result, reasons: [String(error)] }, null, 2));
    process.exit(1);
  });

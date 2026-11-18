// Exercise the browser secret leaf through a real React Native for Web mount.
// Source assertions cannot catch the two timing contracts here: blur has to
// leave the shared auto-commit timer alive, and a completed stage has to report
// `saved` so the WebDAV child enables its password-only save.

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const ROOT = path.resolve(import.meta.dirname, "..");
const SHARED = path.join(ROOT, "rn/packages/shared/src");
const WEB_APP = path.join(ROOT, "rn/apps/web");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function chromePath() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const candidates = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ];
  return candidates.find((candidate) => existsSync(candidate));
}

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

const ENTRY = `
import * as React from "react";
import { createRoot } from "react-dom/client";
import { NativeButton, NativeSecureTextInput } from ${JSON.stringify(path.join(SHARED, "ui/NativeControls"))};
import { setBrowserSecretReader, setBrowserSecretStager } from ${JSON.stringify(path.join(SHARED, "platform/browserSecrets"))};

const writes = [];
setBrowserSecretReader(async () => ({ present: false }));
setBrowserSecretStager(async ({ value }) => {
  writes.push(value);
  return { revision: writes.length, present: value.length > 0 };
});

function App() {
  const [commitRequest, setCommitRequest] = React.useState(0);
  const [status, setStatus] = React.useState("ready");
  const [saved, setSaved] = React.useState(false);
  const timer = React.useRef();
  const onSecretState = React.useCallback((state) => {
    setStatus(state.status);
    if (state.status === "dirty") {
      if (timer.current !== undefined) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCommitRequest((current) => current + 1), 500);
    } else if (state.status === "saved") {
      setSaved(true);
    } else if (state.status === "ready" || state.status === "error") {
      if (timer.current !== undefined) clearTimeout(timer.current);
      timer.current = undefined;
    }
  }, []);
  return <>
    <NativeSecureTextInput
      domain="webdav"
      field="password"
      label="Password"
      plainText
      autoCommit
      commitRequest={commitRequest}
      onSecretState={onSecretState}
    />
    <NativeButton title="Outside" accessibilityLabel="outside" onPress={() => undefined} />
    <output id="state">{JSON.stringify({ status, saved, writes: writes.length })}</output>
  </>;
}

createRoot(document.getElementById("root")).render(<App />);
`;

async function main() {
  const chrome = chromePath();
  if (!chrome) throw new Error("no Chrome or Chromium binary found (set CHROME_PATH)");
  const esbuildPath = path.join(ROOT, "rn/node_modules/esbuild/lib/main.js");
  if (!existsSync(esbuildPath)) throw new Error("esbuild is not installed under rn/; run `pnpm install` there");
  const { build } = await import(pathToFileURL(esbuildPath).href);
  const directory = await mkdtemp(path.join(tmpdir(), "yr-secret-test-"));
  const profile = await mkdtemp(path.join(tmpdir(), "yr-secret-profile-"));
  const page = path.join(directory, "index.html");
  await writeFile(path.join(directory, "entry.tsx"), ENTRY, "utf-8");
  await writeFile(page, `<!doctype html><html><body><div id="root"></div><script src="./bundle.js"></script></body></html>`, "utf-8");
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

  const port = 9500 + Math.floor(Math.random() * 300);
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
        await sleep(100);
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
    await cdp.send("Page.navigate", { url: pathToFileURL(page).href });
    await sleep(300);

    const readState = async () => {
      const answer = await cdp.send("Runtime.evaluate", { expression: "JSON.parse(document.getElementById('state').textContent)", returnByValue: true });
      return answer.result.value;
    };
    const input = await cdp.send("Runtime.evaluate", { expression: "document.querySelector('input')?.getBoundingClientRect().toJSON()", returnByValue: true });
    const outside = await cdp.send("Runtime.evaluate", { expression: "document.querySelector('[aria-label=outside]')?.getBoundingClientRect().toJSON()", returnByValue: true });
    if (!input.result?.value || !outside.result?.value) throw new Error("secret input harness did not mount");
    const click = async (rect) => {
      const x = rect.x + Math.max(2, rect.width / 2);
      const y = rect.y + Math.max(2, rect.height / 2);
      for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
        await cdp.send("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 });
      }
    };
    await click(input.result.value);
    await cdp.send("Input.insertText", { text: "blur-safe-secret" });
    // This is the race that used to lose the edit: blur before the 500 ms
    // quiet period. The timer must still stage exactly once.
    await click(outside.result.value);
    await sleep(750);
    const state = await readState();
    if (state.writes !== 1) throw new Error(`blur cancelled the auto-commit (writes=${state.writes})`);
    if (state.status !== "saved" || state.saved !== true) throw new Error(`stage completion was not reported as saved (${JSON.stringify(state)})`);
    console.log(JSON.stringify({ ok: true, state }));
  } finally {
    if (cdp) await cdp.send("Page.close").catch(() => undefined);
    browser.kill("SIGKILL");
    await rm(profile, { recursive: true, force: true }).catch(() => undefined);
    await rm(directory, { recursive: true, force: true }).catch(() => undefined);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

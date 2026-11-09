#!/usr/bin/env node
/**
 * Build the browser host: one page, one script, and the assets it references.
 *
 * The output is what Core serves (`young-router-core --web-host dist`), so the
 * page's origin is Core and `/v1` is same-origin. Nothing here runs for a
 * native build: the two native apps bundle through Metro and never read this
 * directory.
 *
 * `react-native` and `react-native-web` are bridged through `shim.js` rather
 * than through a bundler alias alone, because the shared UI imports three
 * things React Native for Web does not export — `codegenNativeComponent`,
 * `PlatformColor`, and `react-native/Libraries/Types/CodegenTypes`. The shim
 * answers the first two with the browser's own primitives instead of letting
 * esbuild fail on an unresolved export.
 */

import { build } from "esbuild";
import { createHash } from "node:crypto";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { brotliCompressSync, constants, gzipSync } from "node:zlib";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const app = resolve(root, "apps/web");
const shared = resolve(root, "packages/shared/src");
const outdir = resolve(app, "dist");

await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });

await build({
  entryPoints: [resolve(shared, "platformEntry.web.ts")],
  outfile: resolve(outdir, "bundle.js"),
  bundle: true,
  format: "esm",
  platform: "browser",
  target: ["es2022"],
  jsx: "automatic",
  minify: true,
  sourcemap: false,
  legalComments: "none",
  // Metro resolves PNG imports to an asset id; the browser needs a URL. Inlining
  // the six sidebar tiles keeps the build a single directory with no manifest.
  loader: { ".png": "dataurl" },
  define: { "process.env.NODE_ENV": '"production"' },
  // A source only one host can use comes in a platform-specific file, the way
  // Metro spells it. `ui/code-editor/CodeEditorWebBundle.web.ts` is the one that
  // matters: the whole Ace bundle is inlined into the editor page the native
  // hosts hand their WebView, and this page never inlines that document -- it
  // frames it from Core (`editor.html`), so its own script would carry half a
  // megabyte of JavaScript nothing here ever runs. Metro never resolves a
  // `.web.ts` for a macOS or Windows bundle: `web` is not in its platform list.
  resolveExtensions: [".web.tsx", ".web.ts", ".web.js", ".tsx", ".ts", ".jsx", ".js", ".css", ".json"],
  alias: {
    "react-native": resolve(app, "shim.js"),
    // The web app's own copies: the shared UI must be compiled against the
    // React that React Native for Web was resolved with, not against the native
    // host's copy of React Native.
    react: resolve(app, "node_modules/react"),
    "react-dom": resolve(app, "node_modules/react-dom"),
    "react-native-web": resolve(app, "node_modules/react-native-web"),
  },
  logLevel: "warning",
});

// The script's name carries its content, because Core answers a script with
// `Cache-Control: public, max-age=31536000, immutable`. A stable `bundle.js`
// under that header froze every browser on whichever build it first saw: a
// rebuilt page kept loading the previous script for a year, so a fix shipped in
// the app could not reach the page that was open. The name is what makes
// `immutable` true — a changed bundle is a different URL.
const bundle = await readFile(resolve(outdir, "bundle.js"));
const digest = createHash("sha256").update(bundle).digest("hex").slice(0, 16);
const bundleName = `bundle.${digest}.js`;
await writeFile(resolve(outdir, bundleName), bundle);
await rm(resolve(outdir, "bundle.js"));

// The page Core serves. `#root` is the one element React Native for Web mounts
// into; the fragment carries the bootstrap token, which is why the `<base>` is
// not used and the script is a module of the same origin.
const html = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <!-- The page loads nothing but itself: no font host, no analytics, no
         remote style sheet. Core already sends X-Frame-Options and nosniff. -->
    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; script-src 'self'; connect-src 'self'">
    <!-- No icon request: this page is served under /ui, and a browser asking
         the origin's /favicon.ico reaches the proxy's own routes, not ours. -->
    <link rel="icon" href="data:,">
    <title>Young Router</title>
    <style>
      /* The shared panes paint their own palette — the literals every host
         falls back to — so this page is a light surface in both system
         appearances.  A two-state color-scheme here let the browser draw its own dark
         form chrome (a white-on-white select box) under a pane that is still
         white, which is the one combination nobody can read. */
      :root { color-scheme: light; }
      html, body { height: 100%; margin: 0; }
      /* React Native for Web sets a font on the Text it draws and on nothing
         else: a <button>, a <select> and an <input> are the browser's own
         elements and inherit this one.  Without it every toolbar button was
         drawn at the browser's 13.3 px Arial while the label beside it was
         13 px system sans, and every picker at 16 px Times; the panes then
         read as three different type families at three different sizes.  The
         page may not host a font file (the CSP has no font host and the export
         ships none), so the stack is the platforms' own UI faces, and the size
         is the shared UI's own body size so an unstyled element matches the
         Text around it. */
      body {
        /* The page is the desktop the app's window sits on, not the app's own
           surface.  It was 247,247,247 -- the same literal as the shell's own
           fill (windowBackgroundColor), so a centred shell ended in the middle
           of the page with no boundary at all: the window's edge and the page
           behind it were one colour.  A step darker states that there are two
           surfaces; the shell's own border draws the line between them. */
        background: #e3e3e8;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
        font-size: 13px;
        line-height: 1.35;
      }
      /* A form control does not inherit the page's face by default; it is the
         element the browser styles itself, so it has to be told. */
      button, input, select, textarea { font-family: inherit; font-size: inherit; }
      /* React Native for Web draws every Pressable as a <button> and gives it no
         focus ring: the document held no focus rule at all, and a focused
         button computed outline-style none with no shadow, so tabbing
         through the page moved focus invisibly.  The native hosts have a
         system ring to draw; a page has to state one.  The focus-visible
         selector keeps it off a mouse click, which is what a person expects
         from a desktop app, and the accent is the shared control table's own
         (the ink a primary button already uses). */
      :focus-visible { outline: 2px solid #005fb8; outline-offset: 1px; }
      /* A scroll container that is focusable for keyboard scrolling still gets
         the ring above only when it is actually tabbed to, not on every pane. */
      [tabindex="-1"]:focus { outline: none; }
      #root { height: 100%; display: flex; }
      /* The host's surfaces own the viewport above every pane and child layer;
         the container itself must not swallow clicks meant for the app. */
      #host-chrome { position: fixed; inset: 0; z-index: 2000; pointer-events: none; }
      #host-chrome > * { pointer-events: auto; }
    </style>
  </head>
  <body>
    <div id="root"></div>
    <!-- The host's own surfaces (a decision panel, the record viewer, a menu)
         mount here, beside the app rather than inside it: a dialog must not
         replace the pane it was asked from. -->
    <div id="host-chrome"></div>
    <script type="module" src="./${bundleName}"></script>
  </body>
</html>
`;
await writeFile(resolve(outdir, "index.html"), html);

// The raw editor page. Every host renders the same document: the native hosts
// hand it to their WebView, and this host serves it as its own same-origin file
// for the editor to frame. It must be a real document, not `srcdoc` or a blob:
// both inherit this page's `script-src 'self'`, which refuses the editor's own
// script. The page is built from the shared module so the three hosts cannot
// drift; esbuild is only the way to run that TypeScript here in Node.
const editorPageModule = await build({
  entryPoints: [resolve(shared, "ui/code-editor/codeEditorPage.ts")],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false,
  logLevel: "warning",
});
const editorPagePath = resolve(outdir, ".editor-page.mjs");
await writeFile(editorPagePath, editorPageModule.outputFiles[0].text);
let editorName = "";
try {
  const { codeEditorPageHtml, CODE_EDITOR_WEB_BUNDLE } = await import(pathToFileURL(editorPagePath).href);
  // The editor is a script of its own here, addressed by content so Core's
  // `immutable` header is true of it. The framed document is then a few KB
  // instead of half a megabyte of inlined Ace, and opening the raw editor a
  // second time reads this file out of the browser's cache: `editor.html`
  // itself is answered `no-store`, so an inline editor was fetched again on
  // every open. The native hosts keep the inline copy -- their WebView has no
  // origin to fetch from -- and both copies are the one bundle this repository
  // builds (`rn/scripts/build-code-editor.mjs` writes `CodeEditorWebBundle.ts`).
  const editorScript = Buffer.from(CODE_EDITOR_WEB_BUNDLE, "utf8");
  editorName = `editor.${createHash("sha256").update(editorScript).digest("hex").slice(0, 16)}.js`;
  await writeFile(resolve(outdir, editorName), editorScript);
  await writeFile(resolve(outdir, "editor.html"), codeEditorPageHtml({ platform: "web", scriptSrc: `./${editorName}` }));
} finally {
  await rm(editorPagePath, { force: true });
}

await cp(resolve(app, "shim.js"), resolve(outdir, "shim.js")).catch(() => undefined);

// Precompressed siblings: the page's own script is 1.2 MB, and the wire can
// carry it at a fifth of that. The encoding is decided here rather than in
// Core because compressing this bundle inside the listener would spend a
// worker thread on the first page load of every browser, and a deterministic,
// offline answer already exists at build time. Core answers a sibling only for
// the file it was built beside (`bundle.<hash>.js.br` under a request for
// `bundle.<hash>.js`), so it cannot go stale: the script's name is its own
// digest. The `.br` and `.gz` bytes are derived, not content -- the publisher's
// manifest identifies the page by what it decodes to, and a different brotli
// library must not read as a different page.
const CODECS = [
  ["br", ".br", (bytes) => brotliCompressSync(bytes, { params: { [constants.BROTLI_PARAM_QUALITY]: 11 } })],
  ["gzip", ".gz", (bytes) => gzipSync(bytes, { level: 9 })],
];
const wire = [];
for (const name of ["index.html", "editor.html", bundleName, editorName].filter(Boolean)) {
  const bytes = await readFile(resolve(outdir, name));
  const sizes = [];
  for (const [codec, suffix, compress] of CODECS) {
    const encoded = compress(bytes);
    await writeFile(resolve(outdir, `${name}${suffix}`), encoded);
    sizes.push(`${codec} ${Math.round(encoded.length / 1024)} KB`);
  }
  wire.push(`${name} ${Math.round(bytes.length / 1024)} KB (${sizes.join(", ")})`);
}

console.log(`browser host built: ${outdir}\n  ${wire.join("\n  ")}`);

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
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const app = resolve(root, "apps/web");
const shared = resolve(root, "packages/shared/src");
const outdir = resolve(app, "dist");

await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });

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
      :root { color-scheme: light dark; }
      html, body { height: 100%; margin: 0; }
      body { background: #f7f7f7; }
      #root { height: 100%; display: flex; }
      /* The host's surfaces own the viewport above every pane and child layer;
         the container itself must not swallow clicks meant for the app. */
      #host-chrome { position: fixed; inset: 0; z-index: 2000; pointer-events: none; }
      #host-chrome > * { pointer-events: auto; }
      @media (prefers-color-scheme: dark) { body { background: #1e1e1e; } }
    </style>
  </head>
  <body>
    <div id="root"></div>
    <!-- The host's own surfaces (a decision panel, the record viewer, a menu)
         mount here, beside the app rather than inside it: a dialog must not
         replace the pane it was asked from. -->
    <div id="host-chrome"></div>
    <script type="module" src="./bundle.js"></script>
  </body>
</html>
`;
await writeFile(resolve(outdir, "index.html"), html);

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

await cp(resolve(app, "shim.js"), resolve(outdir, "shim.js")).catch(() => undefined);

const bundle = await readFile(resolve(outdir, "bundle.js"));
console.log(`browser host built: ${outdir} (bundle.js ${Math.round(bundle.length / 1024)} KB)`);

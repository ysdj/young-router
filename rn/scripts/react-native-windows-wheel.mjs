// Carry the wheel-patched Microsoft.ReactNative.dll between machines.
//
// The wheel fix is a source change to React Native Windows, so the shipped
// binary is a source build of it.  Producing that build is half an hour of
// compilation and it produces no valid PRI, so only one machine does it; every
// other machine copies the result.  Copying a DLL by hand is how a machine ends
// up with a binary from a different RNW release, which links and then misbehaves
// in ways nothing names -- so the copy carries its digest and the RNW version it
// was built from, and both are checked before it is installed.
//
//   node rn/scripts/react-native-windows-wheel.mjs export --out <dir>
//   node rn/scripts/react-native-windows-wheel.mjs install --from <dir|url>
//
// ``export`` writes the DLL beside a ``wheel.json`` manifest; ``install`` reads
// that manifest, verifies the bytes and the installed RNW release, and only then
// replaces the target.  Both default to ARM64, the architecture this app ships,
// and both accept ``--arch`` and ``--root``.
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const MANIFEST = "wheel.json";

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  if (index < 0 || !process.argv[index + 1]) return fallback;
  return process.argv[index + 1];
}

function fail(message) {
  console.error(`react-native-windows-wheel: ${message}`);
  process.exit(1);
}

const command = process.argv[2];
const repositoryRoot = resolve(option("--root", resolve(dirname(fileURLToPath(import.meta.url)), "..", "..")));
// The architecture is spelled the way React Native Windows and MSBuild spell
// it -- ``ARM64`` and ``x64`` -- because it becomes a directory name: RNW puts
// its output under ``target\$(Platform)\$(Configuration)``, so uppercasing
// ``x64`` would name a ``target\X64`` that no build creates.
const requestedArch = option("--arch", process.env.YOUNG_ROUTER_WINDOWS_ARCH || "ARM64");
const architecture = /^arm64$/i.test(requestedArch)
  ? "ARM64"
  : /^x64$/i.test(requestedArch)
    ? "x64"
    : undefined;

if (!architecture) {
  fail(`unknown architecture ${requestedArch}; expected ARM64 or x64`);
}

const rnRoot = join(repositoryRoot, "rn");
const target = join(
  rnRoot,
  "node_modules/react-native-windows/target",
  architecture,
  "Release/Microsoft.ReactNative/Microsoft.ReactNative.dll"
);
const rnwPackage = join(rnRoot, "node_modules/react-native-windows/package.json");

function digest(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function stagedRelease() {
  if (!existsSync(rnwPackage)) {
    fail(`React Native Windows is not installed: ${rnwPackage}. Run pnpm install first.`);
  }
  const { version } = JSON.parse(readFileSync(rnwPackage, "utf8"));
  return version;
}

// A source artifact is identified by its bytes, and pairing it with a different
// RNW release is the one mistake that still links: the host calls into the DLL
// by ABI, so a stale copy shows up as a crash or a wrong scroll distance rather
// than as a build error.  The manifest records both facts.
function manifestFor(bytes, version) {
  return {
    reactNativeWindows: version,
    architecture,
    sha256: digest(bytes),
    bytes: bytes.length,
  };
}

function readManifest(source) {
  const path = join(source, MANIFEST);
  if (!existsSync(path)) fail(`no ${MANIFEST} in ${source}`);
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    fail(`${path} is not valid JSON: ${error.message}`);
  }
}

async function readArtifact(source) {
  if (/^https?:\/\//i.test(source)) {
    const response = await fetch(source);
    if (!response.ok) fail(`${source} answered HTTP ${response.status}`);
    return Buffer.from(await response.arrayBuffer());
  }
  // A directory export carries the manifest; a bare file is its own payload.
  if (existsSync(join(source, MANIFEST))) {
    const manifest = readManifest(source);
    const payload = join(source, "Microsoft.ReactNative.dll");
    if (!existsSync(payload)) fail(`no Microsoft.ReactNative.dll in ${source}`);
    return { bytes: readFileSync(payload), manifest };
  }
  if (existsSync(source)) fail(`${source} carries no ${MANIFEST}, so the bytes cannot be verified`);
  fail(`nothing to read at ${source}`);
}

function verify(bytes, manifest) {
  const expected = manifest.sha256;
  if (typeof expected !== "string" || !/^[0-9a-f]{64}$/.test(expected)) {
    fail(`the manifest carries no usable sha256: ${JSON.stringify(expected)}`);
  }
  const actual = digest(bytes);
  if (actual !== expected) fail(`the artifact is not the recorded one: sha256 ${actual}, expected ${expected}`);
  if (typeof manifest.bytes === "number" && manifest.bytes !== bytes.length) {
    fail(`the artifact is ${bytes.length} bytes, the manifest says ${manifest.bytes}`);
  }
  if (manifest.architecture && manifest.architecture.toLowerCase() !== architecture.toLowerCase()) {
    fail(`the artifact is ${manifest.architecture}, this tree builds ${architecture}`);
  }
}

function checkRelease(manifest) {
  const version = stagedRelease();
  if (manifest.reactNativeWindows !== version) {
    fail(
      `the artifact was built from React Native Windows ${manifest.reactNativeWindows}, ` +
        `this tree stages ${version}; rebuild it or fetch the matching one`
    );
  }
  return version;
}

async function install() {
  const source = option("--from", process.env.YOUNG_ROUTER_RNW_PREBUILT);
  if (!source) fail("install needs --from <dir|url> (or YOUNG_ROUTER_RNW_PREBUILT)");
  const { bytes, manifest } = await readArtifact(source);
  verify(bytes, manifest);
  const version = checkRelease(manifest);
  mkdirSync(dirname(target), { recursive: true });
  // Written beside the target and renamed over it, so an interrupted copy can
  // never leave a half-written DLL for the build to link against.
  const temporary = `${target}.incoming`;
  writeFileSync(temporary, bytes);
  renameSync(temporary, target);
  console.log(
    `react-native-windows-wheel: installed ${bytes.length} bytes for React Native Windows ${version} ` +
      `(${architecture}) at ${target}`
  );
}

function exportArtifact() {
  const out = option("--out");
  if (!out) fail("export needs --out <dir>");
  if (!existsSync(target)) {
    fail(
      `no source build at ${target}. Build it on the machine that can ` +
        `(UseExperimentalNuget=false) before exporting it.`
    );
  }
  const bytes = readFileSync(target);
  const version = stagedRelease();
  const destination = resolve(out);
  mkdirSync(destination, { recursive: true });
  writeFileSync(join(destination, "Microsoft.ReactNative.dll"), bytes);
  writeFileSync(join(destination, MANIFEST), `${JSON.stringify(manifestFor(bytes, version), null, 2)}\n`);
  console.log(
    `react-native-windows-wheel: exported ${bytes.length} bytes for React Native Windows ${version} ` +
      `(${architecture}) into ${destination}`
  );
}

function status() {
  if (!existsSync(target)) {
    console.log(`react-native-windows-wheel: no source build at ${target}`);
    return;
  }
  const bytes = readFileSync(target);
  // The recorded digest is a fact about this checkout, not about the tree that
  // --root names: a tree only has to own node_modules.
  const recordedPath = join(dirname(fileURLToPath(import.meta.url)), "react-native-windows-wheel.json");
  const known = existsSync(recordedPath) ? JSON.parse(readFileSync(recordedPath, "utf8")) : undefined;
  const recorded = known?.artifacts?.[architecture];
  const actual = digest(bytes);
  const matches = recorded ? recorded.sha256 === actual : null;
  console.log(
    `react-native-windows-wheel: ${target}\n  sha256 ${actual}\n  bytes  ${bytes.length}` +
      (matches === null
        ? `\n  no build recorded for ${architecture} in react-native-windows-wheel.json`
        : `\n  ${matches ? "matches" : "does NOT match"} the recorded build`)
  );
}

if (command === "install") {
  await install();
} else if (command === "export") {
  exportArtifact();
} else if (command === "status") {
  status();
} else {
  fail("usage: react-native-windows-wheel.mjs <export|install|status> [--root <dir>] [--arch ARM64] [--out|--from <dir|url>]");
}

#!/usr/bin/env node

import {existsSync, mkdirSync, readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {verifyVendor} from './verify-rnmacos-085.mjs';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const rnRoot = path.resolve(scriptDirectory, '..');
const manifestPath = path.join(rnRoot, 'vendor', 'react-native-macos-0.85.json');

// The native modules `React-Fabric` and fourteen other pods compile against are
// declared by the vendored package's own `codegenConfig`, and the C++ sources
// include `FBReactNativeSpec/FBReactNativeSpecJSI.h`. That directory is listed
// in the fork's `.gitignore` and is written by the package's `prepack` script,
// so it exists in the published npm tarball and never in a Git checkout — which
// is what this repository vendors. Without it the macOS build fails in
// `React-Fabric` with:
//
//   NativeAnimatedNodesManager.h:13:10: fatal error:
//   'FBReactNativeSpec/FBReactNativeSpecJSI.h' file not found
//
// No header search path can repair that: the file genuinely does not exist, so
// the generator is run here instead. Only the codegen half of `prepack` is
// invoked, because the script also copies the monorepo README over the
// package's own, which modifies a tracked file and would fail the vendor
// verification below. The generated output is gitignored, so the checkout is
// still reported clean.

function fail(message) {
  throw new Error(`[rnmacos-0.85] ${message}`);
}

function run(command, args, cwd) {
  const result = spawnSync(command, args, {cwd, stdio: 'inherit'});
  if (result.error) {
    fail(`Could not run ${command}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    fail(`${command} ${args.join(' ')} failed with exit code ${result.status}.`);
  }
}

function generateFBReactNativeSpec(packageDirectory) {
  const generator = path.join(
    packageDirectory,
    'scripts',
    'codegen',
    'generate-artifacts-executor',
    'generateFBReactNativeSpecIOS.js',
  );
  if (!existsSync(generator)) {
    fail(`FBReactNativeSpec generator is missing at ${path.relative(rnRoot, generator)}.`);
  }
  const script = `require(${JSON.stringify(generator)}).generateFBReactNativeSpecIOS(${JSON.stringify(packageDirectory)});`;
  run(process.execPath, ['-e', script], packageDirectory);
  const generated = path.join(
    packageDirectory,
    'React',
    'FBReactNativeSpec',
    'FBReactNativeSpecJSI.h',
  );
  if (!existsSync(generated)) {
    fail(`Codegen did not produce ${path.relative(rnRoot, generated)}; the macOS host cannot compile without it.`);
  }
  console.log(`Generated ${path.relative(rnRoot, generated)}.`);
}

function main() {
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  const vendorDirectory = path.resolve(rnRoot, manifest.vendorDirectory);
  const vendorParent = path.dirname(vendorDirectory);
  const yarnRelease = path.join(vendorDirectory, manifest.yarnRelease);
  const shouldRefreshInstall = Boolean(
    process.env.CI || process.env.YOUNG_ROUTER_REFRESH_RN_VENDOR === '1',
  );

  if (!existsSync(vendorDirectory)) {
    mkdirSync(vendorParent, {recursive: true});
    run('git', ['clone', '--filter=blob:none', '--no-checkout', manifest.repository, vendorDirectory], rnRoot);
    run('git', ['fetch', '--depth=1', 'origin', manifest.ref], vendorDirectory);
    run('git', ['checkout', '--detach', manifest.commit], vendorDirectory);
  } else if (!existsSync(path.join(vendorDirectory, '.git'))) {
    fail(`${path.relative(rnRoot, vendorDirectory)} exists but is not a Git checkout. Refusing to replace it.`);
  }

  const currentCommit = spawnSync('git', ['rev-parse', 'HEAD'], {cwd: vendorDirectory, encoding: 'utf8'});
  if (currentCommit.status !== 0 || currentCommit.stdout.trim() !== manifest.commit) {
    fail(`${path.relative(rnRoot, vendorDirectory)} is not pinned to ${manifest.commit}. Refusing to alter an existing checkout.`);
  }
  if (!existsSync(yarnRelease)) {
    fail(`Pinned Yarn release is missing from ${path.relative(rnRoot, vendorDirectory)}.`);
  }

  let result;
  if (!shouldRefreshInstall) {
    try {
      result = verifyVendor();
      console.log('Reusing verified react-native-macos vendor dependencies.');
    } catch {
      // A partial checkout must be repaired with the pinned immutable install.
    }
  }
  if (!result) {
    run(process.execPath, [yarnRelease, 'install', '--immutable'], vendorDirectory);
    result = verifyVendor();
  }
  console.log(`Bootstrapped react-native-macos 0.85 at ${path.relative(rnRoot, result.packageDirectory)}.`);
  generateFBReactNativeSpec(result.packageDirectory);
  // Re-verify: the generator must not have touched a tracked file, and the
  // pinned checkout is what every later step trusts.
  verifyVendor();
  console.log('For macOS CocoaPods/source builds, use: RCT_USE_RN_DEP=0 RCT_USE_PREBUILT_RNCORE=0 RCT_BUILD_HERMES_FROM_SOURCE=true RCT_HERMES_V1_ENABLED=1');
}

main();

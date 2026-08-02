#!/usr/bin/env node

import {existsSync, mkdirSync, readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {verifyVendor} from './verify-rnmacos-085.mjs';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const rnRoot = path.resolve(scriptDirectory, '..');
const manifestPath = path.join(rnRoot, 'vendor', 'react-native-macos-0.85.json');

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

function main() {
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  const vendorDirectory = path.resolve(rnRoot, manifest.vendorDirectory);
  const vendorParent = path.dirname(vendorDirectory);
  const yarnRelease = path.join(vendorDirectory, manifest.yarnRelease);
  const shouldRefreshInstall = Boolean(
    process.env.CI || process.env.LITELLM_MENU_REFRESH_RN_VENDOR === '1',
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
  console.log('For macOS CocoaPods/source builds, use: RCT_USE_RN_DEP=0 RCT_USE_PREBUILT_RNCORE=0 RCT_BUILD_HERMES_FROM_SOURCE=true RCT_HERMES_V1_ENABLED=1');
}

main();

#!/usr/bin/env node

import {existsSync, readFileSync, realpathSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const rnRoot = path.resolve(scriptDirectory, '..');
const manifestPath = path.join(rnRoot, 'vendor', 'react-native-macos-0.85.json');

function fail(message) {
  throw new Error(`[rnmacos-0.85] ${message}`);
}

function readJson(file) {
  if (!existsSync(file)) {
    fail(`Missing ${path.relative(rnRoot, file)}.`);
  }
  return JSON.parse(readFileSync(file, 'utf8'));
}

function run(command, args, cwd) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error) {
    fail(`Could not run ${command}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    fail(`${command} ${args.join(' ')} failed:\n${result.stderr || result.stdout}`);
  }
  return result.stdout.trim();
}

function realpathIfPresent(candidate) {
  return existsSync(candidate) ? realpathSync(candidate) : null;
}

function assertEqual(actual, expected, description) {
  if (actual !== expected) {
    fail(`${description}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}.`);
  }
}

function assertInside(child, parent, description) {
  const relative = path.relative(parent, child);
  if (relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))) {
    return;
  }
  fail(`${description} resolves outside the macOS vendor checkout: ${child}`);
}

function verifyWorkspaceLink(packageDirectory, workspaceDirectory, packageName) {
  const resolved = realpathIfPresent(path.join(packageDirectory, 'node_modules', packageName));
  if (!resolved) {
    fail(`Yarn did not install workspace dependency ${packageName}. Run bootstrap-rnmacos-085.mjs.`);
  }
  const expected = realpathSync(workspaceDirectory);
  assertEqual(resolved, expected, `${packageName} workspace link`);
}

function verifyBuildEnvironment(manifest) {
  for (const [name, expected] of Object.entries(manifest.hermes.requiredBuildEnvironment)) {
    assertEqual(process.env[name], expected, `Environment variable ${name}`);
  }
}

export function verifyVendor({checkBuildEnvironment = false} = {}) {
  const manifest = readJson(manifestPath);
  const vendorDirectory = path.resolve(rnRoot, manifest.vendorDirectory);
  const packageDirectory = path.join(vendorDirectory, manifest.reactNativeDirectory);
  const listsDirectory = path.join(vendorDirectory, manifest.virtualizedListsDirectory);
  const yarnRelease = path.join(vendorDirectory, manifest.yarnRelease);

  if (!existsSync(path.join(vendorDirectory, '.git'))) {
    fail(`Missing Git checkout at ${path.relative(rnRoot, vendorDirectory)}.`);
  }
  assertEqual(run('git', ['remote', 'get-url', 'origin'], vendorDirectory), manifest.repository, 'Vendor origin');
  assertEqual(run('git', ['rev-parse', 'HEAD'], vendorDirectory), manifest.commit, 'Vendor commit');
  if (run('git', ['status', '--porcelain'], vendorDirectory) !== '') {
    fail('Vendor checkout has tracked-file modifications; recreate it instead of carrying a local patch.');
  }

  if (!existsSync(yarnRelease)) {
    fail(`Pinned Yarn release is missing: ${path.relative(rnRoot, yarnRelease)}.`);
  }
  assertEqual(run(process.execPath, [yarnRelease, '--version'], vendorDirectory), manifest.yarnVersion, 'Yarn version');

  const rootPackage = readJson(path.join(vendorDirectory, 'package.json'));
  const nativePackage = readJson(path.join(packageDirectory, 'package.json'));
  const listsPackage = readJson(path.join(listsDirectory, 'package.json'));
  const yarnConfig = readFileSync(path.join(vendorDirectory, '.yarnrc.yml'), 'utf8');
  assertEqual(rootPackage.name, '@react-native-macos/monorepo', 'Vendor root package name');
  assertEqual(rootPackage.packageManager, `yarn@${manifest.yarnVersion}`, 'Vendor package manager');
  if (!Array.isArray(rootPackage.workspaces) || !rootPackage.workspaces.includes('packages/*')) {
    fail('Vendor root must retain the packages/* Yarn workspace.');
  }
  if (!/nodeLinker:\s*pnpm\b/.test(yarnConfig) || !/enableScripts:\s*false\b/.test(yarnConfig)) {
    fail('Vendor .yarnrc.yml must retain the pinned pnpm linker and disabled lifecycle scripts.');
  }
  assertEqual(nativePackage.name, 'react-native-macos', 'Native package name');
  assertEqual(nativePackage.version, manifest.sourceVersion, 'Native package source version');
  assertEqual(listsPackage.name, '@react-native-macos/virtualized-lists', 'Virtualized lists package name');
  assertEqual(listsPackage.version, manifest.sourceVersion, 'Virtualized lists source version');
  assertEqual(nativePackage.dependencies['@react-native-macos/virtualized-lists'], 'workspace:*', 'Virtualized lists dependency');

  const requiredWorkspaces = [
    '@react-native/assets-registry',
    '@react-native/codegen',
    '@react-native/community-cli-plugin',
    '@react-native/gradle-plugin',
    '@react-native/js-polyfills',
    '@react-native/normalize-colors',
  ];
  for (const packageName of requiredWorkspaces) {
    assertEqual(nativePackage.dependencies[packageName], 'workspace:*', `${packageName} dependency`);
  }

  verifyWorkspaceLink(packageDirectory, listsDirectory, '@react-native-macos/virtualized-lists');
  const compilerPackagePath = path.join(packageDirectory, 'node_modules', 'hermes-compiler', 'package.json');
  const compilerPackage = readJson(compilerPackagePath);
  assertEqual(nativePackage.dependencies['hermes-compiler'], manifest.hermes.compilerVersion, 'Hermes compiler dependency');
  assertEqual(compilerPackage.version, manifest.hermes.compilerVersion, 'Installed Hermes compiler version');
  if (manifest.hermes.compilerVersion === manifest.hermes.windowsCompilerVersion) {
    fail('macOS and Windows Hermes compiler versions must be declared separately.');
  }
  assertEqual(readFileSync(path.join(packageDirectory, 'sdks', '.hermesversion'), 'utf8').trim(), manifest.hermes.legacyTag, 'Legacy Hermes tag');
  assertEqual(readFileSync(path.join(packageDirectory, 'sdks', '.hermesv1version'), 'utf8').trim(), manifest.hermes.v1Tag, 'Hermes V1 tag');
  assertEqual(manifest.hermes.sourceTag, `hermes-v${manifest.hermes.compilerVersion}`, 'Hermes source tag');
  if (!/^[0-9a-f]{40}$/.test(manifest.hermes.sourceCommit)) {
    fail('Hermes source commit must be a full lowercase Git commit.');
  }

  const compilerDirectory = realpathSync(path.dirname(compilerPackagePath));
  assertInside(compilerDirectory, realpathSync(vendorDirectory), 'Hermes compiler');
  const topLevelCompiler = realpathIfPresent(path.join(rnRoot, 'node_modules', 'hermes-compiler'));
  if (topLevelCompiler && topLevelCompiler === compilerDirectory) {
    fail('macOS Hermes compiler is hoisted into rn/node_modules; it must remain inside the macOS vendor checkout.');
  }

  for (const variable of ['HERMES_ENGINE_TARBALL_PATH', 'HERMES_OVERRIDE_HERMESC_PATH', 'HERMES_COMMIT', 'REACT_NATIVE_OVERRIDE_HERMES_DIR']) {
    if (process.env[variable]) {
      fail(`${variable} is set. Refuse an externally supplied Hermes engine/compiler for the pinned macOS checkout.`);
    }
  }
  if (checkBuildEnvironment) {
    verifyBuildEnvironment(manifest);
  }

  return {
    commit: manifest.commit,
    packageDirectory,
    yarnRelease,
  };
}

function main() {
  const checkBuildEnvironment = process.argv.slice(2).includes('--check-build-env');
  const result = verifyVendor({checkBuildEnvironment});
  console.log(`Verified react-native-macos 0.85 vendor at ${path.relative(rnRoot, result.packageDirectory)} (${result.commit}).`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}

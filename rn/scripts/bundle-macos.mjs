#!/usr/bin/env node

import {createHash} from 'node:crypto';
import {existsSync, readFileSync, readdirSync, utimesSync, writeFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const projectDirectory = path.resolve(scriptDirectory, '..');
const bundleScript = path.resolve(
  projectDirectory,
  'vendor/react-native-macos-0.85/packages/react-native/scripts/bundle.js',
);

const rawArgs = process.argv.slice(2);
const resetMetroCache = process.env.CI || process.env.YOUNG_ROUTER_RESET_METRO_CACHE === '1';
const args = resetMetroCache
  ? rawArgs
  : rawArgs.filter(arg => arg !== '--reset-cache');

/** The value of a ``--flag value`` or ``--flag=value`` argument. */
function argumentValue(flag) {
  const inline = rawArgs.find(arg => arg.startsWith(`${flag}=`));
  if (inline !== undefined) {
    return inline.slice(flag.length + 1);
  }
  const index = rawArgs.indexOf(flag);
  return index >= 0 ? rawArgs[index + 1] : undefined;
}

const SOURCE_SUFFIXES = ['.ts', '.tsx', '.js', '.jsx', '.json', '.svg', '.png', '.webp', '.mjs', '.cjs'];
const SKIPPED_DIRECTORIES = new Set(['node_modules', 'macos', 'build', 'Pods', '.git', 'vendor', 'scripts']);

/**
 * Every file Metro may read for this bundle, in a stable order.
 *
 * The bundle is a pure function of these sources plus the flags below, so the
 * same list is what decides whether a repeat build has anything to do.  Native
 * project directories are excluded: they are inputs to the app, not to Metro.
 */
function sourceFiles() {
  const roots = [
    path.join(projectDirectory, 'packages'),
    path.join(projectDirectory, 'apps', 'macos', 'src'),
  ];
  const files = [
    'apps/macos/index.js',
    'apps/macos/app.json',
    'apps/macos/package.json',
    'apps/macos/metro.config.js',
    'apps/macos/babel.config.js',
    'package.json',
    'pnpm-workspace.yaml',
    'pnpm-lock.yaml',
  ].map(relative => path.join(projectDirectory, relative));
  const walk = directory => {
    let entries;
    try {
      entries = readdirSync(directory, {withFileTypes: true});
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        if (!SKIPPED_DIRECTORIES.has(entry.name)) {
          walk(full);
        }
        continue;
      }
      if (entry.isFile() && SOURCE_SUFFIXES.some(suffix => entry.name.endsWith(suffix))) {
        files.push(full);
      }
    }
  };
  roots.forEach(walk);
  return files.filter(file => existsSync(file)).sort();
}

/**
 * The hash a bundle is built from: these sources, and the flags that shape it.
 */
function sourceHash() {
  const digest = createHash('sha256');
  const flags = ['--dev', '--minify', '--platform', '--entry-file', '--assets-dest']
    .map(flag => `${flag}=${argumentValue(flag) ?? ''}`)
    .join('\0');
  digest.update(flags);
  digest.update('\0');
  for (const file of sourceFiles()) {
    digest.update(path.relative(projectDirectory, file));
    digest.update('\0');
    digest.update(readFileSync(file));
    digest.update('\0');
  }
  return digest.digest('hex');
}

const bundleOutput = argumentValue('--bundle-output');
const stampPath = bundleOutput ? `${bundleOutput}.yr-sources` : undefined;
// A bundle the sources have not moved since is not rebuilt: Metro would walk
// the whole dependency graph to write the same bytes over the same file.  A
// forced cache reset, a missing bundle, or a missing stamp all fall through to
// the real bundle, so a stale stamp can only cost time, never correctness.
if (stampPath && !resetMetroCache && existsSync(bundleOutput)) {
  const stamp = sourceHash();
  const previous = existsSync(stampPath) ? readFileSync(stampPath, 'utf8').trim() : '';
  if (previous === stamp) {
    // The bundle is still the right one, but Xcode compares the phase's output
    // against its inputs: an untouched file reads as "this phase produced
    // nothing new", and the app keeps the copy it already had — which is how a
    // changed source could ship a stale bundle.  Marking it renewed keeps the
    // copy step honest without paying for Metro.
    const now = new Date();
    utimesSync(bundleOutput, now, now);
    utimesSync(stampPath, now, now);
    console.log(`bundle-macos: sources unchanged (${stamp.slice(0, 12)}); keeping the existing bundle.`);
    process.exit(0);
  }
}

const result = spawnSync(process.execPath, [bundleScript, ...args], {
  env: process.env,
  stdio: 'inherit',
});

if (result.error) {
  throw result.error;
}
if ((result.status ?? 1) === 0 && stampPath) {
  try {
    writeFileSync(stampPath, `${sourceHash()}\n`);
  } catch {
    // A bundle that cannot record its stamp is still a bundle: the next build
    // simply bundles again.
  }
}
process.exitCode = result.status ?? 1;

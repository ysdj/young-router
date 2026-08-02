#!/usr/bin/env node

import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const bundleScript = path.resolve(
  scriptDirectory,
  '..',
  'vendor/react-native-macos-0.85/packages/react-native/scripts/bundle.js',
);
const resetMetroCache = process.env.CI || process.env.LITELLM_MENU_RESET_METRO_CACHE === '1';
const args = resetMetroCache
  ? process.argv.slice(2)
  : process.argv.slice(2).filter(arg => arg !== '--reset-cache');
const result = spawnSync(process.execPath, [bundleScript, ...args], {
  env: process.env,
  stdio: 'inherit',
});

if (result.error) {
  throw result.error;
}
process.exitCode = result.status ?? 1;

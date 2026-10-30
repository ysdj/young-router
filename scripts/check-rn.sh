#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/rn"

command -v node >/dev/null 2>&1 || {
  echo "node is required for the shared React Native contract check" >&2
  exit 1
}

NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
if [[ "$NODE_MAJOR" -lt 22 ]]; then
  echo "Node.js 22 or later is required for the React Native 0.85 workspace." >&2
  exit 1
fi

node scripts/check-contract.mjs

# TypeScript depends on the RN packages and is therefore optional for a clean
# source checkout.  Once dependencies are installed, make it part of the same
# command and fail on type drift.
if [[ -f node_modules/typescript/bin/tsc ]]; then
  command -v pnpm >/dev/null 2>&1 || {
    echo "pnpm is required for the installed React Native workspace checks" >&2
    exit 1
  }
  node node_modules/typescript/bin/tsc --noEmit
  pnpm run codegen:windows:check
else
  echo "RN dependencies are not installed; skipped TypeScript and Windows codegen checks" >&2
fi

if [[ -d vendor/react-native-macos-0.85/.git ]]; then
  node scripts/verify-rnmacos-085.mjs
else
  echo "Pinned react-native-macos source is not bootstrapped; macOS build will bootstrap it." >&2
fi

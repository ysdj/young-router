#!/usr/bin/env bash
# Point this clone's git hooks at the checked-in ones.
#
# ``core.hooksPath`` is recorded in the clone's own .git/config, so it is not
# distributed with the repository: every clone has to opt in once.  Running
# this is a local convenience, never a requirement -- CI runs the same gate, so
# a clone that skips it cannot get a leak into the public branch unnoticed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

git config core.hooksPath scripts/githooks
chmod +x scripts/githooks/pre-commit scripts/githooks/commit-msg scripts/githooks/pre-push

echo "Installed repository hygiene hooks from scripts/githooks (core.hooksPath)."
echo "Bypass one commit with: git commit --no-verify"
echo "Uninstall with:         git config --unset core.hooksPath"

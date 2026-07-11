#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="$ROOT/LITELLM_VERSION"
PYTHON="${LITELLM_UPDATE_PYTHON:-python3}"

latest_version="$("$PYTHON" - <<'PY'
from __future__ import annotations

import json
import urllib.request

request = urllib.request.Request(
    "https://pypi.org/pypi/litellm/json",
    headers={"Accept": "application/json", "User-Agent": "litellm-menu-version-check"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.load(response)
version = payload.get("info", {}).get("version")
if not isinstance(version, str) or not version.strip():
    raise SystemExit("PyPI did not return a LiteLLM version")
print(version.strip())
PY
)"

current_version="$(tr -d '[:space:]' < "$LOCK_FILE" 2>/dev/null || true)"
if [[ "${1:-}" == "--check" ]]; then
  if [[ "$current_version" != "$latest_version" ]]; then
    echo "LiteLLM lock is stale: locked=${current_version:-missing}, latest=$latest_version" >&2
    exit 1
  fi
  echo "LiteLLM lock is current: $latest_version"
  exit 0
fi

if [[ $# -gt 0 ]]; then
  echo "usage: $0 [--check]" >&2
  exit 64
fi

printf '%s\n' "$latest_version" > "$LOCK_FILE"
echo "Updated LiteLLM lock: ${current_version:-missing} -> $latest_version"
echo "Rebuild/restart LiteLLM Menu to install and test the locked release version."

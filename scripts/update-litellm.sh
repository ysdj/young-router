#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${LITELLM_VERSION_FILE:-$ROOT/LITELLM_VERSION}"
PYTHON="${LITELLM_UPDATE_PYTHON:-python3}"
PYPI_JSON_URL="${LITELLM_PYPI_JSON_URL:-https://pypi.org/pypi/litellm/json}"

selection="$(LITELLM_PYPI_JSON_URL="$PYPI_JSON_URL" "$PYTHON" - <<'PY'
from __future__ import annotations

import json
import os
import re
import urllib.request

request = urllib.request.Request(
    os.environ["LITELLM_PYPI_JSON_URL"],
    headers={"Accept": "application/json", "User-Agent": "litellm-menu-version-check"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.load(response)


def stable_version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def has_universal_wheel(files: object) -> bool:
    if not isinstance(files, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("packagetype") == "bdist_wheel"
        and item.get("yanked") is not True
        and isinstance(item.get("filename"), str)
        and item["filename"].endswith("-py3-none-any.whl")
        for item in files
    )


releases = payload.get("releases")
if not isinstance(releases, dict):
    raise SystemExit("PyPI did not return LiteLLM release metadata")
stable_releases = [
    (parsed, version, files)
    for version, files in releases.items()
    if isinstance(version, str) and (parsed := stable_version(version)) is not None
]
if not stable_releases:
    raise SystemExit("PyPI did not return a stable LiteLLM version")
latest_pypi_version = max(stable_releases)[1]
compatible_releases = [
    (parsed, version)
    for parsed, version, files in stable_releases
    if has_universal_wheel(files)
]
if not compatible_releases:
    raise SystemExit("PyPI did not return a stable LiteLLM release with a universal wheel")
latest_compatible_version = max(compatible_releases)[1]
print(f"{latest_compatible_version}\t{latest_pypi_version}")
PY
)"
IFS=$'\t' read -r latest_version latest_pypi_version <<<"$selection"

if [[ "$latest_version" != "$latest_pypi_version" ]]; then
  echo "Latest LiteLLM $latest_pypi_version has no universal macOS-compatible wheel; using $latest_version." >&2
fi

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

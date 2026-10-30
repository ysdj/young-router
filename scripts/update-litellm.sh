#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${LITELLM_UPDATE_PYTHON:-python3}"

exec "$PYTHON" "$ROOT/scripts/update_litellm.py" "$@"

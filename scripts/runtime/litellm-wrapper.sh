#!/usr/bin/env bash
set -euo pipefail

RUNTIME_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="$RUNTIME_ROOT/site-packages${PYTHONPATH:+:$PYTHONPATH}"
exec "$RUNTIME_ROOT/python/bin/python3.12" \
  -c 'from litellm import run_server; run_server()' "$@"

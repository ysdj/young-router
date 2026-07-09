#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT"
/bin/bash "$ROOT/service.sh" start
/bin/bash "$ROOT/service.sh" tail

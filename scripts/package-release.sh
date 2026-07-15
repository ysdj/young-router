#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
BUILD_NUMBER="$(tr -d '[:space:]' < "$ROOT/BUILD_NUMBER")"
LITELLM_VERSION="$(tr -d '[:space:]' < "$ROOT/LITELLM_VERSION")"
ARCH="$(uname -m)"
OUTPUT="${1:-$ROOT/artifacts/litellm-menu-$VERSION-$BUILD_NUMBER-macos-$ARCH.tar.zst}"
UV_BIN="${LITELLM_UV_BIN:-$(command -v uv 2>/dev/null || true)}"
ZSTD_BIN="${LITELLM_ZSTD_BIN:-$(command -v zstd 2>/dev/null || true)}"
RUNTIME_SOURCE="${LITELLM_RELEASE_RUNTIME_SOURCE:-}"

if [[ "$ARCH" != "arm64" ]]; then
  echo "Release packaging currently requires an Apple silicon build host." >&2
  exit 1
fi
if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
  echo "Missing uv. Install uv or set LITELLM_UV_BIN." >&2
  exit 1
fi
if [[ -z "$ZSTD_BIN" || ! -x "$ZSTD_BIN" ]]; then
  echo "Missing zstd. Install zstd or set LITELLM_ZSTD_BIN." >&2
  exit 1
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/litellm-menu-release.XXXXXX")"
SMOKE_PID=""
cleanup() {
  if [[ "$SMOKE_PID" =~ ^[0-9]+$ ]]; then
    kill "$SMOKE_PID" >/dev/null 2>&1 || true
    wait "$SMOKE_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT
APP="$WORK_DIR/LiteLLM Menu.app"
APP_RES="$APP/Contents/Resources/App"
RUNTIME="$APP_RES/runtime"
PYTHON_INSTALLS="$WORK_DIR/python-installs"
SITE_PACKAGES="$RUNTIME/site-packages"

LITELLM_APP_PATH="$APP" LITELLM_UV_BIN="$UV_BIN" "$ROOT/mac_menu/build.sh" >/dev/null
mkdir -p "$RUNTIME/bin" "$SITE_PACKAGES"

if [[ -n "$RUNTIME_SOURCE" ]]; then
  if [[ ! -x "$RUNTIME_SOURCE/bin/python" \
    || ! -x "$RUNTIME_SOURCE/bin/litellm" \
    || ! -f "$RUNTIME_SOURCE/LITELLM_VERSION" \
    || "$(tr -d '[:space:]' < "$RUNTIME_SOURCE/LITELLM_VERSION")" != "$LITELLM_VERSION" ]]; then
    echo "The supplied release runtime is missing or does not match LiteLLM $LITELLM_VERSION." >&2
    exit 1
  fi
  rsync -a "$RUNTIME_SOURCE/" "$RUNTIME/"
else
  mkdir -p "$PYTHON_INSTALLS"
  UV_PYTHON_INSTALL_DIR="$PYTHON_INSTALLS" \
    "$UV_BIN" python install 3.12 >/dev/null
  PYTHON_SOURCE="$(printf '%s\n' "$PYTHON_INSTALLS"/cpython-3.12.*-macos-aarch64-none | head -n 1)"
  if [[ ! -x "$PYTHON_SOURCE/bin/python3.12" ]]; then
    echo "uv did not install the expected macOS arm64 Python 3.12 runtime." >&2
    exit 1
  fi
  mv "$PYTHON_SOURCE" "$RUNTIME/python"

  "$UV_BIN" pip install \
    --python "$RUNTIME/python/bin/python3.12" \
    --target "$SITE_PACKAGES" \
    "litellm==$LITELLM_VERSION" \
    -r "$ROOT/scripts/runtime-requirements.txt" >/dev/null
fi

cp "$ROOT/scripts/runtime/python-wrapper.sh" "$RUNTIME/bin/python"
cp "$ROOT/scripts/runtime/litellm-wrapper.sh" "$RUNTIME/bin/litellm"
cp "$ROOT/LITELLM_VERSION" "$RUNTIME/LITELLM_VERSION"
chmod 0755 "$RUNTIME/bin/python" "$RUNTIME/bin/litellm"

rm -rf \
  "$SITE_PACKAGES/bin" \
  "$RUNTIME/python/include" \
  "$RUNTIME/python/share/man" \
  "$RUNTIME/python/lib/pkgconfig" \
  "$RUNTIME/python/lib/python3.12/config-3.12-darwin" \
  "$RUNTIME/python/lib/python3.12/ensurepip" \
  "$RUNTIME/python/lib/python3.12/idlelib" \
  "$RUNTIME/python/lib/python3.12/site-packages/pip" \
  "$RUNTIME/python/lib/python3.12/site-packages"/pip-*.dist-info \
  "$RUNTIME/python/lib/python3.12/tkinter" \
  "$RUNTIME/python/lib/python3.12/lib-dynload"/_tkinter.* \
  "$RUNTIME/python/lib/tcl9" \
  "$RUNTIME/python/lib/tcl9.0" \
  "$RUNTIME/python/lib/tk9.0" \
  "$RUNTIME/python/lib/itcl4.3.5" \
  "$RUNTIME/python/lib/thread3.0.4" \
  "$RUNTIME/python/lib/libtcl9.0.dylib" \
  "$RUNTIME/python/lib/libtcl9tk9.0.dylib" \
  "$RUNTIME/python/lib/libpython3.12.dylib"

BUILD_ROOT="$WORK_DIR" RUNTIME_PYTHON_ROOT="$RUNTIME/python" \
  "$RUNTIME/bin/python" - <<'PY'
from __future__ import annotations

import os
import re
from pathlib import Path

path = Path(os.environ["RUNTIME_PYTHON_ROOT"]) / "lib/python3.12/_sysconfigdata__darwin_darwin.py"
text = path.read_text(encoding="utf-8")
build_root = os.environ["BUILD_ROOT"].rstrip("/")
normalized_root = re.sub(r"/+", "/", build_root)
resolved_root = str(Path(build_root).resolve())
prefixes = {
    build_root,
    normalized_root,
    resolved_root,
    resolved_root.removeprefix("/private"),
}
for prefix in sorted(prefixes, key=len, reverse=True):
    text = text.replace(prefix, "/opt/litellm-menu-build")
path.write_text(text, encoding="utf-8")
PY

while IFS= read -r cache_dir; do
  rm -rf "$cache_dir"
done < <(find "$RUNTIME" -type d -name __pycache__ -print)
find "$RUNTIME" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

EXPECTED_LITELLM_VERSION="$LITELLM_VERSION" \
  PYTHONDONTWRITEBYTECODE=1 "$RUNTIME/bin/python" - <<'PY'
from importlib import metadata
import os

assert metadata.version("litellm") == os.environ["EXPECTED_LITELLM_VERSION"]
import litellm.proxy.proxy_server  # noqa: F401
PY
PYTHONDONTWRITEBYTECODE=1 "$RUNTIME/bin/litellm" --help >/dev/null

CONFIG_EDITOR_RUNTIME="$WORK_DIR/config-editor-runtime"
mkdir -p "$CONFIG_EDITOR_RUNTIME"
cp "$APP_RES/config.example.yaml" "$CONFIG_EDITOR_RUNTIME/config.yaml"
PYTHONDONTWRITEBYTECODE=1 "$RUNTIME/bin/python" \
  "$APP_RES/config_editor.py" --config "$CONFIG_EDITOR_RUNTIME/config.yaml" load >/dev/null
PYTHONPATH="$APP_RES" PYTHONDONTWRITEBYTECODE=1 "$RUNTIME/bin/python" \
  - "$CONFIG_EDITOR_RUNTIME/config.yaml" <<'PY'
import pathlib
import sys

from config_editor_core.api import save_config
from config_editor_core.load import load_config

path = pathlib.Path(sys.argv[1])
payload = load_config(path)
result = save_config([], path, payload["revision"])
assert result["providers"] == 0
assert load_config(path)["providers"] == []
PY

SMOKE_PORT="$("$RUNTIME/bin/python" - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
PYTHONPATH="$APP_RES" \
  LITELLM_MASTER_KEY="sk-local-litellm" \
  LITELLM_MENU_PROXY_PROCESS=1 \
  LITELLM_PROXY_TELEMETRY=False \
  "$RUNTIME/bin/litellm" \
    --config "$APP_RES/config.example.yaml" \
    --host 127.0.0.1 \
    --port "$SMOKE_PORT" \
    --num_workers 1 \
    --telemetry False \
    --run_gunicorn >"$WORK_DIR/runtime-smoke.log" 2>&1 &
SMOKE_PID="$!"
SMOKE_HEALTH=""
for _ in {1..120}; do
  SMOKE_HEALTH="$(curl -fsS --max-time 0.2 \
    "http://127.0.0.1:$SMOKE_PORT/health/liveliness" 2>/dev/null || true)"
  [[ -n "$SMOKE_HEALTH" ]] && break
  kill -0 "$SMOKE_PID" >/dev/null 2>&1 || break
  sleep 0.25
done
if [[ -z "$SMOKE_HEALTH" ]]; then
  echo "Bundled LiteLLM runtime failed its worker health check." >&2
  tail -n 120 "$WORK_DIR/runtime-smoke.log" >&2
  exit 1
fi
kill "$SMOKE_PID" >/dev/null 2>&1 || true
wait "$SMOKE_PID" 2>/dev/null || true
SMOKE_PID=""

# The release runtime is self-contained; omitting uv keeps Homebrew downloads smaller.
rm -f "$APP_RES/bin/uv"
codesign --force --deep --sign - "$APP" >/dev/null
codesign --verify --deep --strict "$APP"

mkdir -p "$(dirname "$OUTPUT")"
TEMP_OUTPUT="$OUTPUT.tmp"
rm -f "$TEMP_OUTPUT"
COPYFILE_DISABLE=1 tar -cf - -C "$WORK_DIR" "LiteLLM Menu.app" \
  | "$ZSTD_BIN" -q -T0 -19 -o "$TEMP_OUTPUT"
mv "$TEMP_OUTPUT" "$OUTPUT"

printf '%s\n' "$OUTPUT"
shasum -a 256 "$OUTPUT"

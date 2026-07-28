#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVELOPER_DIR="${DEVELOPER_DIR:-}"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
BUILD_NUMBER="$(tr -d '[:space:]' < "$ROOT/BUILD_NUMBER")"
ARCH="$(uname -m)"
OUTPUT="${1:-$ROOT/artifacts/litellm-menu-$VERSION-$BUILD_NUMBER-macos-$ARCH.tar.zst}"
UV_BIN="${LITELLM_UV_BIN:-$(command -v uv 2>/dev/null || true)}"
ZSTD_BIN="${LITELLM_ZSTD_BIN:-$(command -v zstd 2>/dev/null || true)}"

if [[ "$ARCH" != "arm64" ]]; then
  echo "Release packaging currently requires an Apple silicon build host." >&2
  exit 1
fi
if [[ -z "$DEVELOPER_DIR" && -x /Applications/Xcode-beta.app/Contents/Developer/usr/bin/xcodebuild ]]; then
  DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer
fi
if [[ -n "$DEVELOPER_DIR" ]]; then
  [[ -x "$DEVELOPER_DIR/usr/bin/xcodebuild" ]] || {
    echo "Invalid DEVELOPER_DIR: $DEVELOPER_DIR" >&2
    exit 1
  }
  export DEVELOPER_DIR
elif ! xcodebuild -version >/dev/null 2>&1; then
  echo "A complete Xcode installation is required for release packaging." >&2
  exit 1
fi
for command in pnpm plutil codesign rsync; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "Missing release tool: $command" >&2
    exit 1
  }
done
if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
  echo "Missing uv. Install uv or set LITELLM_UV_BIN." >&2
  exit 1
fi
if [[ -z "$ZSTD_BIN" || ! -x "$ZSTD_BIN" ]]; then
  echo "Missing zstd. Install zstd or set LITELLM_ZSTD_BIN." >&2
  exit 1
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/litellm-menu-release.XXXXXX")"
cleanup() {
  [[ ! -d "$WORK_DIR" ]] || rm -rf "$WORK_DIR"
}
trap cleanup EXIT

APP="$WORK_DIR/LiteLLM Menu.app"
CORE="$APP/Contents/Resources/Core"
(
  cd "$ROOT/rn"
  LITELLM_MENU_MACOS_OUTPUT="$APP" \
    LITELLM_UV_BIN="$UV_BIN" \
    pnpm run build:macos
)

test -x "$APP/Contents/MacOS/LiteLLMMenu"
test -x "$CORE/runtime/bin/python"
test -x "$CORE/runtime/bin/litellm"
test -x "$CORE/bin/vision_ocr"
test -f "$CORE/litellm_menu/core/__main__.py"
test -f "$CORE/sitecustomize.py"
plutil -lint "$APP/Contents/Info.plist"
test "$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist")" = "$VERSION"
test "$(plutil -extract CFBundleVersion raw "$APP/Contents/Info.plist")" = "$BUILD_NUMBER"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$CORE" \
  "$CORE/runtime/bin/python" -c \
  'import litellm.proxy.proxy_server, litellm_menu.core, codex_config, config_editor_core, configuration_package, webdav.core'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$CORE" \
  "$CORE/runtime/bin/python" -m litellm_menu.core --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 "$CORE/runtime/bin/litellm" --help >/dev/null
codesign --verify --deep --strict --verbose=2 "$APP"

# Prove that the bundled Core does not depend on its build location or a venv.
RELOCATED_CORE="$WORK_DIR/relocated/Core"
mkdir -p "$(dirname "$RELOCATED_CORE")"
rsync -a "$CORE/" "$RELOCATED_CORE/"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELOCATED_CORE" \
  "$RELOCATED_CORE/runtime/bin/python" -c \
  'import litellm.proxy.proxy_server, litellm_menu.core'
PYTHONDONTWRITEBYTECODE=1 LITELLM_MENU_PROXY_PROCESS=1 PYTHONPATH="$RELOCATED_CORE" \
  "$RELOCATED_CORE/runtime/bin/python" -c \
  'from litellm.proxy.types_utils.utils import get_instance_fn; callback = get_instance_fn("litellm_menu.callbacks.image_generation_routing_hook", config_file_path="runtime/config.yaml"); assert callback.__class__.__name__ == "LiteLLMMenuHook"'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELOCATED_CORE" \
  "$RELOCATED_CORE/runtime/bin/python" -m litellm_menu.core --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 "$RELOCATED_CORE/runtime/bin/litellm" --help >/dev/null
rm -rf "$WORK_DIR/relocated"

mkdir -p "$(dirname "$OUTPUT")"
TEMP_OUTPUT="$OUTPUT.tmp"
rm -f "$TEMP_OUTPUT"
COPYFILE_DISABLE=1 tar --no-xattrs -cf - -C "$WORK_DIR" "LiteLLM Menu.app" \
  | "$ZSTD_BIN" -q -T0 -19 -o "$TEMP_OUTPUT"
ARCHIVE_LIST="$WORK_DIR/archive-list.txt"
"$ZSTD_BIN" -q -d -c "$TEMP_OUTPUT" | tar -tf - >"$ARCHIVE_LIST"
grep -Fq "LiteLLM Menu.app/Contents/Resources/Core/runtime/bin/python" "$ARCHIVE_LIST"
grep -Fq "LiteLLM Menu.app/Contents/Resources/Core/bin/vision_ocr" "$ARCHIVE_LIST"
if grep -Eq 'LiteLLM Menu\.app/Contents/Resources/Core/(\.venv|venv)(/|$)|Contents/Resources/App/' "$ARCHIVE_LIST"; then
  echo "Release archive contains a development runtime or legacy bundle path." >&2
  exit 1
fi
mv "$TEMP_OUTPUT" "$OUTPUT"

printf '%s\n' "$OUTPUT"
shasum -a 256 "$OUTPUT"

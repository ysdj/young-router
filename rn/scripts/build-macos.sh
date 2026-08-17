#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ROOT="$(cd "$ROOT/.." && pwd)"
APP_ROOT="$ROOT/apps/macos"
DEVELOPER_DIR="${DEVELOPER_DIR:-}"
UV_BIN="${LITELLM_UV_BIN:-$(command -v uv 2>/dev/null || true)}"
RUNTIME_SOURCE="${LITELLM_MENU_CORE_RUNTIME_SOURCE:-${LITELLM_RELEASE_RUNTIME_SOURCE:-}}"
ARCH="$(uname -m)"
RUNTIME_WORK="$(mktemp -d "${TMPDIR:-/tmp}/litellm-menu-rn-runtime.XXXXXX")"
cleanup() {
  [[ -z "$RUNTIME_WORK" ]] || rm -rf "$RUNTIME_WORK"
}
trap cleanup EXIT
cd "$ROOT"

copy_tree() {
  local source="$1"
  local destination="$2"
  mkdir -p "$destination"
  # On APFS, -c uses clonefile and falls back to a regular copy across volumes.
  cp -ac "$source/." "$destination/"
}

if [[ -z "$DEVELOPER_DIR" && -x /Applications/Xcode-beta.app/Contents/Developer/usr/bin/xcodebuild ]]; then
  DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer
fi
if [[ -n "$DEVELOPER_DIR" ]]; then
  [[ -x "$DEVELOPER_DIR/usr/bin/xcodebuild" ]] || {
    echo "Invalid DEVELOPER_DIR: $DEVELOPER_DIR" >&2
    exit 3
  }
  export DEVELOPER_DIR
elif ! xcodebuild -version >/dev/null 2>&1; then
  echo "A complete Xcode installation is required to build the React Native macOS host." >&2
  exit 3
fi

command -v pnpm >/dev/null 2>&1 || {
  echo "pnpm is required to build the React Native macOS host." >&2
  exit 1
}

if [[ "${LITELLM_MENU_LITELLM_VERSION_UPDATED:-}" != "1" ]]; then
  "$PROJECT_ROOT/scripts/update-litellm.sh"
  export LITELLM_MENU_LITELLM_VERSION_UPDATED=1
fi

NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
if [[ "$NODE_MAJOR" -lt 22 ]]; then
  echo "Node.js 22 or later is required to build the React Native 0.85 macOS host." >&2
  exit 1
fi

export RCT_USE_RN_DEP=0
export RCT_USE_PREBUILT_RNCORE=0
export RCT_BUILD_HERMES_FROM_SOURCE=true
export RCT_HERMES_V1_ENABLED=1
node scripts/bootstrap-rnmacos-085.mjs
node scripts/verify-rnmacos-085.mjs --check-build-env

if [[ ! -d "$APP_ROOT/macos" ]]; then
  echo "React Native macOS host project is missing at rn/apps/macos/macos." >&2
  exit 2
fi

command -v pod >/dev/null 2>&1 || {
  echo "CocoaPods is required to build the React Native macOS host." >&2
  exit 3
}
for tool in rsync codesign strip file; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "Missing required macOS bundle tool: $tool" >&2
    exit 3
  }
done
SWIFTC_BIN="$(xcrun --sdk macosx --find swiftc 2>/dev/null || true)"
[[ -n "$SWIFTC_BIN" && -x "$SWIFTC_BIN" ]] || {
  echo "Xcode swiftc is required to build the bundled Vision OCR helper." >&2
  exit 3
}
MACOS_SDK="$(xcrun --sdk macosx --show-sdk-path 2>/dev/null || true)"
[[ -n "$MACOS_SDK" && -d "$MACOS_SDK" ]] || {
  echo "Xcode macOS SDK is required to build the bundled Vision OCR helper." >&2
  exit 3
}
if [[ -z "$RUNTIME_SOURCE" && ( -z "$UV_BIN" || ! -x "$UV_BIN" ) ]]; then
  echo "uv is required to build the self-contained macOS Core runtime." >&2
  exit 3
fi
[[ -x /usr/libexec/PlistBuddy ]] || {
  echo "Missing required macOS bundle tool: /usr/libexec/PlistBuddy" >&2
  exit 3
}
# These checks only read the shared/macOS JS/TS workspace while CocoaPods
# prepares the native workspace. Windows codegen remains checked by its build
# and the cross-platform CI job.
pnpm run check:macos &
STATIC_CHECKS_PID=$!
if [[ "${LITELLM_MENU_REFRESH_PODS:-0}" == "1" \
  || -n "${CI:-}" \
  || ! -d "$APP_ROOT/macos/Pods" \
  || ! -d "$APP_ROOT/macos/LiteLLMMenu.xcworkspace" \
  || ! -f "$APP_ROOT/macos/Podfile.lock" ]]; then
  if ! pod install --project-directory="$APP_ROOT/macos"; then
    wait "$STATIC_CHECKS_PID" || true
    exit 1
  fi
else
  printf '%s\n' "Reusing CocoaPods workspace (set LITELLM_MENU_REFRESH_PODS=1 after native dependency or codegen changes)."
fi
wait "$STATIC_CHECKS_PID"
RNMACOS_CLI="$ROOT/vendor/react-native-macos-0.85/packages/react-native/cli.js"
(
  cd "$APP_ROOT"
  node "$RNMACOS_CLI" build-macos --project-path macos --mode Release
)

APP="$(xcodebuild \
  -workspace "$APP_ROOT/macos/LiteLLMMenu.xcworkspace" \
  -scheme LiteLLMMenu-macOS \
  -configuration Release \
  -showBuildSettings 2>/dev/null \
  | awk -F ' = ' '/TARGET_BUILD_DIR = / { target = $2 } /FULL_PRODUCT_NAME = / { product = $2 } END { if (target && product) print target "/" product }')"
if [[ -z "$APP" || ! -d "$APP" ]]; then
  echo "React Native macOS build did not produce LiteLLMMenu.app." >&2
  exit 4
fi

CORE="$APP/Contents/Resources/Core"
rm -rf "$CORE"
mkdir -p "$CORE"
for file in \
  codex_config.py \
  configuration_package.py \
  external_provider_import.py \
  remote_usage_logs.py \
  runtime_settings_io.py \
  sitecustomize.py
do
  cp "$PROJECT_ROOT/$file" "$CORE/$file"
done
for directory in litellm_menu config_editor_core webdav; do
  rsync -a \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '*.pyo' \
    "$PROJECT_ROOT/$directory/" "$CORE/$directory/"
done

if [[ -n "$RUNTIME_SOURCE" ]]; then
  if [[ ! -d "$RUNTIME_SOURCE/python" \
    || ! -d "$RUNTIME_SOURCE/site-packages" \
    || ! -x "$RUNTIME_SOURCE/bin/python" \
    || ! -x "$RUNTIME_SOURCE/bin/litellm" \
    || ! -x "$RUNTIME_SOURCE/python/bin/python3.12" ]]; then
    echo "LITELLM_MENU_CORE_RUNTIME_SOURCE must use the portable release-runtime layout, not a virtualenv." >&2
    exit 5
  fi
  copy_tree "$RUNTIME_SOURCE" "$CORE/runtime"
else
  case "$ARCH" in
    arm64) UV_RUNTIME="cpython-3.12-macos-aarch64-none" ;;
    x86_64) UV_RUNTIME="cpython-3.12-macos-x86_64-none" ;;
    *)
      echo "Unsupported macOS Core runtime architecture: $ARCH" >&2
      exit 5
      ;;
  esac
  mkdir -p "$RUNTIME_WORK/python-installs" "$CORE/runtime/bin" "$CORE/runtime/site-packages"
  UV_PYTHON_INSTALL_DIR="$RUNTIME_WORK/python-installs" \
    "$UV_BIN" python install "$UV_RUNTIME" --no-bin >/dev/null
  PYTHON_SOURCE="$(printf '%s\n' "$RUNTIME_WORK"/python-installs/cpython-3.12.*-macos-*64-none | head -n 1)"
  if [[ ! -x "$PYTHON_SOURCE/bin/python3.12" ]]; then
    echo "uv did not install the expected standalone macOS Python 3.12 runtime." >&2
    exit 5
  fi
  mv "$PYTHON_SOURCE" "$CORE/runtime/python"
  LITELLM_VERSION="$(tr -d '[:space:]' < "$PROJECT_ROOT/LITELLM_VERSION")"
  "$UV_BIN" pip install \
    --python "$CORE/runtime/python/bin/python3.12" \
    --target "$CORE/runtime/site-packages" \
    "litellm[proxy]==$LITELLM_VERSION" \
    "fastapi==0.140.3" \
    Pillow \
    PyYAML \
    ddgs >/dev/null
  cp "$PROJECT_ROOT/scripts/runtime/python-wrapper.sh" "$CORE/runtime/bin/python"
  cp "$PROJECT_ROOT/scripts/runtime/litellm-wrapper.sh" "$CORE/runtime/bin/litellm"
  cp "$PROJECT_ROOT/LITELLM_VERSION" "$CORE/runtime/LITELLM_VERSION"
  chmod 0755 "$CORE/runtime/bin/python" "$CORE/runtime/bin/litellm"
fi
cp "$PROJECT_ROOT/LITELLM_VERSION" "$CORE/runtime/LITELLM_VERSION"

VISION_HELPER_SOURCE="$APP_ROOT/src/native/macos/VisionOCR.swift"
VISION_HELPER="$CORE/bin/vision_ocr"
mkdir -p "$CORE/bin"
"$SWIFTC_BIN" \
  -sdk "$MACOS_SDK" \
  -target "$ARCH-apple-macosx14.0" \
  -file-prefix-map "$PROJECT_ROOT=." \
  -debug-prefix-map "$PROJECT_ROOT=." \
  "$VISION_HELPER_SOURCE" \
  -o "$VISION_HELPER" \
  -framework Vision \
  -framework ImageIO \
  -framework CoreGraphics \
  -framework Foundation
[[ -x "$VISION_HELPER" ]] || {
  echo "The bundled Vision OCR helper is missing or not executable." >&2
  exit 5
}
if "$VISION_HELPER" >/dev/null 2>&1; then
  echo "The bundled Vision OCR helper accepted an invalid empty invocation." >&2
  exit 5
else
  VISION_HELPER_STATUS=$?
  [[ "$VISION_HELPER_STATUS" -eq 64 ]] || {
    echo "The bundled Vision OCR helper could not launch." >&2
    exit 5
  }
fi

[[ -f "$CORE/litellm_menu/core/__main__.py" ]] || {
  echo "Bundled Core launcher is missing." >&2
  exit 5
}
[[ -f "$CORE/config_editor_core/api.py" ]] || {
  echo "Bundled Core dependencies are incomplete." >&2
  exit 5
}
[[ -x "$CORE/runtime/bin/python" ]] || {
  echo "A self-contained Core runtime is required. Set LITELLM_MENU_CORE_RUNTIME_SOURCE." >&2
  exit 5
}
[[ -x "$CORE/runtime/bin/litellm" ]] || {
  echo "The bundled Core runtime does not contain the LiteLLM executable." >&2
  exit 5
}
[[ "$(tr -d '[:space:]' < "$CORE/runtime/LITELLM_VERSION")" == "$(tr -d '[:space:]' < "$PROJECT_ROOT/LITELLM_VERSION")" ]] || {
  echo "The bundled Core runtime does not contain the pinned LiteLLM release lock." >&2
  exit 5
}

# Native wheels carry large local symbol tables that are not needed at runtime.
# Strip only bundled site-package extensions; the outer app is signed again
# below after this transformation.
while IFS= read -r -d '' binary; do
  case "$(file -b "$binary")" in
    Mach-O*)
      strip -x -S "$binary" >/dev/null 2>&1
      # strip rewrites the Mach-O pages and invalidates the wheel's embedded
      # signature.  Sign the rewritten extension itself; signing only the
      # outer app does not make macOS accept these pages when a worker imports
      # the module later.
      codesign --force --sign - "$binary" >/dev/null
      ;;
  esac
done < <(find "$CORE/runtime/site-packages" -type f \( -name '*.so' -o -name '*.dylib' \) -print0)

# A full-tree compileall pass adds roughly 100 MB of caches, most of which
# this app never imports. Remove stale caches before the real startup smoke
# imports populate only the modules on the active path.
find "$CORE/litellm_menu" "$CORE/runtime/site-packages" \
  \( -type d -name __pycache__ -prune -exec rm -rf {} + \) \
  -o \( -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \)

# Import the exact proxy startup path as a packaging smoke test. This also
# verifies the callback fallback in the same environment used by the child.
# The package already carries LiteLLM's fallback cost map, so this must not
# wait on a remote catalog refresh during a local build.
export LITELLM_LOCAL_MODEL_COST_MAP=true
PYTHONDONTWRITEBYTECODE=0 \
PYTHONPATH="$CORE:$CORE/runtime/site-packages" \
LITELLM_MENU_PROXY_PROCESS=1 \
LITELLM_TEMPLATE_ROOT="$CORE" \
  "$CORE/runtime/python/bin/python3.12" -c '
from litellm import run_server
from litellm.proxy.proxy_server import app
from litellm.proxy.types_utils.utils import get_instance_fn
from gunicorn.app.base import BaseApplication
from uvicorn.workers import UvicornWorker

callback = get_instance_fn(
    "litellm_menu.callbacks.image_generation_routing_hook",
    config_file_path="runtime/config.yaml",
)
assert app is not None
assert callback.__class__.__name__ == "LiteLLMMenuHook"
assert run_server is not None and BaseApplication is not None and UvicornWorker is not None
'
[[ -f "$CORE/runtime/site-packages/litellm/__pycache__/__init__.cpython-312.pyc" ]] || {
  echo "The bundled Core startup bytecode could not be generated." >&2
  exit 5
}

PYTHONDONTWRITEBYTECODE=0 PYTHONPATH="$CORE" "$CORE/runtime/bin/python" -c 'import litellm.proxy.proxy_server, litellm_menu.core, litellm_menu.core.__main__, litellm_menu.macos_proxy, codex_config, config_editor_core, configuration_package, external_provider_import, webdav.core'
PYTHONDONTWRITEBYTECODE=1 "$CORE/runtime/bin/litellm" --help >/dev/null
PORTABLE_SMOKE="$RUNTIME_WORK/portable-core"
copy_tree "$CORE" "$PORTABLE_SMOKE"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PORTABLE_SMOKE" "$PORTABLE_SMOKE/runtime/bin/python" -c 'import litellm.proxy.proxy_server, litellm_menu.core'
PYTHONDONTWRITEBYTECODE=1 LITELLM_MENU_PROXY_PROCESS=1 PYTHONPATH="$PORTABLE_SMOKE" \
  "$PORTABLE_SMOKE/runtime/bin/python" -c \
  'from litellm.proxy.types_utils.utils import get_instance_fn; callback = get_instance_fn("litellm_menu.callbacks.image_generation_routing_hook", config_file_path="runtime/config.yaml"); assert callback.__class__.__name__ == "LiteLLMMenuHook"'
PYTHONDONTWRITEBYTECODE=1 "$PORTABLE_SMOKE/runtime/bin/litellm" --help >/dev/null

# The smoke imports above deliberately exercise bytecode generation, but
# __pycache__ entries are not part of AppKit's signed resource contract.
# Remove them after validation so strict codesign verification cannot see
# unsealed cache files in the finished app bundle.
find "$CORE" \
  \( -type d -name __pycache__ -prune -exec rm -rf {} + \) \
  -o \( -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \)

VERSION="$(tr -d '[:space:]' < "$PROJECT_ROOT/VERSION")"
BUILD_NUMBER="$(tr -d '[:space:]' < "$PROJECT_ROOT/BUILD_NUMBER")"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist" >/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$APP/Contents/Info.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUMBER" "$APP/Contents/Info.plist" >/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $BUILD_NUMBER" "$APP/Contents/Info.plist" >/dev/null
if [[ -n "${LITELLM_MENU_MACOS_BUNDLE_IDENTIFIER:-}" ]]; then
  echo "LITELLM_MENU_MACOS_BUNDLE_IDENTIFIER is unsupported: every LiteLLM Menu build must use the production instance identity." >&2
  exit 6
fi
if [[ -n "${LITELLM_MENU_MACOS_DISPLAY_NAME:-}" \
  || -n "${LITELLM_MENU_MACOS_ROUTE_SCHEME:-}" \
  || -n "${LITELLM_MENU_MACOS_PREVIEW_PROFILE_ROOT:-}" \
  || -n "${LITELLM_MENU_MACOS_PREVIEW_PORT:-}" ]]; then
  PREVIEW_BUNDLE_IDENTIFIER="menu.litellm.menu"
  PREVIEW_DISPLAY_NAME="${LITELLM_MENU_MACOS_DISPLAY_NAME:-LiteLLM Menu Preview}"
  PREVIEW_ROUTE_SCHEME="${LITELLM_MENU_MACOS_ROUTE_SCHEME:-litellm-menu-preview}"
  PREVIEW_PROFILE_ROOT="${LITELLM_MENU_MACOS_PREVIEW_PROFILE_ROOT:-}"
  PREVIEW_PORT="${LITELLM_MENU_MACOS_PREVIEW_PORT:-}"
  [[ "$PREVIEW_BUNDLE_IDENTIFIER" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] \
    && [[ "$PREVIEW_DISPLAY_NAME" != *$'\n'* ]] \
    && [[ "$PREVIEW_ROUTE_SCHEME" =~ ^[A-Za-z][A-Za-z0-9.-]*$ ]] || {
      echo "Invalid macOS preview bundle metadata." >&2
      exit 6
    }
  if [[ -n "$PREVIEW_PROFILE_ROOT" ]]; then
    [[ "$PREVIEW_PROFILE_ROOT" = /* ]] \
      && [[ "$PREVIEW_PROFILE_ROOT" != *$'\n'* ]] \
      && [[ "$PREVIEW_PORT" =~ ^[0-9]+$ ]] \
      && (( PREVIEW_PORT >= 1 && PREVIEW_PORT <= 65535 )) || {
        echo "A preview profile root requires an absolute path and valid port." >&2
        exit 6
      }
  fi
  /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $PREVIEW_BUNDLE_IDENTIFIER" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $PREVIEW_DISPLAY_NAME" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleName $PREVIEW_DISPLAY_NAME" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :LiteLLMMenuRouteScheme $PREVIEW_ROUTE_SCHEME" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleURLTypes:0:CFBundleURLName $PREVIEW_BUNDLE_IDENTIFIER.routes" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleURLTypes:0:CFBundleURLSchemes:0 $PREVIEW_ROUTE_SCHEME" "$APP/Contents/Info.plist"
  if [[ -n "$PREVIEW_PROFILE_ROOT" ]]; then
    /usr/libexec/PlistBuddy -c "Delete :LiteLLMMenuPreviewProfileRoot" "$APP/Contents/Info.plist" >/dev/null 2>&1 || true
    /usr/libexec/PlistBuddy -c "Add :LiteLLMMenuPreviewProfileRoot string $PREVIEW_PROFILE_ROOT" "$APP/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Delete :LiteLLMMenuPreviewPort" "$APP/Contents/Info.plist" >/dev/null 2>&1 || true
    /usr/libexec/PlistBuddy -c "Add :LiteLLMMenuPreviewPort string $PREVIEW_PORT" "$APP/Contents/Info.plist"
  fi
fi
codesign --force --deep --sign - "$APP" >/dev/null
codesign --verify --deep --strict --verbose=2 "$APP"

if [[ -n "${LITELLM_MENU_MACOS_OUTPUT:-}" ]]; then
  OUTPUT="$LITELLM_MENU_MACOS_OUTPUT"
  [[ "$OUTPUT" = /* ]] || OUTPUT="$ROOT/$OUTPUT"
  [[ "$OUTPUT" == *.app ]] || {
    echo "LITELLM_MENU_MACOS_OUTPUT must name an .app bundle." >&2
    exit 6
  }
  mkdir -p "$(dirname "$OUTPUT")"
  OUTPUT="$(cd "$(dirname "$OUTPUT")" && pwd -P)/$(basename "$OUTPUT")"
  case "$OUTPUT" in
    /|"$HOME"|"$PROJECT_ROOT"|"$ROOT")
      echo "Refusing unsafe LITELLM_MENU_MACOS_OUTPUT: $OUTPUT" >&2
      exit 6
      ;;
  esac
  STAGED_OUTPUT="$(dirname "$OUTPUT")/.LiteLLMMenu.$$.app"
  rm -rf "$STAGED_OUTPUT"
  copy_tree "$APP" "$STAGED_OUTPUT"
  rm -rf "$OUTPUT"
  mv "$STAGED_OUTPUT" "$OUTPUT"
  APP="$OUTPUT"
fi

# Xcode can reuse an incrementally built bundle while preserving the app
# directory's old modification time. Finder keys its displayed icon cache to
# that bundle metadata, so mark every finished artifact as newly built even
# when the version and output path stay unchanged.
touch "$APP"

printf '%s\n' "$APP"

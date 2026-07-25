#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${LITELLM_APP_PATH:-/Applications/LiteLLM Menu.app}"
APP_PARENT="$(dirname "$APP")"
APP_NAME="$(basename "$APP" .app)"
STAGING_APP="$APP_PARENT/.${APP_NAME}.build.$$.app"
BACKUP_APP="${LITELLM_BACKUP_APP_PATH:-$APP_PARENT/.${APP_NAME}.previous.$$.app}"
BUILD_APP="$STAGING_APP"
APP_RES="$BUILD_APP/Contents/Resources/App"
KEEP_BACKUP="${LITELLM_KEEP_BACKUP:-0}"
ICON="$ROOT/mac_menu/LiteLLMMenu.icns"
ICON_GENERATOR="$ROOT/mac_menu/generate_app_icon.swift"
UV_BIN="${LITELLM_UV_BIN:-$(command -v uv 2>/dev/null || true)}"
MACOS_DEPLOYMENT_TARGET="${LITELLM_MACOS_DEPLOYMENT_TARGET:-13.0}"
SWIFT_TARGET="${LITELLM_SWIFT_TARGET:-$(uname -m)-apple-macosx$MACOS_DEPLOYMENT_TARGET}"

if [[ "$KEEP_BACKUP" != "0" && "$KEEP_BACKUP" != "1" ]]; then
  echo "Invalid LITELLM_KEEP_BACKUP value: $KEEP_BACKUP" >&2
  exit 64
fi

app_is_running() {
  local pid command app_binary="$APP/Contents/MacOS/LiteLLMMenu"
  [[ -x "$app_binary" ]] || return 1
  while read -r pid command; do
    [[ -n "$pid" ]] || continue
    [[ "$command" == "$app_binary"* || "$command" == *" $app_binary"* ]] && return 0
  done < <(ps axww -o pid= -o command=)
  return 1
}

guard_app_not_running() {
  if app_is_running; then
    echo "LiteLLM Menu is running from $APP; refusing to replace its bundle in place." >&2
    echo "Use '$ROOT/app.sh restart --disruptive' from a maintenance shell so the current menu app exits cleanly before rebuild." >&2
    return 1
  fi
}

guard_app_not_running || exit 1

DEPLOYMENT_SWAPPED=0

cleanup() {
  rm -rf "$STAGING_APP"
  if (( DEPLOYMENT_SWAPPED == 0 )) && [[ -e "$BACKUP_APP" ]]; then
    if [[ ! -e "$APP" ]]; then
      mv "$BACKUP_APP" "$APP"
    else
      rm -rf "$BACKUP_APP"
    fi
  elif (( DEPLOYMENT_SWAPPED == 1 )) && [[ "$KEEP_BACKUP" == "0" ]]; then
    rm -rf "$BACKUP_APP"
  fi
}
trap cleanup EXIT INT TERM

for tool in swift swiftc xcrun codesign plutil rsync; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Missing required macOS build tool: $tool" >&2
    echo "Install Xcode Command Line Tools with: xcode-select --install" >&2
    exit 1
  fi
done
if [[ ! -x /usr/libexec/PlistBuddy ]]; then
  echo "Missing required macOS build tool: /usr/libexec/PlistBuddy" >&2
  echo "Install Xcode Command Line Tools with: xcode-select --install" >&2
  exit 1
fi

if [[ ! "$MACOS_DEPLOYMENT_TARGET" =~ ^[0-9]+([.][0-9]+){1,2}$ ]]; then
  echo "Invalid macOS deployment target: $MACOS_DEPLOYMENT_TARGET" >&2
  exit 1
fi

verify_deployment_target() {
  local binary="$1" actual
  actual="$(xcrun vtool -show-build "$binary" | awk '$1 == "minos" { print $2; exit }')"
  if [[ "$actual" != "$MACOS_DEPLOYMENT_TARGET" ]]; then
    echo "Unexpected deployment target for $binary: expected $MACOS_DEPLOYMENT_TARGET, got ${actual:-unknown}" >&2
    exit 1
  fi
}

sync_version_to_plist() {
  local plist="$1" version build
  version="$(tr -d '[:space:]' < "$ROOT/VERSION")"
  build="$(tr -d '[:space:]' < "$ROOT/BUILD_NUMBER")"
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $version" "$plist" >/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $version" "$plist" >/dev/null
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $build" "$plist" >/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $build" "$plist" >/dev/null
}

if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
  echo "Missing uv. Install uv or set LITELLM_UV_BIN so the app can bootstrap Python on a clean macOS install." >&2
  exit 1
fi

if [[ ! -s "$ROOT/LITELLM_VERSION" ]]; then
  echo "Missing or empty LiteLLM version lock: $ROOT/LITELLM_VERSION" >&2
  exit 1
fi

if [[ ! -f "$ICON" || "$ICON_GENERATOR" -nt "$ICON" ]]; then
  /usr/bin/swift "$ICON_GENERATOR" "$ICON"
fi

mkdir -p "$APP_PARENT"
rm -rf "$STAGING_APP" "$BACKUP_APP"
mkdir -p "$BUILD_APP/Contents/MacOS" "$BUILD_APP/Contents/Resources" "$APP_RES/bin" "$APP_RES/scripts"
SWIFT_SOURCES=()
while IFS= read -r source_file; do
  SWIFT_SOURCES+=("$source_file")
done < <(find "$ROOT/mac_menu/Sources" -name '*.swift' -type f | sort)
swiftc \
  -target "$SWIFT_TARGET" \
  -file-prefix-map "$ROOT=." \
  -debug-prefix-map "$ROOT=." \
  "${SWIFT_SOURCES[@]}" \
  -o "$BUILD_APP/Contents/MacOS/LiteLLMMenu" \
  -framework Cocoa
cp "$ROOT/mac_menu/Info.plist" "$BUILD_APP/Contents/Info.plist"
sync_version_to_plist "$BUILD_APP/Contents/Info.plist"
cp "$ICON" "$BUILD_APP/Contents/Resources/LiteLLMMenu.icns"
for file in \
  service.sh \
  watch_config.sh \
  config_editor.py \
  codex_config.py \
  external_provider_import.py \
  provider_billing.py \
  browser_billing.py \
  remote_usage_logs.py \
  configuration_package.py \
  runtime_settings_io.py \
  webdav_sync.py \
  menu_status.py \
  sitecustomize.py \
  config.example.yaml \
  VERSION \
  BUILD_NUMBER \
  LITELLM_VERSION \
  scripts/smoke_websearch.py \
  scripts/smoke_responses_tool_bridge_compare.py
do
  cp "$ROOT/$file" "$APP_RES/$file"
done
for directory in service litellm_menu config_editor_core webdav
do
  mkdir -p "$APP_RES/$directory"
  /usr/bin/rsync -a \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '*.pyo' \
    "$ROOT/$directory/" "$APP_RES/$directory/"
done
cp "$UV_BIN" "$APP_RES/bin/uv"
swiftc \
  -target "$SWIFT_TARGET" \
  -file-prefix-map "$ROOT=." \
  -debug-prefix-map "$ROOT=." \
  "$ROOT/mac_menu/vision_ocr.swift" \
  -o "$APP_RES/bin/vision_ocr" \
  -framework Vision \
  -framework ImageIO \
  -framework CoreGraphics \
  -framework Foundation
chmod +x "$BUILD_APP/Contents/MacOS/LiteLLMMenu"
chmod +x "$APP_RES/service.sh" "$APP_RES/watch_config.sh" "$APP_RES/config_editor.py" "$APP_RES/codex_config.py" "$APP_RES/external_provider_import.py" "$APP_RES/provider_billing.py" "$APP_RES/browser_billing.py" "$APP_RES/remote_usage_logs.py" "$APP_RES/configuration_package.py" "$APP_RES/scripts/smoke_websearch.py" "$APP_RES/scripts/smoke_responses_tool_bridge_compare.py" "$APP_RES/bin/uv" "$APP_RES/bin/vision_ocr"
chmod +x "$APP_RES"/service/*.sh
verify_deployment_target "$BUILD_APP/Contents/MacOS/LiteLLMMenu"
verify_deployment_target "$APP_RES/bin/vision_ocr"
plutil -lint "$BUILD_APP/Contents/Info.plist" >/dev/null
codesign --force --deep --sign - "$BUILD_APP" >/dev/null
codesign --verify --deep --strict "$BUILD_APP"

guard_app_not_running || exit 1
if [[ -e "$APP" ]]; then
  mv "$APP" "$BACKUP_APP"
fi
if ! mv "$STAGING_APP" "$APP"; then
  [[ ! -e "$BACKUP_APP" ]] || mv "$BACKUP_APP" "$APP"
  echo "Could not replace $APP; the previous app was restored." >&2
  exit 1
fi
DEPLOYMENT_SWAPPED=1
if [[ "$KEEP_BACKUP" == "0" ]]; then
  rm -rf "$BACKUP_APP"
fi
echo "$APP"

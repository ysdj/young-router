#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${LITELLM_APP_PATH:-/Applications/LiteLLM Menu.app}"
APP_RES="$APP/Contents/Resources/App"
ICON="$ROOT/mac_menu/LiteLLMMenu.icns"
ICON_GENERATOR="$ROOT/mac_menu/generate_app_icon.swift"
UV_BIN="${LITELLM_UV_BIN:-$(command -v uv 2>/dev/null || true)}"

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

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$APP_RES/bin" "$APP_RES/scripts"
SWIFT_SOURCES=()
while IFS= read -r source_file; do
  SWIFT_SOURCES+=("$source_file")
done < <(find "$ROOT/mac_menu/Sources" -name '*.swift' -type f | sort)
swiftc "${SWIFT_SOURCES[@]}" -o "$APP/Contents/MacOS/LiteLLMMenu" -framework Cocoa
cp "$ROOT/mac_menu/Info.plist" "$APP/Contents/Info.plist"
sync_version_to_plist "$APP/Contents/Info.plist"
cp "$ICON" "$APP/Contents/Resources/LiteLLMMenu.icns"
rm -rf "$APP_RES"
mkdir -p "$APP_RES/bin" "$APP_RES/scripts"
for file in \
  service.sh \
  app.sh \
  run.sh \
  watch_config.sh \
  config_editor.py \
  codex_config.py \
  webdav_sync.py \
  route_trace_report.py \
  route_recovery_report.py \
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
cp -R "$ROOT/service" "$APP_RES/service"
cp -R "$ROOT/litellm_menu" "$APP_RES/litellm_menu"
cp -R "$ROOT/config_editor_core" "$APP_RES/config_editor_core"
cp -R "$ROOT/trace_report" "$APP_RES/trace_report"
cp -R "$ROOT/webdav" "$APP_RES/webdav"
cp "$UV_BIN" "$APP_RES/bin/uv"
swiftc "$ROOT/mac_menu/vision_ocr.swift" -o "$APP_RES/bin/vision_ocr" -framework Vision -framework ImageIO -framework CoreGraphics -framework Foundation
chmod +x "$APP/Contents/MacOS/LiteLLMMenu"
chmod +x "$APP_RES/service.sh" "$APP_RES/app.sh" "$APP_RES/run.sh" "$APP_RES/watch_config.sh" "$APP_RES/config_editor.py" "$APP_RES/route_trace_report.py" "$APP_RES/route_recovery_report.py" "$APP_RES/scripts/smoke_websearch.py" "$APP_RES/scripts/smoke_responses_tool_bridge_compare.py" "$APP_RES/bin/uv" "$APP_RES/bin/vision_ocr"
chmod +x "$APP_RES"/service/*.sh
plutil -lint "$APP/Contents/Info.plist" >/dev/null
codesign --force --deep --sign - "$APP" >/dev/null
echo "$APP"

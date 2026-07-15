#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${LITELLM_APP_PATH:-/Applications/LiteLLM Menu.app}"
BIN="$APP/Contents/MacOS/LiteLLMMenu"
INFO="$APP/Contents/Info.plist"
ICON="$APP/Contents/Resources/LiteLLMMenu.icns"
APP_RES="$APP/Contents/Resources/App"
CONTROL="$APP_RES/service.sh"
ACTION="${1:-open}"
if (( $# > 0 )); then
  shift
fi

RESOURCE_FILES=(
  service.sh
  app.sh
  run.sh
  watch_config.sh
  config_editor.py
  codex_config.py
  webdav_sync.py
  route_trace_report.py
  route_recovery_report.py
  sitecustomize.py
  config.example.yaml
  VERSION
  BUILD_NUMBER
  LITELLM_VERSION
  scripts/smoke_websearch.py
  scripts/smoke_responses_tool_bridge_compare.py
)

RESOURCE_DIRS=(
  config_editor_core
  service
  litellm_menu
  trace_report
  webdav
)

usage() {
  echo "usage: $0 {open|close|restart|version}" >&2
  echo "open/restart must prove the LiteLLM Menu app process first; service health alone is not Menu UI success." >&2
}

if [[ "$ACTION" != "open" && "$ACTION" != "close" && "$ACTION" != "restart" && "$ACTION" != "version" ]]; then
  usage
  exit 64
fi

require_control() {
  if [[ ! -x "$CONTROL" ]]; then
    echo "Missing app service script: $CONTROL" >&2
    exit 1
  fi
}

control() {
  require_control
  /bin/bash "$CONTROL" "$@"
}

app_pids() {
  local pid command
  [[ -x "$BIN" ]] || return 0
  ps axww -o pid= -o command= | while read -r pid command; do
    [[ -n "$pid" && "$command" == "$BIN"* ]] || continue
    printf '%s\n' "$pid"
  done
}

app_running() {
  [[ -n "$(app_pids)" ]]
}

file_mtime_epoch() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

app_pid_start_epoch() {
  local pid="$1" started
  started="$(ps -p "$pid" -o lstart= 2>/dev/null | awk '{$1=$1; print}')"
  [[ -n "$started" ]] || return 1
  date -j -f "%a %b %d %T %Y" "$started" "+%s" 2>/dev/null \
    || date -d "$started" "+%s" 2>/dev/null \
    || return 1
}

app_process_is_older_than_bundle() {
  local bin_mtime pid started
  [[ -x "$BIN" ]] || return 1
  bin_mtime="$(file_mtime_epoch "$BIN")"
  [[ "$bin_mtime" =~ ^[0-9]+$ && "$bin_mtime" -gt 0 ]] || return 1
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    started="$(app_pid_start_epoch "$pid" || true)"
    [[ "$started" =~ ^[0-9]+$ && "$started" -gt 0 ]] || continue
    if (( started < bin_mtime )); then
      return 0
    fi
  done < <(app_pids)
  return 1
}

wait_for_app_stopped() {
  for _ in {1..50}; do
    app_running || return 0
    sleep 0.2
  done
  return 1
}

wait_for_app_running() {
  for _ in {1..50}; do
    app_running && return 0
    sleep 0.2
  done
  return 1
}

service_running() {
  control status >/dev/null 2>&1
}

wait_for_service_running() {
  for _ in {1..120}; do
    service_running && return 0
    sleep 0.5
  done
  return 1
}

needs_build() {
  if [[ ! -f "$ROOT/mac_menu/build.sh" || ! -d "$ROOT/mac_menu/Sources" ]]; then
    return 1
  fi
  if [[ ! -x "$BIN" \
    || "$ROOT/mac_menu/Info.plist" -nt "$INFO" \
    || "$ROOT/VERSION" -nt "$INFO" \
    || "$ROOT/BUILD_NUMBER" -nt "$INFO" \
    || "$ROOT/mac_menu/LiteLLMMenu.icns" -nt "$ICON" \
    || "$ROOT/mac_menu/generate_app_icon.swift" -nt "$ICON" \
    || "$ROOT/mac_menu/vision_ocr.swift" -nt "$APP_RES/bin/vision_ocr" \
    || "$ROOT/mac_menu/build.sh" -nt "$BIN" ]]; then
    return 0
  fi

  local swift_source
  while IFS= read -r -d '' swift_source; do
    if [[ "$swift_source" -nt "$BIN" ]]; then
      return 0
    fi
  done < <(find "$ROOT/mac_menu/Sources" -name '*.swift' -type f -print0)

  for file in "${RESOURCE_FILES[@]}"; do
    if [[ ! -f "$APP_RES/$file" || "$ROOT/$file" -nt "$APP_RES/$file" ]]; then
      return 0
    fi
  done

  local dir source_file relative_file
  for dir in "${RESOURCE_DIRS[@]}"; do
    if [[ ! -d "$APP_RES/$dir" ]]; then
      return 0
    fi
    while IFS= read -r -d '' source_file; do
      relative_file="${source_file#"$ROOT/"}"
      if [[ ! -f "$APP_RES/$relative_file" || "$source_file" -nt "$APP_RES/$relative_file" ]]; then
        return 0
      fi
    done < <(find "$ROOT/$dir" -type f -print0)
  done

  return 1
}

build_app() {
  "$ROOT/mac_menu/build.sh" >/dev/null
}

app_version() {
  local info="$INFO"
  if [[ ! -f "$info" ]]; then
    info="$ROOT/mac_menu/Info.plist"
  fi
  local version build
  version="$(plutil -extract CFBundleShortVersionString raw "$info" 2>/dev/null || true)"
  build="$(plutil -extract CFBundleVersion raw "$info" 2>/dev/null || true)"
  if [[ -z "$version" && -f "$ROOT/VERSION" ]]; then
    version="$(tr -d '[:space:]' < "$ROOT/VERSION")"
  fi
  if [[ -z "$build" && -f "$ROOT/BUILD_NUMBER" ]]; then
    build="$(tr -d '[:space:]' < "$ROOT/BUILD_NUMBER")"
  fi
  if [[ -n "$version" && -n "$build" && "$version" != "$build" ]]; then
    echo "$version ($build)"
  elif [[ -n "$version" ]]; then
    echo "$version"
  elif [[ -n "$build" ]]; then
    echo "build $build"
  else
    echo "unknown"
    return 1
  fi
}

close_litellm_app() {
  control stop

  while IFS= read -r pid; do
    [[ -n "$pid" && "$pid" != "$$" ]] || continue
    kill "$pid" >/dev/null 2>&1 || true
  done < <(app_pids)

  if ! wait_for_app_stopped; then
    echo "LiteLLM Menu app did not exit after service stop." >&2
    exit 1
  fi
}

open_litellm_app() {
  local launched=0
  if ! app_running; then
    launched=1
    if ! /usr/bin/open "$APP" >/dev/null 2>&1; then
      "$BIN" >/dev/null 2>&1 &
    fi
  fi

  if ! wait_for_app_running; then
    echo "LiteLLM Menu app did not start; APP/UI is not restored." >&2
    echo "Do not treat local proxy/service health as a successful Menu launch." >&2
    exit 1
  fi

  require_control
  if [[ "$launched" == "0" ]] && ! service_running; then
    control start
  fi
  if ! wait_for_service_running; then
    echo "LiteLLM Menu app is running, but the managed proxy/service did not become healthy." >&2
    exit 1
  fi
}

case "$ACTION" in
  version)
    app_version
    ;;
  close)
    close_litellm_app
    ;;
  restart)
    if needs_build; then
      build_app
    fi
    close_litellm_app
    open_litellm_app
    ;;
  open)
    if needs_build; then
      if app_running; then
        echo "LiteLLM Menu app is already running and the app bundle needs rebuild." >&2
        echo "Use '$0 restart' to explicitly rebuild and relaunch the Menu app, then verify the managed proxy/service." >&2
        exit 1
      fi
      build_app
    fi
    if app_running && app_process_is_older_than_bundle; then
      echo "LiteLLM Menu app is already running from an older app binary." >&2
      echo "Use '$0 restart' to relaunch the Menu app, then verify the managed proxy/service." >&2
      exit 1
    fi
    open_litellm_app
    ;;
esac

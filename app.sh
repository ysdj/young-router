#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_INPUT="${LITELLM_APP_PATH:-/Applications/LiteLLM Menu.app}"
APP_PARENT_INPUT="$(dirname "$APP_INPUT")"
if [[ -d "$APP_PARENT_INPUT" ]]; then
  APP="$(cd "$APP_PARENT_INPUT" && pwd -P)/$(basename "$APP_INPUT")"
else
  APP="$APP_INPUT"
fi
export LITELLM_APP_PATH="$APP"
BIN="$APP/Contents/MacOS/LiteLLMMenu"
INFO="$APP/Contents/Info.plist"
ICON="$APP/Contents/Resources/LiteLLMMenu.icns"
APP_RES="$APP/Contents/Resources/App"
CONTROL="$APP_RES/service.sh"
LAUNCHCTL_DOMAIN="gui/$(id -u)"
APP_LAUNCH_AGENT_LABEL="${LITELLM_APP_LAUNCH_AGENT_LABEL:-menu.litellm.menu-login}"
APP_LAUNCH_AGENT_PLIST="${LITELLM_APP_LAUNCH_AGENT_PLIST:-$HOME/Library/LaunchAgents/$APP_LAUNCH_AGENT_LABEL.plist}"
APP_LAUNCH_AGENT_WAS_LOADED=0
ACTION="${1:-open}"
if (( $# > 0 )); then
  shift
fi
DISRUPTIVE=0
while (( $# > 0 )); do
  case "$1" in
    --disruptive)
      DISRUPTIVE=1
      ;;
    *)
      echo "Unknown app.sh option: $1" >&2
      exit 64
      ;;
  esac
  shift
done

RESOURCE_FILES=(
  service.sh
  watch_config.sh
  config_editor.py
  codex_config.py
  external_provider_import.py
  provider_billing.py
  browser_billing.py
  remote_usage_logs.py
  configuration_package.py
  runtime_settings_io.py
  webdav_sync.py
  menu_status.py
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
  webdav
)

usage() {
  echo "usage: $0 {open|close|restart|version} [--disruptive]" >&2
  echo "open/restart must prove the LiteLLM Menu app process first; service health alone is not Menu UI success." >&2
  echo "restart is non-disruptive by default; use --disruptive only from a maintenance shell." >&2
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

control_for_owner() {
  local owner_pid="$1"
  shift
  [[ "$owner_pid" =~ ^[0-9]+$ ]] || return 1
  require_control
  LITELLM_MENU_OWNER_PID="$owner_pid" /bin/bash "$CONTROL" "$@"
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

pid_list_contains() {
  local needle="$1" pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    [[ "$pid" == "$needle" ]] && return 0
  done
  return 1
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

app_pid_matches_current_bundle() {
  local pid="$1" bin_mtime started
  [[ "$pid" =~ ^[0-9]+$ && -x "$BIN" ]] || return 1
  bin_mtime="$(file_mtime_epoch "$BIN")"
  started="$(app_pid_start_epoch "$pid" || true)"
  [[ "$bin_mtime" =~ ^[0-9]+$ && "$started" =~ ^[0-9]+$ ]] || return 1
  (( started >= bin_mtime ))
}

current_app_pid() {
  local excluded_pids="${1:-}" pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    pid_list_contains "$pid" <<<"$excluded_pids" && continue
    app_pid_matches_current_bundle "$pid" || continue
    printf '%s\n' "$pid"
    return 0
  done < <(app_pids)
  return 1
}

wait_for_app_stopped() {
  for _ in {1..225}; do
    app_running || return 0
    sleep 0.2
  done
  return 1
}

wait_for_current_app_pid() {
  local excluded_pids="${1:-}" pid
  for _ in {1..50}; do
    if pid="$(current_app_pid "$excluded_pids" 2>/dev/null)"; then
      printf '%s\n' "$pid"
      return 0
    fi
    sleep 0.2
  done
  return 1
}

app_pid_matches_binary() {
  local pid="$1" command
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == "$BIN"* ]]
}

suspend_app_launch_agent() {
  [[ -f "$APP_LAUNCH_AGENT_PLIST" ]] || return 0
  if launchctl print "$LAUNCHCTL_DOMAIN/$APP_LAUNCH_AGENT_LABEL" >/dev/null 2>&1; then
    launchctl bootout "$LAUNCHCTL_DOMAIN/$APP_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || return 1
    APP_LAUNCH_AGENT_WAS_LOADED=1
  fi
}

restore_app_launch_agent() {
  (( APP_LAUNCH_AGENT_WAS_LOADED == 1 )) || return 0
  if ! launchctl bootstrap "$LAUNCHCTL_DOMAIN" "$APP_LAUNCH_AGENT_PLIST" >/dev/null 2>&1; then
    echo "Could not restore the LiteLLM Menu login launch agent: $APP_LAUNCH_AGENT_LABEL" >&2
    return 1
  fi
  launchctl enable "$LAUNCHCTL_DOMAIN/$APP_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
  APP_LAUNCH_AGENT_WAS_LOADED=0
}

wait_for_no_unexpected_app_pid() {
  local excluded_pids="${1:-}" attempts="${2:-15}" pid
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    pid="$(app_pids | while IFS= read -r candidate; do
      [[ -n "$candidate" ]] || continue
      pid_list_contains "$candidate" <<<"$excluded_pids" && continue
      printf '%s\n' "$candidate"
      break
    done)"
    [[ -z "$pid" ]] || return 1
    sleep 0.2
  done
  return 0
}

any_app_pid() {
  [[ -n "$(app_pids)" ]]
}

service_running() {
  control status >/dev/null 2>&1
}

service_running_for_owner() {
  local owner_pid="$1"
  control_for_owner "$owner_pid" status >/dev/null 2>&1
}

wait_for_service_running() {
  local owner_pid="$1"
  for _ in {1..120}; do
    service_running_for_owner "$owner_pid" && return 0
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
    done < <(
      find "$ROOT/$dir" \
        -type d -name '__pycache__' -prune -o \
        -type f ! -name '*.pyc' ! -name '*.pyo' -print0
    )
  done

  return 1
}

build_app() {
  local backup_app="${1:-}"
  if [[ -n "$backup_app" ]]; then
    LITELLM_KEEP_BACKUP=1 \
      LITELLM_BACKUP_APP_PATH="$backup_app" \
      "$ROOT/mac_menu/build.sh" >/dev/null
  else
    "$ROOT/mac_menu/build.sh" >/dev/null
  fi
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
  local bundle_identifier
  app_running || return 0

  bundle_identifier="$(plutil -extract CFBundleIdentifier raw "$INFO" 2>/dev/null || true)"
  if [[ ! "$bundle_identifier" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
    echo "LiteLLM Menu has no valid bundle identifier; refusing to target an unknown app for shutdown." >&2
    return 1
  fi
  /usr/bin/osascript -e "tell application id \"$bundle_identifier\" to quit" >/dev/null 2>&1 || true
  if wait_for_app_stopped; then
    return 0
  fi

  echo "LiteLLM Menu app did not finish its graceful shutdown; refusing to force-kill it or launch a replacement." >&2
  echo "The existing menu process remains the service owner until it has exited cleanly." >&2
  return 1
}

launch_litellm_app() {
  local force_new="$1" variable
  local launch_environment=()
  for variable in \
    LITELLM_RUNTIME_ROOT \
    LITELLM_MENU_HOME \
    LITELLM_PORT \
    LITELLM_APP_PATH \
    LITELLM_APP_LAUNCH_AGENT_LABEL \
    LITELLM_APP_LAUNCH_AGENT_PLIST \
    LITELLM_CONFIG_WATCH_LABEL \
    LITELLM_CONFIG_WATCH_PLIST \
    LITELLM_VENV_DIR \
    LITELLM_NATIVE_PYTHON \
    LITELLM_BIN \
    LITELLM_NUM_WORKERS \
    LITELLM_HEALTH_WAIT_SECONDS \
    LITELLM_RUNTIME_VERIFY_WAIT_SECONDS \
    LITELLM_MENU_TEST_HEADLESS
  do
    if [[ -n "${!variable-}" ]]; then
      launch_environment+=(--env "$variable=${!variable}")
    fi
  done
  if [[ "$force_new" == "1" ]]; then
    # LaunchServices must own the GUI lifecycle. A raw background child can be
    # reaped when this maintenance shell exits even after its service is ready.
    /usr/bin/open -n "${launch_environment[@]}" "$APP" >/dev/null 2>&1
    return $?
  fi
  /usr/bin/open "${launch_environment[@]}" "$APP" >/dev/null 2>&1
}

wait_for_app_stability() {
  local app_pid="$1"
  for _ in {1..12}; do
    app_pid_matches_binary "$app_pid" || return 1
    service_running_for_owner "$app_pid" || return 1
    sleep 0.25
  done
}

open_litellm_app() {
  local excluded_pids="${1:-}" launched=0 force_new=0 app_pid
  [[ -z "$excluded_pids" ]] || force_new=1

  app_pid="$(current_app_pid "$excluded_pids" 2>/dev/null || true)"
  if [[ -z "$app_pid" && "$force_new" == "1" ]] && [[ -n "$(app_pids | while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    pid_list_contains "$candidate" <<<"$excluded_pids" && continue
    printf '%s\n' "$candidate"
    break
  done)" ]]; then
    echo "LiteLLM Menu has an unexpected app process during restart; refusing to create a second instance." >&2
    return 1
  fi
  if [[ -z "$app_pid" ]]; then
    launched=1
    launch_litellm_app "$force_new" || true
    app_pid="$(wait_for_current_app_pid "$excluded_pids")" || true
  fi

  # A normal LaunchServices request can be accepted without creating a process.
  # Retry once with -n, while keeping the app detached from this shell.
  if [[ -z "$app_pid" && "$force_new" == "0" ]]; then
    app_pid="$(current_app_pid "$excluded_pids" 2>/dev/null || true)"
    if [[ -z "$app_pid" ]] && ! any_app_pid; then
      launch_litellm_app 1 || true
      app_pid="$(wait_for_current_app_pid "$excluded_pids")" || true
    fi
  fi

  if [[ -z "$app_pid" ]]; then
    echo "LiteLLM Menu app did not start; APP/UI is not restored." >&2
    echo "A current app binary with a new process identity is required; service health alone is insufficient." >&2
    return 1
  fi

  require_control
  if [[ "$launched" == "0" ]] && ! service_running_for_owner "$app_pid"; then
    control_for_owner "$app_pid" start
  fi
  if ! wait_for_service_running "$app_pid"; then
    echo "LiteLLM Menu app is running, but this app instance does not own a healthy managed proxy/service." >&2
    return 1
  fi
  if [[ "$launched" == "1" ]] && ! wait_for_app_stability "$app_pid"; then
    echo "LiteLLM Menu started but did not remain alive with its healthy managed proxy/service." >&2
    return 1
  fi
  printf 'LiteLLM Menu restored: app pid %s owns the healthy service.\n' "$app_pid"
}

restart_backup_path() {
  local app_parent app_name
  app_parent="$(dirname "$APP")"
  app_name="$(basename "$APP" .app)"
  printf '%s/.%s.restart-backup.%s.app\n' "$app_parent" "$app_name" "$$"
}

restore_previous_app_bundle() {
  local backup_app="$1" failed_app="${APP}.failed.$$.app"
  [[ -d "$backup_app" ]] || return 1
  if [[ -e "$APP" ]]; then
    mv "$APP" "$failed_app" || return 1
  fi
  if ! mv "$backup_app" "$APP"; then
    [[ ! -e "$APP" && -e "$failed_app" ]] && mv "$failed_app" "$APP"
    return 1
  fi
  rm -rf "$failed_app"
}

wait_for_restart_quiescence() {
  local previous_pids="$1"
  if wait_for_no_unexpected_app_pid "$previous_pids"; then
    return 0
  fi
  echo "LiteLLM Menu was launched again while restart was preparing the replacement." >&2
  echo "The existing app was left running; no bundle was replaced." >&2
  return 1
}

restart_litellm_app() {
  local previous_pids backup_app
  if (( DISRUPTIVE == 0 )); then
    echo "Refusing to restart LiteLLM Menu from a normal command: quitting the app stops the local proxy and interrupts active requests." >&2
    echo "Run '$0 restart --disruptive' from a separate maintenance shell after active requests have settled." >&2
    return 75
  fi
  if ! suspend_app_launch_agent; then
    echo "Could not pause the LiteLLM Menu login launch agent; refusing to restart." >&2
    return 1
  fi
  previous_pids="$(app_pids || true)"
  if ! close_litellm_app; then
    restore_app_launch_agent || true
    return 1
  fi

  # Do not swap a bundle after a queued external open has recreated the old app.
  if ! wait_for_restart_quiescence "$previous_pids"; then
    restore_app_launch_agent || true
    return 1
  fi

  if ! needs_build; then
    if ! open_litellm_app "$previous_pids"; then
      restore_app_launch_agent || true
      return 1
    fi
    restore_app_launch_agent || return 1
    return 0
  fi

  backup_app="$(restart_backup_path)"
  if ! build_app "$backup_app"; then
    echo "LiteLLM Menu build failed; the installed app bundle was left unchanged." >&2
    if open_litellm_app "$previous_pids"; then
      echo "The previous LiteLLM Menu app was restored after the failed build." >&2
    else
      echo "The build failed and the previous LiteLLM Menu app could not be restored." >&2
    fi
    restore_app_launch_agent || true
    return 1
  fi

  if open_litellm_app "$previous_pids"; then
    restore_app_launch_agent || return 1
    rm -rf "$backup_app"
    return 0
  fi

  echo "The replacement app did not become healthy; restoring the previous bundle." >&2
  # A failed replacement is never force-killed. It has to exit on its own before
  # the previous bundle can be restored, otherwise the current app stays intact.
  if app_running; then
    if ! close_litellm_app; then
      echo "The replacement app is still running, so its bundle was left in place." >&2
      restore_app_launch_agent || true
      return 1
    fi
  fi
  if app_running; then
    echo "The replacement app did not exit cleanly, so its bundle was left in place." >&2
    restore_app_launch_agent || true
    return 1
  fi
  if ! restore_previous_app_bundle "$backup_app"; then
    echo "Could not restore the previous LiteLLM Menu bundle." >&2
    restore_app_launch_agent || true
    return 1
  fi
  open_litellm_app "$previous_pids" || {
    echo "The previous LiteLLM Menu bundle was restored but did not start." >&2
    restore_app_launch_agent || true
    return 1
  }
  restore_app_launch_agent || return 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "$ACTION" in
    version)
      app_version
      ;;
    close)
      close_litellm_app
      ;;
    restart)
      restart_litellm_app
      ;;
    open)
      if needs_build; then
        if app_running; then
          echo "LiteLLM Menu app is already running and the app bundle needs rebuild." >&2
          echo "Use '$0 restart --disruptive' from a maintenance shell, then verify the managed proxy/service." >&2
          exit 1
        fi
        build_app
      fi
      if app_running && app_process_is_older_than_bundle; then
        echo "LiteLLM Menu app is already running from an older app binary." >&2
        echo "Use '$0 restart --disruptive' from a maintenance shell, then verify the managed proxy/service." >&2
        exit 1
      fi
      open_litellm_app
      ;;
  esac
fi

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESTINATION="${LITELLM_MENU_INSTALL_APP:-/Applications/LiteLLM Menu.app}"
DEVELOPER_DIR="${DEVELOPER_DIR:-}"
STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/litellm-menu-install.XXXXXX")"
STAGED_APP="$STAGE_ROOT/LiteLLM Menu.app"
INSTALL_STAGE=""
PREVIOUS_APP=""
FAILED_APP=""
INSTALL_COMPLETE=0
RESTART_ARMED=0
OLD_PIDS=""
NEW_PIDS=""
START_TIMEOUT_SECONDS="${LITELLM_MENU_START_TIMEOUT_SECONDS:-70}"
STOP_TIMEOUT_SECONDS="${LITELLM_MENU_STOP_TIMEOUT_SECONDS:-20}"
STOP_GRACE_POLLS=20
REQUIRED_HEALTH_CHECKS=3

cleanup() {
  if [[ "$INSTALL_COMPLETE" != "1" && -n "$PREVIOUS_APP" && -e "$PREVIOUS_APP" && ! -e "$DESTINATION" ]]; then
    mv "$PREVIOUS_APP" "$DESTINATION" || true
  fi
  [[ -z "$INSTALL_STAGE" || ! -d "$INSTALL_STAGE" ]] || rm -rf "$INSTALL_STAGE"
  [[ -z "$FAILED_APP" || ! -d "$FAILED_APP" ]] || rm -rf "$FAILED_APP"
  [[ ! -d "$STAGE_ROOT" ]] || rm -rf "$STAGE_ROOT"
  if [[ "$RESTART_ARMED" == "1" && ( -e "$DESTINATION" || -L "$DESTINATION" ) ]]; then
    [[ -n "$(installed_pids)" ]] || open -n "$DESTINATION" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

copy_tree() {
  local source="$1"
  local destination="$2"
  mkdir -p "$destination"
  # On APFS, -c uses clonefile and falls back to a regular copy across volumes.
  cp -ac "$source/." "$destination/"
}

bundle_roots() {
  ps -axo pid=,command= \
    | awk -v bundle="$DESTINATION" -v executable="$DESTINATION/Contents/MacOS/LiteLLMMenu" '
        {
          pid = $1
          line = $0
          sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "", line)
          if (line == executable || index(line, bundle "/Contents/") > 0) print pid
        }
      '
}

process_tree() {
  local pid child
  for pid in "$@"; do
    [[ -n "$pid" ]] || continue
    printf '%s\n' "$pid"
    while read -r child; do
      [[ -n "$child" ]] || continue
      process_tree "$child"
    done < <(pgrep -P "$pid" 2>/dev/null || true)
  done
  return 0
}

bundle_processes() {
  local root
  while read -r root; do
    [[ -n "$root" ]] && process_tree "$root"
  done < <(bundle_roots) | sort -n -u
  return 0
}

installed_pids() {
  local pid command
  while read -r pid; do
    command=$(ps -p "$pid" -o command= 2>/dev/null || true)
    [[ "$command" == "$DESTINATION/Contents/MacOS/LiteLLMMenu" ]] && printf '%s\n' "$pid"
  done < <(bundle_processes)
  return 0
}

stop_installed_app() {
  local deadline bundle_pids="$1" grace_polls pid state
  [[ -n "$bundle_pids" ]] || return 0

  OLD_PIDS="$bundle_pids"
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill -TERM "$pid" 2>/dev/null || true
  done <<<"$bundle_pids"

  grace_polls=$STOP_GRACE_POLLS
  while pids_are_alive "$bundle_pids" && (( grace_polls > 0 )); do
    sleep 0.05
    grace_polls=$((grace_polls - 1))
  done
  if pids_are_alive "$bundle_pids"; then
    while read -r pid; do
      [[ -n "$pid" ]] || continue
      state="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
      [[ -n "$state" && "$state" != Z* ]] && kill -KILL "$pid" 2>/dev/null || true
    done <<<"$bundle_pids"
  fi

  deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))
  while pids_are_alive "$bundle_pids"; do
    if (( SECONDS >= deadline )); then
      echo "LiteLLM Menu did not stop its captured process tree within ${STOP_TIMEOUT_SECONDS}s." >&2
      return 1
    fi
    sleep 0.05
  done
}

pids_are_alive() {
  local pid state
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    state="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
    [[ -n "$state" && "$state" != Z* ]] && return 0
  done <<<"$1"
  return 1
}

pid_is_listed() {
  local expected="$1"
  local candidate
  while read -r candidate; do
    [[ "$candidate" == "$expected" ]] && return 0
  done <<<"$2"
  return 1
}

core_pid_for_app() {
  local app_pid="$1"
  local child command
  while read -r child; do
    [[ -n "$child" ]] || continue
    command=$(ps -p "$child" -o command= 2>/dev/null || true)
    if [[ "$command" == "$DESTINATION/Contents/Resources/Core/runtime/"* ]] \
      && [[ "$command" == *" -m litellm_menu.core "* ]] \
      && [[ "$command" == *" --parent-pid $app_pid"* ]]; then
      printf '%s\n' "$child"
      return 0
    fi
  done < <(pgrep -P "$app_pid" 2>/dev/null || true)
  return 1
}

proxy_port_for_app() {
  local app_pid="$1"
  local core_pid process_pid command port
  core_pid="$(core_pid_for_app "$app_pid")" || return 1
  while read -r process_pid; do
    [[ -n "$process_pid" ]] || continue
    command=$(ps -p "$process_pid" -o command= 2>/dev/null || true)
    [[ "$command" == *"run_server()"* ]] || continue
    port=$(awk '{ for (field = 1; field < NF; field += 1) if ($field == "--port") { print $(field + 1); exit } }' <<<"$command")
    if [[ "$port" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "$port"
      return 0
    fi
  done < <(process_tree "$core_pid")
  return 1
}

app_is_ready() {
  local app_pid="$1"
  local port
  core_pid_for_app "$app_pid" >/dev/null || return 1
  port="$(proxy_port_for_app "$app_pid")" || return 1
  curl --fail --silent --show-error --max-time 1 \
    "http://127.0.0.1:$port/health/liveliness" >/dev/null 2>&1
}

wait_for_started_app() {
  local rejected_pids="$1"
  local deadline=$((SECONDS + START_TIMEOUT_SECONDS))
  local app_pids app_pid ready_pid stable_pid="" stable_checks=0
  while :; do
    app_pids="$(installed_pids)"
    ready_pid=""
    while read -r app_pid; do
      [[ -n "$app_pid" ]] || continue
      pid_is_listed "$app_pid" "$rejected_pids" && continue
      if app_is_ready "$app_pid"; then
        ready_pid="$app_pid"
        break
      fi
    done <<<"$app_pids"
    if [[ -n "$ready_pid" ]]; then
      if [[ "$ready_pid" == "$stable_pid" ]]; then
        stable_checks=$((stable_checks + 1))
      else
        stable_pid="$ready_pid"
        stable_checks=1
      fi
      if (( stable_checks >= REQUIRED_HEALTH_CHECKS )); then
        NEW_PIDS="$app_pids"
        return 0
      fi
    else
      stable_pid=""
      stable_checks=0
    fi
    (( SECONDS >= deadline )) && break
    sleep 0.05
  done
  return 1
}

start_installed_app() {
  local rejected_pids="$1"
  # An in-place bundle replacement can leave LaunchServices briefly pointed at
  # the old instance. Force a fresh launch only after all old bundle processes
  # have exited, then prove the new process remains healthy before discarding
  # the rollback bundle.
  open -n "$DESTINATION"
  wait_for_started_app "$rejected_pids" || {
    echo "The new LiteLLM Menu app did not start from $DESTINATION." >&2
    return 1
  }
  return 0
}

restore_previous_app() {
  local restore_failed=0
  if [[ -n "$PREVIOUS_APP" && ( -e "$PREVIOUS_APP" || -L "$PREVIOUS_APP" ) ]]; then
    if [[ -e "$DESTINATION" || -L "$DESTINATION" ]]; then
      FAILED_APP="$(dirname "$DESTINATION")/.LiteLLMMenu.failed.$$.app"
      rm -rf "$FAILED_APP"
      mv "$DESTINATION" "$FAILED_APP" || restore_failed=1
    fi
    if (( restore_failed == 0 )); then
      mv "$PREVIOUS_APP" "$DESTINATION" || restore_failed=1
    fi
  fi
  return "$restore_failed"
}

case "$(uname -s)" in
  Darwin) ;;
  *)
    echo "LiteLLM Menu installation is supported only on macOS." >&2
    exit 1
    ;;
esac

[[ "$DESTINATION" = /* && "$DESTINATION" == *.app ]] || {
  echo "LITELLM_MENU_INSTALL_APP must be an absolute .app path." >&2
  exit 1
}
DESTINATION="$(cd "$(dirname "$DESTINATION")" && pwd -P)/$(basename "$DESTINATION")"
[[ "$DESTINATION" == "/Applications/LiteLLM Menu.app" ]] || {
  echo "Refusing an installation destination other than /Applications/LiteLLM Menu.app." >&2
  exit 1
}

if [[ -z "$DEVELOPER_DIR" && -x /Applications/Xcode-beta.app/Contents/Developer/usr/bin/xcodebuild ]]; then
  DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer
fi
if [[ -n "$DEVELOPER_DIR" ]]; then
  export DEVELOPER_DIR
fi

# The installed app already carries the exact portable Python runtime needed
# by repeat local builds. Reuse it only when no caller supplied another
# runtime, its pinned LiteLLM version matches this checkout, and the portable
# launchers are present. The macOS host, project Python sources, smoke tests,
# signing, replacement, and readiness checks still run on every build.
if [[ -z "${LITELLM_MENU_CORE_RUNTIME_SOURCE:-}" \
  && -z "${LITELLM_RELEASE_RUNTIME_SOURCE:-}" ]]; then
  INSTALLED_RUNTIME="$DESTINATION/Contents/Resources/Core/runtime"
  if [[ -x "$INSTALLED_RUNTIME/python/bin/python3.12" \
    && -x "$INSTALLED_RUNTIME/bin/python" \
    && -x "$INSTALLED_RUNTIME/bin/litellm" \
    && -f "$INSTALLED_RUNTIME/LITELLM_VERSION" \
    && "$(tr -d '[:space:]' < "$INSTALLED_RUNTIME/LITELLM_VERSION")" == "$(tr -d '[:space:]' < "$ROOT/LITELLM_VERSION")" ]]; then
    export LITELLM_MENU_CORE_RUNTIME_SOURCE="$INSTALLED_RUNTIME"
    printf 'Reusing installed Core runtime: %s\n' "$INSTALLED_RUNTIME"
  fi
fi

(
  cd "$ROOT/rn"
  pnpm install --frozen-lockfile
  LITELLM_MENU_MACOS_OUTPUT="$STAGED_APP" pnpm run build:macos
)

test -x "$STAGED_APP/Contents/MacOS/LiteLLMMenu"
test -x "$STAGED_APP/Contents/Resources/Core/runtime/bin/python"
test -x "$STAGED_APP/Contents/Resources/Core/runtime/bin/litellm"
test -x "$STAGED_APP/Contents/Resources/Core/bin/vision_ocr"
plutil -lint "$STAGED_APP/Contents/Info.plist" >/dev/null
codesign --verify --deep --strict --verbose=2 "$STAGED_APP"

INSTALL_STAGE="$(dirname "$DESTINATION")/.LiteLLMMenu.install.$$.app"
PREVIOUS_APP="$(dirname "$DESTINATION")/.LiteLLMMenu.previous.$$.app"
rm -rf "$INSTALL_STAGE" "$PREVIOUS_APP"
copy_tree "$STAGED_APP" "$INSTALL_STAGE"
codesign --verify --deep --strict --verbose=2 "$INSTALL_STAGE"

# Keep the installed service available while the verified replacement takes
# its place. Running processes retain the old executable after this rename;
# stopping them only after the new bundle is in place shortens the listener
# outage to the stop/start interval.
OLD_PIDS="$(bundle_processes)"
# Arm the EXIT relaunch before moving the live bundle, so an interrupted swap
# cannot leave the installed path without an app to launch.
RESTART_ARMED=1
if [[ -e "$DESTINATION" || -L "$DESTINATION" ]]; then
  mv "$DESTINATION" "$PREVIOUS_APP"
fi
if ! mv "$INSTALL_STAGE" "$DESTINATION"; then
  [[ ! -e "$PREVIOUS_APP" && ! -L "$PREVIOUS_APP" ]] || mv "$PREVIOUS_APP" "$DESTINATION"
  exit 1
fi
INSTALL_STAGE=""
if ! stop_installed_app "$OLD_PIDS"; then
  echo "The old LiteLLM Menu app did not stop; restoring the previous bundle." >&2
  restore_previous_app || {
    echo "The previous LiteLLM Menu bundle could not be restored." >&2
    exit 1
  }
  exit 1
fi
# A Core may have forked its proxy just after the pre-swap snapshot. Capture
# any remaining process still executing the retired bundle before launching
# the replacement, so no old listener can survive into the new lifecycle.
REMAINING_OLD_PIDS="$(bundle_processes)"
if [[ -n "$REMAINING_OLD_PIDS" ]] && ! stop_installed_app "$REMAINING_OLD_PIDS"; then
  echo "The old LiteLLM Menu proxy did not stop; restoring the previous bundle." >&2
  restore_previous_app || {
    echo "The previous LiteLLM Menu bundle could not be restored." >&2
    exit 1
  }
  exit 1
fi
if ! start_installed_app "$OLD_PIDS"; then
  echo "The new LiteLLM Menu app failed readiness checks; restoring the previous bundle." >&2
  restore_previous_app || {
    echo "The previous LiteLLM Menu bundle could not be restored." >&2
    exit 1
  }
  exit 1
fi

RESTART_ARMED=0
rm -rf "$PREVIOUS_APP"
PREVIOUS_APP=""
INSTALL_COMPLETE=1

printf '%s\n' "$DESTINATION"

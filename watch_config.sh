#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_RUNTIME_ROOT="${LITELLM_MENU_HOME:-$HOME/.litellm-menu}"
DEFAULT_ROOT="$DEFAULT_RUNTIME_ROOT"

ROOT="${LITELLM_RUNTIME_ROOT:-$DEFAULT_ROOT}"
TEMPLATE_ROOT="${LITELLM_TEMPLATE_ROOT:-$SCRIPT_DIR}"
RUNTIME_SETTINGS_FILE="${LITELLM_MENU_RUNTIME_SETTINGS_FILE:-$ROOT/runtime-settings.env}"

load_runtime_settings_file() {
  [[ -f "$RUNTIME_SETTINGS_FILE" ]] || return 0
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    value="${value//[[:space:]]/}"
    case "$key" in
      LITELLM_CONFIG_WATCH_INTERVAL|LITELLM_CONFIG_WATCH_SETTLE_INTERVAL)
        [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]] && export "$key=$value"
        ;;
      LITELLM_MENU_LOG_MAX_BYTES)
        [[ "$value" =~ ^[0-9]+$ ]] && export "$key=$value"
        ;;
    esac
  done < "$RUNTIME_SETTINGS_FILE"
}

load_runtime_settings_file

CONFIG="${LITELLM_CONFIG_FILE:-$ROOT/config.yaml}"
CONTROL="${LITELLM_CONTROL_PATH:-$TEMPLATE_ROOT/service.sh}"
LOG_FILE="${LITELLM_CONFIG_WATCH_LOG:-$ROOT/config-watch.log}"
LOG_MAX_BYTES="${LITELLM_MENU_LOG_MAX_BYTES:-10485760}"
POLL_INTERVAL="${LITELLM_CONFIG_WATCH_INTERVAL:-5}"
SETTLE_INTERVAL="${LITELLM_CONFIG_WATCH_SETTLE_INTERVAL:-2}"

rotate_log_if_needed() {
  local current_bytes backup_bytes log_directory temp_path
  [[ "$LOG_MAX_BYTES" =~ ^[0-9]+$ && "$LOG_MAX_BYTES" -gt 0 ]] || return 0
  if [[ -f "$LOG_FILE.1" ]]; then
    backup_bytes="$(wc -c < "$LOG_FILE.1" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$backup_bytes" =~ ^[0-9]+$ ]] && (( backup_bytes > LOG_MAX_BYTES )); then
      log_directory="$(dirname "$LOG_FILE.1")"
      mkdir -p "$log_directory"
      temp_path="$(mktemp "$log_directory/.${LOG_FILE##*/}.1.rotate.XXXXXX")" || return 0
      if tail -c "$LOG_MAX_BYTES" "$LOG_FILE.1" > "$temp_path" 2>/dev/null; then
        chmod 600 "$temp_path" 2>/dev/null || true
        mv "$temp_path" "$LOG_FILE.1" 2>/dev/null || rm -f "$temp_path"
      else
        rm -f "$temp_path"
      fi
    fi
  fi
  [[ -f "$LOG_FILE" ]] || return 0
  current_bytes="$(wc -c < "$LOG_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ "$current_bytes" =~ ^[0-9]+$ ]] || return 0
  (( current_bytes > LOG_MAX_BYTES )) || return 0
  log_directory="$(dirname "$LOG_FILE")"
  mkdir -p "$log_directory"
  temp_path="$(mktemp "$log_directory/.${LOG_FILE##*/}.rotate.XXXXXX")" || return 0
  if tail -c "$LOG_MAX_BYTES" "$LOG_FILE" > "$temp_path" 2>/dev/null; then
    chmod 600 "$temp_path" 2>/dev/null || true
    if mv "$temp_path" "$LOG_FILE.1" 2>/dev/null; then
      cat "$LOG_FILE.1" > "$LOG_FILE" 2>/dev/null || : > "$LOG_FILE" 2>/dev/null || true
    else
      rm -f "$temp_path"
    fi
  else
    rm -f "$temp_path"
    : > "$LOG_FILE" 2>/dev/null || true
  fi
  chmod 600 "$LOG_FILE" 2>/dev/null || true
}

control() {
  /bin/bash "$CONTROL" "$@"
}

timestamp() {
  /bin/date "+%Y-%m-%d %H:%M:%S"
}

mtime() {
  /usr/bin/stat -f "%m" "$CONFIG" 2>/dev/null || echo 0
}

config_hash() {
  if [[ ! -f "$CONFIG" ]]; then
    echo "missing"
    return 0
  fi
  /usr/bin/shasum -a 256 "$CONFIG" | /usr/bin/awk '{print $1}'
}

stable_hash() {
  local previous current
  previous="$(config_hash)"
  while true; do
    /bin/sleep "$SETTLE_INTERVAL"
    current="$(config_hash)"
    if [[ "$current" == "$previous" ]]; then
      echo "$current"
      return 0
    fi
    previous="$current"
  done
}

log() {
  mkdir -p "$(dirname "$LOG_FILE")"
  rotate_log_if_needed
  printf '[%s] %s\n' "$(timestamp)" "$*" >>"$LOG_FILE"
}

stage_if_valid() {
  log "config.yaml changed; validating before staging runtime config"
  rotate_log_if_needed
  if ! control validate >>"$LOG_FILE" 2>&1; then
    log "validation failed; keeping current runtime config"
    return 1
  fi

  log "validation passed; staging config for next runtime apply"
  rotate_log_if_needed
  if control stage-config >>"$LOG_FILE" 2>&1; then
    log "runtime config staged; run apply-config to reload LiteLLM"
    return 0
  else
    log "runtime config staging failed; see error above"
    return 1
  fi
}

webdav_interval_seconds() {
  if ! control webdav-enabled-status >/dev/null 2>&1; then
    echo 0
    return 0
  fi
  control webdav-sync-interval-seconds 2>/dev/null || echo 1800
}

webdav_sync_if_due() {
  local now interval due
  interval="$(webdav_interval_seconds)"
  if [[ "$interval" == "0" ]]; then
    return 0
  fi
  now="$(/bin/date +%s)"
  due=$(( last_webdav_sync_at + interval ))
  if (( now < due )); then
    return 0
  fi
  last_webdav_sync_at="$now"
  log "WebDAV scheduled sync due; running bidirectional sync"
  rotate_log_if_needed
  if control webdav-sync >>"$LOG_FILE" 2>&1; then
    log "WebDAV scheduled sync finished"
  else
    log "WebDAV scheduled sync failed; see error above"
  fi
}

main() {
  log "watcher started for $CONFIG"
  local last_mtime last_staged_hash last_webdav_sync_at
  last_mtime="$(mtime)"
  last_staged_hash="$(config_hash)"
  last_webdav_sync_at="$(/bin/date +%s)"
  while true; do
    /bin/sleep "$POLL_INTERVAL"
    webdav_sync_if_due
    local current_mtime current_hash
    current_mtime="$(mtime)"
    if [[ "$current_mtime" != "$last_mtime" ]]; then
      last_mtime="$current_mtime"
      current_hash="$(stable_hash)"
      last_mtime="$(mtime)"
      if [[ "$current_hash" == "$last_staged_hash" ]]; then
        log "config.yaml changed but content hash is unchanged; skipping stage"
        continue
      fi
      if stage_if_valid; then
        last_staged_hash="$current_hash"
      fi
    fi
  done
}

main

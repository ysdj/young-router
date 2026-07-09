# shellcheck shell=bash
run_webdav_sync() {
  local command="$1"
  shift || true
  LITELLM_CONFIG_FILE="$CONFIG_FILE" \
    LITELLM_RUNTIME_ROOT="$ROOT" \
    LITELLM_WEBDAV_SYNC_SETTINGS="$WEBDAV_SYNC_SETTINGS" \
    "$PYTHON" "$TEMPLATE_ROOT/webdav_sync.py" "$command" \
      --config "$CONFIG_FILE" \
      --settings "$WEBDAV_SYNC_SETTINGS" \
      --state "$WEBDAV_SYNC_STATE_FILE" \
      "$@"
}

webdav_sync_status_timestamp() {
  printf '%s' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}

webdav_sync_json_escape() {
  local json_python="${PYTHON:-}"
  if [[ -z "$json_python" ]]; then
    json_python="$(command -v python3 2>/dev/null || true)"
  fi
  if [[ -n "$json_python" ]] && printf '' | "$json_python" -c 'import json,sys; print(json.dumps(sys.stdin.read()))' >/dev/null 2>&1; then
    "$json_python" -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
    return 0
  fi
  sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a' -e 'N' -e '$!ba' -e 's/\n/\\n/g' -e 's/^/"/' -e 's/$/"/'
}

webdav_sync_record_result() {
  local action="$1" exit_code="$2" output="$3" enabled_state
  if webdav_sync_enabled; then
    enabled_state="true"
  else
    enabled_state="false"
  fi
  mkdir -p "$(dirname "$WEBDAV_SYNC_STATUS_FILE")"
  {
    printf '{\n'
    printf '  "action": %s,\n' "$(printf '%s' "$action" | webdav_sync_json_escape)"
    printf '  "ok": %s,\n' "$([[ "$exit_code" == "0" ]] && printf true || printf false)"
    printf '  "exit_code": %s,\n' "$exit_code"
    printf '  "checked_at": %s,\n' "$(webdav_sync_status_timestamp | webdav_sync_json_escape)"
    printf '  "enabled": %s,\n' "$enabled_state"
    printf '  "output": %s\n' "$(printf '%s' "$output" | webdav_sync_json_escape)"
    printf '}\n'
  } > "$WEBDAV_SYNC_STATUS_FILE"
  chmod 600 "$WEBDAV_SYNC_STATUS_FILE" 2>/dev/null || true
}

webdav_sync_settings() {
  ensure_python_tools
  run_webdav_sync settings
}

webdav_sync_configure() {
  ensure_python_tools
  local output exit_code
  if output="$(run_webdav_sync configure 2>&1)"; then
    exit_code=0
    printf '%s\n' "$output"
  else
    exit_code=$?
    printf '%s\n' "$output" >&2
    webdav_sync_record_result "configure" "$exit_code" "$output"
    return "$exit_code"
  fi
  if webdav_sync_enabled; then
    if output="$(run_webdav_sync sync 2>&1)"; then
      webdav_sync_record_result "sync" 0 "$output"
      printf '%s\n' "$output"
    else
      exit_code=$?
      webdav_sync_record_result "sync" "$exit_code" "$output"
      printf '%s\n' "$output" >&2
      return "$exit_code"
    fi
  fi
}

webdav_sync_status() {
  ensure_python_tools
  run_webdav_sync status
}

webdav_sync_probe() {
  ensure_python_tools
  local output exit_code
  if [[ -t 0 ]]; then
    if output="$(run_webdav_sync probe 2>&1)"; then
      exit_code=0
    else
      exit_code=$?
    fi
  else
    if output="$(run_webdav_sync probe --stdin-settings 2>&1)"; then
      exit_code=0
    else
      exit_code=$?
    fi
  fi
  webdav_sync_record_result "probe" "$exit_code" "$output"
  if [[ "$exit_code" == "0" ]]; then
    printf '%s\n' "$output"
  else
    printf '%s\n' "$output" >&2
  fi
  return "$exit_code"
}

webdav_sync_push() {
  ensure_python_tools
  local output exit_code
  if output="$(run_webdav_sync push 2>&1)"; then
    exit_code=0
    webdav_sync_record_result "push" "$exit_code" "$output"
    printf '%s\n' "$output"
  else
    exit_code=$?
    webdav_sync_record_result "push" "$exit_code" "$output"
    printf '%s\n' "$output" >&2
    return "$exit_code"
  fi
}

webdav_sync_sync() {
  ensure_python_tools
  local output exit_code
  if output="$(run_webdav_sync sync 2>&1)"; then
    exit_code=0
    webdav_sync_record_result "sync" "$exit_code" "$output"
    printf '%s\n' "$output"
  else
    exit_code=$?
    webdav_sync_record_result "sync" "$exit_code" "$output"
    printf '%s\n' "$output" >&2
    return "$exit_code"
  fi
}

webdav_sync_pull() {
  ensure_python_tools
  local output exit_code
  if output="$(run_webdav_sync pull 2>&1)"; then
    exit_code=0
    webdav_sync_record_result "pull" "$exit_code" "$output"
    printf '%s\n' "$output"
  else
    exit_code=$?
    webdav_sync_record_result "pull" "$exit_code" "$output"
    printf '%s\n' "$output" >&2
    return "$exit_code"
  fi
  sync_runtime_config
  echo "Pulled config was staged to $RUNTIME_CONFIG"
  if health_ok; then
    echo "LiteLLM is running; run apply-config to reload and verify the new routes."
  fi
}

webdav_sync_enabled() {
  if [[ -f "$WEBDAV_SYNC_ENABLED_FILE" ]]; then
    return 0
  fi
  return 1
}

webdav_sync_enabled_status() {
  if webdav_sync_enabled; then
    echo "enabled"
    return 0
  fi
  echo "disabled"
  return 1
}

webdav_sync_last_status() {
  ensure_runtime_layout
  if [[ -f "$WEBDAV_SYNC_STATUS_FILE" ]]; then
    cat "$WEBDAV_SYNC_STATUS_FILE"
    return 0
  fi
  if webdav_sync_enabled; then
    printf '{"enabled":true,"ok":null,"action":null,"checked_at":null,"output":""}\n'
  else
    printf '{"enabled":false,"ok":null,"action":null,"checked_at":null,"output":""}\n'
  fi
}

webdav_sync_interval_seconds() {
  local output
  if ! ensure_python_tools >/dev/null 2>&1; then
    echo 1800
    return 0
  fi
  if ! output="$(run_webdav_sync settings 2>/dev/null)"; then
    echo 1800
    return 0
  fi
  printf '%s' "$output" | "$PYTHON" -c 'import json,sys
try:
    data=json.load(sys.stdin)
    minutes=int(data.get("sync_interval_minutes", 30))
except Exception:
    minutes=30
minutes=max(0, min(minutes, 24*60))
print(minutes*60)
'
}

webdav_sync_write_enabled_state() {
  mkdir -p "$(dirname "$WEBDAV_SYNC_ENABLED_FILE")"
  printf '1\n' > "$WEBDAV_SYNC_ENABLED_FILE"
  chmod 600 "$WEBDAV_SYNC_ENABLED_FILE" 2>/dev/null || true
}

webdav_sync_disable() {
  rm -f "$WEBDAV_SYNC_ENABLED_FILE"
  webdav_sync_record_result "disable" 0 "WebDAV sync disabled"
  echo "WebDAV sync disabled"
}

webdav_sync_auto_sync() {
  if ! webdav_sync_enabled; then
    return 0
  fi
  if ! ensure_python_tools; then
    local output="WebDAV auto-sync skipped: Python tools are not available"
    webdav_sync_record_result "sync" 1 "$output"
    echo "$output" >&2
    return 0
  fi

  local output exit_code
  if output="$(run_webdav_sync sync 2>&1)"; then
    webdav_sync_record_result "sync" 0 "$output"
    printf '%s\n' "$output"
  else
    exit_code=$?
    webdav_sync_record_result "sync" "$exit_code" "$output"
    printf '%s\n' "$output" >&2
  fi
  return 0
}

webdav_sync_auto_push() {
  webdav_sync_auto_sync
}

webdav_sync_enable() {
  ensure_python_tools
  local output exit_code
  webdav_sync_write_enabled_state
  if output="$(run_webdav_sync sync 2>&1)"; then
    webdav_sync_record_result "sync" 0 "$output"
    printf '%s\n' "$output"
    echo "WebDAV sync enabled"
  else
    exit_code=$?
    webdav_sync_record_result "sync" "$exit_code" "$output"
    echo "WebDAV sync enabled"
    echo "Initial WebDAV sync failed; the next auto-sync will retry."
    printf '%s\n' "$output" >&2
    return 0
  fi
}

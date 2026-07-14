# shellcheck shell=bash
write_launch_agent() {
  local plist="${1:-$LAUNCH_AGENT_PLIST}" run_at_load="${2:-0}"
  mkdir -p "$(dirname "$plist")"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$(xml_escape "$LAUNCH_AGENT_LABEL")</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$(xml_escape "$TEMPLATE_ROOT/service.sh")</string>
    <string>run-native</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$ROOT")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(xml_escape "$TEMPLATE_ROOT/bin:$VENV_DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin")</string>
    <key>LITELLM_RUNTIME_ROOT</key>
    <string>$(xml_escape "$ROOT")</string>
    <key>LITELLM_TEMPLATE_ROOT</key>
    <string>$(xml_escape "$TEMPLATE_ROOT")</string>
    <key>LITELLM_CONFIG_FILE</key>
    <string>$(xml_escape "$CONFIG_FILE")</string>
    <key>LITELLM_RUNTIME_DIR</key>
    <string>$(xml_escape "$RUNTIME_DIR")</string>
    <key>LITELLM_RUNTIME_CONFIG</key>
    <string>$(xml_escape "$RUNTIME_CONFIG")</string>
    <key>LITELLM_USE_SYSTEM_PROXIES</key>
    <string>$(xml_escape "$(use_system_proxies_value)")</string>
    <key>LITELLM_UV_BIN</key>
    <string>$(xml_escape "$BUNDLED_UV")</string>
    <key>UV_PYTHON_INSTALL_DIR</key>
    <string>$(xml_escape "$UV_PYTHON_INSTALL_DIR")</string>
    <key>LITELLM_MENU_LOG</key>
    <string>$(xml_escape "$LOG_FILE")</string>
    <key>LITELLM_MENU_ACTIONS_LOG</key>
    <string>$(xml_escape "$MENU_ACTIONS_LOG")</string>
    <key>LITELLM_MENU_RUNTIME_SETTINGS_FILE</key>
    <string>$(xml_escape "$RUNTIME_SETTINGS_FILE")</string>
    <key>LITELLM_RECENT_REQUESTS_LOG</key>
    <string>$(xml_escape "$RECENT_REQUESTS_LOG")</string>
    <key>LITELLM_MENU_LOG_MAX_BYTES</key>
    <string>$(xml_escape "$LOCAL_LOG_MAX_BYTES")</string>
    <key>LITELLM_MENU_REQUEST_TIMEOUT_SECONDS</key>
    <string>$(xml_escape "$REQUEST_TIMEOUT_SECONDS")</string>
    <key>LITELLM_MENU_STALL_TIMEOUT_SECONDS</key>
    <string>$(xml_escape "$STALL_TIMEOUT_SECONDS")</string>
    <key>LITELLM_MENU_STREAM_START_TIMEOUT_SECONDS</key>
    <string>$(xml_escape "$STREAM_START_TIMEOUT_SECONDS")</string>
    <key>LITELLM_MENU_CODEX_COMPACTION_START_TIMEOUT_SECONDS</key>
    <string>$(xml_escape "$CODEX_COMPACTION_START_TIMEOUT_SECONDS")</string>
    <key>LITELLM_MENU_RECOVERY_MAX_SECONDS</key>
    <string>$(xml_escape "$RECOVERY_MAX_SECONDS")</string>
    <key>LITELLM_MENU_RECOVERY_INTERVAL_SECONDS</key>
    <string>$(xml_escape "$RECOVERY_INTERVAL_SECONDS")</string>
    <key>LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS</key>
    <string>$(xml_escape "$WEB_FETCH_TIMEOUT_SECONDS")</string>
    <key>LITELLM_MENU_WEB_SEARCH_MAX_RESULTS</key>
    <string>$(xml_escape "$WEB_SEARCH_MAX_RESULTS")</string>
    <key>LITELLM_MENU_WEB_SEARCH_READ_RESULTS</key>
    <string>$(xml_escape "$WEB_SEARCH_READ_RESULTS")</string>
    <key>LITELLM_MENU_WEB_SEARCH_READ_CHARS</key>
    <string>$(xml_escape "$WEB_SEARCH_READ_CHARS")</string>
    <key>LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND</key>
    <string>$(xml_escape "$WEB_SEARCH_DDGS_BACKEND")</string>
    <key>LITELLM_MENU_WEB_SEARCH_REGION</key>
    <string>$(xml_escape "$WEB_SEARCH_REGION")</string>
    <key>LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS</key>
    <string>$(xml_escape "$WEB_SEARCH_MAX_ROUNDS")</string>
    <key>LITELLM_MENU_WEB_SEARCH_MAX_QUERIES</key>
    <string>$(xml_escape "$WEB_SEARCH_MAX_QUERIES")</string>
    <key>LITELLM_MENU_WEB_SEARCH_MAX_OPEN_PAGES</key>
    <string>$(xml_escape "$WEB_SEARCH_MAX_OPEN_PAGES")</string>
    <key>LITELLM_MENU_WEB_SEARCH_MAX_FIND_IN_PAGE</key>
    <string>$(xml_escape "$WEB_SEARCH_MAX_FIND_IN_PAGE")</string>
    <key>LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES</key>
    <string>$(xml_escape "$EXTERNAL_WEB_SEARCH_MODEL_RETRIES")</string>
    <key>LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS</key>
    <string>$(xml_escape "$EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS")</string>
    <key>LITELLM_MENU_IMAGE_TOOL_FALLBACK_MAX_ATTEMPTS</key>
    <string>$(xml_escape "$IMAGE_TOOL_FALLBACK_MAX_ATTEMPTS")</string>
    <key>LITELLM_MENU_DEPLOYMENT_COOLDOWN_FAILURES</key>
    <string>$(xml_escape "$DEPLOYMENT_COOLDOWN_FAILURES")</string>
    <key>LITELLM_MENU_DEPLOYMENT_COOLDOWN_SECONDS</key>
    <string>$(xml_escape "$DEPLOYMENT_COOLDOWN_SECONDS")</string>
    <key>LITELLM_MENU_DEPLOYMENT_COOLDOWN_FILE</key>
    <string>$(xml_escape "$DEPLOYMENT_COOLDOWN_FILE")</string>
    <key>LITELLM_MENU_ROUTE_RECOVERY_STATE_FILE</key>
    <string>$(xml_escape "$ROUTE_RECOVERY_STATE_FILE")</string>
    <key>LITELLM_MENU_COMPUTER_FACADE_BACKEND</key>
    <string>$(xml_escape "$COMPUTER_FACADE_BACKEND")</string>
    <key>LITELLM_MENU_COMPUTER_FACADE_MODEL</key>
    <string>$(xml_escape "$COMPUTER_FACADE_MODEL")</string>
    <key>LITELLM_MENU_COMPUTER_FACADE_MAX_STEPS</key>
    <string>$(xml_escape "$COMPUTER_FACADE_MAX_STEPS")</string>
    <key>LITELLM_MENU_COMPUTER_FACADE_TRACE</key>
    <string>$(xml_escape "$COMPUTER_FACADE_TRACE")</string>
    <key>LITELLM_MENU_COMPUTER_FACADE_TRACE_SCREENSHOTS</key>
    <string>$(xml_escape "$COMPUTER_FACADE_TRACE_SCREENSHOTS")</string>
    <key>LITELLM_MENU_COMPUTER_FACADE_ACTION_DENYLIST</key>
    <string>$(xml_escape "$COMPUTER_FACADE_ACTION_DENYLIST")</string>
    <key>LITELLM_MENU_COMPUTER_FACADE_REQUIRE_OBSERVATION</key>
    <string>$(xml_escape "$COMPUTER_FACADE_REQUIRE_OBSERVATION")</string>
    <key>LITELLM_LOCAL_MODEL_COST_MAP</key>
    <string>$(xml_escape "$LOCAL_MODEL_COST_MAP")</string>
    <key>LITELLM_PROXY_TELEMETRY</key>
    <string>$(xml_escape "$PROXY_TELEMETRY")</string>
    <key>LITELLM_PORT</key>
    <string>$(xml_escape "$PORT")</string>
    <key>LITELLM_HOST</key>
    <string>$(xml_escape "$HOST")</string>
    <key>LITELLM_MASTER_KEY</key>
    <string>$(xml_escape "$MASTER_KEY")</string>
    <key>LITELLM_NUM_WORKERS</key>
    <string>$(xml_escape "$NATIVE_WORKERS")</string>
    <key>LITELLM_MAX_REQUESTS_BEFORE_RESTART</key>
    <string>$(xml_escape "$NATIVE_MAX_REQUESTS_BEFORE_RESTART")</string>
    <key>LITELLM_STATE_TTL_SECONDS</key>
    <string>$(xml_escape "$STATE_TTL_SECONDS")</string>
    <key>LITELLM_HEALTH_WAIT_SECONDS</key>
    <string>$(xml_escape "$HEALTH_WAIT_SECONDS")</string>
    <key>LITELLM_RUNTIME_VERIFY_WAIT_SECONDS</key>
    <string>$(xml_escape "$RUNTIME_VERIFY_WAIT_SECONDS")</string>
    <key>LITELLM_SERVICE_LIFECYCLE_LOCK_WAIT_SECONDS</key>
    <string>$(xml_escape "$SERVICE_LIFECYCLE_LOCK_WAIT_SECONDS")</string>
    <key>LITELLM_SERVICE_THROTTLE_INTERVAL_SECONDS</key>
    <string>$(xml_escape "$SERVICE_THROTTLE_INTERVAL_SECONDS")</string>
    <key>LITELLM_VENV_DIR</key>
    <string>$(xml_escape "$VENV_DIR")</string>
    <key>LITELLM_NATIVE_PYTHON</key>
    <string>$(xml_escape "$NATIVE_PYTHON")</string>
    <key>LITELLM_BIN</key>
    <string>$(xml_escape "$LITELLM_BIN")</string>
    <key>LITELLM_NATIVE_PID_FILE</key>
    <string>$(xml_escape "$NATIVE_PID_FILE")</string>
    <key>LITELLM_ROUTE_TRACE_STATE_FILE</key>
    <string>$(xml_escape "$ROUTE_TRACE_STATE_FILE")</string>
    <key>LITELLM_MENU_ROUTE_TRACE</key>
    <string>$(xml_escape "$(route_trace_effective_value)")</string>
    <key>LITELLM_MENU_ROUTE_TRACE_PREVIEW_CHARS</key>
    <string>$(xml_escape "$ROUTE_TRACE_PREVIEW_CHARS")</string>
    <key>LITELLM_ROUTE_TRACE_SCAN_LINES</key>
    <string>$(xml_escape "$ROUTE_TRACE_SCAN_LINES")</string>
    <key>LITELLM_ROUTE_TRACE_LINES</key>
    <string>$(xml_escape "$ROUTE_TRACE_LINES")</string>
    <key>LITELLM_ROUTE_TRACE_MAX_REQUESTS</key>
    <string>$(xml_escape "$ROUTE_TRACE_MAX_REQUESTS")</string>
  </dict>
  <key>RunAtLoad</key>
  $(bool_xml "$run_at_load")
  <key>KeepAlive</key>
  <false/>
  <key>ThrottleInterval</key>
  <integer>$(xml_escape "$SERVICE_THROTTLE_INTERVAL_SECONDS")</integer>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$LOG_FILE")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$LOG_FILE")</string>
</dict>
</plist>
PLIST
  chmod 600 "$plist"
  plutil -lint "$plist" >/dev/null
}

write_app_launch_agent() {
  mkdir -p "$(dirname "$APP_LAUNCH_AGENT_PLIST")"
  cat > "$APP_LAUNCH_AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$(xml_escape "$APP_LAUNCH_AGENT_LABEL")</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-gj</string>
    <string>$(xml_escape "$APP_BUNDLE_PATH")</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$ROOT")</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$MENU_ACTIONS_LOG")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$MENU_ACTIONS_LOG")</string>
</dict>
</plist>
PLIST
  chmod 600 "$APP_LAUNCH_AGENT_PLIST"
  plutil -lint "$APP_LAUNCH_AGENT_PLIST" >/dev/null
}

bootout_app_launch_agent() {
  launchctl bootout "$LAUNCHCTL_DOMAIN/$APP_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 \
    || launchctl bootout "$LAUNCHCTL_DOMAIN" "$APP_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 \
    || true
}

remove_service_launch_agent() {
  launchctl bootout "$LAUNCHCTL_DOMAIN/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 \
    || launchctl bootout "$LAUNCHCTL_DOMAIN" "$LAUNCH_AGENT_PLIST" >/dev/null 2>&1 \
    || launchctl bootout "$LAUNCHCTL_DOMAIN" "$SESSION_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 \
    || launchctl bootout "$LAUNCHCTL_DOMAIN" "$AUTOSTART_LAUNCH_AGENT_PLIST" >/dev/null 2>&1 \
    || true
  rm -f "$LAUNCH_AGENT_PLIST" "$SESSION_LAUNCH_AGENT_PLIST" "$AUTOSTART_LAUNCH_AGENT_PLIST"
}

enable_autostart() {
  ensure_runtime_layout
  mkdir -p "$(dirname "$AUTOSTART_STATE_FILE")"
  printf '1\n' > "$AUTOSTART_STATE_FILE"
  chmod 600 "$AUTOSTART_STATE_FILE" 2>/dev/null || true
  remove_service_launch_agent
  write_app_launch_agent
  bootout_app_launch_agent
  bootstrap_launch_agent "$APP_LAUNCH_AGENT_PLIST"
  launchctl enable "$LAUNCHCTL_DOMAIN/$APP_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
  echo "Auto start enabled:"
  echo "  menu: $APP_LAUNCH_AGENT_PLIST"
}

disable_autostart() {
  bootout_app_launch_agent
  remove_service_launch_agent
  rm -f "$AUTOSTART_STATE_FILE" "$APP_LAUNCH_AGENT_PLIST"
  echo "Auto start disabled"
}

repair_autostart_if_enabled() {
  [[ -f "$AUTOSTART_STATE_FILE" ]] || return 0
  remove_service_launch_agent
  [[ -f "$APP_LAUNCH_AGENT_PLIST" ]] && return 0
  write_app_launch_agent
}

autostart_status() {
  local missing=()
  repair_autostart_if_enabled
  if [[ -f "$AUTOSTART_STATE_FILE" && -f "$APP_LAUNCH_AGENT_PLIST" ]]; then
    echo "enabled"
    exit 0
  fi
  if [[ -f "$AUTOSTART_STATE_FILE" ]]; then
    [[ -f "$APP_LAUNCH_AGENT_PLIST" ]] || missing+=("menu app launch agent plist")
    if (( ${#missing[@]} == 0 )); then
      missing+=("unknown launch agent state")
    fi
    echo "enabled but missing: ${missing[*]}"
    exit 1
  fi
  echo "disabled"
  exit 1
}

write_config_watch_agent() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$CONFIG_WATCH_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$(xml_escape "$CONFIG_WATCH_LABEL")</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$(xml_escape "$TEMPLATE_ROOT/watch_config.sh")</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$ROOT")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(xml_escape "$TEMPLATE_ROOT/bin:$VENV_DIR/bin:/usr/bin:/bin:/usr/sbin:/sbin")</string>
    <key>LITELLM_RUNTIME_ROOT</key>
    <string>$(xml_escape "$ROOT")</string>
    <key>LITELLM_TEMPLATE_ROOT</key>
    <string>$(xml_escape "$TEMPLATE_ROOT")</string>
    <key>LITELLM_CONTROL_PATH</key>
    <string>$(xml_escape "$TEMPLATE_ROOT/service.sh")</string>
    <key>LITELLM_CONFIG_FILE</key>
    <string>$(xml_escape "$CONFIG_FILE")</string>
    <key>LITELLM_CONFIG_WATCH_LOG</key>
    <string>$(xml_escape "$CONFIG_WATCH_LOG")</string>
    <key>LITELLM_MENU_LOG_MAX_BYTES</key>
    <string>$(xml_escape "$LOCAL_LOG_MAX_BYTES")</string>
    <key>LITELLM_MENU_RUNTIME_SETTINGS_FILE</key>
    <string>$(xml_escape "$RUNTIME_SETTINGS_FILE")</string>
    <key>LITELLM_CONFIG_WATCH_INTERVAL</key>
    <string>$(xml_escape "$CONFIG_WATCH_INTERVAL")</string>
    <key>LITELLM_CONFIG_WATCH_SETTLE_INTERVAL</key>
    <string>$(xml_escape "$CONFIG_WATCH_SETTLE_INTERVAL")</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$CONFIG_WATCH_LOG")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$CONFIG_WATCH_LOG")</string>
</dict>
</plist>
PLIST
  chmod 600 "$CONFIG_WATCH_PLIST"
  plutil -lint "$CONFIG_WATCH_PLIST" >/dev/null
}

enable_config_watch() {
  write_config_watch_agent
  launchctl bootout "$LAUNCHCTL_DOMAIN" "$CONFIG_WATCH_PLIST" >/dev/null 2>&1 || true
  launchctl bootstrap "$LAUNCHCTL_DOMAIN" "$CONFIG_WATCH_PLIST"
  launchctl enable "$LAUNCHCTL_DOMAIN/$CONFIG_WATCH_LABEL" >/dev/null 2>&1 || true
  launchctl kickstart -k "$LAUNCHCTL_DOMAIN/$CONFIG_WATCH_LABEL" >/dev/null 2>&1 || true
  echo "Config staging watcher enabled: $CONFIG_WATCH_PLIST"
}

ensure_config_watch() {
  enable_config_watch
}

disable_config_watch() {
  launchctl bootout "$LAUNCHCTL_DOMAIN" "$CONFIG_WATCH_PLIST" >/dev/null 2>&1 || true
  rm -f "$CONFIG_WATCH_PLIST"
  echo "Config staging watcher disabled"
}

config_watch_status() {
  if launchctl print "$LAUNCHCTL_DOMAIN/$CONFIG_WATCH_LABEL" >/dev/null 2>&1; then
    echo "running"
    exit 0
  fi
  if [[ -f "$CONFIG_WATCH_PLIST" ]]; then
    echo "enabled but not running"
    exit 1
  fi
  echo "disabled"
  exit 1
}

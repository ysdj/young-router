# shellcheck shell=bash

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

enable_autostart() {
  ensure_runtime_layout
  mkdir -p "$(dirname "$AUTOSTART_STATE_FILE")"
  printf '1\n' > "$AUTOSTART_STATE_FILE"
  chmod 600 "$AUTOSTART_STATE_FILE" 2>/dev/null || true
  write_app_launch_agent
  bootout_app_launch_agent
  bootstrap_launch_agent "$APP_LAUNCH_AGENT_PLIST"
  launchctl enable "$LAUNCHCTL_DOMAIN/$APP_LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
  echo "Auto start enabled:"
  echo "  menu: $APP_LAUNCH_AGENT_PLIST"
}

disable_autostart() {
  bootout_app_launch_agent
  rm -f "$AUTOSTART_STATE_FILE" "$APP_LAUNCH_AGENT_PLIST"
  echo "Auto start disabled"
}

autostart_status() {
  local missing=()
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
  local watcher runner watcher_revision
  watcher="$TEMPLATE_ROOT/watch_config.sh"
  runner="$TEMPLATE_ROOT/service/timestamp_log_runner.py"
  if [[ ! -f "$watcher" || ! -f "$runner" ]]; then
    echo "Could not read config watcher timestamping resources." >&2
    return 1
  fi
  watcher_revision="$(/bin/cat "$watcher" "$runner" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
  if [[ ! "$watcher_revision" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Could not read config watcher revision." >&2
    return 1
  fi
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
    <string>$(xml_escape "$NATIVE_PYTHON")</string>
    <string>$(xml_escape "$TEMPLATE_ROOT/service/timestamp_log_runner.py")</string>
    <string>$(xml_escape "$TEMPLATE_ROOT/watch_config.sh")</string>
    <string>$(xml_escape "$CONFIG_WATCH_LOG")</string>
    <string>$(xml_escape "$ROOT")</string>
    <string>--</string>
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
    <key>LITELLM_CONFIG_WATCH_REVISION</key>
    <string>$watcher_revision</string>
  </dict>
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

config_watch_is_loaded() {
  launchctl print "$LAUNCHCTL_DOMAIN/$CONFIG_WATCH_LABEL" >/dev/null 2>&1
}

wait_for_config_watch_unloaded() {
  local attempt
  for ((attempt = 1; attempt <= 50; attempt++)); do
    config_watch_is_loaded || return 0
    sleep 0.1
  done
  return 1
}

load_config_watch_agent() {
  if config_watch_is_loaded; then
    if ! launchctl bootout "$LAUNCHCTL_DOMAIN/$CONFIG_WATCH_LABEL"; then
      echo "Could not reload config watcher: $CONFIG_WATCH_LABEL" >&2
      return 1
    fi
    if ! wait_for_config_watch_unloaded; then
      echo "Config watcher did not unload before reload: $CONFIG_WATCH_LABEL" >&2
      return 1
    fi
  fi
  bootstrap_launch_agent "$CONFIG_WATCH_PLIST"
  launchctl enable "$LAUNCHCTL_DOMAIN/$CONFIG_WATCH_LABEL" >/dev/null 2>&1 || true
}

enable_config_watch() {
  write_config_watch_agent
  load_config_watch_agent
  echo "Config staging watcher enabled: $CONFIG_WATCH_PLIST"
}

ensure_config_watch() {
  local previous_plist="" had_previous=0
  if [[ -f "$CONFIG_WATCH_PLIST" ]]; then
    previous_plist="$(<"$CONFIG_WATCH_PLIST")"
    had_previous=1
  fi

  write_config_watch_agent
  if (( had_previous == 1 )) \
    && [[ "$previous_plist" == "$(<"$CONFIG_WATCH_PLIST")" ]] \
    && launchctl print "$LAUNCHCTL_DOMAIN/$CONFIG_WATCH_LABEL" >/dev/null 2>&1; then
    echo "Config staging watcher already enabled: $CONFIG_WATCH_PLIST"
    return 0
  fi

  load_config_watch_agent
  echo "Config staging watcher enabled: $CONFIG_WATCH_PLIST"
}

disable_config_watch() {
  if config_watch_is_loaded; then
    if ! launchctl bootout "$LAUNCHCTL_DOMAIN/$CONFIG_WATCH_LABEL"; then
      echo "Could not unload config watcher: $CONFIG_WATCH_LABEL" >&2
      return 1
    fi
    if ! wait_for_config_watch_unloaded; then
      echo "Config watcher did not unload: $CONFIG_WATCH_LABEL" >&2
      return 1
    fi
  fi
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

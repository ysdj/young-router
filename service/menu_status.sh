# shellcheck shell=bash
menu_service_state() {
  local expected_owner="${LITELLM_MENU_OWNER_PID:-}"
  if health_ok \
    && { native_running || [[ -n "$(native_port_pids)" ]]; } \
    && { [[ -z "$expected_owner" ]] || native_owned_by_menu_pid "$expected_owner"; }; then
    echo "running"
  elif [[ "$(recent_state || true)" == "starting" ]]; then
    echo "starting"
  elif native_running || [[ -n "$(native_port_pids)" ]] || health_ok; then
    echo "unhealthy"
  else
    echo "stopped"
  fi
}

menu_status_json() {
  local service_state auto_start_state python webdav_enabled

  service_state="$(menu_service_state)"

  if [[ -f "$AUTOSTART_STATE_FILE" && -f "$APP_LAUNCH_AGENT_PLIST" ]]; then
    auto_start_state="enabled"
  elif [[ -f "$AUTOSTART_STATE_FILE" ]]; then
    auto_start_state="incomplete"
  else
    auto_start_state="disabled"
  fi

  webdav_enabled="false"
  webdav_sync_enabled && webdav_enabled="true"

  python="$(runtime_settings_python)" || return 1
  "$python" "$TEMPLATE_ROOT/menu_status.py" \
    --service-state "$service_state" \
    --auto-start-state "$auto_start_state" \
    --webdav-enabled "$webdav_enabled" \
    --webdav-status-file "$WEBDAV_SYNC_STATUS_FILE" \
    --recovery-state-file "$ROUTE_RECOVERY_STATE_FILE" \
    --cooldown-state-file "$DEPLOYMENT_COOLDOWN_FILE"
}

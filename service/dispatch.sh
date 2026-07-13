# shellcheck shell=bash
enforce_isolated_service_target "$ACTION"

case "$ACTION" in
  bootstrap)
    ensure_native_environment
    sync_runtime_config
    echo "LiteLLM Menu runtime ready: $ROOT"
    ;;
  config-editor-bootstrap)
    ensure_config_editor_environment
    echo "Config editor runtime ready: $ROOT"
    ;;
  start)
    with_service_lifecycle_lock start start_server
    ;;
  run-native)
    run_native_process
    ;;
  stop)
    stop_server
    ;;
  reload)
    with_service_lifecycle_lock reload reload_server
    ;;
  restart|hard-restart)
    with_service_lifecycle_lock "${1:-restart}" restart_server
    ;;
  apply-config)
    with_service_lifecycle_lock apply-config apply_config
    ;;
	  status)
	    if ! menu_app_running && [[ -n "$(native_pid_candidates || true)" ]]; then
	      stop_orphaned_service_if_menu_missing
	      echo "stopped"
	      exit 1
	    fi
	    if health_ok && { native_running || launch_agent_loaded; }; then
	      clear_state
	      echo "running"
      exit 0
    fi
    if health_ok && [[ -n "$(native_port_pids)" ]]; then
      clear_state
      echo "running"
      exit 0
    fi
    if health_ok; then
      echo "unmanaged"
      exit 3
    fi
    state="$(recent_state || true)"
    if [[ "$state" == "starting" ]]; then
      echo "starting"
      exit 2
    fi
    if native_running; then
      echo "unhealthy"
      exit 3
    fi
    if [[ -n "$(native_port_pids)" ]]; then
      echo "unhealthy"
      exit 3
    fi
    echo "stopped"
    exit 1
    ;;
  tail)
    rotate_log_if_needed "$LOG_FILE"
    if [[ -f "$LOG_FILE" ]]; then
      tail -n 120 "$LOG_FILE"
    else
      echo "No service log file yet: $LOG_FILE"
    fi
    ;;
  recent-requests)
    recent_requests_log
    ;;
  logs-summary)
    logs_summary
    ;;
  menu-actions-tail)
    menu_actions_tail
    ;;
  route-trace)
    route_trace_log
    ;;
  route-trace-html)
    route_trace_html
    ;;
  route-recovery-html)
    route_recovery_html
    ;;
  route-recovery-summary)
    route_recovery_summary
    ;;
  route-trace-enable)
    route_trace_enable
    ;;
  route-trace-disable)
    route_trace_disable
    ;;
  route-trace-status)
    route_trace_status
    ;;
  computer-facade-smoke)
    computer_facade_smoke
    ;;
  runtime-settings)
    runtime_settings_json
    ;;
  runtime-settings-configure)
    runtime_settings_configure
    ;;
  runtime-settings-reset)
    runtime_settings_reset
    ;;
  webdav-settings)
    webdav_sync_settings
    ;;
  webdav-configure)
    webdav_sync_configure
    ;;
  webdav-enable)
    webdav_sync_enable
    ;;
  webdav-disable)
    webdav_sync_disable
    ;;
  webdav-enabled-status)
    webdav_sync_enabled_status
    ;;
  webdav-status)
    webdav_sync_status
    ;;
  webdav-last-status)
    webdav_sync_last_status
    ;;
  webdav-sync-interval-seconds)
    webdav_sync_interval_seconds
    ;;
  webdav-probe)
    webdav_sync_probe
    ;;
  webdav-push)
    webdav_sync_push
    ;;
  webdav-sync)
    webdav_sync_sync
    ;;
  webdav-pull)
    webdav_sync_pull
    ;;
  autostart-enable)
    enable_autostart
    ;;
  autostart-disable)
    disable_autostart
    ;;
  autostart-status)
    autostart_status
    ;;
  config-watch-enable)
    enable_config_watch
    ;;
  config-watch-ensure)
    ensure_config_watch
    ;;
  config-watch-disable)
    disable_config_watch
    ;;
  config-watch-status)
    config_watch_status
    ;;
  config-watch-tail)
    rotate_log_if_needed "$CONFIG_WATCH_LOG"
    if [[ -f "$CONFIG_WATCH_LOG" ]]; then
      tail -n 80 "$CONFIG_WATCH_LOG"
    else
      echo "No config watch log file yet: $CONFIG_WATCH_LOG"
    fi
    ;;
  stage-config)
    ensure_python_tools
    sync_runtime_config
    ;;
  codex-local-config)
    ensure_python_tools
    LITELLM_CONFIG_FILE="$CONFIG_FILE" \
      LITELLM_RUNTIME_ROOT="$ROOT" \
      "$PYTHON" "$TEMPLATE_ROOT/codex_config.py" local
    ;;
  codex-local-exec)
    ensure_python_tools
    codex_runtime_config="$RUNTIME_CONFIG"
    if [[ ! -f "$codex_runtime_config" ]]; then
      codex_runtime_config="$CONFIG_FILE"
    fi
    LITELLM_CONFIG_FILE="$codex_runtime_config" \
      LITELLM_RUNTIME_ROOT="$ROOT" \
      "$PYTHON" "$TEMPLATE_ROOT/codex_launcher.py" exec "${@:2}"
    ;;
  codex-reapply-pre-switch-config)
    ensure_python_tools
    LITELLM_CONFIG_FILE="$CONFIG_FILE" \
      LITELLM_RUNTIME_ROOT="$ROOT" \
      "$PYTHON" "$TEMPLATE_ROOT/codex_config.py" reapply-pre-switch
    ;;
  validate)
    ensure_python_tools
    validate_config_file "$CONFIG_FILE"
    ;;
  verify-runtime-config)
    ensure_python_tools
    verify_runtime_config
    ;;
  *)
    echo "usage: $0 {bootstrap|config-editor-bootstrap|start|run-native|stop|reload|restart|hard-restart|apply-config|status|tail|recent-requests|logs-summary|menu-actions-tail|route-trace|route-trace-html|route-recovery-html|route-recovery-summary|route-trace-enable|route-trace-disable|route-trace-status|computer-facade-smoke|runtime-settings|runtime-settings-configure|runtime-settings-reset|webdav-settings|webdav-configure|webdav-enable|webdav-disable|webdav-enabled-status|webdav-status|webdav-last-status|webdav-sync-interval-seconds|webdav-probe|webdav-sync|webdav-push|webdav-pull|validate|verify-runtime-config|stage-config|autostart-enable|autostart-disable|autostart-status|config-watch-enable|config-watch-ensure|config-watch-disable|config-watch-status|config-watch-tail|codex-local-config|codex-local-exec|codex-reapply-pre-switch-config}" >&2
    exit 64
    ;;
esac

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
  restart)
    with_service_lifecycle_lock restart restart_server
    ;;
  apply-config)
    with_service_lifecycle_lock apply-config apply_config
    ;;
  menu-status)
    menu_status_json
    ;;
  status)
    if health_ok \
      && native_running \
      && { [[ -z "${LITELLM_MENU_OWNER_PID:-}" ]] || native_owned_by_menu_pid "$LITELLM_MENU_OWNER_PID"; }; then
      clear_state
      echo "running"
      exit 0
    fi
    if health_ok \
      && [[ -n "$(native_port_pids)" ]] \
      && { [[ -z "${LITELLM_MENU_OWNER_PID:-}" ]] || native_owned_by_menu_pid "$LITELLM_MENU_OWNER_PID"; }; then
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
  computer-facade-smoke)
    computer_facade_smoke
    ;;
  runtime-settings)
    runtime_settings_json
    ;;
  runtime-settings-configure)
    runtime_settings_configure
    ;;
  runtime-settings-apply)
    with_service_lifecycle_lock runtime-settings-apply runtime_settings_apply
    ;;
  runtime-settings-save)
    with_service_lifecycle_lock runtime-settings-save runtime_settings_save
    ;;
  configuration-package-export)
    ensure_config_editor_environment
    package_sections=""
    package_arguments=("${@:2}")
    for ((package_index = 0; package_index < ${#package_arguments[@]}; package_index += 1)); do
      if [[ "${package_arguments[package_index]}" == "--sections" ]] \
        && (( package_index + 1 < ${#package_arguments[@]} )); then
        package_sections="${package_arguments[package_index + 1]}"
        break
      fi
    done
    case "$package_sections" in
      runtime_settings)
        "$PYTHON" "$TEMPLATE_ROOT/configuration_package.py" export \
          --settings-file "$RUNTIME_SETTINGS_FILE" "${package_arguments[@]}"
        ;;
      providers_models)
        "$PYTHON" "$TEMPLATE_ROOT/configuration_package.py" export \
          --config "$CONFIG_FILE" "${package_arguments[@]}"
        ;;
      all)
        "$PYTHON" "$TEMPLATE_ROOT/configuration_package.py" export \
          --config "$CONFIG_FILE" \
          --settings-file "$RUNTIME_SETTINGS_FILE" \
          "${package_arguments[@]}"
        ;;
      *)
        echo "Configuration package export requires --sections runtime_settings, providers_models, or all." >&2
        exit 64
        ;;
    esac
    ;;
  configuration-package-import)
    ensure_config_editor_environment
    "$PYTHON" "$TEMPLATE_ROOT/configuration_package.py" import "${@:2}"
    ;;
  external-provider-import)
    ensure_config_editor_environment
    "$PYTHON" "$TEMPLATE_ROOT/external_provider_import.py" "${@:2}"
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
  codex-config-status)
    ensure_config_editor_environment
    LITELLM_CONFIG_FILE="$CONFIG_FILE" \
      LITELLM_RUNTIME_ROOT="$ROOT" \
      "$PYTHON" "$TEMPLATE_ROOT/codex_config.py" status
    ;;
  codex-config-apply)
    ensure_config_editor_environment
    LITELLM_CONFIG_FILE="$CONFIG_FILE" \
      LITELLM_RUNTIME_ROOT="$ROOT" \
      "$PYTHON" "$TEMPLATE_ROOT/codex_config.py" apply "${@:2}"
    ;;
  codex-config-editor-load)
    ensure_config_editor_environment
    LITELLM_CONFIG_FILE="$CONFIG_FILE" \
      LITELLM_RUNTIME_ROOT="$ROOT" \
      "$PYTHON" "$TEMPLATE_ROOT/codex_config.py" load
    ;;
  codex-config-editor-sync)
    ensure_config_editor_environment
    LITELLM_CONFIG_FILE="$CONFIG_FILE" \
      LITELLM_RUNTIME_ROOT="$ROOT" \
      "$PYTHON" "$TEMPLATE_ROOT/codex_config.py" sync
    ;;
  codex-config-editor-apply)
    ensure_config_editor_environment
    LITELLM_CONFIG_FILE="$CONFIG_FILE" \
      LITELLM_RUNTIME_ROOT="$ROOT" \
      "$PYTHON" "$TEMPLATE_ROOT/codex_config.py" apply-editor
    ;;
  provider-billing)
    ensure_config_editor_environment
    billing_config="$RUNTIME_CONFIG"
    [[ -f "$billing_config" ]] || billing_config="$CONFIG_FILE"
    LITELLM_CONFIG_FILE="$billing_config" \
      "$PYTHON" "$TEMPLATE_ROOT/provider_billing.py" --config "$billing_config"
    ;;
  remote-usage-logs)
    ensure_config_editor_environment
    "$PYTHON" "$TEMPLATE_ROOT/remote_usage_logs.py" --config "$CONFIG_FILE"
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
    echo "usage: $0 {bootstrap|config-editor-bootstrap|start|run-native|stop|reload|restart|apply-config|menu-status|status|tail|recent-requests|logs-summary|menu-actions-tail|route-trace|computer-facade-smoke|runtime-settings|runtime-settings-configure|runtime-settings-apply|runtime-settings-save|configuration-package-export|configuration-package-import|external-provider-import|webdav-settings|webdav-configure|webdav-enable|webdav-disable|webdav-enabled-status|webdav-status|webdav-sync-interval-seconds|webdav-probe|webdav-sync|webdav-push|webdav-pull|validate|verify-runtime-config|stage-config|autostart-enable|autostart-disable|autostart-status|config-watch-enable|config-watch-ensure|config-watch-disable|config-watch-status|config-watch-tail|codex-config-status|codex-config-apply|codex-config-editor-load|codex-config-editor-sync|codex-config-editor-apply|provider-billing|remote-usage-logs}" >&2
    exit 64
    ;;
esac

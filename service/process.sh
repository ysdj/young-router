# shellcheck shell=bash

native_owned_pid() {
  local pid="$1" command
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ -n "$command" ]] || return 1
  [[ "$command" == *"$LITELLM_BIN"* ]] \
    || [[ "$command" == *"$VENV_DIR"*"/bin/python"* && "$command" == *"litellm"* ]] \
    || [[ "$command" == *"gunicorn"* && "$command" == *"litellm"* ]]
}

native_pid_alive() {
  local pid="$1"
  native_owned_pid "$pid" && kill -0 "$pid" >/dev/null 2>&1
}

native_pid_from_file() {
  local pid=""
  [[ -f "$NATIVE_PID_FILE" ]] || return 1
  pid="$(tr -d '[:space:]' < "$NATIVE_PID_FILE" 2>/dev/null || true)"
  native_pid_alive "$pid" || return 1
  echo "$pid"
}

native_running() {
  native_pid_from_file >/dev/null 2>&1
}

native_owner_file_path() {
  if [[ -n "${NATIVE_OWNER_FILE:-}" ]]; then
    printf '%s\n' "$NATIVE_OWNER_FILE"
    return 0
  fi
  [[ -n "${NATIVE_PID_FILE:-}" ]] || return 1
  printf '%s/litellm.owner\n' "$(dirname "$NATIVE_PID_FILE")"
}

native_owner_record() {
  local owner_file owner_pid native_pid extra
  owner_file="$(native_owner_file_path)" || return 1
  [[ -f "$owner_file" ]] || return 1
  IFS=' ' read -r owner_pid native_pid extra < "$owner_file" || return 1
  [[ "$owner_pid" =~ ^[0-9]+$ && "$native_pid" =~ ^[0-9]+$ && -z "$extra" ]] || return 1
  printf '%s %s\n' "$owner_pid" "$native_pid"
}

native_owned_by_menu_pid() {
  local expected_owner="$1" record owner_pid native_pid current_pid
  [[ "$expected_owner" =~ ^[0-9]+$ ]] || return 1
  process_is_menu_app_pid "$expected_owner" || return 1
  record="$(native_owner_record)" || return 1
  read -r owner_pid native_pid <<<"$record"
  current_pid="$(native_pid_from_file)" || return 1
  [[ "$owner_pid" == "$expected_owner" && "$native_pid" == "$current_pid" ]]
}

write_native_lifecycle_records() {
  local owner_pid="$1" native_pid="$2" owner_file owner_temp pid_temp
  [[ "$owner_pid" =~ ^[0-9]+$ && "$native_pid" =~ ^[0-9]+$ ]] || return 1
  owner_file="$(native_owner_file_path)" || return 1
  mkdir -p "$(dirname "$NATIVE_PID_FILE")" "$(dirname "$owner_file")"
  owner_temp="${owner_file}.tmp.$$"
  pid_temp="${NATIVE_PID_FILE}.tmp.$$"
  printf '%s %s\n' "$owner_pid" "$native_pid" > "$owner_temp"
  printf '%s\n' "$native_pid" > "$pid_temp"
  chmod 600 "$owner_temp" "$pid_temp"
  mv -f "$owner_temp" "$owner_file"
  mv -f "$pid_temp" "$NATIVE_PID_FILE"
}

native_owner_for_pid() {
  local expected_native_pid="$1" record owner_pid recorded_native_pid
  [[ "$expected_native_pid" =~ ^[0-9]+$ ]] || return 1
  record="$(native_owner_record)" || return 1
  read -r owner_pid recorded_native_pid <<<"$record"
  [[ "$recorded_native_pid" == "$expected_native_pid" ]] || return 1
  printf '%s\n' "$owner_pid"
}

adopt_native_service_for_menu_pid() {
  local new_owner_pid="$1" native_pid record recorded_owner_pid recorded_native_pid
  [[ "$new_owner_pid" =~ ^[0-9]+$ ]] || return 1
  process_is_menu_app_pid "$new_owner_pid" || return 1
  native_pid="$(native_pid_from_file 2>/dev/null || true)"
  [[ -n "$native_pid" ]] || native_pid="$(native_master_pid)" || return 1
  native_pid_alive "$native_pid" || return 1

  record="$(native_owner_record || true)"
  if [[ -z "$record" ]]; then
    # A retained service can outlive the process that was about to maintain its
    # lifecycle records. Recover the records instead of restarting the proxy.
    write_native_lifecycle_records "$new_owner_pid" "$native_pid"
    return $?
  fi
  read -r recorded_owner_pid recorded_native_pid <<<"$record"

  if [[ "$recorded_owner_pid" == "$new_owner_pid" && "$recorded_native_pid" == "$native_pid" ]]; then
    write_native_lifecycle_records "$new_owner_pid" "$native_pid"
    return $?
  fi
  # A second live menu process must not steal a service from its current owner.
  process_is_menu_app_pid "$recorded_owner_pid" && return 1
  write_native_lifecycle_records "$new_owner_pid" "$native_pid"
}

watch_native_owner() {
  local native_pid="$1" owner_pid="$2" replacement_owner
  local owner_missing_since=-1 owner_grace_seconds="${OWNER_GRACE_SECONDS:-30}"
  local log_rotation_countdown=0 now_seconds
  [[ "$owner_grace_seconds" =~ ^[0-9]+$ ]] || owner_grace_seconds=30
  while kill -0 "$native_pid" >/dev/null 2>&1; do
    if (( log_rotation_countdown <= 0 )); then
      rotate_log_if_needed "$LOG_FILE"
      log_rotation_countdown=30
    else
      log_rotation_countdown=$((log_rotation_countdown - 1))
    fi

    if process_is_menu_app_pid "$owner_pid"; then
      owner_missing_since=-1
    else
      replacement_owner="$(native_owner_for_pid "$native_pid" || true)"
      if [[ "$replacement_owner" != "$owner_pid" ]] \
        && process_is_menu_app_pid "$replacement_owner"; then
        {
          printf '[%s] LiteLLM Menu owner handoff: pid %s -> pid %s for native LiteLLM service pid %s\n' \
            "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$owner_pid" "$replacement_owner" "$native_pid"
        } >>"$LOG_FILE"
        owner_pid="$replacement_owner"
        owner_missing_since=-1
      else
        now_seconds="$SECONDS"
        if (( owner_missing_since < 0 )); then
          owner_missing_since="$now_seconds"
          {
            printf '[%s] LiteLLM Menu owner pid %s exited; retaining native LiteLLM service pid %s for recovery or handoff\n' \
              "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$owner_pid" "$native_pid"
          } >>"$LOG_FILE"
        elif (( now_seconds - owner_missing_since >= owner_grace_seconds )); then
          {
            printf '[%s] LiteLLM Menu owner pid %s was not replaced within %ss; stopping native LiteLLM service pid %s\n' \
              "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$owner_pid" "$owner_grace_seconds" "$native_pid"
          } >>"$LOG_FILE"
          kill "$native_pid" >/dev/null 2>&1 || true
          sleep 2
          kill -KILL "$native_pid" >/dev/null 2>&1 || true
          return 0
        fi
      fi
    fi
    sleep 1
  done
}

native_port_pids() {
  local pid
  command -v lsof >/dev/null 2>&1 || return 0
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    native_owned_pid "$pid" && printf '%s\n' "$pid"
  done < <(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
}

native_pid_candidates() {
  {
    native_pid_from_file 2>/dev/null || true
    native_port_pids
  } | awk 'NF && !seen[$0]++'
}

native_live_pid_candidates() {
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    native_pid_alive "$pid" && printf '%s\n' "$pid"
  done < <(native_pid_candidates)
}

process_ppid() {
  local pid="$1"
  ps -p "$pid" -o ppid= 2>/dev/null | awk 'NF { print $1; exit }'
}

native_master_pid() {
  local pid recorded_pid candidates ppid
  recorded_pid="$(native_pid_from_file 2>/dev/null || true)"
  candidates="$(native_port_pids || true)"
  if [[ -z "$candidates" ]]; then
    [[ -n "$recorded_pid" ]] || return 1
    echo "$recorded_pid"
    return 0
  fi
  if [[ -n "$recorded_pid" ]] && pid_list_contains "$recorded_pid" <<<"$candidates"; then
    echo "$recorded_pid"
    return 0
  fi

  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    ppid="$(process_ppid "$pid")"
    if [[ -z "$ppid" ]] || ! grep -qx "$ppid" <<<"$candidates"; then
      echo "$pid"
      return 0
    fi
  done <<<"$candidates"

  printf '%s\n' "$candidates" | head -n 1
}

wait_for_managed_health() {
  local expected_owner="${1:-}" attempt max_attempts
  max_attempts=$((HEALTH_WAIT_SECONDS * 10))
  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if health_ok \
      && { native_running || [[ -n "$(native_port_pids)" ]]; } \
      && { [[ -z "$expected_owner" ]] || native_owned_by_menu_pid "$expected_owner"; }; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

wait_for_native_port_released() {
  local attempt max_attempts pids force_after_attempt
  force_after_attempt="${1:-20}"
  [[ "$force_after_attempt" =~ ^[0-9]+$ ]] || force_after_attempt=20
  (( force_after_attempt > 0 )) || force_after_attempt=1
  max_attempts=$((HEALTH_WAIT_SECONDS * 10))
  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    pids="$(native_port_pids || true)"
    [[ -z "$pids" ]] && return 0
    request_native_process_stop_list "$pids" >/dev/null 2>&1 || true
    if (( attempt >= force_after_attempt )); then
      force_native_process_stop_list "$pids" >/dev/null 2>&1 || true
    fi
    sleep 0.1
  done
  return 1
}

wait_for_native_processes_stopped() {
  local attempt max_attempts pids force_after_attempt requested_max_attempts
  force_after_attempt="${1:-20}"
  requested_max_attempts="${2:-$((HEALTH_WAIT_SECONDS * 10))}"
  [[ "$force_after_attempt" =~ ^[0-9]+$ ]] || force_after_attempt=20
  [[ "$requested_max_attempts" =~ ^[0-9]+$ ]] || requested_max_attempts=$((HEALTH_WAIT_SECONDS * 10))
  (( force_after_attempt > 0 )) || force_after_attempt=1
  (( requested_max_attempts > 0 )) || requested_max_attempts=1
  max_attempts="$requested_max_attempts"

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    pids="$(native_live_pid_candidates || true)"
    [[ -z "$pids" ]] && return 0
    request_native_process_stop_list "$pids" >/dev/null 2>&1 || true
    if (( attempt >= force_after_attempt )); then
      force_native_process_stop_list "$pids" >/dev/null 2>&1 || true
    fi
    sleep 0.1
  done
  return 1
}

print_native_health_failure() {
  local context="$1"
  echo "$context" >&2
  echo "Last LiteLLM service log lines from $LOG_FILE:" >&2
  if [[ -f "$LOG_FILE" ]]; then
    tail -n 120 "$LOG_FILE" >&2 || true
  else
    echo "(no log file yet)" >&2
  fi
}

release_service_lifecycle_lock() {
  if [[ "${SERVICE_LIFECYCLE_LOCK_HELD:-0}" == "1" ]]; then
    rm -rf "$SERVICE_LIFECYCLE_LOCK_DIR"
    SERVICE_LIFECYCLE_LOCK_HELD=0
  fi
}

acquire_service_lifecycle_lock() {
  local action="$1" attempt max_attempts owner_file owner_pid
  mkdir -p "$RUNTIME_DIR"
  owner_file="$SERVICE_LIFECYCLE_LOCK_DIR/owner"
  max_attempts=$((SERVICE_LIFECYCLE_LOCK_WAIT_SECONDS * 10))

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if mkdir "$SERVICE_LIFECYCLE_LOCK_DIR" 2>/dev/null; then
      {
        printf '%s\n' "$$"
        printf '%s\n' "$action"
        date -u '+%Y-%m-%dT%H:%M:%SZ'
      } > "$owner_file"
      SERVICE_LIFECYCLE_LOCK_HELD=1
      return 0
    fi

    owner_pid=""
    if [[ -f "$owner_file" ]]; then
      IFS= read -r owner_pid < "$owner_file" || true
    fi
    if [[ "$owner_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$owner_pid" >/dev/null 2>&1; then
      rm -rf "$SERVICE_LIFECYCLE_LOCK_DIR"
      continue
    fi
    if (( attempt >= 20 )) && [[ -z "$owner_pid" ]]; then
      rm -rf "$SERVICE_LIFECYCLE_LOCK_DIR"
      continue
    fi
    sleep 0.1
  done

  echo "Timed out waiting for LiteLLM service lifecycle lock: $SERVICE_LIFECYCLE_LOCK_DIR" >&2
  return 1
}

with_service_lifecycle_lock() {
  local action="$1" status
  shift

  acquire_service_lifecycle_lock "$action" || return 1
  trap release_service_lifecycle_lock EXIT
  "$@"
  status=$?
  release_service_lifecycle_lock
  trap - EXIT
  return "$status"
}

run_native_process() {
  local owner_pid native_pid watchdog_pid exit_code
  local same_deployment_retries recovery_policy_balance recovery_policy_rate_limit
  local recovery_policy_server recovery_policy_network recovery_policy_stream_start_timeout
  local recovery_policy_stream_idle_timeout recovery_policy_request_error
  owner_pid="$(require_menu_app_owner "run-native")" || exit $?
  if health_ok; then
    if adopt_native_service_for_menu_pid "$owner_pid"; then
      echo "LiteLLM retained the healthy native service on http://127.0.0.1:$PORT; duplicate native start skipped"
      return 0
    fi
    if [[ -n "$(native_port_pids)" ]]; then
      echo "LiteLLM is already healthy under another live menu owner; duplicate native start skipped"
      return 0
    fi
    echo "Port $PORT is serving LiteLLM health, but no owned native process could be identified." >&2
    return 1
  fi
  same_deployment_retries="${SAME_DEPLOYMENT_RETRIES:-0}"
  recovery_policy_balance="${RECOVERY_POLICY_BALANCE:-recovery_cooldown}"
  recovery_policy_rate_limit="${RECOVERY_POLICY_RATE_LIMIT:-recovery_cooldown}"
  recovery_policy_server="${RECOVERY_POLICY_SERVER:-recovery_cooldown}"
  recovery_policy_network="${RECOVERY_POLICY_NETWORK:-recovery}"
  recovery_policy_stream_start_timeout="${RECOVERY_POLICY_STREAM_START_TIMEOUT:-recovery_cooldown}"
  recovery_policy_stream_idle_timeout="${RECOVERY_POLICY_STREAM_IDLE_TIMEOUT:-recovery}"
  recovery_policy_request_error="${RECOVERY_POLICY_REQUEST_ERROR:-error}"
  ensure_native_environment
  if [[ ! -f "$RUNTIME_CONFIG" ]]; then
    sync_runtime_config
  fi
  if [[ ! -f "$CALLBACK_SOURCE" ]]; then
    echo "Missing LiteLLM callback file: $CALLBACK_SOURCE" >&2
    exit 1
  fi
  if [[ ! -d "$CALLBACK_PACKAGE_DIR" ]]; then
    echo "Missing LiteLLM callback package: $CALLBACK_PACKAGE_DIR" >&2
    exit 1
  fi
  mkdir -p "$RUNTIME_DIR"
  rotate_log_if_needed "$LOG_FILE"
  touch "$LOG_FILE"
  chmod 600 "$LOG_FILE" 2>/dev/null || true

  {
    printf '\n[%s] running native LiteLLM service on %s:%s with %s workers; recycling after %s requests\n' \
      "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$HOST" "$PORT" "$NATIVE_WORKERS" "$NATIVE_MAX_REQUESTS_BEFORE_RESTART"
  } >>"$LOG_FILE"

  cd "$ROOT"
  apply_system_proxy_guard

  env \
    LITELLM_MASTER_KEY="$MASTER_KEY" \
    LITELLM_MENU_PROXY_PROCESS=1 \
    LITELLM_RECENT_REQUESTS_LOG="$RECENT_REQUESTS_LOG" \
    LITELLM_MENU_LOG_MAX_BYTES="$LOCAL_LOG_MAX_BYTES" \
    LITELLM_MENU_REQUEST_TIMEOUT_SECONDS="$REQUEST_TIMEOUT_SECONDS" \
    LITELLM_MENU_STALL_TIMEOUT_SECONDS="$STALL_TIMEOUT_SECONDS" \
    LITELLM_MENU_STREAM_START_TIMEOUT_SECONDS="$STREAM_START_TIMEOUT_SECONDS" \
    LITELLM_MENU_CODEX_COMPACTION_START_TIMEOUT_SECONDS="$CODEX_COMPACTION_START_TIMEOUT_SECONDS" \
    LITELLM_MENU_RECOVERY_MAX_SECONDS="$RECOVERY_MAX_SECONDS" \
    LITELLM_MENU_RECOVERY_INTERVAL_SECONDS="$RECOVERY_INTERVAL_SECONDS" \
    LITELLM_MENU_SAME_DEPLOYMENT_RETRIES="$same_deployment_retries" \
    LITELLM_MENU_RECOVERY_POLICY_BALANCE="$recovery_policy_balance" \
    LITELLM_MENU_RECOVERY_POLICY_RATE_LIMIT="$recovery_policy_rate_limit" \
    LITELLM_MENU_RECOVERY_POLICY_SERVER="$recovery_policy_server" \
    LITELLM_MENU_RECOVERY_POLICY_NETWORK="$recovery_policy_network" \
    LITELLM_MENU_RECOVERY_POLICY_STREAM_START_TIMEOUT="$recovery_policy_stream_start_timeout" \
    LITELLM_MENU_RECOVERY_POLICY_STREAM_IDLE_TIMEOUT="$recovery_policy_stream_idle_timeout" \
    LITELLM_MENU_RECOVERY_POLICY_REQUEST_ERROR="$recovery_policy_request_error" \
    LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS="$WEB_FETCH_TIMEOUT_SECONDS" \
    LITELLM_MENU_WEB_SEARCH_MAX_RESULTS="$WEB_SEARCH_MAX_RESULTS" \
    LITELLM_MENU_WEB_SEARCH_READ_RESULTS="$WEB_SEARCH_READ_RESULTS" \
    LITELLM_MENU_WEB_SEARCH_READ_CHARS="$WEB_SEARCH_READ_CHARS" \
    LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND="$WEB_SEARCH_DDGS_BACKEND" \
    LITELLM_MENU_WEB_SEARCH_REGION="$WEB_SEARCH_REGION" \
    LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS="$WEB_SEARCH_MAX_ROUNDS" \
    LITELLM_MENU_WEB_SEARCH_MAX_QUERIES="$WEB_SEARCH_MAX_QUERIES" \
    LITELLM_MENU_WEB_SEARCH_MAX_OPEN_PAGES="$WEB_SEARCH_MAX_OPEN_PAGES" \
    LITELLM_MENU_WEB_SEARCH_MAX_FIND_IN_PAGE="$WEB_SEARCH_MAX_FIND_IN_PAGE" \
    LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES="$EXTERNAL_WEB_SEARCH_MODEL_RETRIES" \
    LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS="$EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS" \
    LITELLM_MENU_IMAGE_TOOL_FALLBACK_MAX_ATTEMPTS="$IMAGE_TOOL_FALLBACK_MAX_ATTEMPTS" \
    LITELLM_MENU_DEPLOYMENT_COOLDOWN_FAILURES="$DEPLOYMENT_COOLDOWN_FAILURES" \
    LITELLM_MENU_DEPLOYMENT_COOLDOWN_SECONDS="$DEPLOYMENT_COOLDOWN_SECONDS" \
    LITELLM_MENU_DEPLOYMENT_COOLDOWN_FILE="${DEPLOYMENT_COOLDOWN_FILE:-$RUNTIME_DIR/deployment-cooldowns.json}" \
    LITELLM_MENU_ROUTE_RECOVERY_STATE_FILE="${ROUTE_RECOVERY_STATE_FILE:-$RUNTIME_DIR/route-recovery-state.json}" \
    LITELLM_MENU_COMPUTER_FACADE_BACKEND="$COMPUTER_FACADE_BACKEND" \
    LITELLM_MENU_COMPUTER_FACADE_MODEL="$COMPUTER_FACADE_MODEL" \
    LITELLM_MENU_COMPUTER_FACADE_MAX_STEPS="$COMPUTER_FACADE_MAX_STEPS" \
    LITELLM_MENU_COMPUTER_FACADE_TRACE="$COMPUTER_FACADE_TRACE" \
    LITELLM_MENU_COMPUTER_FACADE_TRACE_SCREENSHOTS="$COMPUTER_FACADE_TRACE_SCREENSHOTS" \
    LITELLM_MENU_COMPUTER_FACADE_ACTION_DENYLIST="$COMPUTER_FACADE_ACTION_DENYLIST" \
    LITELLM_MENU_COMPUTER_FACADE_REQUIRE_OBSERVATION="$COMPUTER_FACADE_REQUIRE_OBSERVATION" \
    LITELLM_LOCAL_MODEL_COST_MAP="$LOCAL_MODEL_COST_MAP" \
    CODEX_HOME="${CODEX_HOME:-$HOME/.codex}" \
    LITELLM_MENU_DISABLE_SYSTEM_PROXY_LOOKUP="${LITELLM_MENU_DISABLE_SYSTEM_PROXY_LOOKUP:-0}" \
    LITELLM_TEMPLATE_ROOT="$TEMPLATE_ROOT" \
    LITELLM_MENU_ROUTE_TRACE_PREVIEW_CHARS="$ROUTE_TRACE_PREVIEW_CHARS" \
    LITELLM_USE_SYSTEM_PROXIES="$(use_system_proxies_value)" \
    LITELLM_WORKER_STARTUP_HOOKS="${LITELLM_WORKER_STARTUP_HOOKS:+${LITELLM_WORKER_STARTUP_HOOKS},}litellm_menu.search_endpoint:register" \
    PYTHONPATH="$TEMPLATE_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$LITELLM_BIN" \
      --config "$RUNTIME_CONFIG" \
      --host "$HOST" \
      --port "$PORT" \
      --num_workers "$NATIVE_WORKERS" \
      --max_requests_before_restart "$NATIVE_MAX_REQUESTS_BEFORE_RESTART" \
      --telemetry "$PROXY_TELEMETRY" \
      --run_gunicorn &
  native_pid="$!"
  if ! write_native_lifecycle_records "$owner_pid" "$native_pid"; then
    echo "Could not record LiteLLM service ownership." >&2
    kill "$native_pid" >/dev/null 2>&1 || true
    wait "$native_pid" >/dev/null 2>&1 || true
    exit 1
  fi

  (
    watch_native_owner "$native_pid" "$owner_pid"
  ) &
  watchdog_pid="$!"

  cleanup_native_child() {
    kill "$watchdog_pid" >/dev/null 2>&1 || true
    if kill -0 "$native_pid" >/dev/null 2>&1; then
      kill "$native_pid" >/dev/null 2>&1 || true
    fi
  }
  trap cleanup_native_child INT TERM

  if wait "$native_pid"; then
    exit_code=0
  else
    exit_code=$?
  fi
  kill "$watchdog_pid" >/dev/null 2>&1 || true
  wait "$watchdog_pid" >/dev/null 2>&1 || true
  clear_native_pid_file_if_stale_or_targeted "$native_pid"
  exit "$exit_code"
}

pid_list_contains() {
  local needle="$1" pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    [[ "$pid" == "$needle" ]] && return 0
  done
  return 1
}

clear_native_owner_file_if_stale_or_targeted() {
  local pids="$1" owner_file record owner_pid native_pid
  owner_file="$(native_owner_file_path)" || return 0
  [[ -f "$owner_file" ]] || return 0
  record="$(native_owner_record || true)"
  if [[ -z "$record" ]]; then
    rm -f "$owner_file"
    return 0
  fi
  read -r owner_pid native_pid <<<"$record"
  if pid_list_contains "$native_pid" <<<"$pids"; then
    rm -f "$owner_file"
    return 0
  fi
  native_pid_alive "$native_pid" || rm -f "$owner_file"
}

clear_native_pid_file_if_stale_or_targeted() {
  local pids="$1" current_pid
  if [[ ! -f "$NATIVE_PID_FILE" ]]; then
    clear_native_owner_file_if_stale_or_targeted "$pids"
    return 0
  fi
  current_pid="$(tr -d '[:space:]' < "$NATIVE_PID_FILE" 2>/dev/null || true)"
  if [[ -z "$current_pid" ]]; then
    rm -f "$NATIVE_PID_FILE"
    clear_native_owner_file_if_stale_or_targeted "$pids"
    return 0
  fi
  if pid_list_contains "$current_pid" <<<"$pids"; then
    clear_native_owner_file_if_stale_or_targeted "$pids"
    rm -f "$NATIVE_PID_FILE"
    return 0
  fi
  if ! native_pid_alive "$current_pid"; then
    rm -f "$NATIVE_PID_FILE"
  fi
  clear_native_owner_file_if_stale_or_targeted "$pids"
}

request_native_process_stop_list() {
  local pids="$1" pid
  if [[ -z "$pids" ]]; then
    clear_native_pid_file_if_stale_or_targeted ""
    return 1
  fi

  while IFS= read -r pid; do
    [[ -n "$pid" && "$pid" != "$$" ]] || continue
    kill "$pid" >/dev/null 2>&1 || true
  done <<<"$pids"

  clear_native_pid_file_if_stale_or_targeted "$pids"
}

force_native_process_stop_list() {
  local pids="$1" pid
  [[ -n "$pids" ]] || return 1

  while IFS= read -r pid; do
    [[ -n "$pid" && "$pid" != "$$" ]] || continue
    native_pid_alive "$pid" && kill -KILL "$pid" >/dev/null 2>&1 || true
  done <<<"$pids"

  clear_native_pid_file_if_stale_or_targeted "$pids"
}

request_native_processes_to_stop() {
  local pids
  pids="$(native_pid_candidates || true)"
  request_native_process_stop_list "$pids"
}

bootstrap_launch_agent() {
  local plist="$1" attempt output status
  for ((attempt = 1; attempt <= 10; attempt++)); do
    if output="$(launchctl bootstrap "$LAUNCHCTL_DOMAIN" "$plist" 2>&1)"; then
      return 0
    fi
    status=$?
    if (( attempt == 10 )); then
      printf '%s\n' "$output" >&2
      return "$status"
    fi
    sleep 0.3
  done
}

start_native_detached() {
  local owner_pid="${1:-}"
  if [[ -z "$owner_pid" ]]; then
    owner_pid="$(require_menu_app_owner "start")" || return $?
  fi
  mkdir -p "$RUNTIME_DIR"
  rotate_log_if_needed "$LOG_FILE"
  touch "$LOG_FILE"
  chmod 600 "$LOG_FILE" 2>/dev/null || true
  local log_runner="$TEMPLATE_ROOT/service/timestamp_log_runner.py"
  if [[ ! -f "$log_runner" ]]; then
    echo "Missing LiteLLM service log runner." >&2
    return 1
  fi
  LITELLM_MENU_OWNER_PID="$owner_pid" "$NATIVE_PYTHON" - "$log_runner" "$TEMPLATE_ROOT/service.sh" "$LOG_FILE" "$ROOT" <<'PY'
from __future__ import annotations

import os
import subprocess
import sys

runner, script, log_file, root = sys.argv[1:5]
env = os.environ.copy()
subprocess.Popen(
    [sys.executable, runner, script, log_file, root],
    cwd=root,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    env=env,
    close_fds=True,
    start_new_session=True,
)
PY
}

start_service_process() {
  start_native_detached "${1:-}"
}

clear_transient_routing_state() {
  local runtime_dir cooldown_path recovery_path python_bin
  runtime_dir="${RUNTIME_DIR:-}"
  cooldown_path="${DEPLOYMENT_COOLDOWN_FILE:-}"
  recovery_path="${ROUTE_RECOVERY_STATE_FILE:-}"
  if [[ -z "$cooldown_path" && -n "$runtime_dir" ]]; then
    cooldown_path="$runtime_dir/deployment-cooldowns.json"
  fi
  if [[ -z "$recovery_path" && -n "$runtime_dir" ]]; then
    recovery_path="$runtime_dir/route-recovery-state.json"
  fi
  if [[ -z "$cooldown_path" && -z "$recovery_path" ]]; then
    return 0
  fi

  python_bin="${PYTHON:-${NATIVE_PYTHON:-}}"
  if [[ -z "$python_bin" || ! -x "$python_bin" ]]; then
    python_bin="$(command -v python3 2>/dev/null || true)"
  fi
  if [[ -z "$python_bin" || ! -x "$python_bin" ]]; then
    echo "Python is required to reset transient routing state." >&2
    return 1
  fi

  "$python_bin" - "$cooldown_path" "$recovery_path" <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
import sys
import tempfile


def reset(path: str, payload: dict) -> None:
    if not path:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    lock_path = f"{path}.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd, temporary = tempfile.mkstemp(
            prefix=f"{os.path.basename(path)}.reset.",
            dir=directory or None,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


cooldown_path, recovery_path = sys.argv[1:3]
reset(cooldown_path, {"schema_version": 1, "cooldowns": {}})
reset(
    recovery_path,
    {
        "recoveries": {},
        "updated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    },
)
PY
}

start_server() {
  local owner_pid
  owner_pid="$(require_menu_app_owner "start")" || return $?
  ensure_runtime_layout
  if managed_server_reachable "$owner_pid"; then
    clear_state
    echo "LiteLLM already running on http://127.0.0.1:$PORT with native worker target $NATIVE_WORKERS"
    return 0
  fi
  if health_ok; then
    # A config watcher only stages changes for an explicit Apply. Do not turn
    # menu-process recovery into a proxy restart because a staged config is
    # pending, lifecycle records are missing, or an older menu owner overlaps.
    if adopt_native_service_for_menu_pid "$owner_pid"; then
      clear_state
      echo "LiteLLM adopted the healthy native service on http://127.0.0.1:$PORT"
      return 0
    fi
    if [[ -n "$(native_port_pids)" ]]; then
      clear_state
      echo "LiteLLM is healthy under another live menu owner; leaving the native service untouched"
      return 0
    fi
    echo "Port $PORT is serving LiteLLM health, but it is not managed by this app." >&2
    return 1
  fi

  ensure_native_environment || exit 1
  sync_runtime_config || exit 1

  if managed_server_reachable "$owner_pid"; then
    clear_state
    echo "LiteLLM already running on http://127.0.0.1:$PORT with native worker target $NATIVE_WORKERS"
    return 0
  elif health_ok; then
    if adopt_native_service_for_menu_pid "$owner_pid"; then
      clear_state
      echo "LiteLLM adopted the healthy native service on http://127.0.0.1:$PORT"
      return 0
    fi
    if [[ -n "$(native_port_pids)" ]]; then
      clear_state
      echo "LiteLLM is healthy under another live menu owner; leaving the native service untouched"
      return 0
    fi
    echo "Port $PORT is already serving LiteLLM health, but it is not managed by this app." >&2
    return 1
  fi

  request_native_processes_to_stop >/dev/null 2>&1 || true
  if ! wait_for_native_port_released 5; then
    write_state unhealthy
    print_native_health_failure "Timed out waiting for old native LiteLLM listener to stop."
    exit 1
  fi
  clear_transient_routing_state || return 1
  write_state starting
  start_service_process "$owner_pid"

  if wait_for_managed_health "$owner_pid"; then
    clear_state
    echo "LiteLLM started on http://127.0.0.1:$PORT with $NATIVE_WORKERS native workers"
    return 0
  fi

  write_state unhealthy
  print_native_health_failure "Timed out waiting for native LiteLLM."
  exit 1
}

stop_server() {
  local target_pids
  target_pids="$(native_live_pid_candidates || true)"
  if [[ -n "$target_pids" ]]; then
    request_native_process_stop_list "$target_pids" >/dev/null 2>&1 || true
    if ! wait_for_native_processes_stopped 5 100; then
      write_state unhealthy
      print_native_health_failure "Timed out waiting for native LiteLLM processes to stop."
      return 1
    fi
  fi
  clear_native_pid_file_if_stale_or_targeted ""
  clear_state
  echo "LiteLLM stopped"
}

restart_server() {
  local owner_pid
  owner_pid="$(require_menu_app_owner "restart")" || return $?
  ensure_native_environment || return 1
  sync_runtime_config || return 1
  write_state starting
  request_native_processes_to_stop >/dev/null 2>&1 || true
  if ! wait_for_native_port_released 5; then
    write_state unhealthy
    print_native_health_failure "Timed out waiting for old native LiteLLM listener to stop."
    return 1
  fi
  clear_transient_routing_state || return 1
  start_service_process "$owner_pid"

  if wait_for_managed_health "$owner_pid" && wait_for_runtime_config; then
    write_runtime_reload_fingerprint || true
    clear_state
    echo "LiteLLM restarted on http://127.0.0.1:$PORT with $NATIVE_WORKERS native workers; runtime routes verified"
    return 0
  fi

  write_state unhealthy
  print_native_health_failure "Timed out waiting for native LiteLLM."
  return 1
}

reload_server() {
  local pid
  require_menu_app_owner "reload" >/dev/null || return $?
  ensure_python_tools || return 1
  if ! pid="$(native_master_pid)"; then
    echo "No managed native LiteLLM master process found for graceful reload." >&2
    return 1
  fi

  write_state starting
  if ! kill -HUP "$pid" >/dev/null 2>&1; then
    echo "Failed to signal native LiteLLM master process $pid for graceful reload." >&2
    return 1
  fi

  if wait_for_managed_health && wait_for_runtime_config; then
    clear_transient_routing_state || return 1
    write_runtime_reload_fingerprint || true
    clear_state
    echo "LiteLLM gracefully reloaded on http://127.0.0.1:$PORT with runtime routes verified"
    return 0
  fi

  echo "Graceful reload did not expose the runtime routes in time." >&2
  return 1
}

apply_config() {
  ensure_python_tools || exit 1
  sync_runtime_config || exit 1
  if health_ok; then
    require_menu_app_owner "apply-config" >/dev/null || exit $?
    if runtime_reload_fingerprint_changed; then
      echo "Callback package reload inputs changed; trying graceful native service reload..." >&2
      if reload_server; then
        return
      fi
      echo "Graceful reload failed after callback package change; using full native service restart..." >&2
      restart_server
      return
    fi
    if reload_server; then
      return
    fi
    echo "Falling back to full native service restart..." >&2
    restart_server
    return
  fi
  echo "Runtime config synced; LiteLLM is not running."
}

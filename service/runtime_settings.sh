# shellcheck shell=bash
service_action_requires_isolated_target() {
  case "$1" in
    bootstrap|config-editor-bootstrap|\
    start|run-native|stop|reload|restart|apply-config|status|\
    tail|recent-requests|logs-summary|menu-actions-tail|\
    route-trace|\
    computer-facade-smoke|\
    runtime-settings-configure|runtime-settings-apply|runtime-settings-save|\
    configuration-package-export|configuration-package-import|external-provider-import|\
    webdav-settings|webdav-status|webdav-sync-interval-seconds|\
    webdav-configure|webdav-enable|webdav-disable|\
    webdav-probe|webdav-sync|webdav-push|webdav-pull|\
    stage-config|codex-config-status|codex-config-apply|\
    codex-config-editor-load|codex-config-editor-sync|codex-config-editor-apply|\
    provider-billing|remote-usage-logs|\
    validate|verify-runtime-config|\
    autostart-enable|autostart-disable|autostart-status|\
    config-watch-enable|config-watch-ensure|config-watch-disable|config-watch-tail)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

running_from_owned_app_bundle() {
  [[ "$SCRIPT_DIR" == "$APP_BUNDLE_PATH/Contents/Resources/App" ]] || return 1
  [[ -x "$APP_BUNDLE_PATH/Contents/MacOS/LiteLLMMenu" ]] || return 1
  if [[ "$SCRIPT_DIR" == "$DEFAULT_APP_RESOURCE_ROOT" && "$APP_BUNDLE_PATH" == "$DEFAULT_APP_BUNDLE_PATH" ]]; then
    return 0
  fi
  menu_app_running
}

enforce_isolated_runtime_paths() {
  local unsafe variable path mapping shell_variable
  local runtime_paths=(
    LITELLM_MENU_RUNTIME_SETTINGS_FILE:RUNTIME_SETTINGS_FILE
    LITELLM_CONFIG_FILE:CONFIG_FILE
    LITELLM_RUNTIME_DIR:RUNTIME_DIR
    LITELLM_RUNTIME_CONFIG:RUNTIME_CONFIG
    LITELLM_RUNTIME_RELOAD_FINGERPRINT:RUNTIME_RELOAD_FINGERPRINT
    LITELLM_MENU_DEPLOYMENT_COOLDOWN_FILE:DEPLOYMENT_COOLDOWN_FILE
    LITELLM_MENU_ROUTE_RECOVERY_STATE_FILE:ROUTE_RECOVERY_STATE_FILE
    LITELLM_MENU_LOG:LOG_FILE
    LITELLM_MENU_ACTIONS_LOG:MENU_ACTIONS_LOG
    LITELLM_RECENT_REQUESTS_LOG:RECENT_REQUESTS_LOG
    LITELLM_VENV_DIR:VENV_DIR
    LITELLM_NATIVE_PID_FILE:NATIVE_PID_FILE
    LITELLM_NATIVE_OWNER_FILE:NATIVE_OWNER_FILE
    UV_PYTHON_INSTALL_DIR:UV_PYTHON_INSTALL_DIR
    LITELLM_STATE_FILE:STATE_FILE
    LITELLM_SERVICE_LIFECYCLE_LOCK_DIR:SERVICE_LIFECYCLE_LOCK_DIR
    LITELLM_AUTOSTART_STATE_FILE:AUTOSTART_STATE_FILE
    LITELLM_CONFIG_WATCH_LOG:CONFIG_WATCH_LOG
    LITELLM_WEBDAV_SYNC_SETTINGS:WEBDAV_SYNC_SETTINGS
    LITELLM_WEBDAV_SYNC_ENABLED_FILE:WEBDAV_SYNC_ENABLED_FILE
    LITELLM_WEBDAV_SYNC_STATUS_FILE:WEBDAV_SYNC_STATUS_FILE
    LITELLM_WEBDAV_SYNC_STATE:WEBDAV_SYNC_STATE_FILE
  )
  local arguments=("$ROOT" "$PWD")
  for mapping in "${runtime_paths[@]}"; do
    variable="${mapping%%:*}"
    shell_variable="${mapping#*:}"
    path="${!shell_variable}"
    arguments+=("$variable=$path")
  done

  unsafe="$(/usr/bin/python3 - "${arguments[@]}" <<'PY'
import os
import sys

root, cwd, *entries = sys.argv[1:]
if not os.path.isabs(root):
    root = os.path.join(cwd, root)
root = os.path.realpath(root)
unsafe = []
for entry in entries:
    name, path = entry.split("=", 1)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    candidate = os.path.realpath(path)
    try:
        contained = os.path.commonpath((root, candidate)) == root
    except ValueError:
        contained = False
    if not contained:
        unsafe.append(name)
print(" ".join(unsafe))
PY
)" || return 64
  if [[ -n "$unsafe" ]]; then
    echo "Refusing an isolated checkout target with runtime paths outside LITELLM_RUNTIME_ROOT: $unsafe" >&2
    return 64
  fi
}

enforce_isolated_service_target() {
  local action="$1"
  local unsafe=()
  service_action_requires_isolated_target "$action" || return 0
  running_from_owned_app_bundle && return 0

  [[ "$ROOT" == "$USER_DEFAULT_RUNTIME_ROOT" ]] && unsafe+=("LITELLM_RUNTIME_ROOT=$ROOT")
  [[ "$PORT" == "$DEFAULT_PORT" ]] && unsafe+=("LITELLM_PORT=$PORT")
  [[ "$APP_LAUNCH_AGENT_LABEL" == "$DEFAULT_APP_LAUNCH_AGENT_LABEL" ]] && unsafe+=("LITELLM_APP_LAUNCH_AGENT_LABEL=$APP_LAUNCH_AGENT_LABEL")
  [[ "$CONFIG_WATCH_LABEL" == "$DEFAULT_CONFIG_WATCH_LABEL" ]] && unsafe+=("LITELLM_CONFIG_WATCH_LABEL=$CONFIG_WATCH_LABEL")

  if (( ${#unsafe[@]} > 0 )); then
    echo "Refusing to run '$action' from a checkout or app copy with the real app target: ${unsafe[*]}" >&2
    echo "Use an isolated runtime root, port, and launch agent labels for copies/tests." >&2
    return 64
  fi

  enforce_isolated_runtime_paths
}

if [[ -z "${LITELLM_UV_BIN:-}" && ! -x "$BUNDLED_UV" ]]; then
  DETECTED_UV="$(command -v uv 2>/dev/null || true)"
  if [[ -n "$DETECTED_UV" ]]; then
    BUNDLED_UV="$DETECTED_UV"
  fi
fi

quote_sh() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

xml_escape() {
  printf '%s' "$1" | sed \
    -e 's/&/\&amp;/g' \
    -e 's/</\&lt;/g' \
    -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' \
    -e "s/'/\&apos;/g"
}

normalize_bool() {
  local value
  value="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    1|true|yes|y|on|enabled)
      echo "1"
      ;;
    *)
      echo "0"
      ;;
  esac
}

use_system_proxies_value() {
  normalize_bool "${LITELLM_USE_SYSTEM_PROXIES:-0}"
}

log_max_bytes_value() {
  if [[ "${LOCAL_LOG_MAX_BYTES:-}" =~ ^[0-9]+$ && "${LOCAL_LOG_MAX_BYTES:-0}" -gt 0 ]]; then
    printf '%s\n' "$LOCAL_LOG_MAX_BYTES"
    return 0
  fi
  printf '%s\n' 10485760
}

rotate_log_if_needed() {
  local log_path="$1" max_bytes current_bytes backup_bytes log_directory temp_path backup_path
  max_bytes="${2:-$(log_max_bytes_value)}"
  [[ "$max_bytes" =~ ^[0-9]+$ && "$max_bytes" -gt 0 ]] || return 0
  backup_path="$log_path.1"

  if [[ -f "$backup_path" ]]; then
    backup_bytes="$(wc -c < "$backup_path" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$backup_bytes" =~ ^[0-9]+$ ]] && (( backup_bytes > max_bytes )); then
      log_directory="$(dirname "$backup_path")"
      mkdir -p "$log_directory"
      temp_path="$(mktemp "$log_directory/.${backup_path##*/}.rotate.XXXXXX")" || return 0
      if tail -c "$max_bytes" "$backup_path" > "$temp_path" 2>/dev/null; then
        chmod 600 "$temp_path" 2>/dev/null || true
        mv "$temp_path" "$backup_path" 2>/dev/null || rm -f "$temp_path"
      else
        rm -f "$temp_path"
      fi
    fi
  fi

  [[ -f "$log_path" ]] || return 0

  current_bytes="$(wc -c < "$log_path" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ "$current_bytes" =~ ^[0-9]+$ ]] || return 0
  (( current_bytes > max_bytes )) || return 0

  log_directory="$(dirname "$log_path")"
  mkdir -p "$log_directory"
  temp_path="$(mktemp "$log_directory/.${log_path##*/}.rotate.XXXXXX")" || return 0

  if tail -c "$max_bytes" "$log_path" > "$temp_path" 2>/dev/null; then
    chmod 600 "$temp_path" 2>/dev/null || true
    if mv "$temp_path" "$backup_path" 2>/dev/null; then
      cat "$backup_path" > "$log_path" 2>/dev/null || : > "$log_path" 2>/dev/null || true
    else
      rm -f "$temp_path"
    fi
  else
    rm -f "$temp_path"
    : > "$log_path" 2>/dev/null || true
  fi
  chmod 600 "$log_path" 2>/dev/null || true
}

rotate_local_logs_if_needed() {
  rotate_log_if_needed "$RECENT_REQUESTS_LOG"
  rotate_log_if_needed "$LOG_FILE"
  rotate_log_if_needed "$MENU_ACTIONS_LOG"
  rotate_log_if_needed "$CONFIG_WATCH_LOG"
}

apply_system_proxy_guard() {
  if [[ "$(use_system_proxies_value)" == "1" ]]; then
    return 0
  fi

  # Avoid Python/httpx falling back to macOS SystemConfiguration proxy lookup.
  export LITELLM_MENU_DISABLE_SYSTEM_PROXY_LOOKUP=1
  export NO_PROXY="*"
  export no_proxy="*"
  export HTTP_PROXY=""
  export HTTPS_PROXY=""
  export ALL_PROXY=""
  export http_proxy=""
  export https_proxy=""
  export all_proxy=""
}

seed_config_if_missing() {
  [[ -f "$CONFIG_FILE" ]] && return 0
  local source
  source="$TEMPLATE_ROOT/config.example.yaml"
  [[ -f "$source" ]] || return 0
  mkdir -p "$(dirname "$CONFIG_FILE")"
  cp "$source" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE" 2>/dev/null || true
}

ensure_runtime_layout() {
  mkdir -p "$ROOT" "$RUNTIME_DIR"
  seed_config_if_missing
}

find_bootstrap_python() {
  if [[ -n "${PYTHON:-}" && -x "$PYTHON" ]] && python_supports_runtime "$PYTHON"; then
    echo "$PYTHON"
    return 0
  fi
  if [[ -x "$BUNDLED_PYTHON" ]] && python_supports_runtime "$BUNDLED_PYTHON"; then
    echo "$BUNDLED_PYTHON"
    return 0
  fi
  local system_python
  if system_python="$(command -v python3 2>/dev/null)" && python_supports_runtime "$system_python"; then
    echo "$system_python"
    return 0
  fi
  if [[ -x /usr/bin/python3 ]] && python_supports_runtime /usr/bin/python3; then
    echo /usr/bin/python3
    return 0
  fi
  return 1
}

python_supports_runtime() {
  local python="$1"
  [[ -x "$python" ]] || return 1
  "$python" - <<'PY' >/dev/null 2>&1
import sys

raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

helper_python_ready() {
  local python="$1"
  python_supports_runtime "$python" || return 1
  "$python" - <<'PY' >/dev/null 2>&1
import yaml  # noqa: F401
PY
}

create_native_venv() {
  echo "Creating LiteLLM Python runtime in $VENV_DIR" >&2
  if [[ -e "$VENV_DIR" ]]; then
    rm -rf "$VENV_DIR"
  fi
  if [[ -x "$BUNDLED_UV" ]]; then
    UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" \
      "$BUNDLED_UV" venv --python "$PYTHON_VERSION" "$VENV_DIR"
  else
    local bootstrap_python
    if ! bootstrap_python="$(find_bootstrap_python)"; then
      cat >&2 <<EOF
No Python runtime is available.
This app should be built with a bundled uv helper at:
  $TEMPLATE_ROOT/bin/uv
EOF
      return 1
    fi
    "$bootstrap_python" -m venv "$VENV_DIR"
  fi
}

locked_litellm_version() {
  local version
  if [[ ! -f "$LITELLM_VERSION_FILE" ]]; then
    echo "Missing LiteLLM version lock: $LITELLM_VERSION_FILE" >&2
    return 1
  fi
  version="$(tr -d '[:space:]' < "$LITELLM_VERSION_FILE")"
  if [[ ! "$version" =~ ^[0-9][0-9A-Za-z.!+_-]*$ ]]; then
    echo "Invalid LiteLLM version lock in $LITELLM_VERSION_FILE: $version" >&2
    return 1
  fi
  printf '%s\n' "$version"
}

native_deps_ready() {
  local locked_version
  locked_version="$(locked_litellm_version)" || return 1
  [[ -x "$LITELLM_BIN" ]] || return 1
  LITELLM_LOCKED_VERSION="$locked_version" "$NATIVE_PYTHON" - <<'PY' >/dev/null 2>&1
from importlib import metadata
import os

for package in ("gunicorn", "litellm", "Pillow", "PyYAML", "ddgs"):
    metadata.version(package)

if metadata.version("litellm") != os.environ["LITELLM_LOCKED_VERSION"]:
    raise SystemExit(1)
PY
}

ensure_helper_python() {
  local python
  if helper_python_ready "$NATIVE_PYTHON"; then
    PYTHON="$NATIVE_PYTHON"
    return 0
  fi
  if [[ -n "${PYTHON:-}" ]] && helper_python_ready "$PYTHON"; then
    return 0
  fi
  if python="$(find_bootstrap_python)" && helper_python_ready "$python"; then
    PYTHON="$python"
    return 0
  fi
  return 1
}

ensure_native_environment() {
  local locked_litellm_version_value litellm_requirement
  ensure_runtime_layout
  locked_litellm_version_value="$(locked_litellm_version)" || return 1
  litellm_requirement="litellm[proxy]==$locked_litellm_version_value"

  if [[ ! -x "$NATIVE_PYTHON" ]]; then
    create_native_venv
  fi

  if ! native_deps_ready; then
    echo "Installing LiteLLM service dependencies into $VENV_DIR" >&2
    if [[ -x "$BUNDLED_UV" ]]; then
      UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" \
        "$BUNDLED_UV" pip install --python "$NATIVE_PYTHON" \
          "$litellm_requirement" Pillow gunicorn PyYAML ddgs
    else
      "$NATIVE_PYTHON" -m ensurepip --upgrade >/dev/null 2>&1 || true
      "$NATIVE_PYTHON" -m pip install --upgrade pip
      "$NATIVE_PYTHON" -m pip install --upgrade "$litellm_requirement" Pillow gunicorn PyYAML ddgs
    fi
  fi

  PYTHON="$NATIVE_PYTHON"
}

ensure_config_editor_environment() {
  ensure_runtime_layout

  if [[ ! -x "$NATIVE_PYTHON" ]]; then
    create_native_venv
  fi

  if ! helper_python_ready "$NATIVE_PYTHON"; then
    echo "Installing config editor dependencies into $VENV_DIR" >&2
    if [[ -x "$BUNDLED_UV" ]]; then
      UV_PYTHON_INSTALL_DIR="$UV_PYTHON_INSTALL_DIR" \
        "$BUNDLED_UV" pip install --python "$NATIVE_PYTHON" PyYAML
    else
      "$NATIVE_PYTHON" -m ensurepip --upgrade >/dev/null 2>&1 || true
      "$NATIVE_PYTHON" -m pip install --upgrade pip
      "$NATIVE_PYTHON" -m pip install --upgrade PyYAML
    fi
  fi

  PYTHON="$NATIVE_PYTHON"
}

ensure_python_tools() {
  ensure_runtime_layout
  ensure_helper_python && return 0
  ensure_native_environment
}

runtime_settings_python() {
  if [[ -x "$NATIVE_PYTHON" ]]; then
    echo "$NATIVE_PYTHON"
    return 0
  fi
  find_bootstrap_python
}

runtime_settings_json() {
  local python
  if ! python="$(runtime_settings_python)"; then
    echo "No Python runtime is available for runtime settings." >&2
    return 1
  fi
  RUNTIME_SETTINGS_FILE="$RUNTIME_SETTINGS_FILE" "$python" - <<'PY'
from __future__ import annotations

import json
import os
import pathlib
import re

RETAIN_EXISTING_VALUE = "__LITELLM_MENU_RETAIN_EXISTING__"
SECRET_KEYS = {"LITELLM_MENU_VISION_BRIDGE_API_KEY"}

SPECS = [
    {"key": "LITELLM_MENU_REQUEST_TIMEOUT_SECONDS", "category": "Timeouts", "label": "Request timeout", "unit": "seconds", "kind": "float", "default": "7200", "minimum": 0, "maximum": 7200, "help": "Overall timeout for upstream model requests, continuation synthesis, and each recovery probe. 0 disables the local request cap."},
    {"key": "LITELLM_MENU_STREAM_START_TIMEOUT_SECONDS", "category": "Timeouts", "label": "First-event timeout", "unit": "seconds", "kind": "float", "default": "120", "minimum": 0, "maximum": 3600, "help": "Maximum wait for the first upstream stream event on ordinary requests. 0 falls back to Request timeout."},
    {"key": "LITELLM_MENU_CODEX_COMPACTION_START_TIMEOUT_SECONDS", "category": "Timeouts", "label": "Compaction first-event", "unit": "seconds", "kind": "float", "default": "300", "minimum": 0, "maximum": 3600, "help": "Maximum wait for the first event from a structured Codex compaction request. 0 falls back to Request timeout."},
    {"key": "LITELLM_MENU_STALL_TIMEOUT_SECONDS", "category": "Timeouts", "label": "Stream idle timeout", "unit": "seconds", "kind": "float", "default": "120", "minimum": 0, "maximum": 3600, "help": "Maximum gap between stream events after the first event has arrived. 0 disables the local stream-idle cap."},
    {"key": "LITELLM_MENU_RECOVERY_MAX_SECONDS", "category": "Recovery", "label": "Recovery max", "unit": "seconds", "kind": "float", "default": "14400", "minimum": 0, "maximum": 86400, "help": "Maximum route recovery polling time. The same connection stays alive with progress heartbeats while every route is cooling down, then retries after the first cooldown ends. 0 disables polling."},
    {"key": "LITELLM_MENU_RECOVERY_INTERVAL_SECONDS", "category": "Timeouts", "label": "Recovery interval", "unit": "seconds", "kind": "float", "default": "5", "minimum": 0.001, "maximum": 3600, "help": "Delay between real route recovery probes."},
    {"key": "LITELLM_MENU_SAME_DEPLOYMENT_RETRIES", "category": "Recovery", "label": "Same-route retries", "unit": "retries", "kind": "int", "default": "0", "minimum": 0, "maximum": 20, "help": "Additional attempts on the failed deployment before the next peer or order. Default 0 advances immediately; this also overrides LiteLLM Router retry-policy counts."},
    {"key": "LITELLM_MENU_RECOVERY_POLICY_BALANCE", "category": "Recovery", "label": "Balance / quota", "kind": "enum", "default": "recovery_cooldown", "options": ["error", "recovery", "recovery_cooldown"], "help": "How insufficient balance, quota, or billing failures are handled."},
    {"key": "LITELLM_MENU_RECOVERY_POLICY_RATE_LIMIT", "category": "Recovery", "label": "Rate limit / overload", "kind": "enum", "default": "recovery_cooldown", "options": ["error", "recovery", "recovery_cooldown"], "help": "How HTTP 429 and upstream overload or capacity failures are handled."},
    {"key": "LITELLM_MENU_RECOVERY_POLICY_SERVER", "category": "Recovery", "label": "Server / gateway", "kind": "enum", "default": "recovery_cooldown", "options": ["error", "recovery", "recovery_cooldown"], "help": "How temporary server failures, no healthy route, and gateway timeouts are handled."},
    {"key": "LITELLM_MENU_RECOVERY_POLICY_NETWORK", "category": "Recovery", "label": "Network", "kind": "enum", "default": "recovery", "options": ["error", "recovery", "recovery_cooldown"], "help": "How disconnects, DNS failures, and connection errors are handled. Default recovery does not cool down the deployment."},
    {"key": "LITELLM_MENU_RECOVERY_POLICY_STREAM_START_TIMEOUT", "category": "Recovery", "label": "First-event timeout", "kind": "enum", "default": "recovery_cooldown", "options": ["error", "recovery", "recovery_cooldown"], "help": "How a local timeout before the first upstream stream event is handled."},
    {"key": "LITELLM_MENU_RECOVERY_POLICY_STREAM_IDLE_TIMEOUT", "category": "Recovery", "label": "Stream idle timeout", "kind": "enum", "default": "recovery", "options": ["error", "recovery", "recovery_cooldown"], "help": "How a local timeout after streaming has begun is handled. Default recovery does not cool down the deployment."},
    {"key": "LITELLM_MENU_RECOVERY_POLICY_REQUEST_ERROR", "category": "Recovery", "label": "Request / format error", "kind": "enum", "default": "error", "options": ["error", "recovery", "recovery_cooldown"], "help": "How deterministic request, format, model, policy, and context errors are handled. Default error returns a terminal failure so the proxy can be patched instead of waiting."},
    {"key": "LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS", "category": "Timeouts", "label": "Web fetch timeout", "unit": "seconds", "kind": "float", "default": "30", "minimum": 3, "maximum": 60, "help": "Timeout for DDGS search and Jina page fetches. This does not cap model generation."},
    {"key": "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "category": "Web Search", "label": "Search results", "unit": "results", "kind": "int", "default": "8", "minimum": 1, "maximum": 20, "help": "Maximum deduplicated DDGS results collected per search action across configured backends."},
    {"key": "LITELLM_MENU_WEB_SEARCH_READ_RESULTS", "category": "Web Search", "label": "Readable pages", "unit": "pages", "kind": "int", "default": "4", "minimum": 0, "maximum": 20, "help": "Number of top search results expanded through Jina Reader for stronger snippets. 0 disables page expansion."},
    {"key": "LITELLM_MENU_WEB_SEARCH_READ_CHARS", "category": "Web Search", "label": "Readable page chars", "unit": "chars", "kind": "int", "default": "1400", "minimum": 200, "maximum": 5000, "help": "Maximum Jina Reader excerpt characters kept for each expanded result."},
    {"key": "LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND", "category": "Web Search", "label": "DDGS backend", "kind": "string", "default": "auto", "help": "DDGS backend list. Use auto for DDGS aggregation, or comma/space-separated backends such as brave,bing,duckduckgo to aggregate and deduplicate manually."},
    {"key": "LITELLM_MENU_WEB_SEARCH_REGION", "category": "Web Search", "label": "Search region", "kind": "string", "default": "us-en", "help": "DDGS search region such as us-en, cn-zh, or wt-wt, passed directly to the DDGS SDK."},
    {"key": "LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS", "category": "Web Search", "label": "Action rounds", "unit": "rounds", "kind": "int", "default": "6", "minimum": 1, "maximum": 8, "help": "Maximum model-directed web-search action rounds for one response."},
    {"key": "LITELLM_MENU_WEB_SEARCH_MAX_QUERIES", "category": "Web Search", "label": "Total queries", "unit": "queries", "kind": "int", "default": "16", "minimum": 1, "maximum": 64, "help": "Maximum unique search queries across all web-search rounds."},
    {"key": "LITELLM_MENU_WEB_SEARCH_MAX_OPEN_PAGES", "category": "Web Search", "label": "Open pages", "unit": "pages", "kind": "int", "default": "8", "minimum": 0, "maximum": 32, "help": "Maximum explicit page-open actions across all web-search rounds. 0 disables page opens."},
    {"key": "LITELLM_MENU_WEB_SEARCH_MAX_FIND_IN_PAGE", "category": "Web Search", "label": "Find in page", "unit": "actions", "kind": "int", "default": "12", "minimum": 0, "maximum": 64, "help": "Maximum find-in-page actions across all web-search rounds. 0 disables in-page find."},
    {"key": "LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES", "category": "Web Search", "label": "Model retries", "unit": "retries", "kind": "int", "default": "2", "minimum": 0, "maximum": 5, "help": "Retries for temporary rate-limit failures while the model plans or synthesizes bridged web search."},
    {"key": "LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS", "category": "Web Search", "label": "Model retry delay", "unit": "seconds", "kind": "float", "default": "1", "minimum": 0, "maximum": 30, "help": "Base delay between temporary web-search model retries."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_BACKEND", "category": "Vision Bridge", "label": "Backend", "kind": "enum", "default": "auto", "options": ["auto", "local", "api", "off"], "help": "Auto tries the configured OpenAI-compatible endpoint first, then falls back to bundled local Vision OCR. Local skips any external vision endpoint. API requires a reachable OpenAI-compatible vision service. Off disables image-to-text fallback."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_API_BASE", "category": "Vision Bridge", "label": "API base", "kind": "string", "default": "http://127.0.0.1:11434/v1", "help": "OpenAI-compatible local vision endpoint, such as Ollama /v1 or another local APIURL bridge."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_MODEL", "category": "Vision Bridge", "label": "Model", "kind": "string", "default": "qwen2.5vl:3b", "help": "Vision model used only to convert images into text before retrying the original route."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_API_KEY", "category": "Vision Bridge", "label": "API key", "kind": "string", "default": "", "secret": True, "retain_existing": "__LITELLM_MENU_RETAIN_EXISTING__", "help": "Optional bearer token for the vision bridge endpoint. Leave unchanged to retain the saved token, or clear it to remove the token."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_TIMEOUT_SECONDS", "category": "Vision Bridge", "label": "Timeout", "unit": "seconds", "kind": "float", "default": "45", "minimum": 1, "maximum": 600, "help": "Timeout for each local image-to-text bridge call."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_LOCAL_FORMAT", "category": "Vision Bridge", "label": "Local format", "kind": "enum", "default": "compact", "options": ["compact", "detailed"], "help": "Compact keeps local fallback summaries shorter to save tokens. Detailed includes the fuller region and element breakdown."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_PROMPT", "category": "Vision Bridge", "label": "Prompt", "kind": "string", "default": "Describe the image accurately for a text-only language model. Include visible text, UI elements, layout, objects, and any important details.", "help": "Instruction sent to the local vision model when converting an image into text."},
    {"key": "LITELLM_MENU_DEPLOYMENT_COOLDOWN_FAILURES", "category": "Fallback", "label": "Cooldown failures", "unit": "failures", "kind": "int", "default": "2", "minimum": 0, "maximum": 20, "help": "Consecutive upstream failures before that deployment/protocol pair is temporarily skipped. Other configured protocols on the same deployment remain eligible. 0 disables cooldown."},
    {"key": "LITELLM_MENU_DEPLOYMENT_COOLDOWN_SECONDS", "category": "Fallback", "label": "Cooldown duration", "unit": "seconds", "kind": "float", "default": "300", "minimum": 0, "maximum": 86400, "help": "How long a failed deployment/protocol pair is skipped after reaching the threshold. The deployment is excluded only while all configured protocols are cooling down. 0 disables cooldown."},
    {"key": "LITELLM_MENU_IMAGE_TOOL_FALLBACK_MAX_ATTEMPTS", "category": "Fallback", "label": "Image tool attempts", "unit": "attempts", "kind": "int", "default": "3", "minimum": 0, "maximum": 20, "help": "Maximum same-request image-generation tool recovery attempts before returning a safe failure. 0 disables this recovery."},
    {"key": "LITELLM_MENU_BALANCE_REFRESH_MINUTES", "category": "Billing", "label": "Balance refresh", "unit": "minutes", "kind": "int", "default": "5", "minimum": 0, "maximum": 1440, "help": "Refresh model balances in the background while Providers & Models is open. 0 disables timed refresh; Model Probe still refreshes once."},
    {"key": "LITELLM_BROWSER_BILLING", "category": "Billing", "label": "Browser billing fallback", "kind": "bool", "default": "0", "help": "After direct billing endpoints fail, allow a request in an already-open same-origin Chrome page. This does not read Chrome profile or cookie files, open or switch tabs, or start Chrome."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_BACKEND", "category": "Computer Facade", "label": "Backend", "kind": "enum", "default": "auto", "options": ["auto", "mcp", "browser", "chrome", "playwright", "cua", "mock"], "help": "Executor backend. Explicit choices do not silently fall back to another real backend."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_MODEL", "category": "Computer Facade", "label": "Planner model", "kind": "string", "default": "", "help": "Optional model group or route for the internal JSON planner. Empty uses the request model."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_MAX_STEPS", "category": "Computer Facade", "label": "Max steps", "unit": "steps", "kind": "int", "default": "20", "minimum": 1, "maximum": 200, "help": "Maximum computer observation/action turns before safe failure."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_TRACE", "category": "Computer Facade", "label": "Trace", "kind": "bool", "default": "0", "help": "Log action summaries and backend choices to route trace."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_TRACE_SCREENSHOTS", "category": "Computer Facade", "label": "Trace screenshots", "kind": "bool", "default": "0", "help": "Privacy-sensitive: when enabled, screenshots are written locally with 0600 permissions instead of being logged inline."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_ACTION_DENYLIST", "category": "Computer Facade", "label": "Action denylist", "kind": "string", "default": "", "help": "Comma-separated actions to block, for example click,type,drag."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_REQUIRE_OBSERVATION", "category": "Computer Facade", "label": "Require observation", "kind": "bool", "default": "1", "help": "Require executor observation before planner completion or action success."},
    {"key": "LITELLM_MENU_LOG_MAX_BYTES", "category": "Logs", "label": "Local log file cap", "unit": "MB", "kind": "mb", "default": "10", "minimum": 0.25, "maximum": 100, "help": "Per-file cap for local logs: recent requests, service stdout/stderr, menu actions, and config watch. Each log keeps one .1 backup containing the previous tail."},
    {"key": "LITELLM_MENU_ROUTE_TRACE_PREVIEW_CHARS", "category": "Logs", "label": "Trace preview chars", "unit": "chars", "kind": "int", "default": "2000", "minimum": 80, "maximum": 2000, "help": "Maximum request-preview characters retained in the always-on local route trace."},
    {"key": "LITELLM_USE_SYSTEM_PROXIES", "category": "Network", "label": "Use system proxies", "kind": "bool", "default": "0", "help": "Allow upstream HTTP clients to use macOS system proxy settings. Off isolates LiteLLM from system proxy auto-discovery."},
    {"key": "LITELLM_PORT", "category": "Service", "label": "Local port", "kind": "int", "default": "4000", "minimum": 1, "maximum": 65535, "help": "Local HTTP port for the LiteLLM proxy. Changing this updates health checks and requires a service restart."},
    {"key": "LITELLM_NUM_WORKERS", "category": "Service", "label": "Worker count", "unit": "workers", "kind": "int", "default": "16", "minimum": 1, "maximum": 64, "help": "Gunicorn workers for the local LiteLLM proxy."},
    {"key": "LITELLM_MAX_REQUESTS_BEFORE_RESTART", "category": "Service", "label": "Worker request recycle", "unit": "requests", "kind": "int", "default": "1000", "minimum": 1, "maximum": 100000, "help": "Restart each Gunicorn worker after this many handled requests to cap long-running memory growth."},
    {"key": "LITELLM_STATE_TTL_SECONDS", "category": "Service", "label": "State TTL", "unit": "seconds", "kind": "int", "default": "180", "minimum": 1, "maximum": 3600, "help": "How long transient start/stop state is considered fresh."},
    {"key": "LITELLM_HEALTH_WAIT_SECONDS", "category": "Service", "label": "Health wait", "unit": "seconds", "kind": "int", "default": "60", "minimum": 1, "maximum": 600, "help": "How long start/restart waits for the health endpoint."},
    {"key": "LITELLM_RUNTIME_VERIFY_WAIT_SECONDS", "category": "Service", "label": "Runtime verify wait", "unit": "seconds", "kind": "int", "default": "30", "minimum": 1, "maximum": 600, "help": "How long runtime config verification may wait."},
    {"key": "LITELLM_SERVICE_LIFECYCLE_LOCK_WAIT_SECONDS", "category": "Service", "label": "Lifecycle lock wait", "unit": "seconds", "kind": "int", "default": "120", "minimum": 1, "maximum": 1800, "help": "Maximum wait for concurrent start/restart/apply-config operations."},
    {"key": "LITELLM_CONFIG_WATCH_INTERVAL", "category": "Config Watch", "label": "Poll interval", "unit": "seconds", "kind": "float", "default": "5", "minimum": 0.2, "maximum": 300, "help": "How often config.yaml is checked for changes."},
    {"key": "LITELLM_CONFIG_WATCH_SETTLE_INTERVAL", "category": "Config Watch", "label": "Settle interval", "unit": "seconds", "kind": "float", "default": "2", "minimum": 0, "maximum": 300, "help": "How long the watcher waits for file writes to settle."},
]


def read_configured(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    allowed = {spec["key"] for spec in SPECS}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "#" in line:
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in allowed and value and value != RETAIN_EXISTING_VALUE:
            values[key] = value
    return values


def bytes_to_mb_text(value: str) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = 10 * 1024 * 1024
    mb = size / (1024 * 1024)
    return f"{mb:.6f}".rstrip("0").rstrip(".")


def truthy_text(value: str) -> str:
    return "1" if str(value).strip().lower() in {"1", "true", "yes", "on"} else "0"


def normalize_value(spec: dict[str, object], raw: object) -> str:
    kind = str(spec.get("kind", "string"))
    default = str(spec.get("default", ""))
    text = str(raw if raw is not None else default).strip()
    if not text:
        text = default
    if kind == "bool":
        if text.lower() in {"1", "true", "yes", "on"}:
            return "1"
        if text.lower() in {"0", "false", "no", "off"}:
            return "0"
        raise ValueError(f"{spec['key']} must be a boolean.")
    if kind == "bool_auto":
        if text.lower() in {"1", "true", "yes", "on", "auto", "enabled"}:
            return "auto"
        if text.lower() in {"0", "false", "no", "off", "disabled"}:
            return "off"
        raise ValueError(f"{spec['key']} must be a boolean.")
    if kind == "enum":
        options = [str(option) for option in spec.get("options", [])]
        lowered = text.lower()
        if lowered not in options:
            raise ValueError(f"{spec['key']} must be one of: {', '.join(options)}")
        return lowered
    if kind == "string":
        return text
    if kind == "int":
        if not re.fullmatch(r"\d+", text):
            raise ValueError(f"{spec['key']} must be an integer.")
        numeric = int(text)
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if minimum is not None and numeric < int(minimum):
            raise ValueError(f"{spec['key']} must be at least {minimum}.")
        if maximum is not None and numeric > int(maximum):
            raise ValueError(f"{spec['key']} must be at most {maximum}.")
        return str(numeric)
    if kind == "float":
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            raise ValueError(f"{spec['key']} must be a number.")
        numeric = float(text)
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if minimum is not None and numeric < float(minimum):
            raise ValueError(f"{spec['key']} must be at least {minimum}.")
        if maximum is not None and numeric > float(maximum):
            raise ValueError(f"{spec['key']} must be at most {maximum}.")
        return f"{numeric:.6f}".rstrip("0").rstrip(".")
    if kind == "mb":
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            raise ValueError(f"{spec['key']} must be a number of MB.")
        numeric = float(text)
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if minimum is not None and numeric < float(minimum):
            raise ValueError(f"{spec['key']} must be at least {minimum}.")
        if maximum is not None and numeric > float(maximum):
            raise ValueError(f"{spec['key']} must be at most {maximum}.")
        return str(int(round(numeric * 1024 * 1024)))
    raise ValueError(f"Unsupported runtime setting kind: {kind}")


path = pathlib.Path(os.environ["RUNTIME_SETTINGS_FILE"])
configured = read_configured(path)
settings = []
for spec in SPECS:
    item = dict(spec)
    env_key = spec["key"]
    if env_key in SECRET_KEYS:
        effective_secret = str(os.environ.get(env_key, "")).strip()
        value = RETAIN_EXISTING_VALUE if effective_secret else ""
    elif spec["kind"] == "mb":
        value = os.environ.get(env_key, str(10 * 1024 * 1024))
        value = bytes_to_mb_text(value)
    elif spec["kind"] == "bool":
        value = truthy_text(os.environ.get(env_key, spec["default"]))
    elif spec["kind"] == "bool_auto":
        value = normalize_value(spec, os.environ.get(env_key, spec["default"]))
    else:
        value = os.environ.get(env_key, spec["default"])
        if spec["kind"] == "enum":
            value = str(value).strip().lower()
    item["value"] = value
    item["configured"] = env_key in configured
    settings.append(item)
print(json.dumps({"path": str(path), "settings": settings}, ensure_ascii=False, indent=2))
PY
}

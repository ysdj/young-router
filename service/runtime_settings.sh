# shellcheck shell=bash
service_action_requires_isolated_target() {
  case "$1" in
    bootstrap|config-editor-bootstrap|\
    start|run-native|stop|reload|restart|hard-restart|apply-config|\
    autostart-enable|autostart-disable|autostart-status|\
    config-watch-enable|config-watch-ensure|config-watch-disable)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

running_from_default_installed_app() {
  [[ "$SCRIPT_DIR" == "$DEFAULT_APP_RESOURCE_ROOT" && "$APP_BUNDLE_PATH" == "$DEFAULT_APP_BUNDLE_PATH" ]]
}

enforce_isolated_service_target() {
  local action="$1"
  local unsafe=()
  service_action_requires_isolated_target "$action" || return 0
  running_from_default_installed_app && return 0

  [[ "$ROOT" == "$USER_DEFAULT_RUNTIME_ROOT" ]] && unsafe+=("LITELLM_RUNTIME_ROOT=$ROOT")
  [[ "$PORT" == "$DEFAULT_PORT" ]] && unsafe+=("LITELLM_PORT=$PORT")
  [[ "$LAUNCH_AGENT_LABEL" == "$DEFAULT_LAUNCH_AGENT_LABEL" ]] && unsafe+=("LITELLM_LAUNCH_AGENT_LABEL=$LAUNCH_AGENT_LABEL")
  [[ "$APP_LAUNCH_AGENT_LABEL" == "$DEFAULT_APP_LAUNCH_AGENT_LABEL" ]] && unsafe+=("LITELLM_APP_LAUNCH_AGENT_LABEL=$APP_LAUNCH_AGENT_LABEL")
  [[ "$CONFIG_WATCH_LABEL" == "$DEFAULT_CONFIG_WATCH_LABEL" ]] && unsafe+=("LITELLM_CONFIG_WATCH_LABEL=$CONFIG_WATCH_LABEL")

  if (( ${#unsafe[@]} > 0 )); then
    echo "Refusing to run '$action' from a checkout or app copy with the real app target: ${unsafe[*]}" >&2
    echo "Use an isolated runtime root, port, and launch agent labels for copies/tests." >&2
    return 64
  fi
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

route_trace_state_value() {
  local value=""
  if [[ -f "$ROUTE_TRACE_STATE_FILE" ]]; then
    value="$(tr -d '[:space:]' < "$ROUTE_TRACE_STATE_FILE" 2>/dev/null || true)"
  fi
  normalize_bool "$value"
}

route_trace_effective_value() {
  if [[ -n "${LITELLM_MENU_ROUTE_TRACE:-}" ]]; then
    normalize_bool "$LITELLM_MENU_ROUTE_TRACE"
    return
  fi
  route_trace_state_value
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
  if [[ -n "${PYTHON:-}" && -x "$PYTHON" ]]; then
    echo "$PYTHON"
    return 0
  fi
  if [[ -x "$BUNDLED_PYTHON" ]]; then
    echo "$BUNDLED_PYTHON"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if [[ -x /usr/bin/python3 ]]; then
    echo /usr/bin/python3
    return 0
  fi
  return 1
}

helper_python_ready() {
  local python="$1"
  [[ -x "$python" ]] || return 1
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

SPECS = [
    {"key": "LITELLM_MENU_REQUEST_TIMEOUT_SECONDS", "category": "Timeouts", "label": "Request timeout", "unit": "seconds", "kind": "float", "default": "7200", "minimum": 0, "maximum": 7200, "help": "Overall timeout for upstream model requests, continuation synthesis, and each recovery probe. 0 disables the local request cap."},
    {"key": "LITELLM_MENU_STALL_TIMEOUT_SECONDS", "category": "Timeouts", "label": "Stall timeout", "unit": "seconds", "kind": "float", "default": "120", "minimum": 0, "maximum": 3600, "help": "No stream event within this window is treated as a stalled stream. The same guard covers the first and later chunks."},
    {"key": "LITELLM_MENU_RECOVERY_MAX_SECONDS", "category": "Timeouts", "label": "Recovery max", "unit": "seconds", "kind": "float", "default": "43200", "minimum": 0, "maximum": 86400, "help": "Maximum time to keep route recovery polling alive after a recoverable upstream failure. 0 disables recovery polling."},
    {"key": "LITELLM_MENU_RECOVERY_INTERVAL_SECONDS", "category": "Timeouts", "label": "Recovery interval", "unit": "seconds", "kind": "float", "default": "5", "minimum": 0.001, "maximum": 3600, "help": "Delay between real route recovery probes."},
    {"key": "LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS", "category": "Timeouts", "label": "Web fetch timeout", "unit": "seconds", "kind": "float", "default": "30", "minimum": 3, "maximum": 60, "help": "Timeout for DDGS search and Jina page fetches. This does not cap model generation."},
    {"key": "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "category": "Web Search", "label": "Search results", "unit": "results", "kind": "int", "default": "8", "minimum": 1, "maximum": 20, "help": "Maximum deduplicated DDGS results collected per search action across configured backends."},
    {"key": "LITELLM_MENU_WEB_SEARCH_READ_RESULTS", "category": "Web Search", "label": "Readable pages", "unit": "pages", "kind": "int", "default": "4", "minimum": 0, "maximum": 20, "help": "Number of top search results expanded through Jina Reader for stronger snippets. 0 disables page expansion."},
    {"key": "LITELLM_MENU_WEB_SEARCH_READ_CHARS", "category": "Web Search", "label": "Readable page chars", "unit": "chars", "kind": "int", "default": "1400", "minimum": 200, "maximum": 5000, "help": "Maximum Jina Reader excerpt characters kept for each expanded result."},
    {"key": "LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND", "category": "Web Search", "label": "DDGS backend", "kind": "string", "default": "auto", "help": "DDGS backend list. Use auto for DDGS aggregation, or comma/space-separated backends such as brave,bing,duckduckgo to aggregate and deduplicate manually."},
    {"key": "LITELLM_MENU_WEB_SEARCH_REGION", "category": "Web Search", "label": "Search region", "kind": "string", "default": "us-en", "help": "DDGS search region such as us-en, cn-zh, or wt-wt, passed directly to the DDGS SDK."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_BACKEND", "category": "Vision Bridge", "label": "Backend", "kind": "enum", "default": "auto", "options": ["auto", "local", "api", "off"], "help": "Auto tries the configured OpenAI-compatible endpoint first, then falls back to bundled local Vision OCR. Local skips any external vision endpoint. API requires a reachable OpenAI-compatible vision service. Off disables image-to-text fallback."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_API_BASE", "category": "Vision Bridge", "label": "API base", "kind": "string", "default": "http://127.0.0.1:11434/v1", "help": "OpenAI-compatible local vision endpoint, such as Ollama /v1 or another local APIURL bridge."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_MODEL", "category": "Vision Bridge", "label": "Model", "kind": "string", "default": "qwen2.5vl:3b", "help": "Vision model used only to convert images into text before retrying the original route."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_API_KEY", "category": "Vision Bridge", "label": "API key", "kind": "string", "default": "", "help": "Optional bearer token for the vision bridge endpoint. Leave empty for local Ollama."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_TIMEOUT_SECONDS", "category": "Vision Bridge", "label": "Timeout", "unit": "seconds", "kind": "float", "default": "45", "minimum": 1, "maximum": 600, "help": "Timeout for each local image-to-text bridge call."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_LOCAL_FORMAT", "category": "Vision Bridge", "label": "Local format", "kind": "enum", "default": "compact", "options": ["compact", "detailed"], "help": "Compact keeps local fallback summaries shorter to save tokens. Detailed includes the fuller region and element breakdown."},
    {"key": "LITELLM_MENU_VISION_BRIDGE_PROMPT", "category": "Vision Bridge", "label": "Prompt", "kind": "string", "default": "Describe the image accurately for a text-only language model. Include visible text, UI elements, layout, objects, and any important details.", "help": "Instruction sent to the local vision model when converting an image into text."},
    {"key": "LITELLM_MENU_DEPLOYMENT_COOLDOWN_FAILURES", "category": "Fallback", "label": "Cooldown failures", "unit": "failures", "kind": "int", "default": "2", "minimum": 0, "maximum": 20, "help": "Consecutive upstream failures before that deployment/protocol pair is temporarily skipped. Other configured protocols on the same deployment remain eligible. 0 disables cooldown."},
    {"key": "LITELLM_MENU_DEPLOYMENT_COOLDOWN_SECONDS", "category": "Fallback", "label": "Cooldown duration", "unit": "seconds", "kind": "float", "default": "300", "minimum": 0, "maximum": 86400, "help": "How long a failed deployment/protocol pair is skipped after reaching the threshold. The deployment is excluded only while all configured protocols are cooling down. 0 disables cooldown."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_BACKEND", "category": "Computer Facade", "label": "Backend", "kind": "enum", "default": "auto", "options": ["auto", "mcp", "browser", "chrome", "playwright", "cua", "mock"], "help": "Executor backend. Explicit choices do not silently fall back to another real backend."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_MODEL", "category": "Computer Facade", "label": "Planner model", "kind": "string", "default": "", "help": "Optional model group or route for the internal JSON planner. Empty uses the request model."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_MAX_STEPS", "category": "Computer Facade", "label": "Max steps", "unit": "steps", "kind": "int", "default": "20", "minimum": 1, "maximum": 200, "help": "Maximum computer observation/action turns before safe failure."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_TRACE", "category": "Computer Facade", "label": "Trace", "kind": "bool", "default": "0", "help": "Log action summaries and backend choices to route trace."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_TRACE_SCREENSHOTS", "category": "Computer Facade", "label": "Trace screenshots", "kind": "bool", "default": "0", "help": "Privacy-sensitive: when enabled, screenshots are written locally with 0600 permissions instead of being logged inline."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_ACTION_DENYLIST", "category": "Computer Facade", "label": "Action denylist", "kind": "string", "default": "", "help": "Comma-separated actions to block, for example click,type,drag."},
    {"key": "LITELLM_MENU_COMPUTER_FACADE_REQUIRE_OBSERVATION", "category": "Computer Facade", "label": "Require observation", "kind": "bool", "default": "1", "help": "Require executor observation before planner completion or action success."},
    {"key": "LITELLM_MENU_LOG_MAX_BYTES", "category": "Logs", "label": "Local log file cap", "unit": "MB", "kind": "mb", "default": "10", "minimum": 0.25, "maximum": 100, "help": "Per-file cap for local logs: recent requests, service stdout/stderr, menu actions, and config watch. Each log keeps one .1 backup containing the previous tail."},
    {"key": "LITELLM_PORT", "category": "Service", "label": "Local port", "kind": "int", "default": "4000", "minimum": 1, "maximum": 65535, "help": "Local HTTP port for the LiteLLM proxy. Changing this updates health checks and requires a service restart."},
    {"key": "LITELLM_NUM_WORKERS", "category": "Service", "label": "Worker count", "unit": "workers", "kind": "int", "default": "16", "minimum": 1, "maximum": 64, "help": "Gunicorn workers for the local LiteLLM proxy."},
    {"key": "LITELLM_MAX_REQUESTS_BEFORE_RESTART", "category": "Service", "label": "Worker request recycle", "unit": "requests", "kind": "int", "default": "1000", "minimum": 1, "maximum": 100000, "help": "Restart each Gunicorn worker after this many handled requests to cap long-running memory growth."},
    {"key": "LITELLM_STATE_TTL_SECONDS", "category": "Service", "label": "State TTL", "unit": "seconds", "kind": "int", "default": "180", "minimum": 1, "maximum": 3600, "help": "How long transient start/stop state is considered fresh."},
    {"key": "LITELLM_HEALTH_WAIT_SECONDS", "category": "Service", "label": "Health wait", "unit": "seconds", "kind": "int", "default": "60", "minimum": 1, "maximum": 600, "help": "How long start/restart waits for the health endpoint."},
    {"key": "LITELLM_RUNTIME_VERIFY_WAIT_SECONDS", "category": "Service", "label": "Runtime verify wait", "unit": "seconds", "kind": "int", "default": "30", "minimum": 1, "maximum": 600, "help": "How long runtime config verification may wait."},
    {"key": "LITELLM_SERVICE_LIFECYCLE_LOCK_WAIT_SECONDS", "category": "Service", "label": "Lifecycle lock wait", "unit": "seconds", "kind": "int", "default": "120", "minimum": 1, "maximum": 1800, "help": "Maximum wait for concurrent start/restart/apply-config operations."},
    {"key": "LITELLM_SERVICE_THROTTLE_INTERVAL_SECONDS", "category": "Service", "label": "Launchd throttle interval", "unit": "seconds", "kind": "int", "default": "1", "minimum": 1, "maximum": 300, "help": "Launchd restart throttle interval for the proxy service."},
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
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in allowed and value:
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
    if spec["kind"] == "mb":
        value = os.environ.get(env_key, str(10 * 1024 * 1024))
        value = bytes_to_mb_text(value)
    elif spec["kind"] == "bool":
        value = truthy_text(os.environ.get(env_key, spec["default"]))
    elif spec["kind"] == "bool_auto":
        value = normalize_value(spec, os.environ.get(env_key, spec["default"]))
    else:
        if env_key == "LITELLM_MENU_VISION_BRIDGE_BACKEND":
            value = os.environ.get(env_key) or os.environ.get("LITELLM_MENU_VISION_BRIDGE_MODE") or spec["default"]
            if str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}:
                value = "auto"
            elif str(value).strip().lower() in {"0", "false", "no", "off", "disabled"}:
                value = "off"
        else:
            value = os.environ.get(env_key, spec["default"])
        if spec["kind"] == "enum":
            value = str(value).strip().lower()
    item["value"] = value
    if env_key == "LITELLM_MENU_VISION_BRIDGE_BACKEND":
        item["configured"] = env_key in configured or "LITELLM_MENU_VISION_BRIDGE_MODE" in configured
    else:
        item["configured"] = env_key in configured
    settings.append(item)
print(json.dumps({"path": str(path), "settings": settings}, ensure_ascii=False, indent=2))
PY
}

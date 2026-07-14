# shellcheck shell=bash
runtime_settings_configure() {
  local python payload
  if ! python="$(runtime_settings_python)"; then
    echo "No Python runtime is available for runtime settings." >&2
    return 1
  fi
  payload="$(cat)"
  RUNTIME_SETTINGS_FILE="$RUNTIME_SETTINGS_FILE" RUNTIME_SETTINGS_PAYLOAD="$payload" "$python" - <<'PY'
from __future__ import annotations

import json
import math
import os
import pathlib
import re
import sys
import tempfile

SPECS = [
    ("LITELLM_MENU_REQUEST_TIMEOUT_SECONDS", "float", "7200", 0, 7200),
    ("LITELLM_MENU_STREAM_START_TIMEOUT_SECONDS", "float", "120", 0, 3600),
    ("LITELLM_MENU_CODEX_COMPACTION_START_TIMEOUT_SECONDS", "float", "300", 0, 3600),
    ("LITELLM_MENU_STALL_TIMEOUT_SECONDS", "float", "120", 0, 3600),
    ("LITELLM_MENU_RECOVERY_MAX_SECONDS", "float", "43200", 0, 86400),
    ("LITELLM_MENU_RECOVERY_INTERVAL_SECONDS", "float", "5", 0.001, 3600),
    ("LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS", "float", "30", 3, 60),
    ("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS", "int", "8", 1, 20),
    ("LITELLM_MENU_WEB_SEARCH_READ_RESULTS", "int", "4", 0, 20),
    ("LITELLM_MENU_WEB_SEARCH_READ_CHARS", "int", "1400", 200, 5000),
    ("LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND", "string", "auto", None, None),
    ("LITELLM_MENU_WEB_SEARCH_REGION", "string", "us-en", None, None),
    ("LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS", "int", "6", 1, 8),
    ("LITELLM_MENU_WEB_SEARCH_MAX_QUERIES", "int", "16", 1, 64),
    ("LITELLM_MENU_WEB_SEARCH_MAX_OPEN_PAGES", "int", "8", 0, 32),
    ("LITELLM_MENU_WEB_SEARCH_MAX_FIND_IN_PAGE", "int", "12", 0, 64),
    ("LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES", "int", "2", 0, 5),
    ("LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS", "float", "1", 0, 30),
    ("LITELLM_MENU_VISION_BRIDGE_BACKEND", "enum", "auto", None, None),
    ("LITELLM_MENU_VISION_BRIDGE_API_BASE", "string", "http://127.0.0.1:11434/v1", None, None),
    ("LITELLM_MENU_VISION_BRIDGE_MODEL", "string", "qwen2.5vl:3b", None, None),
    ("LITELLM_MENU_VISION_BRIDGE_API_KEY", "string", "", None, None),
    ("LITELLM_MENU_VISION_BRIDGE_TIMEOUT_SECONDS", "float", "45", 1, 600),
    ("LITELLM_MENU_VISION_BRIDGE_LOCAL_FORMAT", "enum", "compact", None, None),
    ("LITELLM_MENU_VISION_BRIDGE_PROMPT", "string", "Describe the image accurately for a text-only language model. Include visible text, UI elements, layout, objects, and any important details.", None, None),
    ("LITELLM_MENU_DEPLOYMENT_COOLDOWN_FAILURES", "int", "2", 0, 20),
    ("LITELLM_MENU_DEPLOYMENT_COOLDOWN_SECONDS", "float", "300", 0, 86400),
    ("LITELLM_MENU_IMAGE_TOOL_FALLBACK_MAX_ATTEMPTS", "int", "3", 0, 20),
    ("LITELLM_MENU_COMPUTER_FACADE_BACKEND", "enum", "auto", None, None),
    ("LITELLM_MENU_COMPUTER_FACADE_MODEL", "string", "", None, None),
    ("LITELLM_MENU_COMPUTER_FACADE_MAX_STEPS", "int", "20", 1, 200),
    ("LITELLM_MENU_COMPUTER_FACADE_TRACE", "bool", "0", None, None),
    ("LITELLM_MENU_COMPUTER_FACADE_TRACE_SCREENSHOTS", "bool", "0", None, None),
    ("LITELLM_MENU_COMPUTER_FACADE_ACTION_DENYLIST", "string", "", None, None),
    ("LITELLM_MENU_COMPUTER_FACADE_REQUIRE_OBSERVATION", "bool", "1", None, None),
    ("LITELLM_MENU_LOG_MAX_BYTES", "mb", "10", 0.25, 100),
    ("LITELLM_MENU_ROUTE_TRACE_PREVIEW_CHARS", "int", "2000", 80, 2000),
    ("LITELLM_USE_SYSTEM_PROXIES", "bool", "0", None, None),
    ("LITELLM_PORT", "int", "4000", 1, 65535),
    ("LITELLM_NUM_WORKERS", "int", "16", 1, 64),
    ("LITELLM_MAX_REQUESTS_BEFORE_RESTART", "int", "1000", 1, 100000),
    ("LITELLM_STATE_TTL_SECONDS", "int", "180", 1, 3600),
    ("LITELLM_HEALTH_WAIT_SECONDS", "int", "60", 1, 600),
    ("LITELLM_RUNTIME_VERIFY_WAIT_SECONDS", "int", "30", 1, 600),
    ("LITELLM_SERVICE_LIFECYCLE_LOCK_WAIT_SECONDS", "int", "120", 1, 1800),
    ("LITELLM_SERVICE_THROTTLE_INTERVAL_SECONDS", "int", "1", 1, 300),
    ("LITELLM_CONFIG_WATCH_INTERVAL", "float", "5", 0.2, 300),
    ("LITELLM_CONFIG_WATCH_SETTLE_INTERVAL", "float", "2", 0, 300),
]
SPEC_BY_KEY = {key: (key, kind, default, minimum, maximum) for key, kind, default, minimum, maximum in SPECS}


def parse_payload() -> dict[str, object]:
    try:
        payload = json.loads(os.environ.get("RUNTIME_SETTINGS_PAYLOAD", ""))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Settings payload must be a JSON object.")
    values = payload.get("values", payload)
    if not isinstance(values, dict):
        raise ValueError("Settings payload must contain an object at values.")
    return values


def normalize_number(key: str, raw: object) -> str:
    _, kind, default, minimum, maximum = SPEC_BY_KEY[key]
    text = str(raw if raw is not None else default).strip()
    if not text:
        text = default
    if kind == "int":
        if not re.fullmatch(r"\d+", text):
            raise ValueError(f"{key} must be an integer.")
        numeric = int(text)
        normalized = str(numeric)
    elif kind == "mb":
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            raise ValueError(f"{key} must be a number of MB.")
        numeric = float(text)
        if not math.isfinite(numeric):
            raise ValueError(f"{key} must be finite.")
        normalized = str(int(round(numeric * 1024 * 1024)))
    else:
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            raise ValueError(f"{key} must be a number.")
        numeric = float(text)
        if not math.isfinite(numeric):
            raise ValueError(f"{key} must be finite.")
        normalized = f"{numeric:.6f}".rstrip("0").rstrip(".")
    if numeric < minimum or numeric > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return normalized


def normalize_value(key: str, raw: object) -> str:
    _, kind, default, minimum, maximum = SPEC_BY_KEY[key]
    if kind in {"int", "float", "mb"}:
        return normalize_number(key, raw)
    raw_text = str(raw if raw is not None else default)
    if kind == "string" and any(character in raw_text for character in "\n\r#"):
        raise ValueError(f"{key} cannot contain newlines or #.")
    text = raw_text.strip()
    if not text:
        text = default
    if kind == "bool":
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return "1"
        if lowered in {"0", "false", "no", "off"}:
            return "0"
        raise ValueError(f"{key} must be a boolean.")
    if kind == "bool_auto":
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on", "auto", "enabled"}:
            return "auto"
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return "off"
        raise ValueError(f"{key} must be a boolean.")
    if kind == "enum":
        options = {
            "LITELLM_MENU_COMPUTER_FACADE_BACKEND": {"auto", "mcp", "browser", "chrome", "playwright", "cua", "mock"},
            "LITELLM_MENU_VISION_BRIDGE_BACKEND": {"auto", "api", "local", "off"},
            "LITELLM_MENU_VISION_BRIDGE_LOCAL_FORMAT": {"compact", "detailed"},
        }.get(key, set())
        lowered = text.lower()
        if lowered not in options:
            raise ValueError(f"{key} must be one of: {', '.join(sorted(options))}")
        return lowered
    if kind == "string":
        if key == "LITELLM_MENU_WEB_SEARCH_REGION" and any(
            character.isspace() for character in text
        ):
            raise ValueError(f"{key} cannot contain whitespace.")
        return text
    raise ValueError(f"Unsupported runtime setting kind: {kind}")


def numeric_equal(left: str, right: str, kind: str) -> bool:
    if kind == "int":
        return int(left) == int(right)
    if kind == "mb":
        return int(left) == int(round(float(right) * 1024 * 1024))
    return float(left) == float(right)


def normalize_stored_number(key: str, raw: object) -> str:
    _, kind, default, minimum, maximum = SPEC_BY_KEY[key]
    if kind != "mb":
        return normalize_number(key, raw)
    text = str(raw if raw is not None else "").strip()
    if not text:
        return normalize_number(key, default)
    if not re.fullmatch(r"\d+", text):
        raise ValueError(f"{key} must be stored as integer bytes.")
    numeric = int(text)
    minimum_bytes = int(round(float(minimum) * 1024 * 1024))
    maximum_bytes = int(round(float(maximum) * 1024 * 1024))
    if numeric < minimum_bytes or numeric > maximum_bytes:
        raise ValueError(f"{key} must be between {minimum} and {maximum} MB.")
    return str(numeric)


try:
    submitted = parse_payload()
    unknown = sorted(set(submitted) - set(SPEC_BY_KEY))
    if unknown:
        raise ValueError("Unknown runtime setting(s): " + ", ".join(unknown))
    normalized: dict[str, str] = {}
    for key, kind, default, _, _ in SPECS:
        if key in submitted:
            value = normalize_value(key, submitted[key])
        else:
            if kind == "mb":
                value = normalize_stored_number(key, os.environ.get(key))
            else:
                value = normalize_value(key, os.environ.get(key))
        if kind in {"int", "float", "mb"}:
            if not numeric_equal(value, default, kind):
                normalized[key] = value
        elif kind == "bool_auto":
            if value != str(default):
                normalized[key] = value
        elif value != str(default):
            normalized[key] = value
except Exception as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)

path = pathlib.Path(os.environ["RUNTIME_SETTINGS_FILE"])
path.parent.mkdir(parents=True, exist_ok=True)
if normalized:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("# LiteLLM Menu runtime thresholds. Generated by the menu app.\n")
        for key, _, _, _, _ in SPECS:
            if key in normalized:
                handle.write(f"{key}={normalized[key]}\n")
    os.replace(tmp_name, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
else:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
print(json.dumps({"path": str(path), "saved": normalized}, ensure_ascii=False, indent=2))
PY
}

runtime_settings_reset() {
  rm -f "$RUNTIME_SETTINGS_FILE"
  echo "Runtime settings reset to defaults: $RUNTIME_SETTINGS_FILE"
}

health_ok() {
  curl -fsS --max-time 1 "$HEALTH_URL" >/dev/null 2>&1
}

runtime_config_matches_source() {
  [[ -f "$CONFIG_FILE" && -f "$RUNTIME_CONFIG" ]] || return 1
  cmp -s "$CONFIG_FILE" "$RUNTIME_CONFIG"
}

managed_server_reachable() {
  health_ok && { native_running || launch_agent_loaded; }
}

menu_app_binary_path() {
  printf '%s\n' "$APP_BUNDLE_PATH/Contents/MacOS/LiteLLMMenu"
}

process_is_menu_app_pid() {
  local pid="$1" command app_bin
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  app_bin="$(menu_app_binary_path)"
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ -n "$command" ]] || return 1
  [[ "$command" == "$app_bin"* ]]
}

menu_app_pids() {
  local pid command app_bin
  app_bin="$(menu_app_binary_path)"
  ps axww -o pid= -o command= 2>/dev/null | while read -r pid command; do
    [[ -n "$pid" && "$command" == "$app_bin"* ]] || continue
    printf '%s\n' "$pid"
  done
}

menu_app_owner_pid() {
  local pid
  pid="${LITELLM_MENU_OWNER_PID:-}"
  if process_is_menu_app_pid "$pid"; then
    printf '%s\n' "$pid"
    return 0
  fi

  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    printf '%s\n' "$pid"
    return 0
  done < <(menu_app_pids)
  return 1
}

menu_app_running() {
  menu_app_owner_pid >/dev/null 2>&1
}

stop_orphaned_service_if_menu_missing() {
  local pids
  menu_app_running && return 0
  pids="$(native_pid_candidates || true)"
  [[ -n "$pids" ]] || return 0
  request_native_process_stop_list "$pids" >/dev/null 2>&1 || true
  sleep 0.3
  force_native_process_stop_list "$(native_pid_candidates || true)" >/dev/null 2>&1 || true
  clear_state
  return 0
}

require_menu_app_owner() {
  local action="$1" owner_pid
  if owner_pid="$(menu_app_owner_pid)"; then
    printf '%s\n' "$owner_pid"
    return 0
  fi

  stop_orphaned_service_if_menu_missing
  echo "Refusing to $action LiteLLM service because LiteLLM Menu app is not running." >&2
  echo "Open LiteLLM Menu.app instead; the Menu app is the required service owner." >&2
  return 64
}

write_state() {
  mkdir -p "$(dirname "$STATE_FILE")"
  printf '%s %s\n' "$(date +%s)" "$1" > "$STATE_FILE"
}

clear_state() {
  rm -f "$STATE_FILE"
}

recent_state() {
  local state_time state_name now
  [[ -f "$STATE_FILE" ]] || return 1
  read -r state_time state_name < "$STATE_FILE" || return 1
  [[ "$state_time" =~ ^[0-9]+$ ]] || return 1
  now="$(date +%s)"
  if (( now - state_time <= STATE_TTL_SECONDS )); then
    echo "$state_name"
    return 0
  fi
  return 1
}

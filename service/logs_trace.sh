# shellcheck shell=bash
route_trace_enable() {
  local tmp
  mkdir -p "$RUNTIME_DIR"
  tmp="$(mktemp "$RUNTIME_DIR/route-trace.enabled.XXXXXX")"
  printf '1\n' > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ROUTE_TRACE_STATE_FILE"
  echo "Route trace enabled for the running service and future starts"
}

route_trace_disable() {
  local tmp
  mkdir -p "$RUNTIME_DIR"
  tmp="$(mktemp "$RUNTIME_DIR/route-trace.disabled.XXXXXX")"
  {
    printf '0\n'
    printf 'disabled_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ROUTE_TRACE_STATE_FILE"
  echo "Route trace disabled for the running service and future starts"
}

route_trace_status() {
  if [[ "$(route_trace_state_value)" == "1" ]]; then
    echo "enabled"
    exit 0
  fi
  echo "disabled"
  exit 1
}

recent_requests_log() {
  local formatter_python
  rotate_log_if_needed "$RECENT_REQUESTS_LOG"

  cat <<EOF
Source: local LiteLLM Menu request summary log.
Log path: $RECENT_REQUESTS_LOG
Showing all request events in this file.

This is a local JSONL summary log. It stores routing/status metadata only;
prompt bodies, message content, Authorization headers, and provider API keys are not stored.

EOF

  if [[ ! -s "$RECENT_REQUESTS_LOG" ]]; then
    cat <<EOF
No recent request log entries yet.

The service writes this file after it is restarted with the current LiteLLM Menu hook.
For deeper routing diagnostics, enable Route Trace, run a request, and open the trace log.

Related local logs:
  recent requests: $RECENT_REQUESTS_LOG
  service stdout/stderr: $LOG_FILE
  menu actions: $MENU_ACTIONS_LOG
  config watch: $CONFIG_WATCH_LOG
EOF
    return 0
  fi

  if [[ -x "$NATIVE_PYTHON" ]]; then
    formatter_python="$NATIVE_PYTHON"
  elif formatter_python="$(find_bootstrap_python 2>/dev/null)"; then
    :
  else
    echo "Could not start the bundled Python formatter; showing raw JSONL instead." >&2
    cat "$RECENT_REQUESTS_LOG"
    return 0
  fi

  "$formatter_python" - "$RECENT_REQUESTS_LOG" <<'PY'
from __future__ import annotations

import json
import pathlib
import sys


path = pathlib.Path(sys.argv[1])
rows: list[dict] = []
invalid = 0

with path.open("r", encoding="utf-8", errors="replace") as handle:
    for line in handle:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if isinstance(row, dict):
            rows.append(row)


def text(value, default="-"):
    if value is None or value == "":
        return default
    return str(value)


def ts(value):
    value = text(value)
    if value == "-":
        return value
    return value.replace("T", " ").replace("Z", "")[:19]


def duration(value):
    if isinstance(value, int):
        return f"{value}ms"
    return "-"


def money(value):
    if isinstance(value, (int, float)):
        return f"${value:.6f}"
    return "-"


def usage(value):
    if not isinstance(value, dict):
        return "-"
    total = value.get("total_tokens")
    in_tokens = value.get("input_tokens") or value.get("prompt_tokens")
    out_tokens = value.get("output_tokens") or value.get("completion_tokens")
    parts = []
    if total is not None:
        parts.append(f"total={total}")
    if in_tokens is not None:
        parts.append(f"in={in_tokens}")
    if out_tokens is not None:
        parts.append(f"out={out_tokens}")
    return ",".join(parts) if parts else "-"


def session(value):
    if not isinstance(value, dict):
        return "-"
    if value.get("name"):
        return text(value.get("name"))
    if value.get("id"):
        return text(value.get("id"))
    return "-"


def tools(value):
    if isinstance(value, list) and value:
        return ",".join(str(item) for item in value)
    return "-"


def error(value):
    if not isinstance(value, dict) or not value:
        return "-"
    parts = []
    for key in ("type", "status_code", "reason", "code", "failed_deployment_id", "failed_deployment_order"):
        item = value.get(key)
        if item not in (None, ""):
            parts.append(f"{key}={item}")
    return " ".join(parts) if parts else "-"


def stuck(value):
    if not isinstance(value, dict) or not value:
        return "-"
    parts = []
    for key in (
        "reason",
        "stream_idle_timeout_seconds",
        "stream_start_timeout_seconds",
        "stream_saw_chunk",
        "stream_buffered_chunks",
    ):
        item = value.get(key)
        if item not in (None, ""):
            parts.append(f"{key}={item}")
    return " ".join(parts) if parts else "-"


if not rows:
    print("No valid recent request JSONL entries found.")
    if invalid:
        print(f"Skipped invalid JSONL lines: {invalid}")
    sys.exit(0)

for row in rows:
    route = text(row.get("route_key"))
    if not route or route == "-":
        parts = [
            text(row.get("provider")),
            text(row.get("upstream_model")),
        ]
        key_name = text(row.get("api_key_name"))
        if key_name and key_name != "-":
            parts.append(f"key={key_name}")
        if row.get("deployment_order") is not None:
            parts.append(f"order={row.get('deployment_order')}")
        route = " / ".join(part for part in parts if part and part != "-") or "-"
    token = text(row.get("deployment_token") or row.get("deployment_id"))
    flags = []
    if row.get("has_image_generation_tool") is True:
        flags.append("image_tool")
    if row.get("has_image_input") is True:
        flags.append("image_input")
    flag_text = f" flags={','.join(flags)}" if flags else ""
    print(
        f"{ts(row.get('ts'))} "
        f"{text(row.get('status')).upper():<7} "
        f"{duration(row.get('duration_ms')):<8} "
        f"model={text(row.get('model_group'))} "
        f"route={route} "
        f"token={token} "
        f"host={text(row.get('api_base_host'))} "
        f"tokens={usage(row.get('usage'))} "
        f"cost={money(row.get('response_cost'))} "
        f"tools={tools(row.get('tool_types'))} "
        f"session={session(row.get('session'))} "
        f"request_id={text(row.get('request_id'))}"
        f"{flag_text}"
    )
    if row.get("stuck"):
        print(f"  stuck: {stuck(row.get('stuck'))}")
    if row.get("error"):
        print(f"  error: {error(row.get('error'))}")

if invalid:
    print(f"\nSkipped invalid JSONL lines: {invalid}")
PY
}

menu_actions_tail() {
  rotate_log_if_needed "$MENU_ACTIONS_LOG"
  if [[ -f "$MENU_ACTIONS_LOG" ]]; then
    tail -n 120 "$MENU_ACTIONS_LOG"
  else
    echo "No menu actions log file yet: $MENU_ACTIONS_LOG"
  fi
}

log_file_summary_line() {
  local label="$1" path="$2" description="$3" bytes lines modified
  if [[ -f "$path" ]]; then
    bytes="$(wc -c < "$path" | tr -d '[:space:]')"
    lines="$(wc -l < "$path" | tr -d '[:space:]')"
    modified="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$path" 2>/dev/null || echo unknown)"
    printf '%-22s present  %10s bytes  %7s lines  modified=%s\n' "$label" "$bytes" "$lines" "$modified"
  else
    printf '%-22s missing  %s\n' "$label" "$path"
  fi
  printf '  path: %s\n' "$path"
  printf '  use: %s\n' "$description"
}

logs_summary() {
  rotate_local_logs_if_needed
  cat <<EOF
LiteLLM Menu local logs
Runtime root: $ROOT

EOF
  log_file_summary_line "recent requests" "$RECENT_REQUESTS_LOG" "View Recent Requests Log"
  if [[ -f "$RECENT_REQUESTS_LOG.1" ]]; then
    log_file_summary_line "recent requests .1" "$RECENT_REQUESTS_LOG.1" "previous rotated request summary file"
  fi
  log_file_summary_line "service log" "$LOG_FILE" "View Service Log"
  if [[ -f "$LOG_FILE.1" ]]; then
    log_file_summary_line "service log .1" "$LOG_FILE.1" "previous rotated service log tail"
  fi
  log_file_summary_line "menu actions" "$MENU_ACTIONS_LOG" "View Menu Actions Log"
  if [[ -f "$MENU_ACTIONS_LOG.1" ]]; then
    log_file_summary_line "menu actions .1" "$MENU_ACTIONS_LOG.1" "previous rotated menu actions tail"
  fi
  log_file_summary_line "config watch" "$CONFIG_WATCH_LOG" "View Config Watch Log"
  if [[ -f "$CONFIG_WATCH_LOG.1" ]]; then
    log_file_summary_line "config watch .1" "$CONFIG_WATCH_LOG.1" "previous rotated config watch tail"
  fi

  cat <<EOF

Route trace:
  state: $(route_trace_state_value)
  state file: $ROUTE_TRACE_STATE_FILE
  route trace viewer: View Route Trace Log

Launch agents:
  session service plist: $SESSION_LAUNCH_AGENT_PLIST
  login service plist: $AUTOSTART_LAUNCH_AGENT_PLIST
  login menu app plist: $APP_LAUNCH_AGENT_PLIST
  config watch plist: $CONFIG_WATCH_PLIST

Default policy:
  No local database is required or configured for this app runtime.
EOF
}

route_trace_log() {
  rotate_log_if_needed "$LOG_FILE"
  local scan_lines trace_lines lines
  scan_lines="$ROUTE_TRACE_SCAN_LINES"
  trace_lines="$ROUTE_TRACE_LINES"

  cat <<EOF
Source: native service log filtered to litellm_route_trace lines from litellm_menu/callbacks.py.
Showing the last $trace_lines trace lines found while scanning the last $scan_lines LiteLLM log lines.
Requires Route Trace to be enabled before the requests you want to inspect.

EOF

  if [[ ! -f "$LOG_FILE" ]]; then
    echo "No service log file yet: $LOG_FILE"
    return 0
  fi

  lines="$(tail -n "$scan_lines" "$LOG_FILE" | grep -F 'litellm_route_trace' | tail -n "$trace_lines" || true)"
  if [[ -n "$lines" ]]; then
    printf '%s\n' "$lines"
  else
    cat <<'EOF'
No litellm_route_trace lines found in the scanned native service log window.
Enable Route Trace, run a request, and open this log again.
EOF
  fi
}

route_trace_html() {
  rotate_log_if_needed "$LOG_FILE"
  local scan_lines max_requests
  scan_lines="$ROUTE_TRACE_SCAN_LINES"
  max_requests="$ROUTE_TRACE_MAX_REQUESTS"
  ensure_python_tools || return 1
  if [[ ! -f "$LOG_FILE" ]]; then
    echo "No service log file yet: $LOG_FILE" >&2
    return 1
  fi
  tail -n "$scan_lines" "$LOG_FILE" | "$PYTHON" "$TEMPLATE_ROOT/route_trace_report.py" \
    --scan-lines "$scan_lines" \
    --trace-state-file "$ROUTE_TRACE_STATE_FILE" \
    --trace-state-status "$(route_trace_state_value)" \
    --max-requests "$max_requests"
}

route_recovery_html() {
  ensure_python_tools || return 1
  "$PYTHON" "$TEMPLATE_ROOT/route_recovery_report.py" \
    --recovery-state-file "$ROUTE_RECOVERY_STATE_FILE" \
    --cooldown-state-file "$DEPLOYMENT_COOLDOWN_FILE" \
    --recent-requests-log "$RECENT_REQUESTS_LOG"
}

route_recovery_summary() {
  ensure_python_tools || return 1
  "$PYTHON" "$TEMPLATE_ROOT/route_recovery_report.py" \
    --summary \
    --recovery-state-file "$ROUTE_RECOVERY_STATE_FILE" \
    --cooldown-state-file "$DEPLOYMENT_COOLDOWN_FILE"
}

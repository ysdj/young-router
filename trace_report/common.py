#!/usr/bin/env python3
"""Render LiteLLM route trace JSON lines as a static HTML viewer."""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import html
import json
import pathlib
import re
import sys
from typing import Any


TRACE_MARKER = "litellm_route_trace "
SUMMARY_PREVIEW_CHARS = 260
TIMESTAMP_FIELDS = ("timestamp", "time", "created_at")
EVENT_LABELS = {
    "generic_fallback_helper_start": "Fallback check started",
    "filter_deployments": "Filtered candidate pool",
    "selected_deployment": "Selected upstream",
    "deployment_failover_marked": "Marked upstream as failed",
    "fallback_target_order_constraint": "Moved to fallback order",
    "same_order_peer_fallback_available": "Same-order fallback available",
    "same_order_peer_fallback_unavailable": "No same-order fallback",
    "same_order_peer_fallback_start": "Trying same-order fallback",
    "litellm_fallback_common_utils": "LiteLLM fallback started",
    "streaming_error_fallback_start": "Streaming fallback started",
    "streaming_error_fallback_error": "Streaming fallback failed",
    "generic_fallback_helper_error": "Fallback helper failed",
    "filter_deployments_error": "Candidate filtering failed",
}
INTERNAL_CONTEXT_PREFIXES = (
    "another language model started to solve this problem",
    "<environment_context>",
    "<permissions instructions>",
    "<app-context>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<plugins_instructions>",
    "<skill>",
)
FALLBACK_EVENTS = {
    "deployment_failover_marked",
    "fallback_target_order_constraint",
    "same_order_peer_fallback_available",
    "same_order_peer_fallback_unavailable",
    "same_order_peer_fallback_start",
    "litellm_fallback_common_utils",
    "streaming_error_fallback_start",
    "streaming_error_fallback_error",
    "generic_fallback_helper_error",
    "filter_deployments_error",
}


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def compact_text(value: Any, *, limit: int = SUMMARY_PREVIEW_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def is_internal_context_text(value: Any) -> bool:
    lowered = str(value or "").strip().lower()
    return any(lowered.startswith(prefix) for prefix in INTERNAL_CONTEXT_PREFIXES)


def split_preview_segment(segment: str) -> tuple[str, str]:
    for role in ("user", "human", "assistant", "system", "developer"):
        prefix = f"{role}:"
        if segment.lower().startswith(prefix):
            return role, segment[len(prefix):].strip()
    return "", segment.strip()


def recover_user_text_from_preview(preview_text: Any) -> str:
    text = str(preview_text or "")
    for segment in reversed([part.strip() for part in text.split(" | ") if part.strip()]):
        role, body = split_preview_segment(segment)
        if role in {"user", "human"} and body and not is_internal_context_text(body):
            return body
    return ""


def full_preview_attr(display_value: Any, full_value: Any) -> str:
    display_text = str(display_value or "").strip()
    full_text = str(full_value or "").strip()
    if not full_text or full_text == display_text:
        return ""
    return f' data-full-preview="{esc(full_text)}" tabindex="0"'


def parse_datetime(value: Any) -> _dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return _dt.datetime.fromtimestamp(timestamp, tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if "," in text:
        text = text.replace(",", ".", 1)
    if re.search(r"[+-]\d{4}$", text):
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def event_datetime(event: dict[str, Any]) -> _dt.datetime | None:
    for field in TIMESTAMP_FIELDS:
        parsed = parse_datetime(event.get(field))
        if parsed is not None:
            return parsed
    return None


def event_time_label(event: dict[str, Any], *, include_date: bool = False) -> str:
    parsed = event_datetime(event)
    if parsed is None:
        return "missing timestamp"
    local = parsed.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S" if include_date else "%H:%M:%S")


def time_range_label(events: list[dict[str, Any]]) -> str:
    parsed = [value for value in (event_datetime(event) for event in events) if value]
    if not parsed:
        return "no timestamp"
    first = min(parsed).astimezone()
    last = max(parsed).astimezone()
    if first.date() == last.date():
        return f"{first:%H:%M:%S} - {last:%H:%M:%S}"
    return f"{first:%m-%d %H:%M:%S} - {last:%m-%d %H:%M:%S}"


def duration_label(events: list[dict[str, Any]]) -> str:
    parsed = [value for value in (event_datetime(event) for event in events) if value]
    if len(parsed) < 2:
        return ""
    seconds = max(0.0, (max(parsed) - min(parsed)).total_seconds())
    if seconds < 1:
        return f"{int(seconds * 1000)} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {int(remainder)}s"


def local_datetime_label(value: _dt.datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def first_state_token(text: str) -> str:
    for line in text.splitlines():
        token = line.strip().split(maxsplit=1)[0] if line.strip() else ""
        if token:
            return token.lower()
    return ""


def read_trace_state(trace_state_file: str | None) -> dict[str, Any]:
    state: dict[str, Any] = {"status": "unknown", "disabled_at": None}
    if not trace_state_file:
        return state

    path = pathlib.Path(trace_state_file)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return state
    except OSError as exc:
        state["error"] = str(exc)
        return state

    token = first_state_token(text)
    if token in {"1", "true", "yes", "y", "on", "enabled", "debug"}:
        state["status"] = "enabled"
        return state

    state["status"] = "disabled"
    for line in text.splitlines()[1:]:
        key, sep, value = line.partition("=")
        if sep and key.strip() == "disabled_at":
            state["disabled_at"] = parse_datetime(value.strip())
            break
    if state["disabled_at"] is None:
        try:
            state["disabled_at"] = _dt.datetime.fromtimestamp(
                path.stat().st_mtime,
                tz=_dt.timezone.utc,
            )
        except OSError:
            pass
    return state


def merge_trace_state_status(trace_state: dict[str, Any], status_value: str | None) -> dict[str, Any]:
    token = str(status_value or "").strip().lower()
    if not token:
        return trace_state
    merged = dict(trace_state)
    if token in {"1", "true", "yes", "y", "on", "enabled", "debug"}:
        merged["status"] = "enabled"
    elif token in {"0", "false", "no", "n", "off", "disabled"}:
        merged["status"] = "disabled"
    return merged


def trace_state_banner(trace_state: dict[str, Any]) -> str:
    if trace_state.get("status") != "disabled":
        return ""
    disabled_at = local_datetime_label(trace_state.get("disabled_at"))
    if disabled_at:
        message = (
            f"No trace events are recorded after {disabled_at} because Route Trace is off."
        )
    else:
        message = "Route Trace is off, so new requests are not being recorded."
    return (
        '<section class="trace-state trace-state-off">'
        "<b>Route Trace is off</b>"
        f"<span>{esc(message)} Turn it back on from the menu to resume logging.</span>"
        "</section>"
    )


def event_label(name: str) -> str:
    if name in EVENT_LABELS:
        return EVENT_LABELS[name]
    return name.replace("_", " ").strip().title() or "Trace Event"


def parse_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        if TRACE_MARKER not in line:
            continue
        payload = line.split(TRACE_MARKER, 1)[1].strip()
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if not event.get("timestamp"):
            continue
        event["_seq"] = len(events) + 1
        events.append(event)
    return events

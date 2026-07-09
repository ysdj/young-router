#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional


DEFAULT_RUNTIME_DIR = Path.home() / ".litellm-menu"
DEFAULT_RECENT_REQUESTS = DEFAULT_RUNTIME_DIR / "recent-requests.jsonl"
DEFAULT_ROUTE_LOG = DEFAULT_RUNTIME_DIR / "menu-server.log"
ROUTE_TRACE_PREFIX = "litellm_route_trace "


def compact_text(value: Any, *, limit: int = 220) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


def json_loads_line(line: str) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(line)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def nested_dict(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, dict) else {}


def nested_list(value: Any, key: str) -> list[Any]:
    if not isinstance(value, dict):
        return []
    nested = value.get(key)
    return nested if isinstance(nested, list) else []


def session_id_from(value: Any) -> Optional[str]:
    session = nested_dict(value, "session")
    session_id = session.get("id")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    return None


def request_id_from(value: Any) -> Optional[str]:
    request_id = value.get("request_id") if isinstance(value, dict) else None
    if isinstance(request_id, str) and request_id.strip():
        return request_id.strip()
    return None


def route_key_from_event(event: dict[str, Any]) -> str:
    for source in (
        event,
        nested_dict(event, "request"),
        nested_dict(event, "retry_request"),
        nested_dict(event, "deployment"),
    ):
        route_key = source.get("route_key") if isinstance(source, dict) else None
        if isinstance(route_key, str) and route_key.strip():
            return route_key.strip()
    deployment = nested_dict(event, "deployment")
    provider = deployment.get("provider")
    model = deployment.get("model")
    if isinstance(provider, str) and isinstance(model, str):
        return f"{provider} / {model}"
    return ""


def model_group_from_event(event: dict[str, Any]) -> str:
    for source in (event, nested_dict(event, "request"), nested_dict(event, "retry_request")):
        model_group = source.get("model_group") if isinstance(source, dict) else None
        if isinstance(model_group, str) and model_group.strip():
            return model_group.strip()
    return ""


def event_exception_summary(event: dict[str, Any]) -> str:
    exc = event.get("exception")
    if not isinstance(exc, dict):
        return ""
    parts = []
    for key in ("reason", "status_code", "class"):
        value = exc.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    text = compact_text(exc.get("text"), limit=160)
    if text:
        parts.append(text)
    return "; ".join(parts)


def request_preview_from_event(event: dict[str, Any]) -> str:
    candidates = []
    for source in (event, nested_dict(event, "request"), nested_dict(event, "retry_request")):
        for key in ("request_preview", "preview"):
            preview = nested_dict(source, key)
            if not preview:
                continue
            for text_key in ("latest_user", "preview"):
                value = preview.get(text_key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
    return compact_text(" | ".join(candidates), limit=260)


def tool_names_from(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    names = value.get("tool_names")
    if isinstance(names, list):
        return [str(name) for name in names if name]
    tools = nested_dict(value, "tools")
    tool_names = tools.get("names")
    if isinstance(tool_names, list):
        return [str(name) for name in tool_names if name]
    return []


def record_ids(record: dict[str, Any]) -> set[str]:
    ids = set()
    for value in (request_id_from(record), session_id_from(record)):
        if value:
            ids.add(value)
    return ids


def event_ids(event: dict[str, Any]) -> set[str]:
    ids = set()
    for value in (request_id_from(event), session_id_from(event)):
        if value:
            ids.add(value)
    return ids


def line_matches_identifiers(line: str, identifiers: set[str]) -> bool:
    return any(identifier and identifier in line for identifier in identifiers)


def scan_recent_requests(
    path: Path,
    identifiers: set[str],
    *,
    text_search: bool = False,
) -> tuple[list[dict[str, Any]], set[str]]:
    matches: list[dict[str, Any]] = []
    discovered: set[str] = set()
    if not path.exists() or not identifiers:
        return matches, discovered
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            record = json_loads_line(line)
            if record is None:
                continue
            structured_ids = record_ids(record)
            if not structured_ids.intersection(identifiers) and not (
                text_search and line_matches_identifiers(line, identifiers)
            ):
                continue
            discovered.update(record_ids(record))
            matches.append({"line": line_number, "record": record})
    return matches, discovered


def scan_route_trace(
    path: Path,
    identifiers: set[str],
    *,
    text_search: bool = False,
) -> tuple[list[dict[str, Any]], set[str]]:
    matches: list[dict[str, Any]] = []
    discovered: set[str] = set()
    if not path.exists() or not identifiers:
        return matches, discovered
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            if ROUTE_TRACE_PREFIX not in line:
                continue
            raw = line.split(ROUTE_TRACE_PREFIX, 1)[1].strip()
            event = json_loads_line(raw)
            if event is None:
                continue
            structured_ids = event_ids(event)
            if not structured_ids.intersection(identifiers) and not (
                text_search and line_matches_identifiers(line, identifiers)
            ):
                continue
            discovered.update(event_ids(event))
            matches.append({"line": line_number, "event": event})
    return matches, discovered


def dedupe_by_line(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        line = item.get("line")
        if not isinstance(line, int) or line in seen:
            continue
        seen.add(line)
        result.append(item)
    return sorted(result, key=lambda item: item["line"])


def summarize_recent_item(item: dict[str, Any]) -> dict[str, Any]:
    record = item["record"]
    error = record.get("error") if isinstance(record.get("error"), dict) else {}
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    return {
        "line": item["line"],
        "ts": record.get("ts"),
        "request_id": request_id_from(record),
        "session_id": session_id_from(record),
        "status": record.get("status"),
        "model_group": record.get("model_group"),
        "route_key": record.get("route_key"),
        "duration_ms": record.get("duration_ms"),
        "usage": usage or None,
        "tool_names": tool_names_from(record),
        "error": {
            "reason": error.get("reason"),
            "status_code": error.get("status_code"),
            "type": error.get("type"),
        }
        if error
        else None,
    }


def summarize_event_item(item: dict[str, Any]) -> dict[str, Any]:
    event = item["event"]
    request = nested_dict(event, "request")
    retry_request = nested_dict(event, "retry_request")
    metadata_flags = request.get("metadata_flags") or retry_request.get("metadata_flags")
    selected = nested_list(event, "selected_candidates")
    healthy = nested_list(event, "healthy")
    return {
        "line": item["line"],
        "timestamp": event.get("timestamp"),
        "event": event.get("event"),
        "request_id": request_id_from(event),
        "session_id": session_id_from(event),
        "model_group": model_group_from_event(event),
        "route_key": route_key_from_event(event),
        "poll_attempt": event.get("poll_attempt"),
        "elapsed_seconds": event.get("elapsed_seconds"),
        "metadata_flags": metadata_flags if isinstance(metadata_flags, dict) else None,
        "tool_names": tool_names_from(request) or tool_names_from(retry_request) or tool_names_from(event),
        "queries": event.get("queries"),
        "actions": event.get("actions") or event.get("next_actions"),
        "exception": event_exception_summary(event),
        "preview": request_preview_from_event(event),
        "selected_route_keys": [candidate.get("route_key") for candidate in selected if isinstance(candidate, dict) and candidate.get("route_key")],
        "healthy_route_keys": [candidate.get("route_key") for candidate in healthy if isinstance(candidate, dict) and candidate.get("route_key")],
    }


def build_debug_result(
    identifiers: list[str],
    *,
    recent_requests_path: Path = DEFAULT_RECENT_REQUESTS,
    route_log_path: Path = DEFAULT_ROUTE_LOG,
    max_passes: int = 4,
    text_search: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    active_ids = {identifier.strip() for identifier in identifiers if identifier.strip()}
    all_recent: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []

    for _pass in range(max(1, max_passes)):
        before = set(active_ids)
        recent_matches, recent_ids = scan_recent_requests(
            recent_requests_path,
            active_ids,
            text_search=text_search,
        )
        event_matches, event_ids_found = scan_route_trace(
            route_log_path,
            active_ids,
            text_search=text_search,
        )
        all_recent.extend(recent_matches)
        all_events.extend(event_matches)
        active_ids.update(recent_ids)
        active_ids.update(event_ids_found)
        if active_ids == before:
            break

    recent_items = dedupe_by_line(all_recent)
    event_items = dedupe_by_line(all_events)
    recent_summaries = [summarize_recent_item(item) for item in recent_items]
    event_summaries = [summarize_event_item(item) for item in event_items]

    request_ids = sorted(
        value
        for value in {item.get("request_id") for item in recent_summaries + event_summaries}
        if isinstance(value, str) and value
    )
    session_ids = sorted(
        value
        for value in {item.get("session_id") for item in recent_summaries + event_summaries}
        if isinstance(value, str) and value
    )
    statuses = Counter(str(item.get("status") or "unknown") for item in recent_summaries)
    events = Counter(str(item.get("event") or "unknown") for item in event_summaries)
    recovery_events = [
        item
        for item in event_summaries
        if isinstance(item.get("event"), str) and "recovery" in item["event"]
    ]
    failures = [item for item in recent_summaries if item.get("status") == "failure"]
    successes = [item for item in recent_summaries if item.get("status") == "success"]

    result = {
        "input_ids": sorted(active_ids.intersection(set(identifiers))) or identifiers,
        "logs": {
            "recent_requests": str(recent_requests_path),
            "route_log": str(route_log_path),
            "recent_requests_exists": recent_requests_path.exists(),
            "route_log_exists": route_log_path.exists(),
            "text_search": text_search,
        },
        "related_ids": {
            "request_ids": request_ids,
            "session_ids": session_ids,
            "all_ids": sorted(active_ids),
        },
        "summary": {
            "recent_request_count": len(recent_summaries),
            "route_trace_event_count": len(event_summaries),
            "status_counts": dict(statuses),
            "event_counts": dict(events),
            "route_recovery_event_count": len(recovery_events),
            "latest_success": successes[-1] if successes else None,
            "latest_failure": failures[-1] if failures else None,
            "latest_route_event": event_summaries[-1] if event_summaries else None,
        },
        "recent_requests": recent_summaries,
        "route_trace_events": event_summaries,
    }
    if full:
        result["raw"] = {
            "recent_requests": recent_items,
            "route_trace_events": event_items,
        }
    return result


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "(none)"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        cells = []
        for value in row:
            text = compact_text(value, limit=180).replace("|", "\\|")
            cells.append(text)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_markdown(result: dict[str, Any], *, max_recent: int, max_events: int) -> str:
    summary = result["summary"]
    related = result["related_ids"]
    logs = result["logs"]
    lines = ["# Codex Debug Results", ""]
    lines.append(f"Input ids: `{', '.join(result['input_ids'])}`")
    lines.append(f"Recent requests: `{logs['recent_requests']}` exists={logs['recent_requests_exists']}")
    lines.append(f"Route log: `{logs['route_log']}` exists={logs['route_log_exists']}")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Matched recent requests: {summary['recent_request_count']}")
    lines.append(f"- Matched route trace events: {summary['route_trace_event_count']}")
    lines.append(f"- Status counts: `{json.dumps(summary['status_counts'], ensure_ascii=False, sort_keys=True)}`")
    lines.append(f"- Route recovery events: {summary['route_recovery_event_count']}")
    lines.append(f"- Request ids: `{', '.join(related['request_ids']) or '(none)'}`")
    lines.append(f"- Session ids: `{', '.join(related['session_ids']) or '(none)'}`")
    if summary.get("latest_success"):
        item = summary["latest_success"]
        lines.append(
            "- Latest success: "
            f"line {item.get('line')} ts={item.get('ts')} request_id={item.get('request_id')} "
            f"route={item.get('route_key')} usage={compact_text(item.get('usage'), limit=120)}"
        )
    if summary.get("latest_failure"):
        item = summary["latest_failure"]
        lines.append(
            "- Latest failure: "
            f"line {item.get('line')} ts={item.get('ts')} request_id={item.get('request_id')} "
            f"route={item.get('route_key')} error={compact_text(item.get('error'), limit=160)}"
        )
    lines.append("")
    lines.append("## Recent Requests")
    recent_rows = []
    for item in result["recent_requests"][-max_recent:]:
        recent_rows.append(
            [
                item.get("line"),
                item.get("ts"),
                item.get("status"),
                item.get("request_id"),
                item.get("session_id"),
                item.get("model_group"),
                item.get("route_key"),
                item.get("duration_ms"),
                item.get("usage") or item.get("error"),
            ]
        )
    lines.append(
        markdown_table(
            ["line", "ts", "status", "request_id", "session", "model", "route", "ms", "usage/error"],
            recent_rows,
        )
    )
    lines.append("")
    lines.append("## Route Trace Timeline")
    event_rows = []
    for item in result["route_trace_events"][-max_events:]:
        evidence = item.get("exception") or item.get("actions") or item.get("queries") or item.get("selected_route_keys")
        event_rows.append(
            [
                item.get("line"),
                item.get("timestamp"),
                item.get("event"),
                item.get("request_id"),
                item.get("session_id"),
                item.get("model_group"),
                item.get("route_key"),
                item.get("poll_attempt"),
                evidence,
            ]
        )
    lines.append(
        markdown_table(
            ["line", "timestamp", "event", "request_id", "session", "model", "route", "poll", "evidence"],
            event_rows,
        )
    )
    return "\n".join(lines) + "\n"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Correlate a Codex/session/request id to LiteLLM debug results from "
            "recent-requests.jsonl and menu-server route trace logs."
        )
    )
    parser.add_argument("ids", nargs="+", help="Codex session/thread id or LiteLLM request id to inspect.")
    parser.add_argument("--recent-requests", type=Path, default=DEFAULT_RECENT_REQUESTS)
    parser.add_argument("--route-log", type=Path, default=DEFAULT_ROUTE_LOG)
    parser.add_argument("--max-passes", type=int, default=4, help="Identifier expansion passes.")
    parser.add_argument("--max-recent", type=int, default=80, help="Recent request rows to render in Markdown.")
    parser.add_argument("--max-events", type=int, default=120, help="Route trace rows to render in Markdown.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    parser.add_argument("--json-out", type=Path, help="Also write the JSON result to this path.")
    parser.add_argument("--full", action="store_true", help="Include raw matched records/events in JSON output.")
    parser.add_argument(
        "--text-search",
        action="store_true",
        help=(
            "Also match ids inside free-text previews. Off by default to avoid "
            "polluting results when a debugging thread merely mentions another id."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    result = build_debug_result(
        args.ids,
        recent_requests_path=args.recent_requests,
        route_log_path=args.route_log,
        max_passes=args.max_passes,
        text_search=args.text_search,
        full=args.full,
    )
    json_text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if args.json_out:
        args.json_out.write_text(json_text + "\n", encoding="utf-8")
    if args.json:
        sys.stdout.write(json_text + "\n")
    else:
        sys.stdout.write(
            render_markdown(result, max_recent=args.max_recent, max_events=args.max_events)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

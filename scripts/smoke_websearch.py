#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:4000"
DEFAULT_API_KEY = "sk-local-litellm"
DEFAULT_MODEL = "balanced-chat"
DEFAULT_QUERY = "OpenAI homepage URL"
DEFAULT_USER_AGENT = "codex-smoke-websearch/1.0"
DEFAULT_TRACE_LOG = str(Path.home() / ".litellm-menu" / "menu-server.log")


def responses_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/responses"
    return f"{base_url}/v1/responses"


def json_loads_maybe(value: bytes | str) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return value


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def response_output_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    output = payload.get("output")
    return [item for item in output if isinstance(item, dict)] if isinstance(output, list) else []


def text_from_message_item(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text") or part.get("output_text") or part.get("input_text")
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks).strip()


def response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    chunks: list[str] = []
    for item in response_output_items(payload):
        if item.get("type") == "message":
            text = text_from_message_item(item)
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def collect_urls(value: Any) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []

    def add(raw: Any) -> None:
        if not isinstance(raw, str):
            return
        raw_urls = re.findall(r"https?://[^\s<>\"']+", raw)
        # A provider citation can concatenate the cited URL and the Markdown
        # target without whitespace; inspect the target separately as well.
        raw_urls.extend(re.findall(r"\]\]\((https?://[^)]+)\)", raw))
        for match in raw_urls:
            url = _clean_extracted_url(match)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

    def visit(node: Any) -> None:
        if isinstance(node, str):
            add(node)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            for key in ("url", "source_url", "href"):
                add(node.get(key))
            for child in node.values():
                visit(child)

    visit(value)
    return urls


def _clean_extracted_url(value: str) -> str:
    """Normalize URLs found inside prose/Markdown without breaking paths."""
    url = value.strip().rstrip(".,;:]}")
    citation_marker = re.search(r"\[\[\d+\]\]\(https?://", url)
    if citation_marker:
        url = url[: citation_marker.start()].rstrip(".,;:]}")
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(".,;:]}")
    return url


def summarize_payload(payload: Any, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    events = events or []
    completed_response = None
    for event in events:
        if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
            completed_response = event["response"]
    payload_for_text = completed_response if completed_response is not None else payload
    output_items = response_output_items(payload_for_text)
    event_items = [
        event.get("item")
        for event in events
        if isinstance(event.get("item"), dict)
    ]
    all_items = output_items + [item for item in event_items if isinstance(item, dict)]

    web_search_items = [
        item
        for item in all_items
        if item.get("type") == "web_search_call"
    ]
    raw_web_search_function_calls = [
        item
        for item in all_items
        if item.get("type") == "function_call" and item.get("name") == "web_search"
    ]
    web_search_event_types = [
        event.get("type")
        for event in events
        if isinstance(event.get("type"), str) and "web_search_call" in event.get("type", "")
    ]
    final_text = response_text(payload_for_text)
    urls = collect_urls(payload_for_text)
    if not urls:
        urls = collect_urls(events)

    return {
        "final_text": final_text,
        "final_text_preview": final_text[:800],
        "has_final_text": bool(final_text),
        "raw_web_search_function_call_count": len(raw_web_search_function_calls),
        "source_urls": urls[:20],
        "stream_event_count": len(events),
        "stream_web_search_event_types": web_search_event_types,
        "web_search_observed": bool(web_search_items or web_search_event_types),
        "web_search_output_item_count": len(web_search_items),
    }


def trace_log_offset(trace_log: str) -> int:
    if not trace_log:
        return 0
    try:
        return Path(trace_log).stat().st_size
    except OSError:
        return 0


def _trace_log_paths(trace_log: str) -> list[Path]:
    if not trace_log:
        return []
    path = Path(trace_log)
    return [Path(f"{path}.1"), path]


def _route_trace_raw_lines(path: Path, offset: int = 0) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    prefix = "litellm_route_trace "
    raw_lines: list[str] = []
    for line in text.splitlines():
        marker = line.find(prefix)
        if marker >= 0:
            raw_lines.append(line[marker + len(prefix) :].strip())
    return raw_lines


def _route_trace_line_digest(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).digest()


def route_trace_cursor(trace_log: str) -> set[bytes]:
    """Snapshot trace lines across the current and rotated service logs."""
    return {
        _route_trace_line_digest(raw)
        for path in _trace_log_paths(trace_log)
        for raw in _route_trace_raw_lines(path)
    }


def read_route_trace_events(trace_log: str, offset: int) -> list[dict[str, Any]]:
    if not trace_log:
        return []
    events: list[dict[str, Any]] = []
    for raw in _route_trace_raw_lines(Path(trace_log), offset):
        try:
            event = json.loads(raw)
        except Exception:
            continue
        events.append(event)
    return events


def read_route_trace_events_since(
    trace_log: str,
    cursor: set[bytes],
) -> list[dict[str, Any]]:
    """Read new trace events even if the service log rotated mid-request."""
    events: list[dict[str, Any]] = []
    seen = set(cursor)
    for path in _trace_log_paths(trace_log):
        for raw in _route_trace_raw_lines(path):
            digest = _route_trace_line_digest(raw)
            if digest in seen:
                continue
            seen.add(digest)
            try:
                event = json.loads(raw)
            except Exception:
                continue
            events.append(event)
    return events


def route_trace_event_text(event: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("event", "request_id", "model_group"):
        value = event.get(key)
        if isinstance(value, str):
            pieces.append(value)
    preview = event.get("request_preview")
    if isinstance(preview, dict):
        for key in ("latest_user", "preview"):
            value = preview.get(key)
            if isinstance(value, str):
                pieces.append(value)
    for key in ("queries", "actions"):
        value = event.get(key)
        if value is not None:
            try:
                pieces.append(json.dumps(value, ensure_ascii=False))
            except Exception:
                pieces.append(str(value))
    return "\n".join(pieces)


def route_trace_request_ids(events: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    request_ids: list[str] = []
    for event in events:
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            continue
        if request_id in seen:
            continue
        seen.add(request_id)
        request_ids.append(request_id)
    return request_ids


def select_route_trace_events(
    events: list[dict[str, Any]],
    *,
    request_id: str,
    query: str,
    model: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not events:
        return [], {"matched_by": "none"}

    request_ids = route_trace_request_ids(events)
    by_request_id: dict[str, list[dict[str, Any]]] = {
        item: [event for event in events if event.get("request_id") == item]
        for item in request_ids
    }
    if request_id in by_request_id:
        return by_request_id[request_id], {
            "available_request_ids": request_ids,
            "matched_by": "request_id",
            "selected_request_id": request_id,
        }

    exact_model_request_ids = {
        candidate_id
        for candidate_id, candidate_events in by_request_id.items()
        if any(
            event.get("model_group") == model
            or (
                isinstance(event.get("deployment"), dict)
                and event["deployment"].get("id") == model
            )
            for event in candidate_events
        )
    }
    needles = [value.lower() for value in (query, model) if value]
    scored: list[tuple[int, int, str, list[dict[str, Any]]]] = []
    for index, candidate_id in enumerate(request_ids):
        if exact_model_request_ids and candidate_id not in exact_model_request_ids:
            continue
        candidate_events = by_request_id[candidate_id]
        text = "\n".join(route_trace_event_text(event) for event in candidate_events).lower()
        score = sum(10 for needle in needles if needle and needle in text)
        if any(
            isinstance(event.get("event"), str)
            and event["event"].startswith("external_web_search_bridge")
            for event in candidate_events
        ):
            score += 100
        if any(event.get("model_group") == model for event in candidate_events):
            score += 1000
        scored.append((score, index, candidate_id, candidate_events))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_score, _index, best_id, best_events = scored[0]
    if best_score > 0:
        return best_events, {
            "available_request_ids": request_ids,
            "matched_by": (
                "exact_model_group_score"
                if exact_model_request_ids
                else "log_offset_score"
            ),
            "score": best_score,
            "selected_request_id": best_id,
        }

    return events, {
        "available_request_ids": request_ids,
        "matched_by": "log_offset_all_events",
    }


def summarize_route_trace(events: list[dict[str, Any]]) -> dict[str, Any]:
    names = [event.get("event") for event in events if isinstance(event.get("event"), str)]
    web_events = [
        event
        for event in events
        if isinstance(event.get("event"), str)
        and event["event"].startswith("external_web_search_bridge")
    ]
    actions: list[Any] = []
    queries: list[Any] = []
    errors: list[dict[str, Any]] = []
    request_ids = route_trace_request_ids(events)
    for event in web_events:
        if "actions" in event:
            actions.append(event.get("actions"))
        if "queries" in event:
            queries.append(event.get("queries"))
        event_name = event.get("event", "")
        if isinstance(event_name, str) and event_name.endswith("_error"):
            errors.append(
                {
                    "event": event_name,
                    "exception": event.get("exception"),
                }
            )
    return {
        "event_count": len(events),
        "events": names[:120],
        "events_truncated": len(names) > 120,
        "external_web_search_actions": actions,
        "external_web_search_errors": errors,
        "external_web_search_event_count": len(web_events),
        "external_web_search_events": [
            event.get("event") for event in web_events if isinstance(event.get("event"), str)
        ],
        "external_web_search_observed": bool(web_events),
        "external_web_search_queries": queries,
        "request_ids": request_ids,
        "router_model_groups": [
            event.get("model_group")
            for event in events
            if event.get("event") in {"generic_fallback_helper_start", "selected_deployment"}
        ],
    }


def read_sse_events(response: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal data_lines
        if not data_lines:
            return
        data = "\n".join(data_lines).strip()
        data_lines = []
        if not data or data == "[DONE]":
            return
        parsed = json_loads_maybe(data)
        if isinstance(parsed, dict):
            events.append(parsed)
        else:
            events.append({"type": "raw", "data": parsed})

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    flush()
    return events


def request_responses(
    *,
    url: str,
    api_key: str,
    user_agent: str,
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, dict[str, str], Any, list[dict[str, Any]]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Accept": "text/event-stream" if payload.get("stream") else "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    }
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            if payload.get("stream"):
                events = read_sse_events(response)
                completed = next(
                    (
                        event.get("response")
                        for event in reversed(events)
                        if event.get("type") == "response.completed"
                        and isinstance(event.get("response"), dict)
                    ),
                    {"events": events},
                )
                return status, response_headers, completed, events
            body = response.read()
            return status, response_headers, json_loads_maybe(body), []
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, json_loads_maybe(body), []
    except urllib.error.URLError as exc:
        return 0, {}, {"error": {"type": exc.__class__.__name__, "message": str(exc)}}, []
    except (TimeoutError, OSError) as exc:
        return 0, {}, {"error": {"type": exc.__class__.__name__, "message": str(exc)}}, []


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    prompt = args.prompt or (
        "Use web_search now to search for this query: "
        f"{args.query!r}. Reply with one short sentence and include one source URL."
    )
    payload: dict[str, Any] = {
        "model": args.model,
        "input": prompt,
        "metadata": {
            "litellm_call_id": args.request_id,
            "smoke": "websearch",
            "request_id": args.request_id,
        },
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
        "max_output_tokens": args.max_output_tokens,
        "stream": args.stream,
    }
    if args.metadata:
        payload["metadata"]["smoke_query"] = args.query
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke-test LiteLLM Menu Responses web_search.")
    parser.add_argument("--base-url", default=os.environ.get("LITELLM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=os.environ.get("LITELLM_API_KEY", DEFAULT_API_KEY))
    parser.add_argument("--model", default=os.environ.get("LITELLM_SMOKE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--query", default=os.environ.get("LITELLM_SMOKE_WEB_QUERY", DEFAULT_QUERY))
    parser.add_argument("--prompt", help="override the default web_search prompt")
    parser.add_argument(
        "--user-agent",
        default=(
            os.environ.get("CODEX_USER_AGENT")
            or os.environ.get("LITELLM_SMOKE_USER_AGENT")
            or DEFAULT_USER_AGENT
        ),
    )
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("LITELLM_SMOKE_TIMEOUT", "180")))
    parser.add_argument("--sleep-before", type=float, default=float(os.environ.get("LITELLM_SMOKE_SLEEP_BEFORE", "0")))
    parser.add_argument("--max-output-tokens", type=int, default=384)
    parser.add_argument("--metadata", action="store_true", help="include extra harmless smoke metadata")
    parser.add_argument("--request-id", default=os.environ.get("LITELLM_SMOKE_REQUEST_ID"))
    parser.add_argument("--trace-log", default=os.environ.get("LITELLM_SMOKE_TRACE_LOG", DEFAULT_TRACE_LOG))
    parser.add_argument("--trace-wait", type=float, default=float(os.environ.get("LITELLM_SMOKE_TRACE_WAIT", "1")))
    parser.add_argument("--dump", action="store_true", help="print the raw response/events after the summary")
    parser.add_argument(
        "--require-final",
        action="store_true",
        help="fail unless the HTTP response is 2xx and contains final text",
    )
    stream_group = parser.add_mutually_exclusive_group()
    stream_group.add_argument("--stream", dest="stream", action="store_true", default=True)
    stream_group.add_argument("--no-stream", dest="stream", action="store_false")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.request_id:
        args.request_id = f"smoke-websearch-{uuid.uuid4()}"
    if args.sleep_before > 0:
        time.sleep(args.sleep_before)

    url = responses_url(args.base_url)
    payload = build_payload(args)
    trace_cursor = route_trace_cursor(args.trace_log)
    status, headers, response_payload, events = request_responses(
        url=url,
        api_key=args.api_key,
        user_agent=args.user_agent,
        payload=payload,
        timeout=args.timeout,
    )
    if args.trace_wait > 0:
        time.sleep(args.trace_wait)
    all_trace_events = read_route_trace_events_since(args.trace_log, trace_cursor)
    trace_events, trace_match = select_route_trace_events(
        all_trace_events,
        request_id=args.request_id,
        query=args.query,
        model=args.model,
    )
    summary = summarize_payload(response_payload, events)
    route_trace_summary = summarize_route_trace(trace_events)
    bridge_observed = bool(
        summary["web_search_observed"]
        or route_trace_summary["external_web_search_observed"]
    )
    final_ok = 200 <= status < 300 and summary["has_final_text"]
    if bridge_observed and final_ok:
        verdict = "final_text_with_web_search"
    elif bridge_observed:
        verdict = "bridge_executed_but_final_failed"
    elif summary["raw_web_search_function_call_count"]:
        verdict = "raw_web_search_function_call_unresolved"
    elif 200 <= status < 300:
        verdict = "no_web_search_observed"
    else:
        verdict = "request_failed_before_web_search_observed"
    result = {
        "all_route_trace_event_count_after_offset": len(all_trace_events),
        "base_url": args.base_url,
        "bridge_observed": bridge_observed,
        "final_ok": final_ok,
        "http_status": status,
        "model": args.model,
        "request_id": args.request_id,
        "request_url": url,
        "route_trace": route_trace_summary,
        "route_trace_match": trace_match,
        "stream": args.stream,
        "summary": summary,
        "user_agent": args.user_agent,
        "verdict": verdict,
    }
    request_id = headers.get("x-request-id") or headers.get("x-litellm-request-id")
    if request_id:
        result["response_request_id"] = request_id
    if status >= 400 or status == 0:
        result["error_payload"] = response_payload

    print(compact_json(result))
    if args.dump:
        print("\nRAW_RESPONSE_OR_EVENTS")
        print(compact_json(events if events else response_payload))

    if args.require_final and not final_ok:
        return 1
    if bridge_observed:
        return 0
    if status < 200 or status >= 300:
        return 1
    if summary["raw_web_search_function_call_count"]:
        return 3
    if not summary["web_search_observed"]:
        return 4
    if not summary["has_final_text"]:
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

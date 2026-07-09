#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
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
DEFAULT_USER_AGENT = "codex-bridge-compare/1.0"
DEFAULT_TRACE_LOG = str(Path.home() / ".litellm-menu" / "menu-server.log")
EXPECTED_SUFFIX = "OK 2+3=5"


def responses_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/responses"
    return f"{base_url}/v1/responses"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def json_loads_maybe(value: bytes | str) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return value


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


def stream_text(events: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for event in events:
        event_type = event.get("type")
        if event_type in {
            "response.output_text.delta",
            "response.refusal.delta",
            "response.reasoning_summary_text.delta",
        }:
            delta = event.get("delta")
            if isinstance(delta, str):
                chunks.append(delta)
    return "".join(chunks).strip()


def completed_response_from_events(events: list[dict[str, Any]]) -> Any:
    for event in reversed(events):
        if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
            return event["response"]
    return None


def response_payload_for_text(payload: Any, events: list[dict[str, Any]]) -> Any:
    completed = completed_response_from_events(events)
    return completed if completed is not None else payload


def trace_log_offset(trace_log: str) -> int:
    if not trace_log:
        return 0
    try:
        return Path(trace_log).stat().st_size
    except OSError:
        return 0


def read_route_trace_events(trace_log: str, offset: int) -> list[dict[str, Any]]:
    if not trace_log:
        return []
    path = Path(trace_log)
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    prefix = "litellm_route_trace "
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        marker = line.find(prefix)
        if marker < 0:
            continue
        try:
            events.append(json.loads(line[marker + len(prefix) :].strip()))
        except Exception:
            continue
    return events


def request_ids_for_marker(events: list[dict[str, Any]], marker: str) -> list[str]:
    seen: set[str] = set()
    request_ids: list[str] = []
    for event in events:
        try:
            event_text = json.dumps(event, ensure_ascii=False)
        except Exception:
            event_text = str(event)
        if marker not in event_text:
            continue
        request_id = event.get("request_id")
        if isinstance(request_id, str) and request_id and request_id not in seen:
            seen.add(request_id)
            request_ids.append(request_id)
    return request_ids


def selected_trace_events(events: list[dict[str, Any]], marker: str) -> list[dict[str, Any]]:
    request_ids = set(request_ids_for_marker(events, marker))
    if request_ids:
        return [event for event in events if event.get("request_id") in request_ids]
    selected: list[dict[str, Any]] = []
    for event in events:
        try:
            event_text = json.dumps(event, ensure_ascii=False)
        except Exception:
            event_text = str(event)
        if marker in event_text:
            selected.append(event)
    return selected


def request_summary_from_event(event: dict[str, Any]) -> dict[str, Any]:
    request = event.get("retry_request") or event.get("request")
    return request if isinstance(request, dict) else {}


def trace_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    event_counts: dict[str, int] = {}
    effective_surfaces: list[str] = []
    route_keys: list[str] = []
    retry_tool_types: list[str] = []
    retry_tool_names: list[str] = []
    metadata_flags: dict[str, Any] = {}
    for event in events:
        name = event.get("event")
        if isinstance(name, str):
            event_counts[name] = event_counts.get(name, 0) + 1
        route_key = event.get("route_key")
        if not isinstance(route_key, str):
            deployment = event.get("deployment")
            if isinstance(deployment, dict):
                route_key = deployment.get("route_key")
        if isinstance(route_key, str) and route_key not in route_keys:
            route_keys.append(route_key)
        request = request_summary_from_event(event)
        interface = request.get("interface") if isinstance(request, dict) else {}
        if isinstance(interface, dict):
            surface = interface.get("effective_upstream_surface")
            if isinstance(surface, str) and surface not in effective_surfaces:
                effective_surfaces.append(surface)
        flags = request.get("metadata_flags") if isinstance(request, dict) else {}
        if isinstance(flags, dict):
            metadata_flags.update(flags)
        tools = request.get("tools") if isinstance(request, dict) else {}
        if isinstance(tools, dict):
            for tool_type in tools.get("types") or []:
                if isinstance(tool_type, str) and tool_type not in retry_tool_types:
                    retry_tool_types.append(tool_type)
            for tool_name in tools.get("names") or []:
                if isinstance(tool_name, str) and tool_name not in retry_tool_names:
                    retry_tool_names.append(tool_name)
        for tool_type in event.get("retry_tool_types") or []:
            if isinstance(tool_type, str) and tool_type not in retry_tool_types:
                retry_tool_types.append(tool_type)
        for tool_name in event.get("retry_tool_names") or []:
            if isinstance(tool_name, str) and tool_name not in retry_tool_names:
                retry_tool_names.append(tool_name)

    return {
        "event_count": len(events),
        "event_counts": event_counts,
        "effective_upstream_surfaces": effective_surfaces,
        "external_web_search_bridge_observed": bool(
            event_counts.get("responses_external_web_search_bridge_start")
            or metadata_flags.get("external_web_search_bridge") is True
        ),
        "function_tool_bridge_observed": bool(
            event_counts.get("responses_function_tool_bridge_start")
            or metadata_flags.get("responses_function_tool_bridge_attempted") is True
        ),
        "chat_bridge_observed": bool(
            event_counts.get("responses_chat_bridge_preemptive_start")
            or event_counts.get("responses_chat_bridge_retry_start")
            or metadata_flags.get("responses_chat_bridge_attempted") is True
        ),
        "route_recovery_observed": any(
            isinstance(name, str) and name.startswith("route_recovery")
            for name in event_counts
        ),
        "metadata_flags": metadata_flags,
        "retry_tool_names_tail": retry_tool_names[-12:],
        "retry_tool_types": retry_tool_types,
        "route_keys": route_keys[-8:],
    }


def codex_like_tools(*, include_web_search: bool) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": "exec_command",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
        {
            "type": "function",
            "name": "write_stdin",
            "description": "Write to a command session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "integer"},
                    "chars": {"type": "string"},
                },
                "required": ["session_id"],
            },
        },
        {
            "type": "custom",
            "name": "apply_patch",
            "description": "Apply a patch to workspace files.",
        },
        {
            "type": "namespace",
            "name": "mcp__node_repl",
            "tools": [
                {
                    "type": "function",
                    "name": "js",
                    "description": "Run JavaScript.",
                    "parameters": {
                        "type": "object",
                        "properties": {"code": {"type": "string"}},
                        "required": ["code"],
                    },
                },
                {
                    "type": "function",
                    "name": "js_reset",
                    "description": "Reset JavaScript REPL state.",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        },
        {
            "type": "namespace",
            "name": "codex_app",
            "tools": [
                {
                    "type": "function",
                    "name": "read_thread_terminal",
                    "description": "Read Codex terminal output.",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        },
        {"type": "tool_search"},
    ]
    if include_web_search:
        tools.append({"type": "web_search"})
    return tools


def build_payload(
    *,
    model: str,
    marker: str,
    variant: str,
    include_web_search: bool,
    max_output_tokens: int,
    stream: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "input": (
            f"Bridge comparison marker {marker}. Reply exactly "
            f"{marker}: {EXPECTED_SUFFIX}. Do not call tools."
        ),
        "stream": stream,
        "tools": codex_like_tools(include_web_search=include_web_search),
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "max_output_tokens": max_output_tokens,
        "metadata": {
            "bridge_compare_marker": marker,
            "bridge_compare_variant": variant,
        },
    }
    if variant == "responses_to_chat":
        payload["use_chat_completions_api"] = True
    return payload


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


def send_request(
    *,
    url: str,
    api_key: str,
    user_agent: str,
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, dict[str, str], Any, list[dict[str, Any]], float]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "accept": "text/event-stream" if payload.get("stream") else "application/json",
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "user-agent": user_agent,
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            if payload.get("stream"):
                events = read_sse_events(response)
                elapsed = time.perf_counter() - started
                completed = completed_response_from_events(events)
                payload_or_events = completed if completed is not None else {"events": events}
                return response.status, headers, payload_or_events, events, elapsed
            body = response.read()
            elapsed = time.perf_counter() - started
            return response.status, headers, json_loads_maybe(body), [], elapsed
    except urllib.error.HTTPError as exc:
        body = exc.read()
        elapsed = time.perf_counter() - started
        return (
            exc.code,
            {key.lower(): value for key, value in exc.headers.items()},
            json_loads_maybe(body),
            [],
            elapsed,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return (
            0,
            {},
            {"error": {"message": str(exc), "type": type(exc).__name__}},
            [],
            elapsed,
        )


def response_tool_calls(payload: Any, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in response_output_items(payload):
        item_type = item.get("type")
        if item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
            calls.append(item)
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
            calls.append(item)
    return calls


def run_once(args: argparse.Namespace, variant: str, index: int) -> dict[str, Any]:
    marker = f"BRIDGE_COMPARE_{variant.upper()}_{index}_{uuid.uuid4().hex[:8]}"
    payload = build_payload(
        model=args.model,
        marker=marker,
        variant=variant,
        include_web_search=args.include_web_search,
        max_output_tokens=args.max_output_tokens,
        stream=args.stream,
    )
    offset = trace_log_offset(args.trace_log)
    status, headers, response_payload, stream_events, elapsed = send_request(
        url=responses_url(args.base_url),
        api_key=args.api_key,
        user_agent=args.user_agent,
        payload=payload,
        timeout=args.timeout,
    )
    if args.trace_wait > 0:
        time.sleep(args.trace_wait)
    route_events = selected_trace_events(
        read_route_trace_events(args.trace_log, offset),
        marker,
    )
    payload_for_text = response_payload_for_text(response_payload, stream_events)
    final_text = response_text(payload_for_text) or stream_text(stream_events)
    expected_text = f"{marker}: {EXPECTED_SUFFIX}"
    tool_calls = response_tool_calls(response_payload, stream_events)
    route = trace_summary(route_events)
    stream_completed = completed_response_from_events(stream_events) is not None
    stream_failed = any(
        event.get("type") in {"response.failed", "response.incomplete"}
        for event in stream_events
    )
    accuracy_exact = final_text.strip() == expected_text
    accuracy_contains = marker in final_text and EXPECTED_SUFFIX in final_text
    protocol_ok = (
        200 <= status < 300
        and not tool_calls
        and not stream_failed
        and (not args.stream or stream_completed)
    )
    expected_surface = "chat" if variant == "responses_to_chat" else "responses"
    surface_ok = expected_surface in route["effective_upstream_surfaces"]
    bridge_ok = route["function_tool_bridge_observed"] or route["chat_bridge_observed"]
    if args.include_web_search:
        bridge_ok = bridge_ok and route["external_web_search_bridge_observed"]

    return {
        "accuracy_contains": accuracy_contains,
        "accuracy_exact": accuracy_exact,
        "bridge_ok": bridge_ok,
        "elapsed_ms": round(elapsed * 1000, 1),
        "error": response_payload.get("error") if isinstance(response_payload, dict) else None,
        "expected_surface": expected_surface,
        "expected_text": expected_text,
        "final_text": final_text,
        "final_text_preview": final_text[:300],
        "http_status": status,
        "marker": marker,
        "protocol_ok": protocol_ok,
        "response_id": response_payload.get("id") if isinstance(response_payload, dict) else None,
        "response_request_id": headers.get("x-request-id") or headers.get("x-litellm-request-id"),
        "route_trace": route,
        "stream": args.stream,
        "stream_completed": stream_completed,
        "stream_event_count": len(stream_events),
        "stream_event_types_tail": [
            event.get("type")
            for event in stream_events[-20:]
            if isinstance(event.get("type"), str)
        ],
        "stream_failed": stream_failed,
        "surface_ok": surface_ok,
        "tool_call_count": len(tool_calls),
        "tool_call_types": [item.get("type") for item in tool_calls],
        "variant": variant,
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [item["elapsed_ms"] for item in results if item.get("http_status")]
    return {
        "accuracy_contains": sum(1 for item in results if item["accuracy_contains"]),
        "accuracy_exact": sum(1 for item in results if item["accuracy_exact"]),
        "bridge_ok": sum(1 for item in results if item["bridge_ok"]),
        "elapsed_ms_median": round(statistics.median(elapsed), 1) if elapsed else None,
        "elapsed_ms_mean": round(statistics.mean(elapsed), 1) if elapsed else None,
        "http_2xx": sum(1 for item in results if 200 <= item["http_status"] < 300),
        "protocol_ok": sum(1 for item in results if item["protocol_ok"]),
        "route_recovery": sum(
            1 for item in results if item["route_trace"]["route_recovery_observed"]
        ),
        "runs": len(results),
        "status_counts": {
            str(status): sum(1 for item in results if item["http_status"] == status)
            for status in sorted({item["http_status"] for item in results})
        },
        "surface_ok": sum(1 for item in results if item["surface_ok"]),
    }


def markdown_report(result: dict[str, Any]) -> str:
    lines = [
        "# Responses Tool Bridge Compare",
        "",
        f"- model: `{result['model']}`",
        f"- base_url: `{result['base_url']}`",
        f"- runs per variant: `{result['runs_per_variant']}`",
        f"- include_web_search: `{result['include_web_search']}`",
        f"- stream: `{result['stream']}`",
        "",
        "## Summary",
        "",
        "| variant | 2xx | bridge | surface | exact | contains | protocol | recovery | median ms | statuses |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for variant in ("responses_to_responses", "responses_to_chat"):
        summary = result["summary"][variant]
        lines.append(
            "| {variant} | {http_2xx}/{runs} | {bridge_ok}/{runs} | {surface_ok}/{runs} | "
            "{accuracy_exact}/{runs} | {accuracy_contains}/{runs} | {protocol_ok}/{runs} | "
            "{route_recovery}/{runs} | {elapsed_ms_median} | `{status_counts}` |".format(
                variant=variant,
                **summary,
            )
        )
    lines.extend(["", "## Runs", ""])
    for variant in ("responses_to_responses", "responses_to_chat"):
        lines.append(f"### {variant}")
        for item in result["results"][variant]:
            trace = item["route_trace"]
            lines.append(
                "- status={status} elapsed_ms={elapsed} surface={surface} bridge={bridge} "
                "exact={exact} protocol={protocol} stream_events={stream_events} events={events} route={route}".format(
                    status=item["http_status"],
                    elapsed=item["elapsed_ms"],
                    surface=trace["effective_upstream_surfaces"],
                    bridge=item["bridge_ok"],
                    exact=item["accuracy_exact"],
                    protocol=item["protocol_ok"],
                    stream_events=item["stream_event_count"],
                    events=trace["event_counts"],
                    route=trace["route_keys"],
                )
            )
            if item.get("error"):
                lines.append(f"  error: `{json.dumps(item['error'], ensure_ascii=False)[:500]}`")
            elif item.get("final_text_preview"):
                lines.append(f"  text: `{item['final_text_preview']}`")
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare Codex-like Responses tool bridge behavior for responses->responses and responses->chat.",
    )
    parser.add_argument("--base-url", default=os.environ.get("LITELLM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=os.environ.get("LITELLM_API_KEY", DEFAULT_API_KEY))
    parser.add_argument("--model", default=os.environ.get("LITELLM_COMPARE_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--user-agent",
        default=(
            os.environ.get("CODEX_USER_AGENT")
            or os.environ.get("LITELLM_COMPARE_USER_AGENT")
            or DEFAULT_USER_AGENT
        ),
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--trace-log", default=os.environ.get("LITELLM_SMOKE_TRACE_LOG", DEFAULT_TRACE_LOG))
    parser.add_argument("--trace-wait", type=float, default=1.0)
    parser.add_argument("--max-output-tokens", type=int, default=64)
    parser.add_argument("--include-web-search", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--require-ok", action="store_true")
    stream_group = parser.add_mutually_exclusive_group()
    stream_group.add_argument("--stream", dest="stream", action="store_true", default=True)
    stream_group.add_argument("--no-stream", dest="stream", action="store_false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    results = {
        "responses_to_responses": [
            run_once(args, "responses_to_responses", index)
            for index in range(1, args.runs + 1)
        ],
        "responses_to_chat": [
            run_once(args, "responses_to_chat", index)
            for index in range(1, args.runs + 1)
        ],
    }
    result = {
        "base_url": args.base_url,
        "include_web_search": args.include_web_search,
        "model": args.model,
        "results": results,
        "runs_per_variant": args.runs,
        "stream": args.stream,
        "summary": {variant: aggregate(items) for variant, items in results.items()},
    }
    if args.format == "json":
        print(compact_json(result))
    else:
        print(markdown_report(result))
    if not args.require_ok:
        return 0
    for items in results.values():
        for item in items:
            if not (
                item["accuracy_contains"]
                and item["bridge_ok"]
                and item["protocol_ok"]
                and item["surface_ok"]
            ):
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
from typing import Any

from .common import esc, full_preview_attr, is_internal_context_text, recover_user_text_from_preview
from .deployments import bool_badge, deployment_pool, detail_row, pill, value_text

def request_details_html(details: dict[str, Any], tool_calls: dict[str, Any]) -> str:
    interface = details.get("interface") if isinstance(details.get("interface"), dict) else {}
    reasoning = details.get("reasoning") if isinstance(details.get("reasoning"), dict) else {}
    generation = details.get("generation") if isinstance(details.get("generation"), dict) else {}
    tools = details.get("tools") if isinstance(details.get("tools"), dict) else {}
    flags = details.get("metadata_flags") if isinstance(details.get("metadata_flags"), dict) else {}

    interface_rows = [
        detail_row("client surface", surface_label(interface.get("client_surface"))),
        detail_row("effective upstream", surface_label(interface.get("effective_upstream_surface"))),
        detail_row("endpoint", interface.get("requested_endpoint")),
        detail_row("call type", interface.get("call_type")),
        detail_row("method", interface.get("method_name")),
        detail_row("stream", interface.get("stream")),
        detail_row("api host", interface.get("api_base_host")),
        detail_row("upstream surface", interface.get("upstream_url_surface")),
        detail_row("supported surfaces", interface.get("supported_upstream_url_surfaces")),
    ]
    interface_caps = (
        '<div class="detail-pills">'
        f'{bool_badge("responses image", interface.get("supports_responses_image_input"))}'
        f'{bool_badge("hosted tools", interface.get("supports_responses_hosted_tools"))}'
        f'{bool_badge("client tools", interface.get("supports_responses_client_tools"))}'
        f'{bool_badge("web search", interface.get("supports_responses_web_search") or interface.get("supports_web_search"))}'
        "</div>"
    )

    reasoning_rows = [
        detail_row("present", reasoning.get("present")),
        detail_row("effort", reasoning.get("effort") or reasoning.get("reasoning_effort")),
        detail_row("text verbosity", reasoning.get("text_verbosity")),
        detail_row("config", reasoning.get("reasoning")),
    ]
    generation_rows = [
        detail_row(key, generation.get(key))
        for key in (
            "temperature",
            "top_p",
            "max_output_tokens",
            "max_tokens",
            "max_completion_tokens",
            "parallel_tool_calls",
            "response_format",
            "service_tier",
            "truncation",
            "seed",
        )
        if key in generation
    ]
    if not generation_rows:
        generation_rows = [detail_row("generation", "default")]

    exposed_tools = tools.get("exposed") if isinstance(tools.get("exposed"), list) else []
    exposed_html = "".join(
        pill(
            str(item.get("type") or "tool"),
            item.get("name") or item.get("namespace") or "",
            kind="tool",
        )
        for item in exposed_tools
        if isinstance(item, dict)
    )
    if not exposed_html:
        exposed_html = '<span class="empty">No exposed tools captured.</span>'
    tool_summary = (
        '<div class="detail-pills">'
        f'{pill("count", tools.get("count"), kind="tool")}'
        f'{pill("types", ", ".join(str(item) for item in tools.get("types") or []), kind="tool")}'
        f'{pill("names", ", ".join(str(item) for item in tools.get("names") or []), kind="tool")}'
        f'{bool_badge("hosted web", tools.get("has_web_search_tool"))}'
        f'{bool_badge("bridge web", tools.get("has_litellm_web_search_bridge"))}'
        f'{bool_badge("image tool", tools.get("has_image_generation_tool"))}'
        f'{bool_badge("image input", tools.get("has_image_input"))}'
        "</div>"
        + detail_row("tool choice", tools.get("tool_choice"))
        + detail_row("parallel calls", tools.get("parallel_tool_calls"))
        + f'<div class="tool-pills">{exposed_html}</div>'
    )

    actual_calls = tool_calls.get("calls") if isinstance(tool_calls.get("calls"), list) else []
    call_rows = []
    for call in actual_calls:
        if not isinstance(call, dict):
            continue
        label = call.get("name") or call.get("type") or "tool"
        meta = []
        for key in ("type", "status", "id", "call_id", "namespace"):
            if call.get(key) not in (None, ""):
                meta.append(f"{key}={value_text(call.get(key))}")
        detail = call.get("action") or call.get("arguments_preview")
        call_rows.append(
            '<div class="tool-call">'
            f'<span class="tool-call-name">{esc(label)}</span>'
            f'<span class="tool-call-meta">{esc(" | ".join(meta))}</span>'
            f'<span class="tool-call-args">{esc(value_text(detail))}</span>'
            "</div>"
        )
    actions = tool_calls.get("actions") if isinstance(tool_calls.get("actions"), list) else []
    action_rows = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_rows.append(
            '<div class="tool-call tool-action">'
            f'<span class="tool-call-name">{esc(action.get("type") or "action")}</span>'
            f'<span class="tool-call-args">{esc(value_text(action))}</span>'
            "</div>"
        )
    actual_html = (
        "".join(call_rows + action_rows)
        or '<span class="empty">No actual tool calls captured.</span>'
    )
    if tool_calls.get("truncated"):
        actual_html += '<span class="preview-status warning">tool calls truncated</span>'

    flag_html = "".join(
        pill(key, value, kind="flag")
        for key, value in sorted(flags.items())
    ) or '<span class="empty">No route metadata flags captured.</span>'

    return (
        '<section class="details-grid">'
        '<section class="detail-panel">'
        "<h4>Interface</h4>"
        f'{"".join(interface_rows)}{interface_caps}'
        "</section>"
        '<section class="detail-panel">'
        "<h4>Reasoning</h4>"
        f'{"".join(reasoning_rows)}'
        "</section>"
        '<section class="detail-panel">'
        "<h4>Generation</h4>"
        f'{"".join(generation_rows)}'
        "</section>"
        '<section class="detail-panel">'
        "<h4>Exposed Tools</h4>"
        f"{tool_summary}"
        "</section>"
        '<section class="detail-panel detail-panel-wide">'
        "<h4>Actual Tool Calls</h4>"
        f"{actual_html}"
        "</section>"
        '<section class="detail-panel detail-panel-wide">'
        "<h4>Route Flags</h4>"
        f'<div class="detail-pills">{flag_html}</div>'
        "</section>"
        "</section>"
    )


def selected_deployment(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        dep = event.get("deployment")
        if event.get("event") == "selected_deployment" and isinstance(dep, dict):
            return dep
    return None


def latest_filter(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event") == "filter_deployments":
            return event
    return None


def latest_request_preview(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        request = event.get("request")
        if isinstance(request, dict) and isinstance(request.get("preview"), dict):
            return request["preview"]
        preview = event.get("request_preview")
        if isinstance(preview, dict):
            return preview
    return {}


def latest_request_details(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        request = event.get("request")
        if isinstance(request, dict):
            return request
    return {}


def latest_session(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        session = event.get("session")
        if isinstance(session, dict) and (session.get("id") or session.get("name")):
            return session
    return {}


def request_preview_text(preview: dict[str, Any]) -> str:
    if not preview:
        return "No request preview in this trace. Newer trace events include a request preview when available."
    latest_user = preview.get("latest_user")
    if (
        isinstance(latest_user, str)
        and latest_user.strip()
        and not is_internal_context_text(latest_user)
    ):
        return latest_user.strip()
    recovered = recover_user_text_from_preview(preview.get("preview"))
    if recovered:
        return recovered
    text = preview.get("preview")
    if isinstance(text, str) and text.strip() and not is_internal_context_text(text):
        return text.strip()
    if isinstance(latest_user, str) and latest_user.strip():
        return "Internal context summary (not a user prompt)."
    return "Request preview was empty or non-text only."


def request_preview_note(preview: dict[str, Any]) -> str:
    if not preview:
        return ""
    latest_user = preview.get("latest_user")
    if isinstance(latest_user, str) and latest_user.strip() and is_internal_context_text(latest_user):
        if recover_user_text_from_preview(preview.get("preview")):
            return "internal context classified; recovered user text from preview"
        return "internal context classified"
    count = preview.get("internal_context_block_count")
    if isinstance(count, int) and count > 0:
        return f"internal context classified: {count}"
    return ""


def request_preview_truncated(preview: dict[str, Any]) -> bool | None:
    if not preview:
        return None
    latest_user = preview.get("latest_user")
    if isinstance(latest_user, str) and latest_user.strip():
        value = preview.get("latest_user_truncated")
        return bool(value) if isinstance(value, bool) else None
    text = preview.get("preview")
    if isinstance(text, str) and text.strip():
        value = preview.get("preview_truncated")
        return bool(value) if isinstance(value, bool) else None
    return None


def merge_unique_strings(target: list[str], value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            merge_unique_strings(target, item)
        return
    if value in (None, ""):
        return
    text = str(value)
    if text not in target:
        target.append(text)


def surface_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"openai/responses", "responses"}:
        return "responses"
    if lowered in {"openai/chat", "chat", "chat_completions"}:
        return "chat"
    if lowered == "anthropic":
        return "anthropic"
    return text


def interface_surface(interface: Any) -> str:
    if not isinstance(interface, dict):
        return ""
    client_surface = surface_label(interface.get("client_surface"))
    effective_surface = surface_label(
        interface.get("effective_upstream_surface")
        or interface.get("upstream_url_surface")
    )
    if client_surface and effective_surface and client_surface != effective_surface:
        return f"{client_surface} -> {effective_surface}"
    return effective_surface or client_surface


def event_request_interface(event: dict[str, Any], key: str) -> dict[str, Any]:
    request = event.get(key)
    if not isinstance(request, dict):
        return {}
    interface = request.get("interface")
    return interface if isinstance(interface, dict) else {}


def deployment_surface(dep: Any) -> str:
    if not isinstance(dep, dict):
        return ""
    return surface_label(
        dep.get("upstream_url_surface")
        or dep.get("mode")
    )


def event_surface_summary(event: dict[str, Any]) -> str:
    request_surface = interface_surface(event_request_interface(event, "request"))
    retry_surface = interface_surface(event_request_interface(event, "retry_request"))
    if request_surface and retry_surface and request_surface != retry_surface:
        return f"{request_surface} -> {retry_surface}"
    if request_surface and retry_surface:
        return retry_surface
    if retry_surface:
        return retry_surface
    if request_surface:
        return request_surface
    return deployment_surface(event.get("deployment")) or "other"


def interface_detail_text(label: str, interface: dict[str, Any]) -> str:
    if not interface:
        return ""
    parts = []
    client_surface = surface_label(interface.get("client_surface"))
    effective_surface = surface_label(interface.get("effective_upstream_surface"))
    endpoint = interface.get("requested_endpoint")
    call_type = interface.get("call_type")
    method = interface.get("method_name")
    upstream_surface = surface_label(interface.get("upstream_url_surface"))
    if client_surface:
        parts.append(f"client={client_surface}")
    if effective_surface:
        parts.append(f"upstream={effective_surface}")
    if endpoint:
        parts.append(f"endpoint={endpoint}")
    if call_type:
        parts.append(f"call={call_type}")
    if method:
        parts.append(f"method={method}")
    if upstream_surface and upstream_surface != effective_surface:
        parts.append(f"url_surface={upstream_surface}")
    return f"{label} " + " ".join(parts) if parts else ""


def event_surface_detail(event: dict[str, Any]) -> str:
    details = [
        interface_detail_text("request", event_request_interface(event, "request")),
        interface_detail_text("retry", event_request_interface(event, "retry_request")),
    ]
    dep_surface = deployment_surface(event.get("deployment"))
    if dep_surface:
        details.append(f"deployment upstream={dep_surface}")
    return " | ".join(detail for detail in details if detail)


def surface_chip(label: str, *, class_name: str = "surface-chip", title: str = "") -> str:
    if not label:
        return ""
    title_attr = f' title="{esc(title)}"' if title else ""
    return f'<span class="{esc(class_name)}"{title_attr}>{esc(label)}</span>'


def aggregate_request_details(events: list[dict[str, Any]], latest: dict[str, Any]) -> dict[str, Any]:
    interfaces: list[dict[str, Any]] = []
    reasoning: dict[str, Any] = {}
    generation: dict[str, Any] = {}
    tools: dict[str, Any] = {
        "count": 0,
        "types": [],
        "names": [],
        "exposed": [],
        "has_web_search_tool": False,
        "has_litellm_web_search_bridge": False,
        "has_image_generation_tool": False,
        "has_image_input": False,
    }
    metadata_flags: dict[str, Any] = {}

    for event in events:
        request = event.get("request")
        if not isinstance(request, dict):
            continue
        interface = request.get("interface")
        if isinstance(interface, dict):
            interfaces.append(interface)
        event_reasoning = request.get("reasoning")
        if isinstance(event_reasoning, dict) and event_reasoning:
            reasoning = event_reasoning
        event_generation = request.get("generation")
        if isinstance(event_generation, dict) and event_generation:
            generation = event_generation
        event_tools = request.get("tools")
        if isinstance(event_tools, dict):
            if isinstance(event_tools.get("count"), int):
                tools["count"] = max(int(tools["count"]), int(event_tools["count"]))
            for key in ("types", "names"):
                merge_unique_strings(tools[key], event_tools.get(key))
            exposed = event_tools.get("exposed")
            if isinstance(exposed, list):
                existing = {
                    json.dumps(item, ensure_ascii=False, sort_keys=True)
                    for item in tools["exposed"]
                    if isinstance(item, dict)
                }
                for item in exposed:
                    if not isinstance(item, dict):
                        continue
                    item_key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                    if item_key not in existing:
                        tools["exposed"].append(item)
                        existing.add(item_key)
            for key in (
                "has_web_search_tool",
                "has_litellm_web_search_bridge",
                "has_image_generation_tool",
                "has_image_input",
                "has_web_search_options",
            ):
                if event_tools.get(key) is True:
                    tools[key] = True
            if event_tools.get("tool_choice") is not None:
                tools["tool_choice"] = event_tools.get("tool_choice")
            if event_tools.get("parallel_tool_calls") is not None:
                tools["parallel_tool_calls"] = event_tools.get("parallel_tool_calls")
        flags = request.get("metadata_flags")
        if isinstance(flags, dict):
            metadata_flags.update(flags)

    latest_interface = latest.get("interface") if isinstance(latest.get("interface"), dict) else {}
    if isinstance(latest_interface, dict) and latest_interface:
        interfaces.append(latest_interface)
    return {
        "latest": latest,
        "interfaces": interfaces,
        "interface": interfaces[-1] if interfaces else {},
        "reasoning": reasoning,
        "generation": generation,
        "tools": tools,
        "metadata_flags": metadata_flags,
    }


def aggregate_tool_call_details(events: list[dict[str, Any]]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    types: list[str] = []
    names: list[str] = []
    actions: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    seen_actions: set[str] = set()
    truncated = False

    def add_call(call: Any) -> None:
        nonlocal truncated
        if not isinstance(call, dict):
            return
        key = json.dumps(call, ensure_ascii=False, sort_keys=True)
        if key in seen_calls:
            return
        seen_calls.add(key)
        if len(calls) >= 40:
            truncated = True
            return
        calls.append(call)
        merge_unique_strings(types, call.get("type"))
        merge_unique_strings(names, call.get("name"))

    def add_action(action: Any) -> None:
        if not isinstance(action, dict):
            return
        key = json.dumps(action, ensure_ascii=False, sort_keys=True)
        if key in seen_actions:
            return
        seen_actions.add(key)
        actions.append(action)

    for event in events:
        response = event.get("response")
        if isinstance(response, dict):
            tool_calls = response.get("tool_calls")
            if isinstance(tool_calls, dict):
                for call in tool_calls.get("calls") or []:
                    add_call(call)
                merge_unique_strings(types, tool_calls.get("types"))
                merge_unique_strings(names, tool_calls.get("names"))
                truncated = truncated or bool(tool_calls.get("truncated"))
            for action in response.get("web_search_actions") or []:
                add_action(action)
        event_tool_calls = event.get("tool_calls")
        if isinstance(event_tool_calls, list):
            for call in event_tool_calls:
                add_call(call)
        for action in event.get("actions") or []:
            add_action(action)

    return {
        "count": len(calls),
        "types": types,
        "names": names,
        "calls": calls,
        "actions": actions,
        "truncated": truncated,
    }

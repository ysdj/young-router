from __future__ import annotations

import json
from typing import Any

from .common import FALLBACK_EVENTS, compact_text, esc, event_label, event_time_label, time_range_label, duration_label
from .deployments import deployment_diagnostic_label, deployment_route_path, deployment_summary_label, event_class
from .details import (
    aggregate_request_details,
    aggregate_tool_call_details,
    deployment_surface,
    event_surface_summary,
    event_surface_detail,
    latest_filter,
    latest_request_details,
    latest_request_preview,
    latest_session,
    request_preview_note,
    request_preview_text,
    request_preview_truncated,
    selected_deployment,
    surface_chip,
)

def request_summary(request_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    selected = selected_deployment(events)
    filt = latest_filter(events)
    preview = latest_request_preview(events)
    request_details = latest_request_details(events)
    request_observability = aggregate_request_details(events, request_details)
    tool_calls = aggregate_tool_call_details(events)
    session = latest_session(events)
    fallback = any(str(event.get("event")) in FALLBACK_EVENTS for event in events)
    image = any(
        bool(event.get("has_image_generation_tool") or event.get("has_image_input"))
        for event in events
    )
    if not image:
        image = bool(request_observability.get("tools", {}).get("has_image_input")) or bool(
            request_observability.get("tools", {}).get("has_image_generation_tool")
        )
    model_group = next(
        (event.get("model_group") for event in events if event.get("model_group")),
        "",
    )
    return {
        "request_id": request_id,
        "events": len(events),
        "time_range": time_range_label(events),
        "duration": duration_label(events),
        "model_group": model_group,
        "selected": selected,
        "filter": filt,
        "session": session,
        "preview": preview,
        "request_details": request_observability,
        "tool_calls": tool_calls,
        "preview_text": request_preview_text(preview),
        "preview_note": request_preview_note(preview),
        "preview_truncated": request_preview_truncated(preview),
        "fallback": fallback,
        "image": image,
    }


def short_event_text(event: dict[str, Any]) -> str:
    name = str(event.get("event") or "")
    target_order = event.get("target_order")
    if name == "selected_deployment" and isinstance(event.get("deployment"), dict):
        dep = event["deployment"]
        return (
            f"{deployment_summary_label(dep)} via {dep.get('provider') or '?'} "
            f"(order {dep.get('order') if dep.get('order') is not None else '?'})"
        )
    if name == "filter_deployments":
        image_bits = []
        if event.get("has_image_generation_tool"):
            image_bits.append("image tool")
        if event.get("has_image_input"):
            image_bits.append("image input")
        image_text = ", ".join(image_bits) if image_bits else "text request"
        target_text = (
            f", target order {target_order}" if target_order not in (None, "") else ""
        )
        return (
            f"{len(event.get('selected_candidates') or [])} candidates from "
            f"{len(event.get('healthy') or [])} healthy deployments; {image_text}{target_text}"
        )
    if name == "generic_fallback_helper_start":
        target_text = (
            f"target order {target_order}" if target_order not in (None, "") else "normal order"
        )
        excluded = event.get("excluded_deployment_ids")
        excluded_count = len(excluded) if isinstance(excluded, list) else 0
        return f"{event.get('model_group') or 'model group'} using {target_text}; excluded {excluded_count}"
    if name == "deployment_failover_marked":
        exc = event.get("exception") if isinstance(event.get("exception"), dict) else {}
        reason = exc.get("reason") or exc.get("class") or exc.get("status_code") or "unknown"
        failed = event.get("route_key") or event.get("deployment_route_key") or event.get("deployment_id") or "?"
        token = event.get("deployment_id")
        token_text = f" (token {token})" if token and failed != token else ""
        return (
            f"{failed}{token_text} failed at order {event.get('deployment_order') or '?'}; reason {reason}"
        )
    if name == "fallback_target_order_constraint":
        return f"retrying only deployments at order {event.get('target_order') or '?'}"
    if name == "same_order_peer_fallback_available":
        candidates = event.get("candidates")
        count = len(candidates) if isinstance(candidates, list) else 0
        failed = event.get("failed_route_key") or event.get("failed_deployment_id") or "?"
        return (
            f"{count} peer candidate(s) available after "
            f"{failed} failed"
        )
    if name == "same_order_peer_fallback_unavailable":
        failed = event.get("failed_route_key") or event.get("failed_deployment_id") or "?"
        return f"no same-order peer available after {failed} failed"
    if name == "same_order_peer_fallback_start":
        return f"retrying another deployment at order {event.get('failed_order') or '?'}"
    if name == "streaming_error_fallback_start":
        return (
            f"retrying stream via {event.get('method') or '?'} "
            f"at order {event.get('target_order') or '?'}"
        )
    if name == "streaming_error_fallback_error":
        exc = event.get("exception") if isinstance(event.get("exception"), dict) else {}
        return f"streaming retry failed: {exc.get('class') or exc.get('text') or 'unknown error'}"
    if "exception" in event and isinstance(event["exception"], dict):
        exc = event["exception"]
        return f"{exc.get('class') or 'exception'} {exc.get('text') or ''}"
    if isinstance(event.get("retry_request"), dict):
        details = []
        retry_tool_types = event.get("retry_tool_types")
        retry_tool_names = event.get("retry_tool_names")
        if isinstance(retry_tool_types, list) and retry_tool_types:
            details.append("tools " + ", ".join(str(item) for item in retry_tool_types))
        if isinstance(retry_tool_names, list) and retry_tool_names:
            details.append("names " + ", ".join(str(item) for item in retry_tool_names))
        if event.get("preemptive_reason"):
            details.append(f"reason {event.get('preemptive_reason')}")
        return "; ".join(details)
    return ""


def timeline(events: list[dict[str, Any]]) -> str:
    rows = []
    for event in events:
        name = str(event.get("event") or "(event)")
        surface = event_surface_summary(event)
        surface_detail = event_surface_detail(event)
        raw_event = {key: value for key, value in event.items() if not key.startswith("_")}
        raw = json.dumps(raw_event, ensure_ascii=False, indent=2, sort_keys=True)
        rows.append(
            '<article class="timeline-row">'
            '<div class="event-timebox">'
            f'<span class="event-time">{esc(event_time_label(event))}</span>'
            f'<span class="seq">#{event.get("_seq")}</span>'
            "</div>"
            '<div class="timeline-body">'
            f'<div class="timeline-head"><span class="event-chip {event_class(name)}" title="{esc(name)}">{esc(event_label(name))}</span>'
            f'{surface_chip(surface, title=surface_detail)}'
            f'<span class="event-text">{esc(short_event_text(event))}</span></div>'
            f'<div class="event-raw-name">{esc(name)}</div>'
            f'<details><summary>Raw event JSON</summary><pre>{esc(raw)}</pre></details>'
            "</div></article>"
        )
    return '<section class="timeline">' + "".join(rows) + "</section>"


def deployment_route_label(dep: dict[str, Any]) -> str:
    token = deployment_diagnostic_label(dep)
    token_text = f" / token {token}" if token and token != "(token unavailable)" else ""
    return f"{deployment_route_path(dep)}{token_text}"


def route_chain_steps(events: list[dict[str, Any]], selected: Any) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(
        kind: str,
        title: str,
        detail: str,
        *,
        surface: str = "",
        surface_detail: str = "",
    ) -> None:
        title = title.strip()
        detail = detail.strip()
        surface = surface.strip()
        surface_detail = surface_detail.strip()
        key = (kind, title, detail, surface)
        if title and key not in seen:
            seen.add(key)
            steps.append(
                {
                    "kind": kind,
                    "title": title,
                    "detail": detail,
                    "surface": surface,
                    "surface_detail": surface_detail,
                }
            )

    for event in events:
        name = str(event.get("event") or "")
        surface = event_surface_summary(event)
        surface_detail = event_surface_detail(event)
        if name == "deployment_failover_marked":
            exc = event.get("exception") if isinstance(event.get("exception"), dict) else {}
            failed = (
                event.get("route_key")
                or event.get("deployment_route_key")
                or event.get("deployment_id")
                or exc.get("failed_deployment_id")
                or "?"
            )
            order = event.get("deployment_order") or exc.get("failed_deployment_order") or "?"
            reason = exc.get("reason") or exc.get("status_code") or exc.get("class") or "?"
            token = event.get("deployment_id") or exc.get("failed_deployment_id")
            token_text = f" / token {token}" if token and token != failed else ""
            add(
                "failed",
                "Failed upstream",
                f"{failed} / order {order}{token_text} / {reason}",
                surface=surface,
                surface_detail=surface_detail,
            )
        elif name == "fallback_target_order_constraint":
            add(
                "fallback",
                "Move to fallback order",
                f"try order {event.get('target_order') or '?'}",
                surface=surface,
                surface_detail=surface_detail,
            )
        elif name == "same_order_peer_fallback_available":
            candidates = event.get("candidates")
            count = len(candidates) if isinstance(candidates, list) else 0
            add(
                "fallback",
                "Same-order peer available",
                f"order {event.get('failed_order') or '?'}"
                + (f" / {count} candidate(s)" if count else ""),
                surface=surface,
                surface_detail=surface_detail,
            )
        elif name == "same_order_peer_fallback_unavailable":
            add(
                "failed",
                "No same-order peer",
                f"order {event.get('failed_order') or '?'}",
                surface=surface,
                surface_detail=surface_detail,
            )
        elif name == "same_order_peer_fallback_start":
            add(
                "fallback",
                "Try same-order fallback",
                f"order {event.get('failed_order') or '?'}",
                surface=surface,
                surface_detail=surface_detail,
            )
        elif name == "streaming_error_fallback_start":
            add(
                "fallback",
                "Streaming retry",
                f"{event.get('method') or '?'} / order {event.get('target_order') or '?'}",
                surface=surface,
                surface_detail=surface_detail,
            )
        elif name == "streaming_error_fallback_error":
            add(
                "failed",
                "Streaming retry failed",
                str(event.get("method") or "?"),
                surface=surface,
                surface_detail=surface_detail,
            )
        elif name == "filter_deployments":
            candidates = event.get("selected_candidates")
            healthy = event.get("healthy")
            candidate_count = len(candidates) if isinstance(candidates, list) else 0
            healthy_count = len(healthy) if isinstance(healthy, list) else 0
            target_order = event.get("target_order")
            detail = f"{candidate_count} candidate(s) from {healthy_count} healthy"
            if target_order not in (None, ""):
                detail += f" / target order {target_order}"
            add(
                "filter",
                "Filter candidate pool",
                detail,
                surface=surface,
                surface_detail=surface_detail,
            )
        elif name == "selected_deployment" and isinstance(event.get("deployment"), dict):
            dep = event["deployment"]
            add(
                "final",
                "Final selected",
                deployment_route_label(dep),
                surface=surface,
                surface_detail=surface_detail,
            )
        elif isinstance(event.get("retry_request"), dict):
            detail_parts = []
            retry_tool_types = event.get("retry_tool_types")
            retry_tool_names = event.get("retry_tool_names")
            if isinstance(retry_tool_types, list) and retry_tool_types:
                detail_parts.append("tools " + ", ".join(str(item) for item in retry_tool_types))
            if isinstance(retry_tool_names, list) and retry_tool_names:
                detail_parts.append("names " + ", ".join(str(item) for item in retry_tool_names))
            if event.get("preemptive_reason"):
                detail_parts.append(f"reason {event.get('preemptive_reason')}")
            add(
                "fallback",
                event_label(name),
                " / ".join(detail_parts) or "retry request",
                surface=surface,
                surface_detail=surface_detail,
            )

    if not any(step["kind"] == "final" for step in steps) and isinstance(selected, dict):
        surface = deployment_surface(selected) or "other"
        add(
            "final",
            "Final selected",
            deployment_route_label(selected),
            surface=surface,
            surface_detail=f"deployment upstream={surface}",
        )

    if not steps:
        for event in reversed(events):
            if event.get("event") != "filter_deployments":
                continue
            candidates = event.get("selected_candidates")
            if not isinstance(candidates, list) or not candidates:
                continue
            first = candidates[0]
            if isinstance(first, dict):
                surface = deployment_surface(first) or "other"
                add(
                    "filter",
                    "First candidate",
                    deployment_route_label(first),
                    surface=surface,
                    surface_detail=f"deployment upstream={surface}",
                )
            break

    return steps or [
        {
            "kind": "empty",
            "title": "No route chain captured",
            "detail": "",
            "surface": "other",
            "surface_detail": "",
        }
    ]


def route_chain_html(events: list[dict[str, Any]], selected: Any) -> str:
    steps = route_chain_steps(events, selected)
    final = next((step for step in reversed(steps) if step["kind"] == "final"), None)
    final_html = ""
    if final:
        final_html = (
            '<span class="chain-final-callout">'
            f'<span class="chain-final-label">Final</span>{esc(final["detail"])}'
            "</span>"
        )

    nodes = []
    for index, step in enumerate(steps, start=1):
        if index > 1:
            nodes.append('<span class="chain-arrow">→</span>')
        nodes.append(
            f'<span class="chain-node chain-{esc(step["kind"])}">'
            f'<span class="chain-index">{index}</span>'
            '<span class="chain-copy">'
            f'<span class="chain-title">{esc(step["title"])}'
            f'{surface_chip(step.get("surface", ""), class_name="chain-surface-chip", title=step.get("surface_detail", ""))}'
            "</span>"
            f'<span class="chain-detail">{esc(step["detail"])}</span>'
            "</span></span>"
        )

    return (
        '<div class="route-chain">'
        '<div class="chain-header">'
        '<span class="chain-label">Route chain</span>'
        f"{final_html}"
        "</div>"
        f'<div class="chain-flow">{"".join(nodes)}</div>'
        "</div>"
    )

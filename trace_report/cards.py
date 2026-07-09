from __future__ import annotations

import json
from typing import Any

from .common import compact_text, esc, full_preview_attr
from .deployments import (
    bool_badge,
    deployment_model_label,
    deployment_pill,
    deployment_pool,
    deployment_route_path,
    provider_class,
    value_text,
)
from .details import event_surface_detail, event_surface_summary, request_details_html, surface_chip, surface_label
from .summaries import request_summary, route_chain_html, route_chain_steps, timeline

def session_html(session: Any) -> str:
    if not isinstance(session, dict) or not (session.get("id") or session.get("name")):
        return ""
    parts = []
    if session.get("name"):
        parts.append(f'<span class="session-chip session-name">{esc(session.get("name"))}</span>')
    if session.get("id"):
        parts.append(f'<span class="session-chip session-id">{esc(session.get("id"))}</span>')
    return '<span class="session-group">' + "".join(parts) + "</span>"


def request_card(summary: dict[str, Any], events: list[dict[str, Any]]) -> str:
    request_id = str(summary["request_id"])
    selected = summary.get("selected")
    filt = summary.get("filter")
    session = summary.get("session")
    preview = summary.get("preview") if isinstance(summary.get("preview"), dict) else {}
    preview_text = str(summary.get("preview_text") or "")
    summary_preview_text = compact_text(preview_text)
    preview_truncated = summary.get("preview_truncated")
    preview_note = str(summary.get("preview_note") or "")
    request_details = (
        summary.get("request_details")
        if isinstance(summary.get("request_details"), dict)
        else {}
    )
    tool_calls = (
        summary.get("tool_calls") if isinstance(summary.get("tool_calls"), dict) else {}
    )
    interface = (
        request_details.get("interface")
        if isinstance(request_details.get("interface"), dict)
        else {}
    )
    reasoning = (
        request_details.get("reasoning")
        if isinstance(request_details.get("reasoning"), dict)
        else {}
    )
    tools = (
        request_details.get("tools")
        if isinstance(request_details.get("tools"), dict)
        else {}
    )
    effective_surface = surface_label(
        interface.get("effective_upstream_surface") or interface.get("client_surface")
    )
    reasoning_effort = str(
        reasoning.get("effort") or reasoning.get("reasoning_effort") or ""
    )
    exposed_tool_names = [
        str(item) for item in tools.get("names") or [] if str(item).strip()
    ]
    exposed_tool_types = [
        str(item) for item in tools.get("types") or [] if str(item).strip()
    ]
    actual_tool_names = [
        str(item) for item in tool_calls.get("names") or [] if str(item).strip()
    ]
    actual_tool_types = [
        str(item) for item in tool_calls.get("types") or [] if str(item).strip()
    ]
    time_range = str(summary.get("time_range") or "no timestamp")
    duration = str(summary.get("duration") or "")
    selected_id = selected.get("id") if isinstance(selected, dict) else None
    selected_text = "No deployment selected"
    selected_meta = ""
    selected_class = "provider-other"
    if isinstance(selected, dict):
        selected_text = deployment_route_path(selected)
        selected_meta = f"upstream {deployment_model_label(selected)}"
        deployment_id = selected.get("id")
        if deployment_id not in (None, ""):
            selected_meta += f" / token {deployment_id}"
        if selected.get("upstream_url_surface"):
            selected_meta += f" / surface {surface_label(selected.get('upstream_url_surface'))}"
        selected_class = provider_class(selected.get("provider"))

    classes = ["request-card"]
    if summary.get("fallback"):
        classes.append("has-fallback")
    if summary.get("image"):
        classes.append("has-image")

    search_text = " ".join(
        [
            request_id,
            str((session or {}).get("id") or "") if isinstance(session, dict) else "",
            str((session or {}).get("name") or "") if isinstance(session, dict) else "",
            time_range,
            duration,
            str(summary.get("model_group") or ""),
            preview_text,
            selected_text,
            selected_meta,
            effective_surface,
            reasoning_effort,
            " ".join(exposed_tool_names),
            " ".join(exposed_tool_types),
            " ".join(actual_tool_names),
            " ".join(actual_tool_types),
            value_text(request_details.get("metadata_flags")),
            value_text(tool_calls.get("actions")),
            " ".join(str(event.get("event") or "") for event in events),
            " ".join(event_surface_summary(event) for event in events),
            " ".join(event_surface_detail(event) for event in events),
            " ".join(
                str((event.get("deployment") or {}).get("api_base") or "")
                for event in events
                if isinstance(event.get("deployment"), dict)
            ),
        ]
    ).lower()

    flags = []
    flags.append('<span class="flag flag-fallback">fallback</span>' if summary.get("fallback") else '<span class="flag">no fallback</span>')
    flags.append('<span class="flag flag-image">image/vision</span>' if summary.get("image") else '<span class="flag">text route</span>')
    if effective_surface:
        flags.append(f'<span class="flag flag-surface">{esc(effective_surface)}</span>')
    if reasoning_effort:
        flags.append(f'<span class="flag flag-reasoning">reasoning {esc(reasoning_effort)}</span>')
    if exposed_tool_names or exposed_tool_types:
        label = ", ".join(exposed_tool_names or exposed_tool_types)
        flags.append(f'<span class="flag flag-tools">tools {esc(compact_text(label, limit=48))}</span>')
    if actual_tool_names or actual_tool_types:
        label = ", ".join(actual_tool_names or actual_tool_types)
        flags.append(f'<span class="flag flag-calls">called {esc(compact_text(label, limit=48))}</span>')

    route_html = ""
    if isinstance(filt, dict):
        filter_flags = (
            '<div class="filter-flags">'
            f'{bool_badge("image tool", filt.get("has_image_generation_tool"))}'
            f'{bool_badge("image input", filt.get("has_image_input"))}'
            f'{bool_badge("image filter", filt.get("image_generation_filtered"))}'
            f'{bool_badge("vision filter", filt.get("vision_filtered"))}'
            "</div>"
        )
        route_html = (
            '<details class="inner-details">'
            '<summary>Deployment pools and filters</summary>'
            '<div class="pool-strip">'
            f"{filter_flags}"
            f'{deployment_pool("Healthy", filt.get("healthy"), selected_id)}'
            f'{deployment_pool("After Constraints", filt.get("after_constraints"), selected_id)}'
            f'{deployment_pool("Selected Candidates", filt.get("selected_candidates"), selected_id)}'
            "</div></details>"
        )

    preview_meta = []
    if preview:
        if preview.get("source"):
            preview_meta.append(f"source={preview.get('source')}")
        if preview.get("message_count") is not None:
            preview_meta.append(f"messages={preview.get('message_count')}")
        if preview.get("text_block_count") is not None:
            preview_meta.append(f"text_blocks={preview.get('text_block_count')}")
        if preview.get("internal_context_block_count") is not None:
            preview_meta.append(f"internal_context={preview.get('internal_context_block_count')}")
        if preview.get("scan_direction") and preview.get("scan_item_limit") is not None:
            preview_meta.append(
                f"scan={preview.get('scan_direction')} {preview.get('scan_item_limit')}"
            )
        if preview.get("preview_limit") is not None:
            preview_meta.append(f"saved_chars={preview.get('preview_limit')}")
        tool_types = preview.get("tool_types")
        if isinstance(tool_types, list) and tool_types:
            preview_meta.append("tools=" + ",".join(str(item) for item in tool_types))
        if preview.get("tool_choice"):
            preview_meta.append(f"tool_choice={preview.get('tool_choice')}")

    preview_status = ""
    if preview_truncated is True:
        preview_status = '<span class="preview-status warning">saved preview truncated</span>'
    elif preview and preview_truncated is None:
        preview_status = '<span class="preview-status neutral">old trace: completeness unknown</span>'
    if preview_note:
        preview_status += (
            f'<span class="preview-status neutral">{esc(preview_note)}</span>'
        )

    duration_badge = (
        f'<span class="duration-badge">{esc(duration)}</span>' if duration else ""
    )
    preview_detail = (
        '<section class="preview-panel">'
        f'<h4>Request Preview {preview_status}</h4>'
        f'<p class="preview-main">{esc(preview_text)}</p>'
        f'<p class="preview-meta">{esc(" | ".join(preview_meta))}</p>'
        "</section>"
    )
    observability_detail = request_details_html(request_details, tool_calls)

    return (
        f'<details class="{" ".join(classes)}" data-search="{esc(search_text)}" '
        f'data-fallback="{str(bool(summary.get("fallback"))).lower()}" '
        f'data-image="{str(bool(summary.get("image"))).lower()}">'
        '<summary>'
        '<div class="summary-top">'
        '<div class="summary-left">'
        f'<span class="request-id">{esc(request_id)}</span>'
        f'<span class="time-badge">{esc(time_range)}</span>'
        f'{duration_badge}'
        f'<span class="model">{esc(summary.get("model_group") or "(unknown model)")}</span>'
        f'{session_html(session)}'
        "</div>"
        '<div class="summary-right">'
        f'<span class="selected-badge {selected_class}">{esc(selected_text)}</span>'
        f'<span class="selected-meta">{esc(selected_meta)}</span>'
        f'<span class="event-count">{summary["events"]} events</span>'
        f'{"".join(flags)}'
        "</div></div>"
        f"{route_chain_html(events, selected)}"
        f'<div class="summary-preview"{full_preview_attr(summary_preview_text, preview_text)}>'
        f'{esc(summary_preview_text)}</div>'
        "</summary>"
        f"{preview_detail}"
        f"{observability_detail}"
        f"{route_html}"
        '<details class="inner-details">'
        f'<summary>Timeline events ({len(events)})</summary>{timeline(events)}</details>'
        "</details>"
    )

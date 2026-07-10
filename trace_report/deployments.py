from __future__ import annotations

import collections
import json
from typing import Any

from .common import FALLBACK_EVENTS, esc

def provider_class(provider: Any) -> str:
    value = str(provider or "").lower()
    if "provider_alpha" in value:
        return "provider-provider_alpha"
    if "provider_beta" in value:
        return "provider-provider_beta"
    if "compat_provider" in value:
        return "provider-compat_provider"
    return "provider-other"


def event_class(name: str) -> str:
    if name in FALLBACK_EVENTS or "error" in name or "failover" in name:
        return "event-warning"
    if name == "selected_deployment":
        return "event-success"
    if name == "filter_deployments":
        return "event-info"
    return "event-neutral"


def bool_badge(label: str, value: Any) -> str:
    state = "yes" if value is True else "no" if value is False else "unknown"
    return f'<span class="cap cap-{state}">{esc(label)}: {esc(state)}</span>'


def deployment_model_label(dep: dict[str, Any]) -> str:
    return str(dep.get("model") or "(unknown upstream model)")


def deployment_surface_label(value: Any) -> str:
    text = str(value or "").strip()
    if text == "openai/responses":
        return "responses"
    if text == "openai/chat":
        return "chat"
    if text == "anthropic":
        return "anthropic"
    return text


def deployment_route_path(dep: dict[str, Any]) -> str:
    explicit = str(dep.get("route_key") or "").strip()
    if explicit:
        return explicit
    provider = str(dep.get("provider") or "unknown-provider")
    model = deployment_model_label(dep)
    parts = [provider, model]
    key_name = str(dep.get("api_key_name") or "").strip()
    if key_name:
        parts.append(f"key={key_name}")
    order = dep.get("order")
    if order not in (None, ""):
        parts.append(f"order={order}")
    return " / ".join(parts)


def deployment_diagnostic_label(dep: dict[str, Any]) -> str:
    return str(dep.get("id") or dep.get("token") or "(token unavailable)")


def deployment_summary_label(dep: dict[str, Any]) -> str:
    return deployment_route_path(dep)


def deployment_pill(dep: dict[str, Any], selected_id: str | None = None) -> str:
    dep_id = str(dep.get("id") or "")
    classes = ["deployment", provider_class(dep.get("provider"))]
    if selected_id and dep_id == selected_id:
        classes.append("selected")
    title = " | ".join(
        str(item)
        for item in (
            deployment_route_path(dep),
            f"token={deployment_diagnostic_label(dep)}",
            dep.get("api_base"),
        )
        if item not in (None, "")
    )
    meta_parts = []
    order = dep.get("order") if dep.get("order") is not None else "?"
    meta_parts.append(f"order={order}")
    dep_id = dep.get("id")
    if dep_id not in (None, ""):
        meta_parts.append(f"token={dep_id}")
    if dep.get("upstream_url_surface"):
        meta_parts.append(f"surface={deployment_surface_label(dep.get('upstream_url_surface'))}")
    caps = [
        bool_badge("vision", dep.get("supports_vision")),
        bool_badge("image tool", dep.get("supports_responses_image_generation_tool")),
        bool_badge("resp image", dep.get("supports_responses_image_input")),
        bool_badge("resp tools", dep.get("supports_responses_hosted_tools")),
        bool_badge("web", dep.get("supports_responses_web_search") or dep.get("supports_web_search")),
    ]
    return (
        f'<div class="{" ".join(classes)}" title="{esc(title)}">'
        f'<span class="dep-main">{esc(deployment_route_path(dep))}</span>'
        f'<span class="dep-meta">{esc(" ".join(meta_parts))}</span>'
        f'<span class="dep-caps">{"".join(caps)}</span>'
        "</div>"
    )


def deployment_pool(title: str, deployments: Any, selected_id: str | None = None) -> str:
    if not isinstance(deployments, list):
        deployments = []
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for dep in deployments:
        if isinstance(dep, dict):
            order = dep.get("order")
            grouped[str(order) if order is not None else "?"].append(dep)
    if not grouped:
        body = '<p class="empty">No deployments in this pool.</p>'
    else:
        order_blocks = []
        def order_key(item: tuple[str, list[dict[str, Any]]]) -> tuple[int, str]:
            key = item[0]
            return (int(key), key) if key.isdigit() else (9999, key)

        for order, deps in sorted(grouped.items(), key=order_key):
            pills = "".join(deployment_pill(dep, selected_id) for dep in deps)
            order_blocks.append(
                f'<div class="order-lane"><div class="order-label">order {esc(order)}</div>'
                f'<div class="deployments">{pills}</div></div>'
            )
        body = "".join(order_blocks)
    return (
        '<section class="pool">'
        f'<h4>{esc(title)} <span>{len(deployments)}</span></h4>'
        f"{body}</section>"
    )


def value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def detail_row(label: str, value: Any) -> str:
    text = value_text(value)
    if not text:
        text = "unknown"
    return (
        '<div class="detail-row">'
        f'<span class="detail-key">{esc(label)}</span>'
        f'<span class="detail-value">{esc(text)}</span>'
        "</div>"
    )


def pill(label: str, value: Any = None, *, kind: str = "neutral") -> str:
    text = label if value in (None, "") else f"{label}: {value_text(value)}"
    return f'<span class="info-pill info-{esc(kind)}">{esc(text)}</span>'

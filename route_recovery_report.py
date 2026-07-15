#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time
from typing import Any

from trace_report.assets import ROUTE_TRACE_CSS
from trace_report.cards import session_html
from trace_report.common import compact_text, esc, full_preview_attr
from trace_report.deployments import detail_row, value_text


RECOVERY_CSS = r"""
.thread-section-heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 14px;
  margin: 22px 0 9px;
}
.thread-section-heading h2 {
  margin: 0;
  color: #344055;
  font-size: 16px;
}
.thread-section-heading p {
  max-width: 780px;
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.section-count {
  flex: none;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
  color: #344055;
  padding: 2px 8px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.selected-badge.status-polling {
  background: var(--blue);
  border-color: var(--blue);
  color: #fff;
}
.selected-badge.status-cooldown {
  background: var(--amber);
  border-color: var(--amber);
  color: #fff;
}
.selected-badge.status-recent {
  background: #667085;
  border-color: #667085;
  color: #fff;
}
.flag-recovering { color: var(--blue); border-color: #b9d2f0; background: #eef6ff; }
.flag-cooldown { color: var(--amber); border-color: #e5c17b; background: #fff7e8; }
.flag-current { color: var(--blue); border-color: #b9d2f0; background: #eef6ff; }
.raw-record pre { margin: 0; }
.countdown-badge {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.request-card.countdown-expired .countdown-badge {
  color: var(--green);
}
[data-countdown-until] {
  font-variant-numeric: tabular-nums;
}
.recovery-stats {
  grid-template-columns: repeat(3, minmax(140px, 1fr));
}
.empty-state {
  padding: 16px 18px;
}
.empty-state h2 {
  margin: 0;
  color: #344055;
  font-size: 15px;
}
.empty-state p {
  margin: 4px 0 0;
  font-size: 13px;
}
.cooldown-card {
  border-color: #e5c17b;
  box-shadow: 0 1px 2px rgba(80, 55, 13, 0.04);
}
.cooldown-card-main {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(180px, auto);
  gap: 18px;
  align-items: center;
  padding: 16px 18px 14px;
}
.cooldown-identity {
  min-width: 0;
}
.cooldown-kicker {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 4px;
  color: #8a5200;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.cooldown-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #d97706;
  box-shadow: 0 0 0 3px #fff2d7;
}
.cooldown-card h3 {
  margin: 0;
  color: var(--text);
  font-size: 18px;
  overflow-wrap: anywhere;
}
.cooldown-subtitle {
  margin: 2px 0 0;
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.cooldown-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 9px;
}
.cooldown-chip {
  display: inline-flex;
  max-width: 100%;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #f8fafc;
  color: #344055;
  padding: 2px 8px;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.cooldown-chip b {
  margin-right: 4px;
  color: var(--muted);
  font-weight: 500;
}
.cooldown-timer {
  display: grid;
  justify-items: end;
  align-content: center;
  min-width: 180px;
  border-left: 1px solid #f0d7a3;
  padding-left: 18px;
}
.countdown-caption {
  color: #8a5200;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.03em;
  text-transform: uppercase;
}
.countdown-value {
  color: #8a5200;
  font-size: 28px;
  line-height: 1.15;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.countdown-until {
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}
.cooldown-reason {
  display: grid;
  grid-template-columns: 26px minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  margin: 0 18px 14px;
  border: 1px solid #f0d7a3;
  border-radius: 8px;
  background: #fff9ed;
  color: #6f4200;
  padding: 10px 12px;
}
.cooldown-reason-icon {
  display: inline-flex;
  width: 24px;
  height: 24px;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: #f6d58e;
  color: #6f4200;
  font-weight: 800;
}
.cooldown-reason-copy {
  display: grid;
  gap: 2px;
}
.cooldown-reason-copy strong {
  color: #6f4200;
  font-size: 13px;
  line-height: 1.35;
}
.cooldown-reason-copy span {
  color: #7c5a22;
  font-size: 12px;
}
.cooldown-technical-toggle {
  width: 100%;
  border: 0;
  border-top: 1px solid #f0d7a3;
  border-radius: 0;
  background: #fffdf8;
  color: #6f5a35;
  padding: 9px 18px;
  text-align: left;
  font-size: 12px;
}
.cooldown-technical-toggle:hover {
  background: #fff9ed;
}
.cooldown-chevron {
  display: inline-block;
  margin-left: 4px;
  transition: transform 120ms ease;
}
.cooldown-technical-toggle[aria-expanded="true"] .cooldown-chevron {
  transform: rotate(180deg);
}
.cooldown-technical {
  border-top: 1px solid #f0d7a3;
}
.cooldown-technical .details-grid {
  border-top: 0;
}
.cooldown-card.countdown-expired {
  border-color: #b9dfcc;
}
.cooldown-card.countdown-expired .cooldown-dot {
  background: var(--green);
  box-shadow: 0 0 0 3px #dff3e9;
}
.cooldown-card.countdown-expired .cooldown-kicker,
.cooldown-card.countdown-expired .countdown-caption,
.cooldown-card.countdown-expired .countdown-value {
  color: var(--green);
}
@media (max-width: 760px) {
  .recovery-stats { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
  .thread-section-heading { align-items: start; margin-top: 20px; }
  .cooldown-card-main { grid-template-columns: 1fr; gap: 12px; }
  .cooldown-timer {
    justify-items: start;
    min-width: 0;
    border-top: 1px solid #f0d7a3;
    border-left: 0;
    padding-top: 12px;
    padding-left: 0;
  }
  .cooldown-reason-copy strong { font-weight: 600; }
}
"""

RECOVERY_JS = r"""
const cards = Array.from(document.querySelectorAll('.request-card'));
const search = document.getElementById('search');
const buttons = Array.from(document.querySelectorAll('button[data-filter]'));
const countdownNodes = Array.from(document.querySelectorAll('.countdown-badge[data-countdown-until]'));
const technicalToggles = Array.from(document.querySelectorAll('[data-expand-toggle]'));
const generatedAt = Number(document.body.dataset.generatedAt || Date.now() / 1000);
const clockSkewMs = Date.now() - (generatedAt * 1000);
let activeFilter = 'all';

function applyFilters() {
  const q = (search.value || '').trim().toLowerCase();
  for (const card of cards) {
    const textMatch = !q || (card.dataset.search || '').includes(q);
    const filterMatch =
      activeFilter === 'all' ||
      card.dataset.source === activeFilter ||
      card.dataset.status === activeFilter;
    card.classList.toggle('hidden', !(textMatch && filterMatch));
  }
}

function durationLabel(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 1) return `${Math.floor(value * 1000)} ms`;
  if (value < 60) return `${value.toFixed(1)} s`;
  const minutes = Math.floor(value / 60);
  const remainder = Math.floor(value % 60);
  if (minutes < 60) return `${minutes}m ${remainder}s`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return `${hours}h ${mins}m`;
}

function nowEpoch() {
  return (Date.now() - clockSkewMs) / 1000;
}

function tickCountdowns() {
  const now = nowEpoch();
  for (const node of countdownNodes) {
    const until = Number(node.dataset.countdownUntil || 0);
    if (!Number.isFinite(until) || until <= 0) continue;
    const remaining = Math.max(0, until - now);
    const label = remaining > 0 ? durationLabel(remaining) : 'Ready now';
    node.textContent = label;
    const card = node.closest('.request-card');
    if (card) {
      card.dataset.remainingSeconds = remaining.toFixed(3);
      if (remaining <= 0) {
        card.classList.add('countdown-expired');
        const caption = card.querySelector('.countdown-caption');
        const status = card.querySelector('.cooldown-status-label');
        if (caption) caption.textContent = 'Cooldown ended';
        if (status) status.textContent = 'Ready for routing';
      }
    }
  }
}

function setExpanded(toggle, expanded) {
  const panelId = toggle.getAttribute('aria-controls');
  const panel = panelId ? document.getElementById(panelId) : null;
  if (!panel) return;
  toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  panel.hidden = !expanded;
  const label = toggle.querySelector('[data-expand-label]');
  if (label) label.textContent = expanded ? 'Hide technical details' : 'Show technical details';
}

search.addEventListener('input', applyFilters);
for (const button of buttons) {
  button.addEventListener('click', () => {
    activeFilter = button.dataset.filter;
    for (const item of buttons) item.classList.toggle('active', item === button);
    applyFilters();
  });
}
for (const toggle of technicalToggles) {
  toggle.addEventListener('click', () => {
    setExpanded(toggle, toggle.getAttribute('aria-expanded') !== 'true');
  });
}
document.getElementById('expand').addEventListener('click', () => {
  cards.forEach(card => {
    if (card.tagName === 'DETAILS') card.open = true;
  });
  technicalToggles.forEach(toggle => setExpanded(toggle, true));
});
document.getElementById('collapse').addEventListener('click', () => {
  cards.forEach(card => {
    if (card.tagName === 'DETAILS') card.open = false;
  });
  technicalToggles.forEach(toggle => setExpanded(toggle, false));
});
tickCountdowns();
if (countdownNodes.length) {
  setInterval(tickCountdowns, 250);
}
"""


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def iso_from_epoch(value: Any) -> str:
    parsed = number(value)
    if parsed is None or parsed <= 0:
        return ""
    return dt.datetime.fromtimestamp(parsed, tz=dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def duration_label(seconds: Any) -> str:
    parsed = number(seconds)
    if parsed is None:
        return "-"
    parsed = max(0.0, parsed)
    if parsed < 1:
        return f"{int(parsed * 1000)} ms"
    if parsed < 60:
        return f"{parsed:.1f} s"
    minutes, remainder = divmod(parsed, 60)
    if minutes < 60:
        return f"{int(minutes)}m {int(remainder)}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m"


def countdown_attr(until: Any) -> str:
    parsed = number(until)
    if parsed is None or parsed <= 0:
        return ""
    return f' data-countdown-until="{esc(f"{parsed:.3f}")}"'


def countdown_text(record: dict[str, Any], *, fallback: str = "-") -> str:
    remaining = number(record.get("remaining_poll_seconds"))
    if remaining is None:
        remaining = number(record.get("remaining_seconds"))
    if remaining is None:
        return fallback
    return duration_label(remaining)


def countdown_detail_row(label: str, record: dict[str, Any]) -> str:
    value = countdown_text(record)
    until = number(record.get("cooldown_until"))
    attrs = countdown_attr(until) if until is not None and until > 0 else ""
    return (
        '<div class="detail-row">'
        f'<span class="detail-key">{esc(label)}</span>'
        f'<span class="detail-value countdown-badge"{attrs}>{esc(value)}</span>'
        '</div>'
    )


def cooldown_ends_at_row(record: dict[str, Any]) -> str:
    if record.get("source") != "cooldown":
        return ""
    ends_at = iso_from_epoch(record.get("cooldown_until"))
    if not ends_at:
        return ""
    return detail_row("ends at", ends_at)


def route_key_fields(value: Any) -> dict[str, str]:
    if not isinstance(value, str):
        return {}
    fields: dict[str, str] = {}
    for part in value.split(" / "):
        key, separator, raw_value = part.partition("=")
        if not separator:
            continue
        key = key.strip()
        raw_value = raw_value.strip()
        if key and raw_value:
            fields[key] = raw_value
    return fields


def cooldown_surface(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    marker = "|surface:"
    if marker not in value:
        return ""
    return value.rsplit(marker, 1)[1].strip()


def surface_label(value: Any) -> str:
    raw = str(value or "").strip().lower()
    labels = {
        "openai/responses": "OpenAI Responses",
        "openai/chat": "OpenAI Chat Completions",
        "anthropic": "Anthropic Messages",
        "responses": "Responses",
        "chat": "Chat Completions",
    }
    if raw in labels:
        return labels[raw]
    if not raw:
        return "All configured protocols"
    return raw.replace("_", " ").replace("/", " / ").title()


def load_json(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_recent_stuck(path: str, *, limit: int = 80) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        if len(rows) >= limit:
            break
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("status") == "stuck":
            rows.append(row)
    return rows


def active_cooldowns(payload: dict[str, Any], *, now: float) -> list[dict[str, Any]]:
    cooldowns = payload.get("cooldowns")
    if not isinstance(cooldowns, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key, raw_state in cooldowns.items():
        if not isinstance(raw_state, dict):
            continue
        until = number(raw_state.get("cooldown_until")) or 0.0
        if until <= now:
            continue
        row = dict(raw_state)
        row["cooldown_key"] = key
        row["remaining_seconds"] = max(0.0, until - now)
        rows.append(row)
    rows.sort(key=lambda item: number(item.get("remaining_seconds")) or 0.0, reverse=True)
    return rows


def active_recoveries(payload: dict[str, Any], *, now: float) -> list[dict[str, Any]]:
    recoveries = payload.get("recoveries")
    if not isinstance(recoveries, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key, raw_state in recoveries.items():
        if not isinstance(raw_state, dict):
            continue
        pid = raw_state.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
            except OSError:
                continue
        row = dict(raw_state)
        row.setdefault("key", key)
        rows.append(row)
    rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return rows


def summary_counts(recoveries: list[dict[str, Any]], cooldowns: list[dict[str, Any]]) -> tuple[int, int]:
    return len(recoveries), len(cooldowns)


def session_label(session: Any) -> str:
    if not isinstance(session, dict):
        return "-"
    return str(session.get("name") or session.get("id") or "-")


def exception_label(exception: Any) -> str:
    if not isinstance(exception, dict):
        return "-"
    parts = []
    for key in ("type", "status_code", "reason", "code", "message"):
        value = exception.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value_text(value)}")
    return " | ".join(parts) if parts else "-"


def request_preview(request: Any) -> str:
    if not isinstance(request, dict):
        return ""
    preview = request.get("preview")
    if isinstance(preview, dict):
        for key in ("latest_user", "text", "summary", "preview"):
            value = preview.get(key)
            if value:
                return str(value)
    return ""


def recent_thread_record(row: dict[str, Any]) -> dict[str, Any]:
    stuck = row.get("stuck") if isinstance(row.get("stuck"), dict) else {}
    error = row.get("error") if isinstance(row.get("error"), dict) else {}
    return {
        "source": "recent",
        "status": "recent",
        "key": row.get("request_id") or row.get("route_key") or row.get("ts"),
        "request_id": row.get("request_id"),
        "session": row.get("session"),
        "model_group": row.get("model_group"),
        "provider": row.get("provider"),
        "upstream_model": row.get("upstream_model"),
        "api_base_host": row.get("api_base_host"),
        "route_key": row.get("route_key"),
        "deployment_id": row.get("deployment_id"),
        "deployment_order": row.get("deployment_order"),
        "stuck": stuck,
        "exception": error,
        "updated_at": row.get("ts"),
        "raw": row,
    }


def current_thread_record(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "source": "current", "raw": row}


def cooldown_thread_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "cooldown",
        "status": "cooldown",
        "key": row.get("cooldown_key"),
        "request_id": row.get("cooldown_key"),
        "model_group": row.get("model_group") or "deployment cooldown",
        "route_key": row.get("route_key"),
        "deployment_id": row.get("deployment_id"),
        "provider": row.get("provider"),
        "upstream_model": row.get("upstream_model"),
        "deployment_order": row.get("deployment_order"),
        "attempt": row.get("failures"),
        "remaining_poll_seconds": row.get("remaining_seconds"),
        "cooldown_until": row.get("cooldown_until"),
        "updated_at": iso_from_epoch(row.get("cooldown_until")) or row.get("updated_at"),
        "stuck": {"reason": "deployment_cooldown"},
        "exception": {"type": "deployment_cooldown", "reason": row.get("cooldown_key")},
        "raw": row,
    }


def cooldown_card(row: dict[str, Any], *, index: int) -> str:
    route_fields = route_key_fields(row.get("route_key"))
    deployment_id = row.get("deployment_id")
    model_group = row.get("model_group") or route_fields.get("model")
    provider = row.get("provider") or route_fields.get("provider")
    upstream_model = row.get("upstream_model") or route_fields.get("upstream")
    api_host = row.get("api_base_host") or route_fields.get("host")
    deployment_order = row.get("deployment_order") or route_fields.get("order")
    raw_surface = cooldown_surface(row.get("cooldown_key"))
    protocol = surface_label(raw_surface)
    failures_value = number(row.get("failures"))
    failures = int(failures_value) if failures_value is not None else 0
    until = number(row.get("cooldown_until"))
    remaining = number(row.get("remaining_seconds")) or 0.0
    ends_at = iso_from_epoch(until)
    last_failure_at = iso_from_epoch(row.get("last_failure_at")) or "-"

    title = str(model_group or upstream_model or deployment_id or "Deployment route")
    subtitle_parts = [str(value) for value in (provider, upstream_model) if value]
    subtitle = " · ".join(dict.fromkeys(subtitle_parts))
    failure_noun = "failure" if failures == 1 else "failures"
    if raw_surface:
        impact = (
            f"Only {protocol} on this deployment is paused. "
            "Other configured protocols remain eligible for routing."
        )
    else:
        impact = (
            "This deployment is skipped until the timer ends. "
            "Other matching deployments remain eligible for routing."
        )
    reason = (
        f"LiteLLM recorded {failures} consecutive upstream {failure_noun} and "
        "temporarily stopped sending new requests to this route."
    )
    chips = [
        f'<span class="cooldown-chip"><b>Protocol</b>{esc(protocol)}</span>',
        f'<span class="cooldown-chip"><b>Failures</b>{esc(failures)}</span>',
    ]
    if provider:
        chips.insert(1, f'<span class="cooldown-chip"><b>Provider</b>{esc(provider)}</span>')
    details_id = f"cooldown-details-{index}"
    search_payload = {
        **row,
        "protocol": protocol,
        "model_group": model_group,
        "provider": provider,
        "upstream_model": upstream_model,
        "api_base_host": api_host,
    }
    search_text = json.dumps(search_payload, ensure_ascii=False, sort_keys=True, default=str).lower()
    countdown_attrs = countdown_attr(until)
    card_attrs = (
        f'data-search="{esc(search_text)}" data-source="cooldown" '
        f'data-status="cooldown" data-remaining-seconds="{esc(f"{remaining:.3f}")}"'
    )
    return (
        f'<article class="request-card cooldown-card" {card_attrs}>'
        '<div class="cooldown-card-main">'
        '<div class="cooldown-identity">'
        '<div class="cooldown-kicker"><span class="cooldown-dot"></span>'
        '<span class="cooldown-status-label">Temporarily paused</span></div>'
        f'<h3>{esc(title)}</h3>'
        f'<p class="cooldown-subtitle">{esc(subtitle or deployment_id or "Route details unavailable")}</p>'
        f'<div class="cooldown-meta">{"".join(chips)}</div>'
        '</div>'
        '<div class="cooldown-timer">'
        '<span class="countdown-caption">Routing resumes in</span>'
        f'<strong class="countdown-value countdown-badge"{countdown_attrs}>{esc(duration_label(remaining))}</strong>'
        f'<span class="countdown-until">{esc("at " + ends_at if ends_at else "automatically")}</span>'
        '</div>'
        '</div>'
        '<div class="cooldown-reason">'
        '<span class="cooldown-reason-icon">!</span>'
        '<span class="cooldown-reason-copy">'
        f'<strong>{esc(reason)}</strong>'
        f'<span>{esc(impact)}</span>'
        '</span>'
        '</div>'
        f'<button class="cooldown-technical-toggle" type="button" data-expand-toggle '
        f'aria-expanded="false" aria-controls="{esc(details_id)}">'
        '<span data-expand-label>Show technical details</span>'
        '<span class="cooldown-chevron" aria-hidden="true">⌄</span>'
        '</button>'
        f'<section class="cooldown-technical" id="{esc(details_id)}" hidden>'
        '<section class="details-grid">'
        '<section class="detail-panel">'
        '<h4>Pause</h4>'
        f'{detail_row("status", "Temporarily skipped")}'
        f'{detail_row("failure count", failures)}'
        f'{detail_row("last failure", last_failure_at)}'
        f'{countdown_detail_row("resumes in", row)}'
        f'{detail_row("resumes at", ends_at)}'
        f'{detail_row("protocol", protocol)}'
        '</section>'
        '<section class="detail-panel">'
        '<h4>Affected Route</h4>'
        f'{detail_row("model group", model_group)}'
        f'{detail_row("provider", provider)}'
        f'{detail_row("upstream model", upstream_model)}'
        f'{detail_row("api host", api_host)}'
        f'{detail_row("deployment id", deployment_id)}'
        f'{detail_row("deployment order", deployment_order)}'
        f'{detail_row("route key", row.get("route_key"))}'
        '</section>'
        '<section class="detail-panel detail-panel-wide raw-record">'
        '<h4>Raw Record</h4>'
        f'<pre>{esc(json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True, default=str))}</pre>'
        '</section>'
        '</section>'
        '</section>'
        '</article>'
    )


def thread_preview_text(record: dict[str, Any]) -> str:
    preview = request_preview(record.get("request"))
    if preview:
        return preview
    stuck = record.get("stuck") if isinstance(record.get("stuck"), dict) else {}
    reason = stuck.get("reason") or exception_label(record.get("exception"))
    return str(reason or "No request preview captured for this thread.")


def record_time_label(record: dict[str, Any]) -> str:
    for key in ("updated_at", "started_at"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value.replace("T", " ").replace("Z", "")[:19]
    return "no timestamp"


def thread_detail_panels(record: dict[str, Any]) -> str:
    session = record.get("session") if isinstance(record.get("session"), dict) else {}
    stuck = record.get("stuck") if isinstance(record.get("stuck"), dict) else {}
    exception = record.get("exception") if isinstance(record.get("exception"), dict) else {}
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else record
    return (
        '<section class="details-grid">'
        '<section class="detail-panel">'
        '<h4>Thread</h4>'
        f'{detail_row("session name", session.get("name"))}'
        f'{detail_row("session id", session.get("id"))}'
        f'{detail_row("request id", record.get("request_id"))}'
        f'{detail_row("state key", record.get("key"))}'
        f'{detail_row("source", record.get("source"))}'
        '</section>'
        '<section class="detail-panel">'
        '<h4>Recovery State</h4>'
        f'{detail_row("status", record.get("status"))}'
        f'{detail_row("attempt", record.get("attempt"))}'
        f'{detail_row("elapsed", duration_label(record.get("elapsed_seconds")))}'
        f'{countdown_detail_row("remaining", record)}'
        f'{cooldown_ends_at_row(record)}'
        f'{detail_row("max poll", duration_label(record.get("max_poll_seconds")))}'
        f'{detail_row("poll interval", duration_label(record.get("poll_interval_seconds")))}'
        f'{detail_row("target order", record.get("target_order"))}'
        f'{detail_row("started", record.get("started_at"))}'
        f'{detail_row("updated", record.get("updated_at"))}'
        '</section>'
        '<section class="detail-panel">'
        '<h4>Route</h4>'
        f'{detail_row("model group", record.get("model_group"))}'
        f'{detail_row("provider", record.get("provider"))}'
        f'{detail_row("upstream model", record.get("upstream_model"))}'
        f'{detail_row("api host", record.get("api_base_host"))}'
        f'{detail_row("deployment id", record.get("deployment_id"))}'
        f'{detail_row("deployment order", record.get("deployment_order"))}'
        f'{detail_row("route key", record.get("route_key"))}'
        '</section>'
        '<section class="detail-panel">'
        '<h4>Failure</h4>'
        f'{detail_row("exception", exception_label(exception))}'
        f'{detail_row("recovery reason", stuck.get("reason"))}'
        f'{detail_row("idle timeout", duration_label(stuck.get("stream_idle_timeout_seconds")))}'
        f'{detail_row("start timeout", duration_label(stuck.get("stream_start_timeout_seconds")))}'
        f'{detail_row("saw chunk", stuck.get("stream_saw_chunk"))}'
        f'{detail_row("buffered chunks", stuck.get("stream_buffered_chunks"))}'
        '</section>'
        '<section class="detail-panel detail-panel-wide raw-record">'
        '<h4>Raw Record</h4>'
        f'<pre>{esc(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True, default=str))}</pre>'
        '</section>'
        '</section>'
    )


def thread_card(record: dict[str, Any]) -> str:
    source = str(record.get("source") or "current")
    status = str(record.get("status") or "polling").lower()
    session = record.get("session") if isinstance(record.get("session"), dict) else {}
    request_id = str(record.get("request_id") or record.get("key") or "missing request id")
    model = record.get("model_group") or "(unknown model)"
    title = session_label(session)
    preview = thread_preview_text(record)
    summary_preview = compact_text(preview, limit=260)
    if source == "recent":
        status_label = "recovery timeout"
    elif status == "polling":
        status_label = "recovering"
    else:
        status_label = status
    status_class = "recent" if source == "recent" else status
    if source == "current":
        source_flag = '<span class="flag flag-current">current</span>'
    elif source == "cooldown":
        source_flag = '<span class="flag flag-cooldown">deployment cooldown</span>'
    else:
        source_flag = '<span class="flag">recent</span>'
    status_flag_class = "flag-cooldown" if status == "cooldown" else "flag-recovering"
    flags = (
        f'{source_flag}'
        f'<span class="flag {status_flag_class}">{esc(status_label)}</span>'
        f'<span class="flag flag-surface">{esc(record.get("model_group") or "unknown model")}</span>'
    )
    search_text = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str).lower()
    meta = []
    for key in ("provider", "upstream_model", "api_base_host", "route_key"):
        if record.get(key):
            meta.append(f"{key}={record.get(key)}")
    cooldown_until = number(record.get("cooldown_until"))
    countdown_attrs = countdown_attr(cooldown_until)
    remaining_label = countdown_text(record, fallback="")
    if source == "cooldown" and remaining_label:
        duration_html = (
            f'<span class="duration-badge countdown-badge"{countdown_attrs}>'
            f'{esc(remaining_label)}</span>'
        )
    else:
        duration_html = (
            f'<span class="duration-badge">{esc(duration_label(record.get("elapsed_seconds")))}</span>'
        )
    card_attrs = (
        f'data-search="{esc(search_text)}" '
        f'data-source="{esc(source)}" data-status="{esc(status)}" '
        f'data-fallback="false" data-image="false"'
    )
    if source == "cooldown" and cooldown_until is not None and cooldown_until > 0:
        remaining_value = number(record.get("remaining_poll_seconds")) or 0.0
        remaining_text = f"{remaining_value:.3f}"
        card_attrs += f' data-remaining-seconds="{esc(remaining_text)}"'
    return (
        f'<details class="request-card" {card_attrs}>'
        '<summary>'
        '<div class="summary-top">'
        '<div class="summary-left">'
        f'<span class="request-id">{esc(request_id)}</span>'
        f'<span class="time-badge">{esc(record_time_label(record))}</span>'
        f'{duration_html}'
        f'<span class="model">{esc(model)}</span>'
        f'{session_html(session)}'
        '</div>'
        '<div class="summary-right">'
        f'<span class="selected-badge status-{esc(status_class)}">{esc(status_label)}</span>'
        f'<span class="selected-meta">{esc(" / ".join(meta))}</span>'
        f'{flags}'
        '</div></div>'
        f'<div class="summary-preview"{full_preview_attr(summary_preview, preview)}>{esc(summary_preview)}</div>'
        '</summary>'
        '<section class="preview-panel">'
        '<h4>Thread Preview</h4>'
        f'<p class="preview-main">{esc(preview)}</p>'
        f'<p class="preview-meta">{esc(exception_label(record.get("exception")))}</p>'
        '</section>'
        f'{thread_detail_panels(record)}'
        '</details>'
    )


def empty_state(title: str, message: str) -> str:
    return f'<section class="empty-state"><h2>{esc(title)}</h2><p>{esc(message)}</p></section>'


def section_heading(title: str, message: str, count: int) -> str:
    return (
        '<div class="thread-section-heading">'
        '<div>'
        f'<h2>{esc(title)}</h2>'
        f'<p>{esc(message)}</p>'
        '</div>'
        f'<span class="section-count">{esc(count)}</span>'
        '</div>'
    )


def render(*, recovery_state_path: str, cooldown_state_path: str, recent_requests_path: str) -> str:
    now_epoch = time.time()
    now_label = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_rows = [current_thread_record(row) for row in active_recoveries(load_json(recovery_state_path), now=now_epoch)]
    cooldowns = active_cooldowns(load_json(cooldown_state_path), now=now_epoch)
    recent_rows = [recent_thread_record(row) for row in load_recent_stuck(recent_requests_path)]
    recovering_count, cooldown_count = summary_counts(current_rows, cooldowns)
    current_cards = "\n".join(thread_card(row) for row in current_rows) or empty_state(
        "No recovering threads",
        "No thread is currently being kept alive by recovery polling.",
    )
    cooldown_cards = "\n".join(
        cooldown_card(row, index=index)
        for index, row in enumerate(cooldowns, start=1)
    ) or empty_state(
        "All routes are available",
        "No deployment or protocol is temporarily paused after upstream failures.",
    )
    recent_cards = "\n".join(thread_card(row) for row in recent_rows) or empty_state(
        "No recent recovery timeout records",
        "Recent stream start/idle timeout records will appear here after requests are logged.",
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>LiteLLM Recovery</title>
<style>
{ROUTE_TRACE_CSS}
{RECOVERY_CSS}
</style>
</head>
<body data-generated-at="{esc(f'{now_epoch:.3f}')}">
<header>
  <h1>LiteLLM Recovery</h1>
  <div class="sub">Generated {esc(now_label)}. Countdown timers update live; refresh to load routing-state changes.</div>
  <div class="toolbar">
    <input id="search" type="search" placeholder="Search thread id, request id, model, provider, route, exception">
    <button data-filter="all" class="active">All</button>
    <button data-filter="current">Current</button>
    <button data-filter="recent">Recent</button>
    <button data-filter="polling">Recovering</button>
    <button data-filter="cooldown">Paused routes</button>
    <button id="expand">Expand all</button>
    <button id="collapse">Collapse all</button>
  </div>
</header>
<main>
  <section class="stats recovery-stats">
    <div class="metric"><b>{recovering_count}</b><span>recovering threads</span></div>
    <div class="metric"><b>{cooldown_count}</b><span>temporarily paused routes</span></div>
    <div class="metric"><b>{len(recent_rows)}</b><span>recent timeouts</span></div>
  </section>
  {section_heading(
      "Recovering Threads",
      "Requests currently kept alive while LiteLLM polls an upstream response.",
      recovering_count,
  )}
  <section id="current-threads">{current_cards}</section>
  {section_heading(
      "Temporarily Paused Routes",
      "After repeated upstream failures, LiteLLM briefly skips the affected deployment and protocol, then retries it automatically.",
      cooldown_count,
  )}
  <section id="cooldown-threads">{cooldown_cards}</section>
  {section_heading(
      "Recent Recovery Timeouts",
      "Previous requests that exhausted their configured stream recovery window.",
      len(recent_rows),
  )}
  <section id="recent-threads">{recent_cards}</section>
</main>
<script>
{RECOVERY_JS}
</script>
</body>
</html>
"""


def summary(*, recovery_state_path: str, cooldown_state_path: str) -> str:
    now_epoch = time.time()
    recoveries = active_recoveries(load_json(recovery_state_path), now=now_epoch)
    cooldowns = active_cooldowns(load_json(cooldown_state_path), now=now_epoch)
    recovering, cooldown = summary_counts(recoveries, cooldowns)
    return f"{recovering} recovering / {cooldown} cooldown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recovery-state-file", default="")
    parser.add_argument("--cooldown-state-file", default="")
    parser.add_argument("--recent-requests-log", default="")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    try:
        if args.summary:
            sys.stdout.write(summary(
                recovery_state_path=args.recovery_state_file,
                cooldown_state_path=args.cooldown_state_file,
            ) + "\n")
        else:
            sys.stdout.write(render(
                recovery_state_path=args.recovery_state_file,
                cooldown_state_path=args.cooldown_state_file,
                recent_requests_path=args.recent_requests_log,
            ))
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

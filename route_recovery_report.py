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
.thread-section-title {
  margin: 18px 0 8px;
  color: #344055;
  font-size: 15px;
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
  color: #9a3412;
}
[data-countdown-until] {
  font-variant-numeric: tabular-nums;
}
"""

RECOVERY_JS = r"""
const cards = Array.from(document.querySelectorAll('.request-card'));
const search = document.getElementById('search');
const buttons = Array.from(document.querySelectorAll('button[data-filter]'));
const countdownNodes = Array.from(document.querySelectorAll('[data-countdown-until]'));
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
    const label = remaining > 0 ? durationLabel(remaining) : 'expired';
    node.textContent = label;
    const card = node.closest('.request-card');
    if (card) {
      card.dataset.remainingSeconds = remaining.toFixed(3);
      if (remaining <= 0) {
        card.classList.add('countdown-expired');
      }
    }
  }
}

search.addEventListener('input', applyFilters);
for (const button of buttons) {
  button.addEventListener('click', () => {
    activeFilter = button.dataset.filter;
    for (const item of buttons) item.classList.toggle('active', item === button);
    applyFilters();
  });
}
document.getElementById('expand').addEventListener('click', () => cards.forEach(card => card.open = true));
document.getElementById('collapse').addEventListener('click', () => cards.forEach(card => card.open = false));
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
        until_text = f"{cooldown_until:.3f}"
        remaining_text = f"{remaining_value:.3f}"
        card_attrs += (
            f' data-countdown-until="{esc(until_text)}" '
            f'data-remaining-seconds="{esc(remaining_text)}"'
        )
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


def cooldown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<span class="empty">No deployment cooldowns.</span>'
    body = []
    for row in rows:
        body.append(
            '<div class="detail-row">'
            f'<span class="detail-key">{esc(duration_label(row.get("remaining_seconds")))}</span>'
            f'<span class="detail-value">{esc(value_text(row))}</span>'
            '</div>'
        )
    return ''.join(body)


def render(*, recovery_state_path: str, cooldown_state_path: str, recent_requests_path: str) -> str:
    now_epoch = time.time()
    now_label = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_rows = [current_thread_record(row) for row in active_recoveries(load_json(recovery_state_path), now=now_epoch)]
    cooldowns = active_cooldowns(load_json(cooldown_state_path), now=now_epoch)
    cooldown_rows = [cooldown_thread_record(row) for row in cooldowns]
    recent_rows = [recent_thread_record(row) for row in load_recent_stuck(recent_requests_path)]
    recovering_count, cooldown_count = summary_counts(current_rows, cooldowns)
    current_cards = "\n".join(thread_card(row) for row in current_rows) or empty_state(
        "No recovering threads",
        "No thread is currently being kept alive by recovery polling.",
    )
    cooldown_cards = "\n".join(thread_card(row) for row in cooldown_rows) or empty_state(
        "No deployment cooldowns",
        "Routes with deployment cooldowns will appear here with their route key and remaining time.",
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
  <div class="sub">Generated {esc(now_label)}. Cooldown remaining times tick down live in this page; refresh for recovery status changes.</div>
  <div class="toolbar">
    <input id="search" type="search" placeholder="Search thread id, request id, model, provider, route, exception">
    <button data-filter="all" class="active">All</button>
    <button data-filter="current">Current</button>
    <button data-filter="recent">Recent</button>
    <button data-filter="polling">Recovering</button>
    <button data-filter="cooldown">Cooldown</button>
    <button id="expand">Expand all</button>
    <button id="collapse">Collapse all</button>
  </div>
</header>
<main>
  <section class="stats">
    <div class="metric"><b>{recovering_count}</b><span>recovering threads</span></div>
    <div class="metric"><b>{cooldown_count}</b><span>cooldown threads</span></div>
    <div class="metric"><b>{len(cooldowns)}</b><span>deployment cooldowns</span></div>
    <div class="metric"><b>{len(recent_rows)}</b><span>recent timeouts</span></div>
  </section>
  <h2 class="thread-section-title">Recovering Threads</h2>
  <section id="current-threads">{current_cards}</section>
  <h2 class="thread-section-title">Deployment Cooldowns</h2>
  <section id="cooldown-threads">{cooldown_cards}</section>
  <h2 class="thread-section-title">Recent Recovery Timeouts</h2>
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

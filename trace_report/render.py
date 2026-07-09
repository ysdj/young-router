from __future__ import annotations

import argparse
import collections
import datetime as _dt
import sys
from typing import Any

from .assets import ROUTE_TRACE_CSS, ROUTE_TRACE_JS
from .cards import request_card
from .common import (
    esc,
    event_label,
    merge_trace_state_status,
    parse_events,
    read_trace_state,
    trace_state_banner,
    local_datetime_label,
)
from .deployments import provider_class
from .summaries import request_summary

def render(
    events: list[dict[str, Any]],
    *,
    scan_lines: str,
    max_requests: int,
    trace_state: dict[str, Any] | None = None,
) -> str:
    trace_state = trace_state or {}
    grouped: dict[str, list[dict[str, Any]]] = collections.OrderedDict()
    for event in events:
        request_id = str(event.get("request_id") or "(missing request_id)")
        grouped.setdefault(request_id, []).append(event)

    summaries = [request_summary(request_id, req_events) for request_id, req_events in grouped.items()]
    summaries.reverse()
    visible_summaries = summaries[:max_requests]
    event_counts = collections.Counter(str(event.get("event") or "") for event in events)
    provider_counts = collections.Counter()
    for event in events:
        dep = event.get("deployment")
        if isinstance(dep, dict) and dep.get("provider"):
            provider_counts[str(dep["provider"])] += 1

    request_cards = "\n".join(
        request_card(summary, grouped[summary["request_id"]]) for summary in visible_summaries
    )
    event_count_html = "".join(
        f'<span class="stat-chip" title="{esc(name)}">{esc(event_label(name))} <b>{count}</b></span>'
        for name, count in sorted(event_counts.items())
    )
    provider_count_html = "".join(
        f'<span class="stat-chip {provider_class(name)}">{esc(name)} <b>{count}</b></span>'
        for name, count in sorted(provider_counts.items())
    )
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    showing_text = (
        f"Showing {len(visible_summaries)} most recent requests"
        if len(summaries) > len(visible_summaries)
        else f"Showing {len(visible_summaries)} requests"
    )

    empty = ""
    if not events:
        disabled_at = local_datetime_label(trace_state.get("disabled_at"))
        if trace_state.get("status") == "disabled" and disabled_at:
            empty_message = (
                f"Route Trace is off. No trace events are recorded after {disabled_at}."
            )
        elif trace_state.get("status") == "disabled":
            empty_message = "Route Trace is off, so new requests are not being recorded."
        else:
            empty_message = "Enable Route Trace, run a request, and open this page again."
        empty = (
            '<section class="empty-state">'
            "<h2>No route trace events found</h2>"
            f"<p>{esc(empty_message)}</p>"
            "</section>"
        )
    state_banner = trace_state_banner(trace_state)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>LiteLLM Route Trace</title>
<style>
{ROUTE_TRACE_CSS}
</style>
</head>
<body>
<header>
  <h1>LiteLLM Route Trace</h1>
  <div class="sub">Generated {esc(now)}. Scanned last {esc(scan_lines)} native proxy log lines. {esc(showing_text)}.</div>
  <div class="toolbar">
    <input id="search" type="search" placeholder="Search request id, provider, model, event, api base">
    <button data-filter="all" class="active">All</button>
    <button data-filter="fallback">Fallback only</button>
    <button data-filter="image">Image or vision</button>
    <button id="expand">Expand all</button>
    <button id="collapse">Collapse all</button>
  </div>
</header>
<main>
  <section class="stats">
    <div class="metric"><b>{len(grouped)}</b><span>requests</span></div>
    <div class="metric"><b>{len(events)}</b><span>trace events</span></div>
    <div class="metric"><b>{sum(1 for item in summaries if item["fallback"])}</b><span>fallback requests</span></div>
    <div class="metric"><b>{sum(1 for item in summaries if item["image"])}</b><span>image/vision requests</span></div>
  </section>
  {state_banner}
  <div class="stat-row">{event_count_html}</div>
  <div class="stat-row">{provider_count_html}</div>
  {empty}
  <section id="requests">{request_cards}</section>
</main>
<script>
{ROUTE_TRACE_JS}
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-lines", default="unknown")
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--trace-state-file", default="")
    parser.add_argument("--trace-state-status", default="")
    args = parser.parse_args()
    events = parse_events(sys.stdin.read().splitlines())
    trace_state = merge_trace_state_status(
        read_trace_state(args.trace_state_file),
        args.trace_state_status,
    )
    try:
        sys.stdout.write(
            render(
                events,
                scan_lines=args.scan_lines,
                max_requests=max(1, args.max_requests),
                trace_state=trace_state,
            )
        )
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

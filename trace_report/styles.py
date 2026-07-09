from __future__ import annotations

ROUTE_TRACE_CSS = r"""
:root {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --line: #d9dee7;
  --text: #172033;
  --muted: #637083;
  --soft: #eef1f5;
  --green: #1f8a5b;
  --blue: #246fc7;
  --amber: #b56a00;
  --red: #b42318;
  --violet: #6b4bb8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.45;
}
header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: rgba(246, 247, 249, 0.96);
  border-bottom: 1px solid var(--line);
  padding: 18px 22px 14px;
}
h1 { margin: 0 0 4px; font-size: 22px; letter-spacing: 0; }
.sub { color: var(--muted); font-size: 13px; }
.toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-top: 14px;
  flex-wrap: wrap;
}
input[type="search"] {
  width: min(520px, 100%);
  padding: 9px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  color: var(--text);
}
button {
  border: 1px solid var(--line);
  background: #fff;
  color: var(--text);
  border-radius: 6px;
  padding: 8px 10px;
  cursor: pointer;
}
button.active {
  background: #172033;
  color: #fff;
  border-color: #172033;
}
main { padding: 18px 22px 34px; }
.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(140px, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.metric {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
}
.metric b { display: block; font-size: 24px; }
.metric span { color: var(--muted); font-size: 12px; text-transform: uppercase; }
.trace-state {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid #e5c17b;
  border-radius: 8px;
  background: #fff7e8;
  color: #6f4200;
  padding: 10px 12px;
  margin: 0 0 14px;
  flex-wrap: wrap;
}
.trace-state b { color: var(--amber); }
.trace-state span { color: #6f4200; }
.trace-state-legacy {
  border-color: #b9d2f0;
  background: #eef6ff;
  color: #174a7c;
}
.trace-state-legacy b {
  color: var(--blue);
}
.trace-state-legacy span {
  color: #174a7c;
}
.stat-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 10px 0 16px;
}
.stat-chip, .flag, .cap {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
  padding: 3px 8px;
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}
.flag-fallback { color: var(--amber); border-color: #e5c17b; background: #fff7e8; }
.flag-image { color: var(--violet); border-color: #c9b9ef; background: #f4f0ff; }
.flag-surface { color: var(--blue); border-color: #b9d2f0; background: #eef6ff; }
.flag-reasoning { color: #7a4b00; border-color: #e5c17b; background: #fff7e8; }
.flag-tools { color: #155e75; border-color: #a5d8e8; background: #ecfeff; }
.flag-calls { color: var(--green); border-color: #b9dfcc; background: #eaf7f0; }
.request-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  margin: 10px 0;
  overflow: hidden;
}
.request-card[open] { border-color: #aeb8c7; }
.request-card > summary {
  list-style: none;
  cursor: pointer;
  padding: 12px 14px;
}
.request-card > summary::-webkit-details-marker { display: none; }
.summary-top {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(320px, 2fr);
  gap: 12px;
  align-items: center;
}
.summary-left, .summary-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.request-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #344055;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.time-badge, .duration-badge {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #f8fafc;
  color: #344055;
  padding: 2px 7px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  white-space: nowrap;
}
.duration-badge {
  color: var(--blue);
  border-color: #b9d2f0;
  background: #eef6ff;
}
.model { color: var(--muted); }
.session-group {
  display: inline-flex;
  gap: 5px;
  min-width: 0;
}
.session-chip {
  display: inline-block;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 2px 7px;
  color: #344055;
  background: #f8fafc;
  font-size: 12px;
}
.session-name { font-weight: 600; }
.session-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--muted);
}
.summary-preview {
  margin-top: 7px;
  color: #344055;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.route-chain {
  display: grid;
  gap: 8px;
  margin-top: 7px;
  min-width: 0;
}
.chain-header {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.chain-label {
  color: var(--muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0;
}
.chain-final-callout {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid #b9dfcc;
  background: #eaf7f0;
  color: #1f8a5b;
  font-size: 12px;
}
.chain-final-label {
  font-weight: 700;
}
.chain-flow {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: stretch;
  min-width: 0;
}
.chain-arrow {
  align-self: center;
  color: var(--muted);
  font-weight: 700;
  margin: 0 1px;
}
.chain-node {
  display: inline-grid;
  grid-template-columns: 22px minmax(0, 1fr);
  gap: 8px;
  min-width: 180px;
  max-width: 320px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  padding: 7px 8px;
}
.chain-node.chain-fallback {
  border-color: #c9b9ef;
  background: #f8f3ff;
}
.chain-node.chain-failed {
  border-color: #f0b8b2;
  background: #fff3f1;
}
.chain-node.chain-final {
  border-color: #b9dfcc;
  background: #eaf7f0;
}
.chain-node.chain-filter {
  border-color: #b9d2f0;
  background: #eef6ff;
}
.chain-node.chain-empty {
  border-style: dashed;
  color: var(--muted);
}
.chain-index {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  background: #172033;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
}
.chain-final .chain-index {
  background: var(--green);
}
.chain-copy {
  display: grid;
  gap: 1px;
  min-width: 0;
}
.chain-title {
  color: #172033;
  font-size: 12px;
  font-weight: 600;
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.chain-detail {
  color: var(--muted);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.summary-preview[data-full-preview] {
  cursor: help;
  outline-offset: 2px;
}
.selected-badge {
  border-radius: 6px;
  padding: 4px 8px;
  color: #fff;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.selected-meta, .event-count { color: var(--muted); font-size: 12px; }
.provider-provider_alpha { background: var(--green); border-color: var(--green); color: #fff; }
.provider-provider_beta { background: var(--blue); border-color: var(--blue); color: #fff; }
.provider-compat_provider { background: var(--amber); border-color: var(--amber); color: #fff; }
.provider-other { background: #667085; border-color: #667085; color: #fff; }
.preview-panel {
  border-top: 1px solid var(--line);
  padding: 12px 14px;
  background: #fbfcfe;
}
.preview-panel h4 {
  margin: 0 0 6px;
  font-size: 13px;
  color: #344055;
}
.preview-status {
  display: inline-flex;
  margin-left: 8px;
  border-radius: 999px;
  padding: 2px 7px;
  font-size: 11px;
  font-weight: 600;
}
.preview-status.warning {
  color: var(--amber);
  background: #fff7e8;
  border: 1px solid #e5c17b;
}
.preview-status.neutral {
  color: var(--muted);
  background: #f1f3f6;
  border: 1px solid var(--line);
}
.preview-main {
  margin: 0;
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.preview-meta {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.details-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(260px, 1fr));
  gap: 10px;
  border-top: 1px solid var(--line);
  padding: 12px 14px;
  background: #fff;
}
.detail-panel {
  border: 1px solid var(--soft);
  border-radius: 8px;
  padding: 10px;
  background: #fbfcfe;
  min-width: 0;
}
.detail-panel-wide {
  grid-column: 1 / -1;
}
.detail-panel h4 {
  margin: 0 0 8px;
  font-size: 13px;
  color: #344055;
}
.detail-row {
  display: grid;
  grid-template-columns: 138px minmax(0, 1fr);
  gap: 8px;
  padding: 3px 0;
  border-top: 1px solid #f0f2f5;
}
.detail-row:first-of-type {
  border-top: 0;
}
.detail-key {
  color: var(--muted);
  font-size: 12px;
}
.detail-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #172033;
  overflow-wrap: anywhere;
}
.detail-pills, .tool-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 8px;
}
.info-pill {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 2px 7px;
  background: #fff;
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.info-tool {
  color: #155e75;
  border-color: #a5d8e8;
  background: #ecfeff;
}
.info-flag {
  color: #344055;
  border-color: #cbd5e1;
  background: #f8fafc;
}
.tool-call {
  display: grid;
  grid-template-columns: minmax(120px, 220px) minmax(180px, 1fr);
  gap: 4px 10px;
  border-top: 1px solid #eef1f5;
  padding: 7px 0;
}
.tool-call:first-of-type {
  border-top: 0;
}
.tool-call-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: #172033;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.tool-call-meta {
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.tool-call-args {
  grid-column: 1 / -1;
  color: #344055;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.tool-action .tool-call-name {
  color: var(--blue);
}
.inner-details {
  border-top: 1px solid var(--line);
}
.inner-details > summary {
  cursor: pointer;
  padding: 9px 14px;
  color: #344055;
  font-weight: 600;
}
.pool-strip {
  padding: 10px 14px 2px;
}
.filter-flags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.pool {
  border-top: 1px solid var(--soft);
  padding: 10px 0;
}
.pool h4 {
  margin: 0 0 8px;
  font-size: 13px;
  color: #344055;
}
.pool h4 span { color: var(--muted); font-weight: 500; }
.order-lane {
  display: grid;
  grid-template-columns: 72px 1fr;
  gap: 8px;
  margin: 7px 0;
}
.order-label { color: var(--muted); font-size: 12px; padding-top: 6px; }
.deployments { display: flex; flex-wrap: wrap; gap: 6px; }
.deployment {
  display: grid;
  gap: 2px;
  min-width: 160px;
  max-width: 260px;
  color: #172033;
  background: #fff;
  border: 1px solid var(--line);
  border-left-width: 4px;
  border-radius: 6px;
  padding: 7px 8px;
}
.deployment.provider-provider_alpha { border-left-color: var(--green); }
.deployment.provider-provider_beta { border-left-color: var(--blue); }
.deployment.provider-compat_provider { border-left-color: var(--amber); }
.deployment.provider-other { border-left-color: #667085; }
.deployment.selected {
  outline: 2px solid #172033;
  outline-offset: 1px;
}
.dep-main {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dep-meta { color: var(--muted); font-size: 12px; }
.dep-caps { display: flex; gap: 4px; flex-wrap: wrap; }
.cap { padding: 1px 6px; font-size: 11px; }
.cap-yes { color: var(--green); background: #eaf7f0; border-color: #b9dfcc; }
.cap-no { color: var(--red); background: #fff0ee; border-color: #f0b8b2; }
.cap-unknown { color: var(--muted); background: #f5f6f8; }
.timeline {
  border-top: 1px solid var(--line);
  padding: 4px 14px 12px;
}
.timeline-row {
  display: grid;
  grid-template-columns: 118px 1fr;
  gap: 10px;
  border-bottom: 1px solid var(--soft);
  padding: 10px 0;
}
.event-timebox {
  display: grid;
  gap: 3px;
  align-content: start;
  justify-items: end;
}
.event-time {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: #344055;
  font-size: 12px;
  white-space: nowrap;
}
.seq {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--muted);
  font-size: 11px;
}
.timeline-head {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.event-chip {
  border-radius: 6px;
  padding: 3px 7px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.surface-chip, .chain-surface-chip {
  display: inline-flex;
  align-items: center;
  border: 1px solid #b9d2f0;
  border-radius: 999px;
  padding: 2px 7px;
  color: var(--blue);
  background: #eef6ff;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  line-height: 1.2;
  white-space: nowrap;
}
.chain-surface-chip {
  padding: 1px 6px;
}
.event-neutral { background: #f1f3f6; color: #344055; }
.event-info { background: #eaf3ff; color: var(--blue); }
.event-success { background: #eaf7f0; color: var(--green); }
.event-warning { background: #fff4e5; color: var(--amber); }
.event-text { color: var(--muted); }
.event-raw-name {
  margin-top: 3px;
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
}
details details summary {
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: #101828;
  color: #edf2f7;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
}
.empty, .empty-state { color: var(--muted); }
.empty-state {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
}
.hidden { display: none; }
.full-preview-popover {
  position: fixed;
  z-index: 1000;
  display: none;
  max-width: min(720px, calc(100vw - 24px));
  max-height: min(440px, calc(100vh - 24px));
  overflow: auto;
  padding: 12px 14px;
  border: 1px solid #243044;
  border-radius: 8px;
  background: #111827;
  color: #f8fafc;
  box-shadow: 0 18px 46px rgba(17, 24, 39, 0.28);
  font-size: 13px;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.full-preview-popover.visible { display: block; }
@media (max-width: 760px) {
  .stats { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
  .summary-top { grid-template-columns: 1fr; }
  .order-lane { grid-template-columns: 1fr; }
  .details-grid { grid-template-columns: 1fr; }
  .detail-row { grid-template-columns: 1fr; }
  .tool-call { grid-template-columns: 1fr; }
}
"""

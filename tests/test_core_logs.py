from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from young_router.core import CoreIPCClient, CoreIPCServer, CoreStore
from young_router.core.domains.logs import MAX_FILTER_CHARS, MAX_VIEW_BYTES, LOG_TABS, LogsDomain, LogsDomainError
from young_router.core.service import LOG_TABS as CORE_LOG_TABS


class _UsageReader:
    def refresh(self) -> list[dict[str, str]]:
        return [
            {"timestamp": "2026-01-01T00:00:00Z", "source": "newapi", "source_label": "relay", "model": "default-chat", "total_tokens": "12"},
            {"timestamp": "2026-01-01T00:00:00Z", "source": "newapi", "source_label": "relay", "model": "other-chat", "total_tokens": "4"},
        ]


class _ChangingUsageReader:
    def __init__(self) -> None:
        self.calls = 0

    def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
        self.calls += 1
        stamp = f"2026-01-01T00:00:0{self.calls}Z"
        return [
            {"timestamp": stamp, "source": "newapi", "source_label": "relay", "model": "default-chat", "total_tokens": str(self.calls)},
            {"timestamp": stamp, "source": "newapi", "source_label": "relay", "model": "other-chat", "total_tokens": str(self.calls)},
        ]


class _ScopedUsageReader:
    """A usage reader that reports which relay each refresh asked for."""

    def __init__(self) -> None:
        self.scopes: list[str] = []

    def sources(self) -> list[dict[str, str]]:
        return [
            {"id": "one", "label": "relay-one"},
            {"id": "two", "label": "relay-two"},
        ]

    def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
        self.scopes.append(source_id or "")
        return [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "source": "newapi",
                "source_label": source_id or "all",
                "model": f"{source_id or 'all'} rows",
                "total_tokens": "1",
            }
        ]


class _PagedUsageReader:
    """A relay whose five rows span two pages without a second remote read."""

    def __init__(self) -> None:
        self.calls = 0

    def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
        self.calls += 1
        return [
            {
                "timestamp": f"2026-01-01T00:00:0{index}Z",
                "source": "newapi",
                "source_label": "paged-relay",
                "model": f"model-{index}",
            }
            for index in range(5)
        ]


class _StructuredUsageReader:
    """A usage reader answering with the record shape Core projects."""

    def sources(self) -> list[dict[str, str]]:
        return [
            {"id": "one", "label": "flux-code"},
            {"id": "two", "label": "tken"},
        ]

    def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
        # Each relay answers only for itself, the way the real reader scopes.
        rows = {
            "one": {
                "timestamp": "2026-07-18T10:00:00Z",
                "source": "newapi",
                "source_label": "flux-code",
                "source_family": "NewAPI",
                "model": "gpt-5",
                "status": "success",
                "channel": "channel-one",
                "total_tokens": "300",
            },
            "two": {
                "timestamp": "2026-07-18T11:00:00Z",
                "source": "newapi",
                "source_label": "tken",
                "source_family": "NewAPI",
                "model": "claude-opus",
                "status": "success",
                "total_tokens": "88",
            },
        }
        return [rows[source_id]] if source_id in rows else []


class _SilentUsageReader:
    """A reader for a relay that exposes no usage feed at all."""

    def refresh(self, source_id: str | None = None) -> tuple[list[dict[str, str]], str]:
        from young_router.core.remote_usage_logs import USAGE_REASON_UNSUPPORTED

        return [], USAGE_REASON_UNSUPPORTED


class _OverlappingUsageReader:
    """An older source waits until a newer source has answered."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
        if source_id == "slow":
            self.started.set()
            self.release.wait(2)
        return [{
            "timestamp": "2026-07-18T10:00:00Z",
            "source_label": source_id or "",
            "model": f"{source_id} model",
        }]


class LogsDomainTests(unittest.TestCase):
    def test_reads_all_tabs_without_exposing_bodies_secrets_or_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "sk-synthetic-log-secret"
            (root / "recent-requests.jsonl").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "model_group": "default-chat",
                        "body": {"prompt": "private request"},
                        "authorization": secret,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "server.log").write_text(
                f"failed at {root}/config.yaml Bearer {secret}\n",
                encoding="utf-8",
            )
            domain = LogsDomain(root)

            snapshot_text = json.dumps({tab: domain.view(tab) for tab in LOG_TABS})

            self.assertEqual(CORE_LOG_TABS, LOG_TABS)
            self.assertEqual(set(LOG_TABS), set(domain.snapshot()["tabs"]))
            self.assertNotIn("config-watch", domain.snapshot()["tabs"])
            self.assertNotIn(secret, snapshot_text)
            self.assertNotIn(str(root), snapshot_text)
            self.assertNotIn("private request", snapshot_text)

    def test_redacts_generic_sensitive_key_value_log_forms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = "synthetic-menu-token"
            password = "synthetic-menu-password"
            api_key = "synthetic-menu-api-key"
            (root / "server.log").write_text(
                f"token={token} password: {password} api_key={api_key} ANTHROPIC_AUTH_TOKEN={token}\n",
                encoding="utf-8",
            )

            records = LogsDomain(root).view("service")["log"]["records"]
            projected = json.dumps(records)

            self.assertNotIn(token, projected)
            self.assertNotIn(password, projected)
            self.assertNotIn(api_key, projected)
            self.assertIn("token=configured", projected)
            self.assertIn("password: configured", projected)
            self.assertIn("ANTHROPIC_AUTH_TOKEN=configured", projected)

    def test_service_log_removes_console_color_control_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.log").write_text(
                "[2026-08-01T04:10:11Z] \x1b[92mLiteLLM Proxy:WARNING\x1b[0m: safe detail\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("service")["log"]["records"][0]

            self.assertEqual(
                "[2026-08-01T04:10:11Z] LiteLLM Proxy:WARNING: safe detail",
                record,
            )

    def test_route_trace_parses_the_prefixed_json_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "timestamp": "2026-08-01T04:10:11Z",
                "event": "deployment_selected",
                "provider": "example-provider",
                "status": "ok",
            }
            (root / "server.log").write_text(
                f"[2026-08-01T04:10:11Z] litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            tab = LogsDomain(root).view("route-trace")["log"]

            self.assertEqual(1, tab["line_count"])
            self.assertEqual("deployment_selected", tab["records"][0]["event"])
            self.assertEqual("example-provider", tab["records"][0]["provider"])

    def test_route_trace_reads_previous_segment_and_filters_before_limiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text("YOUNG_ROUTER_LOG_VIEW_LIMIT=2\n", encoding="utf-8")
            previous = root / "server.log.1"
            current = root / "server.log"
            older = {
                "timestamp": "2026-08-01T04:10:11Z",
                "event": "selected_deployment",
                "model_group": "older-chat",
            }
            newer = {
                "timestamp": "2026-08-01T04:10:12Z",
                "event": "selected_deployment",
                "model_group": "newer-chat",
            }
            previous.write_text(
                "\n".join(
                    [
                        f"litellm_route_trace {json.dumps(older)}",
                        *("ordinary service output" for _ in range(32)),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            current.write_text(
                f"litellm_route_trace {json.dumps(newer)}\n",
                encoding="utf-8",
            )

            tab = LogsDomain(root, runtime_settings_path=settings).view("route-trace")["log"]

            self.assertEqual(2, tab["line_count"])
            self.assertEqual(
                ["older-chat", "newer-chat"],
                [record["public_model"] for record in tab["records"]],
            )

    def test_cleared_route_trace_does_not_restore_previous_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / "server.log.1"
            current = root / "server.log"
            previous.write_text(
                "litellm_route_trace "
                + json.dumps(
                    {
                        "timestamp": "2026-08-01T04:10:11Z",
                        "event": "selected_deployment",
                        "model_group": "older-chat",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            current.write_text(
                "litellm_route_trace "
                + json.dumps(
                    {
                        "timestamp": "2026-08-01T04:10:12Z",
                        "event": "selected_deployment",
                        "model_group": "current-chat",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            domain = LogsDomain(root)

            self.assertEqual(2, domain.view("route-trace")["log"]["line_count"])
            domain.dispatch("logs.clear", {"tab": "route-trace"})
            self.assertEqual(0, domain.view("route-trace")["log"]["line_count"])

            with current.open("a", encoding="utf-8") as handle:
                handle.write(
                    "litellm_route_trace "
                    + json.dumps(
                        {
                            "timestamp": "2026-08-01T04:10:13Z",
                            "event": "selected_deployment",
                            "model_group": "after-clear-chat",
                        }
                    )
                    + "\n"
                )

            records = domain.view("route-trace")["log"]["records"]
            self.assertEqual(["after-clear-chat"], [record["public_model"] for record in records])

    def test_route_trace_projects_nested_deployment_into_table_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "timestamp": "2026-08-01T04:10:11Z",
                "event": "selected_deployment",
                "model_group": "public-chat",
                "deployment": {
                    "id": "deployment-a",
                    "provider": "provider-a",
                    "model": "openai/upstream-chat",
                    "order": 0.08,
                    "api_base": "https://provider.example/v1",
                },
                "request": {
                    "preview": "private request body",
                    "interface": {
                        "effective_upstream_surface": "responses",
                        "requested_endpoint": "/v1/responses",
                        "stream": True,
                    },
                },
            }
            (root / "server.log").write_text(
                f"[2026-08-01T04:10:11Z] litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("route-trace")["log"]["records"][0]

            self.assertEqual("selected_deployment", record["event"])
            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("upstream-chat", record["upstream_model"])
            self.assertEqual("provider-a", record["provider"])
            self.assertEqual("order=0.08 · protocol=responses · stream=true", record["detail"])
            self.assertNotIn("request", record)
            self.assertNotIn("private request body", json.dumps(record))

    def test_route_trace_projects_a_served_route_like_its_selection(self) -> None:
        """The success event must name the same route its selection named.

        The trace view groups by request and merges a route's records into one
        step, so a success that projected a different identity would land as a
        separate route and never confirm the one that actually answered.
        """

        route_key = (
            "model=public-chat / provider=provider-a / upstream=openai/upstream-chat"
            " / host=provider.example / key=team / order=0"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {
                    "timestamp": "2026-08-01T04:10:11Z",
                    "event": "selected_deployment",
                    "request_id": "req-1",
                    "model_group": "public-chat",
                    "deployment_id": "deployment-a",
                    "order": 0,
                    "route_key": route_key,
                },
                {
                    "timestamp": "2026-08-01T04:10:14Z",
                    "event": "deployment_success",
                    "request_id": "req-1",
                    "model_group": "public-chat",
                    "deployment_id": "deployment-a",
                    "order": 0,
                    "route_key": route_key,
                },
            ]
            (root / "server.log").write_text(
                "".join(
                    f"[{record['timestamp']}] litellm_route_trace {json.dumps(record)}\n"
                    for record in records
                ),
                encoding="utf-8",
            )

            projected = LogsDomain(root).view("route-trace")["log"]["records"]

            self.assertEqual(2, len(projected))
            by_event = {record["event"]: record for record in projected}
            success = by_event["deployment_success"]
            selection = by_event["selected_deployment"]
            self.assertEqual("deployment_success", success["event"])
            self.assertEqual("order=0", success["detail"])
            self.assertEqual(selection["route"], success["route"])
            self.assertEqual("deployment-a", success["route"]["deployment_id"])

    def test_route_trace_uses_configured_public_model_for_deployment_only_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(
                """providers:
  provider-a:
    api_base: https://provider.example/v1
    api_keys:
      - name: key-a
        value: key-a
model_list:
  - model_name: public-chat
    litellm_params:
      model: openai/upstream-chat
      api_base: https://provider.example/v1
      api_key: key-a
    model_info:
      id: abcdef12
      supported_upstream_url_surfaces:
        - openai/responses
      upstream_url_surface: openai/responses
""",
                encoding="utf-8",
            )
            payload = {
                "timestamp": "2026-08-01T04:10:11Z",
                "event": "selected_deployment",
                "deployment": {
                    "id": "abcdef12",
                    "provider": "provider-a",
                    "model": "openai/upstream-chat",
                },
            }
            (root / "server.log").write_text(
                f"litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("route-trace")["log"]["records"][0]

            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("upstream-chat", record["upstream_model"])

    def test_request_rows_resolve_the_public_model_by_route_when_the_id_is_dynamic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(
                """providers:
  provider-a:
    api_base: https://provider-a.example/v1
    api_keys:
      - name: key-a
        value: key-a
model_list:
  - model_name: public-chat
    litellm_params:
      model: openai/upstream-chat
      api_base: https://provider-a.example/v1
      api_key: key-a
    model_info:
      id: abcdef12
      order: 0.08
""",
                encoding="utf-8",
            )
            records = [
                # LiteLLM hashes the deployment id for client-side-credential
                # and rebuilt-fallback calls; the route identity still names
                # the configured public model instead of the upstream spelling.
                {
                    "ts": "2026-08-01T04:10:00Z",
                    "request_id": "dynamic-id",
                    "status": "failure",
                    "deployment_id": "a" * 64,
                    "provider": "provider-a",
                    "upstream_model": "openai/upstream-chat",
                    "api_key_name": "key-a",
                    "route_key": (
                        "model=openai/upstream-chat / provider=provider-a / "
                        "upstream=openai/upstream-chat / host=relay.example / "
                        "key=key-a / order=1"
                    ),
                },
                # A recorded order that differs from the staged default still
                # resolves through the orderless route index.
                {
                    "ts": "2026-08-01T04:10:01Z",
                    "request_id": "dynamic-id-other-order",
                    "status": "failure",
                    "deployment_id": "c" * 64,
                    "provider": "provider-a",
                    "upstream_model": "openai/upstream-chat",
                    "api_key_name": "key-a",
                    "route_key": (
                        "model=openai/upstream-chat / provider=provider-a / "
                        "upstream=openai/upstream-chat / host=relay.example / "
                        "key=key-a / order=0.08"
                    ),
                },
                # A route the configuration does not contain keeps its own name.
                {
                    "ts": "2026-08-01T04:10:02Z",
                    "request_id": "unknown-route",
                    "status": "failure",
                    "deployment_id": "b" * 64,
                    "provider": "provider-b",
                    "upstream_model": "openai/upstream-chat",
                    "api_key_name": "key-b",
                    "route_key": (
                        "model=openai/upstream-chat / provider=provider-b / "
                        "upstream=openai/upstream-chat / host=relay.example / "
                        "key=key-b / order=0.08"
                    ),
                },
            ]
            (root / "recent-requests.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            projected = LogsDomain(root).view("requests")["log"]["records"]

            self.assertEqual(
                {
                    "dynamic-id": "public-chat",
                    "dynamic-id-other-order": "public-chat",
                    "unknown-route": "openai/upstream-chat",
                },
                {
                    record["request_id"]: record["public_model"]
                    for record in projected
                },
            )

    def test_route_trace_projects_failed_route_into_recovery_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "timestamp": "2026-08-01T04:10:11Z",
                "event": "route_recovery_poll_waiting_for_cooldown",
                "model_group": "public-chat",
                "exception": {
                    "failed_deployment_id": "deployment-a",
                    "failed_deployment_route_key": (
                        "model=public-chat / provider=provider-a / "
                        "upstream=openai/upstream-chat"
                    ),
                    "reason": "upstream-status-504",
                },
            }
            (root / "server.log").write_text(
                f"litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("route-trace")["log"]["records"][0]

            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("upstream-chat", record["upstream_model"])
            self.assertEqual("provider-a", record["provider"])
            self.assertEqual("reason=upstream-status-504", record["detail"])

    def test_route_trace_projects_fallback_details_without_request_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "timestamp": "2026-08-01T04:10:11Z",
                "event": "next_order_fallback_available",
                "model_group": "public-chat",
                "failed_order": 1,
                "target_order": 2,
                "excluded_deployment_ids": ["deployment-a"],
                "request": {"preview": "private request body"},
                "candidates": [
                    {"api_base": "https://provider.example/v1"},
                    {"api_base": "https://other.example/v1"},
                ],
            }
            (root / "server.log").write_text(
                f"litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("route-trace")["log"]["records"][0]

            self.assertEqual(
                "failed_order=1 · next_order=2 · candidates=2 · excluded=1",
                record["detail"],
            )
            self.assertNotIn("candidates", record)
            self.assertNotIn("private request body", json.dumps(record))

    def test_route_trace_projects_failure_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {
                    "timestamp": "2026-08-01T04:10:11Z",
                    "event": "deployment_failover_marked",
                    "deployment_order": 0.08,
                    "exception": {
                        "failed_deployment_order": 0.08,
                        "reason": "upstream-compatible-bad-request",
                        "text": "private upstream response",
                    },
                },
            ]
            (root / "server.log").write_text(
                "\n".join(
                    f"litellm_route_trace {json.dumps(record)}" for record in records
                ) + "\n",
                encoding="utf-8",
            )

            trace_records = LogsDomain(root).view("route-trace")["log"]["records"]

            self.assertEqual(
                "failed_order=0.08 · reason=upstream-compatible-bad-request",
                trace_records[0]["detail"],
            )
            self.assertNotIn("private upstream response", json.dumps(trace_records))

    def test_route_trace_counts_filtered_candidates_without_serializing_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "timestamp": "2026-08-01T04:10:11Z",
                "event": "filter_deployments",
                "model_group": "public-chat",
                "after_constraints": [
                    {"api_base": "https://provider.example/v1"},
                    {"api_base": "https://other.example/v1"},
                ],
            }
            (root / "server.log").write_text(
                f"litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("route-trace")["log"]["records"][0]

            self.assertEqual("candidates=2", record["detail"])
            self.assertNotIn("after_constraints", record)

    def test_route_trace_projects_search_synthesis_details_without_response_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {
                    "event": "external_web_search_bridge_empty_continuation_synthesis",
                    "round": 2,
                    "queries": ["private query"],
                },
                {
                    "event": "external_web_search_bridge_synthesis_done",
                    "queries": ["private query", "another private query"],
                    "source_url_count": 3,
                    "response_preview": "private answer",
                },
                {
                    "event": "external_web_search_bridge_synthesis_chat_start",
                    "request": {"preview": "private request"},
                },
                {
                    "event": "external_web_search_bridge_model_retry",
                    "phase": "synthesis",
                    "retry_attempt": 2,
                    "max_retries": 3,
                    "retry_delay_seconds": 1.5,
                    "exception": {"reason": "upstream-network-connectivity"},
                },
            ]
            (root / "server.log").write_text(
                "\n".join(
                    f"litellm_route_trace {json.dumps(record)}" for record in records
                ) + "\n",
                encoding="utf-8",
            )

            projected = LogsDomain(root).view("route-trace")["log"]["records"]

            self.assertEqual("round=2 · queries=1", projected[0]["detail"])
            self.assertEqual("queries=2 · sources=3", projected[1]["detail"])
            self.assertEqual("phase=synthesis", projected[2]["detail"])
            self.assertEqual(
                "phase=synthesis · retry=2 · max_retries=3 · retry_delay=1.5s · reason=upstream-network-connectivity",
                projected[3]["detail"],
            )
            self.assertNotIn("private query", json.dumps(projected))
            self.assertNotIn("private answer", json.dumps(projected))
            self.assertNotIn("private request", json.dumps(projected))

    def test_route_trace_projects_fallback_protocol_without_request_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "event": "generic_fallback_helper_start",
                "request": {
                    "preview": "private request body",
                    "interface": {
                        "client_surface": "responses",
                        "stream": True,
                    },
                },
            }
            (root / "server.log").write_text(
                f"litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("route-trace")["log"]["records"][0]

            self.assertEqual("protocol=responses · stream=true", record["detail"])
            self.assertNotIn("private request body", json.dumps(record))

    def test_route_trace_view_stays_below_ipc_message_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                f"[2026-08-01T04:10:{index % 60:02d}Z] litellm_route_trace "
                + json.dumps(
                    {
                        "timestamp": "2026-08-01T04:10:11Z",
                        "event": "deployment_selected",
                        "model_group": f"public-{index}",
                        "detail": "x" * 1800,
                    }
                )
                for index in range(3000)
            ]
            (root / "server.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

            view = LogsDomain(root).view("route-trace")
            encoded = json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

            self.assertLessEqual(len(encoded), MAX_VIEW_BYTES)
            self.assertEqual("public-2999", view["log"]["records"][-1]["public_model"])
            self.assertLessEqual(len(view["log"]["records"][-1].get("detail", "")), 260)

    def test_request_log_projects_the_credential_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recent-requests.jsonl").write_text(
                json.dumps(
                    {
                        "ts": "2026-08-01T04:10:11Z",
                        "status": "ok",
                        "model_group": "default-chat",
                        "public_model": "public-chat",
                        "provider": "provider-a",
                        "api_key_name": "credential-a",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("requests")["log"]["records"][0]

            self.assertEqual("credential-a", record["api_key_name"])
            self.assertEqual("public-chat", record["public_model"])

    def test_request_log_derives_legacy_public_model_from_route_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recent-requests.jsonl").write_text(
                json.dumps(
                    {
                        "model_group": "upstream-chat",
                        "upstream_model": "openai/upstream-chat",
                        "route_key": "model=public-chat / provider=provider-a / upstream=openai/upstream-chat",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("requests")["log"]["records"][0]

            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("openai/upstream-chat", record["upstream_model"])

    def test_request_log_corrects_upstream_name_using_deployment_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(
                """model_list:
  - model_name: public-chat
    litellm_params:
      model: openai/vendor-chat-tagged
    model_info:
      id: abc12345
      upstream_url_surface: openai/chat
      supported_upstream_url_surfaces: [openai/chat]
""",
                encoding="utf-8",
            )
            (root / "recent-requests.jsonl").write_text(
                json.dumps(
                    {
                        "deployment_id": "abc12345",
                        "public_model": "vendor-chat-tagged",
                        "upstream_model": "openai/vendor-chat-tagged",
                        "route_key": "model=vendor-chat-tagged / provider=provider-a / upstream=openai/vendor-chat-tagged",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("requests")["log"]["records"][0]

            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("openai/vendor-chat-tagged", record["upstream_model"])

    def test_request_log_never_projects_a_prefixed_route_model_as_the_public_name(self) -> None:
        """Route keys with ``model=openai/...`` must not surface a bare ``openai`` name."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(
                """model_list:
  - model_name: public-chat
    litellm_params:
      model: openai/upstream-chat
    model_info:
      id: abc12345
      upstream_url_surface: openai/chat
      supported_upstream_url_surfaces: [openai/chat]
""",
                encoding="utf-8",
            )
            (root / "recent-requests.jsonl").write_text(
                json.dumps(
                    {
                        "deployment_id": "abc12345",
                        "public_model": "openai/upstream-chat",
                        "model_group": "openai/upstream-chat",
                        "upstream_model": "openai/upstream-chat",
                        "route_key": "model=openai/upstream-chat / provider=provider-a / upstream=openai/upstream-chat",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("requests")["log"]["records"][0]

            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("openai/upstream-chat", record["upstream_model"])

    def test_recovery_log_prefers_the_configured_public_model_over_a_prefixed_route_key(self) -> None:
        """A prefixed failed route key must not truncate the public model to ``openai``."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc).isoformat()
            (root / "config.yaml").write_text(
                """model_list:
  - model_name: public-chat
    litellm_params:
      model: openai/upstream-chat
    model_info:
      id: abc12345
      upstream_url_surface: openai/chat
      supported_upstream_url_surfaces: [openai/chat]
""",
                encoding="utf-8",
            )
            state = root / ".litellm-runtime" / "route-recovery-state.json"
            state.parent.mkdir()
            state.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "route-a": {
                                "key": "route-a",
                                "status": "polling",
                                "updated_at": now,
                                "model_group": "openai/upstream-chat",
                                "exception": {
                                    "failed_deployment_id": "abc12345",
                                    "failed_deployment_route_key": (
                                        "model=openai/upstream-chat / provider=provider-a / "
                                        "upstream=openai/upstream-chat"
                                    ),
                                    "reason": "upstream-status-500",
                                },
                            }
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("recovery")["log"]["records"][0]

            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("upstream-chat", record["upstream_model"])
            self.assertEqual("provider-a", record["provider"])
            self.assertNotEqual("openai", record["public_model"])

    def test_request_log_preserves_token_counts_and_safe_failure_cause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recent-requests.jsonl").write_text(
                json.dumps(
                    {
                        "status": "failure",
                        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                        "error": {
                            "status_code": 403,
                            "type": "AuthenticationError",
                            "reason": "upstream-status-403",
                            "message": "private upstream response body",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("requests")["log"]["records"][0]

            self.assertEqual(18, record["usage"]["total_tokens"])
            self.assertEqual(403, record["error"]["status_code"])
            self.assertEqual("AuthenticationError", record["error"]["type"])
            self.assertEqual("unselected", record["routing_state"])
            self.assertNotIn("message", record["error"])

    def test_request_log_projects_prompt_completion_usage_as_sent_received(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recent-requests.jsonl").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 7,
                            "total_tokens": 18,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("requests")["log"]["records"][0]

            self.assertEqual(11, record["usage"]["input_tokens"])
            self.assertEqual(7, record["usage"]["output_tokens"])
            self.assertEqual(18, record["usage"]["total_tokens"])

    def test_service_log_omits_litellm_banner_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.log").write_text(
                "\n".join(
                    [
                        "[2026-08-01T04:10:11Z] #----------------------#",
                        "[2026-08-01T04:10:11Z] # Give Feedback / Get Help: https://github.com/BerriAI/litellm/issues/new #",
                        "[2026-08-01T04:10:11Z] openai/example-model",
                        "[2026-08-01T04:10:11Z]    ██╗     ██╗████████╗███████╗",
                        "[2026-08-01T04:10:11Z]    ╚══════╝╚═╝   ╚═╝   ╚══════╝",
                        "[2026-08-01T04:10:11Z] LiteLLM: Proxy initialized with Config, Set models:",
                        "[2026-08-01T04:10:11Z] gemini-3.1-pro",
                        "[2026-08-01T04:10:12Z] INFO: Started server process [123]",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = LogsDomain(root).view("service")["log"]["records"]

            self.assertEqual(["[2026-08-01T04:10:12Z] INFO: Started server process [123]"], records)

    def test_service_log_folds_one_worker_burst_into_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.log").write_text(
                "\n".join(
                    [
                        "[2026-08-01T04:10:11Z] INFO: Started server process [101]",
                        "[2026-08-01T04:10:11Z] INFO: Waiting for application startup.",
                        "[2026-08-01T04:10:11Z] INFO: Started server process [102]",
                        "[2026-08-01T04:10:11Z] INFO: Waiting for application startup.",
                        "[2026-08-01T04:10:12Z] INFO: Application startup complete.",
                        "[2026-08-01T04:10:12Z] INFO: Application startup complete.",
                        "[2026-08-01T04:10:12Z] INFO: 127.0.0.1:50001 - \"GET /v1/models HTTP/1.1\" 200 OK",
                        "[2026-08-01T04:10:12Z] INFO: 127.0.0.1:50002 - \"GET /v1/models HTTP/1.1\" 200 OK",
                        "[2026-08-01T04:10:12Z] INFO: 127.0.0.1:50003 - \"GET /v1/models HTTP/1.1\" 404 Not Found",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = LogsDomain(root).view("service")["log"]["records"]

            self.assertEqual(
                [
                    "[2026-08-01T04:10:11Z] INFO: Started server process [101] (×2)",
                    "[2026-08-01T04:10:11Z] INFO: Waiting for application startup. (×2)",
                    "[2026-08-01T04:10:12Z] INFO: Application startup complete. (×2)",
                ]
                + [
                    "[2026-08-01T04:10:12Z] INFO: 127.0.0.1:50001 - \"GET <private-path> HTTP/1.1\" 200 OK (×2)",
                    "[2026-08-01T04:10:12Z] INFO: 127.0.0.1:50003 - \"GET <private-path> HTTP/1.1\" 404 Not Found",
                ],
                records,
            )

    def test_service_log_keeps_a_late_repeat_as_its_own_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.log").write_text(
                "\n".join(
                    [
                        "[2026-08-01T04:10:11Z] INFO: Application startup complete.",
                        "[2026-08-01T04:15:30Z] INFO: Application startup complete.",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = LogsDomain(root).view("service")["log"]["records"]

            self.assertEqual(
                [
                    "[2026-08-01T04:10:11Z] INFO: Application startup complete.",
                    "[2026-08-01T04:15:30Z] INFO: Application startup complete.",
                ],
                records,
            )

    def test_service_log_splits_records_two_workers_wrote_into_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.log").write_text(
                "\n".join(
                    [
                        "[2026-08-01T04:10:11Z]    gemini-3.1-pro[2026-08-01T04:10:11Z] INFO: Started server process [101]",
                        "[2026-08-01T04:10:11Z] INFO: Started server process [101][2026-08-01T04:10:11Z] INFO: Started server process [102]",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = LogsDomain(root).view("service")["log"]["records"]

            self.assertEqual(
                ["[2026-08-01T04:10:11Z] INFO: Started server process [101] (×3)"],
                records,
            )

    def test_a_route_trace_record_is_never_split_by_a_quoted_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.log").write_text(
                "[2026-08-01T04:10:11Z] litellm_route_trace "
                '{"event": "probe", "preview": "[2026-08-01T04:10:11Z] text", '
                '"timestamp": "2026-08-01T04:10:11Z"}\n'
                "[2026-08-01T04:10:12Z] INFO: Started server process [7]\n",
                encoding="utf-8",
            )

            domain = LogsDomain(root)

            self.assertEqual(
                ["[2026-08-01T04:10:12Z] INFO: Started server process [7]"],
                domain.view("service")["log"]["records"],
            )
            self.assertEqual(1, len(domain.view("route-trace")["log"]["records"]))

    def test_a_long_service_row_keeps_its_repeat_count_inside_the_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = "x" * 900
            (root / "server.log").write_text(
                "\n".join(
                    f"[2026-08-01T04:10:1{index}Z] INFO: {message}" for index in range(2)
                )
                + "\n",
                encoding="utf-8",
            )

            records = LogsDomain(root).view("service")["log"]["records"]

            self.assertEqual(1, len(records))
            self.assertTrue(records[0].endswith(" (×2)"))
            self.assertLessEqual(len(records[0]), 512)

    def test_service_traceback_lines_are_one_selectable_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.log").write_text(
                "\n".join(
                    [
                        "[2026-08-01T04:10:12Z] INFO: Traceback (most recent call last):",
                        "[2026-08-01T04:10:12Z] INFO: File \"worker.py\", line 7, in run",
                        "[2026-08-01T04:10:12Z] INFO: response = await self._send()",
                        "[2026-08-01T04:10:12Z] INFO: ValueError: upstream failed",
                        "[2026-08-01T04:10:12Z] INFO: after traceback",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = LogsDomain(root).view("service")["log"]["records"]

            self.assertEqual(2, len(records))
            self.assertIn("File \"worker.py\"", records[0])
            self.assertIn("ValueError: upstream failed", records[0])
            self.assertEqual("[2026-08-01T04:10:12Z] INFO: after traceback", records[1])

    def test_request_records_are_sorted_by_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {"timestamp": "2026-08-01T04:10:13Z", "request_id": "newer", "status": "success"},
                {"timestamp": "2026-08-01T04:10:11Z", "request_id": "older", "status": "success"},
            ]
            (root / "recent-requests.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            projected = LogsDomain(root).view("requests")["log"]["records"]

            self.assertEqual(["older", "newer"], [record["request_id"] for record in projected])

    def test_request_records_project_each_live_lifecycle_to_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Live lifecycle rows stay fresh so the projection only exercises
            # the one-row-per-request collapse, not the stale-row fallback.
            base = datetime.now(timezone.utc).replace(microsecond=0)

            def stamp(offset: int) -> str:
                return (base + timedelta(seconds=offset)).isoformat()

            records = [
                {"ts": stamp(0), "request_id": "completed", "status": "pending"},
                {"ts": stamp(1), "request_id": "completed", "status": "stream"},
                {"ts": stamp(2), "request_id": "completed", "status": "success"},
                {"ts": stamp(3), "request_id": "completed", "status": "stream"},
                {"ts": stamp(4), "request_id": "retried", "status": "failure"},
                {"ts": stamp(5), "request_id": "retried", "status": "pending"},
                {"ts": stamp(6), "request_id": "retried", "status": "stream"},
            ]
            (root / "recent-requests.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            projected = LogsDomain(root).view("requests")["log"]["records"]

            self.assertEqual(2, len(projected))
            self.assertEqual(
                {"completed": "success", "retried": "stream"},
                {record["request_id"]: record["status"] for record in projected},
            )

    def test_request_records_normalize_epoch_seconds_and_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {"timestamp": 1_800_000_001_000, "request_id": "newer", "status": "success"},
                {"timestamp": 1_800_000_000, "request_id": "older", "status": "success"},
            ]
            (root / "recent-requests.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            projected = LogsDomain(root).view("requests")["log"]["records"]

            self.assertEqual(["older", "newer"], [record["request_id"] for record in projected])

    def test_request_rows_without_progress_are_reported_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            stale = (now - timedelta(hours=7)).replace(microsecond=0).isoformat()
            fresh = (now - timedelta(seconds=5)).replace(microsecond=0).isoformat()
            records = [
                {"ts": stale, "request_id": "abandoned", "status": "pending"},
                {"ts": stale, "request_id": "abandoned-stream", "status": "stream"},
                {"ts": stale, "request_id": "finished", "status": "success"},
                {"ts": fresh, "request_id": "active", "status": "pending"},
            ]
            (root / "recent-requests.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            projected = LogsDomain(root).view("requests")["log"]["records"]

            statuses = {record["request_id"]: record["status"] for record in projected}
            self.assertEqual(
                {
                    "abandoned": "aborted",
                    "abandoned-stream": "aborted",
                    "finished": "success",
                    "active": "pending",
                },
                statuses,
            )
            aborted = next(
                record for record in projected if record["request_id"] == "abandoned"
            )
            self.assertEqual("stale", aborted["aborted"]["reason"])

    def test_request_stale_window_follows_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text(
                "YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS=0\n",
                encoding="utf-8",
            )
            stale = (
                datetime.now(timezone.utc) - timedelta(hours=7)
            ).replace(microsecond=0).isoformat()
            (root / "recent-requests.jsonl").write_text(
                json.dumps({"ts": stale, "request_id": "open", "status": "pending"})
                + "\n",
                encoding="utf-8",
            )

            projected = LogsDomain(
                root, runtime_settings_path=settings
            ).view("requests")["log"]["records"]

            self.assertEqual("pending", projected[-1]["status"])

    def test_request_rows_follow_a_configured_stale_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text(
                "YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS=60\n",
                encoding="utf-8",
            )
            observed = (
                datetime.now(timezone.utc) - timedelta(seconds=61)
            ).replace(microsecond=0).isoformat()
            (root / "recent-requests.jsonl").write_text(
                json.dumps({"ts": observed, "request_id": "slow", "status": "pending"})
                + "\n",
                encoding="utf-8",
            )

            projected = LogsDomain(
                root, runtime_settings_path=settings
            ).view("requests")["log"]["records"]

            self.assertEqual("aborted", projected[-1]["status"])

    def test_request_rows_keep_the_aborted_reason_and_drop_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recent-requests.jsonl").write_text(
                json.dumps(
                    {
                        "ts": "2026-08-01T04:10:00Z",
                        "request_id": "closed",
                        "status": "aborted",
                        "public_model": "public-chat",
                        "aborted": {
                            "reason": "service_restart",
                            "api_key": "sk-synthetic-secret",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            raw = (root / "recent-requests.jsonl").read_text(encoding="utf-8")
            projected = LogsDomain(root).view("requests")["log"]["records"]

            self.assertEqual("aborted", projected[-1]["status"])
            self.assertEqual(
                {"reason": "service_restart"},
                projected[-1]["aborted"],
            )
            self.assertNotIn("sk-synthetic-secret", json.dumps(projected))
            self.assertIn("sk-synthetic-secret", raw)

    def test_recovery_records_are_sorted_by_updated_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            older = (now - timedelta(seconds=2)).isoformat()
            newer = now.isoformat()
            state = root / ".litellm-runtime" / "route-recovery-state.json"
            state.parent.mkdir()
            state.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "newer": {"updated_at": newer, "status": "waiting"},
                            "older": {"updated_at": older, "status": "waiting"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            projected = LogsDomain(root).view("recovery")["log"]["records"]

            self.assertEqual([older, newer], [record["timestamp"] for record in projected])

    def test_recovery_records_accept_canonical_timestamp_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            older = (now - timedelta(seconds=2)).isoformat()
            newer = now.isoformat()
            state = root / ".litellm-runtime" / "route-recovery-state.json"
            state.parent.mkdir()
            state.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "newer": {"timestamp": newer, "status": "waiting"},
                            "older": {"time": older, "status": "waiting"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            projected = LogsDomain(root).view("recovery")["log"]["records"]

            self.assertEqual([older, newer], [record["timestamp"] for record in projected])

    def test_route_trace_exposes_request_and_route_chain_fields_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "timestamp": "2026-08-01T04:10:11Z",
                "event": "deployment_failover_marked",
                "request_id": "request-a",
                "session_id": "session-a",
                "route_key": "model=public-chat / provider=provider-a / upstream=openai/one",
                "deployment": {"id": "deployment-a", "order": 0},
                "failed_route_key": "model=public-chat / provider=provider-b / upstream=openai/two",
                "failed_deployment_id": "deployment-b",
                "candidates": [
                    {"id": "deployment-c", "provider": "provider-c", "model": "openai/three", "order": 3},
                ],
            }
            (root / "server.log").write_text(
                f"litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("route-trace")["log"]["records"][0]

            self.assertEqual("request-a", record["request_id"])
            self.assertEqual("session-a", record["session_id"])
            self.assertEqual("deployment-a", record["route"]["deployment_id"])
            self.assertEqual(0, record["route"]["order"])
            self.assertEqual("deployment-b", record["failed_route"]["deployment_id"])
            self.assertEqual("deployment-c", record["candidate_routes"][0]["deployment_id"])

    def test_route_trace_does_not_mark_a_failed_only_route_as_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {
                "timestamp": "2026-08-01T04:10:11Z",
                "event": "deployment_failover_marked",
                "exception": {
                    "failed_deployment_id": "deployment-a",
                    "failed_deployment_route_key": (
                        "model=public-chat / provider=provider-a / upstream=openai/one"
                    ),
                },
            }
            (root / "server.log").write_text(
                f"litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("route-trace")["log"]["records"][0]

            self.assertNotIn("route", record)
            self.assertEqual("deployment-a", record["failed_route"]["deployment_id"])

    def test_default_view_limit_is_ten_thousand_and_runtime_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            (root / "actions.log").write_text(
                "\n".join(f"line-{index}" for index in range(150)) + "\n",
                encoding="utf-8",
            )
            domain = LogsDomain(root, runtime_settings_path=settings)

            self.assertEqual(10_000, domain.view("actions")["log"]["limit"])
            settings.write_text("YOUNG_ROUTER_LOG_VIEW_LIMIT=100\n", encoding="utf-8")

            tab = domain.view("actions")["log"]
            self.assertEqual(100, tab["limit"])
            self.assertEqual(100, tab["line_count"])

    def test_recovery_log_projects_route_identity_from_state_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc).isoformat()
            (root / "config.yaml").write_text(
                """providers:
  provider-a:
    api_base: https://provider.example/v1
    api_keys:
      - name: key-a
        value: example-key
model_list:
  - model_name: public-chat
    litellm_params:
      model: openai/upstream-chat
      api_base: https://provider.example/v1
      api_key: example-key
    model_info:
      id: abcdef12
      supported_upstream_url_surfaces:
        - openai/responses
      upstream_url_surface: openai/responses
""",
                encoding="utf-8",
            )
            state = root / ".litellm-runtime" / "route-recovery-state.json"
            state.parent.mkdir()
            state.write_text(
                json.dumps(
                    {
                        "updated_at": now,
                        "recoveries": {
                            "route-a": {
                                "key": "route-a",
                                "status": "polling",
                                "updated_at": now,
                                "model_group": "upstream-chat",
                                "request": {
                                    "deployment_id": "abcdef12",
                                    "route_key": (
                                        "model=public-chat / provider=provider-a / "
                                        "upstream=openai/upstream-chat / key=key-a"
                                    ),
                                },
                                "attempt": 2,
                                "attempt_timeout_seconds": 120,
                                "diagnostic": {"kind": "timeout"},
                            }
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            tab = LogsDomain(root).view("recovery")["log"]
            record = tab["records"][0]

            self.assertEqual(1, tab["line_count"])
            self.assertEqual("polling", record["status"])
            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("upstream-chat", record["upstream_model"])
            self.assertEqual("provider-a", record["provider"])
            self.assertEqual("key-a", record["api_key_name"])
            self.assertEqual("attempt=2 · timeout=120s · reason=timeout", record["detail"])
            self.assertNotIn("request", record)
            self.assertNotIn("diagnostic", record)

    def test_recovery_log_uses_a_cooling_candidate_when_no_route_was_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc).isoformat()
            state = root / ".litellm-runtime" / "route-recovery-state.json"
            state.parent.mkdir()
            state.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "request-a": {
                                "updated_at": now,
                                "status": "waiting",
                                "model_group": "public-chat",
                                "cooldown_deployments": [
                                    {
                                        "id": "deployment-a",
                                        "provider": "provider-a",
                                        "model": "openai/upstream-chat",
                                        "api_key_name": "key-a",
                                        "route_key": (
                                            "model=public-chat / provider=provider-a / "
                                            "upstream=openai/upstream-chat / key=key-a"
                                        ),
                                    }
                                ],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            record = LogsDomain(root).view("recovery")["log"]["records"][0]

            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("upstream-chat", record["upstream_model"])
            self.assertEqual("provider-a", record["provider"])
            self.assertEqual("key-a", record["api_key_name"])

    def test_recovery_view_matches_live_summary_and_includes_active_cooldowns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".litellm-runtime"
            runtime.mkdir()
            now = datetime.now(timezone.utc)
            (runtime / "route-recovery-state.json").write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "live": {
                                "heartbeat_at": (now - timedelta(seconds=5)).isoformat(),
                                "status": "polling",
                                "model_group": "public-chat",
                            },
                            "stale": {
                                "heartbeat_at": (now - timedelta(minutes=5)).isoformat(),
                                "status": "polling",
                                "model_group": "stale-chat",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "deployment-cooldowns.json").write_text(
                json.dumps(
                    {
                        "cooldowns": {
                            "active": {
                                "last_failure_at": now.timestamp() - 3,
                                "cooldown_until": now.timestamp() + 60,
                                "failures": 2,
                                "route_key": (
                                    "model=public-chat / provider=provider-a / "
                                    "upstream=openai/upstream-chat / key=key-a"
                                ),
                            },
                            "expired": {
                                "cooldown_until": now.timestamp() - 60,
                                "model_group": "expired-chat",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            tab = LogsDomain(root).view("recovery")["log"]

            self.assertEqual(2, tab["line_count"])
            self.assertEqual(["cooldown", "polling"], sorted(record["status"] for record in tab["records"]))
            cooldown = next(record for record in tab["records"] if record["status"] == "cooldown")
            self.assertEqual("public-chat", cooldown["public_model"])
            self.assertEqual("upstream-chat", cooldown["upstream_model"])
            self.assertEqual("provider-a", cooldown["provider"])
            self.assertEqual("key-a", cooldown["api_key_name"])
            self.assertIn("cooldown=", cooldown["detail"])
            self.assertIn("failures=2", cooldown["detail"])
            self.assertNotIn("route_key", cooldown)
            self.assertNotIn("stale-chat", json.dumps(tab))
            self.assertNotIn("expired-chat", json.dumps(tab))

    def test_recovery_cooldown_countdown_uses_whole_seconds_and_refreshes_each_second(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".litellm-runtime"
            runtime.mkdir()
            now = 1_800_000_000.25
            (runtime / "deployment-cooldowns.json").write_text(
                json.dumps(
                    {
                        "cooldowns": {
                            "active": {
                                "cooldown_until": now + 60.75,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            domain = LogsDomain(root)

            with mock.patch("young_router.core.domains.logs.time.time", return_value=now):
                first = domain.view("recovery")
            with mock.patch("young_router.core.domains.logs.time.time", return_value=now + 1):
                second = domain.view("recovery", known_revision=first["revision"])

            self.assertEqual("cooldown=61s", first["log"]["records"][0]["detail"])
            self.assertTrue(second["changed"])
            self.assertEqual("cooldown=60s", second["log"]["records"][0]["detail"])

    def test_clear_recovery_and_cooldowns_removes_both_routing_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".litellm-runtime"
            runtime.mkdir()
            now = datetime.now(timezone.utc)
            recovery_path = runtime / "route-recovery-state.json"
            recovery_payload = {
                "recoveries": {
                    "live": {
                        "heartbeat_at": now.isoformat(),
                        "status": "waiting",
                    }
                }
            }
            recovery_path.write_text(json.dumps(recovery_payload), encoding="utf-8")
            cooldown_path = runtime / "deployment-cooldowns.json"
            cooldown_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cooldowns": {"active": {"cooldown_until": now.timestamp() + 60}},
                        "image_tool_unsupported": {"preserved": {"expires_at": now.timestamp() + 60}},
                    }
                ),
                encoding="utf-8",
            )
            domain = LogsDomain(root)

            domain.dispatch("logs.clear_recovery_and_cooldowns", {"tab": "recovery"})

            self.assertEqual({}, json.loads(recovery_path.read_text(encoding="utf-8"))["recoveries"])
            cooldown_payload = json.loads(cooldown_path.read_text(encoding="utf-8"))
            self.assertEqual({}, cooldown_payload["cooldowns"])
            self.assertEqual(
                {"preserved": {"expires_at": now.timestamp() + 60}},
                cooldown_payload["image_tool_unsupported"],
            )
            tab = domain.view("recovery")["log"]
            self.assertEqual(0, tab["line_count"])

    def test_clearing_recovery_view_hides_both_state_sources_until_they_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".litellm-runtime"
            runtime.mkdir()
            now = datetime.now(timezone.utc)
            recovery_path = runtime / "route-recovery-state.json"
            recovery_path.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "first": {
                                "heartbeat_at": now.isoformat(),
                                "status": "polling",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "deployment-cooldowns.json").write_text(
                json.dumps(
                    {
                        "cooldowns": {
                            "active": {
                                "cooldown_until": now.timestamp() + 60,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            domain = LogsDomain(root)
            self.assertEqual(2, domain.view("recovery")["log"]["line_count"])

            domain.dispatch("logs.clear", {"tab": "recovery"})

            self.assertEqual(0, domain.view("recovery")["log"]["line_count"])
            recovery_path.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "first": {
                                "heartbeat_at": now.isoformat(),
                                "status": "polling",
                            },
                            "second": {
                                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                                "status": "waiting",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(3, domain.view("recovery")["log"]["line_count"])

    def test_a_filter_is_bounded_by_the_same_unit_the_contract_states(self) -> None:
        """The contract's 256 and Core's limit are the same number of characters.

        Core counted bytes while the IPC schema counted characters, so a filter
        of 86 CJK characters passed the contract and was then refused by Core —
        with a message the pane has no translation for.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "actions.log").write_text("first action\nsecond action\n", encoding="utf-8")
            domain = LogsDomain(root)

            domain.dispatch("logs.set_filter", {"tab": "actions", "filter": "汉" * MAX_FILTER_CHARS})
            self.assertEqual(0, domain.view("actions")["log"]["line_count"])
            domain.dispatch("logs.set_filter", {"tab": "actions", "filter": ""})
            self.assertEqual(2, domain.view("actions")["log"]["line_count"])

            with self.assertRaises(LogsDomainError):
                domain.dispatch("logs.set_filter", {"tab": "actions", "filter": "汉" * (MAX_FILTER_CHARS + 1)})

    def test_pause_filter_clear_and_resume_are_view_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "actions.log"
            source.write_text("first action\nsecond action\n", encoding="utf-8")
            domain = LogsDomain(root)

            domain.dispatch("logs.set_filter", {"tab": "actions", "filter": "second"})
            self.assertEqual(1, domain.view("actions")["log"]["line_count"])
            domain.dispatch("logs.pause", {"tab": "actions"})
            self.assertTrue(domain.view("actions")["log"]["paused"])
            domain.dispatch("logs.clear", {"tab": "actions"})
            self.assertEqual(0, domain.view("actions")["log"]["line_count"])
            self.assertTrue(source.exists())
            domain.dispatch("logs.resume", {"tab": "actions"})
            self.assertEqual(1, domain.view("actions")["log"]["line_count"])

    def test_clear_while_playing_reveals_only_new_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "actions.log"
            source.write_text("first action\nsecond action\n", encoding="utf-8")
            domain = LogsDomain(root)

            self.assertEqual(2, domain.view("actions")["log"]["line_count"])
            domain.dispatch("logs.clear", {"tab": "actions"})
            self.assertEqual(0, domain.view("actions")["log"]["line_count"])
            domain.dispatch(
                "logs.record_menu_action",
                {"tab": "actions", "menu_action": "open-logs"},
            )

            view = domain.view("actions")["log"]
            self.assertEqual(1, view["line_count"])
            self.assertTrue(view["records"][0].endswith("open-logs"))

    def test_records_a_safe_timestamped_menu_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory)

            domain.dispatch(
                "logs.record_menu_action",
                {"tab": "actions", "menu_action": "open-logs"},
            )
            record = domain.view("actions")["log"]["records"][-1]

            self.assertRegex(record, r"^\[\d{4}-\d{2}-\d{2}T.*Z\] \[INFO\] open-logs$")

            for removed_or_invalid_action in ("webdav-toggle", "not-a-menu-action"):
                with self.assertRaisesRegex(ValueError, "Menu action is invalid"):
                    domain.dispatch(
                        "logs.record_menu_action",
                        {"tab": "actions", "menu_action": removed_or_invalid_action},
                    )

    def test_menu_action_log_honors_runtime_log_cap_and_backup_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text(
                "YOUNG_ROUTER_LOG_MAX_BYTES=262144\n"
                "YOUNG_ROUTER_LOG_BACKUP_SEGMENTS=1\n",
                encoding="utf-8",
            )
            action_log = root / "actions.log"
            action_log.write_bytes(b"x" * (300 * 1024))
            domain = LogsDomain(root, runtime_settings_path=settings)

            domain.dispatch(
                "logs.record_menu_action",
                {"tab": "actions", "menu_action": "open-logs"},
            )

            # The Core writes this log itself, so the configured cap and
            # backup count must apply without the proxy environment.
            self.assertTrue((root / "actions.log.1").exists())
            self.assertFalse((root / "actions.log.2").exists())
            self.assertLessEqual(action_log.stat().st_size, 262144)
            self.assertIn("open-logs", action_log.read_text(encoding="utf-8"))

    def test_menu_action_recording_is_not_a_configuration_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            core = CoreStore(domains=[LogsDomain(directory)])

            core.dispatch(
                {
                    "domain": "logs",
                    "type": "logs.record_menu_action",
                    "payload": {"tab": "actions", "menu_action": "open-logs"},
                }
            )

            self.assertFalse(core.snapshot()["drafts"]["logs"]["dirty"])

    def test_line_limit_is_bounded_and_changes_the_projected_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "actions.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
            domain = LogsDomain(root)

            domain.dispatch("logs.set_limit", {"tab": "actions", "limit": 2})
            tab = domain.view("actions")["log"]

            self.assertEqual(2, tab["limit"])
            self.assertEqual(["two", "three"], tab["records"])

    def test_core_snapshot_projects_domain_records_to_the_typed_log_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "actions.log").write_text("safe action\n", encoding="utf-8")
            core = CoreStore(domains=[LogsDomain(root)])

            tab = core.log_view("actions")["log"]

            self.assertTrue(tab["available"])
            self.assertEqual(["safe action"], tab["records"])

    def test_global_snapshot_does_not_read_or_carry_log_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server.log").write_text("safe line\n", encoding="utf-8")
            domain = LogsDomain(root)
            core = CoreStore(domains=[domain])

            with mock.patch.object(domain, "_read_lines", side_effect=AssertionError("unexpected log read")):
                snapshot = core.snapshot()

            self.assertNotIn("records", snapshot["logs"]["service"])
            self.assertNotIn("records", snapshot["domains"]["logs"]["tabs"]["service"])

    def test_unchanged_log_view_returns_only_its_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "actions.log").write_text("safe action\n", encoding="utf-8")
            domain = LogsDomain(root)

            first = domain.view("actions")
            with mock.patch.object(domain, "_read_lines", side_effect=AssertionError("unexpected repeat read")):
                second = domain.view("actions", first["revision"])

            self.assertFalse(second["changed"])
            self.assertIsNone(second["log"])

    def test_request_tab_projects_only_appended_lines_and_matches_a_cold_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "recent-requests.jsonl"
            path.write_text(
                json.dumps({"ts": "2026-08-01T04:10:00Z", "request_id": "first", "status": "success"})
                + "\n",
                encoding="utf-8",
            )
            domain = LogsDomain(root)
            domain.view("requests")

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"ts": "2026-08-01T04:10:01Z", "request_id": "second", "status": "success"})
                    + "\n"
                )
            domain._source_signatures.pop("requests", None)
            projected = [
                mock.patch.object(domain, "_read_lines", side_effect=AssertionError("unexpected full read")),
            ]
            with projected[0]:
                incremental = domain.view("requests")["log"]["records"]

            self.assertEqual(["first", "second"], [record["request_id"] for record in incremental])
            self.assertEqual(
                LogsDomain(root).view("requests")["log"]["records"],
                incremental,
            )

    def test_request_tab_reads_a_partial_line_only_after_its_newline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "recent-requests.jsonl"
            record = json.dumps({"ts": "2026-08-01T04:10:00Z", "request_id": "split", "status": "success"})
            path.write_text(record[:20], encoding="utf-8")
            domain = LogsDomain(root)

            self.assertEqual([], domain.view("requests")["log"]["records"])

            with path.open("a", encoding="utf-8") as handle:
                handle.write(record[20:] + "\n")
            domain._source_signatures.pop("requests", None)

            self.assertEqual(
                ["split"],
                [entry["request_id"] for entry in domain.view("requests")["log"]["records"]],
            )

    def test_request_tab_rebuilds_the_window_after_rotation_and_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "recent-requests.jsonl"
            path.write_text(
                json.dumps({"ts": "2026-08-01T04:10:00Z", "request_id": "before", "status": "success"})
                + "\n",
                encoding="utf-8",
            )
            domain = LogsDomain(root)
            self.assertEqual(
                ["before"],
                [record["request_id"] for record in domain.view("requests")["log"]["records"]],
            )

            rotated = root / "recent-requests.jsonl.1"
            path.rename(rotated)
            path.write_text(
                json.dumps({"ts": "2026-08-01T04:11:00Z", "request_id": "after", "status": "success"})
                + "\n",
                encoding="utf-8",
            )
            domain._source_signatures.pop("requests", None)
            self.assertEqual(
                ["after"],
                [record["request_id"] for record in domain.view("requests")["log"]["records"]],
            )

            path.write_text(
                json.dumps({"ts": "2026-08-01T04:12:00Z", "request_id": "truncated", "status": "success"})
                + "\n",
                encoding="utf-8",
            )
            domain._source_signatures.pop("requests", None)
            records = domain.view("requests")["log"]["records"]

            self.assertEqual(["truncated"], [record["request_id"] for record in records])

    def test_request_tab_view_actions_rebuild_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "recent-requests.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps({"ts": f"2026-08-01T04:10:0{index}Z", "request_id": name, "status": "success"})
                    for index, name in enumerate(("alpha", "beta"))
                )
                + "\n",
                encoding="utf-8",
            )
            domain = LogsDomain(root)
            self.assertEqual(2, domain.view("requests")["log"]["line_count"])

            domain.dispatch("logs.set_filter", {"tab": "requests", "filter": "beta"})

            self.assertEqual(
                ["beta"],
                [record["request_id"] for record in domain.view("requests")["log"]["records"]],
            )

    def test_request_tab_window_follows_a_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "recent-requests.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps({"ts": f"2026-08-01T04:1{index}:00Z", "request_id": f"row-{index}", "status": "success"})
                    for index in range(4)
                )
                + "\n",
                encoding="utf-8",
            )
            domain = LogsDomain(root)
            domain.dispatch("logs.set_limit", {"tab": "requests", "limit": 2})

            self.assertEqual(
                ["row-2", "row-3"],
                [record["request_id"] for record in domain.view("requests")["log"]["records"]],
            )

            domain.dispatch("logs.set_limit", {"tab": "requests", "limit": 4})

            self.assertEqual(
                ["row-0", "row-1", "row-2", "row-3"],
                [record["request_id"] for record in domain.view("requests")["log"]["records"]],
            )

    def test_request_tab_reprojects_when_the_configuration_revision_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                """providers:
  provider-a:
    api_base: https://provider-a.example/v1
    api_keys:
      - name: key-a
        value: key-a
model_list:
  - model_name: first-name
    litellm_params:
      model: openai/upstream-chat
      api_base: https://provider-a.example/v1
      api_key: key-a
    model_info:
      id: abcdef12
""",
                encoding="utf-8",
            )
            (root / "recent-requests.jsonl").write_text(
                json.dumps({"ts": "2026-08-01T04:10:00Z", "request_id": "row", "status": "success", "deployment_id": "abcdef12"})
                + "\n",
                encoding="utf-8",
            )
            domain = LogsDomain(root)

            self.assertEqual(
                "first-name", domain.view("requests")["log"]["records"][0]["public_model"]
            )

            config.write_text(
                config.read_text(encoding="utf-8").replace("first-name", "second-name"),
                encoding="utf-8",
            )
            domain._source_signatures.pop("requests", None)

            self.assertEqual(
                "second-name", domain.view("requests")["log"]["records"][0]["public_model"]
            )

    def test_ipc_log_view_is_separate_and_revision_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "actions.log").write_text("safe action\n", encoding="utf-8")
            core = CoreStore(domains=[LogsDomain(root)])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            snapshot = client.call("snapshot")["snapshot"]
            first = client.call("logs", {"tab": "actions"})
            second = client.call("logs", {"tab": "actions", "revision": first["revision"]})

            self.assertNotIn("records", snapshot["logs"]["actions"])
            self.assertEqual(["safe action"], first["log"]["records"])
            self.assertFalse(second["changed"])
            self.assertIsNone(second["log"])

    def test_ipc_accepts_the_online_usage_view_and_keeps_its_pages(self) -> None:
        class _UsageIPCReader:
            def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
                return [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "source_label": source_id or "",
                        "model": f"model-{index}",
                    }
                    for index in range(450)
                ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = CoreIPCServer(CoreStore(domains=[LogsDomain(root, online_usage_reader=_UsageIPCReader())]))
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            server.core.dispatch(
                {"domain": "logs", "type": "logs.refresh_online_usage", "payload": {"tab": "online-usage", "source": "relay"}}
            )
            first = client.call("logs", {"tab": "online-usage"})["log"]
            self.assertEqual((300, 1, 2), (first["line_count"], first["page"], first["page_count"]))

            server.core.dispatch(
                {"domain": "logs", "type": "logs.load_more", "payload": {"tab": "online-usage"}}
            )
            second = client.call("logs", {"tab": "online-usage"})["log"]
            self.assertEqual((450, 2, 2, 0), (second["line_count"], second["page"], second["page_count"], second["remaining"]))
            self.assertEqual([], [key for key in first if key.startswith("_")])

    def test_default_core_can_start_when_the_optional_worker_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_root = root / ".young-router"
            provider_root = runtime_root / ".litellm-runtime"
            provider_root.mkdir(parents=True)
            (provider_root / "config.yaml").write_text(
                "model_list:\n  - model_name: healthy\n    litellm_params:\n      model: openai/healthy\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"HOME": str(root), "YOUNG_ROUTER_OPENCODE2DSH_DIR": ""}):
                core = CoreStore.with_default_domains(
                    metadata_path=runtime_root / "metadata.json",
                    reset_transient_routing_state=False,
                )
                self.addCleanup(core.shutdown)
                server = CoreIPCServer(core)
                endpoint = server.start()
                self.addCleanup(server.stop)
                client = CoreIPCClient(endpoint, server.bootstrap_token)
                self.addCleanup(client.close)
                view = client.call("logs", {"tab": "online-usage"})
            self.assertEqual("online-usage", view["log"]["tab"])

    def test_online_usage_reads_nothing_until_a_relay_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = _ScopedUsageReader()
            domain = LogsDomain(directory, online_usage_reader=reader)

            self.assertFalse(domain.view("online-usage")["log"]["available"])

            # A refresh with no selection is not a request to read every relay:
            # the pane asks the user to choose one instead.
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage"})
            tab = domain.view("online-usage")["log"]

            self.assertEqual(0, tab["line_count"])
            self.assertEqual([], reader.scopes)

            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "two"})
            tab = domain.view("online-usage")["log"]

            self.assertTrue(tab["available"])
            self.assertEqual("two", tab["source_id"])
            self.assertEqual(1, tab["line_count"])

    def test_online_usage_toolbar_lists_relays_before_a_refresh(self) -> None:
        class _NeverRead:
            def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
                self.fail("A source listing must not contact a relay.")

            def sources(self) -> list[dict[str, str]]:
                return [{"id": "one", "label": "one"}]

        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_NeverRead())
            self.assertEqual(
                [{"id": "one", "label": "one"}],
                domain.view("online-usage")["log"]["sources"],
            )

    def test_clearing_online_usage_does_not_hide_the_next_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = _ChangingUsageReader()
            domain = LogsDomain(directory, online_usage_reader=reader)

            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
            domain.dispatch("logs.clear", {"tab": "online-usage"})
            self.assertEqual(0, domain.view("online-usage")["log"]["line_count"])

            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
            records = domain.view("online-usage")["log"]["records"]

            self.assertEqual(2, len(records))
            self.assertEqual("2026-01-01T00:00:02Z", records[0]["timestamp"])
            self.assertEqual("2", records[0]["total_tokens"])


    def test_online_usage_names_its_sources_and_reads_the_selected_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = _ScopedUsageReader()
            domain = LogsDomain(directory, online_usage_reader=reader)

            tab = domain.view("online-usage")["log"]
            self.assertEqual(
                [{"id": "one", "label": "relay-one"}, {"id": "two", "label": "relay-two"}],
                tab["sources"],
            )
            self.assertEqual("", tab["source_id"])

            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "two"})

            tab = domain.view("online-usage")["log"]
            self.assertEqual("two", tab["source_id"])
            self.assertEqual(["two"], sorted(set(reader.scopes)))
            self.assertIn("two rows", "\n".join(str(record) for record in tab["records"]))

    def test_online_usage_keeps_the_records_full_relay_name_and_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_StructuredUsageReader())
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})

            records = domain.view("online-usage")["log"]["records"]

            # The chosen relay answers for itself alone.
            self.assertEqual(1, len(records))
            first = records[0]
            # The view carries the station's full identity and every field it
            # reported, so the table can render a row without re-parsing text.
            self.assertEqual("flux-code", first["source_label"])
            self.assertEqual("NewAPI", first["source_family"])
            self.assertEqual("gpt-5", first["model"])
            self.assertEqual("300", first["total_tokens"])
            self.assertEqual("channel-one", first["channel"])
            # The detail a double-click opens is the same record, not a
            # re-flattened line of it.
            self.assertIsInstance(first, dict)

    def test_a_relay_with_no_usage_feed_states_why_rather_than_reporting_a_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_SilentUsageReader())
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})

            tab = domain.view("online-usage")["log"]

            # No usage was reported, so the pane must show no rows at all: a
            # row shaped like a usage record would read as usage the relay
            # never reported.
            self.assertEqual([], tab["records"])
            self.assertEqual(0, tab["line_count"])
            # The reason is a code the pane phrases, never a row and never a
            # sentence that only reads correctly in English.
            self.assertEqual("unsupported", tab["reason"])

    def test_online_usage_refresh_without_a_scope_keeps_the_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = _ScopedUsageReader()
            domain = LogsDomain(directory, online_usage_reader=reader)

            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "two"})
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage"})

            self.assertEqual(["two"], sorted(set(reader.scopes)))
            self.assertEqual("two", domain.view("online-usage")["log"]["source_id"])

    def test_default_core_resolves_usage_sources_from_the_provider_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory) / "home" / ".young-router"
            provider_root = runtime_root / ".litellm-runtime"
            provider_root.mkdir(parents=True)
            (provider_root / "config.yaml").write_text(
                """\
providers:
  relay:
    api_base: http://127.0.0.1:45678/v1
    api_keys:
      - name: default
        value: usage-key
model_list:
  - model_name: shared-model
    litellm_params:
      model: openai/shared-model
      api_base: http://127.0.0.1:45678/v1
    model_info:
      provider: relay
      api_key_name: default
""",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"HOME": str(runtime_root.parent)}):
                core = CoreStore.with_default_domains(
                    metadata_path=runtime_root / "metadata.json",
                    reset_transient_routing_state=False,
                )
                sources = core.log_view("online-usage")["log"]["sources"]

            self.assertEqual(["relay"], [source["label"] for source in sources])

    def test_online_usage_pages_move_over_fetched_rows_in_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = _PagedUsageReader()
            with mock.patch("young_router.core.domains.logs.ONLINE_USAGE_PAGE_ROWS", 3):
                domain = LogsDomain(directory, online_usage_reader=reader)
                domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
                tab = domain.view("online-usage")["log"]
                self.assertEqual(5, tab["total"])
                self.assertEqual((1, 2, 3), (tab["page"], tab["page_count"], tab["line_count"]))
                self.assertEqual(["model-2", "model-3", "model-4"], [row["model"] for row in tab["records"]])

                domain.dispatch("logs.load_more", {"tab": "online-usage"})
                tab = domain.view("online-usage")["log"]
                self.assertEqual((2, 2, 5), (tab["page"], tab["page_count"], tab["line_count"]))
                self.assertEqual("model-0", tab["records"][0]["model"])
                self.assertEqual(0, tab["remaining"])

                domain.dispatch("logs.previous_page", {"tab": "online-usage"})
                tab = domain.view("online-usage")["log"]
                self.assertEqual((1, 2, 3), (tab["page"], tab["page_count"], tab["line_count"]))
                self.assertEqual("model-2", tab["records"][0]["model"])
                self.assertEqual(2, tab["remaining"])

            self.assertEqual(1, reader.calls)

    def test_online_usage_first_page_carries_three_hundred_rows(self) -> None:
        class _LargeUsageReader:
            def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
                return [
                    {"timestamp": "2026-01-01T00:00:00Z", "source_label": source_id or "", "model": str(index)}
                    for index in range(450)
                ]

        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_LargeUsageReader())
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
            tab = domain.view("online-usage")["log"]

            self.assertGreaterEqual(tab["line_count"], 300)
            self.assertEqual(tab["line_count"] + tab["remaining"], tab["total"])
            self.assertEqual((450, 1, 2), (tab["total"], tab["page"], tab["page_count"]))

            domain.dispatch("logs.load_more", {"tab": "online-usage"})
            next_page = domain.view("online-usage")["log"]
            self.assertEqual(450, next_page["line_count"])
            self.assertEqual((2, 2, 0), (next_page["page"], next_page["page_count"], next_page["remaining"]))

    def test_a_byte_trimmed_online_usage_page_does_not_hide_following_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = _PagedUsageReader()
            with mock.patch("young_router.core.domains.logs.ONLINE_USAGE_PAGE_ROWS", 3), \
                mock.patch("young_router.core.domains.logs.MAX_VIEW_BYTES", 100):
                domain = LogsDomain(directory, online_usage_reader=reader)
                domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
                tab = domain.view("online-usage")["log"]
                self.assertLess(tab["line_count"], 3)
                self.assertEqual(2, tab["remaining"])
                self.assertLess(tab["line_count"], 3)

                domain.dispatch("logs.load_more", {"tab": "online-usage"})
                tab = domain.view("online-usage")["log"]
                self.assertEqual((2, 0), (tab["page"], tab["remaining"]))

    def test_online_usage_time_range_filters_rows_and_resets_pages(self) -> None:
        class _TimedUsageReader:
            def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
                return [
                    {"timestamp": f"2026-01-0{day}T12:00:00Z", "model": str(day)}
                    for day in range(1, 5)
                ]

        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_TimedUsageReader())
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
            domain.dispatch("logs.set_time_range", {
                "tab": "online-usage",
                "since": 1767319200,
                "until": 1767491999,
            })
            tab = domain.view("online-usage")["log"]
            self.assertEqual(["2", "3"], [row["model"] for row in tab["records"]])
            self.assertEqual((2, 1), (tab["total"], tab["page"]))
            domain.dispatch("logs.set_time_range", {"tab": "online-usage"})
            restored = domain.view("online-usage")["log"]
            self.assertEqual(4, restored["total"])
            self.assertEqual(1, restored["page"])

    def test_online_usage_time_range_accepts_a_millisecond_epoch(self) -> None:
        """The pane sends Date.getTime(); the rows are Unix seconds.

        A range compared at the wrong scale is not a filter that shows nothing
        by accident: it excludes every row, so the tab looked empty for a range
        the data clearly falls in.
        """

        class _TimedUsageReader:
            def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
                return [
                    {"timestamp": f"2026-01-0{day}T12:00:00Z", "model": str(day)}
                    for day in range(1, 5)
                ]

        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_TimedUsageReader())
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
            domain.dispatch("logs.set_time_range", {
                "tab": "online-usage",
                "since": 1767319200 * 1000,
                "until": 1767491999 * 1000,
            })
            tab = domain.view("online-usage")["log"]
            self.assertEqual(["2", "3"], [row["model"] for row in tab["records"]])
            self.assertEqual(2, tab["total"])

    def test_online_usage_time_range_survives_a_relay_switch(self) -> None:
        """The range is a view over whichever relay answered.

        A switch used to clear it, so the pane's own time menu kept naming a
        range that no longer filtered the rows underneath it.
        """

        class _TwoSourceUsageReader:
            def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
                day = 1 if source_id == "one" else 3
                return [
                    {"timestamp": f"2026-01-0{day}T12:00:00Z", "model": f"{source_id}-{day}"},
                    {"timestamp": "2026-01-04T12:00:00Z", "model": f"{source_id}-4"},
                ]

        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_TwoSourceUsageReader())
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
            domain.dispatch("logs.set_time_range", {
                "tab": "online-usage",
                "since": 1767319200,
                "until": 1767491999,
            })
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "two"})
            tab = domain.view("online-usage")["log"]
            self.assertEqual(["two-3"], [row["model"] for row in tab["records"]])
            self.assertEqual(1, tab["total"])

    def test_online_usage_date_only_timestamp_is_a_calendar_day(self) -> None:
        class _DateUsageReader:
            def refresh(self, source_id: str | None = None) -> list[dict[str, str]]:
                return [
                    {"timestamp": "2026-10-17", "model": "yesterday"},
                    {"timestamp": "2026-10-18", "model": "today"},
                ]

        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_DateUsageReader())
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
            domain.dispatch("logs.set_time_range", {
                "tab": "online-usage",
                "since": datetime(2026, 10, 18).timestamp(),
                "until": datetime(2026, 10, 18, 23, 59, 59).timestamp(),
            })
            tab = domain.view("online-usage")["log"]
            self.assertEqual(["today"], [row["model"] for row in tab["records"]])

    def test_online_usage_keeps_the_files_structured_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory)

            with mock.patch(
                "young_router.core.remote_usage_logs.collect",
                return_value=(
                    [{
                        "timestamp": "2026-07-18T10:00:00Z",
                        "source": "sub2api",
                        "source_id": "one",
                        "source_label": "relay",
                        "source_family": "Sub2API",
                        "model": "gpt-5",
                        "prompt_tokens": "100",
                        "completion_tokens": "20",
                        "total_tokens": "120",
                    }],
                    "",
                ),
            ) as collect:
                domain.dispatch(
                    "logs.refresh_online_usage",
                    {"tab": "online-usage", "source": "one"},
                )

            collect.assert_called_once_with(domain.config_path, 5.0, "one")
            record = domain.view("online-usage")["log"]["records"][0]
            self.assertEqual("2026-07-18T10:00:00Z", record["timestamp"])
            self.assertEqual("relay", record["source_label"])
            self.assertEqual("Sub2API", record["source_family"])
            self.assertEqual("gpt-5", record["model"])
            self.assertEqual("120", record["total_tokens"])

    def test_online_usage_latest_choice_wins_when_reads_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = _OverlappingUsageReader()
            domain = LogsDomain(directory, online_usage_reader=reader)
            slow = threading.Thread(target=lambda: domain.dispatch(
                "logs.refresh_online_usage",
                {"tab": "online-usage", "source": "slow", "request": "request-one"},
            ))
            slow.start()
            self.assertTrue(reader.started.wait(1))

            domain.dispatch(
                "logs.refresh_online_usage",
                {"tab": "online-usage", "source": "fast", "request": "request-two"},
            )
            reader.release.set()
            slow.join(2)

            tab = domain.view("online-usage")["log"]
            self.assertEqual("fast", tab["source_id"])
            self.assertEqual("fast model", tab["records"][0]["model"])


if __name__ == "__main__":
    unittest.main()

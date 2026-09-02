from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from litellm_menu.core import CoreIPCClient, CoreIPCServer, CoreStore
from litellm_menu.core.domains.logs import MAX_VIEW_BYTES, LOG_TABS, LogsDomain
from litellm_menu.core.service import LOG_TABS as CORE_LOG_TABS


class _UsageReader:
    def refresh(self) -> list[str]:
        return ["Updated 2026-01-01T00:00:00Z", "default-chat tokens=12"]


class _ChangingUsageReader:
    def __init__(self) -> None:
        self.calls = 0

    def refresh(self) -> list[str]:
        self.calls += 1
        return [f"Updated 2026-01-01T00:00:0{self.calls}Z", f"default-chat tokens={self.calls}"]


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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
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
            settings.write_text("LITELLM_MENU_LOG_VIEW_LIMIT=2\n", encoding="utf-8")
            previous = root / "menu-server.log.1"
            current = root / "menu-server.log"
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
            previous = root / "menu-server.log.1"
            current = root / "menu-server.log"
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
                f"litellm_route_trace {json.dumps(payload)}\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("route-trace")["log"]["records"][0]

            self.assertEqual("public-chat", record["public_model"])
            self.assertEqual("upstream-chat", record["upstream_model"])

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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

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

    def test_service_log_omits_litellm_banner_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menu-server.log").write_text(
                "\n".join(
                    [
                        "[2026-08-01T04:10:11Z] #----------------------#",
                        "[2026-08-01T04:10:11Z] # Give Feedback / Get Help: https://github.com/BerriAI/litellm/issues/new #",
                        "[2026-08-01T04:10:11Z] openai/example-model",
                        "[2026-08-01T04:10:12Z] INFO: Started server process [123]",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = LogsDomain(root).view("service")["log"]["records"]

            self.assertEqual(["[2026-08-01T04:10:12Z] INFO: Started server process [123]"], records)

    def test_service_traceback_lines_are_one_selectable_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menu-server.log").write_text(
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
            records = [
                {"ts": "2026-08-01T04:10:10Z", "request_id": "completed", "status": "pending"},
                {"ts": "2026-08-01T04:10:11Z", "request_id": "completed", "status": "stream"},
                {"ts": "2026-08-01T04:10:12Z", "request_id": "completed", "status": "success"},
                {"ts": "2026-08-01T04:10:13Z", "request_id": "completed", "status": "stream"},
                {"ts": "2026-08-01T04:10:20Z", "request_id": "retried", "status": "failure"},
                {"ts": "2026-08-01T04:10:21Z", "request_id": "retried", "status": "pending"},
                {"ts": "2026-08-01T04:10:22Z", "request_id": "retried", "status": "stream"},
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-server.log").write_text(
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
            (root / "menu-actions.log").write_text(
                "\n".join(f"line-{index}" for index in range(150)) + "\n",
                encoding="utf-8",
            )
            domain = LogsDomain(root, runtime_settings_path=settings)

            self.assertEqual(10_000, domain.view("menu")["log"]["limit"])
            settings.write_text("LITELLM_MENU_LOG_VIEW_LIMIT=100\n", encoding="utf-8")

            tab = domain.view("menu")["log"]
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

            with mock.patch("litellm_menu.core.domains.logs.time.time", return_value=now):
                first = domain.view("recovery")
            with mock.patch("litellm_menu.core.domains.logs.time.time", return_value=now + 1):
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

    def test_pause_filter_clear_and_resume_are_view_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "menu-actions.log"
            source.write_text("first action\nsecond action\n", encoding="utf-8")
            domain = LogsDomain(root)

            domain.dispatch("logs.set_filter", {"tab": "menu", "filter": "second"})
            self.assertEqual(1, domain.view("menu")["log"]["line_count"])
            domain.dispatch("logs.pause", {"tab": "menu"})
            self.assertTrue(domain.view("menu")["log"]["paused"])
            domain.dispatch("logs.clear", {"tab": "menu"})
            self.assertEqual(0, domain.view("menu")["log"]["line_count"])
            self.assertTrue(source.exists())
            domain.dispatch("logs.resume", {"tab": "menu"})
            self.assertEqual(1, domain.view("menu")["log"]["line_count"])

    def test_clear_while_playing_reveals_only_new_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "menu-actions.log"
            source.write_text("first action\nsecond action\n", encoding="utf-8")
            domain = LogsDomain(root)

            self.assertEqual(2, domain.view("menu")["log"]["line_count"])
            domain.dispatch("logs.clear", {"tab": "menu"})
            self.assertEqual(0, domain.view("menu")["log"]["line_count"])
            domain.dispatch(
                "logs.record_menu_action",
                {"tab": "menu", "menu_action": "open-logs"},
            )

            view = domain.view("menu")["log"]
            self.assertEqual(1, view["line_count"])
            self.assertTrue(view["records"][0].endswith("open-logs"))

    def test_records_a_safe_timestamped_menu_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory)

            domain.dispatch(
                "logs.record_menu_action",
                {"tab": "menu", "menu_action": "open-logs"},
            )
            record = domain.view("menu")["log"]["records"][-1]

            self.assertRegex(record, r"^\[\d{4}-\d{2}-\d{2}T.*Z\] \[INFO\] open-logs$")

            for removed_or_invalid_action in ("webdav-toggle", "not-a-menu-action"):
                with self.assertRaisesRegex(ValueError, "Menu action is invalid"):
                    domain.dispatch(
                        "logs.record_menu_action",
                        {"tab": "menu", "menu_action": removed_or_invalid_action},
                    )

    def test_menu_action_recording_is_not_a_configuration_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            core = CoreStore(domains=[LogsDomain(directory)])

            core.dispatch(
                {
                    "domain": "logs",
                    "type": "logs.record_menu_action",
                    "payload": {"tab": "menu", "menu_action": "open-logs"},
                }
            )

            self.assertFalse(core.snapshot()["drafts"]["logs"]["dirty"])

    def test_line_limit_is_bounded_and_changes_the_projected_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menu-actions.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
            domain = LogsDomain(root)

            domain.dispatch("logs.set_limit", {"tab": "menu", "limit": 2})
            tab = domain.view("menu")["log"]

            self.assertEqual(2, tab["limit"])
            self.assertEqual(["two", "three"], tab["records"])

    def test_core_snapshot_projects_domain_records_to_the_typed_log_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menu-actions.log").write_text("safe action\n", encoding="utf-8")
            core = CoreStore(domains=[LogsDomain(root)])

            tab = core.log_view("menu")["log"]

            self.assertTrue(tab["available"])
            self.assertEqual(["safe action"], tab["records"])

    def test_global_snapshot_does_not_read_or_carry_log_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menu-server.log").write_text("safe line\n", encoding="utf-8")
            domain = LogsDomain(root)
            core = CoreStore(domains=[domain])

            with mock.patch.object(domain, "_read_lines", side_effect=AssertionError("unexpected log read")):
                snapshot = core.snapshot()

            self.assertNotIn("records", snapshot["logs"]["service"])
            self.assertNotIn("records", snapshot["domains"]["logs"]["tabs"]["service"])

    def test_unchanged_log_view_returns_only_its_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menu-actions.log").write_text("safe action\n", encoding="utf-8")
            domain = LogsDomain(root)

            first = domain.view("menu")
            with mock.patch.object(domain, "_read_lines", side_effect=AssertionError("unexpected repeat read")):
                second = domain.view("menu", first["revision"])

            self.assertFalse(second["changed"])
            self.assertIsNone(second["log"])

    def test_ipc_log_view_is_separate_and_revision_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menu-actions.log").write_text("safe action\n", encoding="utf-8")
            core = CoreStore(domains=[LogsDomain(root)])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            snapshot = client.call("snapshot")["snapshot"]
            first = client.call("logs", {"tab": "menu"})
            second = client.call("logs", {"tab": "menu", "revision": first["revision"]})

            self.assertNotIn("records", snapshot["logs"]["menu"])
            self.assertEqual(["safe action"], first["log"]["records"])
            self.assertFalse(second["changed"])
            self.assertIsNone(second["log"])

    def test_online_usage_is_loaded_only_after_explicit_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_UsageReader())

            self.assertFalse(domain.view("online-usage")["log"]["available"])
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage"})
            tab = domain.view("online-usage")["log"]

            self.assertTrue(tab["available"])
            self.assertEqual(2, tab["line_count"])

    def test_clearing_online_usage_does_not_hide_the_next_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = _ChangingUsageReader()
            domain = LogsDomain(directory, online_usage_reader=reader)

            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage"})
            domain.dispatch("logs.clear", {"tab": "online-usage"})
            self.assertEqual(0, domain.view("online-usage")["log"]["line_count"])

            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage"})
            records = domain.view("online-usage")["log"]["records"]

            self.assertEqual(2, len(records))
            self.assertIn("00:00:02Z", "\n".join(records))


if __name__ == "__main__":
    unittest.main()

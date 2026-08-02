from __future__ import annotations

import json
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

    def test_route_trace_view_stays_below_ipc_message_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                f"[2026-08-01T04:10:{index % 60:02d}Z] litellm_route_trace "
                + json.dumps(
                    {
                        "timestamp": "2026-08-01T04:10:11Z",
                        "event": "deployment_selected",
                        "request_preview": "x" * 1800,
                        "selected_candidates": [{"id": f"deployment-{index}", "healthy": True}],
                    }
                )
                for index in range(3000)
            ]
            (root / "menu-server.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

            view = LogsDomain(root).view("route-trace")
            encoded = json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

            self.assertLessEqual(len(encoded), MAX_VIEW_BYTES)
            self.assertEqual("deployment-2999", view["log"]["records"][-1]["selected_candidates"][0]["id"])

    def test_request_log_projects_the_credential_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recent-requests.jsonl").write_text(
                json.dumps(
                    {
                        "ts": "2026-08-01T04:10:11Z",
                        "status": "ok",
                        "model_group": "default-chat",
                        "provider": "provider-a",
                        "api_key_name": "credential-a",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            record = LogsDomain(root).view("requests")["log"]["records"][0]

            self.assertEqual("credential-a", record["api_key_name"])

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

    def test_recovery_log_expands_the_multiline_state_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".litellm-runtime" / "route-recovery-state.json"
            state.parent.mkdir()
            state.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-08-01T04:10:11Z",
                        "recoveries": {
                            "route-a": {
                                "key": "route-a",
                                "status": "polling",
                                "updated_at": "2026-08-01T04:10:11Z",
                                "model_group": "default-chat",
                            }
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            tab = LogsDomain(root).view("recovery")["log"]

            self.assertEqual(1, tab["line_count"])
            self.assertEqual("polling", tab["records"][0]["status"])
            self.assertEqual("default-chat", tab["records"][0]["model_group"])

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


if __name__ == "__main__":
    unittest.main()

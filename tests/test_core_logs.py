from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from litellm_menu.core import CoreStore
from litellm_menu.core.domains.logs import LOG_TABS, LogsDomain
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

            snapshot_text = json.dumps(domain.snapshot())

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

            records = LogsDomain(root).snapshot()["tabs"]["service"]["records"]
            projected = json.dumps(records)

            self.assertNotIn(token, projected)
            self.assertNotIn(password, projected)
            self.assertNotIn(api_key, projected)
            self.assertIn("token=configured", projected)
            self.assertIn("password: configured", projected)
            self.assertIn("ANTHROPIC_AUTH_TOKEN=configured", projected)

    def test_pause_filter_clear_and_resume_are_view_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "menu-actions.log"
            source.write_text("first action\nsecond action\n", encoding="utf-8")
            domain = LogsDomain(root)

            domain.dispatch("logs.set_filter", {"tab": "menu", "filter": "second"})
            self.assertEqual(1, domain.snapshot()["tabs"]["menu"]["line_count"])
            domain.dispatch("logs.pause", {"tab": "menu"})
            self.assertTrue(domain.snapshot()["tabs"]["menu"]["paused"])
            domain.dispatch("logs.clear", {"tab": "menu"})
            self.assertEqual(0, domain.snapshot()["tabs"]["menu"]["line_count"])
            self.assertTrue(source.exists())
            domain.dispatch("logs.resume", {"tab": "menu"})
            self.assertEqual(1, domain.snapshot()["tabs"]["menu"]["line_count"])

    def test_line_limit_is_bounded_and_changes_the_projected_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menu-actions.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
            domain = LogsDomain(root)

            domain.dispatch("logs.set_limit", {"tab": "menu", "limit": 2})
            tab = domain.snapshot()["tabs"]["menu"]

            self.assertEqual(2, tab["limit"])
            self.assertEqual(["two", "three"], tab["records"])

    def test_core_snapshot_projects_domain_records_to_the_typed_log_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menu-actions.log").write_text("safe action\n", encoding="utf-8")
            core = CoreStore(domains=[LogsDomain(root)])

            tab = core.snapshot()["logs"]["menu"]

            self.assertTrue(tab["available"])
            self.assertEqual(["safe action"], tab["records"])

    def test_online_usage_is_loaded_only_after_explicit_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_UsageReader())

            self.assertFalse(domain.snapshot()["tabs"]["online-usage"]["available"])
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage"})
            tab = domain.snapshot()["tabs"]["online-usage"]

            self.assertTrue(tab["available"])
            self.assertEqual(2, tab["line_count"])


if __name__ == "__main__":
    unittest.main()

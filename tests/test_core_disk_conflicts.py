from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from litellm_menu.core import ConfirmationNeeded, CoreStore
from litellm_menu.core.domains.claude import ClaudeSettingsDomain
from litellm_menu.core.domains.providers_models import ProvidersModelsDomain
from litellm_menu.core.domains.runtime import RuntimeSettingsDomain
from litellm_menu.core.domains.webdav import WebDAVSettingsDomain
from litellm_menu.core.service import RecoverableDomain


class CoreDiskConflictTests(unittest.TestCase):
    def test_confirmation_required_does_not_checkpoint_or_restore_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"model":"baseline"}\n', encoding="utf-8")
            core = CoreStore(domains=[ClaudeSettingsDomain(path)])
            core.dispatch(
                {
                    "domain": "claude",
                    "type": "patch",
                    "payload": {"model": "draft"},
                }
            )
            external = '{"model":"external"}\n'
            path.write_text(external, encoding="utf-8")

            with (
                mock.patch("litellm_menu.core.service._checkpoint_files") as checkpoints,
                self.assertRaises(ConfirmationNeeded) as raised,
            ):
                core.apply("claude", revision=core.revision)

            self.assertEqual(("overwrite_external_claude",), raised.exception.codes)
            checkpoints.assert_not_called()
            self.assertEqual(external, path.read_text(encoding="utf-8"))

    def test_each_observed_external_write_advances_conflict_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"model":"baseline"}\n', encoding="utf-8")
            core = CoreStore(domains=[ClaudeSettingsDomain(path)])
            core.dispatch(
                {
                    "domain": "claude",
                    "type": "patch",
                    "payload": {"model": "draft"},
                }
            )

            path.write_text('{"model":"external-one"}\n', encoding="utf-8")
            first = core.snapshot()
            path.write_text('{"model":"external-two"}\n', encoding="utf-8")
            second = core.snapshot()

            self.assertTrue(first["disk"]["claude"]["changed"])
            self.assertEqual(1, first["disk"]["claude"]["generation"])
            self.assertTrue(second["disk"]["claude"]["changed"])
            self.assertEqual(2, second["disk"]["claude"]["generation"])
            self.assertEqual("draft", second["domains"]["claude"]["settings"]["model"])
            self.assertEqual(
                {"changed", "generation", "keep_draft"},
                set(second["disk"]["claude"]),
            )
            self.assertNotIn(str(path), json.dumps(second))

    def test_clean_auto_reload_keeps_generation_and_event_revision_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"model":"baseline"}\n', encoding="utf-8")
            core = CoreStore(domains=[ClaudeSettingsDomain(path)])
            events: list[dict[str, object]] = []
            unsubscribe = core.subscribe(events.append)
            self.addCleanup(unsubscribe)

            path.write_text('{"model":"external"}\n', encoding="utf-8")
            core._emit()

            self.assertEqual(1, len(events))
            event = events[0]
            snapshot = event["snapshot"]
            self.assertIsInstance(snapshot, dict)
            assert isinstance(snapshot, dict)
            self.assertEqual(event["revision"], snapshot["revision"])
            self.assertEqual(core.revision, snapshot["revision"])
            self.assertEqual(
                {"changed": False, "generation": 1, "keep_draft": False},
                snapshot["disk"]["claude"],
            )
            self.assertEqual("external", snapshot["domains"]["claude"]["settings"]["model"])

            unchanged = core.snapshot()
            self.assertEqual(1, unchanged["disk"]["claude"]["generation"])
            self.assertEqual(
                {"changed", "generation", "keep_draft"},
                set(unchanged["disk"]["claude"]),
            )

    def test_invalid_claude_source_recovers_when_disk_file_becomes_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"model":', encoding="utf-8")
            factory = lambda: ClaudeSettingsDomain(path)
            core = CoreStore(domains=[RecoverableDomain("claude", factory)])

            unavailable = core.snapshot()
            self.assertFalse(unavailable["domains"]["claude"]["available"])

            path.write_text('{"model":"recovered"}\n', encoding="utf-8")
            recovered = core.snapshot()

            self.assertEqual("recovered", recovered["domains"]["claude"]["settings"]["model"])
            self.assertEqual(1, recovered["disk"]["claude"]["generation"])
            self.assertFalse(recovered["disk"]["claude"]["changed"])
            self.assertIn('"recovered"', core.trusted_editor_text("claude", "settings", revision=core.revision))

    def test_clean_external_changes_auto_reload_every_editable_domain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            providers_path = root / "config.yaml"
            providers_path.write_text("model_list: []\n", encoding="utf-8")
            runtime_path = root / "runtime-settings.env"
            runtime_path.write_text("LITELLM_PORT=4100\n", encoding="utf-8")
            webdav_path = root / "webdav.json"
            enabled_path = root / "webdav.enabled"
            core = CoreStore(
                domains=[
                    ProvidersModelsDomain(providers_path),
                    RuntimeSettingsDomain(runtime_path),
                    WebDAVSettingsDomain(webdav_path, enabled_path=enabled_path),
                ]
            )

            providers_path.write_text("model_list: []\nlitellm_settings: {}\n", encoding="utf-8")
            runtime_path.write_text("LITELLM_PORT=4200\n", encoding="utf-8")
            webdav_path.write_text('{"url":"https://example.test/webdav/","remote_name":"config.json"}\n', encoding="utf-8")
            enabled_path.write_text("1\n", encoding="utf-8")
            snapshot = core.snapshot()

            for name in ("providers_models", "runtime", "webdav"):
                self.assertFalse(snapshot["disk"][name]["changed"])
                self.assertEqual(1, snapshot["disk"][name]["generation"])
                self.assertFalse(snapshot["drafts"][name]["dirty"])
            self.assertEqual("4200", snapshot["domains"]["runtime"]["values"]["LITELLM_PORT"])
            self.assertTrue(snapshot["domains"]["webdav"]["enabled"])

    def test_provider_disk_state_reports_a_deleted_source_file_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("model_list: []\n", encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            path.unlink()

            state = domain.external_disk_state()
            self.assertTrue(state["changed"])
            self.assertFalse(state["exists"])

    def test_dirty_external_changes_keep_draft_and_require_explicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_path = root / "runtime-settings.env"
            runtime_path.write_text("LITELLM_PORT=4100\n", encoding="utf-8")
            domain = RuntimeSettingsDomain(runtime_path)
            core = CoreStore(domains=[domain])
            core.dispatch({"domain": "runtime", "type": "set_setting", "payload": {"key": "LITELLM_PORT", "value": "4300"}})
            runtime_path.write_text("LITELLM_PORT=4200\n", encoding="utf-8")

            snapshot = core.snapshot()
            self.assertTrue(snapshot["disk"]["runtime"]["changed"])
            self.assertEqual("4300", snapshot["domains"]["runtime"]["values"]["LITELLM_PORT"])
            with self.assertRaises(ConfirmationNeeded) as raised:
                core.apply("runtime", revision=core.revision)
            self.assertEqual(("overwrite_external_runtime",), raised.exception.codes)

            applied = core.apply(
                "runtime",
                revision=core.revision,
                confirmation=["overwrite_external_runtime"],
            )
            self.assertTrue(applied["applied"])
            self.assertIn("LITELLM_PORT=4300", runtime_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

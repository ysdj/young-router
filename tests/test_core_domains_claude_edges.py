from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from young_router.claude_aliases import claude_desktop_alias
from young_router.core.claude_desktop import ClaudeDesktopConfig
from young_router.core.domains.claude import ClaudeSettingsDomain, redact

_ENV_KEY = "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK"


def _write_desktop_config(root: pathlib.Path, config: dict[str, object]) -> None:
    config_id = "synthetic-config"
    root.mkdir(parents=True)
    (root / "_meta.json").write_text(
        json.dumps({"appliedId": config_id, "entries": [{"id": config_id, "name": "Synthetic"}]}),
        encoding="utf-8",
    )
    (root / f"{config_id}.json").write_text(json.dumps(config), encoding="utf-8")


class ClaudeDesktopRetirementFailureTests(unittest.TestCase):
    def test_a_failed_retirement_leaves_the_old_list_in_the_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root, {"inferenceModels": [{"name": "claude-opus-5-5"}]}
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            generated = claude_desktop_alias("public-a")
            domain._desktop.set_model_names([generated])
            domain._desktop.apply()
            with mock.patch.object(
                ClaudeDesktopConfig, "apply", side_effect=RuntimeError("changed")
            ):
                domain.reload()
            self.assertEqual([generated], domain.snapshot()["desktop"]["model_names"])
            self.assertFalse(domain._desktop.is_dirty())


class ClaudeDesktopPreferenceFailureTests(unittest.TestCase):
    def test_a_failed_preference_leaves_the_profile_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            failed = mock.patch.object(
                ClaudeDesktopConfig, "apply", side_effect=RuntimeError("changed")
            )
            with (
                mock.patch.object(domain, "desktop_uses_local_api", return_value=True),
                mock.patch.object(domain, "_publishes_one_million_context", return_value=True),
                failed,
            ):
                domain._prefer_one_million_context()
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertNotIn("modelPrefer1mContext", config)
            self.assertFalse(domain._desktop.is_dirty())


class ClaudeNonstreamFallbackRefusalTests(unittest.TestCase):
    def test_a_refused_toggle_leaves_both_documents_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "old"}}), encoding="utf-8"
            )
            domain = ClaudeSettingsDomain(path)
            path.write_text(
                json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "new"}}), encoding="utf-8"
            )
            with self.assertRaises(Exception):
                domain.dispatch("claude.nonstream_fallback.set", {"enabled": True})
            self.assertFalse(
                domain.snapshot()["clients"]["claudeCode"][
                    "nonstream_fallback_enabled"
                ]
            )
            draft = json.loads(domain.raw_text(include_sensitive=True))
            self.assertNotIn(_ENV_KEY, draft["env"])


class ClaudeStructuredPatchNullClearTests(unittest.TestCase):
    def test_null_permission_and_sandbox_blocks_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "permissions": {"defaultMode": "default"},
                        "sandbox": {"enabled": True},
                    }
                ),
                encoding="utf-8",
            )
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"permissions": None, "sandbox": None})
            draft = json.loads(domain.raw_text(include_sensitive=True))
            self.assertNotIn("permissions", draft)
            self.assertNotIn("sandbox", draft)

    def test_null_nested_sandbox_blocks_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "enabled": True,
                            "filesystem": {"disabled": True},
                            "network": {"httpProxyPort": 8080},
                        }
                    }
                ),
                encoding="utf-8",
            )
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"sandbox": {"filesystem": None, "network": None}})
            draft = json.loads(domain.raw_text(include_sensitive=True))
            self.assertEqual({"enabled": True}, draft["sandbox"])


class ClaudeRedactedGatewayUrlTests(unittest.TestCase):
    def test_redaction_withholds_a_gateway_url_with_query_credentials(self) -> None:
        clean = "https://gateway.example.test/v1"
        value = redact({"env": {"ANTHROPIC_BASE_URL": f"{clean}?token=synthetic-token"}})
        self.assertNotIn("synthetic-token", json.dumps(value))
        self.assertEqual("configured", value["env"]["ANTHROPIC_BASE_URL"])
        self.assertEqual(
            {"env": {"ANTHROPIC_BASE_URL": clean}},
            redact({"env": {"ANTHROPIC_BASE_URL": clean}}),
        )

    def test_redaction_withholds_a_desktop_gateway_url_with_query_credentials(self) -> None:
        value = redact(
            {"config": {"inferenceGatewayBaseUrl": "https://gateway.example.test/v1?key=synthetic-key"}}
        )
        self.assertEqual("configured", value["config"]["inferenceGatewayBaseUrl"])


class ClaudeSubMillionWindowTests(unittest.TestCase):
    def test_a_users_window_on_a_sub_million_route_survives_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            domain = ClaudeSettingsDomain(root / "settings.json")
            model_id = claude_desktop_alias("gpt-6.1-sol")
            with mock.patch.object(
                domain,
                "_public_model_rows",
                return_value=[{"id": "gpt-6.1-sol", "max_input_tokens": 500_000}],
            ):
                settings = domain._code_model_settings(
                    ["gpt-6.1-sol"],
                    {model_id: {"autoCompactWindow": 300_000, "effortLevel": "high"}},
                )
            self.assertEqual(300_000, settings[model_id]["autoCompactWindow"])
            self.assertEqual("high", settings[model_id]["effortLevel"])


if __name__ == "__main__":
    unittest.main()

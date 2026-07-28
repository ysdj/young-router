from __future__ import annotations

import json
import os
import pathlib
import stat
import tempfile
import unittest

from litellm_menu.core import ConfirmationNeeded, CoreStore
from litellm_menu.core.domains.claude import (
    ClaudeSettingsDomain,
    ClaudeSettingsError,
    ConfirmationRequired,
)
from litellm_menu.core.domains.language import (
    LANGUAGE_OPTIONS,
    LanguageSettingsDomain,
    LanguageSettingsError,
    create_translator,
    resolve_language,
)


class ClaudeSettingsDomainTests(unittest.TestCase):
    def test_core_registers_both_domains_and_keeps_apply_confirmation_at_the_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            core = CoreStore.with_default_domains(
                claude_settings_path=root / "claude" / "settings.json",
                language_path=root / "language.json",
            )
            self.assertIn("claude", core.domains)
            self.assertIn("language", core.domains)
            core.dispatch(
                {
                    "type": "patch",
                    "domain": "claude",
                    "payload": {"permissions_mode": "bypassPermissions"},
                }
            )
            core.stage_secret("claude", "deployment_token", None, "synthetic-core-token", revision=core.revision)
            self.assertNotIn("synthetic-core-token", json.dumps(core.snapshot()))
            self.assertNotIn(str(root), json.dumps(core.snapshot()))
            with self.assertRaises(ConfirmationNeeded):
                core.apply("claude", revision=core.revision)
            core.apply("claude", revision=core.revision, confirmation="accepted")

    def test_deployment_apply_writes_only_public_connection_fields_and_redacts_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / ".claude" / "settings.json"
            domain = ClaudeSettingsDomain(path)
            domain.dispatch(
                "patch",
                {
                    "env": {
                        "KEEP_THIS": "value",
                        "OLD_MODEL": "do-not-rewrite",
                    },
                    "desktop_profile": {"futureField": {"keep": True}},
                },
            )
            domain.dispatch(
                "select_deployment",
                {
                    "model": "public-chat",
                    "base_url": "https://gateway.example.test/v1",
                    "token": "synthetic-token",
                },
            )

            snapshot_text = json.dumps(domain.snapshot())
            self.assertNotIn("synthetic-token", snapshot_text)
            self.assertNotIn(str(root), snapshot_text)
            self.assertEqual(
                "https://gateway.example.test/v1",
                domain.snapshot()["settings"]["gateway_url"],
            )
            result = domain.apply({"confirm_risks": []})

            self.assertTrue(result["applied"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("public-chat", saved["model"])
            self.assertEqual("public-chat", saved["env"]["ANTHROPIC_MODEL"])
            self.assertEqual("https://gateway.example.test/v1", saved["env"]["ANTHROPIC_BASE_URL"])
            self.assertEqual("synthetic-token", saved["env"]["ANTHROPIC_AUTH_TOKEN"])
            self.assertEqual("value", saved["env"]["KEEP_THIS"])
            self.assertEqual("do-not-rewrite", saved["env"]["OLD_MODEL"])
            self.assertEqual({"keep": True}, saved["desktop_profile"]["futureField"])
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_snapshot_omits_gateway_urls_with_embedded_query_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / ".claude" / "settings.json"
            path.parent.mkdir()
            path.write_text(
                json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example.test/v1?token=synthetic-token"}}),
                encoding="utf-8",
            )

            domain = ClaudeSettingsDomain(path)

            snapshot_text = json.dumps(domain.snapshot())
            self.assertIsNone(domain.snapshot()["settings"]["gateway_url"])
            self.assertNotIn("synthetic-token", snapshot_text)

    def test_risky_permissions_are_denied_until_each_confirmation_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ClaudeSettingsDomain(pathlib.Path(directory) / "settings.json")
            domain.dispatch(
                "patch",
                {
                    "permissions": {"defaultMode": "bypassPermissions"},
                    "sandbox": {"enabled": False, "filesystem": {"allowed_paths": ["*"]}},
                    "network": {"allowed_domains": ["*"]},
                    "cowork": {"egress": ["*"]},
                },
            )
            with self.assertRaises(ConfirmationRequired) as context:
                domain.apply()
            self.assertEqual(
                {
                    "bypass_permissions",
                    "sandbox_disabled",
                    "filesystem_scope_broadened",
                    "network_scope_broadened",
                    "cowork_egress_all",
                },
                set(context.exception.codes),
            )
            self.assertNotIn("settings.json", json.dumps(domain.snapshot()))
            with self.assertRaises(ConfirmationRequired):
                domain.apply({"confirm_risks": ["bypass_permissions"]})
            domain.apply(
                {
                    "confirm_risks": [
                        "bypass_permissions",
                        "sandbox_disabled",
                        "filesystem_scope_broadened",
                        "network_scope_broadened",
                        "cowork_egress_all",
                    ]
                }
            )

            # The shared IPC apply envelope carries one explicit accepted
            # confirmation token after the native confirmation sheet; the
            # adapter accepts that token but never treats an omitted token as
            # consent.
            domain.dispatch("patch", {"network": {"allow_all": True}})
            with self.assertRaises(ConfirmationRequired):
                domain.apply()
            domain.apply({"confirmation": "accepted"})

    def test_nested_bash_sandbox_disable_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ClaudeSettingsDomain(pathlib.Path(directory) / "settings.json")
            domain.dispatch("patch", {"sandbox": {"bash": {"enabled": False}}})

            self.assertIn(
                "sandbox_disabled",
                domain.snapshot()["settings"]["risk_confirmations"],
            )
            with self.assertRaises(ConfirmationRequired):
                domain.apply()
            domain.apply({"confirm_risks": ["sandbox_disabled"]})

    def test_profile_attachment_is_explicit_and_unknown_json_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = root / "selected-profile.json"
            profile.write_text(
                json.dumps({"profileName": "example", "future": {"value": 7}}),
                encoding="utf-8",
            )
            domain = ClaudeSettingsDomain(root / "settings.json")
            domain.dispatch("attach_profile", {"path": str(profile)})
            self.assertTrue(domain.snapshot()["settings"]["desktop_profile_attached"])
            domain.apply({"confirm_risks": []})
            saved = json.loads((root / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(7, saved["desktop_profile"]["future"]["value"])
            self.assertNotIn(str(profile), json.dumps(domain.snapshot()))

    def test_core_uses_an_opaque_native_profile_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            profile = root / "selected-profile.json"
            profile.write_text(json.dumps({"future": {"value": 7}}), encoding="utf-8")
            core = CoreStore.with_default_domains(
                claude_settings_path=root / "settings.json",
                language_path=root / "language.json",
            )
            self.assertTrue({"providers_models", "codex", "claude", "runtime", "webdav", "logs", "language"}.issubset(set(core.domains)))
            token = core.file_capabilities.register(profile, "claude-profile")
            revision = core.dispatch(
                {
                    "domain": "claude-settings",
                    "type": "attach_profile",
                    "payload": {"file_token": token},
                }
            )["revision"]
            core.apply("claude-settings", revision=revision)
            snapshot = json.dumps(core.snapshot())
            self.assertNotIn(str(profile), snapshot)
            saved = json.loads((root / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(7, saved["desktop_profile"]["future"]["value"])

    def test_invalid_json_and_symlinks_are_safe_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "settings.json"
            secret = "synthetic-invalid-json"
            path.write_text('{"token": "' + secret, encoding="utf-8")
            with self.assertRaises(ClaudeSettingsError) as context:
                ClaudeSettingsDomain(path)
            self.assertNotIn(secret, str(context.exception))

            target = root / "real.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaises(ClaudeSettingsError):
                ClaudeSettingsDomain(link)

    def test_apply_refuses_an_external_disk_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            path.write_text('{"model":"initial"}\n', encoding="utf-8")
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"model": "draft"})
            external = '{"model":"external"}\n'
            path.write_text(external, encoding="utf-8")

            with self.assertRaisesRegex(ClaudeSettingsError, "changed on disk"):
                domain.apply()

            self.assertEqual(external, path.read_text(encoding="utf-8"))

    def test_first_apply_refuses_a_file_created_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "settings.json"
            domain = ClaudeSettingsDomain(path)
            domain.dispatch("patch", {"model": "draft"})
            external = '{"model":"external"}\n'
            path.write_text(external, encoding="utf-8")

            with self.assertRaisesRegex(ClaudeSettingsError, "changed on disk"):
                domain.apply()

            self.assertEqual(external, path.read_text(encoding="utf-8"))


class LanguageSettingsDomainTests(unittest.TestCase):
    def test_malformed_optional_files_keep_routes_registered_and_reloadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            claude = root / "claude.json"
            language = root / "language.json"
            claude.write_text("{", encoding="utf-8")
            language.write_text("{", encoding="utf-8")
            core = CoreStore.with_default_domains(
                metadata_path=root / "core.json",
                config_path=root / "config.yaml",
                runtime_settings_path=root / "runtime.env",
                webdav_settings_path=root / "webdav.json",
                webdav_enabled_path=root / "webdav.enabled",
                claude_settings_path=claude,
                language_path=language,
                runtime_root=root,
            )

            unavailable = core.snapshot()
            self.assertIn("claude", unavailable["domains"])
            self.assertIn("language", unavailable["domains"])
            self.assertFalse(unavailable["domains"]["claude"]["available"])
            self.assertFalse(unavailable["domains"]["language"]["available"])

            claude.write_text("{}\n", encoding="utf-8")
            language.write_text('{"language":"en"}\n', encoding="utf-8")
            core.reload("claude")
            core.reload("language")
            repaired = core.snapshot()
            self.assertNotEqual(False, repaired["domains"]["claude"].get("available"))
            self.assertEqual("en", repaired["language"])

    def test_system_resolution_and_user_override(self) -> None:
        self.assertEqual("zh-Hans", resolve_language("system", "zh_CN"))
        self.assertEqual("zh-Hans", resolve_language("system", "zh-TW"))
        self.assertEqual("en", resolve_language("system", "ja-JP"))
        self.assertEqual("en", resolve_language("en", "zh-CN"))
        self.assertEqual("zh-Hans", resolve_language("zh-Hans", "en-US"))

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "language.json"
            domain = LanguageSettingsDomain(path, system_locale="zh-CN")
            self.assertEqual("zh-Hans", domain.snapshot()["resolved"])
            self.assertEqual(list(LANGUAGE_OPTIONS), domain.snapshot()["options"])
            domain.dispatch("set_language", {"language": "en"})
            self.assertEqual("en", domain.snapshot()["resolved"])
            domain.apply()
            reloaded = LanguageSettingsDomain(path, system_locale="zh-CN")
            self.assertEqual("en", reloaded.snapshot()["choice"])
            self.assertEqual("en", reloaded.snapshot()["resolved"])

    def test_invalid_choice_is_rejected_and_messages_do_not_translate_values(self) -> None:
        with self.assertRaises(LanguageSettingsError):
            resolve_language("fr")
        with tempfile.TemporaryDirectory() as directory:
            domain = LanguageSettingsDomain(pathlib.Path(directory) / "language.json")
            with self.assertRaises(LanguageSettingsError):
                domain.dispatch("set", {"language": "fr"})
        translator = create_translator("zh-Hans")
        self.assertEqual("Claude 设置", translator("menu.claude"))
        self.assertEqual("provider-custom", translator("provider-custom"))

    def test_apply_refuses_an_external_disk_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "language.json"
            path.write_text('{"language":"system"}\n', encoding="utf-8")
            domain = LanguageSettingsDomain(path, system_locale="en-US")
            domain.dispatch("set", {"language": "en"})
            external = '{"language":"zh-Hans"}\n'
            path.write_text(external, encoding="utf-8")

            with self.assertRaisesRegex(LanguageSettingsError, "changed on disk"):
                domain.apply()

            self.assertEqual(external, path.read_text(encoding="utf-8"))

    def test_first_apply_refuses_a_file_created_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "language.json"
            domain = LanguageSettingsDomain(path, system_locale="en-US")
            domain.dispatch("set", {"language": "en"})
            external = '{"language":"zh-Hans"}\n'
            path.write_text(external, encoding="utf-8")

            with self.assertRaisesRegex(LanguageSettingsError, "changed on disk"):
                domain.apply()

            self.assertEqual(external, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

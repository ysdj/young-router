from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from young_router.core.claude_desktop import (
    ClaudeDesktopConfig,
    ClaudeDesktopConfigError,
    ClaudeDeveloperSettings,
)
from young_router.core.domains.claude import ClaudeSettingsDomain


def _write_desktop_config(root: pathlib.Path, config: dict[str, object]) -> pathlib.Path:
    config_id = "synthetic-config"
    root.mkdir(parents=True)
    (root / "_meta.json").write_text(
        json.dumps({"appliedId": config_id, "entries": [{"id": config_id, "name": "Synthetic"}]}),
        encoding="utf-8",
    )
    config_path = root / f"{config_id}.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


class ClaudeDesktopConfigTests(unittest.TestCase):
    def test_developer_mode_uses_claude_desktop_developer_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "Claude" / "developer_settings.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"allowDevTools": True, "future": "keep"}), encoding="utf-8")

            settings = ClaudeDeveloperSettings(path)

            self.assertTrue(settings.snapshot()["developer_mode_enabled"])
            settings.patch({"allowDevTools": False})
            self.assertIn('"allowDevTools": false', settings.raw_text())
            settings.apply()
            self.assertEqual(
                {"allowDevTools": False, "future": "keep"},
                json.loads(path.read_text(encoding="utf-8")),
            )

    def test_claude_domain_keeps_developer_settings_as_a_third_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            developer_path = root / "Claude" / "developer_settings.json"
            developer_path.parent.mkdir(parents=True)
            developer_path.write_text('{"allowDevTools":true}\n', encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                developer_settings_path=developer_path,
            )

            self.assertTrue(domain.snapshot()["developer"]["developer_mode_enabled"])
            self.assertIn("allowDevTools", domain.raw_text(document="developer"))
            domain.dispatch("developer_patch", {"allowDevTools": False})
            domain.apply()
            self.assertFalse(json.loads(developer_path.read_text(encoding="utf-8"))["allowDevTools"])

    def test_current_model_entries_load_and_round_trip_without_losing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "configLibrary"
            model_entries = [
                {"name": "model-primary", "supports1m": True, "future": {"keep": True}},
                "model-secondary",
            ]
            config_path = _write_desktop_config(
                root,
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": "https://gateway.example.test",
                    "inferenceGatewayApiKey": "synthetic-secret",
                    "inferenceModels": model_entries,
                },
            )

            desktop = ClaudeDesktopConfig(root)

            self.assertTrue(desktop.snapshot()["models_configured"])
            self.assertEqual(["model-primary", "model-secondary"], desktop.snapshot()["model_names"])
            self.assertNotIn("synthetic-secret", desktop.raw_text())
            desktop.patch({"inferenceGatewayAuthScheme": "x-api-key"})
            desktop.apply()
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(model_entries, saved["inferenceModels"])

    def test_model_object_requires_a_non_empty_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "configLibrary"
            _write_desktop_config(root, {"inferenceModels": [{"supports1m": True}]})

            with self.assertRaisesRegex(ClaudeDesktopConfigError, "entry name"):
                ClaudeDesktopConfig(root)

    def test_claude_domain_loads_a_current_desktop_model_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root,
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": "https://gateway.example.test",
                    "inferenceModels": [{"name": "model-primary"}],
                },
            )

            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
            )

            self.assertTrue(domain.snapshot()["desktop"]["available"])
            self.assertTrue(domain.snapshot()["desktop"]["models_configured"])

    def test_structured_model_list_preserves_metadata_and_clears_to_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "configLibrary"
            _write_desktop_config(
                root,
                {
                    "inferenceModels": [
                        {"name": "model-primary", "supports1m": True, "future": {"keep": True}},
                        "model-secondary",
                    ]
                },
            )
            desktop = ClaudeDesktopConfig(root)

            snapshot = desktop.set_model_names(["model-secondary", "model-primary", "model-new"])

            self.assertEqual(["model-secondary", "model-primary", "model-new"], snapshot["model_names"])
            self.assertEqual(
                [
                    "model-secondary",
                    {"name": "model-primary", "supports1m": True, "future": {"keep": True}},
                    {"name": "model-new"},
                ],
                desktop.draft_state()["config"]["inferenceModels"],
            )
            cleared = desktop.set_model_names([])
            self.assertFalse(cleared["models_configured"])
            self.assertEqual([], cleared["model_names"])
            self.assertNotIn("inferenceModels", desktop.draft_state()["config"])

    def test_domain_stages_the_structured_desktop_model_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {"inferenceModels": [{"name": "model-primary"}]})
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
            )

            snapshot = domain.dispatch(
                "desktop_models_patch",
                {"model_names": ["model-new", "model-primary"]},
            )

            self.assertEqual(["model-new", "model-primary"], snapshot["desktop"]["model_names"])

    def test_claude_package_round_trips_all_three_drafts_without_public_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            settings = root / "settings.json"
            settings.write_text(
                '{"env":{"ANTHROPIC_AUTH_TOKEN":"replace-claude-token"}}\n',
                encoding="utf-8",
            )
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root,
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": "https://gateway.example.test",
                    "inferenceGatewayApiKey": "replace-desktop-token",
                },
            )
            developer = root / "developer_settings.json"
            developer.write_text('{"allowDevTools":true}\n', encoding="utf-8")
            source = ClaudeSettingsDomain(
                settings,
                desktop_config_library_path=desktop_root,
                developer_settings_path=developer,
            )

            trusted = source.export(include_sensitive=True)
            public = source.export()
            self.assertIn("replace-claude-token", json.dumps(trusted))
            self.assertIn("replace-desktop-token", json.dumps(trusted))
            self.assertNotIn("replace-claude-token", json.dumps(public))
            self.assertNotIn("replace-desktop-token", json.dumps(public))

            target_root = root / "target"
            target_desktop = target_root / "configLibrary"
            _write_desktop_config(target_desktop, {})
            target_developer = target_root / "developer_settings.json"
            target_developer.parent.mkdir(parents=True, exist_ok=True)
            target_developer.write_text("{}\n", encoding="utf-8")
            target = ClaudeSettingsDomain(
                target_root / "settings.json",
                desktop_config_library_path=target_desktop,
                developer_settings_path=target_developer,
            )

            target.import_package(trusted)

            imported = target.export(include_sensitive=True)
            self.assertEqual(trusted["settings"], imported["settings"])
            self.assertEqual(trusted["desktop"]["config"], imported["desktop"]["config"])
            self.assertEqual(trusted["developer"], imported["developer"])

    def test_both_claude_apps_share_one_client_entry_and_keep_their_own_files(self) -> None:
        # The pane's rail offers one Claude entry, and the listing says which
        # app owns each document so the detail can group them.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {"inferenceModels": [{"name": "model-primary"}]})
            developer = root / "developer_settings.json"
            developer.write_text('{"allowDevTools":true}\n', encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                developer_settings_path=developer,
            )

            rows = domain.client_files()

            self.assertEqual({"claude"}, {row["client"] for row in rows})
            self.assertEqual(
                {"claudeCode": ["settings"], "claudeDesktop": ["desktop", "developer"]},
                {
                    app: [row["document"] for row in rows if row["app"] == app]
                    for app in ("claudeCode", "claudeDesktop")
                },
            )

    def test_desktop_and_code_adopt_the_local_api_independently(self) -> None:
        # One domain, two apps: pointing Claude Code at this app's proxy leaves
        # Claude Desktop's profile untouched, and each app reports its own
        # switch and its own remembered route.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root,
                {"inferenceGatewayBaseUrl": "https://gateway.example.test"},
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                desktop_loader=None,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch("young_router.core.domains._shared_api.endpoint", return_value=("http://127.0.0.1:4100/v1", "sk-local")):
                snapshot = domain.dispatch(
                    "use_local_api",
                    {"client": "claudeCode", "model": "public-a", "provider": "prov-a", "deployment_id": "dep-a"},
                )

            env = snapshot["settings"]
            self.assertTrue(env["gateway_configured"])
            code = json.loads(domain.raw_text(include_sensitive=True))
            # Claude Code appends ``/v1/messages`` itself, so the base it
            # stores is the Messages root, and the token is the bearer form a
            # gateway states.
            self.assertEqual("http://127.0.0.1:4100", code["env"]["ANTHROPIC_BASE_URL"])
            self.assertEqual("sk-local", code["env"]["ANTHROPIC_AUTH_TOKEN"])
            self.assertEqual("public-a", code["env"]["ANTHROPIC_MODEL"])
            self.assertEqual("public-a", code["model"])
            self.assertTrue(snapshot["clients"]["claudeCode"]["uses_local_api"])
            self.assertFalse(snapshot["clients"]["claudeDesktop"]["uses_local_api"])
            # A remembered route is what the off direction returns to, and it is
            # this app's own bookkeeping, never a field of the client's document.
            self.assertEqual(
                {"model": "public-a", "provider": "prov-a", "deployment_id": "dep-a"},
                snapshot["clients"]["claudeCode"]["designated_route"],
            )
            self.assertTrue((root / "client-routes.json").is_file())

    def test_desktop_adopts_the_local_api_as_its_third_party_gateway(self) -> None:
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
            with mock.patch("young_router.core.domains._shared_api.endpoint", return_value=("http://127.0.0.1:4100/v1", "sk-local")):
                snapshot = domain.dispatch("use_local_api", {"client": "claudeDesktop"})

            self.assertTrue(snapshot["clients"]["claudeDesktop"]["uses_local_api"])
            self.assertFalse(snapshot["clients"]["claudeCode"]["uses_local_api"])
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            # The Messages root carries no /v1: Claude Desktop appends
            # /v1/messages itself.
            self.assertEqual("http://127.0.0.1:4100", config["inferenceGatewayBaseUrl"])
            self.assertEqual("gateway", config["inferenceProvider"])
            self.assertEqual("sk-local", config["inferenceGatewayApiKey"])

    def test_generated_model_lists_follow_the_public_list_and_its_own_switch(self) -> None:
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
            with mock.patch.object(domain, "_public_models", return_value=["public-a", "public-b"]):
                enabled = domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                self.assertTrue(enabled["clients"]["claudeCode"]["model_list_enabled"])
                picker = json.loads(domain.raw_text(include_sensitive=True))["modelPicker"]
                # The switch means "this list, alone": a lineup that only adds
                # rows would leave Claude's own models beside the router's.
                self.assertTrue(picker["replaceBuiltInOptions"])
                self.assertEqual(
                    [{"model": "public-a"}, {"model": "public-b"}],
                    picker["options"],
                )

                # A new public list reaches the file on the next refresh.
                with mock.patch.object(domain, "_public_models", return_value=["public-b", "public-c"]):
                    self.assertTrue(domain.refresh_model_lists())
                self.assertIn("public-c", domain.raw_text(include_sensitive=True))

                # An observation that is merely empty is not a change.
                with mock.patch.object(domain, "_public_models", return_value=None):
                    self.assertFalse(domain.refresh_model_lists())
                self.assertIn("public-c", domain.raw_text(include_sensitive=True))

                disabled = domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": False})
                self.assertFalse(disabled["clients"]["claudeCode"]["model_list_enabled"])
                self.assertNotIn("modelPicker", json.loads(domain.raw_text(include_sensitive=True)))

    def test_desktop_model_list_replaces_its_own_list_and_clears_to_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {"inferenceModels": [{"name": "hand-written"}]})
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            # A list this app did not generate is not claimed by it: the switch
            # reads the record of what was generated, so a profile brought in
            # from somewhere else is never overwritten by a refresh.
            self.assertFalse(domain.snapshot()["clients"]["claudeDesktop"]["model_list_enabled"])

            with mock.patch.object(domain, "_public_models", return_value=["public-a"]):
                enabled = domain.dispatch("claude.models.set", {"client": "claudeDesktop", "enabled": True})
            self.assertTrue(enabled["clients"]["claudeDesktop"]["model_list_enabled"])
            self.assertEqual(["public-a"], enabled["desktop"]["model_names"])

            with mock.patch.object(domain, "_public_models", return_value=["public-a", "public-b"]):
                self.assertTrue(domain.refresh_model_lists())
            self.assertEqual(["public-a", "public-b"], domain.snapshot()["desktop"]["model_names"])

            cleared = domain.dispatch("claude.models.set", {"client": "claudeDesktop", "enabled": False})
            self.assertFalse(cleared["clients"]["claudeDesktop"]["model_list_enabled"])
            # An empty list is how Claude Desktop goes back to discovering the
            # gateway's own models.
            self.assertEqual([], cleared["desktop"]["model_names"])

    def test_use_saved_model_restores_the_routes_own_endpoint_and_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime_config = root / "config.yaml"
            runtime_config.write_text(
                """
model_list:
  - model_name: public-a
    litellm_params:
      model: anthropic/claude-upstream
      api_base: https://relay.example.test/v1
      api_key: sk-route
    model_info:
      id: dep-a
      provider: prov-a
      upstream_url_surface: anthropic
providers:
  prov-a:
    api_base: https://relay.example.test/v1
general_settings:
  master_key: sk-router
""",
                encoding="utf-8",
            )
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=runtime_config,
                runtime_root=root,
            )

            snapshot = domain.dispatch(
                "use_saved_model",
                {"client": "claudeCode", "model": "public-a", "provider": "prov-a", "deployment_id": "dep-a"},
            )

            env = json.loads(domain.raw_text(include_sensitive=True))["env"]
            self.assertEqual("https://relay.example.test", env["ANTHROPIC_BASE_URL"])
            self.assertEqual("sk-route", env["ANTHROPIC_AUTH_TOKEN"])
            self.assertEqual("public-a", env["ANTHROPIC_MODEL"])
            self.assertFalse(snapshot["clients"]["claudeCode"]["uses_local_api"])
            self.assertEqual("prov-a", snapshot["clients"]["claudeCode"]["designated_route"]["provider"])


if __name__ == "__main__":
    unittest.main()

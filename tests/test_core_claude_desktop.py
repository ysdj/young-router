from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from young_router.claude_aliases import ALIAS_FAMILY as FABLE_ALIAS_PREFIX
from young_router.claude_aliases import claude_desktop_alias as alias_claude_desktop
from young_router.claude_aliases import desktop_keeps_model_id as desktop_keeps_id
from young_router.core.claude_desktop import (
    ClaudeDesktopConfig,
    ClaudeDesktopConfigError,
    ClaudeDeveloperSettings,
)
from young_router.core.domains.claude import ClaudeSettingsDomain
from young_router.proxy import claude_desktop_listing as claude_desktop_listing_module


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
                # The pane applies a staged toggle; a refresh never commits an
                # edit the user has not applied, so the list has to be on disk
                # before the background pass may touch it.
                domain.apply()
                picker = json.loads(domain.raw_text(include_sensitive=True))["modelPicker"]
                # The switch means "this list, alone": a lineup that only adds
                # rows would leave Claude's own models beside the router's.
                self.assertTrue(picker["replaceBuiltInOptions"])
                # One id space: the picker names the alias both Claude apps
                # send, so the desktop Code tab and the terminal select the
                # same model.
                self.assertEqual(
                    [
                        {"model": alias_claude_desktop("public-a")},
                        {"model": alias_claude_desktop("public-b")},
                    ],
                    picker["options"],
                )

                # The pass that just wrote this list finds nothing to do: an
                # ids-are-aliases list compared against public names would look
                # different on every pass and rewrite the file each second.
                self.assertFalse(domain.refresh_model_lists())

                # A new public list reaches the file on the next refresh.
                with mock.patch.object(domain, "_public_models", return_value=["public-b", "public-c"]):
                    self.assertTrue(domain.refresh_model_lists())
                    self.assertIn(alias_claude_desktop("public-c"), domain.raw_text(include_sensitive=True))
                    # ...and then settles again.
                    self.assertFalse(domain.refresh_model_lists())

                # An observation that is merely empty is not a change.
                with mock.patch.object(domain, "_public_models", return_value=None):
                    self.assertFalse(domain.refresh_model_lists())
                self.assertIn(alias_claude_desktop("public-c"), domain.raw_text(include_sensitive=True))

                disabled = domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": False})
                self.assertFalse(disabled["clients"]["claudeCode"]["model_list_enabled"])
                self.assertNotIn("modelPicker", json.loads(domain.raw_text(include_sensitive=True)))

    def test_claude_code_picker_names_the_id_both_claude_apps_send(self) -> None:
        # Claude Code's modelPicker and Claude Desktop's Code tab are one client
        # with one settings file, and the desktop side sends the derived alias.
        # A picker keyed by the public name would describe a model that session
        # never asks for, and every context setting would be written twice --
        # once per spelling -- with only one of them reaching the session.
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
            public = ["claude-opus-5-5", "gemini-3.8-flash", "gpt-6.1-sol"]
            rows = [
                {"id": "claude-opus-5-5", "max_input_tokens": 1_000_000},
                {"id": "gemini-3.8-flash", "max_input_tokens": 1_048_576},
                {"id": "gpt-6.1-sol", "max_input_tokens": 500_000},
            ]
            with (
                mock.patch.object(domain, "_public_models", return_value=public),
                mock.patch.object(domain, "_public_model_rows", return_value=rows),
            ):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.apply()

            picker = json.loads(domain.raw_text(include_sensitive=True))["modelPicker"]
            self.assertTrue(picker["replaceBuiltInOptions"])
            # One row per route: no second row for the desktop spelling.
            self.assertEqual(
                ["claude-opus-5-5", "claude-mythos-319034", "claude-mythos-dedfef"],
                [row["model"] for row in picker["options"]],
            )
            by_id = {row["model"]: row for row in picker["options"]}
            gemini = by_id["claude-mythos-319034"]
            self.assertEqual(
                {
                    "model": "claude-mythos-319034",
                    "description": "\u200b",
                    "label": "gemini-3.8-flash",
                    "behavesAs": "claude-opus-5-5",
                },
                gemini,
            )
            # A 500k route gets the 1M behavior model; the exact 500k comes
            # from its per-model autoCompactWindow below.
            self.assertEqual("claude-opus-5-5", by_id["claude-mythos-dedfef"]["behavesAs"])
            self.assertEqual("gpt-6.1-sol", by_id["claude-mythos-dedfef"]["label"])
            # A real Claude id is already in the client's catalog and needs no
            # substitute; the client keeps its own friendly label for it.
            self.assertEqual({"model": "claude-opus-5-5", "description": "\u200b"}, by_id["claude-opus-5-5"])
            settings = json.loads(domain.raw_text(include_sensitive=True))["modelSettings"]
            self.assertEqual(1_000_000, settings["claude-opus-5-5"]["autoCompactWindow"])
            # 1,048,576 is clamped to the setting's accepted maximum of 1M.
            self.assertEqual(1_000_000, settings["claude-mythos-319034"]["autoCompactWindow"])
            self.assertEqual(500_000, settings["claude-mythos-dedfef"]["autoCompactWindow"])
            # A public-name entry nobody wrote is not invented beside them.
            self.assertNotIn("gpt-6.1-sol", settings)

    def test_claude_code_picker_keeps_a_users_own_row_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            settings = {
                "modelPicker": {
                    "replaceBuiltInOptions": True,
                    "options": [
                        {
                            "model": "gemini-3.8-flash",
                            "label": "Gemini",
                            "description": "My label",
                            "behavesAs": "claude-opus-4-8",
                        }
                    ],
                }
            }
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with (
                mock.patch.object(domain, "_public_models", return_value=["gemini-3.8-flash"]),
                mock.patch.object(
                    domain,
                    "_public_model_rows",
                    return_value=[{"id": "gemini-3.8-flash", "max_input_tokens": 1_048_576}],
                ),
            ):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.apply()

            row = json.loads(domain.raw_text(include_sensitive=True))["modelPicker"]["options"][0]
            # One id space: the row names the alias both Claude apps send, and a
            # field the user typed on the old public-name row follows it there.
            self.assertEqual("claude-mythos-319034", row["model"])
            self.assertEqual("Gemini", row["label"])
            self.assertEqual("My label", row["description"])
            self.assertEqual("claude-opus-4-8", row["behavesAs"])

    def test_claude_code_per_model_windows_preserve_a_users_own_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            settings = {
                "modelSettings": {
                    "gemini-3.8-flash": {"autoCompactWindow": 500_000, "effortLevel": "high"},
                    "someone-elses-model": {"autoCompactWindow": "auto"},
                }
            }
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with (
                mock.patch.object(
                    domain, "_public_models", return_value=["gemini-3.8-flash", "gpt-6.1-sol"]
                ),
                mock.patch.object(
                    domain,
                    "_public_model_rows",
                    return_value=[
                        {"id": "gemini-3.8-flash", "max_input_tokens": 1_048_576},
                        {"id": "gpt-6.1-sol", "max_input_tokens": 500_000},
                    ],
                ),
            ):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.apply()

            settings = json.loads(domain.raw_text(include_sensitive=True))["modelSettings"]
            # A user's lower window and effort choice survive a regeneration and
            # follow the id the picker now names; the new route gets the derived
            # 500k under the same kind of id.
            self.assertEqual(500_000, settings["claude-mythos-319034"]["autoCompactWindow"])
            self.assertEqual("high", settings["claude-mythos-319034"]["effortLevel"])
            self.assertEqual(500_000, settings["claude-mythos-dedfef"]["autoCompactWindow"])
            # That entry carries a field this app never writes (the user's
            # effort), so the older spelling stays as its own record instead of
            # being folded into the id's.
            self.assertEqual("high", settings["gemini-3.8-flash"]["effortLevel"])
            # A name this app does not serve is untouched.
            self.assertEqual("auto", settings["someone-elses-model"]["autoCompactWindow"])

    def test_the_background_pass_repairs_a_stale_rendering_of_the_same_routes(self) -> None:
        # Same routes, wrong rows: a previous release wrote the public name as
        # the id (and a second row for the desktop spelling).  Comparing only
        # the names would call that file current and leave both apps reading a
        # model list that no longer matches the router.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            settings = {
                "modelPicker": {
                    "replaceBuiltInOptions": True,
                    "options": [
                        {"model": "gemini-3.8-flash", "description": "\u200b"},
                        {"model": "claude-mythos-319034", "label": "gemini-3.8-flash (Desktop)"},
                    ],
                }
            }
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with (
                mock.patch.object(domain, "_public_models", return_value=["gemini-3.8-flash"]),
                mock.patch.object(
                    domain,
                    "_public_model_rows",
                    return_value=[{"id": "gemini-3.8-flash", "max_input_tokens": 1_048_576}],
                ),
            ):
                self.assertTrue(domain.refresh_model_lists())

            rows = json.loads(domain.raw_text(include_sensitive=True))["modelPicker"]["options"]
            self.assertEqual(
                [
                    {
                        "model": "claude-mythos-319034",
                        "description": "\u200b",
                        "label": "gemini-3.8-flash",
                        "behavesAs": "claude-opus-5-5",
                    }
                ],
                rows,
            )

    def test_a_superseded_public_name_entry_is_retired_not_duplicated(self) -> None:
        # Every generated entry is ours: one number for one route.  A file that
        # kept the old spelling beside the new id would describe the same model
        # twice and grow a second entry per model on every change.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            settings = {"modelSettings": {"gemini-3.8-flash": {"autoCompactWindow": 1_000_000}}}
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with (
                mock.patch.object(domain, "_public_models", return_value=["gemini-3.8-flash"]),
                mock.patch.object(
                    domain,
                    "_public_model_rows",
                    return_value=[{"id": "gemini-3.8-flash", "max_input_tokens": 1_048_576}],
                ),
            ):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.apply()

            settings = json.loads(domain.raw_text(include_sensitive=True))["modelSettings"]
            self.assertEqual({"claude-mythos-319034": {"autoCompactWindow": 1_000_000}}, settings)

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

            with mock.patch.object(domain, "_public_models", return_value=["claude-opus-5-5"]):
                enabled = domain.dispatch("claude.models.set", {"client": "claudeDesktop", "enabled": True})
                domain.apply()
            self.assertTrue(enabled["clients"]["claudeDesktop"]["model_list_enabled"])
            self.assertEqual(["claude-opus-5-5"], enabled["desktop"]["model_names"])

            # Desktop discovers /v1/models now; a background refresh never
            # rewrites its profile, so the list the switch wrote stays until
            # the switch is turned off.
            with mock.patch.object(domain, "_public_models", return_value=["claude-opus-5-5", "claude-sonnet-4-5"]):
                self.assertFalse(domain.refresh_model_lists())
            self.assertEqual(["claude-opus-5-5"], domain.snapshot()["desktop"]["model_names"])

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

    def test_use_saved_model_clears_the_picker_list_that_named_this_proxy(self) -> None:
        """A client leaving the proxy drops the picker list this app generated.

        Claude Code's lineup holds this router's public model names, which only
        its proxy serves, so it leaves with the switch that pointed the client
        at the proxy: the pane's disabled list switch has to read the state the
        client is actually in.  Desktop is judged by the record of what this app
        generated, so a route whose own model is also in that record would
        otherwise let a background refresh put the whole proxy list back onto a
        client that no longer talks to it.
        """

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
    api_keys:
      - name: default
        value: sk-route
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
            public = ["public-a", "claude-opus-5-5"]
            with mock.patch.object(domain, "_public_models", return_value=public):
                for client in ("claudeCode", "claudeDesktop"):
                    domain.dispatch("claude.models.set", {"client": client, "enabled": True})
            domain.apply()
            self.assertIn("modelPicker", json.loads(domain.raw_text(include_sensitive=True)))
            self.assertTrue(domain.snapshot()["clients"]["claudeDesktop"]["model_list_enabled"])

            left = domain.dispatch(
                "use_saved_model",
                {"client": "claudeCode", "model": "public-a", "provider": "prov-a", "deployment_id": "dep-a"},
            )
            self.assertNotIn("modelPicker", json.loads(domain.raw_text(include_sensitive=True)))
            self.assertFalse(left["clients"]["claudeCode"]["model_list_enabled"])
            # Desktop is still on this app's list: leaving for one client never
            # touches the other's picker.
            self.assertTrue(left["clients"]["claudeDesktop"]["model_list_enabled"])

            desktop = domain.dispatch(
                "use_saved_model",
                {"client": "claudeDesktop", "model": "public-a", "provider": "prov-a", "deployment_id": "dep-a"},
            )
            self.assertFalse(desktop["clients"]["claudeDesktop"]["model_list_enabled"])
            # Desktop keeps only a name it can map onto an Anthropic model, so
            # this route's own public name leaves an empty list -- and that is
            # what the record must say, or a refresh would read "a subset of
            # what we generated" and put the whole proxy list back.
            self.assertEqual([], desktop["desktop"]["model_names"])
            with mock.patch.object(domain, "_public_models", return_value=["public-a", "public-b"]):
                self.assertFalse(domain.refresh_model_lists())
            self.assertEqual([], domain.snapshot()["desktop"]["model_names"])

    def test_desktop_list_derives_an_alias_for_every_model_it_would_drop(self) -> None:
        # Verified against the app: it keeps an id that reads as an Anthropic
        # model and drops everything else.  The list is therefore derived, not
        # curated -- a public name it keeps is written as itself, and every
        # other one is written under the alias this app computes for it, with
        # the public name as the label the picker shows.
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
            public = [
                "grok-4.7",
                "claude-opus-5-5",
                "gpt-6.1-sol",
                "deepseek-v4.1-flash",
                "anthropic/claude-sonnet-4-5",
            ]
            with mock.patch.object(domain, "_public_models", return_value=public):
                snapshot = domain.dispatch(
                    "claude.models.set", {"client": "claudeDesktop", "enabled": True}
                )

            entries = json.loads(
                domain.raw_text(document="desktop", include_sensitive=True)
            )["inferenceModels"]
            names = [row["name"] for row in entries]
            # Two public names survive as written; the other three are aliased.
            self.assertIn("claude-opus-5-5", names)
            self.assertIn("anthropic/claude-sonnet-4-5", names)
            self.assertEqual(len(public), len(entries))
            labelled = {row["name"]: row["labelOverride"] for row in entries if "labelOverride" in row}
            for name in ("grok-4.7", "gpt-6.1-sol", "deepseek-v4.1-flash"):
                alias = alias_claude_desktop(name)
                self.assertIn(alias, names)
                self.assertEqual(name, labelled[alias])
                # Desktop drops an id carrying a refused brand, so the derived
                # id must never contain one -- and it must carry a family that
                # the app gives a reasoning ladder to, or the picker offers no
                # Effort control for these routes at all.
                self.assertTrue(desktop_keeps_id(alias))
                self.assertTrue(alias.startswith(FABLE_ALIAS_PREFIX))
            self.assertEqual(len(set(names)), len(names), "aliases must be unique")
            self.assertTrue(snapshot["clients"]["claudeDesktop"]["model_list_enabled"])

    def test_the_list_keeps_the_order_the_public_names_arrive_in(self) -> None:
        # The picker is read top-down and picked by number, so its order is the
        # reader's.  That order is settled once, where the public names are read
        # (``codex_config.local_exposed_models``), and this derivation must not
        # re-rank it: a row written as itself and a row written under an alias
        # sit in the list exactly where their public name does.
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
            public = ["claude-opus-5-5", "deepseek-v4.1-flash", "grok-4.7", "kimi-k3.1-pro"]
            with mock.patch.object(domain, "_public_models", return_value=public):
                entries = domain.desktop_model_entries()

            self.assertEqual(public, [row.get("labelOverride") or row["name"] for row in entries])

    def test_a_snapshot_leaves_the_desktop_list_to_discovery(self) -> None:
        # Discovery reads /v1/models at launch, so an open window must not reach
        # into the profile and rewrite the list the retired switch wrote.
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
            with mock.patch.object(domain, "_public_models", return_value=["deepseek-v4.1-flash"]):
                domain.dispatch("claude.models.set", {"client": "claudeDesktop", "enabled": True})
                domain.apply()
            before = list(domain.snapshot()["desktop"]["model_names"])

            with mock.patch.object(
                domain,
                "_public_models",
                return_value=["deepseek-v4.1-flash", "brand-new-route"],
            ):
                after = list(domain.snapshot()["desktop"]["model_names"])

            self.assertEqual(before, after)

    def test_a_snapshot_does_not_read_the_public_list_for_desktop(self) -> None:
        # The retired list has no snapshot-driven refresh left at all: a poll is
        # a read of the draft, not a loopback /v1/models request.
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
            with mock.patch.object(domain, "_public_models") as reader:
                domain.snapshot()
                domain.snapshot()
            reader.assert_not_called()

    def test_desktop_list_never_needs_a_claude_shaped_public_name(self) -> None:
        # The point of deriving the id: a router whose public names are all
        # third-party models still fills Desktop's picker.
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
            with mock.patch.object(domain, "_public_models", return_value=["grok-4.7", "gpt-6.1-sol"]):
                snapshot = domain.dispatch(
                    "claude.models.set", {"client": "claudeDesktop", "enabled": True}
                )
            self.assertEqual(2, len(snapshot["desktop"]["model_names"]))

    def test_a_new_public_model_does_not_rewrite_the_desktop_list(self) -> None:
        # A provider added later reaches Desktop through the discovery view at
        # its next launch, not through this app editing its profile.
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
            with mock.patch.object(domain, "_public_models", return_value=["deepseek-v4.1-flash"]):
                domain.dispatch("claude.models.set", {"client": "claudeDesktop", "enabled": True})
                domain.apply()
            before = list(domain.snapshot()["desktop"]["model_names"])
            with mock.patch.object(
                domain, "_public_models", return_value=["deepseek-v4.1-flash", "some-new-model-x"]
            ):
                self.assertFalse(domain.refresh_model_lists())
            self.assertEqual(before, domain.snapshot()["desktop"]["model_names"])

    def test_the_desktop_profile_prefers_the_1m_option_on_this_proxy(self) -> None:
        # A discovery row states 1M through max_input_tokens; without this
        # preference the picker also offers a 200k standard row, and that is the
        # number a session on it reports.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root,
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": "http://127.0.0.1:4100",
                },
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch.object(domain, "desktop_uses_local_api", return_value=True), mock.patch.object(
                domain, "_public_models", return_value=["gpt-5.4"]
            ), mock.patch.object(
                claude_desktop_listing_module, "_declared_context_window", return_value=1_000_000
            ):
                self.assertTrue(domain._declares_one_million_context())
                domain._prefer_one_million_context()
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertTrue(config["modelPrefer1mContext"])

    def test_a_window_this_app_only_resolved_is_not_a_declaration(self) -> None:
        # ``/v1/models`` can state a window nobody on the route declared
        # (LiteLLM's table, a provider profile).  That is a reading, and a 1M
        # reading is what made the picker hide rows that could run.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root,
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": "http://127.0.0.1:4100",
                },
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch.object(domain, "desktop_uses_local_api", return_value=True), mock.patch.object(
                domain, "_public_models", return_value=["gemini-3.8-flash"]
            ), mock.patch.object(
                claude_desktop_listing_module, "_declared_context_window", return_value=None
            ):
                self.assertFalse(domain._declares_one_million_context())
                domain._prefer_one_million_context()
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertNotIn("modelPrefer1mContext", config)

    def test_no_route_declaring_1m_leaves_the_preference_off(self) -> None:
        # The preference is about the whole picker: it hides the standard row of
        # every route that reads as 1M.  Setting it wherever this app's proxy
        # merely answered made the routes whose upstream advertises a large
        # window lose the row that could run.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root,
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": "http://127.0.0.1:4100",
                },
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            rows = [
                {"id": "gemini-3.8-flash", "max_input_tokens": None},
                {"id": "gpt-6.1-sol", "max_input_tokens": 500_000},
            ]
            with mock.patch.object(domain, "desktop_uses_local_api", return_value=True), mock.patch.object(
                domain, "_public_models", return_value=["gemini-3.8-flash", "gpt-6.1-sol"]
            ), mock.patch.object(
                claude_desktop_listing_module, "_declared_context_window", return_value=None
            ):
                domain._prefer_one_million_context()
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertNotIn("modelPrefer1mContext", config)

    def test_an_explicit_1m_preference_is_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root,
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": "http://127.0.0.1:4100",
                    "modelPrefer1mContext": False,
                },
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch.object(domain, "desktop_uses_local_api", return_value=True):
                domain._prefer_one_million_context()
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertFalse(config["modelPrefer1mContext"])

    def test_a_saved_route_profile_does_not_gain_the_1m_preference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root,
                {
                    "inferenceProvider": "gateway",
                    "inferenceGatewayBaseUrl": "https://relay.example.test",
                },
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch.object(domain, "desktop_uses_local_api", return_value=False):
                domain._prefer_one_million_context()
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertNotIn("modelPrefer1mContext", config)

    def test_a_list_carrying_this_apps_alias_is_retired_without_a_record(self) -> None:
        # The record can be lost while the profile still holds the list.  An id
        # in this app's own alias namespace is the mark that says the list is
        # the one it wrote.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(
                desktop_root,
                {
                    "inferenceModels": [
                        {"name": "claude-opus-5-5"},
                        {"name": "claude-fable-eb1394"},
                    ]
                },
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            self.assertEqual([], domain.snapshot()["desktop"]["model_names"])

    def test_a_hand_written_claude_list_is_not_retired(self) -> None:
        # A profile the user curates with real Claude names has neither the
        # record nor a derived alias, so the migration leaves it alone.
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
            self.assertEqual(["claude-opus-5-5"], domain.snapshot()["desktop"]["model_names"])

    def test_a_generated_desktop_list_is_retired_on_reload(self) -> None:
        # The upgrade path: a list this app wrote for the old switch is cleared
        # once, so Desktop goes back to discovering /v1/models.  The record is
        # what distinguishes it from a hand-written profile.
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
            public = ["deepseek-v4.1-flash", "kimi-k3.1-pro"]
            with mock.patch.object(domain, "_public_models", return_value=public):
                domain.dispatch("claude.models.set", {"client": "claudeDesktop", "enabled": True})
                domain.apply()
            self.assertNotEqual([], domain.snapshot()["desktop"]["model_names"])

            domain.reload()
            self.assertFalse(domain.snapshot()["clients"]["claudeDesktop"]["model_list_enabled"])
            self.assertEqual([], domain.snapshot()["desktop"]["model_names"])
            stored = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertNotIn("inferenceModels", stored)

    def test_use_saved_model_never_writes_a_name_desktop_would_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime_config = root / "config.yaml"
            runtime_config.write_text(
                """
model_list:
  - model_name: grok-4.7
    litellm_params:
      model: openai/grok-4.7
      api_base: https://relay.example.test/v1
      api_key: sk-route
    model_info:
      id: dep-a
      provider: prov-a
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
                {"client": "claudeDesktop", "model": "grok-4.7", "provider": "prov-a", "deployment_id": "dep-a"},
            )

            # The route is still what the client talks to; only the picker name
            # is withheld, because Desktop would delete it on the next launch.
            self.assertEqual([], snapshot["desktop"]["model_names"])
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertEqual("https://relay.example.test", config["inferenceGatewayBaseUrl"])


if __name__ == "__main__":
    unittest.main()

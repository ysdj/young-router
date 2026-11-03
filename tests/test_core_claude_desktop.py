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

            with mock.patch.object(domain, "_public_models", return_value=["claude-opus-5-5"]):
                enabled = domain.dispatch("claude.models.set", {"client": "claudeDesktop", "enabled": True})
                domain.apply()
            self.assertTrue(enabled["clients"]["claudeDesktop"]["model_list_enabled"])
            self.assertEqual(["claude-opus-5-5"], enabled["desktop"]["model_names"])

            with mock.patch.object(domain, "_public_models", return_value=["claude-opus-5-5", "claude-sonnet-4-5"]):
                self.assertTrue(domain.refresh_model_lists())
            self.assertEqual(["claude-opus-5-5", "claude-sonnet-4-5"], domain.snapshot()["desktop"]["model_names"])

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

    def test_the_list_is_ordered_by_the_name_the_picker_shows(self) -> None:
        # The picker is read top-down and picked by number, so its order is the
        # reader's, not the router's: the label a row displays is what sorts it,
        # and a row Desktop renders itself sorts by its own id.
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
            public = ["grok-4.7", "kimi-k3.1-pro", "claude-opus-5-5", "deepseek-v4.1-flash"]
            with mock.patch.object(domain, "_public_models", return_value=public):
                entries = domain.desktop_model_entries()

            shown = [row.get("labelOverride") or row["name"] for row in entries]
            self.assertEqual(sorted(shown, key=str.casefold), shown)
            self.assertEqual(
                ["claude-opus-5-5", "deepseek-v4.1-flash", "grok-4.7", "kimi-k3.1-pro"],
                shown,
            )

    def test_a_snapshot_repairs_a_list_the_router_outgrew(self) -> None:
        # A proxy reload regenerates the list, which covers a provider Apply.
        # The other order is a route that changed while no reload was owed, and
        # an open window is then the only thing that looks: its snapshot makes
        # the list current, the way the Codex catalog is kept current by the
        # snapshot that draws it.
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
            before = len(domain.snapshot()["desktop"]["model_names"])

            with mock.patch.object(
                domain,
                "_public_models",
                return_value=["deepseek-v4.1-flash", "brand-new-route"],
            ):
                domain._model_list_refreshed_at = 0.0
                after = domain.snapshot()["desktop"]["model_names"]

            self.assertEqual(before + 1, len(after))
            self.assertIn(alias_claude_desktop("brand-new-route"), after)

    def test_a_snapshot_does_not_re_derive_the_list_on_every_poll(self) -> None:
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
                domain._model_list_refreshed_at = 0.0
                with mock.patch.object(domain, "_public_models", return_value=["deepseek-v4.1-flash"]) as reader:
                    domain.snapshot()
                    calls_after_first = reader.call_count
                    domain.snapshot()
                    domain.snapshot()
                    self.assertEqual(calls_after_first, reader.call_count)

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

    def test_a_new_public_model_reaches_the_desktop_list_without_edits(self) -> None:
        # The complaint that produced the derivation: a provider added later
        # must appear by itself, because nothing here is a maintained table.
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
            before = domain.snapshot()["desktop"]["model_names"]
            with mock.patch.object(
                domain, "_public_models", return_value=["deepseek-v4.1-flash", "some-new-model-x"]
            ):
                self.assertTrue(domain.refresh_model_lists())
            after = domain.snapshot()["desktop"]["model_names"]
            self.assertEqual(len(before) + 1, len(after))
            self.assertIn(alias_claude_desktop("some-new-model-x"), after)

    def test_a_picker_desktop_stripped_still_reads_as_on_and_is_repaired(self) -> None:
        # Desktop deletes the entries it cannot map, so a list this app wrote
        # can come back shorter than the record.  Reading that as "off" left the
        # file permanently broken: no refresh would ever run again.
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

            # Desktop rewrites the file on disk, so the observation comes from
            # disk rather than from a staged draft -- a draft would be the
            # user's own edit and a refresh must not commit it.
            config_path = domain._desktop.desktop_config_path
            self.assertIsNotNone(config_path)
            stored = json.loads(config_path.read_text(encoding="utf-8"))
            stored["inferenceModels"] = stored["inferenceModels"][:1]
            config_path.write_text(json.dumps(stored), encoding="utf-8")
            domain.reload()
            self.assertTrue(domain.snapshot()["clients"]["claudeDesktop"]["model_list_enabled"])

            with mock.patch.object(domain, "_public_models", return_value=public):
                self.assertTrue(domain.refresh_model_lists())
            self.assertEqual(2, len(domain.snapshot()["desktop"]["model_names"]))
            # A refresh rewrites the list, so it has to carry the labels too:
            # writing the bare ids is what left the picker showing
            # ``claude-mythos-xxxxxx`` instead of the model each one stands for.
            refreshed = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertTrue(all(row.get("labelOverride") for row in refreshed["inferenceModels"]))

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

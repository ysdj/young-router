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
from young_router.core.domains import claude as claude_domain
from young_router.core.domains.claude import ClaudeSettingsDomain


def _write_runtime_config(path: pathlib.Path, declarations: dict[str, int]) -> pathlib.Path:
    """A routes document that declares a window on the named routes.

    JSON is valid YAML, which is all ``codex_config.load_yaml`` needs.  What
    matters is that the number is *on the route*: that is the only statement
    that may turn a route into a 1M claim.  A window this app resolved from a
    profile or an upstream catalogue is not on any route and therefore never
    reaches this fixture.
    """

    path.write_text(
        json.dumps(
            {
                "model_list": [
                    {"model_name": name, "model_info": {"max_input_tokens": window}}
                    for name, window in declarations.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


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
    def setUp(self) -> None:
        # These fixtures do not have the model-list plugin installed.
        plugin = mock.patch("young_router.core.model_list_plugin.target_enabled", return_value=False)
        plugin.start()
        self.addCleanup(plugin.stop)

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
        # switch.
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

    def test_the_edit_api_form_reports_the_live_connection(self) -> None:
        # The dialog opens on the live Code pair; saving a different
        # endpoint-and-key pair moves the client off yrapi, and saving this
        # app's own pair moves it back on.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            (root / "settings.json").write_text(json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "https://relay.example.test",
                    "ANTHROPIC_AUTH_TOKEN": "direct-key",
                },
                "model": "claude-direct",
            }), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                desktop_loader=None,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch(
                "young_router.core.domains._shared_api.endpoint",
                return_value=("http://127.0.0.1:4100/v1", "sk-local"),
            ):
                domain.dispatch("use_local_api", {"client": "claudeCode", "model": "public-a"})
                self.assertTrue(domain.snapshot()["clients"]["claudeCode"]["uses_yrapi_api"])
                form = domain.snapshot()["direct_api"]
                self.assertEqual("http://127.0.0.1:4100", form["base_url"])
                self.assertEqual("public-a", form["model"])
                self.assertTrue(form["key_configured"])
                self.assertEqual("bearer", form["auth_mode"])
                self.assertEqual("sk-local", domain.trusted_secret_value("direct_api_key", "edit-api"))

                saved = domain.dispatch("use_direct_api", {
                    "base_url": "https://direct.example.test",
                    "model": "claude-next",
                    "api_key": "next-direct-key",
                    "auth_mode": "bearer",
                })
            self.assertFalse(saved["clients"]["claudeCode"]["uses_yrapi_api"])
            code = json.loads(domain.raw_text(include_sensitive=True))
            self.assertEqual("https://direct.example.test", code["env"]["ANTHROPIC_BASE_URL"])
            self.assertEqual("next-direct-key", code["env"]["ANTHROPIC_AUTH_TOKEN"])

            # URL and key alone are Claude's whole yrapi pair; the model is
            # not part of it.
            with mock.patch(
                "young_router.core.domains._shared_api.endpoint",
                return_value=("http://127.0.0.1:4100/v1", "sk-local"),
            ):
                domain.dispatch("use_direct_api", {
                    "base_url": "http://127.0.0.1:4100",
                    "model": "claude-next",
                    "api_key": "sk-local",
                    "auth_mode": "bearer",
                })
            self.assertTrue(domain.snapshot()["clients"]["claudeCode"]["uses_yrapi_api"])
            # The form follows the live connection onto the proxy.
            self.assertEqual("http://127.0.0.1:4100", domain.snapshot()["direct_api"]["base_url"])
            self.assertEqual("sk-local", domain.trusted_secret_value("direct_api_key", "edit-api"))

    def test_a_sealed_desktop_credential_still_names_this_apps_api(self) -> None:
        # Claude Desktop seals the gateway key with its own Safe Storage after
        # it restarts.  The envelope is the credential the profile carries;
        # comparing it to the plaintext token would turn the pane's switch off
        # while both apps still name this proxy.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {
                "inferenceProvider": "gateway",
                "inferenceGatewayBaseUrl": "http://127.0.0.1:4100",
                "inferenceGatewayAuthScheme": "bearer",
                "inferenceGatewayApiKey": "__sealed__:v1:synthetic-envelope",
            })
            (root / "settings.json").write_text(json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4100",
                    "ANTHROPIC_AUTH_TOKEN": "sk-local",
                },
            }), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                desktop_loader=None,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch(
                "young_router.core.domains._shared_api.endpoint",
                return_value=("http://127.0.0.1:4100/v1", "sk-local"),
            ):
                domain.refresh_local_api_endpoint()
                clients = domain.snapshot()["clients"]
                self.assertTrue(clients["claudeCode"]["uses_yrapi_api"])
                self.assertTrue(clients["claudeDesktop"]["uses_yrapi_api"])
                self.assertTrue(domain.desktop_uses_yrapi_api())

    def test_a_plaintext_desktop_credential_for_another_key_stays_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {
                "inferenceProvider": "gateway",
                "inferenceGatewayBaseUrl": "http://127.0.0.1:4100",
                "inferenceGatewayAuthScheme": "bearer",
                "inferenceGatewayApiKey": "another-key",
            })
            (root / "settings.json").write_text(json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4100",
                    "ANTHROPIC_AUTH_TOKEN": "sk-local",
                },
            }), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                desktop_loader=None,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch(
                "young_router.core.domains._shared_api.endpoint",
                return_value=("http://127.0.0.1:4100/v1", "sk-local"),
            ):
                domain.refresh_local_api_endpoint()
                clients = domain.snapshot()["clients"]
                self.assertTrue(clients["claudeCode"]["uses_yrapi_api"])
                self.assertFalse(clients["claudeDesktop"]["uses_yrapi_api"])

    def test_leaving_the_local_api_restores_both_claude_apps(self) -> None:
        # Claude Code and Claude Desktop keep one connection each; off returns
        # both to what they used before the proxy was adopted.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {
                "inferenceProvider": "gateway",
                "inferenceGatewayBaseUrl": "https://desktop.example.test",
                "inferenceGatewayAuthScheme": "x-api-key",
                "inferenceGatewayApiKey": "desktop-direct-key",
            })
            (root / "settings.json").write_text(json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "https://code.example.test/v1",
                    "ANTHROPIC_API_KEY": "code-direct-key",
                },
                "model": "claude-code-direct",
            }), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                desktop_loader=None,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch(
                "young_router.core.domains._shared_api.endpoint",
                return_value=("http://127.0.0.1:4100/v1", "sk-local"),
            ):
                domain.dispatch("use_local_api", {"client": "claudeCode"})
                domain.dispatch("use_local_api", {"client": "claudeDesktop"})

            domain.dispatch("leave_local_api", {"client": "claudeCode"})
            domain.dispatch("leave_local_api", {"client": "claudeDesktop"})
            self.assertFalse(domain.snapshot()["clients"]["claudeCode"]["uses_local_api"])
            self.assertFalse(domain.snapshot()["clients"]["claudeDesktop"]["uses_local_api"])
            code = json.loads(domain.raw_text(include_sensitive=True))
            self.assertEqual("https://code.example.test", code["env"]["ANTHROPIC_BASE_URL"])
            self.assertEqual("code-direct-key", code["env"]["ANTHROPIC_API_KEY"])
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", code["env"])
            desktop = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertEqual("https://desktop.example.test", desktop["inferenceGatewayBaseUrl"])
            self.assertEqual("x-api-key", desktop["inferenceGatewayAuthScheme"])
            self.assertEqual("desktop-direct-key", desktop["inferenceGatewayApiKey"])

    def test_leaving_without_a_backup_keeps_the_client_until_the_form_is_saved(self) -> None:
        # A client already on the proxy when this build starts has no backup;
        # opening the form does not silently clear the working connection.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {
                "inferenceProvider": "gateway",
                "inferenceGatewayBaseUrl": "http://127.0.0.1:4100",
                "inferenceGatewayAuthScheme": "bearer",
                "inferenceGatewayApiKey": "sk-local",
            })
            (root / "settings.json").write_text(json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4100",
                    "ANTHROPIC_AUTH_TOKEN": "sk-local",
                },
            }), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                desktop_loader=None,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with mock.patch(
                "young_router.core.domains._shared_api.endpoint",
                return_value=("http://127.0.0.1:4100/v1", "sk-local"),
            ):
                domain.refresh_local_api_endpoint()
                self.assertTrue(domain.snapshot()["clients"]["claudeCode"]["uses_yrapi_api"])
                self.assertTrue(domain.snapshot()["clients"]["claudeDesktop"]["uses_yrapi_api"])
                domain.dispatch("leave_local_api", {"client": "claudeCode"})
                domain.dispatch("leave_local_api", {"client": "claudeDesktop"})

            self.assertTrue(domain.snapshot()["clients"]["claudeCode"]["uses_yrapi_api"])
            self.assertTrue(domain.snapshot()["clients"]["claudeDesktop"]["uses_yrapi_api"])
            code = json.loads(domain.raw_text(include_sensitive=True))
            self.assertEqual("http://127.0.0.1:4100", code["env"]["ANTHROPIC_BASE_URL"])
            self.assertEqual("sk-local", code["env"]["ANTHROPIC_AUTH_TOKEN"])
            # The form starts from the live pair, so it can be edited into a
            # direct one.
            form = domain.snapshot()["direct_api"]
            self.assertEqual("http://127.0.0.1:4100", form["base_url"])
            self.assertEqual(
                "sk-local", domain.trusted_secret_value("direct_api_key", "edit-api")
            )

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
                # same model.  The window each row states is the registry's, and
                # these two names have no profile, so it is the published
                # placeholder -- what the pane shows for the same route.
                self.assertEqual(
                    [
                        alias_claude_desktop("public-a"),
                        alias_claude_desktop("public-b"),
                    ],
                    [row["model"] for row in picker["options"]],
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
            _write_runtime_config(
                root / "config.yaml",
                {"gpt-6.1-sol": 500_000, "kimi-k3.1-pro": 1_000_000},
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            public = ["claude-opus-5-5", "gemini-3.8-flash", "gpt-6.1-sol", "kimi-k3.1-pro"]
            rows = [
                {"id": "claude-opus-5-5", "max_input_tokens": 1_000_000},
                {"id": "gemini-3.8-flash", "max_input_tokens": 1_048_576},
                {"id": "gpt-6.1-sol", "max_input_tokens": 500_000},
                {"id": "kimi-k3.1-pro", "max_input_tokens": 1_000_000},
            ]
            with (
                mock.patch.object(domain, "_public_models", return_value=public),
                mock.patch.object(domain, "_public_model_rows", return_value=rows),
            ):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.apply()

            picker = json.loads(domain.raw_text(include_sensitive=True))["modelPicker"]
            self.assertTrue(picker["replaceBuiltInOptions"])
            # One row per route, and each route is named the way the desktop
            # session names it: at or above 1M the client's own ``[1m]`` spelling,
            # including a public name the client already keeps as written.
            self.assertEqual(
                [
                    "claude-opus-5-5[1m]",
                    f"{alias_claude_desktop('gemini-3.8-flash')}[1m]",
                    alias_claude_desktop("gpt-6.1-sol"),
                    f"{alias_claude_desktop('kimi-k3.1-pro')}[1m]",
                ],
                [row["model"] for row in picker["options"]],
            )
            by_id = {row["model"]: row for row in picker["options"]}
            # A route this app publishes at 1M is named as 1M, whoever wrote the
            # number: the pane, the picker row and the discovery row state one
            # window.
            self.assertEqual(
                {
                    "model": f"{alias_claude_desktop('gemini-3.8-flash')}[1m]",
                    "description": "\u200b",
                    "label": "gemini-3.8-flash",
                    "behavesAs": "claude-opus-5-5",
                },
                by_id[f"{alias_claude_desktop('gemini-3.8-flash')}[1m]"],
            )
            # A 500k route gets a 1M behavior model, because the tier caps the
            # window; the exact 500k comes from its per-model autoCompactWindow.
            # The smaller-output 1M entry is the one below a full 1M.
            self.assertEqual("claude-opus-4-8", by_id[alias_claude_desktop("gpt-6.1-sol")]["behavesAs"])
            self.assertEqual("gpt-6.1-sol", by_id[alias_claude_desktop("gpt-6.1-sol")]["label"])
            # A real Claude id needs no substitute, and its row is the client's
            # own: the suffix is the whole statement, so no label either.
            self.assertEqual(
                {"model": "claude-opus-5-5[1m]", "description": "\u200b"},
                by_id["claude-opus-5-5[1m]"],
            )
            settings = json.loads(domain.raw_text(include_sensitive=True))["modelSettings"]
            # A Claude-shaped 1M route keeps the number it states, under the id
            # the row names and under the id a session actually runs.
            self.assertEqual(1_000_000, settings["claude-opus-5-5[1m]"]["autoCompactWindow"])
            self.assertEqual(1_000_000, settings["claude-opus-5-5"]["autoCompactWindow"])
            self.assertEqual(500_000, settings[alias_claude_desktop("gpt-6.1-sol")]["autoCompactWindow"])
            self.assertEqual(
                1_000_000,
                settings[f"{alias_claude_desktop('kimi-k3.1-pro')}[1m]"]["autoCompactWindow"],
            )
            self.assertEqual(
                1_000_000,
                settings[f"{alias_claude_desktop('gemini-3.8-flash')}[1m]"]["autoCompactWindow"],
            )
            # A public-name entry nobody wrote is not invented beside them.
            self.assertNotIn("gpt-6.1-sol", settings)

    def test_adopting_the_local_api_launches_the_id_the_picker_names(self) -> None:
        # A client pointed at this proxy has to name a route the way this app
        # publishes it.  The public name is not that name: a gateway id has no
        # catalogue entry in the client, so a session launched with it answers
        # the client's 200k default and drops the effort ladder, however wide
        # the route is.  The picker id is what the row -- and its
        # ``autoCompactWindow`` and ``behavesAs`` -- is keyed by, so the launch
        # spelling is the row's own.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            _write_runtime_config(
                root / "config.yaml",
                {"gpt-6.1-sol": 500_000, "kimi-k3.1-pro": 1_000_000},
            )
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            public = ["gpt-6.1-sol", "kimi-k3.1-pro"]
            rows = [
                {"id": "gpt-6.1-sol", "max_input_tokens": 500_000},
                {"id": "kimi-k3.1-pro", "max_input_tokens": 1_000_000},
            ]
            one_million = f"{alias_claude_desktop('kimi-k3.1-pro')}[1m]"
            half_million = alias_claude_desktop("gpt-6.1-sol")
            with (
                mock.patch.object(domain, "_public_models", return_value=public),
                mock.patch.object(domain, "_public_model_rows", return_value=rows),
                mock.patch(
                    "young_router.core.domains._shared_api.endpoint",
                    return_value=("http://127.0.0.1:4100/v1", "sk-local"),
                ),
            ):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.dispatch(
                    "use_local_api",
                    {"client": "claudeCode", "model": "kimi-k3.1-pro", "provider": "p", "deployment_id": "d"},
                )
                domain.apply()
                settings = json.loads(domain.raw_text(include_sensitive=True))
                # One statement in both places the client reads it, and a name
                # the file itself answers.
                self.assertEqual(one_million, settings["env"]["ANTHROPIC_MODEL"])
                self.assertEqual(one_million, settings["model"])
                self.assertIn(one_million, {row["model"] for row in settings["modelPicker"]["options"]})

                # A route below 1M keeps the plain id: the row states its window
                # through ``behavesAs`` and the number under ``modelSettings``.
                domain.dispatch(
                    "use_local_api",
                    {"client": "claudeCode", "model": "gpt-6.1-sol", "provider": "p", "deployment_id": "d"},
                )
                domain.apply()
                settings = json.loads(domain.raw_text(include_sensitive=True))
                self.assertEqual(half_million, settings["env"]["ANTHROPIC_MODEL"])
                self.assertNotIn(f"{half_million}[1m]", {row["model"] for row in settings["modelPicker"]["options"]})

    def test_a_refresh_moves_the_launch_model_onto_the_row_it_names(self) -> None:
        # The list is regenerated whenever the router's routes change, and a
        # route that moves onto (or off) the client's 1M spelling is a new id.
        # A launch model left behind names a spelling the file no longer
        # answers -- the session would keep the window it had, which is the
        # 200k default for a route that is now 1M.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            _write_runtime_config(root / "config.yaml", {"kimi-k3.1-pro": 1_000_000})
            one_million = f"{alias_claude_desktop('kimi-k3.1-pro')}[1m]"
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4100",
                            "ANTHROPIC_MODEL": "kimi-k3.1-pro",
                        },
                        "model": "kimi-k3.1-pro",
                        "modelPicker": {"replaceBuiltInOptions": True, "options": []},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch(
                "young_router.core.domains._shared_api.endpoint",
                return_value=("http://127.0.0.1:4100/v1", "sk-local"),
            ):
                domain = ClaudeSettingsDomain(
                    root / "settings.json",
                    desktop_config_library_path=desktop_root,
                    runtime_config_path=root / "config.yaml",
                    runtime_root=root,
                )
                public = ["kimi-k3.1-pro"]
                rows = [{"id": "kimi-k3.1-pro", "max_input_tokens": 1_000_000}]
                with (
                    mock.patch.object(domain, "_public_models", return_value=public),
                    mock.patch.object(domain, "_public_model_rows", return_value=rows),
                ):
                    self.assertTrue(domain.refresh_model_lists())

            settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(one_million, settings["env"]["ANTHROPIC_MODEL"])
            self.assertEqual(one_million, settings["model"])

    def test_a_refresh_leaves_a_model_this_app_does_not_serve_alone(self) -> None:
        # ``model`` is also where a person types a deployment id or a model this
        # router does not expose.  No row answers for it, so a refresh has
        # nothing to say about the spelling and must not invent one.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            _write_runtime_config(root / "config.yaml", {"kimi-k3.1-pro": 1_000_000})
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4100",
                            "ANTHROPIC_MODEL": "gpt-5.4",
                        },
                        "model": "gpt-5.4",
                        "modelPicker": {"replaceBuiltInOptions": True, "options": []},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch(
                "young_router.core.domains._shared_api.endpoint",
                return_value=("http://127.0.0.1:4100/v1", "sk-local"),
            ):
                domain = ClaudeSettingsDomain(
                    root / "settings.json",
                    desktop_config_library_path=desktop_root,
                    runtime_config_path=root / "config.yaml",
                    runtime_root=root,
                )
                with (
                    mock.patch.object(domain, "_public_models", return_value=["kimi-k3.1-pro"]),
                    mock.patch.object(
                        domain,
                        "_public_model_rows",
                        return_value=[{"id": "kimi-k3.1-pro", "max_input_tokens": 1_000_000}],
                    ),
                ):
                    domain.refresh_model_lists()

            settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual("gpt-5.4", settings["env"]["ANTHROPIC_MODEL"])
            self.assertEqual("gpt-5.4", settings["model"])

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
            # One id space: the row names the id both Claude apps send -- which
            # for a route published at 1M carries the client's own spelling --
            # and a field the user typed on the older spelling follows it there.
            self.assertEqual(f"{alias_claude_desktop('gemini-3.8-flash')}[1m]", row["model"])
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
            gemini = f"{alias_claude_desktop('gemini-3.8-flash')}[1m]"
            # A user's lower window and effort choice survive a regeneration and
            # follow the id the picker now names; the new route gets the derived
            # 500k under the same kind of id.
            self.assertEqual(500_000, settings[gemini]["autoCompactWindow"])
            self.assertEqual("high", settings[gemini]["effortLevel"])
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
            _write_runtime_config(root / "config.yaml", {"gemini-3.8-flash": 1_048_576})
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
            # The declared 1M route is named the way the desktop session names
            # it, so one row serves both apps.
            self.assertEqual(
                [
                    {
                        "model": f"{alias_claude_desktop('gemini-3.8-flash')}[1m]",
                        "description": "\u200b",
                        "label": "gemini-3.8-flash",
                        "behavesAs": "claude-opus-5-5",
                    }
                ],
                rows,
            )

    def test_a_pass_leaves_settings_it_does_not_own_alone(self) -> None:
        # The generated document lives inside the person's own settings file.
        # A pass rewrites the two keys it derives and nothing else: hooks, a
        # permission list, an env entry and a key this app has never heard of
        # are theirs, and a repair that dropped one would be a rewrite nobody
        # asked for.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            settings = {
                "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:4100", "MY_FLAG": "keep-me"},
                "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
                "permissions": {"allow": ["Bash(git status:*)"]},
                "myOwnKey": {"nested": [1, 2, 3]},
                "modelPicker": {"replaceBuiltInOptions": True, "options": []},
            }
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with (
                mock.patch.object(domain, "_public_models", return_value=["gpt-6.1-sol"]),
                mock.patch.object(
                    domain,
                    "_public_model_rows",
                    return_value=[{"id": "gpt-6.1-sol", "max_input_tokens": 500_000}],
                ),
            ):
                self.assertTrue(domain.refresh_model_lists())

            written = json.loads(domain.raw_text(include_sensitive=True))
            for key in ("env", "hooks", "permissions", "myOwnKey"):
                self.assertEqual(settings[key], written[key], key)
            self.assertEqual(1, len(written["modelPicker"]["options"]))

    def test_a_pass_writes_nothing_when_only_other_settings_moved(self) -> None:
        # The reverse half: a file whose unrelated keys changed is not a reason
        # to rewrite the generated ones.  Writing on every pass would bump the
        # mtime of somebody else's document for no reason.
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
            with (
                mock.patch.object(domain, "_public_models", return_value=["gpt-6.1-sol"]),
                mock.patch.object(
                    domain,
                    "_public_model_rows",
                    return_value=[{"id": "gpt-6.1-sol", "max_input_tokens": 500_000}],
                ),
            ):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.apply()
                document = json.loads((root / "settings.json").read_text(encoding="utf-8"))
                document["myOwnKey"] = "typed by a person"
                (root / "settings.json").write_text(json.dumps(document), encoding="utf-8")
                domain.reload()
                self.assertFalse(domain.refresh_model_lists())
            written = json.loads((root / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual("typed by a person", written["myOwnKey"])

    def test_a_superseded_public_name_entry_is_retired_not_duplicated(self) -> None:
        # Every generated entry is ours: one number for one route.  A file that
        # kept the old spelling beside the new id would describe the same model
        # twice and grow a second entry per model on every change.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            settings = {"modelSettings": {"gemini-3.8-flash": {"autoCompactWindow": "auto"}}}
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
            # The entry the user had under the public name follows the route to
            # the ids it is named by now -- the picker's row and the suffix-free
            # one a session runs -- and the old spelling is retired rather than
            # left beside them describing one model twice.  ``auto`` was this
            # app's "window unknown" marker, so the resolved number replaces it.
            alias = alias_claude_desktop("gemini-3.8-flash")
            self.assertEqual(
                {
                    f"{alias}[1m]": {"autoCompactWindow": 1_000_000},
                    alias: {"autoCompactWindow": 1_000_000},
                },
                settings,
            )

    def test_a_removed_routes_derived_alias_window_is_retired(self) -> None:
        """A window for a route this app no longer serves describes nothing.

        The alias index is derived from the current public names, so an old
        derived id cannot be routed any more; keeping only its window would
        leave a growing history of dead ids.  A person's own field keeps the
        entry, and an id outside this app's alias family is never touched.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            dead = alias_claude_desktop("retired-model")
            dead_legacy = "claude-fable-" + dead.removeprefix(FABLE_ALIAS_PREFIX)
            live = alias_claude_desktop("public-a")
            launch = alias_claude_desktop("active-but-retired")
            settings = {
                "model": launch,
                "modelSettings": {
                    dead: {"autoCompactWindow": 500_000},
                    f"{dead}[1m]": {"autoCompactWindow": 1_000_000},
                    dead_legacy: {"autoCompactWindow": 200_000},
                    launch: {"autoCompactWindow": 400_000},
                    live: {"autoCompactWindow": 200_000, "effort": "a person's choice"},
                    "typed-by-a-person": {"autoCompactWindow": 300_000},
                },
            }
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with (
                mock.patch.object(domain, "_public_models", return_value=["public-a"]),
                mock.patch.object(
                    domain,
                    "_public_model_rows",
                    return_value=[{"id": "public-a", "max_input_tokens": 200_000}],
                ),
            ):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.apply()

            written = json.loads(domain.raw_text(include_sensitive=True))["modelSettings"]
            self.assertNotIn(dead, written)
            self.assertNotIn(f"{dead}[1m]", written)
            self.assertNotIn(dead_legacy, written)
            # The id the client is launched with is always kept, even when it
            # no longer names a current route.
            self.assertIn(launch, written)
            self.assertEqual("a person's choice", written[live]["effort"])
            self.assertIn("typed-by-a-person", written)

    def test_a_one_million_window_is_named_1m_whatever_wrote_the_number(self) -> None:
        # One window per route, and this app publishes it: the pane, the picker
        # row and the discovery row state the same number.  At or above 1M that
        # statement is the client's ``[1m]`` spelling, which also keeps the
        # picker from synthesizing a second row for the same route.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            settings = {
                "modelPicker": {
                    "replaceBuiltInOptions": True,
                    "options": [
                        {
                            "model": f"{alias_claude_desktop('gemini-3.8-flash')}[1m]",
                            "description": "\u200b",
                            "behavesAs": "claude-opus-5-5",
                        }
                    ],
                }
            }
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            # A routes document with the group present and no window on it.
            (root / "config.yaml").write_text(
                json.dumps({"model_list": [{"model_name": "gemini-3.8-flash"}]}),
                encoding="utf-8",
            )
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

            picker = json.loads(domain.raw_text(include_sensitive=True))["modelPicker"]
            suffixed = f"{alias_claude_desktop('gemini-3.8-flash')}[1m]"
            self.assertEqual(
                [
                    {
                        "model": suffixed,
                        "description": "\u200b",
                        "label": "gemini-3.8-flash",
                        "behavesAs": "claude-opus-5-5",
                    }
                ],
                picker["options"],
            )
            # The route the user typed under the plain alias is moved onto the
            # row's ids, and the suffix-free one is what the session runs.
            settings = json.loads(domain.raw_text(include_sensitive=True))["modelSettings"]
            self.assertEqual(
                {
                    suffixed: {"autoCompactWindow": 1_000_000},
                    alias_claude_desktop("gemini-3.8-flash"): {"autoCompactWindow": 1_000_000},
                },
                settings,
            )

    def test_a_declared_one_million_route_states_the_id_a_session_runs(self) -> None:
        # The suffix names a picker row; the session runs the suffix-free id, so
        # that entry is the one the client reads its window from.  Dropping it
        # when the route moved to 1M left the session on the 200k tier this app
        # had written for the same route earlier.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            settings = {
                "modelSettings": {
                    # Exactly what the previous release wrote for this route: it
                    # read 1M from the published number, under the unsuffixed id.
                    alias_claude_desktop("kimi-k3.1-pro"): {"autoCompactWindow": 1_000_000},
                }
            }
            (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            _write_runtime_config(root / "config.yaml", {"kimi-k3.1-pro": 1_000_000})
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with (
                mock.patch.object(domain, "_public_models", return_value=["kimi-k3.1-pro"]),
                mock.patch.object(
                    domain,
                    "_public_model_rows",
                    return_value=[{"id": "kimi-k3.1-pro", "max_input_tokens": 1_000_000}],
                ),
            ):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.apply()

            suffixed = f"{alias_claude_desktop('kimi-k3.1-pro')}[1m]"
            settings = json.loads(domain.raw_text(include_sensitive=True))["modelSettings"]
            self.assertEqual(
                {
                    suffixed: {"autoCompactWindow": 1_000_000},
                    alias_claude_desktop("kimi-k3.1-pro"): {"autoCompactWindow": 1_000_000},
                },
                settings,
            )

    def test_a_route_that_moved_to_1m_moves_the_window_a_session_reads(self) -> None:
        # The document a user was left holding: the route's row was named 1M by
        # an earlier pass while the id a session runs still carried the 200k
        # tier this app wrote when the window was unknown.  The desktop Code tab
        # showed 200k for a route published at 1M, and no refresh repaired it
        # because the suffixed entry already agreed with itself.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            desktop_root = root / "configLibrary"
            _write_desktop_config(desktop_root, {})
            alias = alias_claude_desktop("deepseek-v4.1-flash")
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "model": f"{alias}[1m]",
                        "modelPicker": {"replaceBuiltInOptions": True, "options": []},
                        "modelSettings": {
                            alias: {"autoCompactWindow": 200_000},
                            f"{alias}[1m]": {"autoCompactWindow": 1_000_000},
                        },
                    }
                ),
                encoding="utf-8",
            )
            _write_runtime_config(root / "config.yaml", {"deepseek-v4.1-flash": 1_000_000})
            domain = ClaudeSettingsDomain(
                root / "settings.json",
                desktop_config_library_path=desktop_root,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            with (
                mock.patch.object(domain, "_public_models", return_value=["deepseek-v4.1-flash"]),
                mock.patch.object(
                    domain,
                    "_public_model_rows",
                    return_value=[{"id": "deepseek-v4.1-flash", "max_input_tokens": 1_000_000}],
                ),
            ):
                self.assertTrue(domain.refresh_model_lists())

            settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))["modelSettings"]
            self.assertEqual(1_000_000, settings[alias]["autoCompactWindow"])
            self.assertEqual(1_000_000, settings[f"{alias}[1m]"]["autoCompactWindow"])

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

    def test_direct_api_clears_the_picker_list_that_named_this_proxy(self) -> None:
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

            left = domain.dispatch("use_direct_api", {
                "base_url": "https://relay.example.test/v1",
                "api_key": "fixture-direct",
                "auth_mode": "bearer",
                "model": "claude-direct",
            })
            self.assertNotIn("modelPicker", json.loads(domain.raw_text(include_sensitive=True)))
            self.assertFalse(left["clients"]["claudeCode"]["model_list_enabled"])
            self.assertFalse(left["clients"]["claudeDesktop"]["model_list_enabled"])
            self.assertEqual([], left["desktop"]["model_names"])
            self.assertEqual("https://relay.example.test", left["desktop"]["gateway_url"])
            domain.apply()
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
                domain,
                "_public_model_rows",
                return_value=[{"id": "gpt-5.4", "max_input_tokens": 1_000_000}],
            ):
                self.assertTrue(domain._publishes_one_million_context())
                domain._prefer_one_million_context()
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertTrue(config["modelPrefer1mContext"])

    def test_the_preference_follows_the_window_this_app_publishes(self) -> None:
        # One statement per route: the number this app publishes is the number
        # every surface states.  A route it publishes below 1M therefore leaves
        # the picker's 1M preference alone, because there is no 1M row for it.
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
                domain,
                "_public_model_rows",
                return_value=[{"id": "gemini-3.8-flash", "max_input_tokens": 272_000}],
            ):
                self.assertFalse(domain._publishes_one_million_context())
                domain._prefer_one_million_context()
            config = json.loads(domain.raw_text(document="desktop", include_sensitive=True))
            self.assertNotIn("modelPrefer1mContext", config)

    def test_no_published_route_at_one_million_leaves_the_preference_off(self) -> None:
        # The preference is about the whole picker: it hides the standard row of
        # every route that reads as 1M.  With no route at 1M there is nothing to
        # collapse and the profile keeps its own answer.
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
            ), mock.patch.object(domain, "_public_model_rows", return_value=rows):
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
            with mock.patch.object(domain, "desktop_uses_local_api", return_value=True), mock.patch.object(
                domain, "_public_models", return_value=["gpt-5.4"]
            ), mock.patch.object(
                domain,
                "_public_model_rows",
                return_value=[{"id": "gpt-5.4", "max_input_tokens": 1_000_000}],
            ):
                self.assertTrue(domain._publishes_one_million_context())
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

    def test_the_model_list_plugin_owns_the_picker_only_while_client_and_target_agree(self) -> None:
        """One picker, one writer: the plugin's target and the client's own plugin switch.

        Claude Code runs a plugin's hooks only while ``enabledPlugins`` names it,
        so a target the plugin's configuration switched on is not enough on its
        own.  While both agree the background pass leaves the file alone; when
        either stops naming the plugin, the list this app restored is its own
        again instead of staying stale forever.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_runtime_config(root / "config.yaml", {"deepseek-v4.1-flash": 200_000})
            settings_path = root / "settings.json"
            settings_path.write_text(
                json.dumps({
                    "modelPicker": {"replaceBuiltInOptions": True, "options": [{"model": "stale-route"}]},
                }),
                encoding="utf-8",
            )
            domain = ClaudeSettingsDomain(
                settings_path,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            target = {"on": True}
            with mock.patch(
                "young_router.core.model_list_plugin.target_enabled",
                side_effect=lambda host: target["on"],
            ):
                self.assertFalse(domain.plugin_owns_model_list())
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                settings["enabledPlugins"] = {"model-list-plugin@model-list-plugin": True}
                settings_path.write_text(json.dumps(settings), encoding="utf-8")
                domain.reload()
                self.assertTrue(domain.plugin_owns_model_list())

                with mock.patch.object(domain, "_public_models", return_value=["deepseek-v4.1-flash"]):
                    self.assertFalse(domain.refresh_model_lists())
                kept = json.loads(settings_path.read_text(encoding="utf-8"))["modelPicker"]
                self.assertEqual("stale-route", kept["options"][0]["model"])

                # The plugin's own configuration turned this host off: the file
                # it restored is this app's to maintain again.
                target["on"] = False
                self.assertFalse(domain.plugin_owns_model_list())
                with mock.patch.object(domain, "_public_models", return_value=["deepseek-v4.1-flash"]):
                    self.assertTrue(domain.refresh_model_lists())
                rewritten = json.loads(settings_path.read_text(encoding="utf-8"))["modelPicker"]
                self.assertNotEqual("stale-route", rewritten["options"][0]["model"])

    def test_leaving_the_proxy_keeps_a_picker_the_plugin_maintains(self) -> None:
        """The pop belongs to the switch that created the list, not to the plugin's.

        A client leaving this proxy drops the picker this app generated, because
        those ids name routes only this proxy serves.  While the model list
        plugin owns the file, that list follows the client's new gateway instead:
        the plugin rewrites it on its next hook, so removing it here would fight
        the writer the user installed.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_runtime_config(root / "config.yaml", {"deepseek-v4.1-flash": 200_000})
            settings_path = root / "settings.json"
            settings_path.write_text(json.dumps({"env": {}}), encoding="utf-8")
            domain = ClaudeSettingsDomain(
                settings_path,
                runtime_config_path=root / "config.yaml",
                runtime_root=root,
            )
            target = {"on": True}
            with mock.patch(
                "young_router.core.model_list_plugin.target_enabled",
                side_effect=lambda host: target["on"],
            ), mock.patch.object(domain, "_public_models", return_value=["deepseek-v4.1-flash"]):
                domain.dispatch("claude.models.set", {"client": "claudeCode", "enabled": True})
                domain.apply()
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                settings["enabledPlugins"] = {"model-list-plugin@model-list-plugin": True}
                settings_path.write_text(json.dumps(settings), encoding="utf-8")
                domain.reload()

                direct = {
                    "base_url": "https://relay.example.test/v1",
                    "api_key": "fixture-direct",
                    "auth_mode": "bearer",
                    "model": "claude-direct",
                }
                left = domain.dispatch("use_direct_api", direct)
                self.assertIn("modelPicker", json.loads(domain.raw_text(include_sensitive=True)))
                self.assertTrue(left["clients"]["claudeCode"]["model_list_enabled"])

                # The plugin's handoff projection uses the same published id
                # as the app's picker: the derived alias (and its ``[1m]``
                # spelling).  A session launched from this form must name that
                # id, because the plugin's row describes the alias, not the
                # public route.
                with mock.patch(
                    "young_router.core.domains.claude._shared_api.endpoint",
                    return_value=("http://127.0.0.1:4000", "fixture-router-key"),
                ):
                    domain.dispatch("use_local_api", {"client": "claudeCode", "model": "deepseek-v4.1-flash"})
                adopted = json.loads(domain.raw_text(include_sensitive=True))
                self.assertEqual("claude-mythos-eb1394", adopted["env"]["ANTHROPIC_MODEL"])

                target["on"] = False
                left = domain.dispatch("use_direct_api", direct)
                self.assertNotIn("modelPicker", json.loads(domain.raw_text(include_sensitive=True)))
                self.assertFalse(left["clients"]["claudeCode"]["model_list_enabled"])


def test_apply_preserves_the_plugins_model_list(tmp_path, monkeypatch) -> None:
    """A settings edit after takeover must not write this app's old picker back.

    The plugin writes ``modelPicker``/``modelSettings`` behind the domain.  The
    next ordinary Apply owns the other keys, so it has to adopt the plugin's
    fields from disk and treat that known external change as the baseline, not
    as a conflict and not as a reason to restore the stale draft.
    """

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "config.json").write_text(json.dumps({
        "enabled": True,
        "targets": {"claude": True},
    }))
    monkeypatch.setenv("MODEL_LIST_PLUGIN_DATA", str(plugin_root))
    runtime = tmp_path / "config.yaml"
    runtime.write_text("model_list: []\n")
    settings_path = tmp_path / "settings.json"
    original = {
        "env": {},
        "enabledPlugins": {"model-list-plugin@model-list-plugin": True},
        "modelPicker": {"replaceBuiltInOptions": True, "options": [{"model": "stale"}]},
        "modelSettings": {"stale": {"autoCompactWindow": 100000}},
    }
    settings_path.write_text(json.dumps(original))
    domain = ClaudeSettingsDomain(
        settings_path,
        runtime_config_path=runtime,
        runtime_root=tmp_path,
    )
    plugin_picker = {
        "replaceBuiltInOptions": True,
        "options": [{"model": "claude-mythos-eb1394[1m]", "label": "deepseek-v4.1-flash"}],
    }
    plugin_settings = {
        **original,
        "modelPicker": plugin_picker,
        "modelSettings": {"claude-mythos-eb1394[1m]": {"autoCompactWindow": 1000000}},
    }
    settings_path.write_text(json.dumps(plugin_settings))

    domain.apply({"settings": {**original, "autoMemoryEnabled": False}})

    saved = json.loads(settings_path.read_text())
    assert saved["modelPicker"] == plugin_picker
    assert saved["modelSettings"] == plugin_settings["modelSettings"]
    assert saved["autoMemoryEnabled"] is False


if __name__ == "__main__":
    unittest.main()


def test_leaving_the_proxy_retires_a_picker_the_plugin_restored(tmp_path, monkeypatch) -> None:
    """The plugin restores the picker it found; that picker dies with the API.

    The plugin takes a snapshot before it starts writing.  When it is disabled
    on the way out, that snapshot is this app's own generated list.  Leaving
    the gateway means this app no longer owns the list either, so the restored
    rows must not outlive the connection they named.
    """

    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "config.json").write_text(json.dumps({
        "enabled": True,
        "targets": {"claude": False},
    }))
    monkeypatch.setenv("MODEL_LIST_PLUGIN_DATA", str(plugin_root))
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4100",
            "ANTHROPIC_AUTH_TOKEN": "sk-local",
        },
        "enabledPlugins": {"model-list-plugin@model-list-plugin": True},
        "modelPicker": {
            "replaceBuiltInOptions": True,
            "options": [{"model": "claude-mythos-eb1394[1m]"}],
        },
        "modelSettings": {"claude-mythos-eb1394[1m]": {"autoCompactWindow": 1000000}},
    }))
    from young_router.core.domains._shared_api import (
        store_direct_connection,
        store_generated_models,
    )
    store_direct_connection(tmp_path, "claudeCode", {
        "provider": "gateway",
        "base_url": "https://before.example.test",
        "auth_mode": "bearer",
        "event": "keep",
    })
    store_generated_models(tmp_path, "claudeCode", ["deepseek-v4.1-flash"])
    runtime = tmp_path / "config.yaml"
    runtime.write_text("model_list: []\n")
    domain = ClaudeSettingsDomain(
        settings_path,
        runtime_config_path=runtime,
        runtime_root=tmp_path,
    )
    with mock.patch.object(
        claude_domain._shared_api,
        "endpoint",
        return_value=("http://127.0.0.1:4100/v1", "sk-local"),
    ):
        domain.dispatch("leave_local_api", {"client": "claudeCode"})
    domain.apply()
    saved = json.loads(settings_path.read_text())
    assert "modelPicker" not in saved
    assert saved["env"]["ANTHROPIC_BASE_URL"] == "https://before.example.test"

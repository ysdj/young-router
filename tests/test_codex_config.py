from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEX_CONFIG = ROOT / "codex_config.py"


class CodexConfigTests(unittest.TestCase):
    def write_runtime_config(self, path: Path) -> None:
        path.write_text(
            textwrap.dedent(
                """
                providers:
                  active:
                    api_base: https://active.example.test/v1
                    api_keys:
                      - name: default
                        value: replace-me
                  disabled:
                    enabled: false
                    api_base: https://disabled.example.test/v1
                    api_keys:
                      - name: default
                        value: replace-me-disabled
                model_list:
                  - model_name: active-chat
                    litellm_params:
                      model: openai/active-chat
                    model_info:
                      id: a1b2c3d4
                      provider: active
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                  - model_name: disabled-chat
                    litellm_params:
                      model: openai/disabled-chat
                    model_info:
                      id: a1b2c3d5
                      provider: active
                      x-litellm-menu-model-enabled: false
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                  - model_name: unavailable-chat
                    litellm_params:
                      model: openai/unavailable-chat
                    model_info:
                      id: a1b2c3d6
                      provider: disabled
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                general_settings:
                  master_key: sk-test-local
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def run_command(
        self,
        runtime_config: Path,
        codex_home: Path,
        *arguments: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "LITELLM_CONFIG_FILE": str(runtime_config),
                "CODEX_HOME": str(codex_home),
            }
        )
        return subprocess.run(
            [sys.executable, str(CODEX_CONFIG), *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
        )

    def editor_request(self, config_text: str, auth_text: str, patch: dict | None = None) -> str:
        payload: dict[str, object] = {
            "config_text": config_text,
            "auth_text": auth_text,
        }
        if patch is not None:
            payload["patch"] = patch
        return json.dumps(payload)

    def test_editor_load_reports_existing_permission_conflict_without_hiding_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            config_text = (
                'model = "test-model"\n'
                'sandbox_mode = "workspace-write"\n'
                'default_permissions = ":workspace"\n'
            )
            (codex_home / "config.toml").write_text(config_text, encoding="utf-8")
            (codex_home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-editor-key"}\n', encoding="utf-8")

            result = self.run_command(runtime_config, codex_home, "load")

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(config_text, payload["config_text"])
            self.assertEqual('{"OPENAI_API_KEY": "sk-editor-key"}\n', payload["auth_text"])
            self.assertIn("Permissions conflict", payload["validation_error"])
            self.assertEqual("mixed", payload["structured"]["permissions"]["mode"])

    def test_editor_sync_invalid_raw_text_returns_safe_zero_exit_payload_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            config_path = codex_home / "config.toml"
            auth_path = codex_home / "auth.json"
            config_path.write_text('personality = "disk"\n', encoding="utf-8")
            auth_path.write_text('{"OPENAI_API_KEY": "disk-key"}\n', encoding="utf-8")
            marker = "sk-do-not-echo-from-parser"
            config_text = f'api_key = "{marker}\n'

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(config_text, "{}\n"),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(config_text, payload["config_text"])
            self.assertEqual("{}\n", payload["auth_text"])
            self.assertIn("config.toml is not valid TOML", payload["validation_error"])
            self.assertNotIn(marker, payload["validation_error"])
            self.assertEqual('personality = "disk"\n', config_path.read_text(encoding="utf-8"))
            self.assertEqual('{"OPENAI_API_KEY": "disk-key"}\n', auth_path.read_text(encoding="utf-8"))

    def test_editor_sync_round_trips_structured_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            raw_config = textwrap.dedent(
                """
                unknown_setting = "preserve"
                sandbox_mode = "workspace-write"

                [model_providers.previous]
                base_url = "https://previous.example.test/v1"
                wire_api = "responses"

                [mcp_servers.old]
                command = "old-command"
                """
            ).lstrip()
            raw_auth = '{"tokens": {"keep": true}}\n'
            structured_patch = {
                "model": "new-model",
                "review_model": "review-model",
                "model_provider": "custom-provider",
                "openai_base_url": "https://proxy.example.test/v1",
                "api_key": "sk-swift-editor-key",
                "cli_auth_credentials_store": "file",
                "forced_login_method": "api",
                "model_reasoning_effort": "high",
                "plan_mode_reasoning_effort": "medium",
                "model_reasoning_summary": "concise",
                "model_verbosity": "low",
                "personality": "friendly",
                "service_tier": "fast",
                "web_search": "live",
                "model_context_window": "128000",
                "model_auto_compact_token_limit": "80000",
                "tool_output_token_limit": "4096",
                "features": {
                    "fast_mode": True,
                    "goals": True,
                    "js_repl": True,
                    "experimental_use_unified_exec_tool": True,
                    "shell_snapshot": False,
                    "shell_tool": True,
                    "skill_mcp_dependency_install": False,
                    "personality": True,
                },
                "permissions": {
                    "mode": "profile",
                    "default_permissions": ":workspace",
                    "sandbox_mode": None,
                    "approval_policy": None,
                    "network_access": None,
                    "writable_roots": None,
                },
                "providers": [
                    {
                        "id": "custom-provider",
                        "name": "Custom provider",
                        "base_url": "https://upstream.example.test/v1",
                        "wire_api": "responses",
                        "env_key": "",
                        "requires_openai_auth": False,
                        "auth_command": "get-custom-token",
                    }
                ],
                "mcp_servers": [
                    {
                        "id": "project-tools",
                        "enabled": True,
                        "required": False,
                        "transport": "stdio",
                        "command": "project-mcp",
                        "url": "",
                    }
                ],
                "plugins": [{"id": "sample-plugin", "enabled": True}],
                "advanced": {
                    "shell_environment_inherit": "core",
                    "history_persistence": "save-all",
                    "agents_max_threads": "4",
                    "agents_max_depth": "2",
                    "file_opener": "vscode",
                    "mcp_oauth_credentials_store": "keyring",
                },
            }

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(raw_config, raw_auth, structured_patch),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsNone(payload["validation_error"])
            self.assertEqual("new-model", payload["structured"]["model"])
            self.assertEqual("review-model", payload["structured"]["review_model"])
            self.assertEqual("4096", payload["structured"]["tool_output_token_limit"])
            self.assertEqual("profile", payload["structured"]["permissions"]["mode"])
            provider = next(item for item in payload["structured"]["providers"] if item["id"] == "custom-provider")
            self.assertEqual("get-custom-token", provider["auth_command"])
            server = next(item for item in payload["structured"]["mcp_servers"] if item["id"] == "project-tools")
            self.assertEqual("project-mcp", server["command"])
            self.assertEqual("", server["url"])
            self.assertEqual("core", payload["structured"]["advanced"]["shell_environment_inherit"])
            self.assertEqual("4", payload["structured"]["advanced"]["agents_max_threads"])
            parsed = tomllib.loads(payload["config_text"])
            self.assertEqual("preserve", parsed["unknown_setting"])
            self.assertNotIn("sandbox_mode", parsed)
            self.assertEqual(":workspace", parsed["default_permissions"])
            self.assertEqual("get-custom-token", parsed["model_providers"]["custom-provider"]["auth"]["command"])
            self.assertEqual("project-mcp", parsed["mcp_servers"]["project-tools"]["command"])
            self.assertTrue(parsed["features"]["experimental_use_unified_exec_tool"])
            self.assertEqual("sk-swift-editor-key", json.loads(payload["auth_text"])["OPENAI_API_KEY"])

    def test_editor_apply_returns_raw_text_and_writes_both_files_0600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            config_text = 'model = "applied-model"\n'
            auth_text = '{"OPENAI_API_KEY": "sk-applied-editor-key"}\n'

            result = self.run_command(
                runtime_config,
                codex_home,
                "apply-editor",
                input_text=self.editor_request(config_text, auth_text),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["applied"])
            self.assertEqual(config_text, payload["config_text"])
            self.assertEqual(auth_text, payload["auth_text"])
            config_path = codex_home / "config.toml"
            auth_path = codex_home / "auth.json"
            self.assertEqual(config_text, config_path.read_text(encoding="utf-8"))
            self.assertEqual(auth_text, auth_path.read_text(encoding="utf-8"))
            self.assertEqual(0o600, stat.S_IMODE(config_path.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(auth_path.stat().st_mode))

    def test_editor_apply_rejects_semantic_conflict_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            config_path = codex_home / "config.toml"
            config_path.write_text('personality = "disk"\n', encoding="utf-8")
            conflict = 'sandbox_mode = "workspace-write"\ndefault_permissions = ":workspace"\n'

            result = self.run_command(
                runtime_config,
                codex_home,
                "apply-editor",
                input_text=self.editor_request(conflict, "{}\n"),
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual('personality = "disk"\n', config_path.read_text(encoding="utf-8"))
            self.assertNotIn("workspace-write", result.stderr)

    def test_editor_sync_rejects_provider_semantics_without_secret_echo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            marker = "sk-private-provider-token"
            config_text = textwrap.dedent(
                f"""
                [model_providers.openai]
                wire_api = "chat"

                [model_providers.custom]
                wire_api = "responses"
                env_key = "CUSTOM_KEY"
                experimental_bearer_token = "{marker}"
                """
            ).lstrip()

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(config_text, "{}\n"),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("reserved built-in provider id", payload["validation_error"])
            self.assertIn("wire_api must be responses", payload["validation_error"])
            self.assertIn("cannot define both env_key and a bearer token", payload["validation_error"])
            self.assertNotIn(marker, payload["validation_error"])

    def test_editor_provider_list_round_trip_preserves_raw_bearer_token(self) -> None:
        """A visible provider edit must not erase a bearer token kept raw-only."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            marker = "synthetic-bearer-token-keep"
            config_text = textwrap.dedent(
                f"""
                [model_providers.bearer]
                name = "Old label"
                base_url = "https://upstream.example.test/v1"
                wire_api = "responses"
                experimental_bearer_token = "{marker}"
                """
            ).lstrip()
            request = {
                "providers": [
                    {
                        "id": "bearer",
                        "name": "Updated label",
                        "base_url": "https://upstream.example.test/v1",
                        "wire_api": "responses",
                        "env_key": "",
                        "requires_openai_auth": False,
                        "auth_mode": "bearer",
                        "auth_command": None,
                    }
                ]
            }

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(config_text, "{}\n", request),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsNone(payload["validation_error"])
            parsed = tomllib.loads(payload["config_text"])
            self.assertEqual("Updated label", parsed["model_providers"]["bearer"]["name"])
            self.assertEqual(marker, parsed["model_providers"]["bearer"]["experimental_bearer_token"])
            provider = payload["structured"]["providers"][0]
            self.assertEqual("bearer", provider["auth_mode"])
            self.assertIsNone(provider["auth_command"])

    def test_editor_minimal_control_patch_preserves_untouched_sections(self) -> None:
        """One structured field must not materialize UI defaults elsewhere."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            config_text = textwrap.dedent(
                """
                unknown_setting = "keep"

                [features]
                remote_future_feature = true

                [model_providers.raw-only]
                experimental_bearer_token = "raw-only-token"
                wire_api = "responses"
                """
            ).lstrip()
            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    config_text,
                    "{}\n",
                    {"model": "direct-model"},
                ),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsNone(payload["validation_error"])
            parsed = tomllib.loads(payload["config_text"])
            self.assertEqual("direct-model", parsed["model"])
            self.assertEqual("keep", parsed["unknown_setting"])
            self.assertEqual({"remote_future_feature": True}, parsed["features"])
            self.assertEqual(
                "raw-only-token",
                parsed["model_providers"]["raw-only"]["experimental_bearer_token"],
            )
            self.assertNotIn("sandbox_mode", parsed)
            self.assertNotIn("default_permissions", parsed)

    def test_editor_switches_between_legacy_and_profile_permissions(self) -> None:
        """The mutually exclusive policy keys must be replaced, not merely toggled in UI."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            legacy = textwrap.dedent(
                """
                sandbox_mode = "workspace-write"
                approval_policy = "never"

                [sandbox_workspace_write]
                network_access = true
                writable_roots = ["/tmp"]
                """
            ).lstrip()

            to_profile = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    legacy,
                    "{}\n",
                    {
                        "permissions": {
                            "mode": "profile",
                            "default_permissions": ":workspace",
                            "approval_policy": "never",
                        }
                    },
                ),
            )
            self.assertEqual(0, to_profile.returncode, to_profile.stderr)
            profile_text = json.loads(to_profile.stdout)["config_text"]
            profile = tomllib.loads(profile_text)
            self.assertEqual(":workspace", profile["default_permissions"])
            self.assertEqual("never", profile["approval_policy"])
            self.assertNotIn("sandbox_mode", profile)
            self.assertNotIn("sandbox_workspace_write", profile)

            to_legacy = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    profile_text,
                    "{}\n",
                    {
                        "permissions": {
                            "mode": "legacy",
                            "sandbox_mode": "workspace-write",
                            "approval_policy": "never",
                            "network_access": True,
                            "writable_roots": ["/tmp"],
                        }
                    },
                ),
            )
            self.assertEqual(0, to_legacy.returncode, to_legacy.stderr)
            legacy_again = tomllib.loads(json.loads(to_legacy.stdout)["config_text"])
            self.assertEqual("workspace-write", legacy_again["sandbox_mode"])
            self.assertEqual("never", legacy_again["approval_policy"])
            self.assertTrue(legacy_again["sandbox_workspace_write"]["network_access"])
            self.assertEqual(["/tmp"], legacy_again["sandbox_workspace_write"]["writable_roots"])
            self.assertNotIn("default_permissions", legacy_again)

    def test_permission_mode_selector_is_complete_without_hidden_companion_fields(self) -> None:
        """The segmented mode control sends only ``mode`` and must still write a usable config."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            legacy = 'sandbox_mode = "workspace-write"\n'

            to_profile = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    legacy,
                    "{}\n",
                    {"permissions": {"mode": "profile"}},
                ),
            )
            self.assertEqual(0, to_profile.returncode, to_profile.stderr)
            profile = tomllib.loads(json.loads(to_profile.stdout)["config_text"])
            self.assertEqual(":workspace", profile["default_permissions"])
            self.assertNotIn("sandbox_mode", profile)

            to_legacy = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    json.loads(to_profile.stdout)["config_text"],
                    "{}\n",
                    {"permissions": {"mode": "legacy"}},
                ),
            )
            self.assertEqual(0, to_legacy.returncode, to_legacy.stderr)
            legacy_again = tomllib.loads(json.loads(to_legacy.stdout)["config_text"])
            self.assertEqual("workspace-write", legacy_again["sandbox_mode"])
            self.assertNotIn("default_permissions", legacy_again)

    def test_editor_direct_connection_writes_the_selected_provider_endpoint(self) -> None:
        """A custom provider must not silently receive an unused openai_base_url."""

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            config_text = textwrap.dedent(
                """
                model = "direct-model"
                model_provider = "relay"
                openai_base_url = "https://openai.example.test/v1"

                [model_providers.relay]
                base_url = "https://old-relay.example.test/v1"
                wire_api = "responses"
                """
            ).lstrip()

            custom = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    config_text,
                    "{}\n",
                    {
                        "direct_connection": {
                            "provider": "relay",
                            "base_url": "https://new-relay.example.test/v1",
                        }
                    },
                ),
            )
            self.assertEqual(0, custom.returncode, custom.stderr)
            custom_config = tomllib.loads(json.loads(custom.stdout)["config_text"])
            self.assertEqual(
                "https://new-relay.example.test/v1",
                custom_config["model_providers"]["relay"]["base_url"],
            )
            self.assertEqual("https://openai.example.test/v1", custom_config["openai_base_url"])

            built_in = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    json.loads(custom.stdout)["config_text"],
                    "{}\n",
                    {
                        "direct_connection": {
                            "provider": "openai",
                            "base_url": "https://new-openai.example.test/v1",
                        }
                    },
                ),
            )
            self.assertEqual(0, built_in.returncode, built_in.stderr)
            built_in_config = tomllib.loads(json.loads(built_in.stdout)["config_text"])
            self.assertEqual("https://new-openai.example.test/v1", built_in_config["openai_base_url"])
            self.assertEqual(
                "https://new-relay.example.test/v1",
                built_in_config["model_providers"]["relay"]["base_url"],
            )

    def test_editor_switches_and_renames_a_custom_provider_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            self.write_runtime_config(runtime_config)
            config_text = textwrap.dedent(
                """
                model = "direct-model"
                model_provider = "relay"

                [model_providers.relay]
                name = "Relay"
                base_url = "https://old-relay.example.test/v1"
                wire_api = "responses"
                """
            ).lstrip()

            result = self.run_command(
                runtime_config,
                codex_home,
                "sync",
                input_text=self.editor_request(
                    config_text,
                    "{}\n",
                    {
                        "providers": [
                            {
                                "id": "renamed-relay",
                                "name": "Relay",
                                "base_url": "https://new-relay.example.test/v1",
                                "wire_api": "responses",
                                "auth_mode": "none",
                            }
                        ],
                        "direct_connection": {
                            "provider": "renamed-relay",
                            "base_url": "https://new-relay.example.test/v1",
                        },
                        "model_provider": "renamed-relay",
                    },
                ),
            )

            self.assertEqual(0, result.returncode, result.stderr)
            parsed = tomllib.loads(json.loads(result.stdout)["config_text"])
            self.assertEqual("renamed-relay", parsed["model_provider"])
            self.assertNotIn("relay", parsed["model_providers"])
            self.assertEqual(
                "https://new-relay.example.test/v1",
                parsed["model_providers"]["renamed-relay"]["base_url"],
            )

    def test_editor_apply_refuses_symbolic_link_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            target = temp / "outside.toml"
            target.write_text('personality = "outside"\n', encoding="utf-8")
            (codex_home / "config.toml").symlink_to(target)

            result = self.run_command(
                runtime_config,
                codex_home,
                "apply-editor",
                input_text=self.editor_request('model = "safe"\n', "{}\n"),
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual('personality = "outside"\n', target.read_text(encoding="utf-8"))
            self.assertNotIn("outside", result.stderr)

    def test_editor_apply_rolls_back_auth_when_config_commit_fails(self) -> None:
        import codex_config

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            codex_home = temp / "codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            auth_path = codex_home / "auth.json"
            original_config = 'model = "before"\n'
            original_auth = '{"OPENAI_API_KEY": "before-key"}\n'
            config_path.write_text(original_config, encoding="utf-8")
            auth_path.write_text(original_auth, encoding="utf-8")
            real_atomic_write = codex_config.atomic_write

            def fail_second_write(path: Path, data: str, mode: int = 0o600) -> None:
                if path == config_path and 'model = "after"' in data:
                    raise OSError("synthetic failure")
                real_atomic_write(path, data, mode)

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), \
                    mock.patch.object(codex_config, "atomic_write", side_effect=fail_second_write):
                with self.assertRaises(OSError):
                    codex_config.atomic_write_editor_files(
                        'model = "after"\n',
                        '{"OPENAI_API_KEY": "after-key"}\n',
                    )

            self.assertEqual(original_config, config_path.read_text(encoding="utf-8"))
            self.assertEqual(original_auth, auth_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

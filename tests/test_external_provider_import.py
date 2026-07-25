from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
IMPORTER = ROOT / "external_provider_import.py"


class ExternalProviderImportTests(unittest.TestCase):
    def temporary_directory(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def run_importer(
        self,
        *arguments: str,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        return subprocess.run(
            [sys.executable, str(IMPORTER), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=process_env,
            input=input_text,
        )

    def test_codex_current_imports_built_in_openai_with_only_auth_json_key(self) -> None:
        codex_home = self.temporary_directory()
        (codex_home / "config.toml").write_text(
            textwrap.dedent(
                """
                model_provider = "openai"
                model = "current-model"
                openai_base_url = "https://proxy.example.test/v1"

                [model_providers.unselected]
                base_url = "https://unselected.example.test/v1"
                env_key = "DO_NOT_RESOLVE"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        (codex_home / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-codex-import"}), encoding="utf-8"
        )

        result = self.run_importer("codex-current", env={"CODEX_HOME": str(codex_home)})

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("codex-current", payload["source"])
        self.assertEqual(1, payload["summary"]["providers"])
        provider = payload["providers"][0]
        self.assertEqual("openai", provider["name"])
        self.assertEqual("https://proxy.example.test/v1", provider["api_base"])
        self.assertEqual([{"name": "default", "value": "sk-codex-import"}], provider["api_keys"])
        self.assertEqual("current-model", provider["models"][0]["model_name"])
        self.assertEqual("openai/current-model", provider["models"][0]["litellm_model"])

    def test_codex_current_never_resolves_custom_environment_key(self) -> None:
        codex_home = self.temporary_directory()
        (codex_home / "config.toml").write_text(
            textwrap.dedent(
                """
                model_provider = "custom"
                model = "custom-model"

                [model_providers.custom]
                base_url = "https://custom.example.test/v1"
                env_key = "UNSAFE_EXTERNAL_IMPORT_TOKEN"
                """
            ).lstrip(),
            encoding="utf-8",
        )

        result = self.run_importer(
            "codex-current",
            env={
                "CODEX_HOME": str(codex_home),
                "UNSAFE_EXTERNAL_IMPORT_TOKEN": "sk-must-not-be-read",
            },
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([], payload["providers"])
        self.assertGreaterEqual(payload["summary"]["skipped"], 1)
        self.assertNotIn("sk-must-not-be-read", result.stdout)
        self.assertNotIn("sk-must-not-be-read", result.stderr)

    def test_imports_litellm_style_yaml_as_editable_provider(self) -> None:
        directory = self.temporary_directory()
        source = directory / "config.yaml"
        source.write_text(
            textwrap.dedent(
                """
                providers:
                  primary:
                    api_base: &base "https://primary.example.test/v1"
                    api_keys:
                      - name: main
                        value: &key "sk-primary-import"
                model_list:
                  - model_name: public-chat
                    litellm_params:
                      model: openai/upstream-chat
                      api_base: *base
                      api_key: *key
                      order: 3
                    model_info:
                      provider: primary
                      api_key_name: main
                      upstream_url_surface: openai/chat
                      supported_upstream_url_surfaces: [openai/chat, openai/responses]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        before = source.read_bytes()

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, source.read_bytes())
        payload = json.loads(result.stdout)
        provider = payload["providers"][0]
        model = provider["models"][0]
        self.assertEqual("primary", provider["name"])
        self.assertEqual("main", model["api_key_name"])
        self.assertEqual("3", model["order"])
        self.assertEqual("openai/chat", model["upstream_url_surface"])
        self.assertEqual(["openai/chat", "openai/responses"], model["supported_upstream_url_surfaces"])

    def test_rejects_yaml_alias_bomb_without_echoing_source_values(self) -> None:
        directory = self.temporary_directory()
        source = directory / "alias-bomb.yaml"
        marker = "sk-external-alias-bomb-leak-marker"
        aliases = ", ".join(["*previous"] * 10)
        layers = ["seed: &previous [safe]"]
        for index in range(6):
            anchor = f"layer_{index}"
            layers.append(f"{anchor}: &{anchor} [{aliases}]")
            aliases = ", ".join([f"*{anchor}"] * 10)
        layers.append(f'secret: "{marker}"')
        source.write_text("\n".join(layers), encoding="utf-8")

        result = self.run_importer("--input", str(source))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unsupported format", result.stderr)
        self.assertNotIn(marker, result.stdout)
        self.assertNotIn(marker, result.stderr)

    def test_imports_external_yaml_with_normal_anchors(self) -> None:
        directory = self.temporary_directory()
        source = directory / "anchored.yaml"
        source.write_text(
            textwrap.dedent(
                """
                shared: &shared
                  base_url: https://anchored.example.test/v1
                  api_key: sk-anchored-import
                  models: [anchored-model]
                providers:
                  anchored:
                    <<: *shared
                """
            ).lstrip(),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        provider = json.loads(result.stdout)["providers"][0]
        self.assertEqual("anchored", provider["name"])
        self.assertEqual("https://anchored.example.test/v1", provider["api_base"])
        self.assertEqual("openai/anchored-model", provider["models"][0]["litellm_model"])

    def test_imports_cliproxyapi_documented_provider_containers(self) -> None:
        directory = self.temporary_directory()
        source = directory / "cliproxy.yaml"
        source.write_text(
            textwrap.dedent(
                """
                force-model-prefix: true
                api-keys: [client-facing-key-must-not-import]
                openai-compatibility:
                  - name: relay-chat
                    disabled: false
                    prefix: team
                    base-url: https://chat.example.test/v1
                    api-key-entries:
                      - api-key: sk-chat-one
                      - api-key: sk-chat-two
                    models:
                      - name: vendor/model-a
                        alias: public-a
                codex-api-key:
                  - api-key: sk-responses
                    prefix: direct
                    base-url: https://responses.example.test/v1
                    models:
                      - name: gpt-example
                        alias: public-responses
                """
            ).lstrip(),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(2, payload["summary"]["providers"])
        chat, responses = payload["providers"]
        self.assertEqual(["sk-chat-one", "sk-chat-two"], [item["value"] for item in chat["api_keys"]])
        self.assertNotIn("client-facing-key-must-not-import", result.stdout)
        self.assertEqual("team/public-a", chat["models"][0]["model_name"])
        self.assertEqual("openai/vendor/model-a", chat["models"][0]["litellm_model"])
        self.assertEqual("openai/chat", chat["models"][0]["upstream_url_surface"])
        self.assertEqual("direct/public-responses", responses["models"][0]["model_name"])
        self.assertEqual("openai/responses", responses["models"][0]["upstream_url_surface"])

    def test_imports_cc_switch_sql_as_text_without_executing_statements(self) -> None:
        directory = self.temporary_directory()
        source = directory / "cc-switch-export.sql"
        settings = json.dumps(
            {
                "auth": {"OPENAI_API_KEY": "sk-sql-codex"},
                "config": textwrap.dedent(
                    """
                    model_provider = "custom"
                    model = "sql-model"
                    [model_providers.custom]
                    base_url = "https://sql.example.test/v1"
                    wire_api = "responses"
                    """
                ).lstrip(),
            },
            separators=(",", ":"),
        ).replace("'", "''")
        source.write_text(
            "-- CC Switch SQLite 导出\n"
            "ATTACH DATABASE '/tmp/must-not-exist.db' AS hostile;\n"
            'INSERT INTO "providers" ("id", "app_type", "name", "settings_config", "meta") '
            f"VALUES ('p1', 'codex', 'SQL Codex', '{settings}', '{{}}');\n"
            "DROP TABLE providers;\n",
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        provider = json.loads(result.stdout)["providers"][0]
        self.assertEqual("SQL Codex", provider["name"])
        self.assertEqual("sql-model", provider["models"][0]["model_name"])
        self.assertFalse(Path("/tmp/must-not-exist.db").exists())

    def test_rejects_non_cc_switch_sql_without_echoing_secret(self) -> None:
        directory = self.temporary_directory()
        source = directory / "unsafe.sql"
        source.write_text("SELECT 'sk-sql-secret';", encoding="utf-8")

        result = self.run_importer("--input", str(source))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("not a CC Switch export", result.stderr)
        self.assertNotIn("sk-sql-secret", result.stderr)

    def test_imports_new_api_ccswitch_link_from_stdin(self) -> None:
        link = "ccswitch://v1/import?" + urlencode(
            {
                "resource": "provider",
                "app": "codex",
                "name": "New API Relay",
                "endpoint": "https://newapi.example.test/v1",
                "apiKey": "sk-newapi-link",
                "model": "newapi-model",
                "enabled": "true",
            }
        )

        result = self.run_importer("--link-stdin", input_text=link)

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("import-link", payload["source"])
        provider = payload["providers"][0]
        self.assertEqual("New API Relay", provider["name"])
        self.assertEqual("sk-newapi-link", provider["api_key"])
        self.assertEqual("openai/responses", provider["models"][0]["upstream_url_surface"])

    def test_invalid_import_link_does_not_echo_its_secret(self) -> None:
        link = "ccswitch://v1/import?" + urlencode(
            {
                "resource": "provider",
                "app": "unsupported",
                "endpoint": "https://invalid.example.test/v1",
                "apiKey": "sk-link-secret",
                "model": "invalid-model",
            }
        )

        result = self.run_importer("--link-stdin", input_text=link)

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("sk-link-secret", result.stderr)

    def test_codex_current_maps_auth_json_to_selected_custom_provider_only_when_required(self) -> None:
        codex_home = self.temporary_directory()
        (codex_home / "config.toml").write_text(
            textwrap.dedent(
                """
                model_provider = "required"
                model = "required-model"

                [model_providers.required]
                base_url = "https://required.example.test/v1"
                wire_api = "responses"
                requires_openai_auth = true

                [model_providers.unselected]
                base_url = "https://unselected.example.test/v1"
                wire_api = "responses"
                requires_openai_auth = false
                models = ["unselected-model"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        (codex_home / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-selected-auth"}), encoding="utf-8"
        )

        result = self.run_importer("codex-current", env={"CODEX_HOME": str(codex_home)})

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(1, payload["summary"]["providers"])
        self.assertEqual("required", payload["providers"][0]["name"])
        self.assertEqual("sk-selected-auth", payload["providers"][0]["api_key"])

    def test_imports_structurally_recognizable_generic_data_entries(self) -> None:
        directory = self.temporary_directory()
        source = directory / "providers.json"
        source.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "name": "local-provider",
                            "base_url": "https://local.example.test/v1",
                            "key": "sk-generic-import",
                            "models": [
                                "generic-a",
                                {"id": "generic-b", "name": "public-b", "wire_api": "responses"},
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(1, payload["summary"]["providers"])
        self.assertEqual(2, payload["summary"]["models"])
        provider = payload["providers"][0]
        self.assertEqual("local-provider", provider["name"])
        self.assertEqual("sk-generic-import", provider["api_key"])
        self.assertEqual(
            ["openai/generic-a", "openai/generic-b"],
            [model["litellm_model"] for model in provider["models"]],
        )

    def test_imports_generic_model_list_without_mistaking_it_for_litellm_yaml(self) -> None:
        directory = self.temporary_directory()
        source = directory / "generic.yaml"
        source.write_text(
            textwrap.dedent(
                """
                providerName: generic-provider
                baseUrl: https://generic.example.test/v1
                apiKey: sk-generic-yaml
                model_list:
                  - generic-model
                """
            ).lstrip(),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("generic-provider", payload["providers"][0]["name"])
        self.assertEqual("openai/generic-model", payload["providers"][0]["models"][0]["litellm_model"])

    def test_imports_custom_codex_toml_only_when_direct_key_is_present(self) -> None:
        codex_home = self.temporary_directory()
        (codex_home / "config.toml").write_text(
            textwrap.dedent(
                """
                model_provider = "example"
                model = "example-model"

                [model_providers.example]
                base_url = "https://example-provider.example.test/v1"
                api_key = "sk-direct-custom-key"
                models = ["example-model", "other-model"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        result = self.run_importer("codex-current", env={"CODEX_HOME": str(codex_home)})

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        provider = payload["providers"][0]
        self.assertEqual("example", provider["name"])
        self.assertEqual("sk-direct-custom-key", provider["api_key"])
        self.assertEqual({"example-model", "other-model"}, {item["model_name"] for item in provider["models"]})

    def test_imports_modern_toml_constructs_with_standard_library_parser(self) -> None:
        directory = self.temporary_directory()
        source = directory / "modern.toml"
        source.write_text(
            textwrap.dedent(
                '''
                model_provider = "example"
                model = "example-model"

                [model_providers.example]
                base_url = "https://example-provider.example.test/v1"
                api_key = "sk-modern-toml"
                models = ["example-model"]
                metadata = { retry = { max_attempts = 2 } }
                '''
            ).lstrip(),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        provider = json.loads(result.stdout)["providers"][0]
        self.assertEqual("example", provider["name"])
        self.assertEqual("example-model", provider["models"][0]["model_name"])

    def test_explicit_codex_toml_does_not_read_adjacent_auth_json(self) -> None:
        directory = self.temporary_directory()
        source = directory / "config.toml"
        source.write_text(
            textwrap.dedent(
                """
                model_provider = "openai"
                model = "proxy-model"
                openai_base_url = "https://proxy.example.test/v1"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        (directory / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-adjacent-auth-must-not-be-read"}),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([], payload["providers"])
        self.assertNotIn("sk-adjacent-auth-must-not-be-read", result.stdout)

    def test_imports_nested_generic_provider_models(self) -> None:
        directory = self.temporary_directory()
        source = directory / "nested.json"
        source.write_text(
            json.dumps(
                {
                    "providers": {
                        "nested": {
                            "base_url": "https://nested.example.test/v1",
                            "api_key": "sk-nested-provider",
                            "models": ["nested-model"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        provider = json.loads(result.stdout)["providers"][0]
        self.assertEqual("nested", provider["name"])
        self.assertEqual("openai/nested-model", provider["models"][0]["litellm_model"])

    def test_imports_common_camel_case_and_comma_separated_model_fields(self) -> None:
        directory = self.temporary_directory()
        source = directory / "camel.json"
        source.write_text(
            json.dumps(
                {
                    "providerName": "camel-provider",
                    "baseURL": "https://camel.example.test/v1",
                    "apiKeys": [{"name": "main", "apiKey": "sk-camel-import"}],
                    "availableModels": "camel-a, camel-b",
                }
            ),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        provider = json.loads(result.stdout)["providers"][0]
        self.assertEqual("camel-provider", provider["name"])
        self.assertEqual("main", provider["api_keys"][0]["name"])
        self.assertEqual({"camel-a", "camel-b"}, {item["model_name"] for item in provider["models"]})

    def test_codex_current_rejects_symbolic_linked_configuration(self) -> None:
        directory = self.temporary_directory()
        target = directory / "target.toml"
        target.write_text(
            'model_provider = "openai"\nopenai_base_url = "https://proxy.example.test/v1"\n',
            encoding="utf-8",
        )
        (directory / "config.toml").symlink_to(target)

        result = self.run_importer("codex-current", env={"CODEX_HOME": str(directory)})

        self.assertNotEqual(0, result.returncode)
        self.assertIn("symbolic link", result.stderr)

    def test_rejects_invalid_input_without_echoing_its_secret(self) -> None:
        directory = self.temporary_directory()
        source = directory / "invalid.json"
        secret = "sk-secret-that-must-not-appear"
        source.write_text('{"api_key": "' + secret + '"', encoding="utf-8")
        before = source.read_bytes()

        result = self.run_importer("--input", str(source))

        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, source.read_bytes())
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)

    def test_rejects_unsupported_extension_before_reading(self) -> None:
        directory = self.temporary_directory()
        source = directory / "not-a-config.txt"
        source.write_text("not a configuration", encoding="utf-8")

        result = self.run_importer("--input", str(source))

        self.assertNotEqual(0, result.returncode)
        self.assertIn(".toml, .yaml, .yml, or .json", result.stderr)

    def test_rejects_provider_or_model_counts_beyond_editor_limits(self) -> None:
        directory = self.temporary_directory()
        source = directory / "too-many-models.json"
        source.write_text(
            json.dumps(
                {
                    "name": "bounded-provider",
                    "base_url": "https://bounded.example.test/v1",
                    "api_key": "sk-bounded",
                    "models": [f"model-{index}" for index in range(5_001)],
                }
            ),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("too many providers or models", result.stderr)
        self.assertNotIn("sk-bounded", result.stderr)

    def test_skips_url_with_embedded_credentials(self) -> None:
        directory = self.temporary_directory()
        source = directory / "unsafe-url.json"
        source.write_text(
            json.dumps(
                {
                    "name": "unsafe",
                    "base_url": "https://embedded-user:embedded-password@example.test/v1",
                    "api_key": "sk-separate-key",
                    "models": ["unsafe-model"],
                }
            ),
            encoding="utf-8",
        )

        result = self.run_importer("--input", str(source))

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([], payload["providers"])
        self.assertNotIn("embedded-password", result.stdout)


if __name__ == "__main__":
    unittest.main()

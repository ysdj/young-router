from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEX_CONFIG = ROOT / "codex_config.py"
STATE_FILE = ".litellm-menu-codex-local-config-state.json"


class CodexConfigTests(unittest.TestCase):
    def run_command(
        self,
        codex_home: Path,
        runtime_config: Path,
        command: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(codex_home),
                "LITELLM_CONFIG_FILE": str(runtime_config),
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(CODEX_CONFIG), command],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def write_runtime_config(self, path: Path, api_key: str = "sk-test-runtime") -> None:
        path.write_text(
            textwrap.dedent(
                f"""
                general_settings:
                  master_key: {api_key}
                model_list: []
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def test_local_manages_only_provider_connection_and_auth_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            original_model_line = 'model = "user-selected-model"'
            (codex_home / "config.toml").write_text(
                textwrap.dedent(
                    f"""
                    {original_model_line}
                    model_provider = "remote"
                    model_context_window = 1048576
                    compact_prompt = "keep me"

                    [model_providers.newapi]
                    name = "User display name"
                    base_url = "https://remote.example.test/v1"
                    wire_api = "responses"
                    requires_openai_auth = false
                    request_max_retries = 7

                    [mcp_servers.example]
                    command = "example"
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            (codex_home / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "sk-test-old", "tokens": {"keep": True}}) + "\n",
                encoding="utf-8",
            )

            result = self.run_command(codex_home, runtime_config, "local")

            self.assertEqual(result.returncode, 0, result.stderr)
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertIn(original_model_line, config)
            self.assertIn('model_provider = "newapi"', config)
            self.assertIn('base_url = "http://127.0.0.1:4000/v1"', config)
            self.assertIn('wire_api = "responses"', config)
            self.assertIn("requires_openai_auth = true", config)
            self.assertIn('name = "OpenAI"', config)
            self.assertIn("request_max_retries = 7", config)
            self.assertIn("[mcp_servers.example]", config)
            self.assertEqual(auth["OPENAI_API_KEY"], "sk-test-runtime")
            self.assertEqual(auth["tokens"], {"keep": True})
            self.assertNotIn("model:", result.stdout)
            self.assertEqual(list(codex_home.glob("*.bak*")), [])

            state = json.loads((codex_home / STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], 3)
            self.assertEqual(state["config"]["top_level"]["model_provider"]["value"], "remote")
            self.assertEqual(
                state["config"]["providers"]["newapi"]["fields"]["base_url"]["value"],
                "https://remote.example.test/v1",
            )

    def test_local_uses_custom_port_without_touching_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            (codex_home / "config.toml").write_text('model = "keep-this-model"\n', encoding="utf-8")

            result = self.run_command(
                codex_home,
                runtime_config,
                "local",
                extra_env={"LITELLM_PORT": "49240", "CODEX_MODEL": "must-be-ignored"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "keep-this-model"', config)
            self.assertNotIn("must-be-ignored", config)
            self.assertIn('base_url = "http://127.0.0.1:49240/v1"', config)

    def test_repeated_local_preserves_first_field_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                textwrap.dedent(
                    """
                    model = "initial-model"
                    model_provider = "remote"

                    [model_providers.newapi]
                    base_url = "https://original.example.test/v1"
                    wire_api = "responses"
                    requires_openai_auth = false
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            (codex_home / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "sk-test-original"}) + "\n",
                encoding="utf-8",
            )

            self.write_runtime_config(runtime_config, "sk-test-first")
            first = self.run_command(codex_home, runtime_config, "local")
            self.assertEqual(first.returncode, 0, first.stderr)
            first_state = (codex_home / STATE_FILE).read_text(encoding="utf-8")

            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            config = config.replace('model = "initial-model"', 'model = "changed-while-local"')
            config += 'personality = "pragmatic"\n'
            (codex_home / "config.toml").write_text(config, encoding="utf-8")
            self.write_runtime_config(runtime_config, "sk-test-second")
            second = self.run_command(codex_home, runtime_config, "local")

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual((codex_home / STATE_FILE).read_text(encoding="utf-8"), first_state)
            auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(auth["OPENAI_API_KEY"], "sk-test-second")

            restored = self.run_command(codex_home, runtime_config, "reapply-pre-switch")
            self.assertEqual(restored.returncode, 0, restored.stderr)
            restored_config = (codex_home / "config.toml").read_text(encoding="utf-8")
            restored_auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertIn('model = "changed-while-local"', restored_config)
            self.assertIn('personality = "pragmatic"', restored_config)
            self.assertIn('model_provider = "remote"', restored_config)
            self.assertIn('base_url = "https://original.example.test/v1"', restored_config)
            self.assertIn("requires_openai_auth = false", restored_config)
            self.assertEqual(restored_auth["OPENAI_API_KEY"], "sk-test-original")
            self.assertFalse((codex_home / STATE_FILE).exists())

    def test_restore_removes_only_fields_created_by_local_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            self.write_runtime_config(runtime_config)

            applied = self.run_command(codex_home, runtime_config, "local")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            config_path = codex_home / "config.toml"
            config = config_path.read_text(encoding="utf-8").replace(
                "requires_openai_auth = true",
                'requires_openai_auth = true\ncustom_header = "keep"',
            )
            config_path.write_text(config, encoding="utf-8")
            auth_path = codex_home / "auth.json"
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
            auth["unrelated"] = "keep"
            auth_path.write_text(json.dumps(auth) + "\n", encoding="utf-8")

            restored = self.run_command(codex_home, runtime_config, "reapply-pre-switch")

            self.assertEqual(restored.returncode, 0, restored.stderr)
            restored_config = config_path.read_text(encoding="utf-8")
            restored_auth = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertNotIn("model_provider =", restored_config)
            self.assertNotIn("base_url =", restored_config)
            self.assertNotIn("wire_api =", restored_config)
            self.assertNotIn("requires_openai_auth =", restored_config)
            self.assertIn('custom_header = "keep"', restored_config)
            self.assertNotIn("OPENAI_API_KEY", restored_auth)
            self.assertEqual(restored_auth["unrelated"], "keep")

    def test_restore_deletes_new_empty_config_and_auth_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            self.write_runtime_config(runtime_config)

            applied = self.run_command(codex_home, runtime_config, "local")
            restored = self.run_command(codex_home, runtime_config, "reapply-pre-switch")

            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertFalse((codex_home / "auth.json").exists())

    def test_restore_requires_current_field_state_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            self.write_runtime_config(runtime_config)
            (codex_home / STATE_FILE).write_text(
                json.dumps({"schema_version": 1, "active": True}) + "\n",
                encoding="utf-8",
            )

            result = self.run_command(codex_home, runtime_config, "reapply-pre-switch")

            self.assertEqual(result.returncode, 1)
            self.assertIn("No active pre-switch Codex config state found.", result.stderr)


if __name__ == "__main__":
    unittest.main()

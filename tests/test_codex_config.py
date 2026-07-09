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

    def run_local(self, codex_home: Path, runtime_config: Path) -> subprocess.CompletedProcess[str]:
        return self.run_command(codex_home, runtime_config, "local")

    def run_reapply_pre_switch(self, codex_home: Path, runtime_config: Path) -> subprocess.CompletedProcess[str]:
        return self.run_command(codex_home, runtime_config, "reapply-pre-switch")

    def test_local_command_uses_litellm_config_file_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            runtime_config.write_text(
                textwrap.dedent(
                    """
                    general_settings:
                      master_key: sk-test-runtime
                    model_list: []
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            result = self.run_local(codex_home, runtime_config)

            self.assertEqual(result.returncode, 0, result.stderr)
            auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertEqual(auth["OPENAI_API_KEY"], "sk-test-runtime")
            self.assertIn('base_url = "http://127.0.0.1:4000/v1"', config)

    def test_local_command_uses_custom_litellm_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            runtime_config.write_text(
                textwrap.dedent(
                    """
                    general_settings:
                      master_key: sk-test-runtime
                    model_list: []
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            result = self.run_command(
                codex_home,
                runtime_config,
                "local",
                extra_env={"LITELLM_PORT": "49240"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            state = json.loads((codex_home / ".litellm-menu-codex-local-config-state.json").read_text(encoding="utf-8"))
            self.assertIn('base_url = "http://127.0.0.1:49240/v1"', config)
            self.assertEqual("http://127.0.0.1:49240/v1", state["target_base_url"])

    def test_local_command_does_not_manage_local_compaction_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            runtime_config.write_text(
                textwrap.dedent(
                    """
                    general_settings:
                      master_key: sk-test-runtime
                    model_list: []
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            (codex_home / "config.toml").write_text(
                textwrap.dedent(
                    """
                    model_provider = "newapi"
                    model = "default-chat"
                    model_context_window = 1048576
                    model_auto_compact_token_limit = 943718
                    compact_prompt = "stale override"
                    review_model = "default-chat"

                    [model_providers.newapi]
                    name = "newapi"
                    base_url = "http://127.0.0.1:4000/v1"
                    wire_api = "responses"
                    requires_openai_auth = true
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            (codex_home / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "sk-old"}) + "\n",
                encoding="utf-8",
            )

            result = self.run_local(codex_home, runtime_config)

            self.assertEqual(result.returncode, 0, result.stderr)
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("model_context_window = 1048576", config)
            self.assertIn("model_auto_compact_token_limit = 943718", config)
            self.assertIn('compact_prompt = "stale override"', config)
            self.assertIn('review_model = "default-chat"', config)
            self.assertNotIn("model_context_window:", result.stdout)
            self.assertNotIn("model_auto_compact_token_limit:", result.stdout)
            self.assertNotIn("compact_prompt", result.stdout)

    def test_local_command_keeps_single_backup_per_codex_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('model = "old-model"\n', encoding="utf-8")
            (codex_home / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "sk-old"}) + "\n",
                encoding="utf-8",
            )
            for file_name in ("config.toml", "auth.json"):
                for stamp in ("20260611-150435", "20260611-175119"):
                    (codex_home / f"{file_name}.bak-{stamp}").write_text(
                        "legacy backup\n",
                        encoding="utf-8",
                    )
                    (codex_home / f"{file_name}.pre-restore-bak-{stamp}").write_text(
                        "legacy pre-restore backup\n",
                        encoding="utf-8",
                    )

            for api_key in ("sk-first", "sk-second"):
                runtime_config.write_text(
                    textwrap.dedent(
                        f"""
                        general_settings:
                          master_key: {api_key}
                        model_list: []
                        """
                    ).lstrip(),
                    encoding="utf-8",
                )
                result = self.run_local(codex_home, runtime_config)
                self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(
                sorted(path.name for path in codex_home.glob("config.toml.bak*")),
                ["config.toml.bak"],
            )
            self.assertEqual(
                sorted(path.name for path in codex_home.glob("auth.json.bak*")),
                ["auth.json.bak"],
            )
            backup_auth = json.loads((codex_home / "auth.json.bak").read_text(encoding="utf-8"))
            self.assertEqual(backup_auth["OPENAI_API_KEY"], "sk-old")

    def test_local_command_replaces_stale_backup_when_already_on_litellm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            runtime_config.write_text(
                textwrap.dedent(
                    """
                    general_settings:
                      master_key: sk-updated-local
                    model_list: []
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            current_config = textwrap.dedent(
                """
                model_provider = "newapi"
                model = "default-chat"

                [model_providers.newapi]
                name = "newapi"
                base_url = "http://127.0.0.1:4000/v1"
                wire_api = "responses"
                requires_openai_auth = true
                """
            ).lstrip()
            previous_config = 'model_provider = "openai"\nmodel = "gpt-4.1"\n'
            previous_auth = json.dumps({"OPENAI_API_KEY": "sk-old"}) + "\n"
            (codex_home / "config.toml").write_text(current_config, encoding="utf-8")
            (codex_home / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "sk-local-litellm"}) + "\n",
                encoding="utf-8",
            )
            (codex_home / "config.toml.bak").write_text(previous_config, encoding="utf-8")
            (codex_home / "auth.json.bak").write_text(previous_auth, encoding="utf-8")

            result = self.run_local(codex_home, runtime_config)

            self.assertEqual(result.returncode, 0, result.stderr)
            config = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('base_url = "http://127.0.0.1:4000/v1"', config)
            self.assertEqual((codex_home / "config.toml.bak").read_text(encoding="utf-8"), current_config)
            self.assertEqual(
                (codex_home / "auth.json.bak").read_text(encoding="utf-8"),
                json.dumps({"OPENAI_API_KEY": "sk-local-litellm"}) + "\n",
            )
            auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(auth["OPENAI_API_KEY"], "sk-updated-local")

    def test_local_command_preserves_original_backup_on_repeated_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            original_config = 'model_provider = "openai"\nmodel = "gpt-4.1"\n'
            original_auth = json.dumps({"OPENAI_API_KEY": "sk-old"}) + "\n"
            (codex_home / "config.toml").write_text(original_config, encoding="utf-8")
            (codex_home / "auth.json").write_text(original_auth, encoding="utf-8")

            for api_key in ("sk-first-local", "sk-second-local"):
                runtime_config.write_text(
                    textwrap.dedent(
                        f"""
                        general_settings:
                          master_key: {api_key}
                        model_list: []
                        """
                    ).lstrip(),
                    encoding="utf-8",
                )
                result = self.run_local(codex_home, runtime_config)
                self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual((codex_home / "config.toml.bak").read_text(encoding="utf-8"), original_config)
            self.assertEqual((codex_home / "auth.json.bak").read_text(encoding="utf-8"), original_auth)
            auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(auth["OPENAI_API_KEY"], "sk-second-local")

    def test_reapply_pre_switch_reapplies_codex_files_without_extra_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            runtime_config.write_text("general_settings: {}\nmodel_list: []\n", encoding="utf-8")

            current_config = 'model_provider = "newapi"\nmodel = "default-chat"\n'
            current_auth = json.dumps({"OPENAI_API_KEY": "sk-local-litellm"}) + "\n"
            previous_config = 'model_provider = "openai"\nmodel = "gpt-4.1"\n'
            previous_auth = json.dumps({"OPENAI_API_KEY": "sk-old"}) + "\n"
            (codex_home / "config.toml").write_text(current_config, encoding="utf-8")
            (codex_home / "auth.json").write_text(current_auth, encoding="utf-8")
            (codex_home / "config.toml.bak").write_text(previous_config, encoding="utf-8")
            (codex_home / "auth.json.bak").write_text(previous_auth, encoding="utf-8")
            (codex_home / ".litellm-menu-codex-local-config-state.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "active": True,
                        "target_base_url": "http://127.0.0.1:4000/v1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (codex_home / "config.toml.pre-restore-bak-20260611-150435").write_text(
                "legacy snapshot\n",
                encoding="utf-8",
            )
            (codex_home / "auth.json.pre-restore-bak-20260611-150435").write_text(
                "legacy snapshot\n",
                encoding="utf-8",
            )

            result = self.run_reapply_pre_switch(codex_home, runtime_config)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((codex_home / "config.toml").read_text(encoding="utf-8"), previous_config)
            self.assertEqual((codex_home / "auth.json").read_text(encoding="utf-8"), previous_auth)
            self.assertFalse((codex_home / ".litellm-menu-codex-local-config-state.json").exists())
            self.assertIn("Codex config reapplied from saved pre-switch files.", result.stdout)
            self.assertEqual(
                sorted(path.name for path in codex_home.glob("config.toml*")),
                ["config.toml", "config.toml.bak"],
            )
            self.assertEqual(
                sorted(path.name for path in codex_home.glob("auth.json*")),
                ["auth.json", "auth.json.bak"],
            )

    def test_reapply_pre_switch_fails_when_no_active_state_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            runtime_config.write_text("general_settings: {}\nmodel_list: []\n", encoding="utf-8")
            (codex_home / "config.toml").write_text('model = "current"\n', encoding="utf-8")

            result = self.run_reapply_pre_switch(codex_home, runtime_config)

            self.assertEqual(result.returncode, 1)
            self.assertIn("No active pre-switch Codex config state found.", result.stderr)

    def test_reapply_pre_switch_ignores_backups_without_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "runtime-config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            runtime_config.write_text("general_settings: {}\nmodel_list: []\n", encoding="utf-8")
            current_config = 'model_provider = "newapi"\nmodel = "default-chat"\n'
            current_auth = json.dumps({"OPENAI_API_KEY": "sk-local-litellm"}) + "\n"
            old_config = 'model_provider = "openai"\nmodel = "gpt-4.1"\n'
            old_auth = json.dumps({"OPENAI_API_KEY": "sk-old"}) + "\n"
            (codex_home / "config.toml").write_text(current_config, encoding="utf-8")
            (codex_home / "auth.json").write_text(current_auth, encoding="utf-8")
            (codex_home / "config.toml.bak").write_text(old_config, encoding="utf-8")
            (codex_home / "auth.json.bak").write_text(old_auth, encoding="utf-8")

            result = self.run_reapply_pre_switch(codex_home, runtime_config)

            self.assertEqual(result.returncode, 1)
            self.assertIn("No active pre-switch Codex config state found.", result.stderr)
            self.assertEqual((codex_home / "config.toml").read_text(encoding="utf-8"), current_config)
            self.assertEqual((codex_home / "auth.json").read_text(encoding="utf-8"), current_auth)


if __name__ == "__main__":
    unittest.main()

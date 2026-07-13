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
CODEX_LAUNCHER = ROOT / "codex_launcher.py"
SYNTHETIC_API_KEY = "sk-test-runtime"


class CodexLauncherTests(unittest.TestCase):
    def write_runtime_config(self, path: Path, master_key: str = SYNTHETIC_API_KEY) -> None:
        path.write_text(
            textwrap.dedent(
                f"""
                general_settings:
                  master_key: {master_key}
                model_list: []
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def write_fake_codex(self, path: Path) -> None:
        path.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json
                import os
                import sys

                payload = {{
                    "argv": sys.argv[1:],
                    "codex_home": os.environ.get("CODEX_HOME"),
                    "synthetic_key_in_environment": any(
                        value == {SYNTHETIC_API_KEY!r} for value in os.environ.values()
                    ),
                }}
                with open(os.environ["FAKE_CODEX_CAPTURE"], "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                """
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)

    def run_launcher(
        self,
        runtime_config: Path,
        arguments: list[str],
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["LITELLM_CONFIG_FILE"] = str(runtime_config)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(CODEX_LAUNCHER), *arguments],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def parsed_overrides(self, argv: list[str]) -> dict[str, object]:
        overrides: dict[str, object] = {}
        for index, argument in enumerate(argv[:-1]):
            if argument != "-c":
                continue
            key, raw_value = argv[index + 1].split("=", 1)
            overrides[key] = json.loads(raw_value)
        return overrides

    def test_isolated_exec_preserves_user_config_auth_and_tools_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            auth_path = codex_home / "auth.json"
            config_path.write_text(
                textwrap.dedent(
                    """
                    model = "user-selected-model"
                    model_provider = "user-provider"

                    [mcp_servers.example]
                    command = "example"

                    [plugins.example]
                    enabled = true
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            auth_path.write_text('{"tokens":{"keep":true}}\n', encoding="utf-8")
            self.write_runtime_config(runtime_config)
            fake_codex = temp / "fake-codex"
            capture_path = temp / "capture.json"
            self.write_fake_codex(fake_codex)
            before = {
                "config": config_path.read_bytes(),
                "auth": auth_path.read_bytes(),
                "files": sorted(path.name for path in codex_home.iterdir()),
            }

            result = self.run_launcher(
                runtime_config,
                ["exec", "exec", "--ephemeral", "synthetic prompt"],
                extra_env={
                    "CODEX_BIN": str(fake_codex),
                    "CODEX_HOME": str(codex_home),
                    "FAKE_CODEX_CAPTURE": str(capture_path),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
            overrides = self.parsed_overrides(capture["argv"])
            provider = "model_providers.litellm_menu_local"
            self.assertEqual(overrides["model_provider"], "litellm_menu_local")
            self.assertEqual(overrides[f"{provider}.name"], "OpenAI")
            self.assertEqual(overrides[f"{provider}.base_url"], "http://127.0.0.1:4000/v1")
            self.assertEqual(overrides[f"{provider}.wire_api"], "responses")
            self.assertEqual(overrides[f"{provider}.auth.command"], sys.executable)
            self.assertEqual(overrides[f"{provider}.auth.args"], [str(CODEX_LAUNCHER), "auth-token"])
            self.assertEqual(overrides[f"{provider}.auth.timeout_ms"], 5000)
            self.assertEqual(overrides[f"{provider}.auth.refresh_interval_ms"], 300000)
            self.assertEqual(capture["argv"][-3:], ["exec", "--ephemeral", "synthetic prompt"])
            self.assertEqual(capture["codex_home"], str(codex_home))
            self.assertFalse(capture["synthetic_key_in_environment"])
            self.assertNotIn(SYNTHETIC_API_KEY, "\n".join(capture["argv"]))
            self.assertNotIn("--ignore-user-config", capture["argv"])
            self.assertNotIn("model", overrides)
            self.assertEqual(config_path.read_bytes(), before["config"])
            self.assertEqual(auth_path.read_bytes(), before["auth"])
            self.assertEqual(sorted(path.name for path in codex_home.iterdir()), before["files"])

    def test_isolated_exec_uses_custom_local_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "config.yaml"
            fake_codex = temp / "fake-codex"
            capture_path = temp / "capture.json"
            self.write_runtime_config(runtime_config)
            self.write_fake_codex(fake_codex)

            result = self.run_launcher(
                runtime_config,
                ["exec", "--version"],
                extra_env={
                    "CODEX_BIN": str(fake_codex),
                    "FAKE_CODEX_CAPTURE": str(capture_path),
                    "LITELLM_PORT": "49240",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
            overrides = self.parsed_overrides(capture["argv"])
            self.assertEqual(
                overrides["model_providers.litellm_menu_local.base_url"],
                "http://127.0.0.1:49240/v1",
            )

    def test_auth_token_reads_key_without_touching_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_config = temp / "config.yaml"
            codex_home = temp / "codex-home"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            auth_path = codex_home / "auth.json"
            config_path.write_text('model = "keep"\n', encoding="utf-8")
            auth_path.write_text('{"tokens":{"keep":true}}\n', encoding="utf-8")
            self.write_runtime_config(runtime_config)
            before = (config_path.read_bytes(), auth_path.read_bytes())

            result = self.run_launcher(
                runtime_config,
                ["auth-token"],
                extra_env={"CODEX_HOME": str(codex_home)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, SYNTHETIC_API_KEY)
            self.assertEqual((config_path.read_bytes(), auth_path.read_bytes()), before)

    def test_auth_token_resolves_environment_backed_master_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_config = Path(temp_dir) / "config.yaml"
            self.write_runtime_config(runtime_config, "os.environ/LITELLM_TEST_MASTER_KEY")

            result = self.run_launcher(
                runtime_config,
                ["auth-token"],
                extra_env={"LITELLM_TEST_MASTER_KEY": "sk-test-from-environment"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "sk-test-from-environment")


if __name__ == "__main__":
    unittest.main()

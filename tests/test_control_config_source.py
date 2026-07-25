from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "service.sh"
YAML_SITE_PACKAGES = Path(yaml.__file__).resolve().parents[1]


class ControlConfigSourceTests(unittest.TestCase):
    def make_checkout(self, temp: Path) -> Path:
        checkout = temp / "checkout"
        checkout.mkdir()
        shutil.copy2(CONTROL, checkout / "service.sh")
        shutil.copytree(ROOT / "service", checkout / "service")
        shutil.copytree(ROOT / "config_editor_core", checkout / "config_editor_core")
        (checkout / "service.sh").chmod(0o755)
        callback_package = checkout / "litellm_menu"
        callback_package.mkdir()
        (callback_package / "__init__.py").write_text("# test callback package\n", encoding="utf-8")
        (callback_package / "callbacks.py").write_text("# test callback\n", encoding="utf-8")
        (checkout / "config.example.yaml").write_text(
            "model_list: []\n",
            encoding="utf-8",
        )
        return checkout

    def run_stage(self, checkout: Path, home: Path, extra_env: dict[str, str] | None = None):
        helper_python = ROOT / ".venv/bin/python"
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "LITELLM_PORT": "49232",
                "LITELLM_APP_LAUNCH_AGENT_LABEL": "menu.litellm.menu-login.config-source-test",
                "LITELLM_CONFIG_WATCH_LABEL": "menu.litellm.config-watch.config-source-test",
                "PYTHON": str(helper_python if helper_python.exists() else sys.executable),
                "PYTHONPATH": os.pathsep.join(
                    value
                    for value in (
                        str(YAML_SITE_PACKAGES),
                        env.get("PYTHONPATH", ""),
                    )
                    if value
                ),
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(checkout / "service.sh"), "stage-config"],
            cwd=checkout,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_runtime_stages_default_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            runtime_root = temp / "runtime"
            runtime_root.mkdir(parents=True)
            source_config = runtime_root / "config.yaml"
            source_config.write_text(
                textwrap.dedent(
                    """
                    model_list:
                      - model_name: checkout-model
                        litellm_params:
                          model: openai/checkout-model
                        model_info:
                          id: c0dec001
                          upstream_url_surface: openai/responses
                          supported_upstream_url_surfaces: [openai/responses]
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            result = self.run_stage(
                checkout,
                home,
                {"LITELLM_RUNTIME_ROOT": str(runtime_root)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            runtime_config = runtime_root / ".litellm-runtime/config.yaml"
            staged = yaml.safe_load(runtime_config.read_text(encoding="utf-8"))
            model_info = staged["model_list"][0]["model_info"]
            self.assertEqual(model_info["id"], "c0dec001")
            self.assertFalse((runtime_config.parent / "litellm_menu" / "callbacks.py").exists())
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), source_config.read_text(encoding="utf-8"))

    def test_stage_config_does_not_echo_invalid_yaml_source_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            runtime_root = temp / "runtime"
            runtime_root.mkdir(parents=True)
            marker = "sk-synthetic-leak-marker"
            (runtime_root / "config.yaml").write_text(
                f'providers:\n  primary:\n    value: "{marker}\n',
                encoding="utf-8",
            )

            result = self.run_stage(
                checkout,
                home,
                {"LITELLM_RUNTIME_ROOT": str(runtime_root)},
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("config.yaml is not valid YAML", result.stderr)
            self.assertNotIn(marker, result.stderr)

    def test_explicit_runtime_root_keeps_runtime_config_as_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            runtime = temp / "runtime"
            runtime.mkdir()
            (checkout / "config.yaml").write_text(
                "model_list: [{model_name: checkout-model, litellm_params: {model: openai/checkout}, model_info: {id: c0dec002, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}}]\n",
                encoding="utf-8",
            )
            runtime_config_source = runtime / "config.yaml"
            runtime_config_source.write_text(
                "model_list: [{model_name: runtime-model, litellm_params: {model: openai/runtime}, model_info: {id: c0dec003, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}}]\n",
                encoding="utf-8",
            )

            result = self.run_stage(
                checkout,
                home,
                {"LITELLM_RUNTIME_ROOT": str(runtime)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            staged = runtime / ".litellm-runtime/config.yaml"
            staged_data = yaml.safe_load(staged.read_text(encoding="utf-8"))
            model_info = staged_data["model_list"][0]["model_info"]
            self.assertEqual(model_info["id"], "c0dec003")
            self.assertFalse((staged.parent / "litellm_menu" / "callbacks.py").exists())

    def test_unchanged_installed_config_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            runtime = temp / "runtime"
            runtime.mkdir()
            source = runtime / "config.yaml"
            source.write_text(
                "model_list: [{model_name: default-chat, litellm_params: {model: openai/default-chat}, model_info: {id: c0dec004, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}}]\n",
                encoding="utf-8",
            )
            environment = {"LITELLM_RUNTIME_ROOT": str(runtime)}

            first = self.run_stage(checkout, home, environment)
            self.assertEqual(first.returncode, 0, first.stderr)
            staged = runtime / ".litellm-runtime/config.yaml"
            first_stat = staged.stat()

            second = self.run_stage(checkout, home, environment)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("config.yaml unchanged", second.stdout)
            second_stat = staged.stat()
            self.assertEqual(first_stat.st_ino, second_stat.st_ino)
            self.assertEqual(first_stat.st_mtime_ns, second_stat.st_mtime_ns)

    def test_stage_config_does_not_augment_context_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            runtime_root = temp / "runtime"
            runtime_root.mkdir(parents=True)
            checkout_config = runtime_root / "config.yaml"
            source_text = textwrap.dedent(
                """
                model_list:
                  - model_name: balanced-chat
                    litellm_params:
                      model: openai/vendor-chat
                      api_base: https://example.test/v1
                    model_info:
                      id: 1234abcd
                      provider: example
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                """
            ).lstrip()
            checkout_config.write_text(source_text, encoding="utf-8")

            result = self.run_stage(
                checkout,
                home,
                {"LITELLM_RUNTIME_ROOT": str(runtime_root)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Augmented context metadata", result.stdout)
            self.assertEqual(checkout_config.read_text(encoding="utf-8"), source_text)

            runtime_config = runtime_root / ".litellm-runtime/config.yaml"
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), source_text)

    def test_stage_config_rejects_removed_context_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            runtime_root = temp / "runtime"
            runtime_root.mkdir(parents=True)
            checkout_config = runtime_root / "config.yaml"
            source_text = textwrap.dedent(
                """
                model_list:
                  - model_name: legacy-chat
                    litellm_params:
                      model: openai/vendor-chat
                      api_base: https://example.test/v1
                    model_info:
                      id: 1234abcf
                      provider: provider_chat
                      max_input_tokens: 1048576
                      max_output_tokens: 32768
                      max_tokens: 32768
                      context_metadata_source: openrouter
                      context_metadata_model_id: vendor/vendor-chat
                """
            ).lstrip()
            checkout_config.write_text(source_text, encoding="utf-8")

            result = self.run_stage(
                checkout,
                home,
                {"LITELLM_RUNTIME_ROOT": str(runtime_root)},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsupported max_input_tokens", result.stderr)
            self.assertEqual(checkout_config.read_text(encoding="utf-8"), source_text)


if __name__ == "__main__":
    unittest.main()

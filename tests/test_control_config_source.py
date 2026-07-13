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
                "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
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

    def test_checkout_apply_stages_checkout_config_into_default_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            checkout_config = checkout / "config.yaml"
            checkout_config.write_text(
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

            result = self.run_stage(checkout, home)

            self.assertEqual(result.returncode, 0, result.stderr)
            runtime_config = home / ".litellm-menu/.litellm-runtime/config.yaml"
            staged = yaml.safe_load(runtime_config.read_text(encoding="utf-8"))
            model_info = staged["model_list"][0]["model_info"]
            self.assertEqual(model_info["id"], "c0dec001")
            self.assertFalse((runtime_config.parent / "litellm_menu" / "callbacks.py").exists())
            editable_config = home / ".litellm-menu/config.yaml"
            self.assertEqual(editable_config.read_text(encoding="utf-8"), checkout_config.read_text(encoding="utf-8"))

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

    def test_codex_local_exec_reads_active_staged_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            runtime = temp / "runtime"
            staged_dir = runtime / ".litellm-runtime"
            staged_dir.mkdir(parents=True)
            editable_config = runtime / "config.yaml"
            staged_config = staged_dir / "config.yaml"
            editable_config.write_text("general_settings: {master_key: sk-test-pending}\n", encoding="utf-8")
            staged_config.write_text("general_settings: {master_key: sk-test-active}\n", encoding="utf-8")
            (checkout / "codex_launcher.py").write_text(
                "import os\nprint(os.environ['LITELLM_CONFIG_FILE'])\n",
                encoding="utf-8",
            )

            helper_python = ROOT / ".venv/bin/python"
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
                    "LITELLM_RUNTIME_ROOT": str(runtime),
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
            result = subprocess.run(
                [str(checkout / "service.sh"), "codex-local-exec", "--version"],
                cwd=checkout,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(staged_config))

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

    def test_checkout_fast_path_requires_current_root_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            source = checkout / "config.yaml"
            source.write_text(
                "model_list: [{model_name: default-chat, litellm_params: {model: openai/default-chat}, model_info: {id: c0dec005, upstream_url_surface: openai/responses, supported_upstream_url_surfaces: [openai/responses]}}]\n",
                encoding="utf-8",
            )

            first = self.run_stage(checkout, home)
            self.assertEqual(first.returncode, 0, first.stderr)
            root_mirror = home / ".litellm-menu/config.yaml"
            root_mirror.write_text("model_list: []\n", encoding="utf-8")

            second = self.run_stage(checkout, home)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("Mirrored checkout config", second.stdout)
            self.assertEqual(root_mirror.read_bytes(), source.read_bytes())

    def test_stage_config_does_not_augment_context_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            checkout_config = checkout / "config.yaml"
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

            result = self.run_stage(checkout, home)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Augmented context metadata", result.stdout)
            self.assertEqual(checkout_config.read_text(encoding="utf-8"), source_text)

            runtime_config = home / ".litellm-menu/.litellm-runtime/config.yaml"
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), source_text)

    def test_stage_config_rejects_removed_context_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            checkout = self.make_checkout(temp)
            home = temp / "home"
            checkout_config = checkout / "config.yaml"
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

            result = self.run_stage(checkout, home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsupported max_input_tokens", result.stderr)
            self.assertEqual(checkout_config.read_text(encoding="utf-8"), source_text)


if __name__ == "__main__":
    unittest.main()

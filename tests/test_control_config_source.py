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
                "model_list: [{model_name: checkout-model, litellm_params: {model: openai/checkout}, model_info: {id: c0dec002}}]\n",
                encoding="utf-8",
            )
            runtime_config_source = runtime / "config.yaml"
            runtime_config_source.write_text(
                "model_list: [{model_name: runtime-model, litellm_params: {model: openai/runtime}, model_info: {id: c0dec003}}]\n",
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
                """
            ).lstrip()
            checkout_config.write_text(source_text, encoding="utf-8")

            result = self.run_stage(checkout, home)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Augmented context metadata", result.stdout)
            self.assertEqual(checkout_config.read_text(encoding="utf-8"), source_text)

            runtime_config = home / ".litellm-menu/.litellm-runtime/config.yaml"
            self.assertEqual(runtime_config.read_text(encoding="utf-8"), source_text)

    def test_stage_config_removes_explicit_context_metadata(self) -> None:
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

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Applied learned context metadata", result.stdout)
            self.assertIn("Removed legacy context metadata", result.stdout)
            self.assertEqual(checkout_config.read_text(encoding="utf-8"), source_text)

            runtime_config = home / ".litellm-menu/.litellm-runtime/config.yaml"
            staged = yaml.safe_load(runtime_config.read_text(encoding="utf-8"))
            model_info = staged["model_list"][0]["model_info"]
            self.assertEqual(model_info["max_output_tokens"], 32768)
            self.assertEqual(model_info["max_tokens"], 32768)
            self.assertNotIn("max_input_tokens", model_info)
            self.assertNotIn("context_metadata_source", model_info)
            self.assertNotIn("context_metadata_model_id", model_info)


if __name__ == "__main__":
    unittest.main()

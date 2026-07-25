from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import configuration_package


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "configuration_package.py"
CONTROL = ROOT / "service.sh"
PACKAGE_FORMAT = "litellm-menu-configuration-package"


class ConfigurationPackageTests(unittest.TestCase):
    def run_tool(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def write_config(self, directory: Path, text: str) -> Path:
        path = directory / "config.yaml"
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
        return path

    def test_bounded_cli_response_reserves_its_trailing_newline(self) -> None:
        with mock.patch.object(configuration_package, "MAX_PACKAGE_BYTES", 6):
            self.assertEqual('"123"', configuration_package._encoded_response("123"))
        with mock.patch.object(configuration_package, "MAX_PACKAGE_BYTES", 5):
            with self.assertRaises(configuration_package.ConfigurationPackageError):
                configuration_package._encoded_response("123")

    def run_control(
        self,
        runtime_root: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "HOME": str(runtime_root.parent / "home"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "LITELLM_RUNTIME_ROOT": str(runtime_root),
            "LITELLM_TEMPLATE_ROOT": str(ROOT),
            "LITELLM_PORT": "49279",
            "LITELLM_APP_LAUNCH_AGENT_LABEL": "menu.litellm.menu-login.configuration-package-test",
            "LITELLM_CONFIG_WATCH_LABEL": "menu.litellm.config-watch.configuration-package-test",
        }
        return subprocess.run(
            ["/bin/bash", str(CONTROL), *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_all_round_trip_is_restricted_and_import_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            config = self.write_config(
                temporary,
                """
                providers:
                  primary:
                    api_base: "https://primary.example.test/v1"
                    api_keys:
                      - name: default
                        value: "synthetic-provider-secret"
                model_list:
                  - model_name: default-chat
                    litellm_params:
                      model: openai/default-chat
                      api_base: "https://primary.example.test/v1"
                      api_key: "synthetic-provider-secret"
                    model_info:
                      id: "00000001"
                      provider: primary
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                """,
            )
            disabled = temporary / "config.disabled-models.yaml"
            disabled.write_text(
                textwrap.dedent(
                    """
                    disabled_model_list:
                      - model_name: backup-chat
                        litellm_params:
                          model: openai/backup-chat
                          api_base: "https://primary.example.test/v1"
                          api_key: "synthetic-provider-secret"
                        model_info:
                          id: "00000002"
                          provider: primary
                          upstream_url_surface: openai/chat
                          supported_upstream_url_surfaces: [openai/chat]
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            settings = temporary / "runtime-settings.env"
            settings.write_text(
                "LITELLM_PORT=49240\n"
                "LITELLM_MENU_VISION_BRIDGE_API_KEY=synthetic-runtime-secret\n",
                encoding="utf-8",
            )
            output = temporary / "configuration.json"
            config_before = config.read_bytes()
            disabled_before = disabled.read_bytes()
            settings_before = settings.read_bytes()

            exported = self.run_tool(
                "export",
                "--sections",
                "all",
                "--config",
                str(config),
                "--settings-file",
                str(settings),
                "--output",
                str(output),
            )

            self.assertEqual(0, exported.returncode, exported.stderr)
            self.assertNotIn("synthetic-provider-secret", exported.stdout)
            self.assertNotIn("synthetic-runtime-secret", exported.stdout)
            self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
            package = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(PACKAGE_FORMAT, package["format"])
            self.assertEqual(1, package["version"])
            self.assertEqual({"runtime_settings", "providers_models"}, set(package["sections"]))
            self.assertEqual("49240", package["sections"]["runtime_settings"]["values"]["LITELLM_PORT"])
            self.assertEqual(config.read_text(encoding="utf-8"), package["sections"]["providers_models"]["document"]["config"])
            self.assertEqual(disabled.read_text(encoding="utf-8"), package["sections"]["providers_models"]["document"]["disabled"])

            imported = self.run_tool("import", "--input", str(output))

            self.assertEqual(0, imported.returncode, imported.stderr)
            result = json.loads(imported.stdout)
            self.assertEqual(["runtime_settings", "providers_models"], result["sections"])
            self.assertEqual("49240", result["runtime_settings"]["values"]["LITELLM_PORT"])
            self.assertEqual(["primary"], [provider["name"] for provider in result["providers_models"]["providers"]])
            self.assertEqual(config.read_text(encoding="utf-8"), result["providers_models"]["document"]["config"])
            self.assertEqual(config_before, config.read_bytes())
            self.assertEqual(disabled_before, disabled.read_bytes())
            self.assertEqual(settings_before, settings.read_bytes())

    def test_each_explicit_section_requires_only_its_own_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            config = self.write_config(temporary, "providers: {}\nmodel_list: []\n")
            settings = temporary / "runtime-settings.env"
            settings.write_text("LITELLM_PORT=49240\n", encoding="utf-8")
            runtime_output = temporary / "runtime.json"
            providers_output = temporary / "providers.json"

            runtime = self.run_tool(
                "export",
                "--sections",
                "runtime_settings",
                "--settings-file",
                str(settings),
                "--output",
                str(runtime_output),
            )
            providers = self.run_tool(
                "export",
                "--sections",
                "providers_models",
                "--config",
                str(config),
                "--output",
                str(providers_output),
            )

            self.assertEqual(0, runtime.returncode, runtime.stderr)
            self.assertEqual(0, providers.returncode, providers.stderr)
            self.assertEqual(
                {"runtime_settings"},
                set(json.loads(runtime_output.read_text(encoding="utf-8"))["sections"]),
            )
            self.assertEqual(
                {"providers_models"},
                set(json.loads(providers_output.read_text(encoding="utf-8"))["sections"]),
            )

            missing_config = self.run_tool(
                "export",
                "--sections",
                "providers_models",
                "--output",
                str(temporary / "missing.json"),
            )
            unused_config = self.run_tool(
                "export",
                "--sections",
                "runtime_settings",
                "--config",
                str(config),
                "--settings-file",
                str(settings),
                "--output",
                str(temporary / "unused.json"),
            )
            invalid_all = self.run_tool(
                "export",
                "--sections",
                "all,runtime_settings",
                "--config",
                str(config),
                "--settings-file",
                str(settings),
                "--output",
                str(temporary / "invalid.json"),
            )

            self.assertNotEqual(0, missing_config.returncode)
            self.assertIn("requires --config", missing_config.stderr)
            self.assertNotEqual(0, unused_config.returncode)
            self.assertIn("no unused config", unused_config.stderr)
            self.assertNotEqual(0, invalid_all.returncode)
            self.assertIn("all cannot", invalid_all.stderr)

    def test_import_rejects_legacy_unknown_duplicate_and_invalid_sections_without_secret_echo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            package = temporary / "configuration.json"
            secret = "synthetic-secret-not-for-diagnostics"
            package.write_text('{"sections":{}}', encoding="utf-8")

            legacy = self.run_tool("import", "--input", str(package))

            self.assertNotEqual(0, legacy.returncode)
            self.assertIn("unsupported shape", legacy.stderr)

            package.write_text(
                json.dumps(
                    {
                        "format": PACKAGE_FORMAT,
                        "version": 1,
                        "sections": {"unknown": {}},
                    }
                ),
                encoding="utf-8",
            )
            unknown = self.run_tool("import", "--input", str(package))
            self.assertNotEqual(0, unknown.returncode)
            self.assertIn("unsupported section", unknown.stderr)

            package.write_text(
                "{"
                f'"format":"{PACKAGE_FORMAT}",'
                '"version":1,'
                '"sections":{"runtime_settings":{"values":{"LITELLM_PORT":"4000","LITELLM_PORT":"bad"}}}'
                "}",
                encoding="utf-8",
            )
            duplicate = self.run_tool("import", "--input", str(package))
            self.assertNotEqual(0, duplicate.returncode)
            self.assertIn("duplicate key", duplicate.stderr)

            package.write_text(
                json.dumps(
                    {
                        "format": PACKAGE_FORMAT,
                        "version": 1,
                        "sections": {
                            "providers_models": {
                                "document": {
                                    "config": f"providers:\n  primary:\n    api_key: {secret}\nmodel_list: []\n",
                                    "disabled": None,
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            invalid_document = self.run_tool("import", "--input", str(package))

            self.assertNotEqual(0, invalid_document.returncode)
            self.assertIn("Provider/model section is invalid", invalid_document.stderr)
            self.assertNotIn(secret, invalid_document.stderr)

    def test_export_does_not_replace_an_explicit_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            settings = temporary / "runtime-settings.env"
            settings.write_text("LITELLM_PORT=49240\n", encoding="utf-8")
            before = settings.read_bytes()

            result = self.run_tool(
                "export",
                "--sections",
                "runtime_settings",
                "--settings-file",
                str(settings),
                "--output",
                str(settings),
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("must differ", result.stderr)
            self.assertEqual(before, settings.read_bytes())

    def test_service_dispatch_exports_each_section_and_imports_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            runtime_root = temporary / "runtime"
            runtime_root.mkdir()
            config = self.write_config(runtime_root, "providers: {}\nmodel_list: []\n")
            settings = runtime_root / "runtime-settings.env"
            settings.write_text("LITELLM_PORT=49241\n", encoding="utf-8")
            before_config = config.read_bytes()
            before_settings = settings.read_bytes()
            outputs = {
                "runtime_settings": temporary / "runtime.json",
                "providers_models": temporary / "providers.json",
                "all": temporary / "all.json",
            }

            for section, output in outputs.items():
                with self.subTest(section=section):
                    exported = self.run_control(
                        runtime_root,
                        "configuration-package-export",
                        "--sections",
                        section,
                        "--output",
                        str(output),
                    )
                    self.assertEqual(0, exported.returncode, exported.stdout + exported.stderr)
                    self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
                    package = json.loads(output.read_text(encoding="utf-8"))
                    expected = (
                        {"runtime_settings", "providers_models"}
                        if section == "all"
                        else {section}
                    )
                    self.assertEqual(expected, set(package["sections"]))

            imported = self.run_control(
                runtime_root,
                "configuration-package-import",
                "--input",
                str(outputs["all"]),
            )

            self.assertEqual(0, imported.returncode, imported.stdout + imported.stderr)
            self.assertEqual(
                ["runtime_settings", "providers_models"],
                json.loads(imported.stdout)["sections"],
            )
            self.assertEqual(before_config, config.read_bytes())
            self.assertEqual(before_settings, settings.read_bytes())


if __name__ == "__main__":
    unittest.main()

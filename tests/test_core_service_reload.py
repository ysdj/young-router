from __future__ import annotations

from pathlib import Path
import json
import tempfile
import textwrap
import unittest
from unittest import mock

from litellm_menu.core.domains.codex import CodexSettingsDomain
from litellm_menu.core.domains.providers_models import ProvidersModelsDomain
from litellm_menu.core.service import CoreStore


class CoreServiceReloadTests(unittest.TestCase):
    def test_provider_apply_reloads_service_after_source_config_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "providers:\n"
                "  primary:\n"
                "    api_base: https://example.test/v1\n"
                "    api_keys:\n"
                "      - name: default\n"
                "        value: replace-me\n"
                "model_list:\n"
                "  - model_name: public-chat\n"
                "    litellm_params:\n"
                "      model: openai/old-chat\n"
                "      api_base: https://example.test/v1\n"
                "      api_key: replace-me\n"
                "    model_info:\n"
                "      id: deadbeef\n"
                "      provider: primary\n"
                "      upstream_url_surface: openai/responses\n"
                "litellm_settings:\n"
                "  public_model_groups: [public-chat]\n",
                encoding="utf-8",
            )
            provider_domain = ProvidersModelsDomain(config_path)
            reload_calls: list[str] = []

            def reload_service(operation: str) -> dict[str, str]:
                reload_calls.append(operation)
                return {"state": "running"}

            core = CoreStore(
                domains=[provider_domain],
                service_handlers={
                    "status": lambda _operation: {"state": "running"},
                    "reload": reload_service,
                },
            )
            core.snapshot()
            staged = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.patch",
                    "payload": {
                        "provider_id": "primary",
                        "model_id": "deadbeef",
                        "changes": {"upstream_model": "openai/new-chat"},
                    },
                },
                expected_revision=core.revision,
            )

            result = core.apply("providers_models", revision=staged["revision"])

            self.assertTrue(result["applied"])
            self.assertEqual(["reload"], reload_calls)
            self.assertIn("model: openai/new-chat", config_path.read_text(encoding="utf-8"))

    def test_provider_apply_does_not_start_a_stopped_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "providers:\n"
                "  primary:\n"
                "    api_base: https://example.test/v1\n"
                "    api_keys:\n"
                "      - name: default\n"
                "        value: replace-me\n"
                "model_list:\n"
                "  - model_name: public-chat\n"
                "    litellm_params:\n"
                "      model: openai/old-chat\n"
                "      api_base: https://example.test/v1\n"
                "      api_key: replace-me\n"
                "    model_info:\n"
                "      id: deadbeef\n"
                "      provider: primary\n"
                "      upstream_url_surface: openai/responses\n"
                "litellm_settings:\n"
                "  public_model_groups: [public-chat]\n",
                encoding="utf-8",
            )
            reload_calls: list[str] = []

            def reload_service(operation: str) -> dict[str, str]:
                reload_calls.append(operation)
                return {"state": "running"}

            core = CoreStore(
                domains=[ProvidersModelsDomain(config_path)],
                service_handlers={
                    "status": lambda _operation: {"state": "stopped"},
                    "reload": reload_service,
                },
            )
            core.snapshot()
            staged = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.patch",
                    "payload": {
                        "provider_id": "primary",
                        "model_id": "deadbeef",
                        "changes": {"upstream_model": "openai/new-chat"},
                    },
                },
                expected_revision=core.revision,
            )

            result = core.apply("providers_models", revision=staged["revision"])

            self.assertTrue(result["applied"])
            self.assertEqual([], reload_calls)

    def test_provider_apply_refreshes_enabled_codex_catalog_and_requests_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    providers:
                      primary:
                        api_base: https://example.test/v1
                        api_keys:
                          - name: default
                            value: replace-me
                    model_list:
                      - model_name: public-a
                        litellm_params:
                          model: openai/upstream-a
                          api_base: https://example.test/v1
                          api_key: replace-me
                        model_info:
                          id: deadbeef
                          provider: primary
                          upstream_url_surface: openai/responses
                    litellm_settings:
                      public_model_groups: [public-a]
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            codex_home = root / "codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('model = "public-a"\n', encoding="utf-8")
            (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
            live_models = {"names": ["public-a"]}

            def exposed_models(_api_key: str) -> tuple[list[str], bool]:
                return list(live_models["names"]), True

            def reload_service(operation: str) -> dict[str, str]:
                self.assertEqual("reload", operation)
                live_models["names"] = ["public-b"]
                return {"state": "running"}

            def status_service(operation: str) -> dict[str, str]:
                self.assertEqual("status", operation)
                return {"state": "running"}

            with mock.patch(
                "codex_config._local_exposed_models",
                side_effect=exposed_models,
            ), mock.patch(
                "litellm_menu.core.model_catalog.load_native_catalog",
                return_value=[],
            ):
                providers = ProvidersModelsDomain(config_path)
                codex = CodexSettingsDomain(config_path, codex_home=codex_home)
                core = CoreStore(
                    domains=[providers, codex],
                    service_handlers={"status": status_service, "reload": reload_service},
                )
                core.snapshot()
                enabled = core.dispatch(
                    {
                        "domain": "codex",
                        "type": "codex.model_catalog.set",
                        "payload": {"enabled": True},
                    },
                    expected_revision=core.revision,
                )
                acknowledged = core.dispatch(
                    {
                        "domain": "codex",
                        "type": "acknowledge_model_catalog_restart",
                        "payload": {},
                    },
                    expected_revision=enabled["revision"],
                )
                staged = core.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "model.patch",
                        "payload": {
                            "provider_id": "primary",
                            "model_id": "deadbeef",
                            "changes": {"name": "public-b"},
                        },
                    },
                    expected_revision=acknowledged["revision"],
                )

                result = core.apply("providers_models", revision=staged["revision"])
                catalog_state = core.snapshot()["domains"]["codex"]["model_catalog"]

            catalog = json.loads(
                (codex_home / "litellm-menu-model-catalog.json").read_text(encoding="utf-8")
            )
            self.assertTrue(result["applied"])
            self.assertEqual(["public-b"], [model["slug"] for model in catalog["models"]])
            self.assertEqual(["public-b"], catalog_state["public_models"])
            self.assertTrue(catalog_state["restart_required"])
            self.assertEqual("catalog_repaired", catalog_state["change_reason"])


if __name__ == "__main__":
    unittest.main()

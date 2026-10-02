from __future__ import annotations

from pathlib import Path
import json
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.service import CoreStore


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
            # The proxy restart runs in the background; wait for it before
            # observing the controller calls it made.
            self.assertTrue(core.wait_for_service_reload(5.0))
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
                "young_router.core.codex_config._local_exposed_models",
                side_effect=exposed_models,
            ), mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
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
                self.assertTrue(core.wait_for_service_reload(5.0))
                # Endpoint-backed repairs require two fresh observations so a
                # transient post-reload worker view cannot manufacture a
                # restart prompt. The apply's forced refresh is the first;
                # the next snapshot with a fresh probe completes the pair.
                codex._catalog_source_checked_at = 0.0
                catalog_state = core.snapshot()["domains"]["codex"]["model_catalog"]

            catalog = json.loads(
                (codex_home / "model-catalog.json").read_text(encoding="utf-8")
            )
            self.assertTrue(result["applied"])
            self.assertEqual(["public-b"], [model["slug"] for model in catalog["models"]])
            self.assertEqual(["public-b"], catalog_state["public_models"])
            self.assertTrue(catalog_state["restart_required"])
            self.assertEqual("catalog_repaired", catalog_state["change_reason"])


    def test_proxy_restart_moves_a_codex_client_that_uses_this_apps_proxy(self) -> None:
        """A client on the proxy follows the endpoint the restart adopts."""

        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains import _shared

            root = Path(directory)
            config_path = root / "config.yaml"
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
            codex_home = root / "codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                'model = "public-chat"\n'
                'model_provider = "relay"\n'
                '\n'
                '[model_providers.relay]\n'
                'name = "relay"\n'
                'base_url = "http://127.0.0.1:19999/v1"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = true\n',
                encoding="utf-8",
            )
            (codex_home / "auth.json").write_text('{"OPENAI_API_KEY": "retired-key"}\n', encoding="utf-8")

            def reload_service(_operation: str) -> dict[str, str]:
                return {"state": "running"}

            def status_service(_operation: str) -> dict[str, str]:
                return {"state": "running"}

            # The app is running on the endpoint the client already uses; the
            # restart below moves the proxy to another one.
            with mock.patch.object(
                _shared, "local_proxy_endpoint", return_value=("http://127.0.0.1:19999/v1", "retired-key")
            ):
                providers = ProvidersModelsDomain(config_path)
                codex = CodexSettingsDomain(config_path, codex_home=codex_home)
            with mock.patch.object(
                _shared, "local_proxy_endpoint", return_value=("http://127.0.0.1:20001/v1", "sk-second")
            ):
                core = CoreStore(
                    domains=[providers, codex],
                    service_handlers={"status": status_service, "reload": reload_service},
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
                self.assertTrue(core.wait_for_service_reload(5.0))
                snapshot = core.snapshot()

            config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
            auth_text = (codex_home / "auth.json").read_text(encoding="utf-8")
            self.assertTrue(result["applied"])
            self.assertIn("http://127.0.0.1:20001/v1", config_text)
            self.assertNotIn("19999", config_text)
            # The rewrite stays under the provider the client already named.
            self.assertIn('model_provider = "relay"', config_text)
            self.assertNotIn("[model_providers.custom]", config_text)
            self.assertIn("sk-second", auth_text)
            self.assertNotIn("retired-key", auth_text)
            # The write is part of the applied state: the pane must not show a
            # pending change for a file the user never edited.
            self.assertFalse(snapshot["drafts"]["codex"]["dirty"])
            self.assertTrue(snapshot["domains"]["codex"]["uses_local_api"])

    def test_provider_apply_returns_before_the_background_restart_finishes(self) -> None:
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
            restart_started = threading.Event()
            restart_release = threading.Event()
            reload_calls: list[str] = []

            def reload_service(operation: str) -> dict[str, str]:
                reload_calls.append(operation)
                restart_started.set()
                self.assertTrue(restart_release.wait(5.0))
                return {"state": "running"}

            core = CoreStore(
                domains=[ProvidersModelsDomain(config_path)],
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

            started = time.monotonic()
            result = core.apply("providers_models", revision=staged["revision"])
            elapsed = time.monotonic() - started

            self.assertTrue(result["applied"])
            # A committed edit must not wait for the seconds-long restart.
            self.assertLess(elapsed, 2.0)
            self.assertTrue(restart_started.wait(5.0))
            # While the planned restart runs, snapshots keep the transitional
            # state instead of a cached controller status.
            self.assertEqual("starting", core.snapshot()["service"]["state"])
            restart_release.set()
            self.assertTrue(core.wait_for_service_reload(5.0))
            self.assertEqual("running", core.snapshot()["service"]["state"])
            self.assertEqual(["reload"], reload_calls)

    def test_provider_apply_coalesces_a_burst_of_edits_into_one_restart(self) -> None:
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
            restart_started = threading.Event()
            restart_release = threading.Event()
            reload_calls: list[str] = []

            def reload_service(operation: str) -> dict[str, str]:
                reload_calls.append(operation)
                if len(reload_calls) == 1:
                    restart_started.set()
                    self.assertTrue(restart_release.wait(5.0))
                return {"state": "running"}

            core = CoreStore(
                domains=[ProvidersModelsDomain(config_path)],
                service_handlers={
                    "status": lambda _operation: {"state": "running"},
                    "reload": reload_service,
                },
            )
            core.snapshot()

            def stage(name: str) -> int:
                staged = core.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "model.patch",
                        "payload": {
                            "provider_id": "primary",
                            "model_id": "deadbeef",
                            "changes": {"name": name},
                        },
                    },
                    expected_revision=core.revision,
                )
                return staged["revision"]

            core.apply("providers_models", revision=stage("public-first"))
            self.assertTrue(restart_started.wait(5.0))
            # Two more edits land while the first restart is in flight; both
            # answer immediately and fold into one follow-up restart.
            core.apply("providers_models", revision=stage("public-second"))
            core.apply("providers_models", revision=stage("public-third"))
            restart_release.set()
            self.assertTrue(core.wait_for_service_reload(5.0))
            self.assertEqual(["reload", "reload"], reload_calls)
            self.assertIn("public-third", config_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

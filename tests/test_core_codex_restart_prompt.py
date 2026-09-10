from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.model_catalog import catalog_model_names
from young_router.core.service import CoreStore


CONFIG = ("providers:\n"
          "  primary:\n"
          "    api_base: https://example.test/v1\n"
          "    api_keys:\n"
          "      - name: default\n"
          "        value: synthetic-key\n"
          "model_list:\n"
          "  - model_name: public-a\n"
          "    litellm_params:\n"
          "      model: openai/upstream-a\n"
          "      api_base: https://example.test/v1\n"
          "      api_key: synthetic-key\n"
          "litellm_settings:\n"
          "  public_model_groups: [public-a]\n")


class CodexRestartPromptTests(unittest.TestCase):
    def _core(self, root: Path) -> CoreStore:
        config = root / "config.yaml"
        config.write_text(CONFIG, encoding="utf-8")
        home = root / "codex"
        home.mkdir()
        (home / "config.toml").write_text('model = "public-a"\n', encoding="utf-8")
        (home / "auth.json").write_text("{}\n", encoding="utf-8")
        domain = CodexSettingsDomain(config, codex_home=home)
        return CoreStore(domains=[domain])

    def _domain(self, root: Path) -> CodexSettingsDomain:
        config = root / "config.yaml"
        config.write_text(CONFIG, encoding="utf-8")
        home = root / "codex"
        home.mkdir()
        (home / "config.toml").write_text('model = "public-a"\n', encoding="utf-8")
        (home / "auth.json").write_text("{}\n", encoding="utf-8")
        return CodexSettingsDomain(config, codex_home=home)

    @staticmethod
    def _force_catalog_observation(domain: CodexSettingsDomain) -> None:
        # The production probe is intentionally rate-limited; each test call
        # below represents a distinct endpoint observation.
        domain._catalog_source_checked_at = 0.0

    def _config_with_models(self, names: list[str]) -> str:
        entries = "\n".join(
            f"  - model_name: {name}\n"
            "    litellm_params:\n"
            f"      model: openai/upstream-{name}\n"
            "      api_base: https://example.test/v1\n"
            "      api_key: synthetic-key\n"
            for name in names
        )
        groups = ", ".join(names)
        return (
            "providers:\n"
            "  primary:\n"
            "    api_base: https://example.test/v1\n"
            "    api_keys:\n"
            "      - name: default\n"
            "        value: synthetic-key\n"
            f"model_list:\n{entries}"
            f"litellm_settings:\n  public_model_groups: [{groups}]\n"
        )

    def test_acknowledged_catalog_signature_does_not_queue_same_prompt_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=(["public-a"], True),
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            core = self._core(Path(directory))
            enabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=core.revision,
            )
            first = core.snapshot()["domains"]["codex"]["model_catalog"]
            self.assertTrue(first["restart_required"])
            core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=enabled["revision"],
            )
            self.assertFalse(core.snapshot()["domains"]["codex"]["model_catalog"]["restart_required"])

            domain = core._domains["codex"]
            domain._queue_catalog_restart("catalog_repaired", names=["public-a"], enabled=True)
            state = core.snapshot()["domains"]["codex"]["model_catalog"]
            self.assertFalse(state["restart_required"])
            self.assertEqual(first["change_event"], state["change_event"])

    def test_new_public_model_signature_still_queues_prompt_after_deferral(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=(["public-a"], True),
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            core = self._core(root)
            enabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=core.revision,
            )
            first = core.snapshot()["domains"]["codex"]["model_catalog"]
            core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=enabled["revision"],
            )
            domain = core._domains["codex"]
            # The applied configuration adopts public-b before the repair.
            (root / "config.yaml").write_text(
                self._config_with_models(["public-a", "public-b"]),
                encoding="utf-8",
            )
            domain._refresh_live_catalog_source(force=True)
            domain._queue_catalog_restart("catalog_repaired", names=["public-a", "public-b"], enabled=True)
            state = core.snapshot()["domains"]["codex"]["model_catalog"]
            self.assertTrue(state["restart_required"])
            self.assertEqual(first["change_event"] + 1, state["change_event"])

    def test_single_snapshot_model_change_does_not_rewrite_or_queue(self) -> None:
        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            domain = self._domain(Path(directory))
            enabled = domain.set_model_catalog_enabled_immediately(True)
            domain.dispatch("acknowledge_model_catalog_restart", {})
            catalog_path = domain.model_catalog_path
            before_event = enabled["model_catalog"]["change_event"]

            endpoint["models"] = ["public-a", "public-b"]
            self._force_catalog_observation(domain)
            state = domain.snapshot()["model_catalog"]

            self.assertFalse(state["restart_required"])
            self.assertEqual(before_event, state["change_event"])
            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

    def test_unconfigured_endpoint_names_never_rewrite_or_queue(self) -> None:
        """Endpoint routes outside the configured list are ignored entirely.

        Workers can transiently expose runtime-added names (for example an
        ``openai/<model>`` alias present on only a subset of workers).  Those
        flapping names must not rewrite the catalog or queue a Codex restart
        prompt, no matter how many consecutive observations agree on them.
        """

        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            domain = self._domain(Path(directory))
            enabled = domain.set_model_catalog_enabled_immediately(True)
            domain.dispatch("acknowledge_model_catalog_restart", {})
            catalog_path = domain.model_catalog_path
            before_event = enabled["model_catalog"]["change_event"]

            endpoint["models"] = ["public-a", "public-b"]
            for _ in range(3):
                self._force_catalog_observation(domain)
                state = domain.snapshot()["model_catalog"]
                self.assertFalse(state["restart_required"])
                self.assertEqual(before_event, state["change_event"])
                self.assertEqual(["public-a"], catalog_model_names(catalog_path))

    def test_unconfigured_route_drop_repairs_catalog_without_prompt(self) -> None:
        """Dropping a never-configured exposed route updates the catalog silently.

        A catalog acknowledged while a runtime-added route was visible can
        carry that route.  When the live view drops it, the catalog repair
        removes it without asking Codex to restart: the change involves no
        configured public model.
        """

        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            domain = self._domain(Path(directory))
            enabled = domain.set_model_catalog_enabled_immediately(True)
            domain.dispatch("acknowledge_model_catalog_restart", {})
            catalog_path = domain.model_catalog_path
            before_event = enabled["model_catalog"]["change_event"]

            # A previous run acknowledged a catalog that included a runtime
            # route outside the configured model list.
            from young_router.core.model_catalog import write_catalog

            write_catalog(catalog_path, ["public-a", "phantom-route"], registry=domain._context_registry)
            domain._catalog_acknowledged_signature = domain._catalog_signature(
                ["public-a", "phantom-route"], enabled=True
            )

            self._force_catalog_observation(domain)
            domain.snapshot()
            self._force_catalog_observation(domain)
            state = domain.snapshot()["model_catalog"]

            self.assertFalse(state["restart_required"])
            self.assertEqual(before_event, state["change_event"])
            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

    def test_configured_model_drop_still_queues_prompt(self) -> None:
        """A configured public model leaving the live exposure still prompts."""

        endpoint = {"models": ["public-a", "public-b"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                self._config_with_models(["public-a", "public-b"]),
                encoding="utf-8",
            )
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "public-a"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            domain = CodexSettingsDomain(config, codex_home=home)
            enabled = domain.set_model_catalog_enabled_immediately(True)
            domain.dispatch("acknowledge_model_catalog_restart", {})
            catalog_path = domain.model_catalog_path
            before_event = enabled["model_catalog"]["change_event"]

            endpoint["models"] = ["public-a"]
            self._force_catalog_observation(domain)
            domain.snapshot()
            self._force_catalog_observation(domain)
            state = domain.snapshot()["model_catalog"]

            self.assertTrue(state["restart_required"])
            self.assertEqual("catalog_repaired", state["change_reason"])
            self.assertEqual(before_event + 1, state["change_event"])
            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

    def test_two_consecutive_snapshot_observations_repair_and_queue(self) -> None:
        """A configured public model entering the live exposure is adopted."""

        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            domain = self._domain(root)
            enabled = domain.set_model_catalog_enabled_immediately(True)
            domain.dispatch("acknowledge_model_catalog_restart", {})
            catalog_path = domain.model_catalog_path
            before_event = enabled["model_catalog"]["change_event"]

            # The applied configuration adopts public-b and the endpoint
            # exposes it.
            (root / "config.yaml").write_text(
                self._config_with_models(["public-a", "public-b"]),
                encoding="utf-8",
            )
            endpoint["models"] = ["public-a", "public-b"]
            self._force_catalog_observation(domain)
            first = domain.snapshot()["model_catalog"]
            self.assertFalse(first["restart_required"])
            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

            # A second UI snapshot while the endpoint probe is still cached
            # is not a second observation and must not complete the repair.
            domain._catalog_source_checked_at = time.monotonic()
            cached = domain.snapshot()["model_catalog"]
            self.assertFalse(cached["restart_required"])
            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

            # Once a fresh identical endpoint observation arrives, the repair
            # becomes stable and may update the catalog and queue the prompt.
            self._force_catalog_observation(domain)
            second = domain.snapshot()["model_catalog"]
            self.assertTrue(second["restart_required"])
            self.assertEqual("catalog_repaired", second["change_reason"])
            self.assertEqual(before_event + 1, second["change_event"])
            self.assertEqual(["public-a", "public-b"], catalog_model_names(catalog_path))

    def test_acknowledged_catalog_ignores_9_10_snapshot_jitter(self) -> None:
        stable_models = [f"public-{index}" for index in range(10)]
        endpoint = {"models": list(stable_models)}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                self._config_with_models(stable_models),
                encoding="utf-8",
            )
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "public-0"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            domain = CodexSettingsDomain(config, codex_home=home)
            enabled = domain.set_model_catalog_enabled_immediately(True)
            domain.dispatch("acknowledge_model_catalog_restart", {})
            catalog_path = domain.model_catalog_path
            before_event = enabled["model_catalog"]["change_event"]

            for names in (stable_models[:-1], stable_models, stable_models[:-1], stable_models):
                endpoint["models"] = list(names)
                self._force_catalog_observation(domain)
                state = domain.snapshot()["model_catalog"]
                self.assertFalse(state["restart_required"])

            self.assertEqual(before_event, state["change_event"])
            self.assertEqual(stable_models, catalog_model_names(catalog_path))

    def test_single_post_apply_refresh_observation_does_not_rewrite_or_queue(self) -> None:
        """A provider apply that changed no exposure must not prompt Codex.

        The post-reload refresh sees the same endpoint jitter as snapshots.
        One observation of a changed view is not stable, so it must neither
        rewrite the catalog nor queue a restart prompt.  Names the endpoint
        exposes outside the configured model list are ignored entirely.
        """

        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            domain = self._domain(root)
            enabled = domain.set_model_catalog_enabled_immediately(True)
            domain.dispatch("acknowledge_model_catalog_restart", {})
            catalog_path = domain.model_catalog_path
            before_event = enabled["model_catalog"]["change_event"]

            endpoint["models"] = ["public-a", "public-b"]
            self._force_catalog_observation(domain)
            refreshed = domain.refresh_model_catalog()

            self.assertFalse(refreshed)
            self.assertFalse(domain.snapshot()["model_catalog"]["restart_required"])
            self.assertEqual(before_event, domain.snapshot()["model_catalog"]["change_event"])
            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

            # Once the applied configuration adopts the new model and two
            # stable refresh observations agree, the repair completes and
            # queues the prompt, so genuine exposure changes are surfaced.
            (root / "config.yaml").write_text(
                self._config_with_models(["public-a", "public-b"]),
                encoding="utf-8",
            )
            self._force_catalog_observation(domain)
            domain.refresh_model_catalog()
            self._force_catalog_observation(domain)
            domain.refresh_model_catalog()
            second = domain.snapshot()["model_catalog"]
            self.assertTrue(second["restart_required"])
            self.assertEqual("catalog_repaired", second["change_reason"])
            self.assertEqual(before_event + 1, second["change_event"])
            self.assertEqual(["public-a", "public-b"], catalog_model_names(catalog_path))

    def test_acknowledged_catalog_signature_survives_core_recreation(self) -> None:
        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            first = self._domain(root)
            enabled = first.set_model_catalog_enabled_immediately(True)
            first.dispatch("acknowledge_model_catalog_restart", {})
            self.assertTrue(first.model_catalog_ack_path.exists())

            # A subscription recovery creates a fresh Codex domain, so this
            # verifies the acknowledgement is not only process-local memory.
            second = CodexSettingsDomain(root / "config.yaml", codex_home=root / "codex")
            self._force_catalog_observation(second)
            unchanged = second.snapshot()["model_catalog"]
            self.assertFalse(unchanged["restart_required"])
            self.assertEqual(0, unchanged["change_event"])

            # The applied configuration adopts public-b; two fresh
            # observations of the changed exposure complete the repair.
            (root / "config.yaml").write_text(
                self._config_with_models(["public-a", "public-b"]),
                encoding="utf-8",
            )
            endpoint["models"] = ["public-a", "public-b"]
            self._force_catalog_observation(second)
            first_observation = second.snapshot()["model_catalog"]
            self.assertFalse(first_observation["restart_required"])
            self._force_catalog_observation(second)
            repaired = second.snapshot()["model_catalog"]
            self.assertTrue(repaired["restart_required"])
            self.assertEqual(1, repaired["change_event"])


if __name__ == "__main__":
    unittest.main()

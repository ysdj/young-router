from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.model_catalog import catalog_model_names, write_catalog
from young_router.core.protocol import validate_method_result
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


class CodexCatalogRepairTests(unittest.TestCase):
    """The managed catalog follows the live model set without asking for a restart.

    Repairing the catalog file is the app's own bookkeeping.  Nothing on this
    path asks the user to restart Codex, and a single endpoint view is still
    never enough to rewrite the client's model list.
    """

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

    def test_catalog_switch_answers_without_a_restart_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
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
            # The catalog switch answers in the dispatch envelope: a revision and
            # an action-scoped summary, never extra top-level fields.
            validate_method_result("dispatch", enabled)
            self.assertEqual({"revision", "action_summary"}, set(enabled))
            self.assertIsInstance(enabled["action_summary"], dict)
            state = core.snapshot()["domains"]["codex"]["model_catalog"]
            self.assertTrue(state["enabled"])
            self.assertEqual(["public-a"], state["public_models"])
            # The reminder and everything that fed it are gone from the
            # projection, so no window can present one.
            self.assertNotIn("restart_required", state)
            self.assertNotIn("change_reason", state)
            self.assertNotIn("change_event", state)

    def test_acknowledge_action_is_no_longer_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
            return_value=(["public-a"], True),
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            core = self._core(Path(directory))
            with self.assertRaises(Exception):
                core.dispatch(
                    {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                    expected_revision=core.revision,
                )

    def test_single_snapshot_model_change_does_not_rewrite(self) -> None:
        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            domain = self._domain(Path(directory))
            domain.set_model_catalog_enabled_immediately(True)
            catalog_path = domain.model_catalog_path

            endpoint["models"] = ["public-a", "public-b"]
            self._force_catalog_observation(domain)
            domain.snapshot()

            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

    def test_unconfigured_endpoint_names_never_rewrite(self) -> None:
        """Endpoint routes outside the configured list are ignored entirely.

        Workers can transiently expose runtime-added names (for example an
        ``openai/<model>`` alias present on only a subset of workers).  Those
        flapping names must not rewrite the catalog, no matter how many
        consecutive observations agree on them.
        """

        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            domain = self._domain(Path(directory))
            domain.set_model_catalog_enabled_immediately(True)
            catalog_path = domain.model_catalog_path

            endpoint["models"] = ["public-a", "public-b"]
            for _ in range(3):
                self._force_catalog_observation(domain)
                domain.snapshot()
                self.assertEqual(["public-a"], catalog_model_names(catalog_path))

    def test_unconfigured_route_drop_repairs_the_catalog_silently(self) -> None:
        """Dropping a never-configured exposed route updates the catalog.

        A catalog written while a runtime-added route was visible can carry
        that route.  When the live view drops it, the repair removes it: the
        change involves no configured public model, so it is the app's own
        bookkeeping.
        """

        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            domain = self._domain(Path(directory))
            domain.set_model_catalog_enabled_immediately(True)
            catalog_path = domain.model_catalog_path

            write_catalog(catalog_path, ["public-a", "phantom-route"], registry=domain._context_registry)

            self._force_catalog_observation(domain)
            domain.snapshot()
            self._force_catalog_observation(domain)
            domain.snapshot()

            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

    def test_configured_model_drop_repairs_the_catalog(self) -> None:
        """A configured public model leaving the live exposure is repaired."""

        endpoint = {"models": ["public-a", "public-b"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
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
            domain.set_model_catalog_enabled_immediately(True)
            catalog_path = domain.model_catalog_path

            endpoint["models"] = ["public-a"]
            self._force_catalog_observation(domain)
            domain.snapshot()
            self._force_catalog_observation(domain)
            domain.snapshot()

            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

    def test_two_consecutive_snapshot_observations_repair_the_catalog(self) -> None:
        """A configured public model entering the live exposure is adopted."""

        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            domain = self._domain(root)
            domain.set_model_catalog_enabled_immediately(True)
            catalog_path = domain.model_catalog_path

            # The applied configuration adopts public-b and the endpoint
            # exposes it.
            (root / "config.yaml").write_text(
                self._config_with_models(["public-a", "public-b"]),
                encoding="utf-8",
            )
            endpoint["models"] = ["public-a", "public-b"]
            self._force_catalog_observation(domain)
            domain.snapshot()
            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

            # A second UI snapshot while the endpoint probe is still cached
            # is not a second observation and must not complete the repair.
            domain._catalog_source_checked_at = time.monotonic()
            domain.snapshot()
            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

            # Once a fresh identical endpoint observation arrives, the repair
            # becomes stable and may update the catalog.
            self._force_catalog_observation(domain)
            domain.snapshot()
            self.assertEqual(["public-a", "public-b"], catalog_model_names(catalog_path))

    def test_catalog_ignores_9_10_snapshot_jitter(self) -> None:
        stable_models = [f"public-{index}" for index in range(10)]
        endpoint = {"models": list(stable_models)}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
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
            domain.set_model_catalog_enabled_immediately(True)
            catalog_path = domain.model_catalog_path

            for names in (stable_models[:-1], stable_models, stable_models[:-1], stable_models):
                endpoint["models"] = list(names)
                self._force_catalog_observation(domain)
                domain.snapshot()

            self.assertEqual(stable_models, catalog_model_names(catalog_path))

    def test_single_post_apply_refresh_observation_does_not_rewrite(self) -> None:
        """A provider apply that changed no exposure must not rewrite anything.

        The post-reload refresh sees the same endpoint jitter as snapshots.
        One observation of a changed view is not stable, so it must not rewrite
        the catalog.  Names the endpoint exposes outside the configured model
        list are ignored entirely.
        """

        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            domain = self._domain(root)
            domain.set_model_catalog_enabled_immediately(True)
            catalog_path = domain.model_catalog_path

            endpoint["models"] = ["public-a", "public-b"]
            self._force_catalog_observation(domain)
            self.assertFalse(domain.refresh_model_catalog())
            self.assertEqual(["public-a"], catalog_model_names(catalog_path))

            # Once the applied configuration adopts the new model and two
            # stable refresh observations agree, the repair completes.
            (root / "config.yaml").write_text(
                self._config_with_models(["public-a", "public-b"]),
                encoding="utf-8",
            )
            self._force_catalog_observation(domain)
            domain.refresh_model_catalog()
            self._force_catalog_observation(domain)
            domain.refresh_model_catalog()
            self.assertEqual(["public-a", "public-b"], catalog_model_names(catalog_path))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from litellm_menu.core.domains.codex import CodexSettingsDomain
from litellm_menu.core.model_catalog import catalog_model_names
from litellm_menu.core.service import CoreStore


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

    def test_acknowledged_catalog_signature_does_not_queue_same_prompt_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=(["public-a"], True),
        ), mock.patch(
            "litellm_menu.core.model_catalog.load_native_catalog",
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
            "litellm_menu.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            core = self._core(Path(directory))
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
            "litellm_menu.core.model_catalog.load_native_catalog",
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

    def test_two_consecutive_snapshot_observations_repair_and_queue(self) -> None:
        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "litellm_menu.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            domain = self._domain(Path(directory))
            enabled = domain.set_model_catalog_enabled_immediately(True)
            domain.dispatch("acknowledge_model_catalog_restart", {})
            catalog_path = domain.model_catalog_path
            before_event = enabled["model_catalog"]["change_event"]

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
            "litellm_menu.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            domain = self._domain(Path(directory))
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

    def test_acknowledged_catalog_signature_survives_core_recreation(self) -> None:
        endpoint = {"models": ["public-a"]}

        def exposed_models(_api_key: str):
            return endpoint["models"], True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "litellm_menu.core.model_catalog.load_native_catalog",
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

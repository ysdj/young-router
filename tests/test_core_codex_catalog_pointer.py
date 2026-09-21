"""Regression tests for the managed Codex model-catalog pointer.

Codex refuses to load its whole configuration while ``model_catalog_json``
names a missing file ("No such file or directory"), so the app must never leave
a managed pointer dangling: it is rebuilt while the router is reachable and
dropped when it is not.  Installations that predate the Young Router rebrand
still point at the retired LiteLLM Menu file name and are migrated.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock

from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.model_catalog import (
    legacy_managed_catalog_path,
    managed_catalog_path,
)


CONFIG = textwrap.dedent(
    """
    providers:
      primary:
        api_base: "https://example.test/v1"
        api_keys:
          - name: default
            value: "replace-me-secret"
    model_list:
      - model_name: default-chat
        litellm_params:
          model: openai/default-chat
          api_base: "https://example.test/v1"
          api_key: "replace-me-secret"
        model_info:
          id: "00000091"
          provider: primary
          upstream_url_surface: openai/responses
    litellm_settings:
      public_model_groups: [default-chat]
    """
).lstrip()

EXPOSED = (["default-chat"], True)


class CodexCatalogPointerTests(unittest.TestCase):
    """Keep a broken-pointer Codex config from surviving any app path."""

    def _fixture(self, root: Path, pointer: str) -> tuple[Path, Path]:
        runtime = root / "config.yaml"
        runtime.write_text(CONFIG, encoding="utf-8")
        home = root / "codex"
        home.mkdir()
        (home / "config.toml").write_text(
            f'model = "default-chat"\nmodel_catalog_json = "{pointer}"\n',
            encoding="utf-8",
        )
        (home / "auth.json").write_text("{}\n", encoding="utf-8")
        return runtime, home

    @staticmethod
    def _catalog_models(path: Path) -> list[str]:
        return [model["slug"] for model in json.loads(path.read_text(encoding="utf-8"))["models"]]

    def test_legacy_pointer_is_migrated_to_the_current_catalog_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=EXPOSED,
        ):
            root = Path(directory)
            home = root / "codex"
            runtime, home = self._fixture(root, str(legacy_managed_catalog_path(home)))
            # The pre-rebrand file is what Codex is reading today.
            legacy_managed_catalog_path(home).write_text(
                json.dumps({"models": [{"slug": "default-chat"}]}), encoding="utf-8"
            )

            domain = CodexSettingsDomain(runtime, codex_home=home)
            snapshot = domain.snapshot()

            current = managed_catalog_path(home)
            self.assertTrue(current.exists())
            text = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn(f'model_catalog_json = "{current}"', text)
            self.assertNotIn("litellm-menu-model-catalog.json", text)
            self.assertTrue(snapshot["model_catalog"]["enabled"])
            self.assertEqual(["default-chat"], snapshot["model_catalog"]["public_models"])

    def test_missing_managed_catalog_file_is_rebuilt_before_codex_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=EXPOSED,
        ):
            root = Path(directory)
            home = root / "codex"
            runtime, home = self._fixture(root, str(managed_catalog_path(home)))
            catalog = managed_catalog_path(home)
            self.assertFalse(catalog.exists())

            domain = CodexSettingsDomain(runtime, codex_home=home)
            snapshot = domain.snapshot()

            self.assertTrue(catalog.exists())
            self.assertEqual(["default-chat"], self._catalog_models(catalog))
            # The pointer was already correct and a missing file is not a
            # model-set change, so no restart prompt is manufactured.
            self.assertTrue(snapshot["model_catalog"]["enabled"])
            self.assertFalse(snapshot["model_catalog"]["restart_required"])
            self.assertIsNone(snapshot["model_catalog"]["change_reason"])

    def test_dangling_pointer_is_dropped_when_the_router_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=([], False),
        ):
            root = Path(directory)
            home = root / "codex"
            runtime, home = self._fixture(root, str(managed_catalog_path(home)))

            domain = CodexSettingsDomain(runtime, codex_home=home)
            snapshot = domain.snapshot()

            text = (home / "config.toml").read_text(encoding="utf-8")
            self.assertNotIn("model_catalog_json", text)
            self.assertFalse(managed_catalog_path(home).exists())
            self.assertFalse(snapshot["model_catalog"]["enabled"])
            self.assertEqual([], snapshot["model_catalog"]["public_models"])
            self.assertTrue(snapshot["model_catalog"]["restart_required"])
            self.assertEqual("catalog_missing", snapshot["model_catalog"]["change_reason"])

    def test_foreign_catalog_pointer_is_never_touched(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=EXPOSED,
        ):
            root = Path(directory)
            foreign = root / "my-own-catalog.json"
            runtime, home = self._fixture(root, str(foreign))
            before = (home / "config.toml").read_text(encoding="utf-8")

            domain = CodexSettingsDomain(runtime, codex_home=home)
            snapshot = domain.snapshot()

            self.assertEqual(before, (home / "config.toml").read_text(encoding="utf-8"))
            self.assertFalse(foreign.exists())
            self.assertFalse(managed_catalog_path(home).exists())
            self.assertFalse(snapshot["model_catalog"]["enabled"])

    def test_pointer_repair_moves_a_staged_draft_pointer_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=EXPOSED,
        ):
            root = Path(directory)
            home = root / "codex"
            runtime, home = self._fixture(root, str(legacy_managed_catalog_path(home)))
            legacy_managed_catalog_path(home).write_text(
                json.dumps({"models": [{"slug": "default-chat"}]}), encoding="utf-8"
            )
            domain = CodexSettingsDomain(runtime, codex_home=home)
            domain.dispatch("patch", {"model_reasoning_effort": "high"})

            snapshot = domain.snapshot()

            current = managed_catalog_path(home)
            draft_text = domain.export(include_sensitive=True)["config_text"]
            # The staged edit survives, and applying it later cannot restore the
            # retired pointer that Codex can no longer load.
            self.assertIn('model_reasoning_effort = "high"', draft_text)
            self.assertIn(f'model_catalog_json = "{current}"', draft_text)
            self.assertNotIn("litellm-menu-model-catalog.json", draft_text)
            self.assertTrue(snapshot["model_catalog"]["enabled"])


if __name__ == "__main__":
    unittest.main()

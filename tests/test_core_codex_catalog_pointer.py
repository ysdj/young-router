"""Regression tests for the managed Codex model-catalog pointer.

Codex refuses to load its whole configuration while ``model_catalog_json``
names a missing file ("No such file or directory"), so the app must never leave
a managed pointer dangling: it is rebuilt while the router is reachable and
dropped when it is not.  Installations written by earlier releases — the
retired LiteLLM Menu file name and the rebrand-era Young Router one — are
migrated to the current neutral ``model-catalog.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from typing import Any
from unittest import mock

from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.model_catalog import (
    legacy_managed_catalog_path,
    managed_catalog_path,
    previous_managed_catalog_path,
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

    @staticmethod
    def _settings_pin(domain: CodexSettingsDomain) -> str:
        """Return the inheritance pin stored in the runtime settings file."""

        from runtime_settings_io import load_specs, read_settings_file

        path = domain.runtime_settings_path
        if path is None:
            return ""
        values = read_settings_file(path, load_specs())
        return values.get("YOUNG_ROUTER_CATALOG_BASE_PROFILE", "")

    def test_retired_sidecar_pin_moves_into_runtime_settings_and_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=EXPOSED,
        ):
            root = Path(directory)
            home = root / "codex"
            runtime, home = self._fixture(root, str(managed_catalog_path(home)))
            settings = root / "runtime-settings.env"
            # The interim app-root store and the older in-Codex-home file.
            (root / "model-catalog-state.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            str(home.resolve()): {"base_profile": "gpt-6-astra"}
                        }
                    }
                ),
                encoding="utf-8",
            )
            legacy = home / "model-catalog-state.json"
            legacy.write_text(
                json.dumps(
                    {"base_profile": "gpt-6-astra", "enabled": True, "models": ["default-chat"]}
                ),
                encoding="utf-8",
            )

            domain = CodexSettingsDomain(runtime, codex_home=home, runtime_settings_path=settings)

            self.assertEqual("gpt-6-astra", domain._load_catalog_base_profile())
            self.assertEqual("gpt-6-astra", self._settings_pin(domain))
            self.assertFalse(legacy.exists())
            self.assertFalse((root / "model-catalog-state.json").exists())

    def test_orphaned_retired_catalog_left_by_an_older_release_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=EXPOSED,
        ):
            root = Path(directory)
            home = root / "codex"
            runtime, home = self._fixture(root, str(managed_catalog_path(home)))
            # The applied pointer already names the current file; the rebrand-era
            # copy is dead app data from before the rename.
            previous_managed_catalog_path(home).write_text(
                json.dumps({"models": [{"slug": "default-chat"}]}), encoding="utf-8"
            )

            domain = CodexSettingsDomain(runtime, codex_home=home)
            snapshot = domain.snapshot()

            self.assertTrue(snapshot["model_catalog"]["enabled"])
            self.assertTrue(managed_catalog_path(home).exists())
            self.assertFalse(previous_managed_catalog_path(home).exists())

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
            # The retired copy leaves the client directory once the pointer moved.
            self.assertFalse(legacy_managed_catalog_path(home).exists())
            text = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn(f'model_catalog_json = "{current}"', text)
            self.assertNotIn("litellm-menu-model-catalog.json", text)
            self.assertTrue(snapshot["model_catalog"]["enabled"])
            self.assertEqual(["default-chat"], snapshot["model_catalog"]["public_models"])

    def test_rebrand_pointer_is_migrated_to_the_current_catalog_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=EXPOSED,
        ):
            root = Path(directory)
            home = root / "codex"
            runtime, home = self._fixture(root, str(previous_managed_catalog_path(home)))
            # The rebrand-era file is what Codex is reading today.
            previous_managed_catalog_path(home).write_text(
                json.dumps({"models": [{"slug": "default-chat"}]}), encoding="utf-8"
            )

            domain = CodexSettingsDomain(runtime, codex_home=home)
            snapshot = domain.snapshot()

            current = managed_catalog_path(home)
            self.assertTrue(current.exists())
            # The retired copy leaves the client directory once the pointer moved.
            self.assertFalse(previous_managed_catalog_path(home).exists())
            text = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn(f'model_catalog_json = "{current}"', text)
            self.assertNotIn("young-router-model-catalog.json", text)
            self.assertTrue(snapshot["model_catalog"]["enabled"])
            self.assertEqual(["default-chat"], snapshot["model_catalog"]["public_models"])

    def test_client_update_does_not_remap_a_pinned_base_profile(self) -> None:
        """A new flagship generation must not silently re-prompt third-party models."""

        def profile(slug: str, priority: int, prompt: str) -> dict[str, Any]:
            return {
                "slug": slug,
                "priority": priority,
                "visibility": "list",
                "base_instructions": prompt,
                "model_messages": {"instructions_template": prompt},
            }

        astra = profile("gpt-6-astra", 1, "Astra instructions")
        orbit = profile("gpt-7-orbit", 0, "GPT-7 instructions")
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=EXPOSED,
        ):
            root = Path(directory)
            home = root / "codex"
            runtime, home = self._fixture(root, str(managed_catalog_path(home)))
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[astra],
            ):
                domain = CodexSettingsDomain(
                    runtime,
                    codex_home=home,
                    runtime_settings_path=root / "runtime-settings.env",
                )
                domain.snapshot()
            catalog = managed_catalog_path(home)
            pinned_text = catalog.read_text(encoding="utf-8")
            self.assertIn("Astra instructions", pinned_text)
            self.assertEqual("gpt-6-astra", self._settings_pin(domain))

            # The client adds a newer flagship: the pinned base keeps serving
            # the alias, so the file stays exactly as it was.
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[orbit, astra],
            ):
                domain.snapshot()
            self.assertEqual(pinned_text, catalog.read_text(encoding="utf-8"))

            # Once the client drops the pinned profile the mapping is re-resolved.
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[orbit],
            ):
                domain.snapshot()
            refreshed = catalog.read_text(encoding="utf-8")
            self.assertIn("GPT-7 instructions", refreshed)
            self.assertNotIn("Astra instructions", refreshed)
            self.assertEqual("gpt-7-orbit", self._settings_pin(domain))

    def test_unreadable_client_catalog_keeps_the_client_derived_file(self) -> None:
        """Codex updating must not swap its own prompts for the fallback."""

        native_profile = {
            "slug": "default-chat",
            "base_instructions": "Native client instructions",
            "model_messages": {"instructions_template": "Native client instructions"},
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=EXPOSED,
        ):
            root = Path(directory)
            home = root / "codex"
            runtime, home = self._fixture(root, str(managed_catalog_path(home)))
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                domain = CodexSettingsDomain(runtime, codex_home=home)
                domain.snapshot()
            catalog = managed_catalog_path(home)
            before = catalog.read_text(encoding="utf-8")
            self.assertIn("Native client instructions", before)

            # The client is installed but its bundled catalog cannot be read
            # right now (an update in flight): the file stays untouched.
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[],
            ):
                domain.snapshot()
            self.assertEqual(before, catalog.read_text(encoding="utf-8"))

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
            self.assertTrue(current.exists())
            self.assertFalse(legacy_managed_catalog_path(home).exists())
            draft_text = domain.export(include_sensitive=True)["config_text"]
            # The staged edit survives, and applying it later cannot restore the
            # retired pointer that Codex can no longer load.
            self.assertIn('model_reasoning_effort = "high"', draft_text)
            self.assertIn(f'model_catalog_json = "{current}"', draft_text)
            self.assertNotIn("litellm-menu-model-catalog.json", draft_text)
            self.assertTrue(snapshot["model_catalog"]["enabled"])


if __name__ == "__main__":
    unittest.main()

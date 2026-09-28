"""Core contracts for the public-model pane: rename, limits, route order."""

from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest

from young_router.core.domains import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain


PUBLIC_MODEL_CONFIG = """
providers:
  primary:
    api_base: "https://example.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret"
  backup:
    api_base: "https://backup.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret-2"
model_list:
  - model_name: gpt-5.6-sol
    litellm_params:
      model: openai/gpt-5.6-sol
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
      order: 1
    model_info:
      id: "00000001"
      provider: primary
  - model_name: gpt-5.6-sol
    litellm_params:
      model: openai/gpt-5.6-sol
      api_base: "https://backup.test/v1"
      api_key: "replace-me-secret-2"
      order: 1.5
    model_info:
      id: "00000002"
      provider: backup
  - model_name: kimi-k3
    litellm_params:
      model: openai/kimi-k3
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
      order: 2
    model_info:
      id: "00000003"
      provider: primary
"""


class PublicModelDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.yaml"
        self.path.write_text(
            textwrap.dedent(PUBLIC_MODEL_CONFIG).lstrip(), encoding="utf-8"
        )
        self.domain = ProvidersModelsDomain(self.path)

    def models(self) -> list[dict]:
        snapshot = self.domain.snapshot()
        return [
            model
            for provider in snapshot["providers"]
            for model in provider["models"]
        ]

    def model(self, deployment_id: str) -> dict:
        return next(
            model
            for model in self.models()
            if model["deployment_id"] == deployment_id
        )

    def test_public_model_patch_writes_the_context_of_every_group_route(self) -> None:
        snapshot = self.domain.dispatch(
            "public.model_patch",
            {
                "public_model": "gpt-5.6-sol",
                "changes": {"max_input_tokens": 372000},
            },
        )
        routes = [
            model
            for provider in snapshot["providers"]
            for model in provider["models"]
            if model["model_name"] == "gpt-5.6-sol"
        ]
        self.assertEqual(2, len(routes))
        for route in routes:
            self.assertEqual(372000, route["max_input_tokens"])
        self.assertIsNone(self.model("00000003")["max_input_tokens"])

        self.domain.apply()
        saved = self.path.read_text(encoding="utf-8")
        self.assertEqual(2, saved.count("max_input_tokens: 372000"))

        reloaded = ProvidersModelsDomain(self.path)
        self.assertEqual(372000, self.model("00000001")["max_input_tokens"])
        self.assertEqual(372000, reloaded.snapshot()["providers"][0]["models"][0]["max_input_tokens"])

    def test_clearing_the_context_removes_the_key_instead_of_writing_zero(self) -> None:
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": 372000}},
        )
        self.domain.apply()
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": None}},
        )
        self.domain.apply()
        saved = self.path.read_text(encoding="utf-8")
        self.assertNotIn("max_input_tokens", saved)
        self.assertIsNone(self.model("00000001")["max_input_tokens"])

    def test_public_model_rename_rewrites_every_route_of_the_group(self) -> None:
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"name": "sol-fast"}},
        )
        self.domain.apply()
        saved = self.path.read_text(encoding="utf-8")
        self.assertEqual(2, saved.count("model_name: sol-fast"))
        self.assertNotIn("model_name: gpt-5.6-sol", saved)
        # The upstream route stays the provider's own model id.
        self.assertEqual(2, saved.count("model: openai/gpt-5.6-sol"))
        self.assertEqual("kimi-k3", self.model("00000003")["model_name"])

    def test_rename_refuses_a_name_another_public_model_already_uses(self) -> None:
        with self.assertRaises(DomainError):
            self.domain.dispatch(
                "public.model_patch",
                {"public_model": "gpt-5.6-sol", "changes": {"name": "kimi-k3"}},
            )

    def test_public_model_patch_refuses_unknown_changes(self) -> None:
        with self.assertRaises(DomainError):
            self.domain.dispatch(
                "public.model_patch",
                {"public_model": "gpt-5.6-sol", "changes": {"provider": "backup"}},
            )

    def test_a_model_limit_can_be_edited_on_one_route(self) -> None:
        self.domain.dispatch(
            "model.patch",
            {
                "provider_id": "primary",
                "model_id": "00000001",
                "changes": {"max_input_tokens": 200000},
            },
        )
        self.assertEqual(200000, self.model("00000001")["max_input_tokens"])
        self.assertIsNone(self.model("00000002")["max_input_tokens"])

    def test_a_negative_context_is_refused(self) -> None:
        with self.assertRaises(DomainError):
            self.domain.dispatch(
                "public.model_patch",
                {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": -4}},
            )

    def test_reorder_permutes_the_typed_order_values_including_decimals(self) -> None:
        self.domain.dispatch(
            "routes.reorder_group",
            {
                "public_model": "gpt-5.6-sol",
                "route_ids": ["00000002", "00000001"],
            },
        )
        # The two routes trade the values the user typed (1 and 1.5) instead
        # of being renumbered to whole numbers.
        self.assertEqual(1, self.model("00000002")["manual_order"])
        self.assertEqual(1.5, self.model("00000001")["manual_order"])

    def test_snapshot_carries_the_registry_default_for_each_public_model(self) -> None:
        contexts = self.domain.snapshot()["model_contexts"]
        self.assertEqual(272000, contexts["gpt-5.6-sol"]["context_window"])
        self.assertEqual(262144, contexts["kimi-k3"]["context_window"])

    def test_the_draft_custom_limit_never_masks_the_registry_default(self) -> None:
        self.domain.dispatch(
            "public.model_patch",
            {"public_model": "gpt-5.6-sol", "changes": {"max_input_tokens": 372000}},
        )
        # The pane reads the custom value from its model rows and keeps the
        # registry default beside it as the fallback hint.
        self.assertEqual(
            272000,
            self.domain.snapshot()["model_contexts"]["gpt-5.6-sol"]["context_window"],
        )


if __name__ == "__main__":
    unittest.main()

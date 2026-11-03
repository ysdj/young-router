"""One route's concurrency cap through the staged configuration.

The cap is the route's own, and an absent one is not zero: absent follows the
runtime's global limit, zero states that this route is unlimited.  Both readings
have to survive a save and a reload, because a document that folded one into the
other would silently start or stop capping a route the user never touched.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest

from young_router.core.domains import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain


ROUTE_CONFIG = """
providers:
  primary:
    api_base: "https://example.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret"
model_list:
  - model_name: sol
    litellm_params:
      model: openai/sol
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
      order: 1
    model_info:
      id: "00000001"
      provider: primary
  - model_name: sol
    litellm_params:
      model: openai/sol-backup
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
      order: 2
    model_info:
      id: "00000002"
      provider: primary
"""


class RouteConcurrencyConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.yaml"
        self.path.write_text(textwrap.dedent(ROUTE_CONFIG).lstrip(), encoding="utf-8")
        self.domain = ProvidersModelsDomain(self.path)

    def model(self, deployment_id: str) -> dict:
        return next(
            model
            for provider in self.domain.snapshot()["providers"]
            for model in provider["models"]
            if model["deployment_id"] == deployment_id
        )

    def patch(self, deployment_id: str, max_concurrency: object) -> None:
        self.domain.dispatch(
            "model.patch",
            {
                "provider_id": "primary",
                "model_id": deployment_id,
                "changes": {"max_concurrency": max_concurrency},
            },
        )

    def test_an_unstated_cap_is_inherited_not_zero(self) -> None:
        # ``None`` is what the pane draws as "follows the global limit".  A zero
        # here would make the saved route permanently ignore that setting.
        self.assertIsNone(self.model("00000001")["max_concurrency"])
        saved = self.path.read_text(encoding="utf-8")
        self.assertNotIn("max_concurrency", saved)

    def test_a_stated_cap_survives_a_save_and_a_reload(self) -> None:
        self.patch("00000001", 3)
        self.domain.apply()

        saved = self.path.read_text(encoding="utf-8")
        self.assertEqual(1, saved.count("max_concurrency: 3"))

        reloaded = ProvidersModelsDomain(self.path)
        route = next(
            model
            for provider in reloaded.snapshot()["providers"]
            for model in provider["models"]
            if model["deployment_id"] == "00000001"
        )
        self.assertEqual(3, route["max_concurrency"])
        # The peer states nothing and keeps following the global limit.
        peer = next(
            model
            for provider in reloaded.snapshot()["providers"]
            for model in provider["models"]
            if model["deployment_id"] == "00000002"
        )
        self.assertIsNone(peer["max_concurrency"])

    def test_zero_is_written_because_unlimited_is_a_choice(self) -> None:
        self.patch("00000001", 0)
        self.domain.apply()

        saved = self.path.read_text(encoding="utf-8")
        self.assertEqual(1, saved.count("max_concurrency: 0"))

        reloaded = ProvidersModelsDomain(self.path)
        route = next(
            model
            for provider in reloaded.snapshot()["providers"]
            for model in provider["models"]
            if model["deployment_id"] == "00000001"
        )
        self.assertEqual(0, route["max_concurrency"])

    def test_clearing_the_field_returns_the_route_to_the_global_limit(self) -> None:
        self.patch("00000001", 4)
        self.domain.apply()
        self.patch("00000001", None)
        self.domain.apply()

        saved = self.path.read_text(encoding="utf-8")
        self.assertNotIn("max_concurrency", saved)
        self.assertIsNone(self.model("00000001")["max_concurrency"])

    def test_a_negative_or_fractional_cap_is_refused(self) -> None:
        for invalid in (-1, 1.5, "many"):
            with self.subTest(value=invalid):
                with self.assertRaises(DomainError):
                    self.patch("00000001", invalid)

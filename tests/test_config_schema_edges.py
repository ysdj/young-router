"""Document shapes ``config.schema`` accepts and the ones it must refuse."""

from __future__ import annotations

import unittest
from pathlib import Path

from young_router.config import schema


CONFIG = Path("config.yaml")


class RouteConcurrencyNullTests(unittest.TestCase):
    def test_a_route_that_states_no_concurrency_still_loads(self) -> None:
        text = (
            "model_list:\n"
            "  - model_name: chat\n"
            "    model_info:\n"
            "      max_concurrency:\n"
        )

        loaded = schema.load_yaml_text(text, CONFIG)

        self.assertIsNone(loaded["model_list"][0]["model_info"]["max_concurrency"])


class ProvidersSectionShapeTests(unittest.TestCase):
    def test_a_providers_section_that_is_not_a_mapping_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "providers must be an object"):
            schema.load_yaml_text("providers: []\nmodel_list: []\n", CONFIG)

    def test_a_provider_entry_that_is_not_a_mapping_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider primary must be an object"):
            schema.load_yaml_text(
                'providers:\n  primary: "text"\nmodel_list: []\n', CONFIG
            )

from __future__ import annotations

import copy
import unittest
from unittest import mock

from young_router.core.model_catalog import catalog_payload
from young_router.core import model_catalog as catalog_module


def _native_profiles() -> list[dict[str, object]]:
    return [
        {
            "slug": f"native-{index}",
            "priority": index,
            "visibility": "list",
            "base_instructions": f"Prompt {index}",
        }
        for index in range(4)
    ]


class ModelCatalogEdgeTests(unittest.TestCase):
    def test_catalog_build_copies_only_profiles_it_returns(self) -> None:
        registry = mock.Mock()
        registry.record_for.return_value = mock.Mock(context_window=0)
        registry.search_tool_capability_for.return_value = None
        registry.route_model_ids.side_effect = lambda name: [name]
        registry.states_image_generation.return_value = False
        registry.reasoning_for.return_value = None
        registry.reasoning_for_model_id.return_value = None
        original = copy.deepcopy
        copied: list[str] = []

        def counter(value: object, memo: object = None) -> object:
            if isinstance(value, dict) and isinstance(value.get("slug"), str):
                copied.append(value["slug"])
            return original(value, memo)

        with mock.patch.object(catalog_module.copy, "deepcopy", side_effect=counter):
            payload = catalog_payload(
                ["route-a", "route-b"], registry=registry, native_models=_native_profiles()
            )
        self.assertEqual(2, len(payload["models"]))
        self.assertLessEqual(len(copied), 3)

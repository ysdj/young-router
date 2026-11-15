from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from young_router.core.model_contexts import (
    MODEL_CONTEXT_SOURCES,
    ModelContextRegistry,
    _record_from_payload,
)


class ModelContextRefreshCacheTests(unittest.TestCase):
    def test_a_locked_refresh_loads_the_cache_it_merges_into(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "contexts.json"
            record = {
                "context_window": 400_000, "max_context_window": 400_000,
                "effective_context_window_percent": 95,
                "source": MODEL_CONTEXT_SOURCES[0], "priority": 20,
            }
            cache.write_text(
                json.dumps({"fetched_at": 1_000_000.0, "records": {"vendor/kept-model": record}}),
                encoding="utf-8",
            )
            def fetch(source: str) -> object:
                if source == MODEL_CONTEXT_SOURCES[2]:
                    return {"vendor/fresh-model": {"limit": {"context": 100_000}}}
                return {}
            registry = ModelContextRegistry(
                cache_path=cache, refresh_enabled=True, fetcher=fetch,
                clock=lambda: 1_000_000.0,
            )
            self.assertFalse(registry.refresh_if_due(sync_cache=False))
            self.assertEqual(400_000, registry.record_for("vendor/kept-model").context_window)


class ImageContractRoundTripTests(unittest.TestCase):
    def test_stated_input_limits_survive_a_cache_round_trip(self) -> None:
        stored = _record_from_payload(
            {
                "id": "vendor/image-cog",
                "type": "image",
                "limit": {"context": 0},
                "inputLimits": {"width": 4096, "height": 4096},
            },
            source="catalog",
            priority=20,
        )
        limits = {"width": 4096, "height": 4096}
        self.assertEqual(limits, stored["image_model_metadata"]["input_limits"])
        # The cache loader re-parses every entry, so a reload must return the
        # contract the normalizer wrote instead of dropping what it stated.
        reloaded = _record_from_payload(stored, source="cache", priority=1)
        self.assertEqual(limits, reloaded["image_model_metadata"]["input_limits"])


class MixedGroupStatedWindowTests(unittest.TestCase):
    def test_a_reading_does_not_raise_a_route_resolved_below_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "contexts.json"
            profile = {"context_window": 128_000, "max_context_window": 128_000, "priority": 40}
            cache.write_text(json.dumps({
                "fetched_at": 1_000_000.0,
                "records": {"openai/vendor-a/small-model-zz": profile},
                "stated_windows": {"vendor-b/fresh-model-zz": 1_000_000},
            }), encoding="utf-8")
            (root / "config.yaml").write_text(
                "model_list:\n"
                "  - model_name: mixed-group\n"
                "    litellm_params:\n"
                "      model: openai/vendor-a/small-model-zz\n"
                "  - model_name: mixed-group\n"
                "    litellm_params:\n"
                "      model: openai/vendor-b/fresh-model-zz\n",
                encoding="utf-8",
            )
            registry = ModelContextRegistry(
                runtime_config_path=root / "config.yaml", cache_path=cache, refresh_enabled=False
            )
            with mock.patch.object(ModelContextRegistry, "_resolve_provider_record", return_value=None):
                self.assertEqual(128_000, registry.default_record_for("mixed-group").context_window)

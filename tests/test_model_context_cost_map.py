"""The cost map is read from LiteLLM's package, not by importing LiteLLM.

Core asks that table two questions -- the context window of a model no profile
knows (``_litellm_record``) and whether a name is an image generator
(``litellm_mode``).  It used to answer them by importing all of LiteLLM, which
measured 2.7 s and a 244 MB resident peak the first time a window drew a route
the public catalogs did not cover.

What these tests pin is that the answers did not change with the source: the
file's rows are the rows ``litellm.model_cost`` holds, the readers work with
LiteLLM made unimportable, and an install whose map LiteLLM would fetch from the
network still goes through LiteLLM instead of guessing from a stale file.
"""

from __future__ import annotations

import builtins
import os
import sys
import unittest
from unittest import mock

from young_router.core import model_contexts


class CostMapSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        # The cache is module state so that a warm Core never re-reads 3 MB per
        # lookup; a test that changes where the map comes from clears it.
        self._saved = model_contexts._packaged_cost_map
        model_contexts._packaged_cost_map = False
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        model_contexts._packaged_cost_map = self._saved

    def _local_map(self) -> dict[str, object]:
        with mock.patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "true"}):
            model_contexts._packaged_cost_map = False
            cost_map = model_contexts._cost_map()
        self.assertIsNotNone(cost_map, "the installed LiteLLM ships no readable cost map")
        return cost_map

    def test_the_file_answers_what_litellm_would_answer(self) -> None:
        """Every row of ``litellm.model_cost`` agrees with the file, field by field."""

        try:
            import litellm
        except Exception as error:  # pragma: no cover - a host without LiteLLM
            self.skipTest(f"LiteLLM is not importable here: {error}")
        cost_map = self._local_map()
        litellm_map = getattr(litellm, "model_cost", None)
        self.assertIsInstance(litellm_map, dict)
        fields = ("max_input_tokens", "max_tokens", "mode")
        mismatched = []
        for model_id, row in litellm_map.items():
            if not isinstance(row, dict):
                continue
            local = cost_map.get(model_id)
            for field in fields:
                if not isinstance(local, dict) or local.get(field) != row.get(field):
                    mismatched.append((model_id, field))
                    break
        self.assertEqual([], mismatched[:5], f"{len(mismatched)} rows disagree with the file")

    def test_the_readers_answer_with_litellm_unimportable(self) -> None:
        """Core can answer both questions on a host where importing LiteLLM fails."""

        with mock.patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "true"}):
            model_contexts._packaged_cost_map = False
            with mock.patch.dict(sys.modules, {"litellm": None}):
                record = model_contexts._litellm_record(["gpt-4o-mini"])
                mode = model_contexts.litellm_mode(["gpt-image-1"])
        self.assertIsNotNone(record, "the file states a window for gpt-4o-mini")
        self.assertEqual("litellm", record.get("source"))
        self.assertEqual("image_generation", mode)

    def test_a_loaded_litellm_map_is_the_live_one(self) -> None:
        """A runtime already in this process is what the answers come from."""

        fake_litellm = mock.Mock()
        fake_litellm.model_cost = {"model-under-test": {"max_input_tokens": 128_000, "mode": "chat"}}
        with mock.patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "true"}):
            model_contexts._packaged_cost_map = False
            with mock.patch.dict(sys.modules, {"litellm": fake_litellm}):
                record = model_contexts._litellm_record(["model-under-test"])
        self.assertEqual(128_000, record["context_window"])
        self.assertEqual("litellm", record["source"])

    def test_an_unpinned_install_still_imports_the_runtime(self) -> None:
        """Nothing local to read and nothing loaded: LiteLLM is the source."""

        fake_litellm = mock.Mock()
        fake_litellm.model_cost = {"model-under-test": {"max_input_tokens": 256_000}}
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "litellm":
                return fake_litellm
            return real_import(name, *args, **kwargs)

        with mock.patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "", "LITELLM_OFFLINE_MODE": ""}), \
                mock.patch.dict(sys.modules), \
                mock.patch("builtins.__import__", side_effect=fake_import):
            sys.modules.pop("litellm", None)
            model_contexts._packaged_cost_map = False
            record = model_contexts._litellm_record(["model-under-test"])
        self.assertEqual(256_000, record["context_window"])


if __name__ == "__main__":
    unittest.main()

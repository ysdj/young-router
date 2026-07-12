from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path


class SearchEndpointTests(unittest.TestCase):
    @staticmethod
    def _module():
        # Import lazily so unittest discovery does not preload the real LiteLLM
        # web-search classes before the callback tests install their stubs.
        return importlib.import_module("litellm_menu.search_endpoint")

    def test_commands_to_actions_supports_queries_literal_urls_and_refs(self) -> None:
        search_endpoint = self._module()
        session = search_endpoint._SearchSession(
            references={"turn0search0": "https://example.test/source"}
        )
        actions, errors = search_endpoint.commands_to_actions(
            {
                "search_query": [{"q": "synthetic query"}],
                "open": [{"ref_id": "https://example.test/direct"}],
                "find": [{"ref_id": "turn0search0", "pattern": "synthetic"}],
            },
            session,
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            actions,
            [
                {"type": "search", "query": "synthetic query"},
                {"type": "openPage", "url": "https://example.test/direct"},
                {
                    "type": "findInPage",
                    "url": "https://example.test/source",
                    "pattern": "synthetic",
                },
            ],
        )

    def test_execute_search_payload_returns_codex_search_response(self) -> None:
        search_endpoint = self._module()
        original = search_endpoint._bridge._external_web_search_run_actions

        async def fake_run_actions(actions, _cache, _tasks):
            return (
                "Title: Synthetic source\nURL: https://example.test/source\nSnippet: synthetic",
                ["https://example.test/source"],
                [["https://example.test/source"]],
                actions,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            previous = os.environ.get("LITELLM_MENU_SEARCH_STATE_FILE")
            os.environ["LITELLM_MENU_SEARCH_STATE_FILE"] = str(
                Path(temp_dir) / "search-state.json"
            )
            search_endpoint._bridge._external_web_search_run_actions = fake_run_actions
            try:
                response = asyncio.run(
                    search_endpoint.execute_search_payload(
                        {
                            "id": "tco_synthetic",
                            "commands": {"search_query": [{"q": "synthetic query"}]},
                        }
                    )
                )
            finally:
                search_endpoint._bridge._external_web_search_run_actions = original
                if previous is None:
                    os.environ.pop("LITELLM_MENU_SEARCH_STATE_FILE", None)
                else:
                    os.environ["LITELLM_MENU_SEARCH_STATE_FILE"] = previous
        self.assertIn("output", response)
        self.assertRegex(response["output"], r"Reference: turn\d+search0")

    def test_url_reference_ids_are_stable_across_sessions(self) -> None:
        search_endpoint = self._module()
        url = "https://example.test/stable"
        first = search_endpoint._SearchSession()
        second = search_endpoint._SearchSession()
        result = f"Title: Stable source\nURL: {url}\nSnippet: synthetic"

        with tempfile.TemporaryDirectory() as temp_dir:
            previous = os.environ.get("LITELLM_MENU_SEARCH_STATE_FILE")
            os.environ["LITELLM_MENU_SEARCH_STATE_FILE"] = str(
                Path(temp_dir) / "search-state.json"
            )
            try:
                search_endpoint._remember_search_sources(first, [[url]], result)
                search_endpoint._remember_search_sources(second, [[url]], result)
                self.assertEqual(first.references, second.references)
                ref_id = next(iter(first.references))
                self.assertRegex(ref_id, r"^turn\d+search0$")

                # Simulate a different Gunicorn worker with no process-local
                # session cache; resolution must come from the shared registry.
                third = search_endpoint._SearchSession()
                actions, errors = search_endpoint.commands_to_actions(
                    {"open": [{"ref_id": ref_id}]},
                    third,
                )
            finally:
                if previous is None:
                    os.environ.pop("LITELLM_MENU_SEARCH_STATE_FILE", None)
                else:
                    os.environ["LITELLM_MENU_SEARCH_STATE_FILE"] = previous

        self.assertEqual(errors, [])
        self.assertEqual(actions, [{"type": "openPage", "url": url}])


if __name__ == "__main__":
    unittest.main()

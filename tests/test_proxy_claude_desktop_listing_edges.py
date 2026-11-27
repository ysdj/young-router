"""Edges of the one window a discovery row states.

Every id this view publishes states the window the app publishes for the
route; none of them echoes the number its listing advertised, including a
Claude-shaped id the client keeps as written.
"""

from __future__ import annotations

import unittest
from unittest import mock

from young_router.proxy import claude_desktop_listing


def _response(model_id: str, advertised: object) -> dict[str, object]:
    """One canonical listing row, carrying the number its upstream advertises."""

    return {
        "data": [
            {
                "type": "model",
                "id": model_id,
                "display_name": model_id,
                "created_at": "2023-02-28T18:56:42Z",
                "max_input_tokens": advertised,
                "max_tokens": 8192,
            }
        ],
        "has_more": False,
        "first_id": model_id,
        "last_id": model_id,
    }


class PublishedContextTests(unittest.TestCase):
    def test_a_claude_shaped_id_states_the_resolved_window(self) -> None:
        # A Claude-shaped route keeps its own row, so this resolver has no row
        # of its own to echo; the published window is the only number it states.
        self.assertEqual(
            200_000,
            claude_desktop_listing._published_context(
                "claude-opus-5-5", lambda name: 200_000
            ),
        )

    def test_a_claude_shaped_id_at_one_million_is_clamped(self) -> None:
        self.assertEqual(
            1_000_000,
            claude_desktop_listing._published_context(
                "claude-opus-5-5", lambda name: 1_048_576
            ),
        )

    def test_a_claude_shaped_id_without_a_published_window_states_nothing(self) -> None:
        self.assertIsNone(
            claude_desktop_listing._published_context("claude-opus-5-5", lambda name: None)
        )


class KeptRowTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            claude_desktop_listing, "_route_is_generation", return_value=False
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_a_claude_shaped_row_is_suffixed_from_the_published_window(self) -> None:
        # Core's Code picker names the launch id from the window the registry
        # publishes; a row reading only the canonical listing publishes the bare
        # id that launch never asks for.
        rows = claude_desktop_listing.desktop_model_rows(
            _response("claude-opus-5-5", 200_000),
            public_window=lambda name: 1_000_000,
        )["data"]
        self.assertEqual("claude-opus-5-5[1m]", rows[0]["id"])
        self.assertEqual(1_000_000, rows[0]["max_input_tokens"])

    def test_a_claude_shaped_row_states_the_window_the_route_publishes(self) -> None:
        # A row stating 1M for a route published below it is the second picker
        # entry the suffix exists to avoid.
        rows = claude_desktop_listing.desktop_model_rows(
            _response("claude-opus-5-5", 1_048_576),
            public_window=lambda name: 200_000,
        )["data"]
        self.assertEqual("claude-opus-5-5", rows[0]["id"])
        self.assertEqual(200_000, rows[0]["max_input_tokens"])


if __name__ == "__main__":
    unittest.main()


class ClaudeCodeViewTests(unittest.TestCase):
    def test_the_code_view_keeps_litellm_aliases_without_a_second_projection(self) -> None:
        # LiteLLM publishes Claude Code's compatibility ids through
        # ``listed_ids``.  This app's picker metadata belongs to the canonical
        # listing the plugin reads; adding it to an already-aliased row made
        # ``... [1m][1m]`` and named the compatibility id instead of the route.
        from litellm.llms.anthropic import common_utils

        claude_desktop_listing.install_patch()
        build = common_utils.create_anthropic_model_list_response
        models = [{
            "id": "deepseek-v4.1-flash",
            "max_input_tokens": 1_000_000,
            "max_output_tokens": 393_216,
        }]
        listed = {"deepseek-v4.1-flash": "claude-router-test[1m]"}
        with mock.patch.object(claude_desktop_listing, "_public_context_window", return_value=1_000_000):
            token = claude_desktop_listing._CODE_VIEW_ACTIVE.set(True)
            try:
                rows = build(models, {"deepseek-v4.1-flash": "DeepSeek"}, listed)["data"]
            finally:
                claude_desktop_listing._CODE_VIEW_ACTIVE.reset(token)
            self.assertEqual("claude-router-test[1m]", rows[0]["id"])
            self.assertEqual("deepseek-v4.1-flash", rows[0]["source_model"])
            self.assertNotIn("claude_alias", rows[0])
            self.assertNotIn("behaves_as", rows[0])

            generic = build(models, {"deepseek-v4.1-flash": "DeepSeek"}, {})["data"][0]
            self.assertEqual("claude-mythos-eb1394[1m]", generic["claude_alias"])
            self.assertEqual("claude-opus-5-5", generic["behaves_as"])

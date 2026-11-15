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
            claude_desktop_listing, "_route_is_image", return_value=False
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

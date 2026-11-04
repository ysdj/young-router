"""Claude Desktop's own /v1/models view.

The desktop app's discovery request is the one client that needs the alias
rows; every other caller keeps the canonical public names.  These tests pin
both halves: the source check and the rows it publishes.
"""

from __future__ import annotations

import unittest
from unittest import mock

from young_router.claude_aliases import claude_desktop_alias
from young_router.proxy import claude_desktop_listing


DESKTOP_HEADERS = {
    "anthropic-version": "2023-06-01",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Claude/4.16336.0 Chrome/144.0.0.0 Safari/537.36 "
        "Electron/44.9.3"
    ),
}


def _response(*ids: str) -> dict[str, object]:
    rows = [
        {
            "type": "model",
            "id": model_id,
            "display_name": model_id,
            "created_at": "2023-02-28T18:56:42Z",
            "max_input_tokens": 1_000_000 if "1m" in model_id else None,
            "max_tokens": 8192,
        }
        for model_id in ids
    ]
    return {
        "data": rows,
        "has_more": False,
        "first_id": rows[0]["id"] if rows else None,
        "last_id": rows[-1]["id"] if rows else None,
    }


class ClaudeDesktopSourceTests(unittest.TestCase):
    def test_electron_discovery_is_the_desktop_app(self) -> None:
        self.assertTrue(claude_desktop_listing.is_claude_desktop_client(DESKTOP_HEADERS))

    def test_the_explicit_header_is_enough(self) -> None:
        self.assertTrue(
            claude_desktop_listing.is_claude_desktop_client(
                {"x-young-router-client": "claude-desktop"}
            )
        )

    def test_claude_code_is_not_the_desktop_app(self) -> None:
        for agent in ("claude-code/2.1.309", "claude-cli/2.1.309"):
            with self.subTest(agent=agent):
                headers = {**DESKTOP_HEADERS, "user-agent": agent}
                self.assertFalse(claude_desktop_listing.is_claude_desktop_client(headers))

    def test_a_non_anthropic_or_non_electron_caller_is_not_the_desktop_app(self) -> None:
        self.assertFalse(
            claude_desktop_listing.is_claude_desktop_client(
                {"user-agent": DESKTOP_HEADERS["user-agent"]}
            )
        )
        self.assertFalse(
            claude_desktop_listing.is_claude_desktop_client(
                {"anthropic-version": "2023-06-01", "user-agent": "curl/8.0"}
            )
        )
        self.assertFalse(claude_desktop_listing.is_claude_desktop_client({}))


class ClaudeDesktopMiddlewareTests(unittest.TestCase):
    def _run(self, *, path: str, headers: dict[str, str]) -> bool:
        seen: list[bool] = []

        async def app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
            seen.append(claude_desktop_listing._VIEW_ACTIVE.get())

        middleware = claude_desktop_listing._DesktopListingMiddleware(app)
        scope = {
            "type": "http",
            "path": path,
            "headers": [
                (name.encode("latin-1"), value.encode("latin-1"))
                for name, value in headers.items()
            ],
        }

        import asyncio

        asyncio.run(middleware(scope, None, None))
        return seen[0]

    def test_the_desktop_request_is_marked_for_the_builder(self) -> None:
        self.assertTrue(self._run(path="/v1/models", headers=DESKTOP_HEADERS))

    def test_another_path_is_not_marked(self) -> None:
        self.assertFalse(self._run(path="/v1/messages", headers=DESKTOP_HEADERS))

    def test_another_client_is_not_marked(self) -> None:
        self.assertFalse(
            self._run(path="/v1/models", headers={**DESKTOP_HEADERS, "user-agent": "curl/8.0"})
        )


class ClaudeDesktopRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            claude_desktop_listing,
            "_route_is_image",
            side_effect=lambda name: name.startswith("gpt-image-"),
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_a_third_party_route_is_published_under_its_alias(self) -> None:
        rows = claude_desktop_listing.desktop_model_rows(
            _response("deepseek-v4.1-flash")
        )["data"]
        self.assertEqual(
            [
                {
                    "type": "model",
                    "id": claude_desktop_alias("deepseek-v4.1-flash"),
                    "display_name": "deepseek-v4.1-flash",
                    "created_at": "2023-02-28T18:56:42Z",
                    "max_input_tokens": None,
                    "max_tokens": 8192,
                }
            ],
            rows,
        )
        self.assertTrue(rows[0]["id"].startswith("claude-mythos-"))

    def test_a_one_million_route_keeps_its_context_window(self) -> None:
        # The app reads max_input_tokens to know a route is 1M; dropping it made
        # every alias assume 200k.
        response = _response("gemini-3.8-flash")
        response["data"][0]["max_input_tokens"] = 1_048_576
        rows = claude_desktop_listing.desktop_model_rows(response)["data"]
        self.assertEqual(1_048_576, rows[0]["max_input_tokens"])

    def test_a_claude_shaped_route_keeps_its_own_id(self) -> None:
        rows = claude_desktop_listing.desktop_model_rows(_response("claude-opus-5-5"))["data"]
        self.assertEqual("claude-opus-5-5", rows[0]["id"])
        self.assertEqual("claude-opus-5-5", rows[0]["display_name"])

    def test_an_image_generator_is_never_listed(self) -> None:
        rows = claude_desktop_listing.desktop_model_rows(
            _response("gpt-image-2.5-flare", "deepseek-v4.1-flash")
        )["data"]
        self.assertEqual(["deepseek-v4.1-flash"], [row["display_name"] for row in rows])
        self.assertFalse(any("image" in str(row["id"]) for row in rows))

    def test_the_view_is_sorted_by_the_display_name(self) -> None:
        rows = claude_desktop_listing.desktop_model_rows(
            _response("grok-4.7", "claude-opus-5-5", "deepseek-v4.1-flash")
        )["data"]
        self.assertEqual(
            ["claude-opus-5-5", "deepseek-v4.1-flash", "grok-4.7"],
            [row["display_name"] for row in rows],
        )

    def test_first_and_last_follow_the_rows(self) -> None:
        response = claude_desktop_listing.desktop_model_rows(_response("grok-4.7"))
        self.assertEqual(response["data"][0]["id"], response["first_id"])
        self.assertEqual(response["data"][-1]["id"], response["last_id"])


class ClaudeDesktopListingPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        from litellm.llms.anthropic import common_utils

        self.module = common_utils
        claude_desktop_listing.install_patch()

    def tearDown(self) -> None:
        claude_desktop_listing._VIEW_ACTIVE.set(False)

    def test_the_canonical_listing_is_unchanged_without_the_marker(self) -> None:
        rows = self.module.create_anthropic_model_list_response(
            [{"id": "deepseek-v4.1-flash", "object": "model"}]
        )["data"]
        self.assertEqual(["deepseek-v4.1-flash"], [row["id"] for row in rows])

    def test_the_marker_publishes_the_desktop_view(self) -> None:
        token = claude_desktop_listing._VIEW_ACTIVE.set(True)
        try:
            with mock.patch.object(
                claude_desktop_listing, "_route_is_image", return_value=False
            ):
                rows = self.module.create_anthropic_model_list_response(
                    [{"id": "deepseek-v4.1-flash", "object": "model"}]
                )["data"]
        finally:
            claude_desktop_listing._VIEW_ACTIVE.reset(token)
        self.assertEqual(
            [claude_desktop_alias("deepseek-v4.1-flash")], [row["id"] for row in rows]
        )
        self.assertEqual(["deepseek-v4.1-flash"], [row["display_name"] for row in rows])


if __name__ == "__main__":
    unittest.main()

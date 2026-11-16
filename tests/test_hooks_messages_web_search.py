from __future__ import annotations

from unittest import mock

from hook_test_utils import *


DECLARATION = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}
ROWS = [
    {
        "type": "text_result",
        "domain": "ruby-lang.org",
        "ref_id": "turn1search0",
        "title": "Ruby 3.4 Released",
        "url": "https://www.ruby-lang.org/en/news/2024/12/25/ruby-3-4-0-released/",
        "snippet": "Ruby 3.4.0 has been released.",
    }
]


def side_query_request(**overrides) -> dict:
    request = {
        "model": "deepseek-v4-pro",
        "max_tokens": 32000,
        "stream": True,
        "system": "You are an assistant for performing a web search tool use",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Perform a web search for the query: ruby 3.4"}
                ],
            }
        ],
        "tools": [dict(DECLARATION)],
    }
    request.update(overrides)
    return request


class HookMessagesWebSearchTests(HookTestCase):
    """The pre-call hook is what turns a search declaration into a search."""

    def _patch_search(self, hooks, rows):
        return (
            mock.patch.object(
                hooks._messages_web_search_module._search_endpoint_module,
                "search_pages",
                rows,
            ),
            mock.patch.object(
                hooks._messages_web_search_module,
                "_served_by_a_claude_route",
                lambda model: False,
            ),
        )

    async def _prepared(self, hooks, request, rows):
        search_patch, route_patch = self._patch_search(hooks, rows)
        with search_patch, route_patch:
            return await hooks.YoungRouterHook().async_pre_call_hook(
                {}, None, request, "anthropic_messages"
            )

    async def test_the_side_query_is_searched_before_the_model_is_asked(self) -> None:
        hooks, _ = load_hook_module()
        searched: list[tuple[str, list[str]]] = []

        async def rows(model, queries, question=None):
            searched.append((model, list(queries)))
            return list(ROWS)

        modified = await self._prepared(hooks, side_query_request(), rows)

        self.assertEqual([("deepseek-v4-pro", ["ruby 3.4"])], searched)
        assert modified is not None
        self.assertNotIn("tools", modified)
        blocks = hooks._messages_web_search_module.marked_blocks(modified)
        self.assertEqual(
            ["server_tool_use", "web_search_tool_result"], [b["type"] for b in blocks]
        )
        self.assertEqual("ruby 3.4", blocks[0]["input"]["query"])
        content = modified["messages"][0]["content"]
        self.assertEqual(1, len(content))
        self.assertIn("Ruby 3.4 Released", content[0]["text"])
        # The original body keeps its declaration: the hook returns a copy and
        # a failed pass must not have edited the request in place.
        self.assertEqual([dict(DECLARATION)], side_query_request()["tools"])

    async def test_a_body_without_the_declaration_is_returned_unchanged(self) -> None:
        hooks, _ = load_hook_module()
        request = side_query_request(tools=[{"name": "Bash", "input_schema": {"type": "object"}}])

        async def rows(model, queries, question=None):
            raise AssertionError("a body with no declaration must not search")

        search_patch, route_patch = self._patch_search(hooks, rows)
        with search_patch, route_patch:
            modified = await hooks.YoungRouterHook().async_pre_call_hook(
                {}, None, request, "anthropic_messages"
            )

        self.assertIsNone(modified)

    async def test_a_responses_body_is_not_a_messages_search(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "model": "deepseek-v4-pro",
            "input": "Perform a web search for the query: ruby 3.4",
            "stream": True,
            "tools": [dict(DECLARATION)],
        }

        async def rows(model, queries, question=None):
            raise AssertionError("the Messages pass must not run on a Responses body")

        search_patch, route_patch = self._patch_search(hooks, rows)
        with search_patch, route_patch:
            await hooks.YoungRouterHook().async_pre_call_hook(
                {}, None, request, "aresponses"
            )

    async def test_the_marker_never_reaches_the_provider(self) -> None:
        hooks, _ = load_hook_module()

        async def rows(model, queries, question=None):
            return list(ROWS)

        modified = await self._prepared(hooks, side_query_request(), rows)
        assert modified is not None

        deployment = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            dict(modified, api_key="k", api_base="https://api.deepseek.com/v1"),
            call_type="anthropic_messages",
        )

        assert deployment is not None
        self.assertNotIn(hooks._messages_web_search_module.MARKER_KEY, deployment)


if __name__ == "__main__":
    unittest.main()

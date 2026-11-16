from __future__ import annotations

import unittest
from unittest import mock

from young_router.proxy import messages_web_search as search

ROWS = [
    {
        "type": "text_result",
        "domain": "ruby-lang.org",
        "ref_id": "turn1search0",
        "title": "Ruby 3.4 Released",
        "url": "https://www.ruby-lang.org/en/news/2024/12/25/ruby-3-4-0-released/",
        "snippet": "Ruby 3.4.0 has been released.",
    },
    {
        "type": "text_result",
        "domain": "github.com",
        "ref_id": "turn1search1",
        "title": "ruby/ruby",
        "url": "https://github.com/ruby/ruby",
        "snippet": "Ruby's source.",
    },
]

DECLARATION = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 8,
}


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
                    {"type": "text", "text": "Perform a web search for the query: ruby 3.4 release"}
                ],
            }
        ],
        "tools": [dict(DECLARATION)],
    }
    request.update(overrides)
    return request


class DeclarationTests(unittest.TestCase):
    def test_only_anthropic_dated_declarations_are_taken(self) -> None:
        tools = [
            dict(DECLARATION),
            {"type": "web_search_20260209", "name": "web_search"},
            {"type": "web_search_preview"},
            {"type": "openrouter:web_search"},
            {"type": "custom", "name": "web_search", "input_schema": {"type": "object"}},
        ]

        self.assertEqual(
            ["web_search_20250305", "web_search_20260209"],
            [tool["type"] for tool in search.native_declarations(tools)],
        )

    def test_a_function_named_web_search_is_not_a_declaration(self) -> None:
        # The newer Claude Code shape is a client tool the CLI executes itself;
        # the search it spawns is the side request this module reads.
        tools = [{"name": "WebSearch", "input_schema": {"type": "object"}}]

        self.assertEqual([], search.native_declarations(tools))


class QueryTests(unittest.TestCase):
    def test_the_query_comes_from_the_clients_own_sentence(self) -> None:
        request = side_query_request()

        self.assertEqual("ruby 3.4 release", search.query_from_messages(request["messages"]))

    def test_a_plain_string_message_is_read(self) -> None:
        messages = [{"role": "user", "content": "Perform a web search for the query: 苏州 天气"}]

        self.assertEqual("苏州 天气", search.query_from_messages(messages))

    def test_a_main_loop_turn_states_no_query(self) -> None:
        messages = [{"role": "user", "content": "What changed in Ruby 3.4?"}]

        self.assertIsNone(search.query_from_messages(messages))

    def test_the_query_stops_at_the_backends_own_bound(self) -> None:
        messages = [
            {"role": "user", "content": "Perform a web search for the query: " + "长" * 400}
        ]

        query = search.query_from_messages(messages)
        assert query is not None
        self.assertEqual(search.MAX_QUERY_CHARS, len(query))


class DomainFilterTests(unittest.TestCase):
    def test_allowed_domains_keep_their_subdomains(self) -> None:
        declaration = {"allowed_domains": ["ruby-lang.org"]}

        kept = search.filtered_rows(ROWS, declaration)

        self.assertEqual(["Ruby 3.4 Released"], [row["title"] for row in kept])

    def test_blocked_domains_drop_their_subdomains(self) -> None:
        declaration = {"blocked_domains": ["github.com"]}

        kept = search.filtered_rows(ROWS, declaration)

        self.assertEqual(["Ruby 3.4 Released"], [row["title"] for row in kept])

    def test_a_lookalike_host_is_not_the_domain(self) -> None:
        declaration = {"allowed_domains": ["ruby-lang.org"]}
        rows = [{"title": "fake", "url": "https://ruby-lang.org.example.test/a"}]

        self.assertEqual([], search.filtered_rows(rows, declaration))


class ResultBlockTests(unittest.TestCase):
    def test_the_pair_names_one_search(self) -> None:
        call, result = search.result_blocks("ruby 3.4", ROWS)

        self.assertEqual("server_tool_use", call["type"])
        self.assertEqual({"query": "ruby 3.4"}, call["input"])
        self.assertTrue(call["id"].startswith("srvtoolu_"))
        self.assertEqual(call["id"], result["tool_use_id"])
        self.assertEqual(
            [
                {
                    "type": "web_search_result",
                    "title": "Ruby 3.4 Released",
                    "url": "https://www.ruby-lang.org/en/news/2024/12/25/ruby-3-4-0-released/",
                    "encrypted_content": "",
                    "page_age": None,
                },
                {
                    "type": "web_search_result",
                    "title": "ruby/ruby",
                    "url": "https://github.com/ruby/ruby",
                    "encrypted_content": "",
                    "page_age": None,
                },
            ],
            result["content"],
        )


class RewriteTests(unittest.TestCase):
    def test_the_declaration_leaves_and_the_pages_are_stated(self) -> None:
        request = side_query_request()

        rewritten = search.rewritten_request(request, "ruby 3.4 release", ROWS, [dict(DECLARATION)])

        self.assertNotIn("tools", rewritten)
        # The client's one text block is extended, not joined by a second one:
        # a Chat bridge reads a single block as one string.
        content = rewritten["messages"][0]["content"]
        self.assertEqual(1, len(content))
        self.assertTrue(
            content[0]["text"].startswith(
                "Perform a web search for the query: ruby 3.4 release"
            )
        )
        self.assertIn("Ruby 3.4 Released", content[0]["text"])
        self.assertIn("https://github.com/ruby/ruby", content[0]["text"])
        self.assertIn("Sources:", content[0]["text"])
        # The client's own body is not mutated: the hook may hand it back.
        self.assertEqual([dict(DECLARATION)], request["tools"])

    def test_other_client_tools_stay(self) -> None:
        request = side_query_request(
            tools=[
                dict(DECLARATION),
                {"name": "Bash", "input_schema": {"type": "object"}},
            ]
        )

        rewritten = search.rewritten_request(request, "q", ROWS, [dict(DECLARATION)])

        self.assertEqual(["Bash"], [tool["name"] for tool in rewritten["tools"]])

    def test_a_string_message_keeps_one_block(self) -> None:
        # A Chat route turns one text block into ``content: string`` and two
        # into a parts list; a relay that only reads the string would refuse
        # the list this pass chose.
        request = side_query_request(
            messages=[
                {
                    "role": "user",
                    "content": "Perform a web search for the query: ruby 3.4",
                }
            ]
        )

        rewritten = search.rewritten_request(request, "ruby 3.4", ROWS, [dict(DECLARATION)])

        content = rewritten["messages"][0]["content"]
        self.assertEqual(1, len(content))
        self.assertEqual("text", content[0]["type"])
        self.assertIn("Ruby 3.4 Released", content[0]["text"])

    def test_a_message_ending_in_a_picture_gets_its_own_text_block(self) -> None:
        request = side_query_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
                        }
                    ],
                }
            ]
        )

        rewritten = search.rewritten_request(request, "ruby 3.4", ROWS, [dict(DECLARATION)])

        content = rewritten["messages"][0]["content"]
        self.assertEqual(["image", "text"], [block["type"] for block in content])
        self.assertIn("Ruby 3.4 Released", content[1]["text"])

    def test_a_forced_choice_of_the_removed_tool_goes_with_it(self) -> None:
        request = side_query_request(tool_choice={"type": "tool", "name": "web_search"})

        rewritten = search.rewritten_request(request, "q", ROWS, [dict(DECLARATION)])

        self.assertNotIn("tool_choice", rewritten)

    def test_a_filtered_empty_answer_still_tells_the_model_why(self) -> None:
        request = side_query_request(tools=[dict(DECLARATION, allowed_domains=["example.test"])])

        rewritten = search.rewritten_request(request, "q", [], [dict(DECLARATION)])

        self.assertIn("no usable page", rewritten["messages"][0]["content"][0]["text"])
        self.assertEqual([], rewritten[search.MARKER_KEY]["blocks"][1]["content"])


class BackendTests(unittest.IsolatedAsyncioTestCase):
    """The Messages wire reaches the same search backends Codex's route does."""

    async def test_a_deepseek_route_answers_with_its_own_search_api(self) -> None:
        endpoint = search._search_endpoint_module
        endpoint.reset_references()
        seen: dict[str, object] = {}

        async def fake(url, key, payload, client):
            seen["url"] = url
            seen["query"] = payload["query"]
            return 200, {
                "results": [
                    {
                        "title": "Ruby 3.4 Released",
                        "url": "https://www.ruby-lang.org/en/news/2024/12/25/ruby-3-4-0-released/",
                        "summary": "Ruby 3.4.0 has been released.",
                    }
                ]
            }

        entries = [
            {
                "litellm_params": {
                    "api_base": "https://api.deepseek.com/v1",
                    "api_key": "k",
                    "model": "deepseek/deepseek-v4-pro",
                }
            }
        ]
        with mock.patch.object(endpoint, "_router_entries", lambda model: entries), mock.patch.object(
            endpoint, "_post_json", fake
        ):
            rows = await endpoint.search_pages(
                "deepseek-v4-pro",
                ["ruby 3.4"],
                question="Perform a web search for the query: ruby 3.4",
            )

        self.assertEqual("https://api.deepseek.com/v1/tools/search", seen["url"])
        self.assertEqual("ruby 3.4", seen["query"])
        self.assertEqual(
            ["https://www.ruby-lang.org/en/news/2024/12/25/ruby-3-4-0-released/"],
            [row["url"] for row in rows],
        )

    async def test_a_group_with_no_search_backend_answers_nothing(self) -> None:
        endpoint = search._search_endpoint_module
        endpoint.reset_references()
        endpoint.reset_unsupported_cache()

        with mock.patch.object(endpoint, "_router_entries", lambda model: []):
            rows = await endpoint.search_pages("some-model", ["q"])

        self.assertEqual([], rows)


class PageReadTests(unittest.IsolatedAsyncioTestCase):
    """The top results are read the way Codex's ``open`` reads a page."""

    def _patched(self, endpoint, fetch):
        return (
            mock.patch.object(endpoint, "_router_entries", lambda model: []),
            mock.patch.object(endpoint, "page_fetcher", lambda client, params=None: fetch),
        )

    async def test_only_the_top_results_are_read(self) -> None:
        endpoint = search._search_endpoint_module
        asked: list[str] = []

        async def fetch(url):
            asked.append(url)
            return (url, "page text for " + url)

        rows = [
            {"title": "a", "url": "https://a.test/1"},
            {"title": "b", "url": "https://a.test/2"},
            {"title": "c", "url": "https://a.test/3"},
        ]
        route_patch, fetch_patch = self._patched(endpoint, fetch)
        with route_patch, fetch_patch:
            enriched = await endpoint.read_result_pages("some-model", rows)

        self.assertEqual(["https://a.test/1", "https://a.test/2"], asked)
        self.assertEqual("page text for https://a.test/1", enriched[0]["content"])
        self.assertNotIn("content", enriched[2])
        # The rows the caller handed in are not mutated.
        self.assertNotIn("content", rows[0])

    async def test_a_page_that_could_not_be_read_keeps_its_row(self) -> None:
        endpoint = search._search_endpoint_module

        async def fetch(url):
            if url.endswith("/2"):
                raise RuntimeError("blocked")
            return None

        route_patch, fetch_patch = self._patched(endpoint, fetch)
        with route_patch, fetch_patch:
            enriched = await endpoint.read_result_pages(
                "some-model",
                [{"url": "https://a.test/1"}, {"url": "https://a.test/2"}],
            )

        self.assertEqual(
            [{"url": "https://a.test/1"}, {"url": "https://a.test/2"}], enriched
        )

    async def test_a_script_rendered_page_is_not_evidence(self) -> None:
        endpoint = search._search_endpoint_module

        async def fetch(url):
            return ("t", "（这个页面用脚本渲染正文，纯文本读取拿不到内容。）")

        route_patch, fetch_patch = self._patched(endpoint, fetch)
        with route_patch, fetch_patch:
            enriched = await endpoint.read_result_pages(
                "some-model", [{"url": "https://a.test/1"}]
            )

        self.assertNotIn("content", enriched[0])

    async def test_the_page_text_is_bounded(self) -> None:
        endpoint = search._search_endpoint_module

        async def fetch(url):
            return ("t", "长" * 4000)

        route_patch, fetch_patch = self._patched(endpoint, fetch)
        with route_patch, fetch_patch:
            enriched = await endpoint.read_result_pages(
                "some-model", [{"url": "https://a.test/1"}]
            )

        self.assertEqual(
            endpoint.MESSAGES_PAGE_TEXT_CHARS, len(enriched[0]["content"])
        )


class PrepareTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_search_that_ran_rewrites_the_request(self) -> None:
        request = side_query_request()

        async def rows(model, queries, question=None):
            self.assertEqual("deepseek-v4-pro", model)
            self.assertEqual(["ruby 3.4 release"], queries)
            self.assertIn("Perform a web search", question or "")
            return list(ROWS)

        async def pages(model, result_rows, limit=2):
            return [dict(row, content="Ruby 3.4.0 shipped.") for row in result_rows]

        with mock.patch.object(search._search_endpoint_module, "search_pages", rows), mock.patch.object(
            search._search_endpoint_module, "read_result_pages", pages
        ), mock.patch.object(
            search, "_served_by_a_claude_route", lambda model: False
        ):
            rewritten = await search.prepare(request)

        assert rewritten is not None
        self.assertNotIn("tools", rewritten)
        self.assertEqual(2, len(search.marked_blocks(rewritten)))
        self.assertIn(
            "Page text (excerpt):", rewritten["messages"][0]["content"][0]["text"]
        )

    async def test_a_page_read_failure_does_not_lose_the_search(self) -> None:
        request = side_query_request()

        async def rows(model, queries, question=None):
            return list(ROWS)

        async def pages(model, result_rows, limit=2):
            raise RuntimeError("reader down")

        with mock.patch.object(search._search_endpoint_module, "search_pages", rows), mock.patch.object(
            search._search_endpoint_module, "read_result_pages", pages
        ), mock.patch.object(
            search, "_served_by_a_claude_route", lambda model: False
        ):
            rewritten = await search.prepare(request)

        assert rewritten is not None
        self.assertIn(
            "Ruby 3.4 Released", rewritten["messages"][0]["content"][0]["text"]
        )

    async def test_a_backend_that_answered_nothing_leaves_the_request_alone(self) -> None:
        async def rows(model, queries, question=None):
            return []

        with mock.patch.object(search._search_endpoint_module, "search_pages", rows), mock.patch.object(
            search, "_served_by_a_claude_route", lambda model: False
        ):
            self.assertIsNone(await search.prepare(side_query_request()))

    async def test_a_claude_route_keeps_its_own_search(self) -> None:
        with mock.patch.object(search._search_endpoint_module, "search_pages") as rows, mock.patch.object(
            search, "_served_by_a_claude_route", lambda model: True
        ):
            self.assertIsNone(await search.prepare(side_query_request()))
        rows.assert_not_called()

    async def test_a_turn_without_a_declaration_is_not_searched(self) -> None:
        request = side_query_request(tools=[])

        with mock.patch.object(search._search_endpoint_module, "search_pages") as rows:
            self.assertIsNone(await search.prepare(request))
        rows.assert_not_called()


class WholeBodyTests(unittest.TestCase):
    def test_blocks_open_the_answer(self) -> None:
        blocks = search.result_blocks("q", ROWS)
        message = {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "hi"}]}

        updated = search.with_result_blocks(message, blocks)

        self.assertEqual(3, len(updated["content"]))
        self.assertEqual("web_search_tool_result", updated["content"][1]["type"])
        # The assembled answer itself is left alone; only its content moved.
        self.assertEqual(1, len(message["content"]))

    def test_the_whole_body_states_its_search_count(self) -> None:
        message = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }

        updated = search.with_result_blocks(message, search.result_blocks("q", ROWS))

        self.assertEqual({"web_search_requests": 1}, updated["usage"]["server_tool_use"])
        self.assertEqual(3, updated["usage"]["input_tokens"])

    def test_a_reported_search_count_is_kept(self) -> None:
        message = {
            "type": "message",
            "content": [],
            "usage": {"server_tool_use": {"web_search_requests": 2}},
        }

        updated = search.with_result_blocks(message, search.result_blocks("q", ROWS))

        self.assertEqual({"web_search_requests": 2}, updated["usage"]["server_tool_use"])

    def test_a_shape_without_content_is_returned_untouched(self) -> None:
        class Answer:
            content = "not a list"

        answer = Answer()

        self.assertIs(answer, search.with_result_blocks(answer, search.result_blocks("q", ROWS)))

    def test_only_this_pass_marker_is_read(self) -> None:
        self.assertEqual([], search.marked_blocks(None))
        self.assertEqual([], search.marked_blocks({"metadata": {"blocks": [{"type": "x"}]}}))
        self.assertEqual([], search.marked_blocks({search.MARKER_KEY: {"blocks": "no"}}))


if __name__ == "__main__":
    unittest.main()

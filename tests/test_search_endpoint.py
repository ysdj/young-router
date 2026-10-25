from __future__ import annotations

import json
import pathlib
import unittest
from unittest import mock

from young_router.proxy import search_endpoint


def _body(**overrides):
    body = {
        "id": "probe",
        "model": "deepseek-v4.1-flash",
        "input": "甪直有什么特产",
        "commands": {"search_query": [{"q": "甪直萝卜"}, {"q": "甪直酱菜"}]},
    }
    body.update(overrides)
    return body


class _PageTextTests(unittest.TestCase):
    """The page a model reads is prose, not the site's navigation tree."""

    def _text(self, html: str) -> str:
        extractor = search_endpoint._TextExtractor()
        extractor.feed(html)
        return extractor.text()

    def test_the_site_menu_is_not_part_of_the_page(self) -> None:
        # The shape that made npm's docs unreadable: one content paragraph
        # behind a navigation tree of link-only entries.
        menu = "".join(
            f'<li><a href="/c/{index}">npm command number {index}</a></li>'
            for index in range(40)
        )
        html = (
            "<html><head><title>npm-prune | npm Docs</title></head><body>"
            f"<nav><ul>{menu}</ul></nav>"
            '<main><h1>npm-prune</h1><h2>Description</h2>'
            "<p>Removes extraneous packages from node_modules.</p>"
            "<p>Use <code>--omit=dev</code> to drop devDependencies.</p></main>"
            "</body></html>"
        )
        text = self._text(html)
        self.assertIn("Removes extraneous packages", text)
        self.assertIn("--omit=dev", text)
        self.assertNotIn("npm command number 7", text)

    def test_a_navigation_page_without_a_main_element_falls_back_to_link_filtering(self) -> None:
        menu = "".join(
            f'<div><a href="/p/{index}">document {index}</a></div>' for index in range(30)
        )
        html = (
            "<html><body>"
            f"{menu}"
            "<p>The page a reader actually wants is this paragraph.</p>"
            "</body></html>"
        )
        text = self._text(html)
        self.assertIn("the page a reader actually wants", text.lower())
        self.assertNotIn("document 3", text)

    def test_a_section_index_repeated_at_the_end_is_dropped(self) -> None:
        outline = "".join(f"<li>Section {index}</li>" for index in range(8))
        html = (
            "<html><body><main>"
            f"<ul>{outline}</ul>"
            "<p>Body text that the reader came for.</p>"
            f"<ul>{outline}</ul>"
            "</main></body></html>"
        )
        text = self._text(html)
        self.assertIn("Body text that the reader came for.", text)
        self.assertEqual(1, text.count("Section 5"))

    def test_a_heading_that_wraps_its_own_anchor_is_kept(self) -> None:
        html = (
            "<html><body><main>"
            '<h2><a href="#description">Description</a></h2>'
            "<p>Body.</p>"
            "</main></body></html>"
        )
        self.assertIn("Description", self._text(html))


class SearchEndpointShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        search_endpoint.reset_unsupported_cache()

    def test_queries_come_from_the_declaration_and_stop_at_the_tool_limit(self) -> None:
        body = _body(
            commands={
                "search_query": [
                    {"q": "a"},
                    {"q": "  "},
                    {"q": "b"},
                    {"q": "c"},
                    {"q": "d"},
                    {"q": "e"},
                ]
            }
        )
        self.assertEqual(["a", "b", "c", "d"], search_endpoint.search_queries(body))

    def test_a_turn_without_queries_falls_back_to_its_input(self) -> None:
        self.assertEqual(
            ["甪直有什么特产"], search_endpoint.search_queries(_body(commands={}))
        )

    def test_response_length_selects_the_result_count(self) -> None:
        body = _body(commands={"search_query": [{"q": "x"}], "response_length": "long"})
        self.assertEqual(20, search_endpoint.result_count(body))
        self.assertEqual(10, search_endpoint.result_count(_body()))

    def test_open_accepts_a_url_and_reports_an_unknown_reference(self) -> None:
        body = _body(
            commands={
                "search_query": [{"q": "x"}],
                "open": [{"ref_id": "turn0search0"}, {"ref_id": "https://example.test/a"}],
            }
        )
        self.assertEqual(
            [{"missing_ref": "turn0search0"}, {"url": "https://example.test/a"}],
            search_endpoint.open_requests(body),
        )

    def test_a_reference_from_this_session_resolves_to_its_page(self) -> None:
        session: dict[str, str] = {}
        search_endpoint.remember_references(session, {"turn7search3": "https://example.test/b"})
        body = _body(
            commands={"search_query": [{"q": "x"}], "open": [{"ref_id": "turn7search3"}]}
        )
        self.assertEqual(
            [{"url": "https://example.test/b"}],
            search_endpoint.open_requests(body, session),
        )

    def test_a_reference_names_one_page_and_a_different_batch_gets_new_ids(self) -> None:
        same_a, _ = search_endpoint.render_results(
            [{"title": "a", "url": "https://example.test/a", "summary": "s"}]
        )
        same_b, _ = search_endpoint.render_results(
            [{"title": "a", "url": "https://example.test/a", "summary": "s"}]
        )
        other, _ = search_endpoint.render_results(
            [{"title": "b", "url": "https://example.test/b", "summary": "s"}]
        )
        ref_a = same_a.split("cite ")[1].split()[0]
        ref_b = same_b.split("cite ")[1].split()[0]
        ref_other = other.split("cite ")[1].split()[0]
        # The same result set keeps its references, so a replayed batch stays
        # resolvable; a different set gets different ids.
        self.assertEqual(ref_a, ref_b)
        self.assertNotEqual(ref_a, ref_other)

    def test_a_reference_survives_the_process_that_produced_it(self) -> None:
        """A later turn's open may land on another worker, so the page is shared."""

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "web-search-references.json")
            with mock.patch.dict("os.environ", {"YOUNG_ROUTER_SEARCH_STATE_FILE": path}):
                output, _ = search_endpoint.render_results(
                    [{"title": "a", "url": "https://example.test/a", "summary": "s"}]
                )
                ref = output.split("cite ")[1].split()[0]
                # A fresh session stands in for the next turn / another worker.
                body = _body(commands={"search_query": [{"q": "x"}], "open": [{"ref_id": ref}]})
                self.assertEqual(
                    [{"url": "https://example.test/a"}], search_endpoint.open_requests(body, {})
                )

    def test_rendered_results_carry_both_a_url_and_a_scoped_reference(self) -> None:
        output, structured = search_endpoint.render_results(
            [
                {
                    "title": "甪直萝卜干_百度百科",
                    "url": "https://bkso.baidu.com/item/x",
                    "summary": "色泽红亮，咸中透甜。",
                }
            ]
        )
        self.assertIn("甪直萝卜干_百度百科 (https://bkso.baidu.com/item/x)", output)
        self.assertRegex(output, r"cite turn\d+search0")
        self.assertEqual("bkso.baidu.com", structured[0]["domain"])
        self.assertEqual("text_result", structured[0]["type"])

    def test_a_result_follows_the_official_envelope_without_inventing_an_ellipsis(self) -> None:
        summary = "正" * 600
        output, _structured = search_endpoint.render_results(
            [
                {"title": "a", "url": "https://example.test/a", "summary": summary,
                 "date": "2023-08-01T00:00:00+00:00"},
                {"title": "b", "url": "https://example.test/b", "summary": "短", "date": None},
            ]
        )
        lines = output.splitlines()
        # title/url, then `` cite … [wordlim: N] Published: …; ``, then the text
        self.assertEqual("a (https://example.test/a)", lines[0])
        self.assertRegex(lines[1], r"^ cite turn\d+search0 Published: 2023-08-01;$")
        self.assertEqual(summary, lines[2])
        self.assertEqual("-" * 80, lines[3])
        self.assertEqual("b (https://example.test/b)", lines[4])
        self.assertEqual(" cite turn" + output.split("turn")[1].split("search")[0] + "search1", lines[5])
        # The upstream text is handed back whole and unconditionally.
        self.assertNotIn("…", output)

    def test_a_result_without_a_date_declares_only_the_reference(self) -> None:
        output, _ = search_endpoint.render_results(
            [{"title": "t", "url": "https://example.test/a", "summary": "s", "date": None}]
        )
        self.assertIn("cite turn", output)
        self.assertNotIn("wordlim", output)
        self.assertNotIn("Published:", output)

    def test_a_command_this_endpoint_cannot_serve_is_named_not_dropped(self) -> None:
        body = _body(
            commands={
                "search_query": [{"q": "x"}],
                "finance": [{"ticker": "AMD", "type": "equity", "market": "USA"}],
            }
        )
        self.assertEqual(["finance"], search_endpoint.unsupported_commands(body))
        payload = search_endpoint._with_unsupported_notes(
            {"output": "results", "results": []}, body
        )
        self.assertIn("results", payload["output"])
        self.assertIn("finance", payload["output"])

    def test_tips_name_the_browser_tools_and_refuse_a_retry(self) -> None:
        output = search_endpoint.tips_output(
            "deepseek-v4.1-flash", ["甪直萝卜"], "这条路线没有可用的搜索后端"
        )
        self.assertIn("cua_repl", output)
        self.assertIn("playwright-cli", output)
        self.assertIn("不要再次调用 web.run", output)
        self.assertIn("https://www.bing.com/search?q=", output)

    def test_payload_matches_the_response_codex_decodes(self) -> None:
        payload = search_endpoint.tips_payload("m", _body(), "no backend")
        self.assertIsNone(payload["encrypted_output"])
        self.assertEqual([], payload["results"])
        json.dumps(payload)

    def test_upstream_model_name_drops_litellm_provider_prefix(self) -> None:
        self.assertEqual(
            "gpt-6.1-sol",
            search_endpoint.upstream_model_name({"model": "openai/gpt-6.1-sol"}),
        )
        self.assertEqual(
            "deepseek-v4.1-flash",
            search_endpoint.upstream_model_name({"model": "deepseek-v4.1-flash"}),
        )

    def test_master_key_resolves_an_environment_reference(self) -> None:
        with mock.patch.dict("os.environ", {"PROBE_MASTER": "sk-local"}):
            self.assertEqual(
                "sk-local",
                search_endpoint.master_key({"master_key": "os.environ/PROBE_MASTER"}),
            )

    def test_deepseek_route_is_recognised_by_its_own_host(self) -> None:
        self.assertTrue(
            search_endpoint.is_deepseek_deployment(
                {"api_base": "https://api.deepseek.com/v1"}
            )
        )
        self.assertFalse(
            search_endpoint.is_deepseek_deployment(
                {"api_base": "https://api.sisct2.xyz/v1"}
            )
        )


class SearchEndpointForwardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        search_endpoint.reset_unsupported_cache()

    async def test_a_refusing_gateway_is_remembered_instead_of_retried(self) -> None:
        calls: list[str] = []

        async def refuse(url, key, payload, client):
            calls.append(url)
            return 404, {"error": "not found"}

        with mock.patch.object(search_endpoint, "_post_json", refuse):
            first = await search_endpoint._alpha_search_forward(
                {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/gpt-6.1-sol"},
                _body(),
                None,  # type: ignore[arg-type]
            )
            second = await search_endpoint._alpha_search_forward(
                {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/gpt-6.1-sol"},
                _body(),
                None,  # type: ignore[arg-type]
            )

        self.assertEqual(("unsupported", None), first)
        self.assertEqual(("unsupported", None), second)
        self.assertEqual(["https://relay.test/v1/alpha/search"], calls)

    async def test_a_transient_failure_is_not_remembered(self) -> None:
        async def boom(url, key, payload, client):
            return 503, {"error": "upstream"}

        with mock.patch.object(search_endpoint, "_post_json", boom):
            state, _payload = await search_endpoint._alpha_search_forward(
                {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/gpt-6.1-sol"},
                _body(),
                None,  # type: ignore[arg-type]
            )
        self.assertEqual("failed:503", state)

    async def test_page_commands_are_forwarded_with_the_search(self) -> None:
        """The upstream implements ``open``/``find``; only a failed answer is repaired."""

        seen: dict[str, object] = {}

        async def ok(url, key, payload, client):
            seen["commands"] = payload["commands"]
            return 200, {"output": "hit", "results": []}

        body = _body(
            commands={
                "search_query": [{"q": "x"}],
                "open": [{"ref_id": "https://example.test/p"}],
                "find": [{"ref_id": "https://example.test/p", "pattern": "y"}],
            }
        )
        with mock.patch.object(search_endpoint, "_post_json", ok):
            await search_endpoint._alpha_search_forward(
                {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/gpt-6.1-sol"},
                body,
                None,  # type: ignore[arg-type]
            )

        self.assertEqual(body["commands"], seen["commands"])

    async def test_a_page_only_call_still_goes_to_the_gateway_first(self) -> None:
        seen: dict[str, object] = {}

        async def ok(url, key, payload, client):
            seen["commands"] = payload["commands"]
            return 200, {"output": "page text", "results": []}

        with mock.patch.object(search_endpoint, "_post_json", ok):
            state, payload = await search_endpoint._alpha_search_forward(
                {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/gpt-6.1-sol"},
                _body(commands={"open": [{"ref_id": "https://example.test/p"}]}),
                None,  # type: ignore[arg-type]
            )

        self.assertEqual("ok", state)
        self.assertEqual("page text", (payload or {}).get("output"))
        self.assertIn("open", seen["commands"])

    async def test_a_capable_gateway_gets_the_route_own_model(self) -> None:
        seen: dict[str, object] = {}

        async def ok(url, key, payload, client):
            seen["url"] = url
            seen["model"] = payload["model"]
            return 200, {"encrypted_output": None, "output": "hit", "results": []}

        with mock.patch.object(search_endpoint, "_post_json", ok):
            state, payload = await search_endpoint._alpha_search_forward(
                {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/gpt-6.1-sol"},
                _body(),
                None,  # type: ignore[arg-type]
            )

        self.assertEqual("ok", state)
        self.assertEqual("hit", (payload or {}).get("output"))
        self.assertEqual("https://relay.test/v1/alpha/search", seen["url"])
        self.assertEqual("gpt-6.1-sol", seen["model"])


class SearchEndpointDeepSeekTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        search_endpoint.reset_references()
        search_endpoint.reset_unsupported_cache()

    async def test_each_query_is_named_in_the_rendered_answer(self) -> None:
        async def fake(url, key, payload, client):
            if url.endswith("/search"):
                return 200, {
                    "results": [
                        {
                            "title": f"r-{payload['query']}",
                            "url": f"https://x.test/{payload['query']}",
                            "summary": "s",
                        }
                    ]
                }
            return 404, None

        body = _body(commands={"search_query": [{"q": "alpha"}, {"q": "beta"}]})
        with mock.patch.object(search_endpoint, "_post_json", fake):
            payload, _reason = await search_endpoint._deepseek_search(
                {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}, body, None
            )

        self.assertIn("查询 1：alpha", payload["output"])
        self.assertIn("查询 2：beta", payload["output"])
        self.assertIn("https://x.test/alpha", payload["output"])
        self.assertIn("https://x.test/beta", payload["output"])

    async def test_every_query_in_one_call_is_searched_and_merged(self) -> None:
        searched: list[str] = []

        async def fake(url, key, payload, client):
            url_path = url.rsplit("/", 1)[-1]
            if url_path == "search":
                searched.append(payload["query"])
                return 200, {
                    "results": [
                        {"title": payload["query"], "url": f"https://example.test/{payload['query']}", "summary": "s"},
                        {"title": "dup", "url": "https://example.test/dup", "summary": "s"},
                    ]
                }
            return 404, None

        body = _body(
            commands={
                "search_query": [{"q": "a"}, {"q": "b"}, {"q": "c"}, {"q": "d"}],
                "response_length": "short",
            }
        )
        with mock.patch.object(search_endpoint, "_post_json", fake):
            payload, reason = await search_endpoint._deepseek_search(
                {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}, body, None
            )

        self.assertEqual("", reason)
        self.assertEqual(["a", "b", "c", "d"], sorted(searched))
        # The duplicate URL is kept once across all four answers.
        self.assertEqual(5, len(payload["results"]))

    async def test_open_by_reference_fetches_the_page_it_names(self) -> None:
        session = search_endpoint._session_for("probe")
        search_endpoint.remember_references(
            session, {"turn11search0": "https://example.test/p"}
        )
        fetched: list[str] = []

        async def fake(url, key, payload, client):
            fetched.append(payload.get("url", ""))
            return 200, {"title": "页面标题", "content": "# 正文\n甪直萝卜"}

        body = _body(commands={"open": [{"ref_id": "turn11search0"}]})
        with mock.patch.object(search_endpoint, "_post_json", fake):
            text, results = await search_endpoint._page_answers(
                body,
                session,
                search_endpoint.page_fetcher(
                    None, {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}
                ),
            )

        self.assertEqual(["https://example.test/p"], fetched)
        self.assertIn("页面标题", text)
        self.assertIn("甪直萝卜", text)
        self.assertEqual("text_result", results[0]["type"])

    async def test_find_returns_only_the_matching_lines_with_their_numbers(self) -> None:
        session = search_endpoint._session_for("probe")
        search_endpoint.remember_references(
            session, {"turn12search1": "https://example.test/q"}
        )

        async def fake(url, key, payload, client):
            return 200, {
                "title": "t",
                "content": "第一行\n甪直萝卜 始创道光年间\n第三行\n又是道光",
            }

        body = _body(commands={"find": [{"ref_id": "turn12search1", "pattern": "道光"}]})
        with mock.patch.object(search_endpoint, "_post_json", fake):
            text, _results = await search_endpoint._page_answers(
                body,
                session,
                search_endpoint.page_fetcher(
                    None, {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}
                ),
            )

        # A find answer is the matching lines, not the whole page again: that
        # is what the model came for, and re-sending the page is the cost this
        # endpoint exists to avoid.
        self.assertIn("L1: 甪直萝卜 始创道光年间", text)
        self.assertIn("L3: 又是道光", text)
        self.assertNotIn("第一行", text)

    async def test_a_find_answer_names_a_view_a_later_open_can_address(self) -> None:
        session = search_endpoint._session_for("probe")
        search_endpoint.remember_references(
            session, {"turn12search1": "https://example.test/q"}
        )

        async def fake(url, key, payload, client):
            return 200, {"title": "t", "content": "a\nb\nc\nd"}

        with mock.patch.object(search_endpoint, "_post_json", fake):
            text, _results = await search_endpoint._page_answers(
                _body(commands={"find": [{"ref_id": "turn12search1", "pattern": "c"}]}),
                session,
                search_endpoint.page_fetcher(
                    None, {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}
                ),
            )

        view = text.split("cite ")[1].split()[0]
        self.assertRegex(view, r"^turn\d+view0$")
        # The view names the page, so the follow-up open it enables is a real
        # page read rather than an unresolved reference.
        self.assertEqual(
            "https://example.test/q", search_endpoint.resolve_reference(view, session)
        )

    async def test_lineno_reads_the_page_from_where_the_model_pointed(self) -> None:
        session = search_endpoint._session_for("probe")
        search_endpoint.remember_references(
            session, {"turn13search0": "https://example.test/r"}
        )

        async def fake(url, key, payload, client):
            return 200, {"title": "t", "content": "L0 body\nL1 body\nL2 body\nL3 body"}

        body = _body(commands={"open": [{"ref_id": "turn13search0", "lineno": 2}]})
        with mock.patch.object(search_endpoint, "_post_json", fake):
            text, _results = await search_endpoint._page_answers(
                body,
                session,
                search_endpoint.page_fetcher(
                    None, {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}
                ),
            )

        self.assertNotIn("L0 body", text)
        self.assertIn("L2 body", text)

    async def test_a_stale_reference_says_so_instead_of_opening_nothing(self) -> None:
        async def fake(url, key, payload, client):
            return 200, {"results": []}

        body = _body(commands={"open": [{"ref_id": "turn99search9"}]})
        with mock.patch.object(search_endpoint, "_post_json", fake):
            text, _results = await search_endpoint._page_answers(
                body,
                search_endpoint._session_for("probe"),
                search_endpoint.page_fetcher(
                    None, {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}
                ),
            )

        self.assertIn("已失效", text)


class _PageReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_meta_refresh_page_is_followed_instead_of_read_as_empty(self) -> None:
        """npm's canonical docs URL is a 77-byte stub that only refreshes.

        Reading it as text gave the model an empty page for a URL the search
        answer had just recommended, so the only way forward looked like
        searching again.
        """

        class _Response:
            status_code = 200
            encoding = "utf-8"
            headers = {"content-type": "text/html; charset=utf-8"}

            def __init__(self, content: bytes) -> None:
                self.content = content

        pages = {
            "https://docs.example.test/cli/commands/prune/": _Response(
                b'<meta http-equiv="refresh" content="0; URL=\'/cli/v12/commands/prune/\'" />'
            ),
            "https://docs.example.test/cli/v12/commands/prune/": _Response(
                b"<html><head><title>npm-prune</title></head><body><main>"
                b"<h2>Description</h2><p>Removes extraneous packages.</p>"
                b"</main></body></html>"
            ),
        }
        asked: list[str] = []

        class _Client:
            async def get(self, url, **kwargs):
                asked.append(url)
                return pages[url]

        title, text = await search_endpoint.local_page(
            "https://docs.example.test/cli/commands/prune/", _Client()
        )

        self.assertEqual(
            [
                "https://docs.example.test/cli/commands/prune/",
                "https://docs.example.test/cli/v12/commands/prune/",
            ],
            asked,
        )
        self.assertEqual("npm-prune", title)
        self.assertIn("Removes extraneous packages.", text)


class SearchEndpointLocalFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        search_endpoint.reset_references()

    async def test_this_host_reads_the_page_before_asking_the_vendor(self) -> None:
        """A vendor fetch answers in escaped Markdown; the plain read is clean."""

        calls: list[str] = []

        async def fake_post(url, key, payload, client):
            calls.append("vendor")
            return 200, {"title": "vendor", "content": "\\#\\# vendor"}

        async def fake_local(url, client):
            calls.append("local")
            return ("本地标题", "本地正文")

        search_endpoint.remember_references(
            search_endpoint._session_for("probe"), {"turn5search0": "https://example.test/p"}
        )
        body = _body(commands={"open": [{"ref_id": "turn5search0"}]})
        with mock.patch.object(search_endpoint, "_post_json", fake_post), \
             mock.patch.object(search_endpoint, "local_page", fake_local):
            text, _results = await search_endpoint._page_answers(
                body,
                search_endpoint._session_for("probe"),
                search_endpoint.page_fetcher(
                    None, {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}
                ),
            )

        self.assertEqual(["local"], calls)
        self.assertIn("本地正文", text)

    async def test_a_page_this_host_cannot_read_goes_to_the_vendor(self) -> None:
        fetched: list[str] = []

        async def fake_post(url, key, payload, client):
            fetched.append(payload.get("url", ""))
            return 200, {"title": "vendor title", "content": "vendor body"}

        async def fake_local(url, client):
            return None

        search_endpoint.remember_references(
            search_endpoint._session_for("probe"), {"turn5search1": "https://example.test/p"}
        )
        body = _body(commands={"open": [{"ref_id": "turn5search1"}]})
        with mock.patch.object(search_endpoint, "_post_json", fake_post), \
             mock.patch.object(search_endpoint, "local_page", fake_local):
            text, _results = await search_endpoint._page_answers(
                body,
                search_endpoint._session_for("probe"),
                search_endpoint.page_fetcher(
                    None, {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}
                ),
            )

        self.assertEqual(["https://example.test/p"], fetched)
        self.assertIn("vendor body", text)

    async def test_a_page_neither_side_can_read_is_reported_plainly(self) -> None:
        async def fake_post(url, key, payload, client):
            return 404, None

        async def fake_local(url, client):
            return None

        search_endpoint.remember_references(
            search_endpoint._session_for("probe"), {"turn6search0": "https://example.test/q"}
        )
        body = _body(commands={"open": [{"ref_id": "turn6search0"}]})
        with mock.patch.object(search_endpoint, "_post_json", fake_post), \
             mock.patch.object(search_endpoint, "local_page", fake_local):
            text, _results = await search_endpoint._page_answers(
                body,
                search_endpoint._session_for("probe"),
                search_endpoint.page_fetcher(
                    None, {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}
                ),
            )

        self.assertIn("打开失败", text)
        self.assertIn("https://example.test/q", text)


class SearchEndpointRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        search_endpoint.reset_unsupported_cache()

    async def test_the_router_order_is_kept_and_only_this_models_routes_are_used(self) -> None:
        routed: list[str] = []

        async def fake_deepseek(params, body, client):
            routed.append("deepseek")
            return {"encrypted_output": None, "output": "ds", "results": []}, ""

        async def fake_alpha(params, body, client):
            routed.append("alpha")
            return "unsupported", None

        entries = [
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "relay-key", "model": "openai/deepseek-v4.1-flash"}},
            {"litellm_params": {"api_base": "https://api.deepseek.com/v1", "api_key": "ds-key", "model": "deepseek-v4.1-flash"}},
        ]

        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_deepseek_search", fake_deepseek), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", fake_alpha):
            payload = await search_endpoint._search(_body(), "deepseek-v4.1-flash")

        self.assertEqual("ds", payload["output"])
        # Declaration order is the fallback order: the relay's own route first,
        # the vendor's own API (which answers for its models) last.
        self.assertEqual(["alpha", "deepseek"], routed)

    async def test_an_empty_vendor_answer_still_falls_back_to_the_same_models_relay(self) -> None:
        routed: list[str] = []

        async def no_results(params, body, client):
            routed.append("deepseek")
            return {"encrypted_output": None, "output": "", "results": []}, ""

        async def fake_alpha(params, body, client):
            routed.append("alpha")
            return "unsupported", None

        entries = [
            {"litellm_params": {"api_base": "https://api.deepseek.com/v1", "api_key": "ds-key", "model": "deepseek-v4.1-flash"}},
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "relay-key", "model": "openai/deepseek-v4.1-flash"}},
        ]

        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_deepseek_search", no_results), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", fake_alpha):
            payload = await search_endpoint._search(_body(), "deepseek-v4.1-flash")

        self.assertEqual(["deepseek", "alpha"], routed)
        self.assertIn("cua_repl", payload["output"])

    async def test_a_find_answered_without_the_pattern_is_repaired_from_the_page(self) -> None:
        """The upstream's ``find`` window starts at the top of the page.

        Its answer can therefore be a page fragment that does not contain the
        pattern at all -- the shape that made the model believe the page had
        nothing and ask again.  Only that self-contradicting answer is replaced.
        """

        async def fake_alpha(params, body, client):
            return "ok", {
                "output": 'npm-prune (https://example.test/p)\n cite turn1view0 '
                "[wordlim: 200] Source: find({\"pattern\":\"Description\"}); Total lines: 400\n"
                "L0: 【0†Skip to search】\nL1: 【1†Skip to content】",
                "results": [{"type": "text_result", "ref_id": "turn1view0", "url": "https://example.test/p"}],
            }

        async def fake_fetch(url):
            return ("页面标题", "导航\nDescription\n第三行")

        entries = [
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/x"}},
        ]
        body = _body(commands={"find": [{"ref_id": "https://example.test/p", "pattern": "Description"}]})
        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", fake_alpha), \
             mock.patch.object(search_endpoint, "local_page", lambda url, client: fake_fetch(url)):
            payload = await search_endpoint._search(body, "x")

        self.assertIn("L1: Description", payload["output"])

    async def test_a_find_the_upstream_answered_correctly_is_left_alone(self) -> None:
        upstream_output = (
            'npm-prune (https://example.test/p)\n cite turn1view0 Total lines: 400\n'
            "L262: ## Description\nL263: This command removes extraneous packages."
        )

        async def fake_alpha(params, body, client):
            return "ok", {
                "output": upstream_output,
                "results": [{"type": "text_result", "ref_id": "turn1view0", "url": "https://example.test/p"}],
            }

        async def should_not_read(url, client):
            raise AssertionError("a correct upstream answer must not be re-read locally")

        entries = [
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/x"}},
        ]
        body = _body(commands={"find": [{"ref_id": "https://example.test/p", "pattern": "Description"}]})
        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", fake_alpha), \
             mock.patch.object(search_endpoint, "local_page", should_not_read):
            payload = await search_endpoint._search(body, "x")

        self.assertEqual(upstream_output, payload["output"])

    async def test_a_genuine_upstream_miss_is_not_turned_into_a_local_hit(self) -> None:
        async def fake_alpha(params, body, client):
            return "ok", {
                "output": 'npm-prune (https://example.test/p)\n'
                'No matching text found for "zzzz"',
                "results": [],
            }

        async def should_not_read(url, client):
            raise AssertionError("a reported miss is a real answer")

        entries = [
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/x"}},
        ]
        body = _body(commands={"find": [{"ref_id": "https://example.test/p", "pattern": "zzzz"}]})
        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", fake_alpha), \
             mock.patch.object(search_endpoint, "local_page", should_not_read):
            payload = await search_endpoint._search(body, "x")

        self.assertIn("No matching text found", payload["output"])

    async def test_an_open_that_never_reached_the_requested_line_is_repaired(self) -> None:
        async def fake_alpha(params, body, client):
            return "ok", {
                "output": 'p (https://example.test/p) Total lines: 400\nL0: head\nL1: head',
                "results": [{"type": "text_result", "ref_id": "turn1view0", "url": "https://example.test/p"}],
            }

        async def fake_fetch(url):
            return ("页面标题", "\n".join(f"line {index}" for index in range(50)))

        entries = [
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/x"}},
        ]
        body = _body(commands={"open": [{"ref_id": "https://example.test/p", "lineno": 30}]})
        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", fake_alpha), \
             mock.patch.object(search_endpoint, "local_page", lambda url, client: fake_fetch(url)):
            payload = await search_endpoint._search(body, "x")

        self.assertIn("L30: line 30", payload["output"])

    async def test_a_page_turn_with_no_backend_is_still_read_here(self) -> None:
        async def unsupported(params, body, client):
            return "unsupported", None

        async def fake_fetch(url):
            return ("页面标题", "body text")

        entries = [
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/x"}},
        ]
        body = _body(commands={"open": [{"ref_id": "https://example.test/p"}]})
        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", unsupported), \
             mock.patch.object(search_endpoint, "local_page", lambda url, client: fake_fetch(url)):
            payload = await search_endpoint._search(body, "x")

        self.assertIn("body text", payload["output"])

    async def test_a_search_and_a_page_in_one_call_do_both_jobs(self) -> None:
        async def fake_alpha(params, body, client):
            return "ok", {"output": "search answer", "results": []}

        async def fake_local(url, client):
            return ("页面标题", "页面正文")

        entries = [
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/x"}},
        ]
        body = _body(
            commands={
                "search_query": [{"q": "x"}],
                "open": [{"ref_id": "https://example.test/p"}],
            }
        )
        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", fake_alpha), \
             mock.patch.object(search_endpoint, "local_page", fake_local):
            payload = await search_endpoint._search(body, "x")

        self.assertIn("search answer", payload["output"])
        self.assertIn("页面正文", payload["output"])

    async def test_a_gateway_answer_still_leaves_its_references_addressable(self) -> None:
        async def fake_alpha(params, body, client):
            return "ok", {
                "output": "r1 (https://example.test/a)\n cite turn7search0",
                "results": [
                    {"type": "text_result", "ref_id": "turn7search0", "url": "https://example.test/a"}
                ],
            }

        entries = [
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/x"}},
        ]
        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", fake_alpha):
            await search_endpoint._search(_body(), "x")

        self.assertEqual(
            "https://example.test/a",
            search_endpoint.resolve_reference("turn7search0", search_endpoint._session_for("probe")),
        )

    async def test_a_route_without_backends_tips_toward_the_browser(self) -> None:
        async def unsupported(params, body, client):
            return "unsupported", None

        entries = [
            {"litellm_params": {"api_base": "https://relay.test/v1", "api_key": "k", "model": "openai/x"}},
        ]
        with mock.patch.object(search_endpoint, "_router_entries", lambda model: entries), \
             mock.patch.object(search_endpoint, "_alpha_search_forward", unsupported):
            payload = await search_endpoint._search(_body(), "deepseek-v4.1-flash")

        self.assertIn("cua_repl", payload["output"])


class _StubResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code


class _StubRequest:
    def __init__(self, body: bytes, authorization: str = "") -> None:
        self._body = body
        self.headers = {"authorization": authorization} if authorization else {}

    async def body(self) -> bytes:
        return self._body


class SearchEndpointRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        search_endpoint.reset_unsupported_cache()

    async def test_both_spellings_of_the_provider_relative_route_are_registered(self) -> None:
        routes: list[str] = []

        class App:
            def add_api_route(self, path, endpoint, methods=None, include_in_schema=True):
                routes.append(path)

        with mock.patch.dict("sys.modules", {
            "fastapi": mock.MagicMock(Request=object),
            "fastapi.responses": mock.MagicMock(JSONResponse=_StubResponse),
        }):
            search_endpoint.register(App())
        self.assertEqual(["/v1/alpha/search", "/alpha/search"], routes)

    async def test_a_missing_body_model_is_refused(self) -> None:
        request = _StubRequest(b'{"id":"x"}')
        response = await search_endpoint.alpha_search(request, _StubResponse)
        self.assertEqual(400, response.status_code)

    async def test_the_route_requires_the_key_this_proxy_hands_out(self) -> None:
        settings = {"master_key": "sk-probe"}
        request = _StubRequest(b'{"model":"m"}', authorization="Bearer wrong")
        with mock.patch.object(search_endpoint, "proxy_general_settings", lambda: settings):
            response = await search_endpoint.alpha_search(request, _StubResponse)
        self.assertEqual(401, response.status_code)

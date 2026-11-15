from __future__ import annotations

import asyncio
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


class _TextExtractorEdgeTests(unittest.TestCase):
    def test_a_void_element_does_not_keep_the_article_scope_open(self) -> None:
        html = (
            "<html><body><article><p>Kept article text.</p><br>"
            "<img src='x'></article>"
            "<div>Trailing chrome that should not be read.</div></body></html>"
        )
        extractor = search_endpoint._TextExtractor()
        extractor.feed(html)
        text = extractor.text()
        self.assertIn("Kept article text.", text)
        self.assertNotIn("Trailing chrome", text)


class _PageReadEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_self_refreshing_page_is_read_once_instead_of_recursing(self) -> None:
        class _Response:
            status_code = 200
            encoding = "utf-8"
            headers = {"content-type": "text/html"}
            content = b'<meta http-equiv="refresh" content="0; url=/same">'

        asked: list[str] = []

        class _Client:
            async def get(self, url, **kwargs):
                asked.append(url)
                return _Response()

        title, _text = await search_endpoint.local_page(
            "https://example.test/same", _Client()
        )
        self.assertEqual(["https://example.test/same"], asked)
        self.assertEqual("https://example.test/same", title)


class _AnthropicResultEdgeTests(unittest.TestCase):
    def test_a_publish_date_is_a_date_not_a_snippet(self) -> None:
        payload = {
            "content": [{
                "type": "web_search_tool_result",
                "content": [{
                    "type": "web_search_result",
                    "title": "甪直古镇",
                    "url": "https://example.test/luzhi",
                    "page_age": "2025-02-07T00:00:00Z",
                }],
            }]
        }
        [result] = search_endpoint._anthropic_search_results(payload)
        self.assertEqual("2025-02-07", result["date"])
        self.assertNotIn("snippet", result)


class _AnthropicSearchEdgeTests(unittest.TestCase):
    def test_an_overlapping_second_query_does_not_discard_the_search(self) -> None:
        payload = {"content": [{"type": "web_search_tool_result", "content": [{
            "type": "web_search_result",
            "title": "甪直 介绍",
            "url": "https://example.test/luzhi",
        }]}]}

        class _Client:
            async def post(self, url, **kwargs):
                response = mock.Mock(status_code=200)
                response.json.return_value = payload
                return response

        params = {"api_base": "https://relay.test/v1", "api_key": "k",
                  "model": "anthropic/claude-opus-5-5"}
        served, reason = asyncio.run(
            search_endpoint._anthropic_search(params, _body(), _Client())
        )
        self.assertIsNotNone(served, reason)
        self.assertIn("https://example.test/luzhi", served["output"])


class _BarrierClient:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.release = asyncio.Event()

    async def post(self, url, **kwargs):
        query = kwargs["json"]["messages"][0]["content"].splitlines()[0]
        query = query.removeprefix("Search the web for: ")
        self.started.append(query)
        if len(self.started) >= 2:
            self.release.set()
        await self.release.wait()
        response = mock.Mock(status_code=200)
        response.json.return_value = {"content": [{
            "type": "web_search_tool_result",
            "content": [{"type": "web_search_result", "title": query,
                         "url": f"https://example.test/{query}"}],
        }]}
        return response


class _AnthropicSearchFanOutEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_queries_are_issued_together(self) -> None:
        client = _BarrierClient()
        params = {"api_base": "https://relay.test/v1", "api_key": "k",
                  "model": "anthropic/claude-opus-5-5"}
        task = asyncio.create_task(
            search_endpoint._anthropic_search(params, _body(), client)
        )
        for _ in range(5):
            if client.release.is_set():
                break
            await asyncio.sleep(0)
        if not client.release.is_set():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(client.release.is_set(), client.started)
        served, reason = await task
        self.assertIsNotNone(served, reason)


class _RepairedSegmentEdgeTests(unittest.TestCase):
    def test_a_segment_reference_is_matched_with_the_live_session(self) -> None:
        output = (
            'p (https://example.test/p)\n cite turn1view0 '
            'Source: find({"ref_id":"turn1view0","pattern":"Description"}); Total lines: 4\n'
            "L0: head"
        )
        unserved = {"find": [{"url": "https://example.test/p",
                              "pattern": "Description"}]}
        session = {"turn1view0": "https://example.test/p"}
        with mock.patch.dict("os.environ", {
            "YOUNG_ROUTER_SEARCH_STATE_FILE": "",
            "YOUNG_ROUTER_ROUTE_RECOVERY_STATE_FILE": "",
        }):
            kept = search_endpoint._drop_repaired_segments(output, unserved, session)
        self.assertEqual("", kept)


class _FetchFallbackTraceEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_fallback_trace_names_the_vendor_reader(self) -> None:
        async def fake_local(url, client):
            return None

        async def fake_post(url, key, payload, client):
            return 200, {"title": "vendor", "content": "body"}

        fields: dict[str, object] = {}

        def fake_trace(event, **values):
            fields.update(values)

        fetch = search_endpoint.page_fetcher(
            None, {"api_base": "https://api.deepseek.com/v1", "api_key": "k"}
        )
        with mock.patch.object(search_endpoint, "local_page", fake_local), \
             mock.patch.object(search_endpoint, "_post_json", fake_post), \
             mock.patch.object(search_endpoint, "_trace", fake_trace):
            await fetch("https://example.test/p")
        self.assertTrue(fields["vendor"])
        self.assertNotIn("local", fields)

"""Edge cases for the image-context HTTP endpoint: the body cap has to hold.

The route reads from the network, so its size limit only means anything if it
is applied before the caller's body has been buffered.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from young_router.proxy import image_context_endpoint

FILLER = b"x" * 4096


class _App:
    def __init__(self) -> None:
        self.handlers = {}

    def add_api_route(self, path, endpoint, methods=None, include_in_schema=True):
        self.handlers[path] = endpoint


class _StreamingRequest:
    """A request whose body arrives in chunks, as it does off a socket."""

    def __init__(self, chunks, authorization: str = "Bearer sk-test") -> None:
        self.headers = {"authorization": authorization}
        self.chunks = list(chunks)
        self.taken = 0

    async def body(self) -> bytes:
        # Reaching for the buffered body is the failure this route must avoid.
        raise AssertionError("the handler buffered the whole body")

    async def stream(self):
        for chunk in self.chunks:
            self.taken += 1
            yield chunk


class ImageContextEndpointBodyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_FETCH": "http",
                    "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_TOKEN": "sk-test",
                },
            )
        )

    def route_handler(self):
        app = _App()
        image_context_endpoint.register(app)
        return app.handlers[image_context_endpoint.MCP_PATHS[0]]

    async def test_an_oversized_body_is_refused_without_reading_it_all(self) -> None:
        request = _StreamingRequest([FILLER] * 4096)  # 16 MB, well past the cap

        response = await self.route_handler()(request)

        self.assertEqual(response.status_code, 413)
        self.assertLess(request.taken, len(request.chunks))

    async def test_a_small_body_is_read_through_the_stream(self) -> None:
        request = _StreamingRequest([b"not json"])

        response = await self.route_handler()(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(request.taken, 1)

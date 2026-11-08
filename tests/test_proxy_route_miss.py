"""A path mistake is named, and is not a 404.

LiteLLM answers an unmatched path with Starlette's ``{"detail":"Not Found"}``.
Both clients this app publishes misread it: Pi renders the body verbatim as the
assistant's answer, and Codex treats an unknown code as retryable -- so a base
URL that carries ``/v1`` twice (or not at all) is retried on the wire while
looking like a model failure on screen.  The tests here pin the parts that make
the replacement safe rather than merely more talkative: a wrong prefix is a
client-side path setting and answers 400, an unknown path keeps its honest 404,
and the hook only runs when nothing matched, so LiteLLM's own routes, its
trailing-slash redirect, its 405 and a 404 raised *inside* a route are all
decided before it.
"""

from __future__ import annotations

import json
import unittest

from young_router.proxy import route_miss


def build_app():  # noqa: ANN201 - FastAPI is a test-time import
    from fastapi import FastAPI, HTTPException

    app = FastAPI()

    @app.get("/v1/models")
    async def models() -> dict[str, object]:
        return {"data": []}

    @app.post("/v1/messages")
    async def messages() -> dict[str, object]:
        return {"type": "message"}

    @app.get("/v1/models/{name}")
    async def model(name: str) -> dict[str, object]:
        raise HTTPException(status_code=404, detail="model not found")

    return app


async def request(app: object, method: str, path: str, *, follow_redirects: bool = True):  # noqa: ANN201
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://gw.test",
        follow_redirects=follow_redirects,
    ) as client:
        return await client.request(method, path)


class ClassificationTests(unittest.TestCase):
    def test_only_a_second_api_prefix_counts_as_doubled(self) -> None:
        for doubled in ("/v1/v1", "/v1/v1/messages", "/v1/v1/responses"):
            self.assertTrue(route_miss.doubled_api_prefix(doubled), doubled)
        # Merely starting with the same letters is a different path.
        for single in ("/v1/messages", "/v1", "/v1x/v1", "/v1/v1x", "/alpha/search", "/"):
            self.assertFalse(route_miss.doubled_api_prefix(single), single)

    def test_the_intended_path_drops_one_prefix(self) -> None:
        self.assertEqual(route_miss.intended_path("/v1/v1/messages"), "/v1/messages")
        self.assertEqual(route_miss.intended_path("/v1/v1/v1/x"), "/v1/v1/x")
        self.assertEqual(route_miss.intended_path("/v1/messages"), "/v1/messages")

    def test_a_bare_wire_path_is_the_missing_prefix_case(self) -> None:
        for bare in ("/messages", "/messages/", "/messages/count_tokens", "/responses"):
            self.assertTrue(route_miss.missing_api_prefix(bare), bare)
        # A prefixed path is not missing it, and an unrelated one is not accused.
        for other in ("/v1/messages", "/nope", "/index.html", "/v1"):
            self.assertFalse(route_miss.missing_api_prefix(other), other)

    def test_the_wire_table_names_no_path_that_does_not_exist(self) -> None:
        """The table is a claim about this port; it must not invent a route."""

        for tail in route_miss.WIRE_TAILS:
            self.assertTrue(tail.startswith(("messages", "responses", "chat", "completions", "models", "embeddings", "images")), tail)


class NotFoundAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_doubled_prefix_is_a_path_setting_error_not_a_404(self) -> None:
        app = build_app()
        self.assertTrue(route_miss.register(app))

        response = await request(app, "POST", "/v1/v1/messages")

        # The status is the point: 404 reads as "this server has no such route"
        # and sends the person looking at the gateway instead of their client.
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("detail", response.json())
        body = response.json()
        self.assertEqual(body["error"]["code"], route_miss.DOUBLED_PREFIX_CODE)
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["type"], "error")
        message = body["error"]["message"]
        self.assertIn("POST /v1/v1/messages", message)
        self.assertIn("POST /v1/messages", message)
        self.assertIn("The /v1 prefix arrived twice", message)
        # One wire adds the prefix itself, the other expects it in the base URL:
        # the sentence has to name the one this path belongs to.
        self.assertIn("origin with no /v1", message)
        self.assertNotIn("one time too many", message)
        self.assertIn("Nothing was sent to a provider", message)

    async def test_a_doubled_openai_wire_asks_for_exactly_one_prefix(self) -> None:
        app = build_app()
        route_miss.register(app)

        response = await request(app, "POST", "/v1/v1/models")

        self.assertEqual(response.status_code, 400)
        message = response.json()["error"]["message"]
        self.assertIn("POST /v1/models", message)
        self.assertIn("one time too many", message)
        self.assertNotIn("origin with no /v1", message)

    async def test_a_missing_prefix_is_named_with_the_path_that_is_served(self) -> None:
        app = build_app()
        route_miss.register(app)

        response = await request(app, "POST", "/messages")

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["code"], route_miss.MISSING_PREFIX_CODE)
        message = body["error"]["message"]
        self.assertIn("POST /messages", message)
        self.assertIn("POST /v1/messages", message)
        self.assertIn("missing the /v1 suffix", message)

    async def test_an_unknown_path_keeps_a_404_and_does_not_accuse_the_client(self) -> None:
        app = build_app()
        route_miss.register(app)

        response = await request(app, "GET", "/nope")

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error"]["code"], route_miss.UNKNOWN_ROUTE_CODE)
        message = body["error"]["message"]
        self.assertIn("GET /nope", message)
        self.assertNotIn("twice", message)
        self.assertIn("Nothing was sent to a provider", message)

    async def test_routes_nothing_matched_keeps_answering(self) -> None:
        app = build_app()
        route_miss.register(app)

        served = await request(app, "GET", "/v1/models")

        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.json(), {"data": []})

    async def test_a_trailing_slash_still_redirects(self) -> None:
        """The hook runs after the redirect, so it cannot turn it into a 400."""

        app = build_app()
        route_miss.register(app)

        response = await request(app, "GET", "/v1/models/", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertTrue(response.headers["location"].endswith("/v1/models"))

    async def test_a_known_path_with_the_wrong_method_is_still_a_405(self) -> None:
        app = build_app()
        route_miss.register(app)

        response = await request(app, "POST", "/v1/models", follow_redirects=False)

        self.assertEqual(response.status_code, 405)
        self.assertIn("GET", response.headers["allow"])

    async def test_a_404_raised_inside_a_route_keeps_its_own_body(self) -> None:
        """A route that says "model not found" is not a path mistake."""

        app = build_app()
        route_miss.register(app)

        response = await request(app, "GET", "/v1/models/ghost", follow_redirects=False)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "model not found"})

    async def test_the_body_survives_a_client_that_parses_json(self) -> None:
        app = build_app()
        route_miss.register(app)

        response = await request(app, "GET", "/v1/v1/responses")

        self.assertEqual(response.headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(
            json.loads(response.text)["error"]["code"],
            route_miss.DOUBLED_PREFIX_CODE,
        )


class RegistrationTests(unittest.TestCase):
    def test_registering_twice_installs_one_hook(self) -> None:
        app = build_app()

        self.assertTrue(route_miss.register(app))
        first = app.router.default
        self.assertTrue(route_miss.register(app))

        self.assertIs(app.router.default, first)

    def test_an_app_without_a_router_is_left_alone(self) -> None:
        class Bare:
            async def __call__(self, scope: object, receive: object, send: object) -> None:
                return None

        self.assertFalse(route_miss.register(Bare()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

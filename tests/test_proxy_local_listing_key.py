"""A local, keyless model listing -- and nothing beyond it."""

from __future__ import annotations

import unittest

from young_router.proxy.local_listing_key import (
    LocalListingKeyMiddleware,
    is_loopback_address,
    local_listing_path,
)


KEY = "sk-local-test"


class _Recorder:
    """The app below the middleware: what it was handed."""

    def __init__(self) -> None:
        self.scope: dict | None = None

    async def __call__(self, scope, receive, send) -> None:
        self.scope = scope


async def _call(middleware: LocalListingKeyMiddleware, **overrides) -> dict:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/models",
        "client": ("127.0.0.1", 51234),
        "headers": [(b"host", b"127.0.0.1:12390")],
    }
    scope.update(overrides)
    await middleware(scope, lambda: None, lambda _message: None)
    assert middleware.app.scope is not None
    return middleware.app.scope


def _headers(scope: dict) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers") or []
    }


class LocalListingKeyTests(unittest.IsolatedAsyncioTestCase):
    def middleware(self) -> LocalListingKeyMiddleware:
        return LocalListingKeyMiddleware(_Recorder(), key_provider=lambda: KEY)

    async def test_a_local_listing_read_gets_the_key(self) -> None:
        for path in ("/v1/models", "/models", "/codex/models", "/v1/models/"):
            scope = await _call(self.middleware(), path=path)
            self.assertEqual(f"Bearer {KEY}", _headers(scope)["authorization"], path)

    async def test_a_head_request_is_answered_too(self) -> None:
        scope = await _call(self.middleware(), method="HEAD")
        self.assertEqual(f"Bearer {KEY}", _headers(scope)["authorization"])

    async def test_a_remote_peer_keeps_the_ordinary_refusal(self) -> None:
        for peer in ("10.0.0.5", "203.0.113.9", "::ffff:10.0.0.5"):
            scope = await _call(self.middleware(), client=(peer, 51234))
            self.assertNotIn("authorization", _headers(scope), peer)

    async def test_a_foreign_host_name_is_not_local(self) -> None:
        """A page on any site can reach 127.0.0.1; it cannot change ``Host``."""

        for host in ("attacker.example", "evil.test:12390", "127.0.0.1.nip.io"):
            scope = await _call(self.middleware(), headers=[(b"host", host.encode())])
            self.assertNotIn("authorization", _headers(scope), host)

    async def test_a_missing_host_is_not_local(self) -> None:
        scope = await _call(self.middleware(), headers=[])
        self.assertNotIn("authorization", _headers(scope))

    async def test_only_a_listing_path_is_affected(self) -> None:
        for path in ("/v1/chat/completions", "/v1/messages", "/ipc", "/v1/models/1", "/health"):
            scope = await _call(self.middleware(), path=path)
            self.assertNotIn("authorization", _headers(scope), path)

    async def test_a_listing_write_is_not_affected(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            scope = await _call(self.middleware(), method=method)
            self.assertNotIn("authorization", _headers(scope), method)

    async def test_a_stated_credential_is_never_replaced(self) -> None:
        """A wrong key must still be refused, not upgraded to the right one."""

        scope = await _call(self.middleware(), headers=[
            (b"host", b"127.0.0.1:12390"),
            (b"authorization", b"Bearer sk-wrong"),
        ])
        self.assertEqual("Bearer sk-wrong", _headers(scope)["authorization"])
        scope = await _call(self.middleware(), headers=[
            (b"host", b"127.0.0.1:12390"),
            (b"x-api-key", b"sk-wrong"),
        ])
        self.assertNotIn("authorization", _headers(scope))
        self.assertEqual("sk-wrong", _headers(scope)["x-api-key"])

    async def test_an_unreadable_key_leaves_the_request_alone(self) -> None:
        middleware = LocalListingKeyMiddleware(_Recorder(), key_provider=lambda: "")
        scope = await _call(middleware)
        self.assertNotIn("authorization", _headers(scope))

    async def test_other_scope_types_pass_through(self) -> None:
        middleware = self.middleware()
        await middleware({"type": "lifespan"}, None, None)
        # Passed through untouched: no headers were touched on the way down.
        self.assertEqual({"type": "lifespan"}, middleware.app.scope)

    def test_loopback_names(self) -> None:
        for value in ("127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "LOCALHOST"):
            self.assertTrue(is_loopback_address(value), value)
        for value in ("", "0.0.0.0", "10.0.0.5", "example.com", None):
            self.assertFalse(is_loopback_address(value), value)

    def test_listing_paths(self) -> None:
        self.assertTrue(local_listing_path("/v1/models"))
        self.assertTrue(local_listing_path("/codex/models"))
        self.assertTrue(local_listing_path("/models"))
        self.assertFalse(local_listing_path("/v1/models/abc"))
        self.assertFalse(local_listing_path("/v1"))


if __name__ == "__main__":
    unittest.main()

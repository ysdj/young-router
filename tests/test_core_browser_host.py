"""The browser host: one page, same Core, same shared UI.

Two things are asserted here, and they are the two that can silently rot:

- The browser session is a cookie Core sets and accepts, and a request that
  states two disagreeing credentials is refused rather than resolved. A page
  cannot read the header the native hosts use, so this is the only credential
  path the third host has.
- The page Core serves is same-origin with `/ipc`, is bounded to loopback host
  names, and cannot be talked into reading a file outside its own bundle. That
  is what makes the browser host need no CORS answer, no second port, and no
  separate auth scheme.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from young_router.core import CoreIPCServer, CoreStore, IPCError, MemoryDomain
from young_router.core import __main__ as core_main
from unittest import mock

from young_router.core import relay_browser, relay_mirror
from young_router.core.service import BROWSER_LOGIN_REAP_SECONDS
from young_router.core.domains.relay_accounts import RelayAccountsDomain, RelayAccountsError, RelaySessionLimitError
from young_router.core.domains.runtime import RuntimeSettingsDomain, WEB_PASSWORD_SETTING
from young_router.core.ipc import (
    SESSION_TOKEN_TTL_SECONDS,
    WEB_SESSION_STORE_FILENAME,
    browser_session_digest,
)


def _hello(
    server: CoreIPCServer,
    *,
    bootstrap: str | None = None,
    cookie: str | None = None,
    forwarded_proto: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    endpoint = server.endpoint
    connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if bootstrap is not None:
        headers["Authorization"] = f"Bearer {bootstrap}"
    if cookie is not None:
        headers["Cookie"] = cookie
    if forwarded_proto is not None:
        # What the proxy states about the hop the browser used.
        headers["X-Forwarded-Proto"] = forwarded_proto
    try:
        connection.request("POST", "/ipc/hello", body=b"{}", headers=headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


class BrowserSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.server.set_web_password("s3cret-word")
        self.addCleanup(self.server.stop)

    def test_native_hello_sends_only_the_session_header(self) -> None:
        status, headers, _body = _hello(self.server, bootstrap=self.server.bootstrap_token)
        self.assertEqual(200, status)
        self.assertIn("x-litellm-core-session", headers)
        self.assertNotIn("set-cookie", headers)

    def test_web_login_sets_an_httponly_strict_cookie(self) -> None:
        status, headers, _body = _login_response(self.server, "s3cret-word")
        self.assertEqual(200, status)
        self.assertNotIn("x-litellm-core-session", headers)
        cookie = headers.get("set-cookie", "")
        self.assertIn("yr-core-session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        # A page's lease is thirty days: a person who typed the key once keeps
        # the page, across a closed browser and across a replaced Core.
        self.assertIn(f"Max-Age={30 * 24 * 60 * 60}", cookie)
        # A loopback page is plain http, so a `Secure` cookie there would never
        # come back.
        self.assertNotIn("Secure", cookie)

    def test_a_published_page_gets_a_secure_cookie(self) -> None:
        # The proxy forwards the scheme the browser used, and a page reached
        # over TLS must not hand its 30-day cookie to a plaintext hop.
        status, headers, _body = _hello(
            self.server,
            forwarded_proto="https",
            cookie=_login(self.server, "s3cret-word"),
        )
        self.assertEqual(200, status)
        self.assertNotIn("x-litellm-core-session", headers)
        self.assertIn("Secure", headers.get("set-cookie", ""))

    def test_a_cookie_alone_authenticates_a_request(self) -> None:
        cookie = _login(self.server, "s3cret-word")
        status, _headers, payload = _post_ipc(self.server, cookie, "snapshot")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

    def test_a_header_and_a_cookie_that_disagree_are_refused(self) -> None:
        cookie = _login(self.server, "s3cret-word")
        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            connection.request(
                "POST",
                "/ipc",
                body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer not-this-session",
                    "Cookie": cookie,
                },
            )
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
        # Two credentials that disagree describe two different callers. The
        # request is refused instead of being resolved to whichever one the
        # handler happened to read first.
        self.assertEqual(401, response.status)

    def test_the_cookie_name_is_not_reusable_by_another_reader(self) -> None:
        # A page is one origin, and the cookie is scoped to it. Naming the cookie
        # in the response and in the reader from one constant is what keeps a
        # second reader from having to guess its spelling.
        from young_router.core.ipc import BROWSER_SESSION_COOKIE

        self.assertEqual("yr-core-session", BROWSER_SESSION_COOKIE)


class BrowserSessionLifetimeTests(unittest.TestCase):
    """What keeps a page signed in, and what a person's sign-out means.

    Two failures this pins down, both seen on a hosted install: the browser's
    cookie was a session cookie, so closing the browser was a sign-out; and the
    lease lived only in the process, so every deploy -- which replaces Core --
    put the password form in front of a person whose key had never been wrong.
    """

    def setUp(self) -> None:
        self.core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sessions = self.root / WEB_SESSION_STORE_FILENAME
        self.bundle = self.root / "web"
        self.bundle.mkdir()
        (self.bundle / "index.html").write_text(
            '<!doctype html><title>Young Router</title><script src="/bundle.js"></script>\n',
            encoding="utf-8",
        )
        (self.bundle / "bundle.js").write_text("export {};\n", encoding="utf-8")
        self.server = self._start()

    def _start(self) -> CoreIPCServer:
        server = CoreIPCServer(self.core, web_sessions_path=self.sessions)
        server.start()
        self.addCleanup(server.stop)
        server.enable_web_host(self.bundle, password="s3cret-word")
        return server

    def _replacement(self) -> CoreIPCServer:
        """A Core the way a deploy or an app restart leaves one: no sessions."""

        self.server.stop()
        self.server = self._start()
        return self.server

    def _page(self, server: CoreIPCServer, cookie: str) -> str:
        # A published name, so the page is gated: a loopback page is the
        # owner's own and never asks for a key.
        status, _headers, body = _get(server, "/", cookie=cookie, page_host="router.example.com")
        self.assertEqual(200, status)
        return body.decode("utf-8")

    def test_a_page_stays_signed_in_across_a_replaced_core(self) -> None:
        cookie = _login(self.server, "s3cret-word")
        server = self._replacement()

        # A reload after the deploy: the cookie is the only credential there is.
        text = self._page(server, cookie)
        self.assertIn("bundle.js", text)
        self.assertNotIn('type="password"', text)
        # The pane's own calls accept it too, or the shell would draw empty.
        self.assertEqual(200, _hello(server, cookie=cookie)[0])
        self.assertEqual(200, _post_ipc(server, cookie, "snapshot")[0])

    def test_polling_renews_the_browser_cookie_before_its_original_expiry(self) -> None:
        wall, monotonic = time.time(), time.monotonic()
        with mock.patch("young_router.core.ipc.time", wraps=time) as clock:
            clock.time.return_value = wall
            clock.monotonic.return_value = monotonic
            status, headers, _body = _login_response(self.server, "s3cret-word")
            self.assertEqual(200, status)
            issued = SimpleCookie(headers["set-cookie"])["yr-core-session"]
            max_age = int(issued["max-age"])
            cookie = f"{issued.key}={issued.value}"
            status, headers, reply = _post_ipc(self.server, cookie, "subscribe")
            self.assertEqual(200, status)
            self.assertNotIn("set-cookie", headers)
            poll = f'/ipc/events?timeout=0&subscription_id={reply["result"]["subscription_id"]}'

            clock.time.return_value = wall + max_age - 1
            clock.monotonic.return_value = monotonic + max_age - 1
            status, headers, _body = _get(self.server, poll, cookie=cookie, forwarded_proto="https")
            self.assertEqual(200, status)
            self.assertNotIn("x-litellm-core-session", headers)
            renewed = SimpleCookie(headers["set-cookie"])[issued.key]
            self.assertEqual(issued.value, renewed.value)
            self.assertEqual(str(max_age), renewed["max-age"])
            self.assertTrue(renewed["httponly"])
            self.assertEqual("Strict", renewed["samesite"])
            self.assertTrue(renewed["secure"])
            self.assertNotIn("set-cookie", _get(self.server, poll, cookie=cookie)[1])

            clock.time.return_value = wall + max_age + 1
            clock.monotonic.return_value = monotonic + max_age + 1
            server = self._replacement()
            self.assertEqual(200, _post_ipc(server, cookie, "snapshot")[0])

    def test_hello_refuses_disagreeing_credentials_without_consuming_bootstrap(self) -> None:
        cookie = _login(self.server, "s3cret-word")
        bootstrap = self.server.bootstrap_token
        status, headers, body = _hello(self.server, bootstrap=bootstrap, cookie=cookie)
        self.assertEqual(401, status)
        self.assertNotIn("set-cookie", headers)
        self.assertEqual("unauthorized", json.loads(body)["error"]["code"])
        self.assertEqual(200, _hello(self.server, bootstrap=bootstrap)[0])
        self.assertEqual(200, _hello(self.server, cookie=cookie)[0])

    def test_the_record_is_a_digest_and_does_not_grow_per_call(self) -> None:
        cookie = _login(self.server, "s3cret-word")
        token = cookie.split("=", 1)[1]
        payload = json.loads(self.sessions.read_text(encoding="utf-8"))
        self.assertNotIn(token, self.sessions.read_text(encoding="utf-8"))
        self.assertEqual(1, len(payload["sessions"]))
        self.assertEqual(browser_session_digest(token), payload["sessions"][0]["digest"])
        # A lease renewed by ordinary use must not add a record per request.
        for _ in range(3):
            self.assertEqual(200, _hello(self.server, cookie=cookie)[0])
        self.assertEqual(1, len(json.loads(self.sessions.read_text(encoding="utf-8"))["sessions"]))

    def test_a_cookie_no_record_names_is_the_gate_again(self) -> None:
        cookie = _login(self.server, "s3cret-word")
        self.sessions.unlink()
        server = self._replacement()
        self.assertIn('type="password"', self._page(server, cookie))
        self.assertEqual(401, _hello(server, cookie=cookie)[0])

    def test_an_expired_record_is_not_resumed(self) -> None:
        cookie = _login(self.server, "s3cret-word")
        payload = json.loads(self.sessions.read_text(encoding="utf-8"))
        payload["sessions"][0]["expires_at"] = int(time.time()) - 60
        self.sessions.write_text(json.dumps(payload), encoding="utf-8")
        server = self._replacement()
        self.assertIn('type="password"', self._page(server, cookie))
        self.assertEqual(401, _hello(server, cookie=cookie)[0])

    def test_signing_out_ends_the_lease_the_cookie_and_the_record(self) -> None:
        cookie = _login(self.server, "s3cret-word")
        status, headers, body = _logout(self.server, cookie)
        self.assertEqual(200, status)
        self.assertTrue(json.loads(body)["ok"])
        # The browser is told to delete the cookie it is holding.
        answer = headers.get("set-cookie", "")
        self.assertIn("yr-core-session=;", answer)
        self.assertIn("Max-Age=0", answer)
        # The page it reloads is the gate, and its `/ipc` calls are refused.
        self.assertIn('type="password"', self._page(self.server, cookie))
        self.assertEqual(401, _hello(self.server, cookie=cookie)[0])
        # A copied cookie does not come back after a restart either: the record
        # it would have been resumed from is gone.
        server = self._replacement()
        self.assertIn('type="password"', self._page(server, cookie))
        self.assertEqual([], json.loads(self.sessions.read_text(encoding="utf-8"))["sessions"])

    def test_signing_out_twice_is_still_signed_out(self) -> None:
        cookie = _login(self.server, "s3cret-word")
        self.assertEqual(200, _logout(self.server, cookie)[0])
        self.assertEqual(200, _logout(self.server, cookie)[0])

    def test_a_native_host_keeps_its_thirty_minute_lease(self) -> None:
        _status, headers, _body = _hello(self.server, bootstrap=self.server.bootstrap_token)
        session = headers["x-litellm-core-session"]
        with self.server._lock:
            lease = self.server._sessions[session]
            self.assertFalse(lease.browser)
            self.assertLessEqual(lease.expires_at - time.monotonic(), SESSION_TOKEN_TTL_SECONDS)
            self.assertEqual({}, self.server._web_sessions)
        # Nothing about a native host's lease is written down: it is this
        # process's, and a restart is meant to end it.
        self.assertFalse(self.sessions.exists())

    def test_both_ttls_survive_a_restart_in_their_own_way(self) -> None:
        # The page's lease is the recorded one; the native lease is not, because
        # it never was: a replaced Core answers a native host's stale token with
        # 401 and the host mints a new one from its own bootstrap credential.
        cookie = _login(self.server, "s3cret-word")
        _status, headers, _body = _hello(self.server, bootstrap=self.server.bootstrap_token)
        native = headers["x-litellm-core-session"]
        server = self._replacement()
        self.assertEqual(200, _hello(server, cookie=cookie)[0])
        self.assertEqual(401, _hello(server, bootstrap=native)[0])


class WebSessionStorePathTests(unittest.TestCase):
    def test_the_record_lives_beside_the_metadata_or_in_the_runtime_root(self) -> None:
        parser = core_main.build_parser()
        self.assertEqual(
            Path("/opt/yr/.litellm-runtime") / WEB_SESSION_STORE_FILENAME,
            core_main._web_session_store_path(
                parser.parse_args(["--metadata", "/opt/yr/.litellm-runtime/metadata.json"])
            ),
        )
        self.assertEqual(
            Path("/srv/example/.litellm-runtime") / WEB_SESSION_STORE_FILENAME,
            core_main._web_session_store_path(parser.parse_args(["--runtime-root", "/srv/example"])),
        )


class BrowserPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "index.html").write_text("<!doctype html><title>Young Router</title><script src=\"/bundle.js\"></script>\n", encoding="utf-8")
        (self.root / "bundle.js").write_text("export {};\n", encoding="utf-8")
        (self.root / "editor.html").write_text("<!doctype html><title>Editor</title>\n", encoding="utf-8")
        (self.root.parent / "outside.txt").write_text("not part of the bundle\n", encoding="utf-8")

    def _get(self, path: str, *, host: str | None = None, cookie: str | None = None, page_host: str | None = None, accept_encoding: str | None = None) -> tuple[int, dict[str, str], bytes]:
        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        headers: dict[str, str] = {"Host": host} if host is not None else {}
        if accept_encoding is not None:
            headers["Accept-Encoding"] = accept_encoding
        # The name the browser typed, which the proxy forwards.  Stating a real
        # hostname is what makes this request a published page rather than the
        # owner's own: the loopback page is served without a key.
        if page_host is not None:
            headers["X-Young-Router-Page-Host"] = page_host
        if cookie is not None:
            headers["Cookie"] = cookie
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def test_no_bundle_means_the_listener_serves_no_page(self) -> None:
        status, _headers, _body = self._get("/")
        self.assertEqual(404, status)

    def test_a_staged_bundle_is_served_from_the_same_listener_as_v1(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        cookie = _login(self.server, "s3cret-word")
        status, headers, body = self._get("/", cookie=cookie)
        self.assertEqual(200, status)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"Young Router", body)
        # The page is one document: the route lives in the fragment, so every
        # route is this same request.
        self.assertEqual((200, headers["content-type"]), (self._get("/", cookie=cookie)[0], self._get("/", cookie=cookie)[1]["content-type"]))
        # The page is not cacheable and not framable, and says so.
        self.assertEqual("no-store", headers["cache-control"])
        self.assertEqual("DENY", headers["x-frame-options"])
        self.assertEqual("nosniff", headers["x-content-type-options"])
        # The script is cacheable: it is addressed by content, not by route.
        script_status, script_headers, _script = self._get("/bundle.js")
        self.assertEqual(200, script_status)
        self.assertIn("javascript", script_headers["content-type"])
        self.assertIn("immutable", script_headers["cache-control"])

    def test_a_script_travels_in_whichever_encoding_both_ends_know(self) -> None:
        """The bundle is 1.2 MB of text, so the wire carries a fifth of it.

        The encoding is decided at build time and shipped as a sibling of the
        asset it was built from, so Core only ever has to pick: brotli when the
        browser reads it, gzip otherwise, and the file itself when the browser
        says neither.  Three things have to hold for that to be safe on a
        cacheable script -- the body, the header that names its codec, and
        `Vary`, which is what keeps a cache from handing one browser's
        encoding to the next.
        """

        import gzip
        import zlib

        source = "export {};\n" * 400
        (self.root / "bundle.js").write_text(source, encoding="utf-8")
        brotli = zlib.compress(source.encode("utf-8"))
        geed = gzip.compress(source.encode("utf-8"))
        (self.root / "bundle.js.br").write_bytes(brotli)
        (self.root / "bundle.js.gz").write_bytes(geed)
        self.server.enable_web_host(self.root, password="s3cret-word")

        status, headers, body = self._get("/bundle.js", accept_encoding="gzip, deflate, br")
        self.assertEqual(200, status)
        self.assertEqual("br", headers["content-encoding"])
        self.assertEqual("Accept-Encoding", headers["vary"])
        self.assertEqual(brotli, body)
        self.assertEqual(str(len(brotli)), headers["content-length"])
        # Brotli wins even when the browser lists gzip first: it is the smaller
        # of the two for this bundle, and the order of the header is a client's
        # preference among equals, not a mandate.
        status, headers, body = self._get("/bundle.js", accept_encoding="gzip, br")
        self.assertEqual(("br", brotli), (headers["content-encoding"], body))

        status, headers, body = self._get("/bundle.js", accept_encoding="gzip, deflate")
        self.assertEqual((200, "gzip", geed), (status, headers["content-encoding"], body))

        # A browser that accepts no encoding is served the file, and the answer
        # still varies on the header -- the cache in front of it has to know
        # this URL has more than one shape.
        status, headers, body = self._get("/bundle.js")
        self.assertEqual(200, status)
        self.assertNotIn("content-encoding", headers)
        self.assertEqual("Accept-Encoding", headers["vary"])
        self.assertEqual(source.encode("utf-8"), body)

        # A stated refusal is a refusal: `br;q=0` asks for the other codec.
        _status, headers, _body = self._get("/bundle.js", accept_encoding="br;q=0, gzip")
        self.assertEqual("gzip", headers["content-encoding"])
        _status, headers, _body = self._get("/bundle.js", accept_encoding="*")
        self.assertEqual("br", headers["content-encoding"])

    def test_a_precompressed_sibling_is_not_an_asset_of_its_own(self) -> None:
        """`bundle.js.br` is not a URL, it is an encoding of another one.

        Nothing in the bundle's own list of content types names it, so asking
        for it by name is the same 404 as asking for any other file this host
        does not serve -- instead of a second copy of the script under a name
        no cache header was written for.
        """

        import zlib

        (self.root / "bundle.js.br").write_bytes(zlib.compress(b"export {};\n"))
        self.server.enable_web_host(self.root, password="s3cret-word")
        status, _headers, _body = self._get("/bundle.js.br")
        self.assertEqual(404, status)

    def test_a_page_with_no_sibling_is_answered_as_stored(self) -> None:
        """A bundle staged before this build has no siblings, and still serves.

        The desktop install copies whatever `dist` holds, and a host that
        installed a page from a release may be one build behind the encoding.
        Nothing here may read as missing: no sibling means the file itself,
        and no `Vary` either, because the answer does not depend on the header.
        """

        self.server.enable_web_host(self.root, password="s3cret-word")
        status, headers, body = self._get("/bundle.js", accept_encoding="br, gzip")
        self.assertEqual(200, status)
        self.assertNotIn("content-encoding", headers)
        self.assertNotIn("vary", headers)
        self.assertEqual(b"export {};\n", body)

    def test_only_the_editor_page_may_be_framed_and_only_by_this_origin(self) -> None:
        """The shell frames exactly one document, and no other origin may.

        The raw editor is a page of its own because the shell's CSP would
        refuse its inline Ace bundle and a `srcdoc`/blob frame inherits that
        policy.  It is therefore answered `SAMEORIGIN` while every other asset
        -- the shell included -- keeps `DENY`.
        """

        self.server.enable_web_host(self.root, password="s3cret-word")
        cookie = _login(self.server, "s3cret-word")
        editor_status, editor_headers, editor_body = self._get("/ui/editor.html")
        self.assertEqual(200, editor_status)
        self.assertEqual("SAMEORIGIN", editor_headers["x-frame-options"])
        self.assertIn(b"Editor", editor_body)
        for path in ("/", "/bundle.js"):
            _status, headers, _body = self._get(path, cookie=cookie)
            self.assertEqual("DENY", headers["x-frame-options"])

    def test_the_mount_root_redirects_so_the_page_finds_its_script(self) -> None:
        """`/ui` without the slash asks the wrong root for the shell's script.

        The shell names `./bundle.js`.  A browser left at `/ui` resolves that
        against the proxy's root, where LiteLLM answers 404 -- a shell with no
        script, which is a blank page and nothing in it to read.  The address
        Core publishes already ends in the slash; this makes the spelling a
        person types reach the same page.
        """

        self.server.enable_web_host(self.root, password="s3cret-word")
        cookie = _login(self.server, "s3cret-word")
        status, headers, body = self._get("/ui", cookie=cookie)
        self.assertEqual(308, status)
        self.assertEqual("/ui/", headers["location"])
        self.assertEqual("no-store", headers["cache-control"])
        self.assertEqual(b"", body)
        # The address it points at is the page, and the script under that mount
        # is the one the shell names.
        page_status, _page_headers, page = self._get("/ui/", cookie=cookie)
        self.assertEqual(200, page_status)
        self.assertIn("bundle.js", page.decode("utf-8"))
        script_status, _script_headers, _script = self._get("/ui/bundle.js")
        self.assertEqual(200, script_status)

    def test_the_gate_is_drawn_in_the_app_language_and_names_nothing(self) -> None:
        """The form is not a place to learn what the service is, or its language.

        Anyone who reaches this port sees these bytes before proving anything,
        so they carry the next step and nothing else: no project name, no
        pointer to where the key is written.  The language is the app's own
        resolution, read per request like the key, so a window set to Chinese
        does not hand the browser an English form.
        """

        chosen = ["zh-Hans"]
        self.server.enable_web_host(
            self.root,
            password="s3cret-word",
            locale_provider=lambda: chosen[0],
        )
        status, _headers, body = self._get("/", page_host="router.example.com")
        text = body.decode("utf-8")
        self.assertEqual(200, status)
        self.assertIn('lang="zh-Hans"', text)
        self.assertIn("输入访问密钥以继续。", text)
        self.assertIn("登录", text)
        for leak in ("Young Router", "漾路由", "General", "常规", "语言设置"):
            self.assertNotIn(leak, text)
        # The same page in the app's other language, without a restart.
        chosen[0] = "en"
        text = self._get("/", page_host="router.example.com")[2].decode("utf-8")
        self.assertIn('lang="en"', text)
        self.assertIn("Enter the access key to continue.", text)
        self.assertNotIn("Young Router", text)

    def test_the_gate_follows_the_browser_only_when_the_preference_is_auto(self) -> None:
        """自动 means the platform, and on the page the platform is the browser.

        The page reads ``navigator.languages`` for exactly this, so the one
        document a browser meets before the app must read the same list.  A
        host whose own ``LANG`` is C.UTF-8 used to hand a Chinese browser an
        English form and then the Chinese app; a stated preference still wins
        over whatever the browser asks for, because that is the point of
        stating it.
        """

        preference = {"choice": "system", "resolved": "en"}
        self.server.enable_web_host(
            self.root,
            password="s3cret-word",
            locale_provider=lambda: preference,
        )
        # The browser's list, ranked by its own q values, not by header order.
        text = _get(
            self.server,
            "/",
            page_host="router.example.com",
            accept_language="fr-FR,zh-CN;q=0.9,en;q=0.4",
        )[2].decode("utf-8")
        self.assertIn('lang="zh-Hans"', text)
        self.assertIn("输入访问密钥以继续。", text)
        # A browser that asks for neither of the app's languages falls back to
        # the machine's own resolution, which this provider states as en.
        text = _get(self.server, "/", page_host="router.example.com", accept_language="fr-FR")[2].decode("utf-8")
        self.assertIn('lang="en"', text)
        # ... and the Chinese half of the same pair.
        preference["resolved"] = "zh-Hans"
        text = _get(self.server, "/", page_host="router.example.com", accept_language="en-US")[2].decode("utf-8")
        self.assertIn('lang="en"', text)
        text = _get(self.server, "/", page_host="router.example.com")[2].decode("utf-8")
        self.assertIn('lang="zh-Hans"', text)

        # A stated choice is the answer even when the browser says otherwise:
        # the person chose it, and the gate is not a place to overrule them.
        self.server.enable_web_host(
            self.root,
            password="s3cret-word",
            locale_provider=lambda: {"choice": "zh-Hans", "resolved": "zh-Hans"},
        )
        text = _get(self.server, "/", page_host="router.example.com", accept_language="en-US,en;q=0.9")[2].decode("utf-8")
        self.assertIn('lang="zh-Hans"', text)
        self.server.enable_web_host(
            self.root,
            password="s3cret-word",
            locale_provider=lambda: {"choice": "en", "resolved": "en"},
        )
        text = _get(self.server, "/", page_host="router.example.com", accept_language="zh-CN,zh;q=0.9")[2].decode("utf-8")
        self.assertIn('lang="en"', text)

    def test_the_published_url_carries_no_credential(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        url = self.server.web_host_url
        self.assertEqual(f"http://127.0.0.1:{self.server.port}/ui/", url)
        # The password is typed into the page. A credential in the address bar
        # is the one nobody ever rotates.
        self.assertNotIn("password", url)
        self.assertNotIn("bootstrap", url)

    def test_a_page_with_no_password_is_served_no_page_to_a_published_visitor(self) -> None:
        # A page published under a real hostname with no key is a closed page:
        # the form is served and nothing can pass it.
        self.server.enable_web_host(self.root, password="")
        self.assertEqual("", self.server.web_host_url)
        status, _headers, _body = _get(self.server, "/", page_host="router.example.com")
        self.assertEqual(200, status)
        body = _body.decode("utf-8")
        self.assertIn('type="password"', body)
        self.assertNotIn("bundle.js", body)

    def test_the_login_page_is_served_instead_of_the_app_before_signing_in(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        status, _headers, body = _get(self.server, "/", page_host="router.example.com")
        self.assertEqual(200, status)
        self.assertIn('id="password"', body.decode("utf-8"))
        # The app's shell is not handed out yet, so nothing in it can run.
        self.assertNotIn("bundle.js", body.decode("utf-8"))

    def test_the_page_on_this_machine_needs_no_key(self) -> None:
        """127.0.0.1 is the owner's own door.

        A key on a loopback page asks the person who runs this machine to
        prove it to themselves, and the address in General -- the one the app
        opens -- becomes a form rather than the settings.  The gate exists for
        a page published behind a reverse proxy; a page nobody can reach but
        the owner is served the app, with the session cookie its `/ipc` calls
        need.
        """

        self.server.enable_web_host(self.root, password="s3cret-word")
        for host in ("127.0.0.1:12390", "localhost:12390", "[::1]:12390"):
            # A direct request to Core carries no proxy marker.  This is the
            # actual loopback boundary; the published proxy path is tested
            # separately because its Host value is supplied by the client.
            status, headers, body = self._get("/", host=host)
            self.assertEqual(200, status)
            text = body.decode("utf-8")
            self.assertIn("bundle.js", text)
            self.assertNotIn('type="password"', text)
            # The pane reads Core over `/ipc` with this cookie: without it the
            # page would draw its shell and then every pane would be empty.
            cookie = headers.get("set-cookie", "")
            self.assertIn("yr-core-session=", cookie)
            token = cookie.split("yr-core-session=", 1)[1].split(";", 1)[0]
            self.assertTrue(self.server._valid_session(token), "the page's cookie is a live session")

    def test_a_published_loopback_host_still_meets_the_password(self) -> None:
        """A proxy marker keeps a client-supplied loopback Host published.

        The proxy connects to Core over loopback for every request, so the
        marker is the boundary between a direct local page and a published
        page.  An external visitor must not turn the latter into the former by
        sending ``Host: localhost``.
        """

        self.server.enable_web_host(self.root, password="s3cret-word")
        status, _headers, body = _get(self.server, "/", page_host="localhost:12390")
        self.assertEqual(200, status)
        text = body.decode("utf-8")
        self.assertIn('type="password"', text)
        self.assertNotIn("bundle.js", text)

    def test_a_proxied_loopback_name_is_still_the_owner(self) -> None:
        """No forwarded name at all is a direct hit, which is loopback too."""

        self.server.enable_web_host(self.root, password="s3cret-word")
        status, headers, body = _get(self.server, "/")
        self.assertEqual(200, status)
        self.assertIn("bundle.js", body.decode("utf-8"))
        self.assertIn("yr-core-session=", headers.get("set-cookie", ""))

    def test_the_password_opens_a_session_and_then_the_app_is_served(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        cookie = _login(self.server, "s3cret-word")
        self.assertIn("yr-core-session=", cookie)
        status, _headers, body = _get(self.server, "/", cookie=cookie)
        self.assertEqual(200, status)
        # The signed-in page is the app, not the form.
        self.assertIn("bundle.js", body.decode("utf-8"))
        self.assertNotIn('type="password"', body.decode("utf-8"))

    def test_a_wrong_password_is_refused_and_leaves_no_cookie(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        status, headers, body = _login_response(self.server, "not-the-password")
        self.assertEqual(401, status)
        self.assertNotIn("yr-core-session=", headers.get("set-cookie", ""))
        self.assertEqual("invalid_password", json.loads(body)["error"]["code"])

    def test_the_session_cookie_alone_reenters_without_the_password(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        cookie = _login(self.server, "s3cret-word")
        token = cookie.split("yr-core-session=", 1)[1].split(";", 1)[0]
        # A reload: no header, no password, only the cookie this page holds.
        status, _headers, _body = _hello(self.server, cookie=cookie)
        self.assertEqual(200, status)
        self.assertTrue(token)

    def test_an_edited_password_applies_to_the_next_sign_in(self) -> None:
        self.server.enable_web_host(self.root, password="first-one")
        self.assertEqual(401, _login_response(self.server, "second-one")[0])
        self.server.set_web_password("second-one")
        self.assertEqual(200, _login_response(self.server, "second-one")[0])
    def test_a_terminal_launch_signs_in_without_the_password(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word", auto_login=True)
        url = self.server.web_host_url
        self.assertIn("#launch=", url)
        token = url.split("#launch=", 1)[1]
        status, headers, _body = _launch_response(self.server, token)
        self.assertEqual(200, status)
        cookie = headers.get("set-cookie", "").split(";", 1)[0]
        self.assertIn("yr-core-session=", cookie)
        # And the app itself is served on that cookie, with no password typed.
        app_status, _app_headers, body = self._get("/", cookie=cookie)
        self.assertEqual(200, app_status)
        self.assertIn("bundle.js", body.decode("utf-8"))

    def test_the_launch_address_keeps_working_for_that_process(self) -> None:
        # A person who reloads the tab a terminal opened must not be asked for
        # a password the terminal never gave them.
        self.server.enable_web_host(self.root, password="s3cret-word", auto_login=True)
        token = self.server.web_host_url.split("#launch=", 1)[1]
        for _ in range(3):
            self.assertEqual(200, _launch_response(self.server, token)[0])

    def test_another_browser_still_meets_the_password(self) -> None:
        # The launch credential is one address's, not the port's: a second
        # visitor gets the form, and a wrong launch token opens nothing.
        self.server.enable_web_host(self.root, password="s3cret-word", auto_login=True)
        self.assertIn('type="password"', self._get("/", page_host="router.example.com")[2].decode("utf-8"))
        self.assertEqual(401, _launch_response(self.server, "not-the-token")[0])

    def test_a_native_install_has_no_launch_credential(self) -> None:
        # auto_login is a terminal's option; a host-launched Core prints an
        # ordinary address, and nothing carries a way past the password.
        self.server.enable_web_host(self.root, password="s3cret-word")
        self.assertEqual(f"http://127.0.0.1:{self.server.port}/ui/", self.server.web_host_url)
        self.assertEqual(401, _launch_response(self.server, "anything")[0])

    def test_a_non_loopback_host_is_not_answered(self) -> None:
        self.server.enable_web_host(self.root)
        # A DNS-rebinding page presents its own attacker domain as `Host`, and
        # that is the request this refuses.
        self.assertEqual(404, self._get("/", host="attacker.example")[0])

    def test_a_path_cannot_escape_the_bundle(self) -> None:
        self.server.enable_web_host(self.root)
        self.assertEqual(404, self._get("/../outside.txt")[0])
        self.assertEqual(404, self._get("/%2E%2E/outside.txt")[0])
        self.assertEqual(404, self._get("/missing.js")[0])

    def test_staging_a_directory_without_a_page_is_refused(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(IPCError):
            self.server.enable_web_host(empty)

    def test_a_staged_bundle_still_hides_the_native_hosts_token(self) -> None:
        # Staging a page does not create a second way for the native hosts'
        # one-shot credential to be spent: a page proves itself with the
        # password, and the bundle it loads never contains the token.
        self.server.enable_web_host(self.root, password="s3cret-word")
        _status, _headers, body = _get(self.server, "/bundle.js")
        self.assertNotIn(self.server.bootstrap_token.encode(), body)
        self.assertEqual("", self.server.web_host_url.split("/")[-1])


class BrowserPageAddressTests(unittest.TestCase):
    """The address a person lands on is a pane, and the tab says what Core says.

    Two failures this pins, both seen in a browser:

    - A launch address is `#launch=<token>`.  The sign-in page spends the token
      and strips the fragment, but a browser that already holds a session is
      served the app directly, so the token became the *route*: the shell drew
      its sidebar over a pane that does not exist.
    - The tab title read `${app}: running` — the protocol's own spelling of the
      service state, beside a Chinese app title.
    - The tab was titled "Young Router" by the template before the page knew
      which language it was speaking: a name a Chinese reader then watched
      change to its own.
    """

    def setUp(self) -> None:
        self.shared = Path(__file__).resolve().parents[1] / "rn/packages/shared/src"

    def test_a_fragment_that_names_no_pane_is_not_a_route(self) -> None:
        entry = (self.shared / "platformEntry.web.ts").read_text(encoding="utf-8")
        # The launch token is not a place: it resolves to the pane the app opens
        # on, and the address is rewritten to name that pane so a reload or Back
        # cannot return to the token.  `home` is not that pane -- on this host it
        # is the windowless state a native menu-bar app has, and a page that
        # resolved to it drew one empty frame.
        self.assertIn("const DEFAULT_ROUTE: AppRoute = \"providers-models\";", entry)
        self.assertIn("if (!isKnownRoute(next)) return DEFAULT_ROUTE;", entry)
        self.assertIn("function adoptRouteAddress(): void {", entry)
        self.assertIn("adoptRouteAddress();\nlet currentRoute = routeFromHash();", entry)
        self.assertIn("isKnownRoute(canonicalPageRoute(route))", entry)

    def test_the_tab_title_uses_the_localized_service_word(self) -> None:
        leaf = (self.shared / "platform/browserLeaf.ts").read_text(encoding="utf-8")
        # A title is a sentence in the app's language, and the language reaches
        # this host only through `setLocalization`.  Writing one from this file's
        # English fallbacks first is what a Chinese browser read in its tab and
        # then watched change, so the write sits behind the table (and a state
        # to word), and the table's own arrival states the title again.
        self.assertIn("const writeDocumentTitle = (): void => {", leaf)
        self.assertIn('if (!strings || !routeStatus || typeof document === "undefined") return;', leaf)
        self.assertIn("serviceStateWord(strings, routeStatus.state)", leaf)
        self.assertGreaterEqual(leaf.count("writeDocumentTitle();"), 2)
        for word in ("serviceStarting", "serviceRunning", "serviceUnhealthy", "serviceStopped", "serviceUnknown"):
            self.assertIn(f"strings.{word}", leaf)

    def test_the_shipped_page_has_no_title_before_the_language_arrives(self) -> None:
        # The tab is named by the host, in the language of the snapshot that
        # chose it.  A `<title>` in the template is a name stated before the
        # page knows which language it speaks -- and a browser shows it, so a
        # Chinese reader saw an English name in the tab until the table replaced
        # it.  The template states no title; the address is a fact until then.
        builder = (Path(__file__).resolve().parents[1] / "rn/apps/web/build.mjs").read_text(encoding="utf-8")
        self.assertNotIn("<title>", builder)


class WebLaunchURLTests(unittest.TestCase):
    """Opening the page from the app lands signed in, without the key.

    The window has already proved itself to Core, so the address it opens
    carries a credential minted by this process alone.  What must not happen:
    the credential reaching a document or a snapshot, the published address
    carrying it for an ordinary install, or a host with no page getting an
    answer it could mistake for one.
    """

    def setUp(self) -> None:
        self.core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "index.html").write_text("<!doctype html><title>Young Router</title>\n", encoding="utf-8")

    def _call(self) -> tuple[int, dict[str, object]]:
        endpoint = self.server.endpoint
        _status, headers, _body = _hello(self.server, bootstrap=self.server.bootstrap_token)
        session = headers["x-litellm-core-session"]
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            connection.request(
                "POST",
                "/ipc",
                body=json.dumps({"protocol_version": 1, "request_id": "launch", "method": "web_launch_url", "params": {}}),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {session}"},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_a_host_that_has_proved_itself_gets_a_signed_in_address(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        status, answer = self._call()
        self.assertEqual(200, status)
        url = answer["result"]["url"]
        self.assertIn("#launch=", url)
        self.assertIn("/ui/", url)
        # The credential is this process's, and it signs a browser in: the
        # launch route accepts what the address carries.
        self.assertEqual(200, _launch_response(self.server, url.split("#launch=", 1)[1])[0])

    def test_the_published_address_stays_credential_free(self) -> None:
        # A native install publishes the plain address; the launch credential is
        # handed only to the host that asks for it.
        self.server.enable_web_host(self.root, password="s3cret-word")
        self.assertNotIn("#launch=", self.server.web_host_url)
        self.assertIn("#launch=", self.server.web_launch_url)

    def test_the_address_is_the_port_the_page_is_published_on(self) -> None:
        """The page lives on the proxy's port, so that is the number it prints.

        Core's own listener is a random port nobody is told about: `web_launch_url`
        used to publish it, so a click on the address in General opened a page on
        a second, unrelated port while the row beside it still said 12390.
        """

        self.server.enable_web_host(self.root, password="s3cret-word")
        # A running proxy owns the published port; the page is forwarded there.
        self.core._service = {"state": "running", "port": 12390}
        self.assertEqual("http://127.0.0.1:12390/ui/", self.server.web_host_url)
        self.assertIn("http://127.0.0.1:12390/ui/#launch=", self.server.web_launch_url)
        # A stopped proxy publishes no port, so the page stays on the listener
        # that actually answers -- there is nowhere else for it to be.
        self.core._service = {"state": "stopped"}
        self.assertEqual(f"http://127.0.0.1:{self.server.port}/ui/", self.server.web_host_url)

    def test_a_core_with_no_page_answers_no_address(self) -> None:
        status, answer = self._call()
        self.assertEqual(400, status)
        self.assertEqual("web_unavailable", answer["error"]["code"])
        self.assertEqual("", self.server.web_launch_url)

    def test_the_credential_is_not_in_any_snapshot(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        _status, answer = self._call()
        token = answer["result"]["url"].split("#launch=", 1)[1]
        self.assertNotIn(token, json.dumps(self.core.snapshot()))


class WebKeyOwnershipTests(unittest.TestCase):
    """The page's key is the Runtime document's, and only General states it.

    Two properties are pinned here because each one was a way this could rot:
    a key generated for a page that states none has to survive a restart, and
    an edited key has to reach the gate without replacing the listener -- the
    pane that changed it must not sign itself out.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = self.root / "runtime-settings.env"
        self.domain = RuntimeSettingsDomain(self.settings)
        self.core = CoreStore(domains=[self.domain])
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.bundle = self.root / "web"
        self.bundle.mkdir()
        (self.bundle / "index.html").write_text('<!doctype html><script src="./bundle.js"></script>', encoding="utf-8")
        (self.bundle / "bundle.js").write_text("export {};", encoding="utf-8")

    def test_a_page_that_states_no_key_gets_one_that_survives_a_restart(self) -> None:
        generated = self.domain.ensure_web_password()
        self.assertTrue(generated)
        # Written, not merely returned: the next process reads the same key,
        # so a restart never silently mints a secret nobody was told.
        self.assertIn(f"{WEB_PASSWORD_SETTING}={generated}", self.settings.read_text(encoding="utf-8"))
        self.assertEqual(generated, RuntimeSettingsDomain(self.settings).web_password())
        self.assertEqual(generated, self.domain.ensure_web_password())

    def test_an_edited_key_reaches_the_gate_without_replacing_the_listener(self) -> None:
        key = self.domain.ensure_web_password()
        self.server.enable_web_host(self.bundle, password=key, password_provider=self.domain.web_password)
        self.assertEqual(200, _login_response(self.server, key)[0])

        self.domain.stage_secret("setting", WEB_PASSWORD_SETTING, "chosen-in-general")
        self.domain.apply({})

        self.assertEqual(401, _login_response(self.server, key)[0])
        self.assertEqual(200, _login_response(self.server, "chosen-in-general")[0])

    def test_the_key_is_not_a_second_settings_surface(self) -> None:
        # A plaintext secret is refused by the ordinary dispatch path, so the
        # only way in is the native lease the General pane uses.  Without this
        # the value would also be settable from any other pane's dispatch.
        self.domain.ensure_web_password()
        with self.assertRaises(Exception):
            self.core.dispatch(
                {
                    "domain": "runtime",
                    "type": "set_setting",
                    "payload": {"key": WEB_PASSWORD_SETTING, "value": "from-elsewhere"},
                },
                expected_revision=self.core.revision,
            )


class BrowserFileCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.bundle = tempfile.TemporaryDirectory()
        self.addCleanup(self.bundle.cleanup)
        root = Path(self.bundle.name)
        (root / "index.html").write_text("<!doctype html><title>Young Router</title>\n", encoding="utf-8")
        self.server.enable_web_host(root, password="preview-password")
        self.cookie = _login(self.server, "preview-password")

    def _post(self, path: str, body: dict[str, object], *, session: str | None = None) -> tuple[int, dict[str, object]]:
        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            connection.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json", "Cookie": f"yr-core-session={session or self.cookie.split('=', 1)[1]}"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_upload_becomes_an_import_capability_and_is_cleaned_after_preview(self) -> None:
        document = json.dumps({"format": "young-router-core-package", "version": 1, "sections": {"language": {"domain": "language", "choice": "en"}}}).encode()
        status, answer = self._post("/ipc/host/file-upload", {"purpose": "import", "name": "example.json", "size": len(document), "data": base64.b64encode(document).decode()})
        self.assertEqual(200, status)
        token = answer["token"]
        self.assertIsInstance(token, str)
        path = self.server._browser_files[token]
        self.assertEqual(document, path.read_bytes())
        self.assertEqual(self.cookie.split("=", 1)[1], self.server._browser_file_sessions[token])
        status, answer = self._post("/ipc", {"protocol_version": 1, "request_id": "upload-preview", "method": "import_preview", "params": {"source_token": token, "revision": 0}})
        self.assertEqual(200, status)
        self.assertEqual(["language"], answer["result"]["detected_sections"])
        self.assertFalse(path.exists())

    def test_upload_rejects_wrong_size_and_unsupported_type(self) -> None:
        for name, size, content in (("example.json", 4, b"{}"), ("example.html", 2, b"{}")):
            with self.subTest(name=name, size=size):
                status, answer = self._post("/ipc/host/file-upload", {"purpose": "import", "name": name, "size": size, "data": base64.b64encode(content).decode()})
                self.assertEqual(400, status)
                self.assertEqual("invalid_file_capability", answer["error"]["code"])
        # The route decodes with `validate=True`; encoding this first would make
        # it valid base64 and exercise the size check instead.
        status, answer = self._post("/ipc/host/file-upload", {"purpose": "import", "name": "example.json", "size": 2, "data": "not base64"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_file_capability", answer["error"]["code"])

    def test_download_returns_written_bytes_once_to_the_owning_session(self) -> None:
        status, answer = self._post("/ipc/host/file-download", {"purpose": "export", "name": "example.json"})
        self.assertEqual(200, status)
        token = answer["token"]
        self.server.core.export(["language"], destination_token=token)
        status, answer = self._post("/ipc/host/file-download", {"token": token})
        self.assertEqual(200, status)
        exported = json.loads(base64.b64decode(answer["data"]))
        self.assertEqual("language", exported["sections"]["language"]["domain"])
        self.assertEqual(400, self._post("/ipc/host/file-download", {"token": token})[0])

    def test_download_is_not_shared_with_another_session(self) -> None:
        _status, answer = self._post("/ipc/host/file-download", {"purpose": "export", "name": "example.json"})
        token = answer["token"]
        other = _login(self.server, "preview-password").split("=", 1)[1]
        self.assertNotEqual(self.cookie.split("=", 1)[1], other)
        status, answer = self._post("/ipc/host/file-download", {"token": token}, session=other)
        self.assertEqual(400, status)
        self.assertEqual("invalid_file_capability", answer["error"]["code"])


class BrowserLoginItemTests(unittest.TestCase):
    """开机自启动 from the page: Core writes the login item the app owns.

    The desktop hosts register theirs in their own process, so their Core only
    records the preference.  A page has no such process behind it even though
    the app is installed on the same machine, and disabling the switch there
    told a person on a Mac with a working login item that the feature did not
    exist.  What must stay true: a host that cannot name an installed app
    answers "not registered" instead of writing a login item nobody can run,
    and the preference itself remains the panes' own dispatch.
    """

    def setUp(self) -> None:
        self.core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.bundle = tempfile.TemporaryDirectory()
        self.addCleanup(self.bundle.cleanup)
        root = Path(self.bundle.name)
        (root / "index.html").write_text("<!doctype html><title>Young Router</title>\n", encoding="utf-8")
        self.server.enable_web_host(root, password="login-item-password")
        self.cookie = _login(self.server, "login-item-password")

    def _post(self, path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            connection.request(
                "POST",
                path,
                body=json.dumps(body),
                headers={"Content-Type": "application/json", "Cookie": self.cookie},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_the_route_needs_a_session(self) -> None:
        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            connection.request("POST", "/ipc/host/autostart", body=json.dumps({"enabled": True}), headers={"Content-Type": "application/json"})
            self.assertEqual(401, connection.getresponse().status)
        finally:
            connection.close()

    def test_a_core_outside_an_install_registers_nothing(self) -> None:
        # This checkout is not inside an app bundle, so there is no login item
        # to write.  The answer says so, and the pane hands its preference back
        # instead of reporting 已保存 over a switch the system never heard of.
        status, answer = self._post("/ipc/host/autostart", {"enabled": True})
        self.assertEqual(200, status)
        self.assertIs(answer["registered"], False)
        self.assertIs(answer["available"], False)

    def test_the_body_is_the_only_shape_it_accepts(self) -> None:
        status, answer = self._post("/ipc/host/autostart", {"enabled": "yes"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_login_item", answer["error"]["code"])
        status, answer = self._post("/ipc/host/autostart", {"enable": True})
        self.assertEqual(400, status)
        self.assertEqual("invalid_login_item", answer["error"]["code"])


class BrowserGroupManagerTests(unittest.TestCase):
    """分组 had no surface in a page, so the pane disabled it.

    The browser host now answers the same four leaf capabilities the native
    sheets implement; what this pins is that the page is *not* the host that
    leaves them undefined — a 分组 button that reads 分组 and does nothing is
    exactly the failure this replaces.
    """

    def test_the_browser_leaf_serves_the_group_manager(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "rn/packages/shared/src/platform/browserLeaf.ts").read_text(encoding="utf-8")
        for capability in (
            "showGroupManager: (options) => openRelayGroupSheet(options)",
            "awaitGroupManagerApply: () => takeRelayGroupApply()",
            "finishGroupManagerApply: async (options) => { finishRelayGroupApply(options); }",
            "updateGroupManager: async (snapshot) => updateRelayGroupSheet(snapshot)",
        ):
            self.assertIn(capability, source)

    def test_the_page_mounts_the_sheet_as_its_own_layer(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "rn/packages/shared/src/platformEntry.web.ts").read_text(encoding="utf-8")
        self.assertIn("setRelayGroupSheetSurface({", source)
        self.assertIn('route: "relay-group-sheet"', source)
        self.assertIn("React.createElement(RelayGroupSheet", source)
        # The sheet's Close asks before it drops a draft, exactly as the native
        # window's does; Escape and the title bar's × go through the same call.
        self.assertIn("requestClose: askCloseRelayGroupSheet", source)

    def test_a_key_value_never_reaches_react(self) -> None:
        sheet = (Path(__file__).resolve().parents[1] / "rn/packages/shared/src/ui/RelayGroupSheet.tsx").read_text(encoding="utf-8")
        # The copy goes through Core's one-shot lease and stops at the
        # clipboard: this tree only ever holds Core's presence sentinel.
        self.assertIn("copyKey(state.accountId, selected.id)", sheet)
        self.assertIn("selected.hint !== \"\" ? \"••••••••\"", sheet)


class BrowserKeepAliveTests(unittest.TestCase):
    """One connection carries many requests, and each body belongs to its own.

    A page keeps its connection open, so `http.server` answers several requests
    on one handler instance.  The "this body was read" flag was never reset per
    request: a 401 or a 404 answered without reading its body then skipped the
    drain, the unread JSON stayed in the socket buffer, and the next request
    line was read back as `{"protocol_version":…}GET` — which http.server
    answers with a 501 "Unsupported method" page.  In the browser that appeared
    as the app being replaced by an "Error response" document in the middle of
    an ordinary pane switch.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.server = CoreIPCServer(CoreStore(domains=[MemoryDomain("language", {"choice": "system"})]))
        self.server.start()
        self.addCleanup(self.server.stop)
        page = root / "web"
        page.mkdir()
        (page / "index.html").write_text("<!doctype html><title>page</title>", encoding="utf-8")
        self.server.enable_web_host(page, password="keep-alive-password")

    def _request(self, connection: http.client.HTTPConnection, method: str, path: str, body: str = "") -> tuple[int, bytes]:
        headers = {"Content-Type": "application/json"} if body else {}
        connection.request(method, path, body=body or None, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()

    def test_a_refused_request_leaves_its_body_out_of_the_next_request(self) -> None:
        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            # A request whose body is read, then one refused before reading it.
            status, _ = self._request(connection, "POST", "/ipc/hello", "{}")
            self.assertEqual(401, status)
            status, _ = self._request(
                connection,
                "POST",
                "/ipc",
                json.dumps({"protocol_version": 1, "request_id": "x", "method": "snapshot", "params": {}}),
            )
            self.assertEqual(401, status)
            # Same socket: the page must still be served, not a 501 page.
            status, body = self._request(connection, "GET", "/ui/")
            self.assertEqual(200, status)
            self.assertIn(b"<!doctype html>", body)
        finally:
            connection.close()

    def test_a_404_with_a_body_does_not_poison_the_connection(self) -> None:
        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            status, _ = self._request(connection, "POST", "/ipc/host/nothing-here", "{}")
            self.assertEqual(404, status)
            status, body = self._request(connection, "GET", "/ui/")
            self.assertEqual(200, status)
            self.assertIn(b"<!doctype html>", body)
        finally:
            connection.close()


class BrowserRelaySignInTests(unittest.TestCase):
    """Adding a station, and signing one back in, from the page host.

    The two native hosts present the station's own login page in a WebKit view
    they own.  A page cannot: another origin's cookie is unreadable from page
    script, and the station's page cannot be served under this app's origin.
    So the page asks Core -- the process on the machine serving it -- to open
    that page in a real browser, and Core reads the session that browser holds.

    That only works for a page open on the machine Core runs on.  A published
    page (a gateway, a LAN name) is open in somebody else's browser, and a
    window on the server's screen is not "a login page popped up" for them:
    Core answers ``unsupported`` with the reason, and the page signs in itself
    by asking for the station account and letting Core post the same form the
    station's page would have posted.

    What this pins: the start route answers which of those two applies, the
    window flow accepts only a session the station itself verified, the form
    keeps the password out of React state, and the add flow creates its account
    only through a successful sign-in.
    """

    shared = Path(__file__).resolve().parents[1] / "rn/packages/shared/src"

    def setUp(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.answer: dict[str, str] | Exception = {
            "username": "person@example.test",
            "cookie": "session=replace-page-cookie",
            "access_token": "replace-page-token",
            "refresh_token": "",
            "user_id": "2411",
        }
        station = self

        self.reads: list[tuple[str, str, dict[str, str]]] = []
        self.reads_done: list[str] = []

        class Station:
            def password_login(self, origin: str, account_type: str, username: str, password: str) -> dict[str, str]:
                station.calls.append((origin, account_type, username, password))
                if isinstance(station.answer, Exception):
                    raise station.answer
                return dict(station.answer)

            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                station.reads.append((origin, path, dict(headers)))
                station.reads_done.append(path)
                return {"success": True, "data": {"id": 7, "username": "window-user", "quota": 500000}}

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.relay = RelayAccountsDomain(self.root, http_client=Station())
        self.core = CoreStore(domains=[self.relay])
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.bundle = tempfile.TemporaryDirectory()
        self.addCleanup(self.bundle.cleanup)
        page = Path(self.bundle.name)
        (page / "index.html").write_text("<!doctype html><title>Young Router</title>\n", encoding="utf-8")
        self.server.enable_web_host(page, password="page-sign-in-password")
        self.cookie = _login(self.server, "page-sign-in-password")

    def _post(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        return self._post_route("/ipc/host/relay/login", body)

    def _post_route(self, route: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            connection.request(
                "POST",
                route,
                body=json.dumps(body),
                headers={"Content-Type": "application/json", "Cookie": self.cookie},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def _body(self, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "account_id": "login-page-0123456789abcdef0123",
            "type": "newapi",
            "label": "Page Station",
            "origin": "https://relay.example.test",
            "username": "person@example.test",
            "password": "replace-page-password",
            "remember_password": True,
            "pending_account": True,
            "station_name": "Page Station",
            "station_type": "newapi",
            "station_origin": "https://relay.example.test",
        }
        body.update(overrides)
        return body

    def test_the_page_route_mints_a_session_from_a_password(self) -> None:
        status, answer = self._post(self._body())
        self.assertEqual(200, status, answer)
        self.assertEqual("signed_in", answer["login_status"])
        self.assertEqual(
            [("https://relay.example.test", "newapi", "person@example.test", "replace-page-password")],
            self.calls,
        )
        accounts = self.core.snapshot()["domains"]["relay_accounts"]["accounts"]
        self.assertEqual(1, len(accounts))
        self.assertEqual("signed_in", accounts[0]["login_status"])
        self.assertTrue(accounts[0]["remember_password"])

    def test_a_refused_sign_in_answers_the_code_the_form_states(self) -> None:
        for failure, code in (
            (RelayAccountsError("Relay username or password is invalid"), "relay_login_rejected"),
            (RelaySessionLimitError(), "relay_session_limit"),
        ):
            with self.subTest(code=code):
                self.answer = failure
                status, answer = self._post(self._body())
                self.assertEqual(400, status)
                self.assertEqual(code, answer["error"]["code"])
                # A failed pending sign-in reserves nothing: the add flow that
                # was cancelled or refused leaves no empty station behind.
                self.assertEqual([], self.core.snapshot()["domains"]["relay_accounts"]["accounts"])

    def test_the_form_keeps_the_password_out_of_react_state(self) -> None:
        chrome = (self.shared / "ui/HostChrome.tsx").read_text(encoding="utf-8")
        surface = chrome.split("function RelaySignInSurface", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("const password = React.useRef(\"\");", surface)
        self.assertIn("password.current = next", surface)
        self.assertIn("secureTextEntry", surface)
        # The field has no `value`, so React never holds the typed secret; only
        # "something was typed" is state, and that is what enables the button.
        self.assertNotIn("secureTextEntry\n          value", surface)
        self.assertIn("setHasPassword(next !== \"\")", surface)

    def test_the_page_signs_in_instead_of_leaving_the_button_dead(self) -> None:
        leaf = (self.shared / "platform/browserLeaf.ts").read_text(encoding="utf-8")
        # The old answer was "restore, else undefined": a 去登录 that opened
        # nothing, and an add flow that answered 登录已取消 without ever asking.
        # A window opens first, and the form is the answer when there is no
        # window to open.
        self.assertIn('"/ipc/host/relay/browser/start"', leaf)
        self.assertIn('kind: "relayBrowser"', leaf)
        self.assertIn('kind: "relaySignIn"', leaf)
        self.assertIn('core.hostRoute("/ipc/host/relay/login", body)', leaf)
        self.assertIn("pending_account: options.pendingAccount === true", leaf)
        self.assertIn('cancelHostChrome("relayBrowser")', leaf)

    def test_a_published_page_gets_the_streamed_screen_not_a_refusal(self) -> None:
        """The server case: the person is not sitting at Core's machine.

        A window this Core opens appears on *its* screen, so a page reached
        through a published name is served the headless browser's frames
        instead.  That decision is the difference between "sign in here with a
        password" and "the station's own page, with its captcha".
        """

        with mock.patch.object(relay_browser, "unavailable_reason", return_value=""), \
             mock.patch.object(relay_browser, "launch", side_effect=relay_browser.BrowserLaunchFailed("no chrome here")):
            answered = self.core.start_relay_browser_login(
                account_id="login-browser-0123456789abcdef",
                account_type="newapi",
                label="Page Station",
                origin="https://relay.example.test",
                pending_account=True,
                page_is_local=False,
            )
        # It asked for a mirror rather than refusing: the answer is a failed
        # launch of a served screen, not "remote".
        self.assertEqual("unsupported", answered["status"])
        self.assertEqual("launch_failed", answered["reason"])
        self.assertEqual({}, self.core._browser_logins)

    def test_a_streamed_login_answers_with_the_size_it_serves(self) -> None:
        """`mirror` in the start answer is what the pane draws."""

        browser = mock.Mock()
        browser.browser.name = "Chromium"
        browser.mirror = mock.Mock(width=560, height=820)
        browser.expired = False
        with mock.patch.object(relay_browser, "unavailable_reason", return_value=""), \
             mock.patch.object(relay_browser, "launch", return_value=browser), \
             mock.patch.object(relay_browser, "embedded_display", return_value=True):
            answered = self.core.start_relay_browser_login(
                account_id="login-browser-0123456789abcdef",
                account_type="newapi",
                label="Page Station",
                origin="https://relay.example.test",
                pending_account=True,
                page_is_local=True,
            )
        self.assertEqual("waiting", answered["status"])
        self.assertEqual({"width": 560, "height": 820}, answered["mirror"])
        entry = self.core.relay_browser_mirror(answered["login_id"])
        self.assertIs(browser, entry)
        # A login id this Core never minted answers nothing.
        self.assertIsNone(self.core.relay_browser_mirror("not-a-login"))

    def test_a_sign_in_window_opens_the_form_and_not_the_stations_home(self) -> None:
        """Measured 2026-11-16 on api.sisct2.xyz: `/` is the marketing home.

        The window opened on the origin drew "SISCT2API — Subscription to API
        Conversion Platform" with a 立即开始 button and no field to sign in
        with, while the form is one route further at `/login`.  Core opened the
        origin, so the page host showed a sign-in dialog nobody could sign in
        through; the address now comes from the station family's own table.
        """

        browser = mock.Mock()
        browser.browser.name = "Chromium"
        browser.mirror = None
        browser.expired = False
        opened: list[str] = []
        with mock.patch.object(relay_browser, "unavailable_reason", return_value=""), \
             mock.patch.object(relay_browser, "launch", side_effect=lambda url, **_: (opened.append(url), browser)[1]):
            answered = self.core.start_relay_browser_login(
                account_id="login-browser-0123456789abcdef",
                account_type="sub2api",
                label="Page Station",
                origin="https://relay.example.test",
                pending_account=True,
                page_is_local=True,
            )
        self.assertEqual("waiting", answered["status"])
        self.assertEqual(["https://relay.example.test/login"], opened)
        # The stored origin stays the origin: it is the base every read is
        # built from (`/api/user/self`, the key page), and a login address kept
        # there would turn every one of them into `/login/api/...`.
        self.assertEqual("https://relay.example.test", self.core._browser_logins[answered["login_id"]]["origin"])

    def test_the_station_family_owns_where_its_pages_are(self) -> None:
        """One table, published to every host that opens a station page.

        `native.relayLogin` is a host leaf: macOS draws the station in a
        WKWebView it owns, Windows in its WebView2, and a page asks Core to
        open a real browser.  "Which page of the station" therefore used to be
        three copies of one rule, and 36ae051 taught only the macOS copy
        `/login` — the sign-in dialog then drew the station's marketing home on
        the web host alone, with every other test green.  The paths live in
        the relay domain now, the snapshot publishes them, and Core's own
        sign-in resolves the same table.
        """

        from young_router.core.domains.relay_accounts import (
            STATION_SURFACES,
            station_surface_address,
            station_surface_path,
        )

        self.assertEqual(
            {
                "newapi": {"sign_in": "login", "dashboard": "console", "usage_logs": "usage-logs"},
                "sub2api": {"sign_in": "login", "dashboard": "dashboard", "usage_logs": "usage"},
            },
            STATION_SURFACES,
        )
        # A surface both families state the same way needs no family: a pending
        # account ("") is asking exactly that, and its sign-in window must not
        # wait for a type it does not have yet.
        self.assertEqual("login", station_surface_path("", "sign_in"))
        self.assertEqual("login", station_surface_path("auto", "sign_in"))
        # The dashboard is not one of those: two families answer two paths, so
        # guessing one is how a console link lands on a marketing home.
        self.assertEqual("", station_surface_path("", "dashboard"))
        self.assertEqual("console", station_surface_path("newapi", "dashboard"))
        self.assertEqual("dashboard", station_surface_path("sub2api", "dashboard"))
        # The published projection is the one the hosts read.
        surfaces = self.core.snapshot()["domains"]["relay_accounts"]["station_surfaces"]
        self.assertEqual(STATION_SURFACES, surfaces)
        for origin, expected in (
            ("https://relay.example.test", "https://relay.example.test/login"),
            ("https://www.relay.example.test", "https://relay.example.test/login"),
            ("https://relay.example.test/", "https://relay.example.test/login"),
            ("https://example.test/relay", "https://example.test/relay/login"),
            ("https://example.test/relay/", "https://example.test/relay/login"),
            ("https://example.test/relay/login", "https://example.test/relay/login"),
            ("http://127.0.0.1:8642", "http://127.0.0.1:8642/login"),
            ("", ""),
        ):
            with self.subTest(origin=origin):
                self.assertEqual(expected, station_surface_address(origin, "sign_in", "sub2api"))
        # A surface the family answers nothing for is the origin, never a guess.
        self.assertEqual("https://relay.example.test", station_surface_address("https://relay.example.test", "dashboard", ""))

    def test_a_host_with_no_browser_answers_which_capability_is_missing(self) -> None:
        with mock.patch.object(relay_browser, "unavailable_reason", return_value="no_browser"):
            answered = self.core.start_relay_browser_login(
                account_id="login-browser-0123456789abcdef",
                account_type="newapi",
                label="Page Station",
                origin="https://relay.example.test",
                pending_account=True,
                page_is_local=True,
            )
        self.assertEqual("unsupported", answered["status"])
        self.assertEqual("no_browser", answered["reason"])

    def test_a_window_session_is_only_accepted_when_the_station_answers(self) -> None:
        """A cookie read from a browser jar is not a login by itself.

        Core asks the station for a profile with that jar before anything is
        written: a stale cookie in a throwaway profile, or one the station
        never accepted, must not sign an account in.  The id the page kept is
        what a New API fork needs on that read.
        """

        cookie = "session=replace-window-cookie"
        with mock.patch.object(relay_browser, "cookie_header", return_value=cookie):
            verified = self.relay.verify_browser_session(
                origin="https://relay.example.test",
                account_type="newapi",
                cookie=cookie,
                user_id="7",
            )
            self.assertEqual("window-user", verified["username"])
            self.assertEqual("7", verified["user_id"])
            self.assertEqual(cookie, verified["cookie"])
            # The read carried the account id the fork requires, and the jar.
            read = [entry for entry in self.reads if entry[1] == "/api/user/self"][-1]
            self.assertEqual(cookie, read[2].get("Cookie"))
            self.assertEqual("7", read[2].get("New-Api-User"))

    def _jar_entry(self) -> dict[str, object]:
        class Browser:
            def cookies(self) -> list[object]:
                return []

            def storage(self) -> dict[str, str]:
                return {}

        return {"browser": Browser(), "origin": "https://relay.example.test", "type": "newapi", "last_cookie": ""}

    def test_a_verified_page_login_keeps_the_login_and_offers_the_password(self) -> None:
        """The browser's accepted session is stored, and the question has both halves.

        Measured on the live app: signing in from the page verified the session,
        asked Core to record it, and Core answered nothing -- the accept path
        named a switch the boundary did not have, so every page sign-in ended as
        a session nobody stored, and the next check (a native host's probe, with
        its own older cookie) recorded 未登录 seconds later.  What the person saw
        was "I just signed in and it is already gone".
        """

        self._post(self._body(pending_account=True, remember_password=False))
        account = self.core.snapshot()["domains"]["relay_accounts"]["accounts"][0]
        class Browser:
            captured_password = "typed-secret"

            def cookies(self) -> list[object]:
                return []

            def storage(self) -> dict[str, str]:
                return {}

        entry = {
            "browser": Browser(),
            "account_id": account["id"],
            "type": account["type"],
            "label": account["label"],
            "origin": account["origin"],
            "username": "",
            "pending_account": False,
            "station_id": account.get("station_id", ""),
            "station_name": "",
            "station_type": account["type"],
            "station_origin": account["origin"],
            "last_cookie": "",
        }

        outcome = self.core._accept_browser_session(
            entry,
            {"username": "person@example.test", "cookie": "session=window", "access_token": "", "user_id": ""},
        )
        self.assertEqual("signed_in", outcome["status"])
        # The question's two halves: nothing is kept as a password yet, and
        # there is one to keep.
        self.assertFalse(outcome["remember_password"])
        self.assertTrue(outcome["can_remember_password"])
        stored = self.core.snapshot()["domains"]["relay_accounts"]["accounts"][0]
        self.assertEqual("signed_in", stored["login_status"])
        self.assertTrue(self.relay.secret_present("session", account["id"]))

        # 记住密码 keeps what Core captured from the station's own page, and the
        # login stays: neither answer touches the session.
        kept = self.core.remember_relay_password(account_id=account["id"], remember=True)
        self.assertTrue(kept["remember_password"])
        self.assertTrue(kept["password_saved"])
        self.assertEqual("typed-secret", self.relay.trusted_secret_value("password", account["id"]))

    def test_a_pending_browser_login_merge_captures_password_on_the_existing_account(self) -> None:
        """The poll's resolved id also addresses the password preference route."""

        assert isinstance(self.answer, dict)
        self.answer["username"] = "window-user"
        self._post(self._body(pending_account=True, remember_password=False))
        account = self.core.snapshot()["domains"]["relay_accounts"]["accounts"][0]

        class Browser:
            captured_password = "typed-secret"
            expired = False
            last_seen_at = 0.0

            def __init__(self) -> None:
                self.process = mock.Mock()
                self.process.poll.return_value = None

            def cookies(self) -> list[dict[str, str]]:
                return [{"name": "session", "value": "window", "domain": "relay.example.test"}]

            def storage(self) -> dict[str, str]:
                return {}

            def close(self) -> None:
                return None

        entry = {
            "browser": Browser(),
            "account_id": "pending-browser-0123456789abcdef",
            "type": account["type"],
            "label": account["label"],
            "origin": account["origin"],
            "username": "",
            "pending_account": True,
            "station_id": account.get("station_id", ""),
            "station_name": "",
            "station_type": account["type"],
            "station_origin": account["origin"],
            "last_cookie": "",
        }
        entry_id = "pending-browser-0123456789abcdef"
        self.core._browser_logins[entry_id] = entry
        status, outcome = self._post_route("/ipc/host/relay/browser/poll", {"login_id": entry_id})

        self.assertEqual(200, status)
        self.assertEqual("signed_in", outcome["status"])
        self.assertTrue(outcome["can_remember_password"])
        self.assertEqual(account["id"], outcome["account_id"])
        self.assertEqual([account["id"]], [row["id"] for row in self.core.snapshot()["domains"]["relay_accounts"]["accounts"]])
        status, kept = self._post_route(
            "/ipc/host/relay/remember",
            {"account_id": outcome["account_id"], "remember": True},
        )
        self.assertEqual(200, status)
        self.assertTrue(kept["remember_password"])
        self.assertEqual("typed-secret", self.relay.trusted_secret_value("password", account["id"]))

    def test_a_jar_the_station_never_answered_for_is_offered_again(self) -> None:
        """A request that never got an answer says nothing about the jar.

        The jar was remembered before the station was asked, so one network
        blip on the poll that first saw the finished login left that jar marked
        as already offered.  The jar is final by then -- it never changes again
        -- so every later poll skipped it and the window waited forever.
        """

        entry = self._jar_entry()
        answers: list[object] = [
            RelayAccountsError("Relay returned an invalid session profile"),
            {"username": "window-user", "cookie": "session=jar", "user_id": "7"},
        ]
        asked: list[str] = []

        def verify(**arguments: str) -> object:
            asked.append(arguments["cookie"])
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch.object(relay_browser, "cookie_header", return_value="session=jar"), mock.patch.object(
            self.relay, "verify_browser_session", verify
        ), mock.patch.object(
            self.core, "_accept_browser_session", lambda _entry, verified: {"status": "signed_in", **verified}
        ):
            self.assertIsNone(self.core._verify_browser_jar("login", entry))
            # Not on the very next poll: an unreachable station is not asked
            # once a second.
            self.assertIsNone(self.core._verify_browser_jar("login", entry))
            self.assertEqual(1, len(asked))
            entry["jar_retry_at"] = 0.0
            outcome = self.core._verify_browser_jar("login", entry)

        assert outcome is not None
        self.assertEqual("signed_in", outcome["status"])
        self.assertEqual(2, len(asked))

    def test_a_jar_the_station_refused_is_not_offered_twice(self) -> None:
        from young_router.core.domains.relay_accounts import RelaySessionRejectedError

        entry = self._jar_entry()
        asked: list[str] = []

        def refuse(**arguments: str) -> object:
            asked.append(arguments["cookie"])
            raise RelaySessionRejectedError()

        with mock.patch.object(relay_browser, "cookie_header", return_value="session=jar"), mock.patch.object(
            self.relay, "verify_browser_session", refuse
        ):
            for _ in range(3):
                self.assertIsNone(self.core._verify_browser_jar("login", entry))

        self.assertEqual(1, len(asked), "a refused jar was sent to the station again")

    def test_a_copy_still_works_on_a_page_with_no_clipboard_api(self) -> None:
        """A page published over plain HTTP has no `navigator.clipboard`.

        Browsers grant that API to secure origins only, so a server page's
        copy button (an API key, a secret) silently did nothing there.  The
        legacy editing command still runs inside a press, which is what a copy
        press is, and the editor page already used that pair.
        """

        leaf = (self.shared / "platform/browserLeaf.ts").read_text(encoding="utf-8")
        self.assertIn("async function writeClipboard(text: string): Promise<boolean>", leaf)
        self.assertIn('copied = document.execCommand("copy");', leaf)
        self.assertIn("return writeClipboard(lease.value);", leaf)

    def test_the_editor_says_how_to_paste_when_it_cannot_read_the_clipboard(self) -> None:
        editor = (self.shared / "ui/code-editor/CodeEditorWebView.tsx").read_text(encoding="utf-8")
        self.assertIn("navigator.clipboard?.readText", editor)
        self.assertIn('post({ type: "focus" })', editor)
        self.assertIn('kind: "notice"', editor)
        page = (self.shared / "ui/code-editor/codeEditorPage.ts").read_text(encoding="utf-8")
        self.assertIn('pasteHint: translate("editor.pasteHint")', page)

    def test_the_window_is_filled_from_the_account_the_app_already_keeps(self) -> None:
        """The form opens with the account's own sign-in, not empty.

        The two native hosts type the remembered username and password into
        the station's form.  A page host cannot hand its browser a WebKit
        credential store, so Core types into the browser it opened instead:
        the page never receives the secret. The station form is for manual
        submission, even if the account needs to sign in.
        """

        status, answer = self._post(self._body())
        self.assertEqual(200, status, answer)
        account = self.core.snapshot()["domains"]["relay_accounts"]["accounts"][0]
        account_id = account["id"]
        self.assertEqual("person@example.test", account["username"])
        # The station's own determined 未登录: no session on hand.
        self.relay.set_login_status(account_id, "signed_out")

        browser = mock.Mock()
        browser.browser.name = "Chromium"
        browser.mirror = None
        browser.expired = False
        with mock.patch.object(relay_browser, "unavailable_reason", return_value=""), \
             mock.patch.object(relay_browser, "launch", return_value=browser), \
             mock.patch.object(relay_browser, "start_prefill") as prefill:
            answered = self.core.start_relay_browser_login(
                account_id=account_id,
                account_type="newapi",
                label="Page Station",
                origin="https://relay.example.test",
                username="",
                page_is_local=False,
            )

        self.assertEqual("waiting", answered["status"])
        prefill.assert_called_once()
        args, kwargs = prefill.call_args
        self.assertIs(browser, args[0])
        # The account's own username wins over the empty one the caller sent.
        self.assertEqual("person@example.test", kwargs["username"])
        self.assertEqual("replace-page-password", kwargs["password"])
        self.assertEqual({"username", "password"}, set(kwargs))
        self.assertEqual("person@example.test", self.core._browser_logins[answered["login_id"]]["username"])

    def test_a_pending_account_has_nothing_to_fill(self) -> None:
        """The add flow creates its row only after the station accepts it."""

        browser = mock.Mock()
        browser.browser.name = "Chromium"
        browser.mirror = None
        browser.expired = False
        with mock.patch.object(relay_browser, "unavailable_reason", return_value=""), \
             mock.patch.object(relay_browser, "launch", return_value=browser), \
             mock.patch.object(relay_browser, "start_prefill") as prefill:
            self.core.start_relay_browser_login(
                account_id="login-page-0123456789abcdef0123",
                account_type="newapi",
                label="Page Station",
                origin="https://relay.example.test",
                pending_account=True,
                page_is_local=True,
            )
        _args, kwargs = prefill.call_args
        self.assertEqual("", kwargs["username"])
        self.assertEqual("", kwargs["password"])
        self.assertEqual({"username", "password"}, set(kwargs))



class MirrorTests(unittest.TestCase):
    """A server's sign-in screen is the browser it already has, streamed.

    A machine with no screen cannot show a window, and a page opened through a
    reverse proxy is not on the machine running Core.  Both run the browser
    headless and send frames to the page — no X server, no VNC, nothing to
    install.  What has to be right here is the shape of that stream: a frame
    only when something changed, input that is a click and not a coordinate
    outside the display, and a login id that answers for one session only.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_a_host_with_a_browser_can_always_stream(self) -> None:
        # Nothing but the browser is needed: no display, no second package.
        with mock.patch.object(relay_browser, "find_browser", return_value=object()):
            self.assertEqual("", relay_browser.unavailable_reason())
        with mock.patch.object(relay_browser, "find_browser", return_value=None):
            self.assertEqual("no_browser", relay_browser.unavailable_reason())

    def test_a_host_states_which_browser_to_drive(self) -> None:
        """A server whose only browser is `chromium` must not need Chrome.

        The pick order is Chrome first, which is right for a desktop and wrong
        for a server image that ships one browser.  `YOUNG_ROUTER_BROWSER`
        states the answer, by path or by name, so "which one is it driving" is
        a question with one answer.
        """

        fake = Path(self.temp.name) / "chromium"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"YOUNG_ROUTER_BROWSER": str(fake)}):
            chosen = relay_browser.find_browser()
        self.assertIsNotNone(chosen)
        self.assertEqual(str(fake), chosen.executable)
        # A name resolves against the installed browsers, case-insensitively.
        candidate = relay_browser.BrowserCandidate(name="Chromium", executable=str(fake))
        with mock.patch.dict(os.environ, {"YOUNG_ROUTER_BROWSER": "chromium"}):
            self.assertEqual(candidate, relay_browser._stated_browser("chromium", [candidate]))
        # Naming something this machine does not have is not a silent fallback.
        with mock.patch.dict(os.environ, {"YOUNG_ROUTER_BROWSER": "/nowhere/browser"}):
            with mock.patch.object(relay_browser.shutil, "which", return_value=None):
                self.assertIsNone(relay_browser.find_browser())
                self.assertEqual("no_browser", relay_browser.unavailable_reason())

    def test_the_deploy_installs_the_browser_it_needs(self) -> None:
        """The prerequisite is part of the install, not a README paragraph."""

        scripts = Path(__file__).resolve().parents[1] / "scripts"
        installer = scripts / "install-chromium.sh"
        self.assertTrue(installer.is_file())
        self.assertTrue(os.access(installer, os.X_OK))
        deploy = (scripts / "build-and-install-macos.sh").read_text(encoding="utf-8")
        self.assertIn('BROWSER_INSTALLER="$ROOT/scripts/install-chromium.sh"', deploy)
        # A build-only caller installs nothing, and a failed install is
        # reported rather than failing the deploy.
        self.assertIn('"$BUILD_ONLY" -eq 0 && -x "$BROWSER_INSTALLER"', deploy)
        self.assertIn("station sign-in will offer the password form only", deploy)

    def test_the_deploy_also_installs_the_font_the_page_needs(self) -> None:
        """Chrome without a CJK font draws every Chinese label as a box.

        Measured on the lv host: the station's login page streamed back with
        `□□` over 用户名, 密码 and the agreement line while ASCII rendered
        normally.  The install has to cover the font for the same reason it
        covers the browser: the sign-in is unreadable without it.
        """

        scripts = Path(__file__).resolve().parents[1] / "scripts"
        installer = (scripts / "install-chromium.sh").read_text(encoding="utf-8")
        self.assertIn("fonts-noto-cjk", installer)
        self.assertIn("fc-list :lang=zh", installer)
        self.assertIn("fonts_present", installer)
        # A host that is not Linux ships the faces and has no fc-list to ask.
        self.assertIn("command -v apt-get >/dev/null 2>&1 || return 0", installer)

    def test_the_account_id_is_found_wherever_the_fork_kept_it(self) -> None:
        """A New API fork's id is not always a flat storage key.

        Measured on pawuvo.com (2026-11-08): the fork writes the whole profile
        as JSON under `user` and nowhere else.  Reading only `id` meant the
        `New-Api-User` header never went out, the profile read answered 401, and
        the mirror sign-in could never complete -- while the person was, in
        fact, signed in.
        """

        from young_router.core.service import _storage_user_id

        self.assertEqual("42", _storage_user_id({"user": '{"id": 42, "username": "person"}'}))
        self.assertEqual("7", _storage_user_id({"id": "7"}))
        self.assertEqual("7", _storage_user_id({"user_id": " 7 "}))
        self.assertEqual("", _storage_user_id({}))
        self.assertEqual("", _storage_user_id({"user": "not json"}))
        self.assertEqual("", _storage_user_id({"user": '{"quota": 1}'}))

    def test_a_token_only_session_is_still_a_session(self) -> None:
        """A sub2api-style station sets no cookie; the token is the session.

        Measured on api.sisct2.xyz through the mirror: the browser held
        `auth_token` in local storage and an **empty cookie jar**, so a verifier
        that required a cookie first skipped every poll -- the person was signed
        in, the station said so, and the account never left "waiting".
        """

        source = (Path(__file__).resolve().parents[1] / "young_router/core/service.py").read_text(encoding="utf-8")
        jar = source.split('header = relay_browser.cookie_header(cookies, entry["origin"])', 1)[1].split("user_id = _storage_user_id(storage)", 1)[0]
        self.assertIn('if not header and not token:', jar)
        self.assertNotIn('if not header:\n            return None', jar)
        domain = (Path(__file__).resolve().parents[1] / "young_router/core/domains/relay_accounts.py").read_text(encoding="utf-8")
        verify = domain.split("def verify_browser_session", 1)[1].split("def set_login_status", 1)[0]
        self.assertIn('_text(cookie, "Relay cookie", limit=32768, required=False)', verify)
        self.assertIn('{"cookie": cookie_value, "access_token": token_value}', verify)

    def test_a_page_that_goes_away_ends_its_sign_in(self) -> None:
        """Closing or reloading the page must not leave a browser running.

        Measured before this existed: reloading the page mid sign-in left three
        Chrome processes and three profiles on the host, each alive until the
        ten-minute limit, because the login id lived only in the document that
        was being replaced.  `beforeunload`/`pagehide` cannot await a fetch, so
        the cancel goes out as a beacon.
        """

        leaf = (Path(__file__).resolve().parents[1] / "rn/packages/shared/src/platform/browserLeaf.ts").read_text(encoding="utf-8")
        self.assertIn('hostBeacon("/ipc/host/relay/browser/cancel", { login_id: loginId })', leaf)
        self.assertIn('window.addEventListener("beforeunload", abandon)', leaf)
        self.assertIn('window.addEventListener("pagehide", abandon)', leaf)
        self.assertIn('window.removeEventListener("beforeunload", abandon)', leaf)
        self.assertIn('window.removeEventListener("pagehide", abandon)', leaf)
        bridge = leaf.split("const hostBeacon =", 1)[1].split("const hostRoute =", 1)[0]
        self.assertIn("navigator.sendBeacon", bridge)
        self.assertIn('type: "application/json"', bridge)

    def test_an_abandoned_sign_in_is_closed_without_anybody_asking(self) -> None:
        """A page that stops polling must not leave a browser running for ever.

        The ten-minute lifetime was only ever read *by a poll*: a tab closed
        without its beacon -- a crash, a killed browser, a network that dropped
        -- never asked again, so nothing reaped it.  A headless Chrome is a few
        hundred megabytes, and that leak had no bound at all.
        """

        from young_router.core import relay_browser as browser_module

        class FakeBrowser:
            def __init__(self, *, idle: bool) -> None:
                self.started_at = time.monotonic() - 5
                self.last_seen_at = time.monotonic() - (browser_module.IDLE_TIMEOUT_SECONDS + 5 if idle else 5)
                self.closed = False

            @property
            def expired(self) -> bool:
                return False

            def idle(self, now=None) -> bool:
                return browser_module.IDLE_TIMEOUT_SECONDS < (now or time.monotonic()) - self.last_seen_at

            def close(self) -> None:
                self.closed = True

        core = CoreStore.__new__(CoreStore)
        core._lock = threading.RLock()
        live, quiet = FakeBrowser(idle=False), FakeBrowser(idle=True)
        core._browser_logins = {"live": {"browser": live}, "quiet": {"browser": quiet}}
        core._browser_reaper = None
        core._browser_reaper_stop = None
        reaped = core._reap_browser_logins()
        # The reaper hands back the entries it took out, so the caller can close
        # them: closing is seconds of process wait and must not hold the lock.
        self.assertEqual([{"browser": quiet}], reaped)
        self.assertEqual({"live"}, set(core._browser_logins))
        # The sweep's own bound: a thread that runs without any request at all.
        self.assertEqual(15.0, BROWSER_LOGIN_REAP_SECONDS)

    def test_a_leftover_frame_is_not_sent_twice(self) -> None:
        mirror = relay_mirror.MirrorSession(port=1, origin="https://example.org")
        with mirror._condition:
            mirror._seq = 7
            mirror._frame = b"jpeg"
        # The caller already saw frame 7: nothing new, so no bytes travel.
        self.assertEqual((7, b""), mirror.latest(7, timeout=0.01))
        with mirror._condition:
            mirror._seq = 8
            mirror._frame = b"next"
            mirror._condition.notify_all()
        self.assertEqual((8, b"next"), mirror.latest(7, timeout=0.01))

    def test_the_mirror_follows_the_page_off_the_station(self) -> None:
        """A sign-in is not one document.

        `Continue with GitHub` opens another origin, and a station that hands
        the form to a single-sign-on host leaves its own address behind.
        Matching only the origin froze the mirror on its last frame and refused
        every click after it — the person was typing into a photograph.
        """

        targets = [
            {"type": "page", "url": "chrome://newtab/", "webSocketDebuggerUrl": "ws://blank"},
            {"type": "page", "url": "https://accounts.example.test/login", "webSocketDebuggerUrl": "ws://other"},
        ]
        with mock.patch.object(relay_browser, "_devtools_targets", return_value=targets):
            chosen = relay_browser.active_page_target(7, "https://station.example.test")
        self.assertEqual("ws://other", chosen["webSocketDebuggerUrl"])
        # Where the sign-in started still wins while it is open: a tab the
        # person left behind must not take the display.
        targets.insert(0, {"type": "page", "url": "https://station.example.test/login", "webSocketDebuggerUrl": "ws://station"})
        with mock.patch.object(relay_browser, "_devtools_targets", return_value=targets):
            chosen = relay_browser.active_page_target(7, "https://station.example.test")
        self.assertEqual("ws://station", chosen["webSocketDebuggerUrl"])

    def test_input_is_refused_rather_than_clamped(self) -> None:
        """A click outside the display is not a click at its edge."""

        dispatched: list[dict] = []

        def exchange(socket_url, commands, *, timeout=5.0):
            dispatched.append({"socket": socket_url, "commands": commands})
            return [{"id": 1, "result": {}}]

        with mock.patch.object(relay_browser, "_page_target", return_value={"webSocketDebuggerUrl": "ws://page"}), \
             mock.patch.object(relay_browser, "_cdp_exchange", side_effect=exchange):
            self.assertTrue(relay_mirror.dispatch(1, "https://example.org", {"type": "click", "x": 10, "y": 20}, width=560, height=820))
            self.assertFalse(relay_mirror.dispatch(1, "https://example.org", {"type": "click", "x": 560, "y": 20}, width=560, height=820))
            self.assertFalse(relay_mirror.dispatch(1, "https://example.org", {"type": "click", "x": 1.5, "y": 20}, width=560, height=820))
            self.assertFalse(relay_mirror.dispatch(1, "https://example.org", {"type": "key", "key": "F5"}, width=560, height=820))
            self.assertFalse(relay_mirror.dispatch(1, "https://example.org", {"type": "text", "text": ""}, width=560, height=820))
            self.assertFalse(relay_mirror.dispatch(1, "https://example.org", {"type": "text", "text": "x" * 1001}, width=560, height=820))
            self.assertTrue(relay_mirror.dispatch(1, "https://example.org", {"type": "key", "key": "Enter"}, width=560, height=820))
            self.assertTrue(relay_mirror.dispatch(1, "https://example.org", {"type": "text", "text": "probe"}, width=560, height=820))
        click = dispatched[0]["commands"]
        self.assertEqual(["Input.dispatchMouseEvent", "Input.dispatchMouseEvent"], [c["method"] for c in click])
        self.assertEqual({"x": 10, "y": 20}, {"x": click[0]["params"]["x"], "y": click[0]["params"]["y"]})

    def test_a_login_that_never_painted_is_not_a_blank_pane(self) -> None:
        """The first frame is waited for before the launch answers."""

        source = (Path(__file__).resolve().parents[1] / "young_router/core/relay_browser.py").read_text(encoding="utf-8")
        launch = source.split("def _launch_mirrored", 1)[1].split("def _slug", 1)[0]
        self.assertIn("MirrorSession(", launch)
        self.assertIn("if not mirror.start(", launch)
        self.assertIn("The station did not paint", launch)
        # A headless browser is what keeps this off a server's screen.
        self.assertIn('"--headless=new"', launch)
        self.assertNotIn("--app=", launch)


class RelayBrowserPrefillTests(unittest.TestCase):
    """The station's form, filled by Core instead of by the person.

    The page host's sign-in window is a real browser on a throwaway profile,
    so it starts with no saved credential in it.  Core holds the account's own
    copy, and types it into the page over the same DevTools channel it reads
    the session with. Only the person may submit this form.
    """

    def test_the_values_are_json_encoded_into_the_page(self) -> None:
        seen: list[str] = []

        def evaluate(port: int, origin: str, expression: str, *, timeout: float = 5.0) -> str:
            seen.append(expression)
            return "filled"

        with mock.patch.object(relay_browser, "_evaluate_page", side_effect=evaluate):
            outcome = relay_browser.prefill_login_form(
                1,
                "https://relay.example.test",
                username='a"b@example.test',
                password="p\\w'd",
            )

        self.assertEqual("filled", outcome)
        expression = seen[0]
        # A quote or a backslash in the password must not end the literal, so
        # both are JSON-encoded rather than concatenated.
        self.assertIn('set(user, "a\\"b@example.test")', expression)
        self.assertIn('set(password, "p\\\\w\u0027d")', expression)
        # A field the person already typed into is never overwritten.
        self.assertIn("if (!node || !value || node.value) return;", expression)

    def test_the_browser_form_never_submits_itself(self) -> None:
        seen: list[str] = []
        with mock.patch.object(relay_browser, "_evaluate_page", side_effect=lambda *args, **kwargs: seen.append(args[2]) or "filled"):
            self.assertEqual(
                "filled",
                relay_browser.prefill_login_form(1, "https://relay.example.test", username="u", password="p"),
            )
        self.assertNotIn(".click()", seen[0])
        self.assertNotIn("requestSubmit", seen[0])

    def test_a_page_that_cannot_be_asked_answers_nothing(self) -> None:
        with mock.patch.object(relay_browser, "_evaluate_page", side_effect=RuntimeError("no page")):
            self.assertEqual(
                "",
                relay_browser.prefill_login_form(1, "https://relay.example.test", username="u", password="p"),
            )

    def test_prefill_waits_for_the_fields_and_stops_when_filled(self) -> None:
        browser = mock.Mock(closed=False, port=1, origin="https://relay.example.test")
        outcomes = iter(["none", "partial", "filled"])
        with mock.patch.object(relay_browser, "prefill_login_form", side_effect=lambda *a, **k: next(outcomes)) as form, \
             mock.patch.object(relay_browser.time, "sleep", return_value=None):
            relay_browser._prefill_loop(browser, "u", "p")
        self.assertEqual(3, form.call_count)

    def test_nothing_is_typed_when_nothing_is_kept(self) -> None:
        browser = mock.Mock()
        with mock.patch.object(relay_browser.threading, "Thread") as thread:
            relay_browser.start_prefill(browser, username="", password="")
        thread.assert_not_called()


class RelayBrowserPasswordCaptureTests(unittest.TestCase):
    def test_password_is_captured_before_a_cross_origin_redirect(self) -> None:
        """The event reaches Core even after the station page changes origin."""

        page = {"origin": "https://relay.example.test"}
        target = {"webSocketDebuggerUrl": "ws://page"}
        browser = mock.Mock(
            closed=False,
            port=1,
            origin="https://relay.example.test",
            captured_password="",
        )

        class Socket:
            def __init__(self) -> None:
                self.messages: list[dict[str, object]] = []
                self.commands: list[dict[str, object]] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def send(self, raw: str) -> None:
                command = json.loads(raw)
                self.commands.append(command)
                if command.get("method") == "Runtime.evaluate":
                    # The page's input/click listener sends the value through
                    # the binding before the browser commits its redirect.
                    self.messages.append(
                        {
                            "method": "Runtime.bindingCalled",
                            "params": {
                                "name": relay_browser._CAPTURE_BINDING,
                                "payload": "typed-password",
                            },
                        }
                    )
                    page["origin"] = "https://identity.example.test"
                self.messages.append({"id": command["id"], "result": {}})

            async def recv(self) -> str:
                while not self.messages:
                    await asyncio.sleep(0)
                message = self.messages.pop(0)
                if message.get("id") == 5:
                    browser.closed = True
                return json.dumps(message)

        socket = Socket()
        with mock.patch.object(relay_browser, "_page_target", return_value=target) as page_target, \
             mock.patch("websockets.connect", return_value=socket):
            relay_browser._capture_loop(browser)

        self.assertEqual("https://identity.example.test", page["origin"])
        self.assertEqual("typed-password", browser.captured_password)
        page_target.assert_called_once_with(1, "https://relay.example.test")
        self.assertEqual(
            ["Runtime.enable", "Page.enable", "Runtime.addBinding", "Page.addScriptToEvaluateOnNewDocument", "Runtime.evaluate"],
            [command["method"] for command in socket.commands],
        )
        expression = socket.commands[-1]["params"]["expression"]
        self.assertIn("document.addEventListener('input', read, true)", expression)
        self.assertIn("window.__youngRouterCapturePassword(node.value)", expression)
        self.assertNotIn("sessionStorage", expression)
        self.assertEqual(expression, socket.commands[-2]["params"]["source"])
        self.assertIn('location.origin === "https://relay.example.test"', expression)

    def test_typing_and_correcting_a_password_keeps_the_complete_value(self) -> None:
        browser = mock.Mock(closed=False, origin="https://relay.example.test", captured_password="")

        class Socket:
            def __init__(self) -> None:
                self.messages: list[dict[str, object]] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def send(self, raw: str) -> None:
                command = json.loads(raw)
                self.messages.append({"id": command["id"], "result": {}})
                if command["method"] == "Runtime.evaluate":
                    for value in ("t", "ty", "typo", "", "typed-password"):
                        self.messages.append({
                            "method": "Runtime.bindingCalled",
                            "params": {"name": relay_browser._CAPTURE_BINDING, "payload": value},
                        })

            async def recv(self) -> str:
                message = self.messages.pop(0)
                if message.get("params", {}).get("payload") == "typed-password":
                    browser.closed = True
                return json.dumps(message)

        socket = Socket()
        with mock.patch("websockets.connect", return_value=socket):
            asyncio.run(relay_browser._capture_page_password(browser, "ws://page"))

        self.assertEqual("typed-password", browser.captured_password)


class MirrorFrameTests(unittest.TestCase):
    """The stream and the input path, through Core's own listener."""
    def _frame_call(self, server: CoreIPCServer, cookie: str, since: int) -> dict[str, object]:
        """One answer from `/ipc/host/relay/browser/frames`."""

        endpoint = server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            connection.request(
                "POST",
                "/ipc/host/relay/browser/frames",
                body=json.dumps({"login_id": "frame-login", "since": since}),
                headers={"Content-Type": "application/json", "Cookie": cookie},
            )
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            return json.loads(response.read())
        finally:
            connection.close()

    def test_a_frame_is_base64_and_an_idle_poll_carries_none(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        server.start()
        self.addCleanup(server.stop)
        server.set_web_password("s3cret-word")
        cookie = _login(server, "s3cret-word")
        mirror = mock.Mock(width=560, height=820)
        mirror.latest = mock.Mock(side_effect=[(4, b"jpeg"), (4, b"")])
        core._browser_logins["frame-login"] = {
            "browser": mock.Mock(mirror=mirror, port=1, origin="https://example.org", expired=False),
        }

        answer = self._frame_call(server, cookie, 0)
        self.assertEqual("frame", answer["status"])
        self.assertEqual(4, answer["seq"])
        self.assertEqual(b"jpeg", base64.b64decode(answer["frame"]))

        idle = self._frame_call(server, cookie, answer["seq"])
        self.assertEqual("idle", idle["status"])
        self.assertEqual(4, idle["seq"])
        self.assertNotIn("frame", idle)

    def test_the_page_surface_draws_frames_and_forwards_input(self) -> None:
        shared = Path(__file__).resolve().parents[1] / "rn/packages/shared/src"
        chrome = (shared / "ui/HostChrome.tsx").read_text(encoding="utf-8")
        surface = chrome.split("function MirrorScreen", 1)[1].split("/** The keys this pane forwards", 1)[0]
        self.assertIn('React.createElement("img"', surface)
        self.assertIn("src: frame", surface)
        self.assertIn('mirror.send({ type: "click"', surface)
        self.assertIn('mirror.send({ type: "text"', surface)
        self.assertIn('mirror.send({ type: "key"', surface)
        # Coordinates are the picture's own, not the pane's.
        self.assertIn("mirror.width", surface)
        self.assertIn("mirror.height", surface)
        leaf = (shared / "platform/browserLeaf.ts").read_text(encoding="utf-8")
        self.assertIn('"/ipc/host/relay/browser/frames"', leaf)
        self.assertIn('"/ipc/host/relay/browser/input"', leaf)
        # The poll is long: Core holds it until something changed.
        self.assertIn("data:image/jpeg;base64,", leaf)

    def test_the_surface_is_drawn_before_anything_is_asked(self) -> None:
        """The click draws a window first; the sign-in loads behind it.

        Measured on the live page: 去登录 awaited the silent restore and then a
        cold browser launch before drawing anything, so the press answered with
        several seconds of nothing on screen.  The order is now the surface,
        then the restore, then the browser — and `let cancelled` has to exist
        before either of them reads it, because the surface's own answer sets
        it and the stream's first tick asks it.  (Missing that ordering is the
        `Cannot access 's' before initialization` that once drew the dialog
        black and never requested a frame.)
        """

        leaf = (Path(__file__).resolve().parents[1] / "rn/packages/shared/src/platform/browserLeaf.ts").read_text(encoding="utf-8")
        body = leaf.split("async function browserWindowLogin(", 1)[1].split("function relaySignInFailure", 1)[0]
        request = body.index('kind: "relayBrowser"')
        restore = body.index('await restoreSavedRelayLogin(core, options)')
        start = body.index('"/ipc/host/relay/browser/start"')
        self.assertIn("let cancelled = false;", body)
        self.assertLess(body.index("let cancelled = false;"), request)
        self.assertLess(request, restore)
        self.assertLess(restore, start)
        self.assertLess(body.index("startMirrorSurface("), body.index("/ipc/host/relay/browser/poll"))
        self.assertIn("void Promise.resolve().then(pump);", leaf)

    def test_a_click_is_mapped_onto_the_drawn_picture_not_the_box(self) -> None:
        """The frame is letterboxed; clicks are not.

        Measured live: the picture looked perfect and nothing on it could be
        clicked.  The pane divided the click by the box it was drawn in, while
        `object-fit: contain` left 31 px of black on each side (a 560x820 frame
        in a 418x520 box), so every click landed ~50 px off in the station's own
        coordinates -- and a click in the margin mapped outside the display,
        where Core refuses the event and the pane looks dead.
        """

        chrome = (Path(__file__).resolve().parents[1] / "rn/packages/shared/src/ui/HostChrome.tsx").read_text(encoding="utf-8")
        rule = chrome.split("export function mirrorPoint", 1)[1].split("function MirrorScreen", 1)[0]
        self.assertIn("Math.min(boxWidth / mirror.width, boxHeight / mirror.height)", rule)
        self.assertIn("(boxWidth - mirror.width * scale) / 2", rule)
        self.assertIn("(boxHeight - mirror.height * scale) / 2", rule)
        # Outside the picture is not a click: the margin is not the station.
        self.assertIn("if (x < 0 || y < 0 || x >= mirror.width || y >= mirror.height) return null", rule)
        surface = chrome.split("function MirrorScreen", 1)[1].split("const MIRROR_KEYS", 1)[0]
        # React delegates to its root: `nativeEvent.currentTarget` is the root
        # container, not the picture, so the handler measures the picture's own
        # rect and uses client coordinates.
        self.assertIn("node.getBoundingClientRect()", surface)
        self.assertIn("native.clientX", surface)
        self.assertIn("if (point) mirror.send({ type: \"click\", ...point })", surface)
        self.assertNotIn("mirror.width) / Math.max(1, element.clientWidth)", chrome)

    def test_nothing_pretends_to_be_a_window_when_frames_are_running(self) -> None:
        chrome = Path(__file__).resolve().parents[1] / "rn/packages/shared/src/ui/HostChrome.tsx"
        text = chrome.read_text(encoding="utf-8")
        surface = text.split("function RelayBrowserSurface", 1)[1].split("function MirrorScreen", 1)[0]
        # The name and the screen arrive after the surface is already drawn, so
        # they are read from the request's own store instead of its fixed body.
        self.assertIn("React.useSyncExternalStore(request.live.subscribe, request.live.snapshot, request.live.snapshot)", surface)
        self.assertIn("{live.mirror || !live.browser ? null : <NativeButton title={request.showLabel}", surface)
        self.assertIn("request.cancelLabel", surface)


class BrowserWindowEdgeTests(unittest.TestCase):
    """The page's app window has to end somewhere.

    The shell frame's fill and the page's own backdrop were the same literal
    (`#f7f7f7`, `windowBackgroundColor`), so a centred frame dissolved into the
    page: there was no edge, no rule, and nothing that said where the app
    stopped.  A native host gets that line from its window server.
    """

    shared = Path(__file__).resolve().parents[1] / "rn/packages/shared/src"
    web = Path(__file__).resolve().parents[1] / "rn/apps/web"

    def test_the_shell_draws_its_own_edge(self) -> None:
        ui = (self.shared / "ui/YoungRouterApp.tsx").read_text(encoding="utf-8")
        frame = ui.split("webShellFrame: {", 1)[1].split("},", 1)[0]
        self.assertIn("borderWidth: 1", frame)
        self.assertIn('borderColor: "#b8b8c0"', frame)
        self.assertIn("shadowOpacity", frame)
        # Both shell layouts: a narrow window is still a window.
        self.assertEqual(2, ui.count("Platform.OS === \"web\" && styles.webShellFrame"))

    def test_the_page_is_its_own_surface(self) -> None:
        build = (self.web / "build.mjs").read_text(encoding="utf-8")
        # Not `#f7f7f7`: that is `windowBackgroundColor`, the app's own fill.
        self.assertIn("background: #e3e3e8;", build)
        self.assertNotIn("background: #f7f7f7;", build)


class BrowserAddressTests(unittest.TestCase):
    """The address in General is the address this page was opened at.

    A Core that derives it from its own port prints the loopback address even
    to a window opened through a reverse proxy, where `127.0.0.1` names the
    reader's own machine and opens nothing.  The page can read its own origin,
    so on this host it states that.
    """

    ui = (Path(__file__).resolve().parents[1] / "rn/packages/shared/src/ui/YoungRouterApp.tsx").read_text(encoding="utf-8")

    def test_the_page_states_the_origin_it_was_opened_at(self) -> None:
        self.assertIn(
            'const webPageOwnOrigin = Platform.OS === "web" && typeof window !== "undefined" && window.location.origin.startsWith("http")',
            self.ui,
        )
        panel = self.ui.split("const webPageAddress = (() => {", 1)[1].split("})();", 1)[0]
        self.assertIn('if (webPageOwnOrigin !== "") {', panel)
        self.assertIn("return `${webPageOwnOrigin}/ui`;", panel)
        # The native hosts have no address bar: they keep the derivation.
        self.assertIn('return port ? `http://127.0.0.1:${port}/ui` : "";', panel)

    def test_the_click_opens_the_address_the_row_printed(self) -> None:
        """A published page's link went to loopback.

        The row stated the address bar (`https://host/ui`) while the press asked
        Core for the launch address, which is this machine's `127.0.0.1` plus a
        credential -- an address the reader at a reverse-proxied page cannot
        open, and one Core already answers for free on the owner's own page.
        The launch credential is for a native host opening a browser beside
        Core, so only that case may replace the printed address.
        """

        launch = self.ui.split("const openWebPage = async", 1)[1].split("const webPageAddress", 1)[0]
        self.assertIn('let address = webPageAddress;', launch)
        guard = launch.split('if (webPageOwnOrigin === "") {', 1)
        self.assertEqual(2, len(guard))
        self.assertIn("await ipc.webLaunchUrl()", guard[1])
        # The printed address is what opens until the guard says otherwise.
        self.assertNotIn("await ipc.webLaunchUrl()", guard[0])

    def test_the_proxy_states_the_name_the_browser_used(self) -> None:
        forward = (Path(__file__).resolve().parents[1] / "young_router/proxy/web_forward.py").read_text(encoding="utf-8")
        # Core sees a loopback `Host` on every forwarded request, so the name
        # the person typed has to travel beside it.
        self.assertIn('PUBLISHED_HOST_HEADER = "X-Young-Router-Page-Host"', forward)
        self.assertIn("outbound.append((PUBLISHED_HOST_HEADER, page_host))", forward)


class BrowserModelChooserTests(unittest.TestCase):
    """选择要添加的模型 is a surface, not a list in a box.

    The two native hosts build it with a heading, the provider and key it is
    choosing for, a search field, 全选 and 反选 over the rows the search left,
    and a count that follows both.  The page drew the checkboxes and a footer,
    so a station offering ninety models could only be browsed by scrolling.
    """

    shared = Path(__file__).resolve().parents[1] / "rn/packages/shared/src"

    def test_the_page_offers_the_same_four_controls(self) -> None:
        chrome = (self.shared / "ui/HostChrome.tsx").read_text(encoding="utf-8")
        self.assertIn('const labels = request.labels;', chrome)
        self.assertIn('<NativeTextField\n        value={query}', chrome)
        self.assertIn('title={labels.selectAll}', chrome)
        self.assertIn('title={labels.invert}', chrome)
        self.assertIn('request.models.length === 0 ? labels.empty : labels.noMatches', chrome)

    def test_the_chooser_opens_with_nothing_ticked(self) -> None:
        chrome = (self.shared / "ui/HostChrome.tsx").read_text(encoding="utf-8")
        # It used to open with the whole list selected, so 添加已选模型 added
        # every model the station offered without anyone ticking one.
        self.assertIn("const [selected, setSelected] = React.useState<string[]>([]);", chrome)

    def test_the_filter_folds_the_way_the_appkit_list_does(self) -> None:
        chrome = (self.shared / "ui/HostChrome.tsx").read_text(encoding="utf-8")
        # A search for `flash` has to find `GLM-5.3-FLASH-FREE`, and a full-width
        # name typed in half-width has to find itself.
        self.assertIn('model.normalize("NFKC").toLocaleLowerCase()', chrome)
        self.assertIn('query.trim().normalize("NFKC").toLocaleLowerCase()', chrome)

    def test_select_all_and_invert_act_on_the_visible_rows(self) -> None:
        chrome = (self.shared / "ui/HostChrome.tsx").read_text(encoding="utf-8")
        # 全选当前可见模型 is what the tooltip promises: ticking the ninety rows
        # the filter hid would be a different promise, and an inverting one that
        # also flipped hidden rows would drop a selection nobody touched.
        self.assertIn('const selectAllVisible = (): void => setSelected((current) => [...current, ...visible.filter((model) => !current.includes(model))]);', chrome)
        self.assertIn('const hidden = current.filter((model) => !visible.includes(model));', chrome)

    def test_the_sign_in_surface_asks_what_a_verified_sign_in_may_keep(self) -> None:
        """A typed password is a question, not a default.

        Both native hosts ask after the station accepted the session: an
        account that keeps no password is exactly the one whose next visit asks
        again, and repeated sign-ins are what a station's session cap runs out
        on.  The page host had no such question at all -- and no store of its
        own to keep the session in -- so a session-only account signed in,
        dropped the cookie, and read 未登录 on its next visit.

        The question is asked only when there is a choice: an account that
        already remembers its password answered it when the switch was turned
        on, and one with nothing to offer has nothing to keep.
        """

        leaf = (self.shared / "platform/browserLeaf.ts").read_text(encoding="utf-8")
        prompt = leaf.split("async function askRememberPassword(", 1)[1].split("/**\n * Sign in by letting Core", 1)[0]
        self.assertIn("if (answer.remember_password === true) return;", prompt)
        self.assertIn("if (answer.can_remember_password !== true) return;", prompt)
        self.assertIn('kind: "confirm"', prompt)
        self.assertIn("confirmLabel: strings?.relayRememberPasswordKeep", prompt)
        self.assertIn("cancelLabel: strings?.relayRememberSessionOnly", prompt)
        # The answer is the only thing that travels; the password stays in
        # Core, which captured it from the station's own page.
        self.assertIn('core.hostRoute("/ipc/host/relay/remember", { account_id: options.accountId, remember: keep === true })', prompt)
        self.assertNotIn("password", prompt.split("hostRoute", 1)[1].split(")", 1)[0])
        # Asked after the sign-in is verified, and never instead of finishing it.
        self.assertIn('if (typeof answer.account_id !== "string" || !answer.account_id) return undefined;', leaf)
        self.assertIn("void askRememberPassword(core, { accountId: answer.account_id, label: options.label }, answer, strings);", leaf)

    def test_a_session_check_reports_every_state_the_station_determined(self) -> None:
        """The page's check answers like a native host's, not only on success.

        Both native hosts hand the pane all three determined states, and the
        pane reads them as the row's login: 已登录 for the session that works,
        未登录/已过期 for the answer that does not.  This host dropped the two
        failures, so its rows kept whatever they held and entering the pane
        looked like no check had run -- the one behaviour the two hosts are
        supposed to share.  What is still dropped is the *absence* of an
        answer: an unreachable station, or one that refused to mint a session,
        states nothing about the session this check is asking about.
        """

        leaf = (self.shared / "platform/browserLeaf.ts").read_text(encoding="utf-8")
        self.assertIn(
            'if (answered !== "signed_in" && answered !== "signed_out" && answered !== "expired") return undefined;',
            leaf,
        )
        # The request states no observation of its own: a host that has not
        # asked the station must not hand Core its guess as the answer.
        restore = leaf.split("restoreRelaySession: async (options) => {", 1)[1].split("openRelayLogs:", 1)[0]
        self.assertIn('login_status: "unknown"', restore)
        self.assertNotIn('login_status: "signed_out"', restore)
        silent_restore = leaf.split("async function restoreSavedRelayLogin(", 1)[1].split("async function askRememberPassword(", 1)[0]
        self.assertIn('login_status: "unknown"', silent_restore)
        self.assertNotIn('login_status: "signed_out"', silent_restore)
        self.assertIn('const loginStatus: "signed_in" | "signed_out" | "expired" = answered;', leaf)
        self.assertIn("loginStatus,\n          username:", leaf)

    def test_the_words_come_from_the_apps_localization(self) -> None:
        leaf = (self.shared / "platform/browserLeaf.ts").read_text(encoding="utf-8")
        # `modelChooserHeading` is not in this list on purpose: the card's own
        # title row states it (a card has no window chrome to repeat it in).
        for key in (
            "modelChooserProvider", "modelChooserKey",
            "modelChooserSearch", "modelChooserAll", "modelChooserSelectAllVisible",
            "modelChooserInvert", "modelChooserInvertVisible", "modelChooserCount",
            "modelChooserCountFiltered", "modelChooserCountSelected",
            "modelChooserEmpty", "modelChooserNoMatches",
        ):
            self.assertIn(f"strings?.{key} ??", leaf)


def _get(
    server: CoreIPCServer,
    path: str,
    *,
    cookie: str | None = None,
    page_host: str | None = None,
    accept_language: str | None = None,
    forwarded_proto: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    endpoint = server.endpoint
    connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
    headers = {"Host": f"127.0.0.1:{endpoint.port}"}
    if page_host is not None:
        headers["X-Young-Router-Page-Host"] = page_host
    if cookie is not None:
        headers["Cookie"] = cookie
    if accept_language is not None:
        headers["Accept-Language"] = accept_language
    if forwarded_proto is not None:
        headers["X-Forwarded-Proto"] = forwarded_proto
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def _login_response(server: CoreIPCServer, password: str) -> tuple[int, dict[str, str], bytes]:
    endpoint = server.endpoint
    connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/ipc/web/login",
            body=json.dumps({"password": password}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{endpoint.port}"},
        )
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def _launch_response(server: CoreIPCServer, token: str) -> tuple[int, dict[str, str], bytes]:
    endpoint = server.endpoint
    connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/ipc/web/launch",
            body=json.dumps({"launch": token}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{endpoint.port}"},
        )
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def _post_ipc(server: CoreIPCServer, cookie: str, method: str) -> tuple[int, dict[str, str], dict[str, object]]:
    endpoint = server.endpoint
    connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
    request = json.dumps(
        {"protocol_version": 1, "request_id": f"web-{method}", "method": method, "params": {}}
    ).encode("utf-8")
    try:
        connection.request(
            "POST",
            "/ipc",
            body=request,
            headers={"Content-Type": "application/json", "Cookie": cookie, "Host": f"127.0.0.1:{endpoint.port}"},
        )
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, json.loads(response.read())
    finally:
        connection.close()


def _logout(server: CoreIPCServer, cookie: str) -> tuple[int, dict[str, str], bytes]:
    endpoint = server.endpoint
    connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/ipc/web/logout",
            body=b"{}",
            headers={"Content-Type": "application/json", "Cookie": cookie, "Host": f"127.0.0.1:{endpoint.port}"},
        )
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def _login(server: CoreIPCServer, password: str) -> str:
    status, headers, _body = _login_response(server, password)
    if status != 200:
        raise AssertionError(f"login was refused with {status}")
    cookie = headers.get("set-cookie", "")
    if "yr-core-session=" not in cookie:
        raise AssertionError("login answered without a session cookie")
    return cookie.split(";", 1)[0]

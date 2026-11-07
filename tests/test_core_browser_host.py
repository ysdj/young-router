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

import base64
import http.client
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request

from young_router.core import CoreIPCServer, CoreStore, IPCError, MemoryDomain
from young_router.core.domains.runtime import RuntimeSettingsDomain, WEB_PASSWORD_SETTING


def _hello(server: CoreIPCServer, *, bootstrap: str | None = None, cookie: str | None = None) -> tuple[int, dict[str, str], bytes]:
    endpoint = server.endpoint
    connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if bootstrap is not None:
        headers["Authorization"] = f"Bearer {bootstrap}"
    if cookie is not None:
        headers["Cookie"] = cookie
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
        self.addCleanup(self.server.stop)

    def test_hello_sets_an_httponly_strict_cookie_beside_the_header(self) -> None:
        status, headers, _body = _hello(self.server, bootstrap=self.server.bootstrap_token)
        self.assertEqual(200, status)
        # The native hosts read this header; the browser host cannot, and reads
        # the cookie instead. Both are minted from the same lease.
        self.assertIn("x-litellm-core-session", headers)
        cookie = headers.get("set-cookie", "")
        self.assertIn("yr-core-session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        # A browser session must not outlive the browser, exactly like the
        # native hosts' in-memory lease.
        self.assertNotIn("Max-Age", cookie)

    def test_a_cookie_alone_authenticates_a_request(self) -> None:
        _status, headers, _body = _hello(self.server, bootstrap=self.server.bootstrap_token)
        session = headers["x-litellm-core-session"]

        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        try:
            request = json.dumps(
                {
                    "protocol_version": 1,
                    "request_id": "web-1",
                    "method": "snapshot",
                    "params": {},
                }
            ).encode("utf-8")
            connection.request(
                "POST",
                "/ipc",
                body=request,
                headers={
                    "Content-Type": "application/json",
                    "Cookie": f"yr-core-session={session}",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
        finally:
            connection.close()
        self.assertEqual(200, response.status)
        self.assertTrue(payload["ok"])

    def test_a_header_and_a_cookie_that_disagree_are_refused(self) -> None:
        _status, headers, _body = _hello(self.server, bootstrap=self.server.bootstrap_token)
        session = headers["x-litellm-core-session"]
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
                    "Cookie": f"yr-core-session={session}",
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
        for host in ("127.0.0.1", "localhost", "[::1]"):
            status, headers, body = _get(self.server, "/", page_host=f"{host}:12390")
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
        self.assertIn("isKnownRoute(canonicalWindowRoute(route) as AppRoute)", entry)

    def test_the_tab_title_uses_the_localized_service_word(self) -> None:
        leaf = (self.shared / "platform/browserLeaf.ts").read_text(encoding="utf-8")
        self.assertIn("serviceStateWord(strings, status.state)", leaf)
        for word in ("serviceStarting", "serviceRunning", "serviceUnhealthy", "serviceStopped", "serviceUnknown"):
            self.assertIn(f"strings?.{word}", leaf)


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

    def _session(self) -> str:
        _status, headers, _body = _hello(self.server, bootstrap=self.server.bootstrap_token)
        return headers["x-litellm-core-session"]

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
        for name, size, content in (("example.json", 4, b"{}"), ("example.html", 2, b"{}"), ("example.json", 2, b"not base64")):
            with self.subTest(name=name, size=size):
                status, answer = self._post("/ipc/host/file-upload", {"purpose": "import", "name": name, "size": size, "data": base64.b64encode(content).decode()})
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
        other = _login(self.server, "preview-password")
        self.assertNotEqual(self.cookie.split("=", 1)[1], other)
        status, answer = self._post("/ipc/host/file-download", {"token": token}, session=other)
        self.assertIn(status, (400, 401))
        self.assertIn(answer["error"]["code"], ("invalid_file_capability", "unauthorized"))


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
        panel = self.ui.split("const webPageAddress = (() => {", 1)[1].split("})();", 1)[0]
        self.assertIn('if (Platform.OS === "web" && typeof window !== "undefined"', panel)
        self.assertIn("return `${window.location.origin}/ui`;", panel)
        # The native hosts have no address bar: they keep the derivation.
        self.assertIn('return port ? `http://127.0.0.1:${port}/ui` : "";', panel)

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


def _login(server: CoreIPCServer, password: str) -> str:
    status, headers, _body = _login_response(server, password)
    if status != 200:
        raise AssertionError(f"login was refused with {status}")
    cookie = headers.get("set-cookie", "")
    if "yr-core-session=" not in cookie:
        raise AssertionError("login answered without a session cookie")
    return cookie.split(";", 1)[0]

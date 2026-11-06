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
        (self.root.parent / "outside.txt").write_text("not part of the bundle\n", encoding="utf-8")

    def _get(self, path: str, *, host: str | None = None, cookie: str | None = None) -> tuple[int, dict[str, str], bytes]:
        endpoint = self.server.endpoint
        connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
        headers: dict[str, str] = {"Host": host} if host is not None else {}
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
        status, _headers, body = self._get("/")
        text = body.decode("utf-8")
        self.assertEqual(200, status)
        self.assertIn('lang="zh-Hans"', text)
        self.assertIn("输入访问密钥以继续。", text)
        self.assertIn("登录", text)
        for leak in ("Young Router", "漾路由", "General", "常规", "语言设置"):
            self.assertNotIn(leak, text)
        # The same page in the app's other language, without a restart.
        chosen[0] = "en"
        text = self._get("/")[2].decode("utf-8")
        self.assertIn('lang="en"', text)
        self.assertIn("Enter the access key to continue.", text)
        self.assertNotIn("Young Router", text)

    def test_the_published_url_carries_no_credential(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        url = self.server.web_host_url
        self.assertEqual(f"http://127.0.0.1:{self.server.port}/ui/", url)
        # The password is typed into the page. A credential in the address bar
        # is the one nobody ever rotates.
        self.assertNotIn("password", url)
        self.assertNotIn("bootstrap", url)

    def test_a_page_with_no_password_is_served_no_page(self) -> None:
        # A web host nobody locked is not an unlocked web host: it is closed.
        self.server.enable_web_host(self.root, password="")
        self.assertEqual("", self.server.web_host_url)
        status, _headers, _body = _get(self.server, "/")
        self.assertEqual(200, status)
        body = _body.decode("utf-8")
        self.assertIn('type="password"', body)
        self.assertNotIn("bundle.js", body)

    def test_the_login_page_is_served_instead_of_the_app_before_signing_in(self) -> None:
        self.server.enable_web_host(self.root, password="s3cret-word")
        status, _headers, body = _get(self.server, "/")
        self.assertEqual(200, status)
        self.assertIn('id="password"', body.decode("utf-8"))
        # The app's shell is not handed out yet, so nothing in it can run.
        self.assertNotIn("bundle.js", body.decode("utf-8"))

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
        self.assertIn('type="password"', self._get("/")[2].decode("utf-8"))
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


def _get(server: CoreIPCServer, path: str, *, cookie: str | None = None) -> tuple[int, dict[str, str], bytes]:
    endpoint = server.endpoint
    connection = http.client.HTTPConnection(endpoint.address, endpoint.port, timeout=5)
    headers = {"Host": f"127.0.0.1:{endpoint.port}"}
    if cookie is not None:
        headers["Cookie"] = cookie
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

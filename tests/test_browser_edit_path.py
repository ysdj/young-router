"""What one click costs the page, measured in a running browser.

The reported defect was not a crash and not a wrong value: a toggle in the
published page took seconds to take effect while the same control in the native
window was instant.  Nothing in this repository could fail on it.  The parity
suite reads source files, `tsc` passes, the build succeeds, and a screenshot of
the page after the wait looks exactly like a page that never waited -- the
defect lives in *what the page asked Core for between the press and the paint*,
and only a running page can answer that.

So this drives the real bundle in a real browser against a real Core: the
assertions are the request shapes a settled edit may cost (a plan read, a
dispatch, an apply -- never the whole projection), the bytes those answers may
carry, the time the pressed control takes to show the new value, and -- on a
link slower than a control's own patience -- whether the press stays put.  A
control that fell back to the old value on a fixed two-second budget and then
painted the new one when the state arrived was the second half of the same
report: the box showed the press, took it back, and showed it again.

The measurement is repeated in `tests/test_browser_edit_path.mjs`, which is also
runnable by hand:

    node tests/test_browser_edit_path.mjs http://127.0.0.1:PORT/ui/ --latency 250 --downstream 125000

Against the page built from `a3039fe` that command reports a checkbox that
painted after 5305 ms having downloaded 551 KB.  A source-only assertion would
have called both builds correct.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import yaml

from young_router import runtime_store
from young_router.core import CoreIPCServer, CoreStore

ROOT = Path(__file__).resolve().parents[1]
BUILT_PAGE = ROOT / "rn" / "apps" / "web" / "dist"
DRIVER = Path(__file__).resolve().with_name("test_browser_edit_path.mjs")


def _provider_document(catalog_url: str | None = None, relay_origin: str | None = None) -> str:
    """A document shaped like an installation someone actually runs."""

    lines = ["providers:"]
    for provider in range(3):
        name = f"provider{provider}"
        endpoint = relay_origin if provider == 0 and relay_origin else catalog_url if provider == 0 and catalog_url else f"https://relay{provider}.example.test/v1"
        lines.extend(
            [
                f"  {name}:",
                f'    api_base: "{endpoint}"',
                "    api_keys:",
                f'      - name: key\n        value: "sk-replace-{provider}"',
            ]
        )
        if provider == 0 and catalog_url:
            lines.extend(['      - name: second-key', '        value: "sk-synthetic-second"'])
    lines.append("model_list:")
    for provider in range(3):
        endpoint = relay_origin if provider == 0 and relay_origin else catalog_url if provider == 0 and catalog_url else f"https://relay{provider}.example.test/v1"
        for route in range(4):
            key = "sk-synthetic-second" if provider == 0 and catalog_url and route >= 2 else f"sk-replace-{provider}"
            lines.extend(
                [
                    f"  - model_name: public-{provider}-{route}",
                    "    litellm_params:",
                    f"      model: openai/upstream-{provider}-{route}",
                    f'      api_base: "{endpoint}"',
                    f'      api_key: "{key}"',
                    "    model_info:",
                    f"      id: {provider:02x}{route:02x}abcd",
                    f"      provider: provider{provider}",
                    "      upstream_url_surface: openai/responses",
                ]
            )
    lines.extend(["general_settings:", "  master_key: sk-replace-master", ""])
    return "\n".join(lines)


def _browser() -> str | None:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)


@unittest.skipUnless(shutil.which("node"), "node is required to drive the browser")
@unittest.skipUnless(_browser(), "a Chrome or Chromium binary is required (set CHROME_PATH)")
class BrowserEditCostTests(unittest.TestCase):
    """One settled edit costs a plan read, a dispatch and an apply."""

    def setUp(self) -> None:
        if not (BUILT_PAGE / "index.html").exists():
            self.skipTest(f"no built page at {BUILT_PAGE}; run `pnpm run build:web` first")

    def _start_core(self, document: str) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        root = Path(self._directory.name)
        self.config_path = root / ".litellm-runtime" / "config.yaml"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(document, encoding="utf-8")
        self.settings_path = root / "runtime-settings.env"
        self.settings_path.write_text("", encoding="utf-8")
        codex_home = root / "codex"
        codex_home.mkdir()
        # The pane resolves the proxy's own listing and this installation's
        # runtime root from the environment; pin both so this fixture is the
        # installation under test and not whichever one the runner has.
        self._environment = {
            "YOUNG_ROUTER_HOME": str(root),
            "LITELLM_RUNTIME_ROOT": str(root),
            "YOUNG_ROUTER_RUNTIME_SETTINGS_FILE": str(self.settings_path),
            "CODEX_HOME": str(codex_home),
            "LITELLM_CONFIG_FILE": "",
        }
        self._saved = {key: os.environ.get(key) for key in self._environment}
        for key, value in self._environment.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        self.addCleanup(self._restore_environment)
        # The production domain set: the shell reads every domain's projection
        # and asks the plan for all of them, so a fixture with three domains is
        # a page drawing an error sentence, not a smaller installation.
        self.core = CoreStore.with_default_domains(
            config_path=self.config_path,
            codex_home=codex_home,
            runtime_settings_path=self.settings_path,
            runtime_root=root,
        )
        self.core.snapshot()
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.server.enable_web_host(BUILT_PAGE)

    def _restore_environment(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_table_columns_resize_at_their_edges_and_survive_log_updates(self) -> None:
        self._start_core(_provider_document())
        log_path = Path(self._directory.name) / "recent-requests.jsonl"
        runtime_store.append_record(
            log_path,
            json.dumps({
                "timestamp": "2026-11-21T09:00:00Z",
                "request_id": "first",
                "status": "success",
                "model_group": "public-0-0",
            }),
        )
        endpoint = self.server.endpoint
        url = f"http://{endpoint.address}:{endpoint.port}/ui/"
        completed = subprocess.run(
            ["node", str(DRIVER), url, "--json", "--resize-columns", sys.executable, str(log_path)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)

    def test_route_trace_keeps_its_selection_while_scrolling_through_a_refresh(self) -> None:
        self._start_core(_provider_document())
        log_path = Path(self._directory.name) / "server.log"
        next_log_path = log_path.with_name("server-after.log")

        def record(index: int) -> str:
            payload = {
                "timestamp": f"2026-11-21T09:00:{index:02d}Z",
                "request_id": f"trace-request-{index:02d}",
                "event": "selected_deployment",
                "model_group": f"trace-model-{index:02d}",
                "route_key": f"model=trace-model-{index:02d} / provider=provider0 / upstream=openai/upstream-{index:02d}",
            }
            return f"litellm_route_trace {json.dumps(payload)}\n"

        log_path.write_text("".join(record(index) for index in range(30)), encoding="utf-8")
        next_log_path.write_text("".join(record(index) for index in range(30) if index != 28), encoding="utf-8")
        endpoint = self.server.endpoint
        completed = subprocess.run(
            ["node", str(DRIVER), f"http://{endpoint.address}:{endpoint.port}/ui/", "--json", "--trace-selection", str(next_log_path), str(log_path)],
            capture_output=True, text=True, timeout=60, cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)

    def test_one_settled_edit_asks_for_the_plan_not_the_projection(self) -> None:
        self._start_core(_provider_document())
        endpoint = self.server.endpoint
        url = f"http://{endpoint.address}:{endpoint.port}/ui/"
        completed = subprocess.run(
            ["node", str(DRIVER), url, "--json"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        payload = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "{}"
        try:
            measured = json.loads(payload)
        except ValueError:
            self.fail(f"the page driver answered no measurement:\n{completed.stdout}\n{completed.stderr}")
        measured["stderr"] = completed.stderr[-2000:]
        self.assertTrue(
            measured.get("ok"),
            "a settled edit did not meet the page's own budget:\n"
            + json.dumps(measured, indent=2),
        )
        edit = measured["measured"]
        # The shapes, not only the sizes: a projection read is the defect even
        # when compression makes it small.
        self.assertEqual(["dispatch", "staged", "apply"], [name for name in edit["methods"] if name != "disk_state"][:3])
        self.assertNotIn("snapshot", edit["methods"])
        self.assertLessEqual(edit["bytes"], 16 * 1024)
        self.assertIsNotNone(edit["paintedMs"])
        self.assertLessEqual(edit["paintedMs"], 400)
        self.assertIn("已保存", edit["strip"])
        # The press is the control's until its write answers.  Falling back to
        # the old value while the request is still open makes one press read as
        # two, which is what the slow-link half of the report described.
        self.assertFalse(edit["returned"], "the press went back to its old value before any answer arrived")
        slow = edit["slowLink"]
        self.assertIsNotNone(slow["paintedMs"], f"the press on a slow link never showed the chosen value: {slow}")
        self.assertFalse(
            slow["returned"],
            f"the press on a slow link went back to its old value after {slow['paintedMs']} ms: {slow}",
        )
        self.assertIn("已保存", slow["strip"], f"the slow link's edit never stated the save: {slow}")

    def test_model_import_keeps_the_selection_until_the_new_row_arrives(self) -> None:
        self._check_model_import("answer-first")

    def test_model_import_selects_the_new_row_when_it_arrives_before_the_answer(self) -> None:
        self._check_model_import("event-first")

    def test_model_import_answer_does_not_override_a_later_row_choice(self) -> None:
        self._check_model_import("answer-first-user-change")

    def test_row_change_delete_provider(self) -> None:
        saved = self._check_row_change("delete-provider")
        self.assertNotIn("provider1", saved["providers"])

    def test_row_change_delete_model(self) -> None:
        doc = yaml.safe_load(_provider_document(catalog_url="http://127.0.0.1:9/v1"))
        rows = doc["model_list"]
        # Config order crosses a credential boundary where the display order
        # does not. After removing row 2, the visible neighbor is row 3.
        rows[1], rows[2] = rows[2], rows[1]
        saved = self._check_row_change("delete-model", yaml.safe_dump(doc, sort_keys=False))
        self.assertNotIn("public-0-2", [row["model_name"] for row in saved["model_list"]])

    def test_row_change_delete_relay_account(self) -> None:
        origin = "http://127.0.0.1:9"
        self._start_core(_provider_document(relay_origin=origin))
        for index in range(3):
            name = f"fixture-account-{index}"
            self.core.dispatch({
                "domain": "relay_accounts", "type": "account.add",
                "payload": {"type": "newapi", "label": name, "username": name, "origin": origin},
            })
        self.core.apply(domains=["relay_accounts"], revision=self.core.revision)
        self._drive_row_change("delete-account")
        relay = self.core._domains["relay_accounts"]
        saved = json.loads(relay.storage_path.read_bytes())["accounts"]
        self.assertEqual([row["username"] for row in saved], ["fixture-account-0", "fixture-account-2"])

    def test_auto_grouping_keeps_the_adjacent_key_selected(self) -> None:
        tokens = [
            {"id": 1, "name": "first-key", "group": "default", "status": 1, "key": "fixture-first"},
            {"id": 2, "name": "duplicate-key", "group": "default", "status": 1, "key": "fixture-duplicate"},
            {"id": 3, "name": "last-key", "group": "premium", "status": 1, "key": "fixture-last"},
        ]

        class RelayStation(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/api/user/self":
                    data = {"id": 7, "username": "fixture"}
                elif self.path == "/api/user/self/groups":
                    data = {"default": {"ratio": 1}, "premium": {"ratio": 2}}
                elif self.path == "/api/token/?p=1&size=100":
                    data = {"items": tokens}
                elif self.path.startswith("/api/user/models"):
                    data = ["fixture-model"]
                else:
                    self.send_error(404)
                    return
                body = json.dumps({"success": True, "data": data}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        station = ThreadingHTTPServer(("127.0.0.1", 0), RelayStation)
        self.addCleanup(station.server_close)
        threading.Thread(target=station.serve_forever, daemon=True).start()
        self.addCleanup(station.shutdown)
        origin = f"http://127.0.0.1:{station.server_port}"
        self._start_core(_provider_document(relay_origin=origin))
        self.core.accept_relay_login(
            account_id="fixture-account", account_type="newapi", label="Relay", origin=origin,
            pending_account=True, username="fixture", access_token="fixture-session", user_id="7",
        )
        refreshed = self.core.refresh_relay_resources("fixture-account", revision=None, force=True)
        self.assertEqual("ready", refreshed["resource_status"])
        endpoint = self.server.endpoint
        completed = subprocess.run(
            ["node", str(DRIVER), f"http://{endpoint.address}:{endpoint.port}/ui/", "--json", "--group-selection"],
            capture_output=True, text=True, timeout=60, cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)

    def test_row_change_move_model(self) -> None:
        self._assert_model_moved(self._check_row_change("move-model"))

    def test_row_change_move_route(self) -> None:
        self._assert_model_moved(self._check_row_change("move-route"))

    def test_row_change_rename_public_model(self) -> None:
        saved = self._check_row_change("rename-group")
        self.assertIn("renamed-model", [row["model_name"] for row in saved["model_list"]])

    def test_row_change_add_route(self) -> None:
        self._check_row_change("add-route")

    def test_route_navigation_keeps_the_selected_model_across_providers(self) -> None:
        self._check_route_navigation("route")

    def test_public_model_navigation_keeps_the_selected_model_across_providers(self) -> None:
        self._check_route_navigation("group")

    def _check_route_navigation(self, kind: str) -> None:
        self._start_core(_provider_document())
        endpoint = self.server.endpoint
        completed = subprocess.run(
            ["node", str(DRIVER), f"http://{endpoint.address}:{endpoint.port}/ui/", "--json", "--route-navigation", kind],
            capture_output=True, text=True, timeout=60, cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)

    def _check_row_change(self, change: str, document: str | None = None) -> dict:
        self._start_core(document or _provider_document())
        self._drive_row_change(change)
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

    def _drive_row_change(self, change: str) -> None:
        endpoint = self.server.endpoint
        completed = subprocess.run(
            ["node", str(DRIVER), f"http://{endpoint.address}:{endpoint.port}/ui/", "--json", "--row-change", change],
            capture_output=True, text=True, timeout=60, cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)

    def _assert_model_moved(self, saved: dict) -> None:
        moved, = [row for row in saved["model_list"] if row["model_name"] == "public-0-2"]
        self.assertEqual(moved["model_info"]["provider"], "provider1")

    def test_open_pane_recovers_when_a_logout_is_later_confirmed(self) -> None:
        fixture = self
        account_id = "fixture-account"
        posts = []

        class RelayStation(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/confirmed-logout":
                    with fixture.core._domain_scope("relay_accounts"):
                        fixture.core._domains["relay_accounts"].set_login_status(account_id, "signed_out")
                        fixture.core._advance_projection_revision()
                    fixture.core._request_emit()
                    self.send_response(204)
                    self.end_headers()
                    return
                posts.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success":true,"data":{"username":"fixture","id":7,"access_token":"fixture-live"}}')

        station = ThreadingHTTPServer(("127.0.0.1", 0), RelayStation)
        self.addCleanup(station.server_close)
        threading.Thread(target=station.serve_forever, daemon=True).start()
        self.addCleanup(station.shutdown)
        origin = f"http://127.0.0.1:{station.server_port}"
        self._start_core(_provider_document(relay_origin=origin))
        self.core.accept_relay_login(
            account_id=account_id, account_type="newapi", label="Relay", origin=origin, pending_account=True,
            username="fixture", access_token="fixture-old", password="fixture-password", remember_password=True,
        )
        with self.core._domain_scope("relay_accounts"):
            relay = self.core._domains["relay_accounts"]
            saved = json.loads(relay.storage_path.read_bytes())
            saved["accounts"][0].update(session={}, session_issued_at="", login_status="signed_out", login_observed_at="")
            relay._store.write(saved)
            relay.reload()
            self.core._advance_projection_revision()

        endpoint = self.server.endpoint
        completed = subprocess.run(
            ["node", str(DRIVER), f"http://{endpoint.address}:{endpoint.port}/ui/", "--json", "--confirmed-logout", f"{origin}/confirmed-logout"],
            capture_output=True, text=True, timeout=60, cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
        self.assertEqual(["/api/user/login"], posts)
        self.assertEqual("fixture-live", json.loads(relay.storage_path.read_bytes())["accounts"][0]["session"]["access_token"])

    def test_open_pane_paints_checking_signing_in_signed_in(self) -> None:
        login_started = threading.Event()
        release_login = threading.Event()
        posts = []

        class RelayStation(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/automatic-login-release":
                    release_login.set()
                    self.send_response(204)
                    self.end_headers()
                    return
                posts.append(self.path)
                login_started.set()
                release_login.wait(timeout=5)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success":true,"data":{"username":"fixture","id":7,"access_token":"fixture-live"}}')

        station = ThreadingHTTPServer(("127.0.0.1", 0), RelayStation)
        self.addCleanup(station.server_close)
        threading.Thread(target=station.serve_forever, daemon=True).start()
        self.addCleanup(station.shutdown)
        origin = f"http://127.0.0.1:{station.server_port}"
        self._start_core(_provider_document(relay_origin=origin))
        self.core.accept_relay_login(
            account_id="fixture-account", account_type="newapi", label="Relay", origin=origin,
            pending_account=True, username="fixture", access_token="fixture-old",
            password="fixture-password", remember_password=True,
        )
        with self.core._domain_scope("relay_accounts"):
            relay = self.core._domains["relay_accounts"]
            saved = json.loads(relay.storage_path.read_bytes())
            saved["accounts"][0].update(session={}, session_issued_at="", login_status="signed_out", login_observed_at="2020-01-01T00:00:00Z")
            relay._store.write(saved)
            relay.reload()
            self.core._advance_projection_revision()

        endpoint = self.server.endpoint
        completed = subprocess.run(
            ["node", str(DRIVER), f"http://{endpoint.address}:{endpoint.port}/ui/", "--json", "--automatic-login-progress", f"{origin}/automatic-login-release"],
            capture_output=True, text=True, timeout=60, cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
        self.assertTrue(login_started.is_set())
        self.assertEqual(["/api/user/login"], posts)
        self.assertEqual("fixture-live", json.loads(relay.storage_path.read_bytes())["accounts"][0]["session"]["access_token"])

    def test_session_limit_retry_is_a_real_page_action(self) -> None:
        state = {"full": False}
        requests: list[tuple[str, str, int]] = []
        profile_tokens: list[str | None] = []

        class RelayStation(BaseHTTPRequestHandler):
            def answer(self, status: int, body: object) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                requests.append(("GET", self.path, 0))
                profile_tokens.append(self.headers.get("Authorization"))
                if self.path != "/api/user/self" or self.headers.get("Authorization") != "Bearer fixture-retried":
                    self.answer(401, {"success": False, "message": "unauthorized"})
                    return
                self.answer(200, {"success": True, "data": {"id": 7, "username": "fixture"}})

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path != "/api/user/login":
                    self.answer(404, {})
                    return
                status = 409 if state["full"] else 200
                requests.append(("POST", self.path, status))
                if status == 409:
                    self.answer(409, {"code": "AUTH_SESSION_LIMIT", "success": False})
                else:
                    self.answer(200, {"success": True, "data": {
                        "username": "fixture", "id": 7, "access_token": "fixture-retried",
                    }})

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        station = ThreadingHTTPServer(("127.0.0.1", 0), RelayStation)
        self.addCleanup(station.server_close)
        threading.Thread(target=station.serve_forever, daemon=True).start()
        self.addCleanup(station.shutdown)
        origin = f"http://127.0.0.1:{station.server_port}"
        self._start_core(_provider_document(relay_origin=origin))
        account = {
            "account_id": "fixture-account", "account_type": "newapi", "label": "Relay", "origin": origin,
        }
        self.core.accept_relay_login(
            **account, pending_account=True, username="fixture", access_token="fixture-old",
            password="fixture-password", remember_password=True, user_id="7",
        )
        relay = self.core._domains["relay_accounts"]
        relay._session_recheck_seconds = lambda: 0
        state["full"] = True
        self.core.restore_relay_session(**account, login_status="unknown")
        self.assertEqual([("POST", "/api/user/login", 409)], [item for item in requests if item[0] == "POST"])
        self.assertEqual("session_limit", relay.snapshot()["accounts"][0]["login_failure"])
        state["full"] = False

        endpoint = self.server.endpoint
        completed = subprocess.run(
            ["node", str(DRIVER), f"http://{endpoint.address}:{endpoint.port}/ui/", "--json", "--session-limit-retry"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
        measured = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(measured["ok"], measured)
        self.assertEqual({"refusal": True, "retry": True}, measured["measured"]["before"])
        self.assertEqual({"signedIn": True, "retry": False}, measured["measured"]["after"])
        self.assertEqual(
            [("POST", "/api/user/login", 409), ("POST", "/api/user/login", 200)],
            [item for item in requests if item[0] == "POST"],
        )
        saved = json.loads(relay.storage_path.read_bytes())["accounts"][0]
        self.assertEqual("fixture-retried", saved["session"]["access_token"])
        self.assertEqual("signed_in", saved["login_status"], (requests, profile_tokens))

    def _check_model_import(self, delivery_order: str) -> None:
        requested = []

        class ModelCatalog(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requested.append((self.path, self.headers.get("Authorization")))
                body = b'{"object":"list","data":[{"id":"added-model"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                pass

        catalog = ThreadingHTTPServer(("127.0.0.1", 0), ModelCatalog)
        self.addCleanup(catalog.server_close)
        threading.Thread(target=catalog.serve_forever, daemon=True).start()
        self.addCleanup(catalog.shutdown)
        self._start_core(_provider_document(f"http://127.0.0.1:{catalog.server_port}/v1"))
        endpoint = self.server.endpoint
        url = f"http://{endpoint.address}:{endpoint.port}/ui/"
        completed = subprocess.run(
            ["node", str(DRIVER), url, "--json", "--model-selection", delivery_order],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
        self.assertEqual(requested, [("/v1/models", "Bearer sk-synthetic-second")])
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))["model_list"]
        self.assertEqual(
            ["sk-synthetic-second"],
            [row["litellm_params"]["api_key"] for row in saved if row["model_name"] == "added-model"],
        )


if __name__ == "__main__":
    unittest.main()

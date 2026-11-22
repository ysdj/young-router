from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from unittest import mock

import pytest

from young_router.core.domains.relay_accounts import RelayAccountsDomain
from young_router.core.operations import OnlineUsageReader
from young_router.core.persistence import PersistenceError
from young_router.core.scheduled_tasks import RELAY_LOGIN
from young_router.core.service import CoreError, CoreStore


@contextmanager
def station(*, probe=(401, {}), refresh=(401, {}), login_status=200, accept_new_token=True, first_login=None):
    calls = []

    class Station(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, status, payload, *, cookie=""):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

        def do_GET(self):
            calls.append(("GET", self.path))
            live = accept_new_token and self.headers.get("Authorization") == "Bearer fixture-live"
            if self.path in {"/api/user/self", "/api/v1/auth/me"}:
                if live:
                    self.answer(200, {"success": True, "data": {"id": 7, "username": "fixture"}})
                else:
                    self.answer(*probe)
            elif self.path == "/api/user/models":
                self.answer(200, {"success": True, "data": []})
            elif self.path.startswith("/api/token/?"):
                if live:
                    self.answer(200, {"success": True, "data": {"items": []}})
                else:
                    self.answer(401, {})
            elif self.path.startswith("/api/v1/usage?"):
                if live:
                    self.answer(200, {"code": 0, "data": {"items": [{"model": "test-model"}]}})
                else:
                    self.answer(*probe)
            else:
                self.answer(404, {})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append(("POST", self.path))
            if self.path == "/api/user/auth/refresh":
                if self.headers.get("Authorization") or "new_api_refresh=fixture-old" not in self.headers.get("Cookie", ""):
                    self.answer(400, {})
                else:
                    self.answer(*refresh, cookie="new_api_refresh=fixture-rotated; HttpOnly; Path=/" if refresh[0] == 200 else "")
            elif self.path == "/api/v1/auth/refresh":
                if json.loads(body) != {"refresh_token": "fixture-refresh"} or self.headers.get("X-User-UI-Request") != "1":
                    self.answer(400, {})
                else:
                    self.answer(*refresh)
            elif self.path in {"/api/user/login", "/api/v1/auth/login"}:
                if first_login and calls.count(("POST", "/api/user/login")) == 1:
                    first_login()
                if login_status == 409:
                    self.answer(409, {"code": "AUTH_SESSION_LIMIT", "success": False})
                else:
                    self.answer(200, {"success": True, "data": {"username": "fixture", "id": 7, "access_token": "fixture-live"}})
            else:
                self.answer(404, {})

    with ThreadingHTTPServer(("127.0.0.1", 0), Station) as http:
        worker = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}", calls
        finally:
            http.shutdown()
            worker.join()


def saved_account(root, origin, *, account_type="newapi", cookie="session=fixture-old", refresh_token=""):
    relay = RelayAccountsDomain(root)
    core = CoreStore(domains=[relay])
    account = {"account_id": "fixture-account", "account_type": account_type, "label": "Relay", "origin": origin}
    core.accept_relay_login(
        **account, username="fixture", cookie=cookie, access_token="fixture-old",
        refresh_token=refresh_token, password="fixture-password", remember_password=True, user_id="7", pending_account=True,
    )
    relay._session_recheck_seconds = lambda: 0
    return core, relay, account


@pytest.mark.parametrize("probe,refresh,has_refresh", [
    ((403, {"message": "Forbidden"}), (401, {}), False),
    ((401, {"message": "New-Api-User does not match"}), (401, {}), False),
    ((401, b"<html>Gateway authentication page</html>"), (401, {}), False),
    ((200, {"success": False, "message": "未登录：未提供 New-Api-User"}), (401, {}), False),
    ((200, {"success": False, "data": {"username": "fixture"}}), (401, {}), False),
    ((200, {"success": True, "message": "unexpected response"}), (401, {}), False),
    ((401, {}), (503, {}), True),
])
def test_inconclusive_probe_never_posts_a_password(tmp_path, probe, refresh, has_refresh):
    with station(probe=probe, refresh=refresh) as (origin, calls):
        core, relay, account = saved_account(tmp_path, origin, cookie="new_api_refresh=fixture-old" if has_refresh else "session=fixture-old")
        before = relay.storage_path.read_bytes()
        core.restore_relay_session(**account, login_status="unknown")
        core.relay_maintenance()._run_due(RELAY_LOGIN, [account["account_id"]])
        assert ("POST", "/api/user/login") not in calls
        assert relay.storage_path.read_bytes() == before


def test_rechecks_rotate_and_persist_the_session_without_a_password(tmp_path):
    refresh = (200, {"success": True, "data": {"access_token": "fixture-live"}})
    with station(refresh=refresh) as (origin, calls):
        core, relay, account = saved_account(tmp_path, origin, cookie="new_api_refresh=fixture-old")
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(core.restore_relay_session, **account, login_status="unknown") for _ in range(4)]
            assert all(future.result()["login_status"] == "signed_in" for future in futures)
        assert [call for call in calls if call[0] == "POST"] == [("POST", "/api/user/auth/refresh")]
        restarted = RelayAccountsDomain(tmp_path)
        assert restarted.restore_saved_session(account["account_id"])["login_status"] == "signed_in"
        saved = json.loads(restarted.trusted_secret_value("session", account["account_id"]))
        assert saved["cookie"] == "new_api_refresh=fixture-rotated"
        assert saved["access_token"] == "fixture-live"


def test_sub2api_usage_and_pane_share_one_rotation_and_persist_the_token_pair(tmp_path):
    refresh = (200, {"code": 0, "data": {"access_token": "fixture-live", "refresh_token": "fixture-rotated"}})
    with station(refresh=refresh) as (origin, calls):
        core, relay, account = saved_account(tmp_path, origin, account_type="sub2api", cookie="", refresh_token="fixture-refresh")
        config = tmp_path / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        usage = OnlineUsageReader(config, relay)
        source = usage.sources()[0]["id"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            usage_result = executor.submit(usage.refresh, source)
            pane = executor.submit(core.restore_relay_session, **account, login_status="unknown")
            assert pane.result(timeout=5)["login_status"] == "signed_in"
            records, reason, _, _ = usage_result.result(timeout=5)
        assert not reason
        assert [row["model"] for row in records] == ["test-model"]
        assert [call for call in calls if call[0] == "POST"] == [("POST", "/api/v1/auth/refresh")]
        fresh = RelayAccountsDomain(tmp_path)
        saved = json.loads(fresh.trusted_secret_value("session", account["account_id"]))
        assert (saved["access_token"], saved["refresh_token"]) == ("fixture-live", "fixture-rotated")


def test_concurrent_recovery_posts_once_and_saves_while_an_edit_is_staged(tmp_path):
    with station() as (origin, calls):
        core, relay, account = saved_account(tmp_path, origin)
        core.dispatch({"domain": "relay_accounts", "type": "station.add", "payload": {
            "name": "Unapplied", "type": "newapi", "origin": "https://draft.example.test",
        }})
        with ThreadPoolExecutor(max_workers=4) as executor:
            maintenance = executor.submit(core.relay_maintenance()._run_due, RELAY_LOGIN, [account["account_id"]])
            checks = [executor.submit(core.restore_relay_session, **account, login_status="unknown") for _ in range(3)]
            assert all(check.result()["login_status"] == "signed_in" for check in checks)
            maintenance.result()
        restarted = RelayAccountsDomain(tmp_path)
        assert restarted.restore_saved_session(account["account_id"])["login_status"] == "signed_in"
        assert calls.count(("POST", "/api/user/login")) == 1
        assert not json.loads(restarted.storage_path.read_bytes())["accounts"][0]["auto_login_unconfirmed"]
        assert len(restarted.snapshot()["stations"]) == 1
        assert core.snapshot()["drafts"]["relay_accounts"]["dirty"]


def test_resource_recovery_and_pane_share_the_in_flight_password_post(tmp_path):
    posting = threading.Event()
    finish_post = threading.Event()
    checking = threading.Event()

    def hold_first_post():
        posting.set()
        finish_post.wait(timeout=5)

    with station(first_login=hold_first_post) as (origin, calls):
        core, _, account = saved_account(tmp_path, origin)

        def check():
            checking.set()
            return core.restore_relay_session(**account, login_status="unknown")

        with ThreadPoolExecutor(max_workers=2) as executor:
            resources = executor.submit(core.refresh_relay_resources, account["account_id"], revision=None)
            try:
                assert posting.wait(timeout=5)
                pane = executor.submit(check)
                assert checking.wait(timeout=5)
                with pytest.raises(TimeoutError):
                    pane.result(timeout=0.2)
            finally:
                finish_post.set()
            assert resources.result(timeout=5)["resource_status"] == "ready"
            assert pane.result(timeout=5)["login_status"] == "signed_in"
        assert calls.count(("POST", "/api/user/login")) == 1


def test_a_failed_session_write_retries_the_write_instead_of_logging_in_again(tmp_path):
    with station() as (origin, calls):
        core, relay, account = saved_account(tmp_path, origin)
        original_write = relay._store.write

        def reject_new_session(document):
            if document["accounts"][0]["session"]["access_token"] == "fixture-live":
                raise PersistenceError("fixture write failure")
            original_write(document)

        with mock.patch.object(relay._store, "write", side_effect=reject_new_session):
            with pytest.raises(CoreError, match="fixture write failure"):
                core.restore_relay_session(**account, login_status="unknown")
            assert relay.snapshot()["accounts"][0]["login_status"] == "unknown"
        assert core.restore_relay_session(**account, login_status="unknown")["login_status"] == "signed_in"
        assert calls.count(("POST", "/api/user/login")) == 1
        assert RelayAccountsDomain(tmp_path).restore_saved_session(account["account_id"])["login_status"] == "signed_in"


def test_retrying_the_manual_form_after_a_local_write_failure_does_not_post_again(tmp_path):
    with station() as (origin, calls):
        core, relay, account = saved_account(tmp_path, origin)
        login = dict(account, username="fixture", password="fixture-password")
        with mock.patch.object(relay._store, "write", side_effect=PersistenceError("fixture write failure")):
            with pytest.raises(CoreError, match="fixture write failure"):
                core.accept_relay_login(**login)
        core.accept_relay_login(**login)
        assert calls.count(("POST", "/api/user/login")) == 1
        saved = json.loads(relay.storage_path.read_bytes())["accounts"][0]
        assert saved["session"]["access_token"] == "fixture-live"


@pytest.mark.parametrize("login_status, failure", [(200, "session_unconfirmed"), (409, "session_limit")])
def test_recovery_refusal_survives_repeated_checks_and_core_restart(tmp_path, login_status, failure):
    with station(login_status=login_status, accept_new_token=False) as (origin, calls):
        core, relay, account = saved_account(tmp_path, origin)
        for _ in range(3):
            core.restore_relay_session(**account, login_status="unknown")
        observed = relay.snapshot()["accounts"][0]
        assert observed["login_status"] == "expired"
        assert observed["login_failure"] == failure
        saved = json.loads(relay.storage_path.read_bytes())["accounts"][0]
        assert bool(saved["session_limit_at"]) == (login_status == 409)
        assert saved["auto_login_unconfirmed"] == (login_status == 200)
        restarted = RelayAccountsDomain(tmp_path)
        core = CoreStore(domains=[restarted])
        core.restore_relay_session(**account, login_status="unknown")
        core.relay_maintenance()._run_due(RELAY_LOGIN, [account["account_id"]])
        assert restarted.snapshot()["accounts"][0]["login_failure"] == failure
        assert calls.count(("POST", "/api/user/login")) == 1


def test_settings_rollback_keeps_a_session_issued_during_apply(tmp_path):
    with station() as (origin, calls):
        _, relay, account = saved_account(tmp_path, origin)
        checkpoint = relay.transaction_checkpoint()
        assert relay.restore_saved_password(account["account_id"])["login_status"] == "signed_in"
        # The coordinator restores the settings document before the adapter.
        relay.storage_path.write_bytes(checkpoint["baseline_bytes"])
        relay.restore_apply_transaction(checkpoint)
        assert RelayAccountsDomain(tmp_path).restore_saved_session(account["account_id"])["login_status"] == "signed_in"
        assert calls.count(("POST", "/api/user/login")) == 1

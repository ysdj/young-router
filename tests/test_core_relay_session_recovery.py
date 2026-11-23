from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from unittest import mock

import pytest

from young_router.core.domains.relay_accounts import RelayAccountsDomain
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.webdav import WebDAVSettingsDomain
from young_router.core.operations import OnlineUsageReader
from young_router.core.persistence import PersistenceError
from young_router.core.scheduled_tasks import RELAY_LOGIN
from young_router.core.service import CoreError, CoreStore
from young_router.webdav import core as webdav_core


@contextmanager
def station(*, probe=(401, {}), refresh=(401, {}), login_status=200, accept_new_token=True, first_login=None, first_refresh=None):
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
                if first_refresh and calls.count(("POST", self.path)) == 1:
                    first_refresh()
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
                status = login_status() if callable(login_status) else login_status
                if status == 409:
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


@contextmanager
def dav_server(*, lock_status=200):
    files = {}
    tokens = {}
    guard = threading.Lock()
    calls = []

    class DAV(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, status, data=b"", **headers):
            calls.append((self.command, self.path, status))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_LOCK(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            with guard:
                if lock_status != 200:
                    self.answer(lock_status)
                elif self.path in tokens:
                    self.answer(423)
                else:
                    token = f"<opaquelocktoken:fixture-{len(calls)}>"
                    tokens[self.path] = token
                    files.setdefault(self.path, b"")
                    self.answer(200, **{"Lock-Token": token})

        def do_UNLOCK(self):
            with guard:
                assert tokens.pop(self.path) == self.headers["Lock-Token"]
                self.answer(204)

        def do_PUT(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            with guard:
                token = tokens.get(self.path)
                if ".young-router-login-" in self.path and (not token or self.headers.get("If") != f"({token})"):
                    self.answer(412)
                else:
                    files[self.path] = data
                    self.answer(201)

        def do_GET(self):
            with guard:
                self.answer(200, files[self.path]) if self.path in files else self.answer(404)

        do_HEAD = do_GET

        def do_MKCOL(self):
            self.answer(405)

    with ThreadingHTTPServer(("127.0.0.1", 0), DAV) as http:
        worker = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{http.server_port}/dav/", files, calls
        finally:
            http.shutdown()
            worker.join()


def dav_core(root, origin, *, direct_bundle=False):
    config = root / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
    relay = RelayAccountsDomain(storage_path=webdav_core.relay_accounts_path(config))
    relay._session_recheck_seconds = lambda: 0
    webdav = WebDAVSettingsDomain(
        root / "webdav.json", enabled_path=root / "webdav.enabled", status_path=root / "webdav-status.json",
        state_path=root / "webdav-state.json", conflict_path=root / "webdav-conflict.json", config_path=config,
    )
    webdav.dispatch("patch", {
        "url": origin + ("fixture.json" if direct_bundle else ""),
        "remote_name": "ignored.json" if direct_bundle else "fixture.json", "enabled": True,
    })
    webdav.apply()
    return CoreStore(domains=[relay, webdav, ProvidersModelsDomain(config)]), relay


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


def test_resource_endpoint_rejection_with_an_unavailable_profile_never_posts_a_password(tmp_path):
    from young_router.core.domains.relay_accounts import RelayAccountsError, RelaySessionRejectedError

    class InconclusiveProfileClient:
        def __init__(self):
            self.password_posts = 0

        def json(self, _origin, path, *, headers):
            del headers
            if path == "/api/user/self":
                raise RelayAccountsError("The profile could not be read")
            if path == "/api/user/models" or path.startswith("/api/token/"):
                raise RelaySessionRejectedError()
            raise RelayAccountsError("The resource could not be read")

        def password_login(self, *_args):
            self.password_posts += 1
            raise AssertionError("an unanswered profile cannot authorize a password POST")

    client = InconclusiveProfileClient()
    relay = RelayAccountsDomain(tmp_path, http_client=client)
    account_id = relay.dispatch("account.add", {
        "type": "newapi", "label": "Relay", "origin": "https://relay.example.test", "remember_password": True,
    })["accounts"][0]["id"]
    relay.accept_login_result(
        account_id, username="fixture", cookie="session=fixture-session", access_token="fixture-token",
        password="fixture-password",
    )
    relay.apply()

    refreshed = relay.refresh_resources(account_id)

    stored = json.loads(relay.storage_path.read_bytes())["accounts"][0]
    assert refreshed["resource_status"] == "unavailable"
    assert refreshed["login_status"] == "signed_in"
    assert client.password_posts == 0
    assert stored["session"]["access_token"] == "fixture-token"


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


@pytest.mark.parametrize("use_refresh", [False, True])
def test_webdav_lease_blocks_a_second_core_until_it_can_reuse_the_session(tmp_path, use_refresh):
    posting = threading.Event()
    release = threading.Event()

    def hold_login():
        posting.set()
        release.wait(timeout=5)

    station_settings = {
        "first_refresh": hold_login,
        "refresh": (200, {"success": True, "data": {"access_token": "fixture-live"}}),
    } if use_refresh else {"first_login": hold_login}
    with station(**station_settings) as (origin, calls), dav_server() as (dav, files, dav_calls):
        first, first_relay = dav_core(tmp_path / "first", dav)
        second, second_relay = dav_core(tmp_path / "second", dav, direct_bundle=True)
        account = dict(account_id="shared-account", account_type="newapi", label="Relay", origin=origin)
        first.accept_relay_login(
            **account, pending_account=True, username="fixture",
            cookie="new_api_refresh=fixture-old" if use_refresh else "session=fixture-old",
            password="fixture-password", remember_password=True, user_id="7",
        )
        first.run_automatic_webdav_sync()
        second.run_automatic_webdav_sync()
        with ThreadPoolExecutor(max_workers=2) as executor:
            recovering = executor.submit(first.restore_relay_session, **account, login_status="unknown")
            try:
                assert posting.wait(timeout=5)
                assert second.restore_relay_session(**account, login_status="unknown")["login_status"] == "expired"
                assert second_relay.snapshot()["accounts"][0]["login_failure"] == "sync_waiting"
            finally:
                release.set()
            assert recovering.result(timeout=5)["login_status"] == "signed_in"
        assert any(method == "LOCK" and status == 423 for method, _, status in dav_calls)
        # The lease is gone, the old bundle is still on the server, and this
        # peer restarts. The shared attempt record still prevents a second POST.
        second, second_relay = dav_core(tmp_path / "second", dav, direct_bundle=True)
        assert second.restore_relay_session(**account, login_status="unknown")["login_status"] == "expired"
        first.run_automatic_webdav_sync()
        assert second.restore_relay_session(**account, login_status="unknown")["login_status"] == "signed_in"
        endpoint = "/api/user/auth/refresh" if use_refresh else "/api/user/login"
        assert [call for call in calls if call[0] == "POST"] == [("POST", endpoint)]
        for relay in (first_relay, second_relay):
            assert json.loads(relay.storage_path.read_bytes())["accounts"][0]["session"]["access_token"] == "fixture-live"
        coordination = next(value for path, value in files.items() if "young-router-login-" in path)
        assert set(json.loads(coordination)) == {"at", "state", "retry_at"}


def test_webdav_without_lock_does_not_fall_back_to_password_login(tmp_path):
    with station() as (origin, calls), dav_server(lock_status=405) as (dav, _, _):
        core, relay = dav_core(tmp_path, dav)
        account = dict(account_id="fixture-account", account_type="newapi", label="Relay", origin=origin)
        core.accept_relay_login(
            **account, pending_account=True, username="fixture", cookie="session=fixture-old",
            password="fixture-password", remember_password=True,
        )
        assert core.restore_relay_session(**account, login_status="unknown")["login_status"] == "expired"
        assert relay.snapshot()["accounts"][0]["login_failure"] == "sync_unavailable"
        assert [call for call in calls if call[0] == "POST"] == []


@pytest.mark.parametrize("with_webdav", [False, True])
def test_user_can_retry_a_session_limit_once_after_a_slot_is_freed(tmp_path, with_webdav):
    capped = True
    with station(login_status=lambda: 409 if capped else 200) as (origin, calls), dav_server() as (dav, _, _):
        if with_webdav:
            core, relay = dav_core(tmp_path, dav)
            account = dict(account_id="fixture-account", account_type="newapi", label="Relay", origin=origin)
            core.accept_relay_login(
                **account, pending_account=True, username="fixture", cookie="session=fixture-old",
                password="fixture-password", remember_password=True,
            )
        else:
            core, relay, account = saved_account(tmp_path, origin)
        assert core.restore_relay_session(**account, login_status="unknown")["login_status"] == "expired"
        before = relay.snapshot()["accounts"][0]
        assert before["login_failure"] == "session_limit"
        assert before["login_failure_at"]

        capped = False
        core.restore_relay_session(**account, login_status="unknown")
        assert calls.count(("POST", "/api/user/login")) == 1
        retry = {"domain": "relay_accounts", "type": "session.retry", "payload": {
            "id": account["account_id"], "type": account["account_type"], "label": account["label"],
            "origin": origin, "refused_at": before["login_failure_at"],
        }}
        assert core.dispatch(retry)["action_summary"]["login_status"] == "signed_in"
        core.dispatch(retry)
        assert calls.count(("POST", "/api/user/login")) == 2
        after = json.loads(relay.storage_path.read_bytes())["accounts"][0]
        assert after["session"]["access_token"] == "fixture-live"
        assert not after["session_limit_at"]


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

from __future__ import annotations

import tempfile
import unittest
from typing import get_args, get_type_hints

from young_router.core.domains.relay_accounts import RelayAccountsDomain, RelayHTTPClient


class EmptySub2APIClient:
    def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        del origin, headers
        if path == "/api/v1/user/profile":
            return {"data": {"balance": 1.0}}
        if path.startswith("/api/v1/keys"):
            return {"data": {"items": []}}
        if path == "/api/v1/groups/available":
            return {"data": []}
        if path == "/api/v1/groups/rates":
            return {"data": {}}
        if path == "/api/v1/channels/available":
            return {"data": []}
        raise AssertionError(f"unexpected relay path: {path}")


class ValidSessionClient:
    def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        del origin, headers
        if path == "/api/user/self":
            return {"success": True, "data": {"id": 7, "username": "person"}}
        raise AssertionError(f"unexpected relay path: {path}")


class _StoredCatalogClient:
    def __init__(self) -> None:
        self.model_reads: list[str] = []

    def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        del origin
        if path.startswith("/api/v1/keys"):
            return {
                "code": 0,
                "data": {
                    "items": [
                        {"id": 4, "name": "Plus", "status": "active", "key": "sk-replace-plus", "group_id": 70}
                    ]
                },
            }
        if path == "/api/v1/channels/available":
            return {
                "code": 0,
                "data": [
                    {
                        "name": "plus",
                        "platforms": [
                            {
                                "platform": "openai",
                                "groups": [{"id": 21, "name": "Plus"}],
                                "supported_models": [{"name": "page-model"}],
                            }
                        ],
                    }
                ],
            }
        if path == "/v1/models":
            self.model_reads.append(headers.get("Authorization", ""))
            return {"object": "list", "data": [{"id": "own-model"}]}
        raise AssertionError(f"unexpected relay path: {path}")


class RelayAccountEdgeTests(unittest.TestCase):
    def test_a_zero_key_answer_keeps_a_staged_create_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=EmptySub2APIClient())
            account = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person", cookie="session=fixture")
            domain.create_api_key(account["id"], "Draft")

            refreshed = domain.refresh_resources(account["id"])

            self.assertEqual("no_api_keys", refreshed["resource_error"])
            self.assertEqual(["Draft"], [resource["name"] for resource in refreshed["resources"]])

    def test_restoring_without_a_password_keeps_a_session_only_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=ValidSessionClient())
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(
                account["id"],
                username="person",
                cookie="session=fixture",
                remember_password=False,
            )

            restored = domain.restore_saved_password(account["id"])

            self.assertEqual("signed_in", restored["login_status"])
            self.assertTrue(domain.secret_present("session", account["id"]))

    def test_remembered_session_is_present_after_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(
                account["id"],
                username="person",
                cookie="session=fixture",
                password="secret",
                remember_password=True,
            )
            domain.apply()

            restored = RelayAccountsDomain(directory)

            self.assertTrue(restored.secret_present("session", account["id"]))

    def test_create_metadata_skips_values_the_key_already_has(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            class Client:
                def put(self, *args: object, **kwargs: object) -> object:
                    calls.append((args, kwargs))
                    return {"success": True}

            domain = RelayAccountsDomain(directory, http_client=Client())
            account = {
                "id": "account-1",
                "type": "sub2api",
                "origin": "https://relay.example.test",
                "session": {"cookie": "session=fixture"},
                "resources": [{"id": "sub2api-4", "name": "Key", "group_id": "3", "enabled": False}],
            }
            operation = {"resource_id": "sub2api-4", "changes": {"group_id": "3", "enabled": False}}

            self.assertFalse(domain._apply_create_metadata(account, operation))
            self.assertEqual([], calls)

    def test_moving_the_last_account_tombstones_the_empty_station(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            first = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "One", "origin": "https://one.example.test"},
            )["accounts"][0]
            second = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "Two", "origin": "https://two.example.test"},
            )["accounts"][1]
            old_station_id = first["station_id"]

            domain.dispatch("account.update", {"id": first["id"], "station_id": second["station_id"]})

            removed = domain.transaction_checkpoint()["removed_stations"]
            self.assertIn(old_station_id, [row["id"] for row in removed])

    def test_merging_a_station_tombstones_the_absorbed_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            first = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "One", "origin": "https://one.example.test"},
            )["accounts"][0]
            domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "Two", "origin": "https://two.example.test"},
            )
            absorbed_id = first["station_id"]

            domain.dispatch(
                "station.update",
                {"id": absorbed_id, "name": "Merged", "origin": "https://two.example.test"},
            )

            removed = domain.transaction_checkpoint()["removed_stations"]
            self.assertIn(absorbed_id, [row["id"] for row in removed])

    def test_decode_storage_annotation_names_all_six_results(self) -> None:
        hints = get_type_hints(RelayAccountsDomain._decode_storage)
        self.assertEqual(6, len(get_args(hints["return"])))

    def test_an_apply_read_re_reads_a_key_with_a_stored_catalog(self) -> None:
        client = _StoredCatalogClient()
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=client)
            account = {
                "id": "account-1",
                "type": "sub2api",
                "origin": "https://relay.example.test",
                "session": {"cookie": "session=fixture"},
                "resources": [
                    {"id": "sub2api-4", "name": "Plus", "enabled": True, "models": ["own-model"]}
                ],
            }

            resources = domain._sub2api_resources(account, for_apply=True)

        self.assertEqual(["own-model"], resources[0]["models"])
        self.assertEqual(["Bearer sk-replace-plus"], client.model_reads)


class _SetCookieHeaders:
    def __init__(self, values: list[str]) -> None:
        self._values = values

    def get_all(self, _name: str) -> list[str]:
        return self._values


class _LoginResponse:
    status = 200

    def __init__(self, set_cookie: list[str]) -> None:
        self.headers = _SetCookieHeaders(set_cookie)

    def getcode(self) -> int:
        return self.status

    def read(self, _limit: int) -> bytes:
        return b'{"data": {"user": {"email": "person@example.test"}}}'

    def __enter__(self) -> "_LoginResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _LoginOpener:
    def __init__(self, set_cookie: list[str]) -> None:
        self._set_cookie = set_cookie

    def open(self, _request: object, timeout: float | None = None) -> _LoginResponse:
        del timeout
        return _LoginResponse(self._set_cookie)


class RelayLoginCookieTests(unittest.TestCase):
    def test_a_header_the_cookie_parser_rejects_keeps_the_parsed_cookies(self) -> None:
        # "$Bogus" is an attribute SimpleCookie.load answers with CookieError.
        opener = _LoginOpener(["session=keep; Path=/", "legacy=1; $Bogus=2"])

        result = RelayHTTPClient(opener=opener).password_login(
            "https://relay.example.test", "newapi", "person@example.test", "replace-password"
        )

        self.assertEqual("session=keep", result["cookie"])

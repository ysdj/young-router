from __future__ import annotations

import json
import tempfile
import unittest

from litellm_menu.core.domains.relay_accounts import RelayAccountsDomain
from litellm_menu.core.service import CoreStore


class RelayMutationHTTPClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str, dict[str, str], dict[str, object] | None]] = []

    def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        if path not in self.responses:
            raise AssertionError(f"unexpected relay path: {path}")
        return self.responses[path]

    def post(
        self,
        origin: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> object:
        self.calls.append(("POST", path, origin, dict(headers), dict(body) if body is not None else None))
        return {"success": True, "data": {"key": "replace-generated-secret"}}

    def put(
        self,
        origin: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> object:
        self.calls.append(("PUT", path, origin, dict(headers), dict(body) if body is not None else None))
        return {"success": True}

    def delete(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        self.calls.append(("DELETE", path, origin, dict(headers), None))
        return {"success": True}


class RelayApiKeyDomainTests(unittest.TestCase):
    def test_newapi_key_crud_is_authenticated_and_secret_free(self) -> None:
        token = {
            "id": 7,
            "name": "old-name",
            "status": 1,
            "key": "replace-secret",
            "expired_time": -1,
            "remain_quota": 0,
            "unlimited_quota": True,
            "model_limits_enabled": False,
            "model_limits": "",
            "allow_ips": "",
            "group": "default",
            "cross_group_retry": False,
        }
        client = RelayMutationHTTPClient(
            {
                "/api/user/models": {"data": ["gpt-test"]},
                "/api/user/self/groups": {"data": {"default": {"ratio": 1}, "premium": {"ratio": 2}}},
                "/api/token/?p=1&size=100": {"data": {"items": [token]}},
                "/api/token/7": {"data": token},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person", cookie="session=fixture")
            refreshed = domain.refresh_resources(account["id"])
            self.assertEqual("newapi-7", refreshed["resources"][0]["id"])
            self.assertTrue(refreshed["resources"][0]["enabled"])
            self.assertEqual(
                [
                    {"id": "default", "name": "default", "multiplier": 1.0},
                    {"id": "premium", "name": "premium", "multiplier": 2.0},
                ],
                refreshed["groups"],
            )
            self.assertNotIn("replace-secret", json.dumps(domain.snapshot()))

            domain.dispatch("api_key.update", {"account_id": account["id"], "resource_id": "newapi-7", "name": "renamed"})
            domain.dispatch("api_key.set_group", {"account_id": account["id"], "resource_id": "newapi-7", "group_id": "premium"})
            domain.dispatch("api_key.set_enabled", {"account_id": account["id"], "resource_id": "newapi-7", "enabled": False})
            domain.dispatch("api_key.create", {"account_id": account["id"], "name": "new-key"})
            self.assertEqual([], client.calls)
            prepared = domain.prepare_apply()
            self.assertTrue(prepared["ready"])
            domain.execute_pending_operations(prepared, phase="non_destructive")
            domain.reconcile_apply(prepared, phase="non_destructive")
            self.assertEqual("POST", client.calls[0][0])
            self.assertEqual("/api/token/", client.calls[0][1])
            self.assertEqual({"name": "new-key", "unlimited_quota": True}, client.calls[0][4])
            self.assertIn(
                ("PUT", "/api/token/?status_only=true", "https://relay.example.test", {"Cookie": "session=fixture"}, {"id": 7, "status": 2}),
                client.calls,
            )
            self.assertFalse(domain.snapshot()["accounts"][0]["resources"][0]["enabled"])

            domain.dispatch("api_key.delete", {"account_id": account["id"], "resource_id": "newapi-7", "dependency_policy": "delete_models"})
            domain.execute_pending_operations(domain.prepare_apply(), phase="destructive")
            self.assertEqual("DELETE", client.calls[-1][0])
            self.assertEqual("/api/token/7", client.calls[-1][1])
            self.assertNotIn("replace-generated-secret", json.dumps(domain.snapshot()))

    def test_sub2api_key_crud_uses_key_ids_and_never_returns_key_values(self) -> None:
        client = RelayMutationHTTPClient(
            {
                "/api/v1/user/profile": {"data": {"balance": 4.5}},
                "/api/v1/keys?page=1&page_size=100": {
                    "data": {"items": [{"id": "key-one", "name": "old-name", "status": "active", "key": "replace-secret", "group_id": 2, "group": {"name": "Balanced"}}]}
                },
                "/api/v1/groups/available": {"data": [{"id": 2, "name": "Balanced", "rate_multiplier": 1.25}, {"id": 3, "name": "Fast", "rate_multiplier": 2}]},
                "/api/v1/groups/rates": {"data": {"3": 1.5}},
                "/api/v1/channels/available": {"data": [{"platforms": [{"supported_models": ["model-test"]}]}]},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person@example.test", cookie="session=fixture")
            refreshed = domain.refresh_resources(account["id"])
            self.assertEqual(
                [
                    {"id": "2", "name": "Balanced", "multiplier": 1.25},
                    {"id": "3", "name": "Fast", "multiplier": 1.5},
                ],
                refreshed["groups"],
            )

            domain.dispatch("api_key.set_enabled", {"account_id": account["id"], "resource_id": "sub2api-key-one", "enabled": False})
            domain.dispatch("api_key.update", {"account_id": account["id"], "resource_id": "sub2api-key-one", "name": "renamed"})
            domain.dispatch("api_key.set_group", {"account_id": account["id"], "resource_id": "sub2api-key-one", "group_id": "3"})
            domain.dispatch("api_key.create", {"account_id": account["id"]})
            self.assertEqual([], client.calls)
            domain.execute_pending_operations(domain.prepare_apply(), phase="non_destructive")
            domain.reconcile_apply(phase="non_destructive")
            self.assertEqual("POST", client.calls[0][0])
            self.assertEqual("/api/v1/keys", client.calls[0][1])
            self.assertEqual({"name": "API 2"}, client.calls[0][4])
            self.assertIn({"status": "inactive"}, [call[4] for call in client.calls])
            self.assertIn({"name": "renamed"}, [call[4] for call in client.calls])
            self.assertIn({"group_id": 3}, [call[4] for call in client.calls])
            self.assertFalse(domain.snapshot()["accounts"][0]["resources"][0]["enabled"])

            domain.dispatch("api_key.delete", {"account_id": account["id"], "resource_id": "sub2api-key-one", "dependency_policy": "delete_models"})
            domain.execute_pending_operations(domain.prepare_apply(), phase="destructive")
            self.assertEqual("DELETE", client.calls[-1][0])
            self.assertEqual("/api/v1/keys/key-one", client.calls[-1][1])
            self.assertNotIn("replace-secret", json.dumps(domain.snapshot()))

    def test_disabled_keys_remain_visible_but_cannot_be_imported(self) -> None:
        client = RelayMutationHTTPClient(
            {
                "/api/user/models": {"data": ["gpt-test"]},
                "/api/user/self/groups": {"data": {"default": {"ratio": 1}}},
                "/api/token/?p=1&size=100": {
                    "data": {"items": [{"id": 7, "name": "disabled-key", "status": 2, "key": "replace-secret"}]}
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person", cookie="session=fixture")

            refreshed = domain.refresh_resources(account["id"])

            self.assertEqual("ready", refreshed["resource_status"])
            self.assertEqual([False], [item["enabled"] for item in refreshed["resources"]])
            self.assertEqual(
                "sk-replace-generated-secret",
                domain.trusted_secret_value("api_key", f"{account['id']}:newapi-7"),
            )
            self.assertNotIn("replace-generated-secret", json.dumps(domain.snapshot()))

    def test_plaintext_relay_key_read_uses_the_native_only_capability_contract(self) -> None:
        client = RelayMutationHTTPClient(
            {
                "/api/user/models": {"data": ["gpt-test"]},
                "/api/user/self/groups": {"data": {"default": {"ratio": 1}}},
                "/api/token/?p=1&size=100": {
                    "data": {"items": [{"id": 7, "name": "visible-key", "status": 1, "key": "replace-secret"}]}
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person", cookie="session=fixture")
            domain.refresh_resources(account["id"])
            core = CoreStore(domains=[domain])
            target = f"{account['id']}:newapi-7"

            descriptor = core.trusted_secret_descriptor("relay_accounts", "api_key", target)
            self.assertTrue(descriptor["present"])
            self.assertEqual(
                "sk-replace-generated-secret",
                core.trusted_secret_value("relay_accounts", "api_key", target, revision=int(descriptor["revision"])),
            )
            self.assertNotIn("replace-generated-secret", json.dumps(core.snapshot()))


if __name__ == "__main__":
    unittest.main()

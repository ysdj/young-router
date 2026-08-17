from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from litellm_menu.core.domains.relay_accounts import RelayAccountsDomain


class StatefulRelayHTTPClient:
    def __init__(self) -> None:
        self.models = ["model-alpha"]
        self.group_multiplier = 1.25
        self.keys: list[dict[str, object]] = [
            {
                "id": "key-one",
                "name": "Primary",
                "status": "active",
                "key": "sk-replace-primary-secret",
                "group_id": 2,
                "group": {"name": "Balanced"},
            }
        ]
        self.write_calls: list[tuple[str, str, dict[str, object] | None]] = []

    def json(self, _origin: str, path: str, *, headers: dict[str, str]) -> object:
        del headers
        if path == "/api/v1/user/profile":
            return {"data": {"balance": 5}}
        if path == "/api/v1/keys?page=1&page_size=100":
            return {"data": {"items": [dict(item) for item in self.keys]}}
        if path == "/api/v1/groups/available":
            return {
                "data": [
                    {"id": 2, "name": "Balanced", "rate_multiplier": self.group_multiplier},
                    {"id": 3, "name": "Saver", "rate_multiplier": 0.75},
                ]
            }
        if path == "/api/v1/groups/rates":
            return {"data": {}}
        if path == "/api/v1/channels/available":
            return {"data": [{"platforms": [{"supported_models": list(self.models)}]}]}
        raise AssertionError(f"unexpected relay path: {path}")

    def post(
        self,
        _origin: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> object:
        del headers
        self.write_calls.append(("POST", path, dict(body) if body is not None else None))
        if path != "/api/v1/keys":
            raise AssertionError(f"unexpected relay path: {path}")
        created = {
            "id": "key-two",
            "name": str((body or {}).get("name", "API")),
            "status": "active",
            "key": "sk-replace-created-secret",
            "group_id": 2,
            "group": {"name": "Balanced"},
        }
        self.keys.append(created)
        return {"data": {"id": "key-two", "key": "sk-replace-created-secret"}}

    def put(
        self,
        _origin: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> object:
        del headers
        payload = dict(body or {})
        self.write_calls.append(("PUT", path, payload))
        key_id = path.rsplit("/", 1)[-1]
        key = next(item for item in self.keys if item["id"] == key_id)
        if "name" in payload:
            key["name"] = payload["name"]
        if "status" in payload:
            key["status"] = payload["status"]
        if "group_id" in payload:
            key["group_id"] = payload["group_id"]
            key["group"] = {"name": "Saver" if payload["group_id"] == 3 else "Balanced"}
        return {"success": True}

    def delete(self, _origin: str, path: str, *, headers: dict[str, str]) -> object:
        del headers
        self.write_calls.append(("DELETE", path, None))
        key_id = path.rsplit("/", 1)[-1]
        self.keys = [item for item in self.keys if item["id"] != key_id]
        return {"success": True}


class FlakyRelayHTTPClient(StatefulRelayHTTPClient):
    def __init__(self) -> None:
        super().__init__()
        self.put_failure = ""
        self.post_failure = ""

    def post(
        self,
        origin: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> object:
        failure = self.post_failure
        self.post_failure = ""
        if failure == "after_apply":
            super().post(origin, path, headers=headers, body=body)
            raise TimeoutError("response lost after remote create")
        return super().post(origin, path, headers=headers, body=body)

    def put(
        self,
        origin: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> object:
        failure = self.put_failure
        self.put_failure = ""
        if failure == "after_apply":
            super().put(origin, path, headers=headers, body=body)
            raise TimeoutError("response lost after remote apply")
        if failure == "before_apply":
            self.write_calls.append(("PUT", path, dict(body) if body is not None else None))
            raise TimeoutError("request did not reach relay")
        return super().put(origin, path, headers=headers, body=body)


class LinkedImportProvider:
    def __init__(self) -> None:
        self.sources: list[dict[str, object]] = []
        self.import_mode = ""

    def stage_relay_import(self, sources: object, *, import_mode: str) -> dict[str, object]:
        self.sources = [dict(item) for item in sources]  # type: ignore[arg-type]
        self.import_mode = import_mode
        return {"domain": "providers_models", "providers": []}

    def snapshot(self) -> dict[str, object]:
        return {"domain": "providers_models", "providers": []}

    def dispatch(self, _action: str, _payload: object = None) -> dict[str, object]:
        return self.snapshot()


class RelayApplyDomainTests(unittest.TestCase):
    @staticmethod
    def _signed_in_domain(directory: str) -> tuple[RelayAccountsDomain, StatefulRelayHTTPClient, dict[str, object]]:
        client = StatefulRelayHTTPClient()
        domain = RelayAccountsDomain(directory, http_client=client)
        account = domain.dispatch(
            "account.add",
            {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
        )["accounts"][0]
        domain.accept_login_result(
            str(account["id"]),
            username="person@example.test",
            cookie="session=replace-session",
        )
        domain.refresh_resources(str(account["id"]))
        return domain, client, account

    def test_api_key_update_is_staged_and_prepare_has_no_http_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain, client, account = self._signed_in_domain(directory)

            staged = domain.dispatch(
                "api_key.update",
                {
                    "account_id": account["id"],
                    "resource_id": "sub2api-key-one",
                    "name": "Renamed",
                },
            )

            self.assertEqual([], client.write_calls)
            self.assertEqual(1, staged["pending_operation_count"])
            self.assertEqual("api_key_update", staged["pending_operations"][0]["kind"])
            self.assertNotIn("replace-primary-secret", json.dumps(staged))
            prepared = domain.prepare_apply()
            self.assertTrue(prepared["ready"])
            self.assertEqual([], client.write_calls)

            executed = domain.execute_pending_operations(prepared, phase="non_destructive")
            self.assertEqual("applied", executed["status"])
            self.assertEqual("PUT", client.write_calls[0][0])
            reconciled = domain.reconcile_apply(prepared, phase="non_destructive")
            self.assertEqual("applied", reconciled["status"])

            materials = domain.binding_materials(
                [
                    {
                        "station_id": account["station_id"],
                        "account_id": account["id"],
                        "resource_id": "sub2api-key-one",
                    }
                ]
            )
            self.assertEqual("sk-replace-primary-secret", materials["resources"][0]["api_key"])
            self.assertEqual(1.25, materials["resources"][0]["multiplier"])
            self.assertNotIn("replace-primary-secret", json.dumps(domain.snapshot()))

            domain.commit_apply()
            finalized = domain.finalize_apply()
            self.assertEqual("applied", finalized["status"])
            self.assertEqual(0, finalized["pending_operations"])
            reloaded = RelayAccountsDomain(directory, http_client=client)
            self.assertEqual("Renamed", reloaded.snapshot()["accounts"][0]["resources"][0]["name"])

    def test_create_resolves_temporary_id_and_delete_runs_only_in_destructive_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain, client, account = self._signed_in_domain(directory)
            created = domain.dispatch(
                "api_key.create",
                {
                    "account_id": account["id"],
                    "name": "Created",
                    "group_id": "3",
                    "enabled": False,
                },
            )
            temporary_id = created["pending_operations"][0]["resource_id"]
            self.assertTrue(str(temporary_id).startswith("pending-"))
            self.assertEqual([], client.write_calls)

            prepared = domain.prepare_apply()
            domain.execute_pending_operations(prepared, phase="non_destructive")
            domain.reconcile_apply(prepared, phase="non_destructive")
            operation = domain.snapshot()["pending_operations"][0]
            self.assertEqual("sub2api-key-two", operation["resource_id"])
            self.assertEqual(["POST", "PUT", "PUT"], [call[0] for call in client.write_calls])
            domain.commit_apply()
            domain.finalize_apply()

            client.write_calls.clear()
            domain.dispatch(
                "api_key.delete",
                {
                    "account_id": account["id"],
                    "resource_id": "sub2api-key-one",
                    "dependency_policy": "delete_models",
                },
            )
            prepared = domain.prepare_apply()
            domain.execute_pending_operations(prepared, phase="non_destructive")
            self.assertEqual([], client.write_calls)
            domain.commit_apply()
            domain.execute_pending_operations(prepared, phase="destructive")
            self.assertEqual(["DELETE"], [call[0] for call in client.write_calls])
            domain.reconcile_apply(prepared, phase="destructive")
            domain.finalize_apply()
            self.assertNotIn("sub2api-key-one", [item["id"] for item in domain.snapshot()["accounts"][0]["resources"]])

    def test_binding_materials_refresh_dynamic_key_catalog_and_multiplier_without_pending_crud(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain, client, account = self._signed_in_domain(directory)
            source = {
                "station_id": account["station_id"],
                "account_id": account["id"],
                "resource_id": "sub2api-key-one",
            }
            domain.commit_apply()
            self.assertEqual(0, domain.snapshot()["pending_operation_count"])

            client.keys[0]["key"] = "sk-replace-rotated-secret"
            client.models = ["model-beta", "model-gamma"]
            client.group_multiplier = 1.75
            materials = domain.binding_materials({"resources": [source]}, refresh=True)

            self.assertEqual([], materials["issues"])
            self.assertEqual("sk-replace-rotated-secret", materials["resources"][0]["api_key"])
            self.assertEqual(["model-beta", "model-gamma"], materials["resources"][0]["models"])
            self.assertEqual(1.75, materials["resources"][0]["multiplier"])
            self.assertEqual([], client.write_calls)
            self.assertNotIn("replace-rotated-secret", json.dumps(domain.snapshot()))

    def test_reconcile_completes_a_remote_change_when_only_its_response_was_lost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FlakyRelayHTTPClient()
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(str(account["id"]), username="person", cookie="session=fixture")
            domain.refresh_resources(str(account["id"]))
            domain.dispatch(
                "api_key.update",
                {
                    "account_id": account["id"],
                    "resource_id": "sub2api-key-one",
                    "name": "Remotely Applied",
                },
            )
            client.put_failure = "after_apply"

            executed = domain.execute_pending_operations(domain.prepare_apply(), phase="non_destructive")
            self.assertEqual("partial", executed["status"])
            self.assertEqual("local_pending", domain.snapshot()["pending_operations"][0]["state"])
            reconciled = domain.reconcile_apply(phase="non_destructive")

            self.assertEqual("applied", reconciled["status"])
            self.assertEqual("Remotely Applied", domain.snapshot()["accounts"][0]["resources"][0]["name"])
            self.assertEqual(1, len(client.write_calls))
            domain.commit_apply()
            self.assertEqual(0, domain.finalize_apply()["pending_operations"])

    def test_reconcile_requeues_a_remote_change_that_did_not_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FlakyRelayHTTPClient()
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(str(account["id"]), username="person", cookie="session=fixture")
            domain.refresh_resources(str(account["id"]))
            domain.dispatch(
                "api_key.set_enabled",
                {
                    "account_id": account["id"],
                    "resource_id": "sub2api-key-one",
                    "enabled": False,
                },
            )
            client.put_failure = "before_apply"

            first = domain.execute_pending_operations(domain.prepare_apply(), phase="non_destructive")
            self.assertEqual("partial", first["status"])
            reconciled = domain.reconcile_apply(phase="non_destructive")
            self.assertEqual("partial", reconciled["status"])
            self.assertEqual("staged", domain.snapshot()["pending_operations"][0]["state"])

            second = domain.execute_pending_operations(domain.prepare_apply(), phase="non_destructive")
            self.assertEqual("applied", second["status"])
            self.assertEqual(2, len(client.write_calls))
            self.assertEqual("applied", domain.reconcile_apply(phase="non_destructive")["status"])
            domain.commit_apply()
            self.assertEqual(0, domain.finalize_apply()["pending_operations"])

    def test_reconcile_resolves_a_created_key_without_replaying_post_after_response_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FlakyRelayHTTPClient()
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(str(account["id"]), username="person", cookie="session=fixture")
            domain.refresh_resources(str(account["id"]))
            domain.dispatch("api_key.create", {"account_id": account["id"], "name": "Created"})
            client.post_failure = "after_apply"

            executed = domain.execute_pending_operations(domain.prepare_apply(), phase="non_destructive")
            self.assertEqual("partial", executed["status"])
            reconciled = domain.reconcile_apply(phase="non_destructive")

            self.assertEqual("applied", reconciled["status"])
            self.assertEqual("sub2api-key-two", domain.snapshot()["pending_operations"][0]["resource_id"])
            self.assertEqual(["POST"], [call[0] for call in client.write_calls])
            domain.commit_apply()
            self.assertEqual(0, domain.finalize_apply()["pending_operations"])

    def test_login_and_refresh_preserve_an_existing_dirty_account_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain, client, account = self._signed_in_domain(directory)
            storage = Path(directory) / ".litellm-runtime" / "relay-accounts.json"

            self.assertNotEqual({}, domain.draft_state())
            self.assertEqual([], json.loads(storage.read_text(encoding="utf-8"))["accounts"])
            self.assertEqual([], client.write_calls)

            domain.commit_apply()
            persisted = json.loads(storage.read_text(encoding="utf-8"))
            self.assertEqual([account["id"]], [item["id"] for item in persisted["accounts"]])

    def test_local_station_removal_is_staged_and_linked_import_is_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain, client, account = self._signed_in_domain(directory)
            domain.commit_apply()
            provider = LinkedImportProvider()

            imported = domain.import_resources(
                str(account["id"]),
                ["sub2api-key-one"],
                provider,
                mode="linked",
            )

            self.assertEqual("linked", imported["import_mode"])
            self.assertEqual("linked", provider.import_mode)
            self.assertEqual(
                (account["station_id"], account["id"], "sub2api-key-one"),
                (
                    provider.sources[0]["station_id"],
                    provider.sources[0]["account_id"],
                    provider.sources[0]["resource_id"],
                ),
            )
            self.assertNotIn("api_key", provider.sources[0])
            self.assertNotIn("replace-primary-secret", json.dumps(imported))
            self.assertEqual([], client.write_calls)

            before = domain.storage_path.read_bytes()
            removed = domain.dispatch(
                "station.remove",
                {"station_id": account["station_id"], "dependency_policy": "detach"},
            )
            self.assertEqual([], removed["accounts"])
            self.assertEqual(before, domain.storage_path.read_bytes())
            self.assertEqual([], client.write_calls)
            domain.commit_apply()
            reloaded = RelayAccountsDomain(directory, http_client=client)
            self.assertEqual([], reloaded.snapshot()["accounts"])
            self.assertEqual(1, len(reloaded.snapshot()["pending_credential_cleanups"]))


if __name__ == "__main__":
    unittest.main()

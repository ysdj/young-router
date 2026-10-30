from __future__ import annotations

import json
import tempfile
import unittest

from young_router.core.domains.relay_accounts import RelayAccountsDomain
from young_router.core.service import CoreStore


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

            # The refresh above is where the station's key values are read and
            # cached, so the staging below is measured from there: every edit
            # stages locally and reaches the station only through Apply.
            staged_from = len(client.calls)
            domain.dispatch("api_key.update", {"account_id": account["id"], "resource_id": "newapi-7", "name": "renamed"})
            domain.dispatch("api_key.set_group", {"account_id": account["id"], "resource_id": "newapi-7", "group_id": "premium"})
            domain.dispatch("api_key.set_enabled", {"account_id": account["id"], "resource_id": "newapi-7", "enabled": False})
            domain.dispatch("api_key.create", {"account_id": account["id"], "name": "new-key"})
            self.assertEqual(staged_from, len(client.calls))
            prepared = domain.prepare_apply()
            self.assertTrue(prepared["ready"])
            domain.execute_pending_operations(prepared, phase="non_destructive")
            domain.reconcile_apply(prepared, phase="non_destructive")
            # The refresh already spent the key-value read, so Apply is the
            # first thing after it that writes to the station.
            create = next(call for call in client.calls[staged_from:] if call[0] == "POST")
            self.assertEqual("/api/token/", create[1])
            self.assertEqual({"name": "new-key", "unlimited_quota": True}, create[4])
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

    def test_auto_grouping_keeps_first_existing_key_and_deletes_group_duplicates(self) -> None:
        client = RelayMutationHTTPClient(
            {
                "/api/v1/user/profile": {"data": {"balance": 4.5}},
                "/api/v1/keys?page=1&page_size=100": {
                    "data": {
                        "items": [
                            {
                                "id": "key-first",
                                "name": "old-first",
                                "status": "inactive",
                                "key": "replace-first",
                                "group_id": 2,
                                "group": {"name": "Balanced"},
                            },
                            {
                                "id": "key-duplicate",
                                "name": "duplicate",
                                "status": "active",
                                "key": "replace-duplicate",
                                "group_id": 2,
                                "group": {"name": "Balanced"},
                            },
                            {
                                "id": "key-fast",
                                "name": "Fast",
                                "status": "active",
                                "key": "replace-fast",
                                "group_id": 3,
                                "group": {"name": "Fast"},
                            },
                            {
                                "id": "key-stale",
                                "name": "old-invalid-group",
                                "status": "active",
                                "key": "replace-stale",
                                "group_id": 99,
                                "group": {"name": "Removed"},
                            },
                            {
                                "id": "key-ungrouped",
                                "name": "old-ungrouped",
                                "status": "active",
                                "key": "replace-ungrouped",
                            },
                        ]
                    }
                },
                "/api/v1/groups/available": {
                    "data": [
                        {"id": 2, "name": "Balanced", "rate_multiplier": 1.25},
                        {"id": 3, "name": "Fast", "rate_multiplier": 2},
                    ]
                },
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
            domain.refresh_resources(account["id"])
            before_toggle = domain.snapshot()["accounts"][0]

            snapshot = domain.dispatch(
                "api_key.set_auto_grouping",
                {"account_id": account["id"], "enabled": True},
            )
            resources = snapshot["accounts"][0]["resources"]
            first = next(item for item in resources if item["id"] == "sub2api-key-first")
            duplicate = next(item for item in resources if item["id"] == "sub2api-key-duplicate")
            stale = next(item for item in resources if item["id"] == "sub2api-key-stale")
            self.assertEqual("Balanced", first["name"])
            self.assertTrue(first["enabled"])
            self.assertEqual("duplicate", duplicate["name"])
            self.assertEqual("old-invalid-group", stale["name"])

            prepared = domain.prepare_apply()
            self.assertEqual(
                ["sub2api-key-duplicate", "sub2api-key-stale", "sub2api-key-ungrouped"],
                sorted(operation["resource_id"] for operation in prepared["destructive"]),
            )
            self.assertNotIn("api_key_create", [operation["kind"] for operation in prepared["operations"]])
            # Turning the layout on *is* an alignment, so it reports the keys it
            # retires together with the policy that governs them.  Reporting
            # only the switch left every retired key's provider slot and routes
            # in the document while the station call removed their credential.
            self.assertEqual("delete_models", snapshot["last_action"]["dependency_policy"])
            self.assertEqual(
                ["sub2api-key-duplicate", "sub2api-key-stale", "sub2api-key-ungrouped"],
                sorted(row["resource_id"] for row in snapshot["last_action"]["resources"]),
            )

            restored = domain.dispatch(
                "api_key.set_auto_grouping",
                {"account_id": account["id"], "enabled": False},
            )["accounts"][0]
            self.assertFalse(restored["auto_grouping"])
            self.assertEqual(before_toggle["resources"], restored["resources"])
            self.assertEqual(before_toggle["groups"], restored["groups"])
            self.assertEqual(0, restored["pending_operation_count"])
            self.assertEqual([], client.calls)

            snapshot = domain.dispatch(
                "api_key.set_auto_grouping",
                {"account_id": account["id"], "enabled": True},
            )

            # The periodic alignment pass is idempotent while Apply is still
            # pending; it must not enqueue another delete for the same key, and
            # it must not report the previous pass's retirements as its own.
            again = domain.dispatch(
                "api_key.auto_group_align",
                {"account_id": account["id"]},
            )
            self.assertNotIn("resources", again["last_action"])
            self.assertEqual("api_key_auto_group_align", again["last_action"]["kind"])
            prepared_again = domain.prepare_apply()
            self.assertEqual(
                ["sub2api-key-duplicate", "sub2api-key-stale", "sub2api-key-ungrouped"],
                sorted(operation["resource_id"] for operation in prepared_again["destructive"]),
            )

            # The stale key is remote-deleted only when Apply enters its
            # destructive phase; alignment itself only stages the operation.
            domain.execute_pending_operations(prepared_again, phase="destructive")
            self.assertIn(
                ("DELETE", "/api/v1/keys/key-stale", "https://relay.example.test", {"Cookie": "session=fixture"}, None),
                client.calls,
            )
            self.assertIn(
                ("DELETE", "/api/v1/keys/key-ungrouped", "https://relay.example.test", {"Cookie": "session=fixture"}, None),
                client.calls,
            )

    def test_auto_grouping_rehomes_a_key_whose_group_was_renamed(self) -> None:
        """A renamed group is a re-homed key, not a deleted and recreated one.

        On a New API station a group's identifier *is* its name, so renaming a
        group leaves every key that served it naming a group the station no
        longer offers.  Reading that as "this key's group is gone" and answering
        with a delete plus a create stages the layout the user asked for while
        destroying the key: the resource id is the identity a provider slot
        binds, so the slot is left pointing at a key the station no longer
        serves — the pane reads a dangling key, its routes never get the group's
        multiplier, and the credential the station issued for that group is
        thrown away for a new one.
        """

        client = RelayMutationHTTPClient(
            {
                "/api/user/models": {"data": ["gpt-test"]},
                "/api/user/self/groups": {
                    "data": {
                        "Claude Max（禁蒸馏）": {"ratio": 1.05},
                        "福利国模": {"ratio": 0.1},
                    }
                },
                "/api/token/?p=1&size=100": {
                    "data": {
                        "items": [
                            {"id": 1, "name": "Claude Max", "status": 1, "key": "replace-renamed", "group": "Claude Max"},
                            {"id": 2, "name": "福利国模", "status": 1, "key": "replace-kept", "group": "福利国模"},
                            {"id": 3, "name": "retired", "status": 1, "key": "replace-retired", "group": "已下线"},
                        ]
                    }
                },
                "/api/user/self": {"data": {"quota": 500_000}},
                "/api/status": {"data": {"quota_per_unit": 500_000}},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person", access_token="replace-token")
            domain.refresh_resources(account["id"])

            aligned = domain.dispatch(
                "api_key.set_auto_grouping",
                {"account_id": account["id"], "enabled": True},
            )["accounts"][0]

            # The group's key is the same key: its id survives, and it now
            # serves the group under the group's own name.
            rehomed = next(item for item in aligned["resources"] if item["id"] == "newapi-1")
            self.assertEqual("Claude Max（禁蒸馏）", rehomed["name"])
            self.assertEqual("Claude Max（禁蒸馏）", rehomed["group_id"])
            # No key was created for it.
            self.assertFalse(any(
                resource["id"].startswith("pending-") for resource in aligned["resources"]
            ))
            prepared = domain.prepare_apply()
            self.assertNotIn("api_key_create", [operation["kind"] for operation in prepared["operations"]])
            self.assertEqual(
                {"api_key_set_group", "api_key_update"},
                {
                    operation["kind"]
                    for operation in prepared["operations"]
                    if operation["resource_id"] == "newapi-1"
                },
            )
            # A group no key of its own was renamed into keeps its key, and the
            # one key that really is under a group the station dropped is the
            # only thing retired.
            self.assertIn("newapi-2", [resource["id"] for resource in aligned["resources"]])
            self.assertEqual(
                ["newapi-3"],
                sorted(operation["resource_id"] for operation in prepared["destructive"]),
            )

    def test_auto_grouping_disable_after_apply_keeps_the_applied_grouping(self) -> None:
        client = RelayMutationHTTPClient(
            {
                "/api/v1/user/profile": {"data": {"balance": 4.5}},
                "/api/v1/keys?page=1&page_size=100": {
                    "data": {
                        "items": [
                            {
                                "id": "key-one",
                                "name": "Balanced",
                                "status": "active",
                                "key": "replace-key",
                                "group_id": 2,
                                "group": {"name": "Balanced"},
                            }
                        ]
                    }
                },
                "/api/v1/groups/available": {
                    "data": [{"id": 2, "name": "Balanced", "rate_multiplier": 1.25}]
                },
                "/api/v1/groups/rates": {"data": {}},
                "/api/v1/channels/available": {"data": [{"platforms": []}]},
                "/v1/models": {"data": {"data": []}},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person@example.test", cookie="session=fixture")
            domain.refresh_resources(account["id"])

            domain.dispatch(
                "api_key.set_auto_grouping",
                {"account_id": account["id"], "enabled": True},
            )
            domain.apply()
            applied = domain.snapshot()["accounts"][0]
            self.assertTrue(applied["auto_grouping"])

            disabled = domain.dispatch(
                "api_key.set_auto_grouping",
                {"account_id": account["id"], "enabled": False},
            )["accounts"][0]
            self.assertFalse(disabled["auto_grouping"])
            self.assertEqual(applied["resources"], disabled["resources"])
            self.assertEqual(applied["groups"], disabled["groups"])

    def test_auto_grouping_survives_a_refresh_after_a_new_upstream_group(self) -> None:
        """A group the station just added is synced, not wedged by the sheet's own read.

                自动分组 stages two kinds of work around a new upstream group: a
        create for the group that owns no key yet, and a retirement for every
        key whose group the station dropped.  The pane reads the station again
        before it ever applies — the mount probe, 刷新, the alignment interval,
        and 分组管理 opening all do — and that read used to erase the draft row a
        staged create had added while hiding the retired one.  Alignment then saw
        the group as still unowned and staged a *second* create for it on every
        pass, and the retirement's target was gone from the account while the
        journal still had to call the station for it, so Apply refused the whole
        draft with ``resource_unavailable``.  Nothing about that refusal can be
        fixed from the pane: the switch stays on, the new group's key is never
        created, and the key of the dropped group is never retired.
        """

        class Station(RelayMutationHTTPClient):
            def __init__(self) -> None:
                super().__init__({})
                self.keys = [
                    {"id": "key-keep", "name": "Balanced", "status": "active", "key": "replace-keep", "group_id": 2, "group": {"name": "Balanced"}},
                    {"id": "key-dead", "name": "Removed", "status": "active", "key": "replace-dead", "group_id": 99, "group": {"name": "Removed"}},
                ]
                self.groups = [{"id": 2, "name": "Balanced", "rate_multiplier": 1.0}]

            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                if path == "/api/v1/user/profile":
                    return {"data": {"balance": 4.5}}
                if path.startswith("/api/v1/keys"):
                    return {"data": {"items": [dict(item) for item in self.keys]}}
                if path == "/api/v1/groups/available":
                    return {"data": [dict(item) for item in self.groups]}
                if path == "/api/v1/groups/rates":
                    return {"data": {}}
                if path == "/api/v1/channels/available":
                    return {"data": [{"platforms": [{"supported_models": ["model-test"]}]}]}
                raise AssertionError(f"unexpected relay path: {path}")

            def post(self, origin: str, path: str, *, headers: dict[str, str], body: dict[str, object] | None = None) -> object:
                payload = super().post(origin, path, headers=headers, body=body)
                # A real station answers a create with the row it just made, and
                # lists it from then on; reconciliation reads that back.
                created = {"id": "key-new", "name": str((body or {}).get("name", "")), "status": "active", "key": "replace-new", "group_id": None}
                self.keys.append(created)
                return {"success": True, "data": {"id": "key-new"}}

            def delete(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                payload = super().delete(origin, path, headers=headers)
                removed = path.rsplit("/", 1)[-1]
                self.keys = [item for item in self.keys if str(item["id"]) != removed]
                return payload

            def put(self, origin: str, path: str, *, headers: dict[str, str], body: dict[str, object] | None = None) -> object:
                payload = super().put(origin, path, headers=headers, body=body)
                # The create's own metadata (its group, then its name) lands on
                # the row the station just made, and reconciliation reads it back.
                target = path.rsplit("/", 1)[-1]
                for item in self.keys:
                    if str(item["id"]) != target:
                        continue
                    for field in ("name", "group_id", "status"):
                        if body and field in body:
                            item[field] = body[field]
                return payload

        client = Station()
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=client)
            account = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person@example.test", cookie="session=fixture")
            domain.refresh_resources(account["id"])
            domain.dispatch(
                "api_key.set_auto_grouping",
                {"account_id": account["id"], "enabled": True},
            )

            # The station adds a group.  Every read from here is the pane's own.
            client.groups.append({"id": 9, "name": "NewGroup", "rate_multiplier": 0.5})

            def align() -> list[str]:
                domain.refresh_resources(account["id"])
                snapshot = domain.dispatch(
                    "api_key.auto_group_align",
                    {"account_id": account["id"]},
                )["accounts"][0]
                return [resource["name"] for resource in snapshot["resources"]]

            self.assertEqual(["Balanced", "NewGroup"], align())
            # A second pass is a derivation, not an accumulation: the group owns
            # the key the first pass staged, so no second create is filed for it.
            self.assertEqual(["Balanced", "NewGroup"], align())
            self.assertEqual(
                ["NewGroup"],
                [
                    operation["changes"]["name"]
                    for operation in domain._pending_operations
                    if operation["kind"] == "api_key_create"
                ],
            )
            # The retired key is hidden from the sheet, and Apply still stages,
            # performs, and reconciles its deletion by the id the journal holds.
            prepared = domain.prepare_apply()
            self.assertTrue(prepared["ready"], prepared["issues"])
            self.assertEqual(
                ["sub2api-key-dead"],
                [operation["resource_id"] for operation in prepared["destructive"]],
            )
            applied = domain.apply()
            self.assertEqual("applied", applied["status"], applied["issues"])
            self.assertIn(
                ("DELETE", "/api/v1/keys/key-dead", "https://relay.example.test", {"Cookie": "session=fixture"}, None),
                client.calls,
            )
            self.assertIn(
                ("POST", "/api/v1/keys", "https://relay.example.test", {"Cookie": "session=fixture"}, {"name": "NewGroup"}),
                client.calls,
            )

    def test_auto_grouping_without_station_groups_keeps_every_key(self) -> None:
        """An unknown group list is not a layout, so nothing is staged to delete."""

        client = RelayMutationHTTPClient(
            {
                "/api/v1/user/profile": {"data": {"balance": 4.5}},
                "/api/v1/keys?page=1&page_size=100": {
                    "data": {
                        "items": [
                            {
                                "id": "key-ungrouped",
                                "name": "old-ungrouped",
                                "status": "active",
                                "key": "replace-ungrouped",
                            }
                        ]
                    }
                },
                "/api/v1/groups/available": {"data": []},
                "/api/v1/groups/rates": {"data": {}},
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
            domain.refresh_resources(account["id"])
            self.assertEqual([], domain.snapshot()["accounts"][0]["groups"])

            snapshot = domain.dispatch(
                "api_key.set_auto_grouping",
                {"account_id": account["id"], "enabled": True},
            )["accounts"][0]
            # The switch still applies, but a key list without groups keeps
            # every key instead of reading the missing groups as "ungrouped".
            self.assertTrue(snapshot["auto_grouping"])
            self.assertEqual(["sub2api-key-ungrouped"], [resource["id"] for resource in snapshot["resources"]])
            self.assertEqual([], domain.prepare_apply()["destructive"])
            self.assertEqual([], client.calls)

            aligned = domain.dispatch("api_key.auto_group_align", {"account_id": account["id"]})["accounts"][0]
            self.assertEqual(["sub2api-key-ungrouped"], [resource["id"] for resource in aligned["resources"]])
            self.assertEqual([], domain.prepare_apply()["destructive"])

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
    def test_a_newapi_key_for_a_group_is_created_in_that_group(self) -> None:
        """The create request states the group, or the key lands ungrouped.

        A New API token carries its group as a plain top-level field, and the
        station's own column defaults to the empty string when the create omits
        it.  A key created for a group without that field is therefore filed
        under no group at all — which is exactly the key that never shows up
        under the group it was created for after 自动分组 relayouts the account.
        """

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
            domain.refresh_resources(account["id"])

            # The refresh is where the station's key values are read and
            # cached; creating a key stages locally and reaches the station
            # only through Apply.
            staged_from = len(client.calls)
            domain.dispatch(
                "api_key.create",
                {"account_id": account["id"], "name": "premium", "group_id": "premium", "enabled": True},
            )
            self.assertEqual(staged_from, len(client.calls))
            prepared = domain.prepare_apply()
            self.assertTrue(prepared["ready"])
            domain.execute_pending_operations(prepared, phase="non_destructive")

            create = next(call for call in client.calls if call[0] == "POST" and call[1] == "/api/token/")
            self.assertEqual(
                {"name": "premium", "unlimited_quota": True, "group": "premium"},
                create[4],
            )
            # The station's own automatic grouping is the user's own
            # configuration: this app creates an ordinary grouped key and never
            # writes the candidate list or the cross-group retry flag.
            self.assertNotIn("auto_groups", create[4])
            self.assertNotIn("cross_group_retry", create[4])


if __name__ == "__main__":
    unittest.main()

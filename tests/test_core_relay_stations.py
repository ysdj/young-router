from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from young_router.core.domains.relay_accounts import RelayAccountsDomain, RelayAccountsError
from young_router.core.service import CoreStore


class RelayStationDomainTests(unittest.TestCase):
    def test_core_dispatch_keeps_station_actions_on_the_generic_ipc_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            relay = RelayAccountsDomain(directory)
            core = CoreStore(domains=[relay])

            result = core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.add",
                    "payload": {
                        "type": "newapi",
                        "label": "account",
                        "origin": "https://relay.example.test",
                        "station_name": "Relay",
                    },
                }
            )

            self.assertEqual(core.revision, result["revision"])
            snapshot = core.snapshot()["domains"]["relay_accounts"]
            self.assertEqual(1, snapshot["station_count"])
            self.assertEqual("Relay", snapshot["stations"][0]["name"])
            self.assertEqual(snapshot["stations"][0]["id"], snapshot["accounts"][0]["station_id"])

    def test_accounts_with_equivalent_origins_share_a_named_station(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            first = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "account-one",
                    "origin": "https://RELAY.example.test:443/v1/",
                    "station_name": "Relay A",
                },
            )["accounts"][0]
            second = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "account-two",
                    "origin": "https://relay.example.test",
                },
            )["accounts"][1]

            snapshot = domain.snapshot()
            self.assertEqual(1, snapshot["station_count"])
            self.assertEqual("Relay A", snapshot["stations"][0]["name"])
            self.assertEqual(2, snapshot["stations"][0]["account_count"])
            self.assertEqual(first["station_id"], second["station_id"])
            self.assertEqual("https://RELAY.example.test:443", snapshot["stations"][0]["origin"])
            self.assertEqual(3, json.loads((Path(directory) / ".litellm-runtime" / "relay-accounts.json").read_text())["version"])

    def test_legacy_accounts_are_migrated_to_station_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / ".litellm-runtime" / "relay-accounts.json"
            storage.parent.mkdir(parents=True)
            storage.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "accounts": [
                            {"id": "account-a", "type": "newapi", "label": "Relay", "origin": "https://relay.example.test/v1"},
                            {"id": "account-b", "type": "newapi", "label": "Relay", "origin": "https://RELAY.example.test:443"},
                        ],
                        "pending_credential_cleanups": [],
                    }
                )
            )

            domain = RelayAccountsDomain(directory)

            snapshot = domain.snapshot()
            self.assertEqual(1, snapshot["station_count"])
            self.assertEqual({"account-a", "account-b"}, {item["id"] for item in snapshot["accounts"]})
            self.assertEqual(1, len({item["station_id"] for item in snapshot["accounts"]}))
            migrated = json.loads(storage.read_text())
            self.assertEqual(3, migrated["version"])
            self.assertEqual(1, len(migrated["stations"]))
            self.assertTrue(all(item.get("station_id") for item in migrated["accounts"]))

    def test_account_add_can_select_an_existing_station(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            first = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "first", "origin": "https://one.example.test", "station_name": "One"},
            )["accounts"][0]
            station_id = first["station_id"]

            second = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "second", "station_id": station_id},
            )["accounts"][1]
            self.assertEqual(station_id, second["station_id"])
            self.assertEqual("https://one.example.test", second["origin"])

            with self.assertRaises(RelayAccountsError):
                domain.dispatch(
                    "account.add",
                    {
                        "type": "sub2api",
                        "label": "wrong-site",
                        "station_id": station_id,
                        "origin": "https://two.example.test",
                    },
                )

    def test_station_update_changes_all_account_origins_and_merges_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            first = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "first", "origin": "https://one.example.test", "station_name": "One"},
            )["accounts"][0]
            second = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "second", "origin": "https://two.example.test", "station_name": "Two"},
            )["accounts"][1]
            stations = domain.snapshot()["stations"]

            domain.accept_login_result(first["id"], username="user", cookie="session=fixture")
            merged = domain.dispatch(
                "station.update",
                {"id": stations[0]["id"], "name": "Merged", "origin": "https://two.example.test"},
            )

            self.assertEqual(1, merged["station_count"])
            self.assertEqual("Merged", merged["stations"][0]["name"])
            self.assertEqual(2, merged["stations"][0]["account_count"])
            self.assertEqual(second["station_id"], merged["accounts"][0]["station_id"])
            self.assertEqual("signed_out", merged["accounts"][0]["login_status"])
            self.assertFalse(domain.secret_present("session", first["id"]))

    def test_www_alias_update_keeps_login_and_resources_on_one_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            account = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "account",
                    "origin": "https://www.tken.fun",
                    "station_name": "tken",
                },
            )["accounts"][0]
            domain.accept_login_result(
                account["id"],
                username="person@example.test",
                cookie="session=fixture",
                password="saved-password",
                remember_password=True,
            )
            domain._accounts[0]["resources"] = [
                {
                    "id": "key-1",
                    "name": "group-a",
                    "api_name": "group-a",
                    "api_base": "https://www.tken.fun/v1",
                    "key_hint": "sk-fixture",
                    "enabled": True,
                    "models": ["gpt-4o"],
                    "group_id": "group-a",
                    "group_name": "group-a",
                }
            ]
            domain._accounts[0]["groups"] = [{"id": "group-a", "name": "group-a", "multiplier": 1.0}]

            updated = domain.dispatch(
                "station.update",
                {"id": account["station_id"], "name": "tken", "origin": "https://tken.fun"},
            )

            self.assertEqual("https://tken.fun", updated["stations"][0]["origin"])
            migrated = updated["accounts"][0]
            self.assertEqual("https://tken.fun", migrated["origin"])
            self.assertEqual("signed_in", migrated["login_status"])
            self.assertTrue(migrated["password_saved"])
            self.assertEqual(["key-1"], [item["id"] for item in migrated["resources"]])
            self.assertEqual(["group-a"], [item["id"] for item in migrated["groups"]])
            self.assertEqual("session=fixture", domain._accounts[0]["session"]["cookie"])
            self.assertEqual("saved-password", domain._accounts[0]["password"])

            domain.apply()
            stored = json.loads((Path(directory) / ".litellm-runtime" / "relay-accounts.json").read_text())
            self.assertEqual("https://tken.fun", stored["stations"][0]["origin"])
            self.assertNotIn("origin", stored["accounts"][0])
            reloaded = RelayAccountsDomain(directory)
            reloaded_account = reloaded.snapshot()["accounts"][0]
            self.assertEqual("https://tken.fun", reloaded_account["origin"])
            self.assertEqual("signed_in", reloaded_account["login_status"])
            self.assertTrue(reloaded_account["password_saved"])

    def test_migration_repairs_only_url_labels_after_station_canonicalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / ".litellm-runtime" / "relay-accounts.json"
            storage.parent.mkdir(parents=True)
            storage.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "stations": [
                            {"id": "station-tken", "name": "tken", "origin": "https://tken.fun", "type": "newapi"},
                        ],
                        "accounts": [
                            {
                                "id": "url-label",
                                "station_id": "station-tken",
                                "type": "newapi",
                                "label": "https://www.tken.fun",
                                "username": "person@example.test",
                            },
                            {
                                "id": "named-label",
                                "station_id": "station-tken",
                                "type": "newapi",
                                "label": "Work account",
                                "username": "other@example.test",
                            },
                        ],
                    }
                )
            )

            domain = RelayAccountsDomain(directory)

            self.assertEqual(
                {"url-label": "tken", "named-label": "Work account"},
                {item["id"]: item["label"] for item in domain.snapshot()["accounts"]},
            )
            stored = json.loads(storage.read_text())
            self.assertEqual(
                {"url-label": "tken", "named-label": "Work account"},
                {item["id"]: item["label"] for item in stored["accounts"]},
            )
            self.assertNotIn("origin", stored["accounts"][0])

    def test_restore_uses_station_identity_when_account_label_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "tken", "origin": "https://tken.fun", "station_name": "tken"},
            )["accounts"][0]
            core = CoreStore(domains=[domain])

            restored = core.restore_relay_session(
                account_id=account["id"],
                account_type="newapi",
                label="https://www.tken.fun",
                origin="https://www.tken.fun",
                login_status="signed_in",
                username="person@example.test",
                cookie="session=fixture",
            )

            self.assertEqual("signed_in", restored["login_status"])

    def test_deleting_last_account_removes_empty_station(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "only", "origin": "https://relay.example.test"},
            )["accounts"][0]
            result = domain.dispatch("account.delete", {"id": account["id"]})
            self.assertEqual([], result["stations"])
            self.assertEqual([], result["accounts"])

    def test_deleting_an_account_deletes_the_keys_it_owned(self) -> None:
        """The delete a pane sends is the delete that reaches the providers.

        ``account.delete`` normalizes to ``account_delete``; only the
        ``*_remove`` spellings were listed as dependency actions, so the
        provider policy never ran and the account's key slots stayed behind --
        drawn as a relay key with no account name beside a 中转站关联 section
        with nothing in it.  The station keeps its own keys: this is the local
        copy, never a station write.
        """

        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "only", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "kept", "origin": "https://kept.example.test"},
            )

            result = domain.dispatch("account.delete", {"id": account["id"]})

            self.assertEqual(["kept"], [item["label"] for item in result["accounts"]])
            # The action names the account's own resources, which is what the
            # provider policy turns into "delete this slot and its routes".
            self.assertEqual("account_remove", result["last_action"]["kind"])
            self.assertEqual("delete_models", result["last_action"]["dependency_policy"])
            self.assertEqual(
                [{"station_id": account["station_id"], "account_id": account["id"], "resource_id": ""}]
                if result["last_action"].get("resources")
                else [],
                result["last_action"].get("resources") or [],
            )

    def test_station_type_update_reclassifies_accounts_and_invalidates_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "account", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="user", cookie="session=fixture")

            result = domain.dispatch(
                "station.update",
                {"id": account["station_id"], "type": "sub2api"},
            )

            self.assertEqual("sub2api", result["stations"][0]["type"])
            self.assertEqual("sub2api", result["accounts"][0]["type"])
            self.assertEqual("signed_out", result["accounts"][0]["login_status"])
            self.assertFalse(domain.secret_present("session", account["id"]))


if __name__ == "__main__":
    unittest.main()

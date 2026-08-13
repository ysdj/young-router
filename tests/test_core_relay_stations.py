from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from litellm_menu.core.domains.relay_accounts import RelayAccountsDomain, RelayAccountsError
from litellm_menu.core.service import CoreStore


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

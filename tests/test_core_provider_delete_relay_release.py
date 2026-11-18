from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.relay_accounts import RelayAccountsDomain
from young_router.core.service import CoreStore


class ProviderDeleteRelayReleaseTests(unittest.TestCase):
    """A provider delete takes the station only it still reached.

    A relay sign-in stores an account of its own, and the row that made it worth
    keeping is the provider the wizard creates.  That provider was cleaned up by
    the pane, but only when it had finished binding to the station: a row whose
    bind never landed (the wizard's own create when the bind was refused) was an
    ordinary custom provider, so deleting it left the station, the account, and
    the dashboard session behind with no row anywhere naming them.  The release
    now lives in Core's delete and asks the same question the bind asks — which
    station does this row reach — so every caller gets it, bound or not.
    """

    def _core(self, root: Path) -> tuple[CoreStore, RelayAccountsDomain, ProvidersModelsDomain]:
        relay = RelayAccountsDomain(root)
        providers = ProvidersModelsDomain(root / "config.yaml")
        return CoreStore(domains=[relay, providers]), relay, providers

    def _signed_in_station(self, core: CoreStore, relay: RelayAccountsDomain, origin: str) -> tuple[str, str]:
        core.dispatch(
            {
                "domain": "relay_accounts",
                "type": "account.add",
                "payload": {"type": "sub2api", "label": "wawapii", "origin": origin},
            }
        )
        # The station is part of the document the user already has: a delete
        # that released nothing would be indistinguishable from a release that
        # returned the draft to its baseline, so the fixture applies it first.
        core.apply(domains=["relay_accounts"], revision=core.revision)
        snapshot = relay.snapshot()
        station_id = snapshot["stations"][0]["id"]
        account_id = snapshot["accounts"][0]["id"]
        return station_id, account_id

    def _provider(
        self,
        core: CoreStore,
        providers: ProvidersModelsDomain,
        *,
        name: str,
        api_base: str,
    ) -> str:
        core.dispatch(
            {
                "domain": "providers_models",
                "type": "provider.add",
                "payload": {
                    "provider": {
                        "name": name,
                        "api_base": api_base,
                        "auth_kind": "api_key",
                        "enabled": True,
                        "models": [],
                    }
                },
            }
        )
        return next(
            provider["editor_id"]
            for provider in providers.snapshot()["providers"]
            if provider["name"] == name
        )

    def test_a_provider_whose_bind_never_landed_still_takes_its_station(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            core, relay, providers = self._core(Path(directory))
            station_id, account_id = self._signed_in_station(core, relay, "https://wawapii.com")
            # The address is the station's own site; the row never said
            # `provider_type: relay`, which is exactly the state the old cleanup
            # read as "this delete has nothing to do with the relay".
            provider_id = self._provider(core, providers, name="wawapii", api_base="wawapii.com")
            self.assertEqual("custom", providers.snapshot()["providers"][0]["provider_type"])

            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.delete",
                    "payload": {"provider_id": provider_id},
                }
            )

            snapshot = relay.snapshot()
            self.assertEqual([], snapshot["stations"])
            self.assertEqual([], snapshot["accounts"])
            # The removal is staged in the relay draft, which is what the Apply
            # that follows writes: a station deleted in memory but never marked
            # dirty would come back on the next read.
            staged = core.commit_plan(["relay_accounts", "providers_models"])
            self.assertTrue(staged["drafts"]["relay_accounts"]["dirty"])
            # The credential tombstone travels in the snapshot, because only the
            # native host can erase the keychain entry.  The storage key names a
            # credential; the wire name may not, or the snapshot redacts the
            # whole list to the string "configured" and nothing ever confirms it.
            self.assertEqual(
                [{"account_id": account_id, "label": "wawapii", "kind": "credentials"}],
                core.snapshot()["domains"]["relay_accounts"]["pending_cleanups"],
            )
            self.assertNotEqual("", station_id)
            self.assertNotEqual("", account_id)
            core.apply(domains=["relay_accounts", "providers_models"], revision=core.revision)
            self.assertEqual([], relay.snapshot()["stations"])
            self.assertEqual([], relay.snapshot()["accounts"])

    def test_deleting_an_account_leaves_an_apply_that_still_passes(self) -> None:
        """The delete is only done when the Apply after it succeeds.

        Measured on the live app: deleting the account answered ok, then the
        apply that follows refused with ``provider_model_invalid`` -- the
        provider still named the station the delete had just emptied, and the
        relay resolution that runs during an apply cannot resolve a station with
        no accounts.  The pane reported the whole press as 操作失败 while the
        account was already gone, which is the worst of both.
        """

        from tests.test_core_relay_accounts import FakeRelayHTTPClient

        responses = {
            "/api/user/models": {"success": True, "data": ["chat-a"]},
            "/api/token/?p=1&size=100": {
                "success": True,
                "data": {"items": [{"id": 7, "name": "Cursor", "status": 1, "key": "sk-replace-cursor"}]},
            },
            "/api/token/7/key": {"success": True, "data": {"key": "sk-replace-cursor"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root, http_client=FakeRelayHTTPClient(responses))
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(domains=[relay, providers])
            core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.add",
                    "payload": {"type": "newapi", "label": "wawapii", "origin": "https://wawapii.com"},
                }
            )
            core.apply(domains=["relay_accounts"], revision=core.revision)
            account = relay.snapshot()["accounts"][0]
            account_id = account["id"]
            relay.accept_login_result(
                account_id, username="station-user@example.com", cookie="session=live", remember_password=True
            )
            relay.refresh_resources(account_id, force=True)
            resource = relay.snapshot()["accounts"][0]["resources"][0]
            source = {
                "station_id": account["station_id"],
                "account_id": account_id,
                "resource_id": resource["id"],
                "api_key_name": resource["name"],
                "resource_name": resource["name"],
                "provider_name": "wawapii",
                "models": list(resource["models"])[:1],
                "multiplier": 1.0,
                "api_base": "https://wawapii.com",
            }
            providers.stage_relay_import([source])
            providers.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "sk-replace-cursor"}]}
            )
            # A key of the person's own on the same provider: the account
            # delete must not take it, so the row cannot be pruned as empty.
            wawa = providers.snapshot()["providers"][0]
            providers.dispatch("provider_key_add", {"provider_id": wawa["editor_id"], "name": "my-own-key"})
            providers.stage_secret("api_key", f"{wawa['editor_id']}\x1fmy-own-key", "sk-my-own-key")
            core.apply(domains=["providers_models"], revision=core.revision)
            self.assertEqual(1, len(providers.snapshot()["providers"][0]["models"]))
            core.dispatch(
                {"domain": "relay_accounts", "type": "account.delete", "payload": {"id": account_id}}
            )

            # The Apply that follows the delete is the half that failed live.
            applied = core.apply(domains=["relay_accounts", "providers_models"], revision=core.revision)
            self.assertTrue(applied.get("applied"), applied)
            # The routes the key carried are gone, and no provider still names
            # the station the delete emptied.  A row left with nothing at all is
            # dropped by the policy's own filter, which is the other answer.
            remaining = providers.snapshot()["providers"]
            self.assertEqual([], [p for p in remaining if p.get("relay_station_id")])
            # The provider row survives the account delete: its name, address
            # and any key of the person's own are not the account's to take.
            survivor = [p for p in remaining if p.get("name") == "wawapii"]
            self.assertEqual(1, len(survivor))
            self.assertEqual(["my-own-key"], [k.get("name") for k in survivor[0].get("key_states") or []])
            self.assertEqual([], survivor[0].get("models") or [])
            self.assertEqual("", survivor[0].get("relay_station_id") or "")
            self.assertEqual(
                [],
                [model for p in remaining for model in p.get("models") or [] if model.get("provider") == "wawapii"],
            )

    def test_a_provider_whose_account_is_deleted_stays_with_no_keys_at_all(self) -> None:
        """The provider is the person's row, not the account's.

        Measured live: deleting a relay account deleted the whole provider with
        it, because the dependency policy pruned every row left without a key or
        a route.  That pruning was written for a delete of the provider itself;
        an account delete now keeps the row, so re-adding the account lands back
        on the name and address it had.
        """

        from tests.test_core_relay_accounts import FakeRelayHTTPClient

        responses = {
            "/api/user/models": {"success": True, "data": ["chat-a"]},
            "/api/token/?p=1&size=100": {
                "success": True,
                "data": {"items": [{"id": 7, "name": "Cursor", "status": 1, "key": "sk-cursor"}]},
            },
            "/api/token/7/key": {"success": True, "data": {"key": "sk-cursor"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root, http_client=FakeRelayHTTPClient(responses))
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(domains=[relay, providers])
            core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.add",
                    "payload": {"type": "newapi", "label": "wawapii", "origin": "https://wawapii.com"},
                }
            )
            core.apply(domains=["relay_accounts"], revision=core.revision)
            account = relay.snapshot()["accounts"][0]
            account_id = account["id"]
            relay.accept_login_result(
                account_id, username="station-user@example.com", cookie="session=live", remember_password=True
            )
            relay.refresh_resources(account_id, force=True)
            resource = relay.snapshot()["accounts"][0]["resources"][0]
            source = {
                "station_id": account["station_id"],
                "account_id": account_id,
                "resource_id": resource["id"],
                "api_key_name": resource["name"],
                "resource_name": resource["name"],
                "provider_name": "wawapii",
                "models": list(resource["models"])[:1],
                "multiplier": 1.0,
                "api_base": "https://wawapii.com",
            }
            providers.stage_relay_import([source])
            providers.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "sk-cursor"}]}
            )
            core.apply(domains=["providers_models"], revision=core.revision)
            before = providers.snapshot()["providers"][0]
            self.assertTrue(before["key_states"])

            core.dispatch(
                {"domain": "relay_accounts", "type": "account.delete", "payload": {"id": account_id}}
            )
            applied = core.apply(domains=["relay_accounts", "providers_models"], revision=core.revision)
            self.assertTrue(applied.get("applied"), applied)
            rows = providers.snapshot()["providers"]
            self.assertEqual(["wawapii"], [p.get("name") for p in rows])
            self.assertEqual([], rows[0]["key_states"])
            self.assertEqual([], rows[0]["models"])
            self.assertEqual("https://wawapii.com/v1", rows[0]["api_base"])

    def test_a_provider_that_still_names_an_absent_station_can_be_deleted(self) -> None:
        """Releasing a station that is already gone is not a failure.

        Measured on the live app: the account behind a provider had been deleted
        (which takes its station with it), and pressing − on the leftover row
        answered 校验未通过，更改未生效。.  The release asked the relay store to
        remove a station id that had been gone for hours; that refusal rolled
        back the whole delete, so the row a person was trying to remove could
        never be removed.
        """

        with tempfile.TemporaryDirectory() as directory:
            core, relay, providers = self._core(Path(directory))
            station_id, account_id = self._signed_in_station(core, relay, "https://wawapii.com")
            provider_id = self._provider(core, providers, name="wawapii", api_base="wawapii.com")
            core.dispatch(
                {"domain": "relay_accounts", "type": "account.delete", "payload": {"id": account_id}}
            )
            # The residue an older build left: the row still reaches a station
            # the relay store no longer has.
            for provider in providers._draft["providers"]:
                if provider.get("name") == "wawapii":
                    provider["relay_station_id"] = station_id

            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.delete",
                    "payload": {"provider_id": provider_id},
                }
            )

            self.assertEqual([], relay.snapshot()["stations"])
            self.assertEqual(
                [],
                [item for item in providers.snapshot()["providers"] if item.get("name") == "wawapii"],
            )

    def test_the_removal_is_written_down_so_a_sync_cannot_reinstate_it(self) -> None:
        """A deletion is durable content, not the absence of content.

        The relay store is a synced file compared as a whole, so "this machine
        never had it" and "this machine deleted it" look identical to the
        merge: an older bundle from another machine brought the deleted station
        and its twelve keys straight back.  The store now names the ids it
        removed, and that list survives the reload a restart performs.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, relay, providers = self._core(root)
            station_id, account_id = self._signed_in_station(core, relay, "https://wawapii.com")
            provider_id = self._provider(core, providers, name="wawapii", api_base="wawapii.com")
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.delete",
                    "payload": {"provider_id": provider_id},
                }
            )
            core.apply(domains=["relay_accounts", "providers_models"], revision=core.revision)

            stored = json.loads(relay.storage_path.read_text(encoding="utf-8"))
            self.assertEqual([station_id], [row["id"] for row in stored["removed_stations"]])
            self.assertEqual([account_id], [row["id"] for row in stored["removed_accounts"]])
            self.assertTrue(all(row["removed_at"] for row in stored["removed_stations"]))

            # A restart reads the same document back: the tombstones are part of
            # the store, not process memory.
            reopened = RelayAccountsDomain(root)
            self.assertEqual([], reopened.snapshot()["stations"])
            self.assertEqual(
                [station_id],
                [row["id"] for row in json.loads(reopened.storage_path.read_text(encoding="utf-8"))["removed_stations"]],
            )

    def test_a_row_that_exists_again_drops_its_tombstone(self) -> None:
        """A document carrying both the row and its tombstone is contradictory.

        An import or a package can put a station back with the id a tombstone
        names.  The row is the later fact, so the tombstone is dropped rather
        than failing the file -- and rather than suppressing the row it names.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root)
            relay.storage_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "stations": [{"id": "station-back", "name": "back", "origin": "https://back.example.test", "type": "newapi"}],
                        "accounts": [],
                        "pending_credential_cleanups": [],
                        "removed_stations": [{"id": "station-back", "removed_at": "2026-01-01T00:00:00Z"}],
                        "removed_accounts": [],
                    }
                ),
                encoding="utf-8",
            )
            relay.reload()
            self.assertEqual(["https://back.example.test"], [station["origin"] for station in relay.snapshot()["stations"]])
            stored = json.loads(relay.storage_path.read_text(encoding="utf-8"))
            self.assertEqual([], stored["removed_stations"])

    def test_a_station_another_row_still_reaches_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            core, relay, providers = self._core(Path(directory))
            self._signed_in_station(core, relay, "https://wawapii.com")
            deleted = self._provider(core, providers, name="wawapii", api_base="wawapii.com")
            # A second row at the same site: the station is still reachable, so
            # deleting one of the two must not log the account out from under it.
            self._provider(core, providers, name="wawapii-mirror", api_base="https://wawapii.com/v1")

            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.delete",
                    "payload": {"provider_id": deleted},
                }
            )

            self.assertEqual(1, len(relay.snapshot()["stations"]))
            self.assertEqual(1, len(relay.snapshot()["accounts"]))
            self.assertFalse(core.commit_plan(["relay_accounts", "providers_models"])["drafts"]["relay_accounts"]["dirty"])

    def test_a_provider_that_reaches_no_station_deletes_only_itself(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            core, relay, providers = self._core(Path(directory))
            self._signed_in_station(core, relay, "https://wawapii.com")
            elsewhere = self._provider(core, providers, name="unrelated", api_base="https://elsewhere.example.test/v1")

            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.delete",
                    "payload": {"provider_id": elsewhere},
                }
            )

            self.assertEqual(1, len(relay.snapshot()["stations"]))
            self.assertEqual(1, len(relay.snapshot()["accounts"]))


if __name__ == "__main__":
    unittest.main()

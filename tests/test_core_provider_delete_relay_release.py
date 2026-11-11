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

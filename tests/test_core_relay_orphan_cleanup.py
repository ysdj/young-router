from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.relay_accounts import RelayAccountsDomain
from young_router.core.service import CoreStore


class RelayOrphanCleanupTests(unittest.TestCase):
    """A station survives only while a provider still reaches it.

    The wizard signs in before it creates the provider row, so dismissing that
    window used to leave a station and its accounts with no provider anywhere
    naming them.  The same shape can be written by an older build.  Startup
    repair removes those rows; a station a provider still reaches by source or
    by address is left alone.
    """

    def _core(self, root: Path) -> tuple[CoreStore, RelayAccountsDomain, ProvidersModelsDomain]:
        relay = RelayAccountsDomain(root)
        providers = ProvidersModelsDomain(root / "config.yaml")
        return CoreStore(domains=[relay, providers]), relay, providers

    def _station(self, core: CoreStore, relay: RelayAccountsDomain, origin: str = "https://wawapii.com") -> tuple[str, str]:
        core.dispatch(
            {
                "domain": "relay_accounts",
                "type": "account.add",
                "payload": {"type": "sub2api", "label": "wawapii", "origin": origin},
            }
        )
        core.apply(domains=["relay_accounts"], revision=core.revision)
        snapshot = relay.snapshot()
        station = next(item for item in snapshot["stations"] if item["origin"] == origin)
        account = next(item for item in snapshot["accounts"] if item["station_id"] == station["id"])
        return station["id"], account["id"]

    def _provider(self, core: CoreStore, providers: ProvidersModelsDomain, name: str, api_base: str) -> str:
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

    def test_a_station_with_no_provider_is_removed_and_written_down(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, relay, _providers = self._core(root)
            station_id, account_id = self._station(core, relay)

            removed = core.prune_orphan_relay_stations()

            self.assertEqual([station_id], removed)
            self.assertEqual([], relay.snapshot()["stations"])
            self.assertEqual([], relay.snapshot()["accounts"])
            stored = json.loads(relay.storage_path.read_text(encoding="utf-8"))
            self.assertEqual([station_id], [row["id"] for row in stored["removed_stations"]])
            self.assertEqual([account_id], [row["id"] for row in stored["removed_accounts"]])
            self.assertEqual(
                [account_id],
                [row["account_id"] for row in core.snapshot()["domains"]["relay_accounts"]["pending_cleanups"]],
            )

    def test_a_station_reached_by_the_providers_address_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, relay, providers = self._core(root)
            station_id, _account_id = self._station(core, relay)
            self._provider(core, providers, "wawapii", "https://wawapii.com/v1")
            core.apply(domains=["providers_models"], revision=core.revision)

            self.assertEqual([], core.prune_orphan_relay_stations())
            self.assertEqual([station_id], [station["id"] for station in relay.snapshot()["stations"]])

    def test_a_station_bound_by_source_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, relay, providers = self._core(root)
            station_id, _account_id = self._station(core, relay)
            provider_id = self._provider(core, providers, "bound", "https://elsewhere.example.test/v1")
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.select_relay_station",
                    "payload": {
                        "provider_id": provider_id,
                        "station_id": station_id,
                    },
                }
            )
            core.apply(domains=["providers_models"], revision=core.revision)

            self.assertEqual([], core.prune_orphan_relay_stations())
            self.assertEqual([station_id], [station["id"] for station in relay.snapshot()["stations"]])

    def test_a_narrowed_sweep_leaves_other_orphans_for_their_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, relay, _providers = self._core(root)
            first, _ = self._station(core, relay, "https://first.example.test")
            second, _ = self._station(core, relay, "https://second.example.test")

            result = core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "prune_orphans",
                    "payload": {"station_ids": [first]},
                }
            )
            self.assertEqual([first], result["removed"])
            self.assertEqual([second], [station["id"] for station in relay.snapshot()["stations"]])


if __name__ == "__main__":
    unittest.main()

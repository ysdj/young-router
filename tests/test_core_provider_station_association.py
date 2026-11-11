from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.relay_accounts import RelayAccountsDomain


EMPTY_CONFIG = """
providers: {}

model_list: []
"""


class RelayStationProviderAssociationTests(unittest.TestCase):
    """A provider may carry a relay station without a key or a route.

    The station is an association of the provider, not a provider type of its
    own.  The wizard used to require a provided key before it would move past
    密钥, and the only way to finish without models still imported that key.
    Binding the station to its provider must not invent a key slot or a model.
    """

    def test_a_provider_can_carry_a_station_with_no_key_and_no_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".litellm-runtime"
            runtime.mkdir()
            config_path = runtime / "config.yaml"
            config_path.write_text(textwrap.dedent(EMPTY_CONFIG).lstrip(), encoding="utf-8")
            providers = ProvidersModelsDomain(config_path)
            relay = RelayAccountsDomain(directory)

            account = relay.dispatch(
                "account.add",
                {
                    "type": "sub2api",
                    "label": "wawapii",
                    "origin": "https://wawapii.com",
                    "station_name": "wawapii",
                },
            )["accounts"][0]
            relay.accept_login_result(account["id"], username="person@example.test", cookie="session=fixture")
            station = relay.snapshot()["stations"][0]

            providers.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "wawapii",
                        "api_base": "https://wawapii.com/v1",
                        "auth_kind": "api_key",
                        "enabled": True,
                        "models": [],
                    },
                },
            )
            provider_id = providers.snapshot()["providers"][0]["editor_id"]
            providers.dispatch(
                "provider.select_relay_station",
                {
                    "provider_id": provider_id,
                    "source": {
                        "kind": "relay",
                        "station_id": station["id"],
                        "name": station["name"],
                        "api_base": station["origin"],
                    },
                },
            )

            provider = providers.snapshot()["providers"][0]
            self.assertEqual("relay", provider["provider_type"])
            self.assertEqual(station["id"], provider["relay_station_id"])
            self.assertEqual([], provider["key_states"])
            self.assertEqual([], provider["models"])
            self.assertEqual({"valid": True, "errors": []}, providers.validate())

            providers.apply()
            written = config_path.read_text(encoding="utf-8")
            self.assertIn("wawapii", written)
            self.assertIn("x-young-router-provider-source: {kind: relay, station_id:", written)
            # No key slot, no route: the station's own keys stay on the station.
            self.assertIn("x-young-router-relay-keys: {version: 1, slots: []}", written)
            self.assertIn("model_list: []", written)

    def test_a_provider_keeps_every_account_of_its_station(self) -> None:
        """One provider carries the station, not one of its accounts.

        A user who signs a second account in while adding a station gets two
        accounts on one station; the provider row that the wizard creates is
        bound to the station and must not narrow it back to the first login.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".litellm-runtime"
            runtime.mkdir()
            config_path = runtime / "config.yaml"
            config_path.write_text(textwrap.dedent(EMPTY_CONFIG).lstrip(), encoding="utf-8")
            providers = ProvidersModelsDomain(config_path)
            relay = RelayAccountsDomain(directory)
            for username in ("first@example.test", "second@example.test"):
                account = relay.dispatch(
                    "account.add",
                    {
                        "type": "newapi",
                        "label": "relay",
                        "origin": "https://relay.example.test",
                        "station_name": "Relay",
                    },
                )["accounts"][-1]
                relay.accept_login_result(account["id"], username=username, cookie="session=fixture")
            station = relay.snapshot()["stations"][0]
            self.assertEqual(2, station["account_count"])

            providers.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "Relay",
                        "api_base": "https://relay.example.test/v1",
                        "auth_kind": "api_key",
                        "enabled": True,
                        "models": [],
                    },
                },
            )
            provider_id = providers.snapshot()["providers"][0]["editor_id"]
            providers.dispatch(
                "provider.select_relay_station",
                {
                    "provider_id": provider_id,
                    "source": {
                        "kind": "relay",
                        "station_id": station["id"],
                        "name": station["name"],
                        "api_base": station["origin"],
                    },
                },
            )

            provider = providers.snapshot()["providers"][0]
            self.assertEqual([], provider["key_states"])
            self.assertEqual([], provider["models"])
            self.assertEqual(2, relay.snapshot()["stations"][0]["account_count"])
            self.assertEqual(
                {"first@example.test", "second@example.test"},
                {account["username"] for account in relay.snapshot()["accounts"]},
            )


if __name__ == "__main__":
    unittest.main()

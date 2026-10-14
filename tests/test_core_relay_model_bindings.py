from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from young_router.core.domains.providers_models import DomainError, ProvidersModelsDomain


def relay_source(
    *,
    station_id: str = "station-a",
    account_id: str = "account-a",
    resource_id: str = "resource-a",
    provider_name: str = "relay-station-a",
    models: list[str] | None = None,
    multiplier: float | None = 1.25,
) -> dict[str, object]:
    source: dict[str, object] = {
        "station_id": station_id,
        "account_id": account_id,
        "resource_id": resource_id,
        "provider_name": provider_name,
        "api_base": "https://relay.example.test/v1",
        "enabled": True,
        "name": "Relay Key",
        "models": models or ["upstream-chat"],
    }
    if multiplier is not None:
        source["multiplier"] = multiplier
    return source


class RelayModelBindingTests(unittest.TestCase):
    def test_model_selects_matching_relay_resource_atomically_and_keeps_key_types_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_base": "https://relay.example.test/v1",
                        "api_keys": [
                            {
                                "name": "independent",
                                "value": "sk-independent-fixture",
                            }
                        ],
                        "models": [],
                    }
                },
            )["providers"][0]
            provider_id = provider["id"]
            added = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "public-chat",
                        "upstream_model": "upstream-chat",
                        "api_key_name": "independent",
                    },
                },
            )
            model_id = added["providers"][0]["models"][0]["id"]

            selected = domain.dispatch(
                "model.select_relay_resource",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "source": relay_source(),
                },
            )
            summary = selected["operation_summary"]
            self.assertEqual("model_relay_key_selected", summary["operation"])
            provider = selected["providers"][0]
            self.assertEqual(
                {"independent", "relay"},
                {key["source"]["kind"] for key in provider["key_states"]},
            )
            relay_key = next(
                key for key in provider["key_states"] if key["source"]["kind"] == "relay"
            )
            self.assertFalse(relay_key["configured"])
            model = provider["models"][0]
            self.assertEqual(relay_key["id"], model["provider_key_id"])
            self.assertEqual(relay_key["name"], model["api_key_name"])
            self.assertEqual("relay_linked", model["catalog_mode"])
            self.assertNotIn("sk-independent-fixture", json.dumps(selected))

            repeated = domain.dispatch(
                "model.select_relay_resource",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "source": relay_source(),
                },
            )
            self.assertTrue(repeated["operation_summary"]["reused"])
            self.assertEqual(2, len(repeated["providers"][0]["key_states"]))

    def test_model_relay_resource_selection_rejects_a_different_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_base": "https://independent.example.test/v1",
                        "api_keys": [{"name": "independent", "value": "fixture"}],
                        "models": [],
                    }
                },
            )["providers"][0]
            provider_id = provider["id"]
            model_id = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "public-chat",
                        "upstream_model": "upstream-chat",
                        "api_key_name": "independent",
                    },
                },
            )["providers"][0]["models"][0]["id"]
            before = domain.snapshot()

            with self.assertRaisesRegex(DomainError, "does not match the provider Base URL"):
                domain.dispatch(
                    "model.select_relay_resource",
                    {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "source": relay_source(),
                    },
                )

            self.assertEqual(before, domain.snapshot())

    def test_provider_relay_key_import_is_idempotent_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_base": "https://independent.example.test/v1",
                        "api_keys": [],
                        "models": [],
                    }
                },
            )["providers"][0]
            source = relay_source()
            imported = domain.dispatch(
                "provider.import_relay_key",
                {
                    "provider_id": provider["id"],
                    "source": source,
                    "api_key_name": "Relay Key",
                },
            )
            summary = imported["operation_summary"]
            self.assertTrue(summary["imported"])
            self.assertFalse(summary["reused"])
            self.assertTrue(summary["slot_id"].startswith("provider-slot-"))
            self.assertNotIn("replace-materialized-credential", json.dumps(imported))
            staged_provider = imported["providers"][0]
            self.assertEqual("https://independent.example.test/v1", staged_provider["api_base"])
            self.assertEqual(1, len(staged_provider["key_states"]))
            key = staged_provider["key_states"][0]
            self.assertFalse(key["configured"])
            self.assertEqual("relay", key["source"]["kind"])
            self.assertEqual("station-a", key["source"]["station_id"])
            with self.assertRaisesRegex(DomainError, "managed by its source"):
                domain.dispatch(
                    "provider.key_patch",
                    {
                        "provider_id": provider["id"],
                        "old_name": key["name"],
                        "name": "renamed-relay-key",
                    },
                )
            with self.assertRaisesRegex(DomainError, "managed by its source"):
                domain.stage_secret(
                    "api_key",
                    f'{provider["id"]}\x1f{key["name"]}',
                    "sk-replace-should-not-stage",
                )

            repeated = domain.dispatch(
                "provider.import_relay_key",
                {
                    "provider_id": provider["id"],
                    "source": source,
                    "api_key_name": "Renamed upstream key must not rename the slot",
                },
            )
            repeated_summary = repeated["operation_summary"]
            self.assertFalse(repeated_summary["imported"])
            self.assertTrue(repeated_summary["reused"])
            self.assertEqual(summary["slot_id"], repeated_summary["slot_id"])
            self.assertEqual(1, len(repeated["providers"][0]["key_states"]))

    def test_model_provider_key_selection_derives_relay_state_and_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_keys": [
                            {
                                "name": "independent",
                                "value": "sk-independent-fixture",
                            }
                        ],
                        "models": [],
                    }
                },
            )["providers"][0]
            provider_id = provider["id"]
            added = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "public-chat",
                        "upstream_model": "upstream-chat",
                        "api_key_name": "independent",
                        "order": 7,
                    },
                },
            )
            model_id = added["providers"][0]["models"][0]["id"]
            imported = domain.dispatch(
                "provider.import_relay_key",
                {
                    "provider_id": provider_id,
                    "source": relay_source(),
                    "api_key_name": "Relay Key",
                },
            )
            relay_key_id = imported["operation_summary"]["slot_id"]
            key_states = imported["providers"][0]["key_states"]
            independent_key_id = next(
                item["id"] for item in key_states if item["source"]["kind"] == "independent"
            )

            linked = domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "changes": {"provider_key_id": relay_key_id},
                },
            )["providers"][0]["models"][0]
            self.assertEqual(relay_key_id, linked["provider_key_id"])
            self.assertEqual("relay_linked", linked["catalog_mode"])
            self.assertEqual("upstream-chat", linked["source_model_id"])
            self.assertEqual("linked", linked["binding_health"]["status"])

            domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "changes": {"order_mode": "relay_multiplier"},
                },
            )
            independent = domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "changes": {"provider_key_id": independent_key_id},
                },
            )["providers"][0]["models"][0]
            self.assertEqual("independent", independent["catalog_mode"])
            self.assertEqual("", independent["source_model_id"])
            self.assertEqual("independent", independent["binding_health"]["status"])
            self.assertEqual("manual", independent["order_mode"])
            self.assertEqual(7, independent["effective_order"])
            with self.assertRaises(DomainError):
                domain.dispatch(
                    "model.patch",
                    {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "changes": {"order_mode": "relay_multiplier"},
                    },
                )

    def test_legacy_model_relay_fields_are_read_but_provider_key_source_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "providers:",
                        "  provider-a:",
                        '    api_base: "https://relay.example.test/v1"',
                        "    api_keys:",
                        "      - name: relay-key",
                        '        value: "sk-legacy-fixture"',
                        "    x-young-router-relay-keys:",
                        "      version: 1",
                        "      slots:",
                        "        - id: provider-slot-legacy",
                        "          api_key_name: relay-key",
                        "          source:",
                        "            kind: relay",
                        "            station_id: station-a",
                        "            account_id: account-a",
                        "            resource_id: resource-a",
                        "model_list:",
                        "  - model_name: public-chat",
                        "    litellm_params:",
                        "      model: openai/upstream-chat",
                        '      api_base: "https://relay.example.test/v1"',
                        '      api_key: "sk-legacy-fixture"',
                        "      order: 3",
                        "    model_info:",
                        '      id: "00000001"',
                        "      provider: provider-a",
                        "      api_key_name: relay-key",
                        "      x-young-router-provider-key-id: provider-slot-legacy",
                        "      x-young-router-relay-catalog-mode: independent",
                        "      x-young-router-relay-source-model: stale-upstream",
                        "      x-young-router-order-mode: manual",
                        "      x-young-router-manual-order: 3",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(path)
            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual("relay_linked", model["catalog_mode"])
            self.assertEqual("upstream-chat", model["source_model_id"])
            self.assertEqual("linked", model["binding_health"]["status"])

            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("x-young-router-relay-catalog-mode", saved)
            self.assertNotIn("x-young-router-relay-source-model", saved)

    def test_linked_import_preflight_materializes_multiplier_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            domain = ProvidersModelsDomain(path)
            source = relay_source()
            source["order_mode"] = "relay_multiplier"
            source["manual_order"] = 7

            imported = domain.stage_relay_import([source])
            self.assertEqual("linked", imported["operation_summary"]["import_mode"])
            self.assertFalse(domain.validate()["valid"])
            self.assertTrue(domain.validate_relay_preflight()["valid"])

            before = imported["providers"][0]
            key_state = before["key_states"][0]
            self.assertEqual("relay", key_state["source"]["kind"])
            self.assertTrue(key_state["id"].startswith("provider-slot-"))
            self.assertNotIn("replace-materialized-credential", json.dumps(imported))

            materialized = domain.materialize_relay_bindings(
                {
                    "resources": [
                        {
                            **source,
                            "api_key": "replace-materialized-credential",
                            "multiplier": 1.5,
                        }
                    ]
                }
            )
            self.assertEqual([], materialized["issues"])
            self.assertEqual(1, materialized["materialized"])
            self.assertNotIn("replace-materialized-credential", json.dumps(materialized))
            self.assertTrue(domain.validate()["valid"])
            domain.apply()

            saved = path.read_text(encoding="utf-8")
            self.assertIn("x-young-router-relay-keys", saved)
            self.assertIn("x-young-router-provider-key-id", saved)
            self.assertIn("x-young-router-order-mode: relay_multiplier", saved)
            self.assertNotIn("x-young-router-relay-catalog-mode", saved)
            self.assertNotIn("x-young-router-relay-source-model", saved)

            reloaded_domain = ProvidersModelsDomain(path)
            reloaded = reloaded_domain.snapshot()["providers"][0]
            model = reloaded["models"][0]
            self.assertEqual("relay_linked", model["catalog_mode"])
            self.assertEqual("upstream-chat", model["source_model_id"])
            self.assertEqual("relay_multiplier", model["order_mode"])
            self.assertEqual(7, int(model["manual_order"]))
            self.assertEqual(1.5, model["effective_order"])
            self.assertEqual(1.5, model["order"])
            self.assertEqual("linked", model["binding_health"]["status"])
            self.assertNotIn("replace-materialized-credential", json.dumps(reloaded))

            rematerialized = reloaded_domain.materialize_relay_bindings(
                {
                    "resources": [
                        {
                            **source,
                            "api_key": "replace-rematerialized-credential",
                            "multiplier": 1.75,
                        }
                    ]
                }
            )
            self.assertEqual([], rematerialized["issues"])
            self.assertNotIn("replace-rematerialized-credential", json.dumps(rematerialized))

    def test_a_key_the_station_no_longer_serves_is_refused_instead_of_riding_its_stored_value(self) -> None:
        """A key that is gone upstream is not revived by the value left on its slot.

        The decoupling above says a station nobody could read is no verdict
        about a key this document already holds.  The opposite answer means the
        opposite thing: the station *was* read, and this key is not among the
        keys it serves.  The stored credential is then the residue of a key the
        user cannot revoke, and materializing it keeps a dead route looking
        linked — the pane shows a resolved key, the Apply reports success, and
        every request fails upstream.  That case resolves as a failed binding
        the pane can mark.
        """

        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            source = relay_source(models=["upstream-chat"])
            domain.stage_relay_import([source])
            domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential"}]}
            )
            domain.apply()
            key = next(
                item
                for item in domain.snapshot()["providers"][0]["key_states"]
                if item["source"]["kind"] == "relay"
            )
            self.assertTrue(key["configured"])

            # The station answered about this source and does not list it.
            result = domain.materialize_relay_bindings(
                {
                    "resources": [],
                    "issues": [
                        {
                            "code": "resource_removed",
                            "account_id": source["account_id"],
                            "resource_id": source["resource_id"],
                        }
                    ],
                }
            )

            self.assertEqual(
                {"resource_removed"}, {item["code"] for item in result["issues"]}
            )
            self.assertEqual(0, result["materialized_provider_keys"])
            # The coordinator records what materialization refused, and that is
            # what the pane marks its rows by.
            domain.record_binding_issues(result["issues"])
            rows = domain.snapshot()["binding_issues"]
            self.assertEqual({key["id"]}, {row["provider_key_id"] for row in rows})
            # The route says so too, and the leftover value never crosses the
            # snapshot boundary either way.
            self.assertEqual(
                "resource_missing",
                domain.snapshot()["providers"][0]["models"][0]["binding_health"]["status"],
            )
            self.assertNotIn("replace-materialized-credential", json.dumps(domain.snapshot()))

    def test_an_absence_the_station_never_confirmed_still_resolves_from_the_document(self) -> None:
        """Only the station's own answer refuses a binding, never code alone.

        The same issue code arrives from a read that never happened, and that
        one must not be read as "this key is gone" — it would refuse the write
        for a route whose credential is perfectly current, which is what the
        relay's own Apply does the instant a model is added.  The relay domain
        is what tells the two apart, and this pins the fact it decides on: the
        account's own resource list.
        """

        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            source = relay_source(models=["upstream-chat"])
            domain.stage_relay_import([source])
            domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential"}]}
            )
            domain.apply()

            # A station that could not be reached reports its own codes; none of
            # them says the key is gone, so the document still resolves it.
            for code in ("refresh_failed", "resource_key_unavailable"):
                with self.subTest(code=code):
                    result = domain.materialize_relay_bindings(
                        {
                            "resources": [],
                            "issues": [
                                {
                                    "code": code,
                                    "account_id": source["account_id"],
                                    "resource_id": source["resource_id"],
                                }
                            ],
                        }
                    )
                    self.assertEqual([], result["issues"])
                    self.assertEqual(1, result["materialized_provider_keys"])

    def test_a_dead_slot_nothing_routes_through_does_not_refuse_an_unrelated_edit(self) -> None:
        """A stale key is the user's to delete, not a reason to fail every save.

        The coordinator fails the whole Apply on any issue materialization
        reports, so an issue about a key no route uses would break every edit
        in every pane — a stale row in one provider refusing an unrelated model
        rename in another.  A slot nothing routes through is reported as
        nothing at all, and only a route that actually depends on the missing
        key is refused.
        """

        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            # One slot a route uses, and one stale slot nothing uses.
            used = relay_source(resource_id="resource-used", models=["upstream-chat"])
            # The stale slot serves no model, so the import binds no route to it:
            # that is exactly the slot nothing routes through.
            stale = {**relay_source(resource_id="resource-stale"), "models": []}
            domain.stage_relay_import([used, stale])
            domain.materialize_relay_bindings(
                {
                    "resources": [
                        {**used, "api_key": "replace-used-credential"},
                        {**stale, "api_key": "replace-stale-credential"},
                    ]
                }
            )
            domain.apply()
            provider = domain.snapshot()["providers"][0]
            stale_slot = next(
                item
                for item in provider["key_states"]
                if item["source"]["resource_id"] == "resource-stale"
            )
            # The stale slot is the one nothing routes through.
            self.assertEqual(0, stale_slot["model_count"])
            # ... and the used slot is the one that does.
            self.assertEqual(
                1,
                next(
                    item
                    for item in provider["key_states"]
                    if item["source"]["resource_id"] == "resource-used"
                )["model_count"],
            )

            result = domain.materialize_relay_bindings(
                {
                    "resources": [],
                    "issues": [
                        {
                            "code": "resource_removed",
                            "account_id": stale["account_id"],
                            "resource_id": "resource-stale",
                        }
                    ],
                }
            )

            self.assertEqual([], result["issues"])
            domain.record_binding_issues(result["issues"])
            self.assertEqual([], domain.snapshot()["binding_issues"])
            # The stale slot keeps its value: nothing here destroys a
            # credential, it only declines to route through one.
            written = domain.export(include_sensitive=True)["providers"][0]
            self.assertEqual(
                "replace-stale-credential",
                next(
                    item
                    for item in written["api_keys"]
                    if item["name"] == stale_slot["name"]
                )["value"],
            )

    def test_a_route_that_names_no_key_is_not_adopted_by_the_linked_key(self) -> None:
        """The pane's “no key” choice survives Apply and the relay keeps its own routes.

        An unbound route answers with the provider's default credential, which on
        a relay-managed provider is the first slot: materialization must not take
        that coincidence as ownership, and the file must not record the route as
        bound to a key its user never picked.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            domain = ProvidersModelsDomain(path)
            source = relay_source(models=["upstream-chat", "other-chat"])
            domain.stage_relay_import([source])
            provider = domain.snapshot()["providers"][0]
            relay_key = next(key for key in provider["key_states"] if key["source"]["kind"] == "relay")
            domain.dispatch(
                "model.add",
                {
                    "provider_id": provider["id"],
                    "model": {
                        "name": "unbound-chat",
                        "upstream_model": "other-chat",
                        "enabled": True,
                        "api_key_name": "",
                        "provider_key_id": "",
                    },
                },
            )

            materialized = domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential"}]}
            )

            self.assertEqual([], materialized["issues"])
            models = {
                model["model_name"]: model for model in domain.snapshot()["providers"][0]["models"]
            }
            unbound = models["unbound-chat"]
            self.assertEqual("", unbound["api_key_name"])
            self.assertEqual("", unbound["provider_key_id"])
            self.assertEqual("independent", unbound["catalog_mode"])
            self.assertEqual("", unbound["source_model_id"])
            self.assertNotIn("replace-materialized-credential", json.dumps(domain.snapshot()))

            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertIn("x-young-router-key-binding: unbound", saved)
            # The unbound route's own label carries no key, while the catalog
            # routes the linked import created keep theirs.
            self.assertIn(
                "model=unbound-chat / provider=relay-station-a / "
                "upstream=openai/other-chat / host=relay.example.test / order=0",
                saved,
            )

            reloaded = {
                model["model_name"]: model
                for model in ProvidersModelsDomain(path).snapshot()["providers"][0]["models"]
            }
            self.assertEqual("", reloaded["unbound-chat"]["api_key_name"])
            self.assertEqual("", reloaded["unbound-chat"]["provider_key_id"])
            self.assertEqual(relay_key["id"], next(
                model["provider_key_id"]
                for name, model in reloaded.items()
                if name != "unbound-chat"
            ))

    def test_a_slot_the_provider_no_longer_carries_never_becomes_the_first_key(self) -> None:
        """A dangling slot id is the route's own claim, not a request for key #1.

        Reading a document used to resolve a slot id the provider does not carry
        onto the provider's first key and persist that rewrite, so merely opening
        the app could move a hand-edited route onto another credential.  The row
        stays unresolved instead, and validation names it.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                """
providers:
  compat:
    api_base: "https://example.test/v1"
    api_keys:
      - name: first
        value: "replace-first-secret"
      - name: second
        value: "replace-second-secret"
model_list:
  - model_name: dangling-chat
    litellm_params:
      model: openai/dangling-chat
      api_base: "https://example.test/v1"
      api_key: "replace-second-secret"
      order: 1
    model_info:
      id: "00000001"
      provider: compat
      x-young-router-provider-key-id: provider-slot-00000000000000000000000000000000
      upstream_url_surface: openai/responses
      upstream_protocol_mode: fallback
""",
                encoding="utf-8",
            )

            domain = ProvidersModelsDomain(path)
            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual(
                "provider-slot-00000000000000000000000000000000", model["provider_key_id"]
            )
            self.assertEqual("", model["api_key_name"])
            self.assertEqual("independent", model["catalog_mode"])
            self.assertEqual("", model["source_model_id"])
            # The row is reported, not repaired: applying would otherwise write a
            # route whose credential nothing can resolve.
            self.assertFalse(domain.validate()["valid"])
            with self.assertRaises(DomainError):
                domain.apply()

            # Choosing a key again is the way out, and it stays that key.
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": domain.snapshot()["providers"][0]["id"],
                    "model_id": model["id"],
                    "changes": {"api_key_name": "second"},
                },
            )
            rebound = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual("second", rebound["api_key_name"])
            self.assertTrue(domain.validate()["valid"])
            domain.apply()
            saved = ProvidersModelsDomain(path).snapshot()["providers"][0]["models"][0]
            self.assertEqual("second", saved["api_key_name"])
            self.assertEqual("independent", saved["catalog_mode"])

    def test_a_route_is_probed_with_the_slot_it_names(self) -> None:
        """A probe measures the credential of the key the route answers with.

        Resolving the probe by the key *name* meant a route bound by slot id (or
        one whose name had gone stale) was measured against another key's
        credential, and the stored result then described a route it never tested.
        """

        provider = {
            "name": "compat",
            "api_base": "https://example.test/v1",
            "api_key": "replace-alice",
            "api_keys": [
                {"id": "slot-a", "name": "alice", "value": "replace-alice", "source": {"kind": "independent"}},
                {"id": "slot-b", "name": "bob", "value": "replace-bob", "source": {"kind": "independent"}},
            ],
        }
        self.assertEqual(
            ("bob", "replace-bob"),
            ProvidersModelsDomain._model_credential(
                provider, {"api_key_name": "alice", "provider_key_id": "slot-b"}
            ),
        )
        self.assertEqual(
            ("alice", "replace-alice"),
            ProvidersModelsDomain._model_credential(provider, {"api_key_name": "alice"}),
        )
        self.assertEqual(
            ("bob", "replace-bob"),
            ProvidersModelsDomain._model_credential(
                provider, {"api_key_name": "", "provider_key_id": "slot-b"}
            ),
        )
        # A route that states a slot the provider does not carry has nothing to
        # probe: it is not measured against the provider's default key.
        with self.assertRaises(DomainError):
            ProvidersModelsDomain._model_credential(
                provider,
                {"provider_key_id": "provider-slot-00000000000000000000000000000000"},
            )

    def test_a_linked_import_joins_the_provider_the_station_is_bound_to(self) -> None:
        """One station is one provider, whatever name a descriptor carries.

        The wizard names a station's provider after the station, while the relay
        coordinator's descriptors carry the neutral `relay-<station_id>`
        placeholder.  Importing with that placeholder must find the binding
        instead of creating a second provider that splits the station's keys and
        routes across two rows.
        """

        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            station_id = "station-a"
            domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "My Relay",
                        "enabled": True,
                        "api_base": "https://relay.example.test/v1",
                        "models": [],
                    }
                },
            )
            domain.dispatch(
                "provider.select_relay_station",
                {
                    "provider_id": "My Relay",
                    "source": {
                        "station_id": station_id,
                        "name": "My Relay",
                        "api_base": "https://relay.example.test/v1",
                    },
                },
            )

            domain.stage_relay_import(
                [
                    {
                        **relay_source(station_id=station_id),
                        "provider_name": f"relay-{station_id}",
                    }
                ]
            )

            providers = domain.snapshot()["providers"]
            self.assertEqual(["My Relay"], [provider["name"] for provider in providers])
            provider = providers[0]
            self.assertEqual(
                station_id, str(provider.get("relay_station_id", "")).strip()
            )
            self.assertEqual(["Relay Key"], [key["name"] for key in provider["key_states"]])

    def test_a_key_the_relay_could_not_resolve_is_projected_onto_its_routes(self) -> None:
        """A refused binding names the row it belongs to, not only the outcome.

        The pane marks rows by ids: Core projects the materialization's
        secret-free issues onto the provider key and the routes that point at
        it, so a key the relay could not refresh (here the whole account could
        not be read) marks its own rows instead of leaving the user with a
        sentence about an unknown one.
        """

        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            source = relay_source()
            domain.stage_relay_import([source])
            provider = domain.snapshot()["providers"][0]
            key = next(item for item in provider["key_states"] if item["source"]["kind"] == "relay")
            linked = provider["models"][0]

            result = domain.materialize_relay_bindings(
                {
                    "resources": [],
                    "issues": [
                        {"code": "refresh_failed", "account_id": source["account_id"], "resource_id": source["resource_id"]},
                    ],
                }
            )
            domain.record_binding_issues(result["issues"])

            rows = domain.snapshot()["binding_issues"]
            self.assertEqual({"refresh_failed", "resource_missing"}, {row["code"] for row in rows})
            self.assertEqual({key["id"]}, {row["provider_key_id"] for row in rows})
            self.assertEqual({linked["editor_id"]}, {row["model_id"] for row in rows})
            self.assertEqual({provider["editor_id"]}, {row["provider"] for row in rows})
            # Nothing user-authored or secret travels with them.
            self.assertNotIn("replace-materialized-credential", json.dumps(rows))

            # A station the whole account read failed on names no resource: the
            # keys of that account are what the rows resolve to.
            domain.record_binding_issues([{"code": "refresh_failed", "account_id": source["account_id"]}])
            account_rows = domain.snapshot()["binding_issues"]
            self.assertEqual({key["id"]}, {row["provider_key_id"] for row in account_rows})
            self.assertEqual({linked["editor_id"]}, {row["model_id"] for row in account_rows})
            # Another account's failure marks nothing here.
            domain.record_binding_issues([{"code": "refresh_failed", "account_id": "account-other"}])
            self.assertEqual([], domain.snapshot()["binding_issues"])

    def test_a_discarded_draft_takes_its_binding_marks_with_it(self) -> None:
        """A brown row must always have a reason on screen.

        The relay-binding marks are how the pane shows which key or route
        refused a write, and they paint their rows in the warning ink.  They
        describe a staged draft, so discarding that draft has to take them: a
        mark left behind painted a row brown for a refusal the user had already
        abandoned, with no message anywhere left to say why — a colour with no
        explanation, which is exactly what an unexplained warning reads as.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = ProvidersModelsDomain(root / "config.yaml")
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_base": "https://relay.example.test/v1",
                        "models": [],
                    }
                },
            )["providers"][0]
            provider_id = provider["id"]
            domain.dispatch(
                "model.select_relay_resource",
                {
                    "provider_id": provider_id,
                    "model_id": domain.dispatch(
                        "model.add",
                        {
                            "provider_id": provider_id,
                            "model": {"name": "public-chat", "upstream_model": "upstream-chat"},
                        },
                    )["providers"][0]["models"][0]["id"],
                    "source": relay_source(),
                },
            )
            state = domain.snapshot()["providers"][0]
            key = next(item for item in state["key_states"] if item["source"]["kind"] == "relay")
            issue = {
                "code": "resource_key_unavailable",
                "provider_key_id": key["id"],
                "provider_id": state["editor_id"],
            }

            for action in ("cancel", "reset", "reload"):
                domain.record_binding_issues([issue])
                self.assertEqual(
                    ["resource_key_unavailable"],
                    [row["code"] for row in domain.snapshot()["binding_issues"]],
                    action,
                )
                domain.dispatch(action, {})
                self.assertEqual(
                    [],
                    domain.snapshot()["binding_issues"],
                    f"{action} left a marked row behind",
                )

    def test_a_key_with_its_own_persisted_value_materializes_without_the_station(self) -> None:
        """Model management stays decoupled from relay sign-in.

        The relay is consulted to learn a key, a catalog, or a multiplier it has
        not reported yet — never as a precondition.  A linked slot that already
        carries the value this document materialized keeps working while the
        station cannot be read at all (no dashboard session, an expired one, or
        a station that is down), because the only thing a binding needs from the
        relay is the key value, and that value is already here.  Reporting the
        station's refusal beside such a route would mark a row that is fine.
        """

        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            source = relay_source(models=["upstream-chat"])
            domain.stage_relay_import([source])
            # The first resolution is what persists the credential.
            domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential"}]}
            )
            domain.apply()
            provider = domain.snapshot()["providers"][0]
            key = next(item for item in provider["key_states"] if item["source"]["kind"] == "relay")
            self.assertTrue(key["configured"])

            # Now the station is unreadable: no resource resolved for this
            # source, and the refresh says so at the account level too.
            result = domain.materialize_relay_bindings(
                {
                    "resources": [],
                    "issues": [
                        {"code": "refresh_failed", "account_id": source["account_id"]},
                        {"code": "resource_key_unavailable", "account_id": source["account_id"], "resource_id": source["resource_id"]},
                    ],
                }
            )

            self.assertEqual([], result["issues"])
            self.assertEqual(1, result["materialized_provider_keys"])
            self.assertEqual(1, result["materialized"])
            written = domain.export(include_sensitive=True)["providers"][0]
            self.assertEqual(
                "replace-materialized-credential",
                next(item for item in written["api_keys"] if item["name"] == key["name"])["value"],
            )
            # Nothing leaked into the pane's own issue projection either.
            domain.record_binding_issues(result["issues"])
            self.assertEqual([], domain.snapshot()["binding_issues"])
            self.assertNotIn("replace-materialized-credential", json.dumps(domain.snapshot()))

    def test_a_station_that_cannot_be_read_still_marks_a_key_with_no_value(self) -> None:
        """A slot with no materialized value is a genuinely unresolved binding.

        The decoupling stops at the value the document does not have: with
        nothing to fall back on, the station's refusal is the row's real cause
        and stays reported.
        """

        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            source = relay_source(models=["upstream-chat"])
            domain.stage_relay_import([source])
            provider = domain.snapshot()["providers"][0]
            key = next(item for item in provider["key_states"] if item["source"]["kind"] == "relay")
            self.assertFalse(key["configured"])

            result = domain.materialize_relay_bindings(
                {
                    "resources": [],
                    "issues": [{"code": "refresh_failed", "account_id": source["account_id"]}],
                }
            )

            self.assertEqual(
                {"resource_missing", "refresh_failed"},
                {item["code"] for item in result["issues"]},
            )
            domain.record_binding_issues(result["issues"])
            rows = domain.snapshot()["binding_issues"]
            self.assertEqual({key["id"]}, {row["provider_key_id"] for row in rows})
            self.assertFalse(
                next(
                    item
                    for item in domain.snapshot()["providers"][0]["key_states"]
                    if item["id"] == key["id"]
                )["configured"]
            )

    def test_materialization_reports_catalog_and_multiplier_problems_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            source = relay_source(models=["upstream-chat"])
            source["order_mode"] = "relay_multiplier"
            domain.stage_relay_import([source])

            result = domain.materialize_relay_bindings(
                {
                    "resources": [
                        {
                            **source,
                            "api_key": "replace-materialized-credential",
                            "models": ["different-model"],
                        }
                    ]
                }
            )

            self.assertEqual({"catalog_model_missing"}, {item["code"] for item in result["issues"]})
            self.assertNotIn("replace-materialized-credential", json.dumps(result))

            domain = ProvidersModelsDomain(Path(directory) / "second.yaml")
            domain.stage_relay_import([source])
            result = domain.materialize_relay_bindings(
                {
                    "resources": [
                        {
                            **source,
                            "api_key": "replace-materialized-credential",
                            "multiplier": None,
                        }
                    ]
                }
            )
            self.assertEqual({"multiplier_missing"}, {item["code"] for item in result["issues"]})
            self.assertNotIn("replace-materialized-credential", json.dumps(result))

    def test_a_provider_holds_one_base_url_and_its_routes_inherit_it(self) -> None:
        """A route never carries a second copy of its provider's address.

        The pane offers no per-route Base URL, so a route repeating its
        provider's base holds no information the document did not already
        have.  The copy was the defect: a station binding stamped the base it
        read *before* the same Apply's materialization rewrote the provider's
        base, so the literal outlived the address it was copied from and the
        dumper then kept it as a per-route override — sending the route to the
        host the user had already left.  The provider holds the base; its
        routes inherit it.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            domain = ProvidersModelsDomain(path)
            source = relay_source()
            source["order_mode"] = "relay_multiplier"
            domain.stage_relay_import([source])
            domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential"}], "issues": []}
            )

            old_base = "https://relay.example.test/v1"
            new_base = "https://relay-new.example.test/v1"
            provider = domain._draft["providers"][0]
            self.assertEqual(old_base, provider["api_base"])

            # Binding the provider to its own station must not stamp the
            # provider's base onto its routes.
            domain.dispatch(
                "provider.select_relay_station",
                {
                    "provider_id": provider["name"],
                    "source": {"station_id": "station-a", "name": provider["name"], "api_base": old_base},
                },
            )
            self.assertEqual([""], [model["api_base"] for model in domain._draft["providers"][0]["models"]])

            # The station moves: the provider follows it and the routes keep
            # inheriting, so the old address cannot survive anywhere.
            result = domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential", "api_base": new_base}], "issues": []}
            )
            self.assertEqual([], result["issues"])

            staged = domain._draft["providers"][0]
            self.assertEqual(new_base, staged["api_base"])
            self.assertEqual([""], [model["api_base"] for model in staged["models"]])

            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("relay.example.test", saved)
            self.assertIn("relay-new.example.test", saved)

    def test_a_real_route_override_survives_a_reload_and_a_provider_move(self) -> None:
        """A route pointed elsewhere keeps its own address through both.

        Only a route whose base differs from its provider's is an override;
        the loader must not mistake the provider's own address for one, and it
        must not discard a genuine one.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "p",
                        "enabled": True,
                        "api_base": "https://provider.example.test/v1",
                        "api_keys": [{"name": "k", "value": "sk-1"}],
                    }
                },
            )
            domain.dispatch(
                "model.add",
                {
                    "provider_id": "p",
                    "model": {
                        "name": "override",
                        "upstream_model": "openai/override",
                        "api_key_name": "k",
                        "api_base": "https://override.example.test/v1",
                    },
                },
            )
            domain.apply()

            reloaded = ProvidersModelsDomain(path)
            self.assertEqual(
                "https://override.example.test/v1",
                reloaded._draft["providers"][0]["models"][0]["api_base"],
            )
            # Moving the provider does not rewrite a route that never pointed
            # at the address the provider used to have.
            reloaded.materialize_relay_bindings(
                {"resources": [], "issues": []}
            )
            self.assertEqual(
                "https://override.example.test/v1",
                reloaded._draft["providers"][0]["models"][0]["api_base"],
            )

    def test_dependency_policy_detaches_or_disables_without_reusing_relay_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            source = relay_source()
            domain.stage_relay_import([source])
            domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential"}]}
            )
            dependency = domain.dependency_summary(
                {
                    "station_id": "station-a",
                    "account_id": "account-a",
                    "resource_id": "resource-a",
                }
            )
            self.assertEqual(1, dependency["provider_key_count"])
            self.assertEqual(1, dependency["model_count"])

            detached = domain.apply_relay_dependency_policy(
                [
                    {
                        "station_id": "station-a",
                        "account_id": "account-a",
                        "resource_id": "resource-a",
                    }
                ],
                "detach",
            )
            self.assertEqual([], detached["issues"])
            self.assertEqual(1, detached["detached_models"])
            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual("independent", model["catalog_mode"])
            self.assertEqual("independent", model["binding_health"]["status"])
            self.assertEqual(0, domain.dependency_summary()["model_count"])

            # A fresh relay binding demonstrates the remote-delete policy:
            # it removes the soon-to-be-revoked credential rather than cloning
            # it into an apparently usable independent deployment.
            domain = ProvidersModelsDomain(Path(directory) / "disabled.yaml")
            domain.stage_relay_import([source])
            domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential"}]}
            )
            disabled = domain.apply_relay_dependency_policy([source], "detach_disabled")
            self.assertEqual([], disabled["issues"])
            self.assertEqual(1, disabled["disabled_detached_models"])
            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertFalse(model["enabled"])
            self.assertEqual("", model["provider_key_id"])
            self.assertEqual("credential_required", model["binding_health"]["status"])
            self.assertNotIn("replace-materialized-credential", json.dumps(domain.snapshot()))


    def test_one_followed_route_locks_the_whole_groups_hand_ordering(self) -> None:
        """A group holding a multiplier route cannot be permuted at all.

        The followed route's order is the station's multiplier, not a typed
        number, so permuting the group's values would either drop that route
        from the order or invent a number for it.  The pane reads the same
        snapshot the routes table does, so it reads the lock before it offers
        ↑/↓ on the group's plain route.
        """

        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            provider_id = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_base": "https://relay.example.test/v1",
                        "api_keys": [{"name": "independent", "value": "sk-independent-fixture"}],
                        "models": [],
                    }
                },
            )["providers"][0]["id"]
            followed_id = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {"name": "public-chat", "upstream_model": "upstream-chat", "api_key_name": "independent"},
                },
            )["providers"][0]["models"][0]["id"]
            domain.dispatch(
                "model.select_relay_resource",
                {"provider_id": provider_id, "model_id": followed_id, "source": relay_source()},
            )
            plain_id = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {"name": "public-chat", "upstream_model": "upstream-chat-2", "api_key_name": "independent"},
                },
            )["providers"][0]["models"][1]["id"]
            domain.dispatch(
                "model.patch",
                {"provider_id": provider_id, "model_id": followed_id, "changes": {"order_mode": "relay_multiplier"}},
            )

            modes = {
                str(model["id"]): str(model["order_mode"])
                for model in domain.snapshot()["providers"][0]["models"]
            }
            self.assertEqual("relay_multiplier", modes[followed_id])
            self.assertEqual("manual", modes[plain_id])

            for renumber in (False, True):
                with self.assertRaises(DomainError):
                    domain.dispatch(
                        "routes.reorder_group",
                        {
                            "public_model": "public-chat",
                            "route_ids": [plain_id, followed_id],
                            **({"renumber": True} if renumber else {}),
                        },
                    )


if __name__ == "__main__":
    unittest.main()

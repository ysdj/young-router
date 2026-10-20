"""The Our Free Model lane, backed by the bundled upstream package.

The upstream protocol is staged unchanged at build time, so these tests drive
the seam instead of the network: a stub runtime stands in for the worker.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from young_router.adapters import our_free_model
from young_router.core.domains import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.provider_auth import ProviderAuthManager
from young_router.core.service import CoreStore


CATALOG = [
    {
        "id": "big-pickle",
        "name": "big-pickle",
        "contextWindow": 200_000,
        "maxTokens": 32_000,
        "supportsImages": False,
        "reasoning": {"efforts": ["off", "minimal", "low", "medium", "high"]},
    },
    {
        "id": "mimo-v2.6-flash-free",
        "name": "mimo-v2.6-flash-free",
        "contextWindow": 1_048_576,
        "maxTokens": 32_000,
        "supportsImages": True,
    },
]


class StubRuntime:
    """The worker surface Core depends on, without a Node process."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, str]] = []
        self.stopped = 0

    def status(self, *, refresh: bool = False) -> dict[str, object]:
        self.calls.append(("status", "refresh" if refresh else "read"))
        if not self.available:
            return {"available": False, "detail": "integration_unstaged", "providers": {}}
        return {
            "available": True,
            "detail": "",
            "providers": {our_free_model.OUR_FREE_MODEL_PROVIDER: self.provider_status()},
        }

    def provider_status(self, provider: str = our_free_model.OUR_FREE_MODEL_PROVIDER) -> dict[str, object]:
        self.calls.append(("provider_status", provider))
        if not self.available:
            return {}
        return {
            "provider": provider,
            "state": "ready",
            "detail": "",
            "modelCount": len(CATALOG),
        }

    def models(
        self, provider: str = our_free_model.OUR_FREE_MODEL_PROVIDER, *, refresh: bool = False
    ) -> dict[str, object]:
        self.calls.append(("models", provider))
        if not self.available:
            return {"available": False, "detail": "integration_unstaged", "models": []}
        return {
            "available": True,
            "detail": "",
            "source": "adapter",
            "models": [dict(entry) for entry in CATALOG],
            # The surface each row answers on, as the upstream routes it.  Both
            # rows here are served over Chat Completions; the Responses rows are
            # the `muse-spark` pair, which this stub does not carry.
            "wires": {"big-pickle": "chat", "mimo-v2.6-flash-free": "chat"},
            "windows": {"big-pickle": 200_000, "mimo-v2.6-flash-free": 1_048_576},
            "status": self.provider_status(provider),
        }

    def environment(self, *, autostart: bool = False) -> dict[str, str]:
        return {
            our_free_model.API_BASE_ENV[our_free_model.OUR_FREE_MODEL_PROVIDER]: "http://127.0.0.1:1/v1",
            our_free_model.API_KEY_ENV[our_free_model.OUR_FREE_MODEL_PROVIDER]: "local-token",
        }

    def environment_values(self) -> dict[str, str]:
        return self.environment()

    def autostart_if_configured(self) -> None:
        self.calls.append(("autostart", "launch"))

    def stop(self) -> None:
        self.stopped += 1


class OurFreeModelProviderTests(unittest.TestCase):
    def _domain(self, runtime: StubRuntime | None = None):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        domain = ProvidersModelsDomain(
            config,
            auth_manager=ProviderAuthManager(root),
            our_free_model=runtime if runtime is not None else StubRuntime(),
        )
        return directory, domain, config

    def test_service_provider_add_imports_the_live_catalog(self) -> None:
        """Adding the lane imports exactly the free roster the adapter serves."""

        directory, domain, config = self._domain()
        with directory:
            summary = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["operation_summary"]
            self.assertEqual(summary["auth_kind"], "our_free_model_login")
            self.assertEqual(summary["auth_status"], "signed_in")

            provider = domain.snapshot()["providers"][0]
            self.assertEqual(provider["name"], our_free_model.OUR_FREE_MODEL_DISPLAY_NAME)
            self.assertEqual(provider["auth_status"], "signed_in")
            self.assertEqual(provider["key_states"][0]["name"], our_free_model.API_KEY_NAME)
            self.assertEqual(
                provider["api_base"], our_free_model.api_base_reference(our_free_model.OUR_FREE_MODEL_PROVIDER)
            )
            # An imported roster is a set of peers, not a ranked list: every
            # route starts on the same order instead of the catalog's own 1, 2.
            self.assertEqual([model["order"] for model in provider["models"]], [0, 0])
            # The public name is the upstream id unchanged: renaming is the
            # user's, and the route addresses the same id either way.
            self.assertEqual(
                [(model["model_name"], model["upstream_model"]) for model in provider["models"]],
                [("big-pickle", "big-pickle"), ("mimo-v2.6-flash-free", "mimo-v2.6-flash-free")],
            )
            first = provider["models"][0]
            self.assertEqual(first["litellm_model"], "openai/big-pickle")
            # The row answers on Chat Completions, and the route says so.  It
            # used to be pinned to Responses because the gateway serves both
            # surfaces -- which is true of the gateway and false of this row, so
            # the pin asked an endpoint the row does not have.
            self.assertEqual(first["upstream_url_surface"], "openai/chat")
            self.assertEqual(first["upstream_protocol_mode"], "fixed")
            # The window the upstream states is the route's declared window, so
            # the public window is the upstream's number rather than the
            # unknown-model fallback the registry would have answered with.
            self.assertEqual(first["max_input_tokens"], 200_000)
            self.assertEqual(first["api_key_name"], our_free_model.API_KEY_NAME)
            self.assertTrue(provider["key_states"][0]["configured"])

            self.assertTrue(domain.validate()["valid"])
            domain.apply()
            document = config.read_text(encoding="utf-8")
            # Only environment references reach the file: the worker port and
            # its bearer stay inside the Core process.
            self.assertIn("os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_BASE", document)
            self.assertIn("os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_KEY", document)
            self.assertNotIn("127.0.0.1", document)
            self.assertNotIn("local-token", document)
            self.assertIn("model: openai/big-pickle", document)

    def test_an_unreachable_lane_refuses_the_add_instead_of_creating_an_empty_account(self) -> None:
        """The lane's own refusal is reported; no provider is created for nothing."""

        directory, domain, _config = self._domain(StubRuntime(available=False))
        with directory:
            with self.assertRaisesRegex(DomainError, "could not be reached"):
                domain.dispatch("service_provider.add", {"kind": "our_free_model_login"})

    def test_the_status_and_catalog_reads_answer_through_the_operation_summary(self) -> None:
        directory, domain, _config = self._domain()
        with directory:
            status = domain.dispatch("our_free_model_status", {})["operation_summary"]
            self.assertEqual(status["operation"], "our_free_model_status")
            self.assertTrue(status["available"])
            self.assertEqual(
                status["providers"][our_free_model.OUR_FREE_MODEL_PROVIDER]["state"], "ready"
            )

            catalog = domain.dispatch("our_free_model_models", {})["operation_summary"]
            self.assertEqual(catalog["operation"], "our_free_model_models")
            self.assertTrue(catalog["available"])
            self.assertEqual(
                [entry["id"] for entry in catalog["models"]],
                ["big-pickle", "mimo-v2.6-flash-free"],
            )

    def test_no_credential_of_the_user_ever_reaches_the_lane(self) -> None:
        """The lane authenticates itself, so the provider carries no user secret."""

        directory, domain, config = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "our_free_model_login"})
            provider = domain.snapshot()["providers"][0]
            self.assertEqual(len(provider["key_states"]), 1)
            domain.apply()
            document = config.read_text(encoding="utf-8")
            self.assertNotIn("sk-", document)

    def test_the_lane_s_key_slot_is_stated_as_core_owned(self) -> None:
        """Core says which slot is its own, so no surface has to infer it.

        The minted slot is stored as ``source: {kind: independent}`` because it
        belongs to no relay, which made it indistinguishable from a user's key
        in the snapshot: the provider editor rendered an editable, copyable
        field over an ``os.environ/`` reference and every edit was refused.
        Ownership is projected from the value's provenance, so a surface reads
        one boolean instead of re-deriving it from the provider's auth kind —
        and a service that later mints a slot is covered without being named.
        """

        directory, domain, _config = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "our_free_model_login"})
            slot = domain.snapshot()["providers"][0]["key_states"][0]
            self.assertEqual(slot["name"], our_free_model.API_KEY_NAME)
            self.assertIs(slot["managed"], True)
            self.assertEqual(slot["source"], {"kind": "independent"})

    def test_a_user_s_key_is_never_reported_as_core_owned(self) -> None:
        """The provenance rule reads the value, not the stored source kind."""

        directory, domain, config = self._domain()
        with directory:
            config.write_text(
                "providers:\n"
                "  mine:\n"
                "    api_base: https://api.example.com/v1\n"
                "    api_keys:\n"
                "      - name: personal\n"
                "        value: sk-real-user-key\n"
                "model_list:\n"
                "  - model_name: unused\n"
                "    litellm_params:\n"
                "      model: openai/unused\n"
                "      api_base: os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_BASE\n"
                "    model_info:\n"
                "      upstream_url_surface: openai/responses\n",
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(
                config,
                auth_manager=ProviderAuthManager(Path(directory.name)),
                our_free_model=StubRuntime(),
            )
            domain.dispatch("service_provider.add", {"kind": "our_free_model_login"})
            providers = {item["name"]: item for item in domain.snapshot()["providers"]}
            self.assertIs(providers["mine"]["key_states"][0]["managed"], False)
            self.assertIs(providers[our_free_model.OUR_FREE_MODEL_DISPLAY_NAME]["key_states"][0]["managed"], True)
            # A user's own slot stays writable, including on a deployment that
            # also runs the lane: the refusal is about the slot, not the file.
            domain.stage_secret("api_key", "mine\x1fpersonal", "sk-rotated")
            self.assertEqual(
                domain.trusted_secret_value("api_key", "mine\x1fpersonal"),
                "sk-rotated",
            )

    def test_an_owned_slot_refuses_a_staged_value_by_what_it_holds(self) -> None:
        """An owned slot accepts no user value, and its reference survives.

        The auth-kind check refuses this lane first, so the provenance rule is
        asserted on its own below: it is what answers for a service that mints
        a slot without being a login kind, which is the case no predicate over
        today's service names would cover.
        """

        directory, domain, _config = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "our_free_model_login"})
            provider = domain.snapshot()["providers"][0]
            target = f"{provider['name']}\x1f{our_free_model.API_KEY_NAME}"
            with self.assertRaises(DomainError):
                domain.stage_secret("api_key", target, "sk-attacker")
            self.assertEqual(
                domain.trusted_secret_value("api_key", target),
                our_free_model.api_key_reference(our_free_model.OUR_FREE_MODEL_PROVIDER),
            )

    def test_the_owned_slot_rule_is_provenance_not_the_auth_kind(self) -> None:
        """Every reference a bundled worker publishes is recognized as Core's.

        The auth kind says what an account is; this rule says what a *value*
        is.  Asking the second question is what keeps the answer true when a
        worker that owns its transport is added: its slot is refused and
        reported as Core's without anyone extending a list of service names.
        """

        from young_router.adapters import managed_key_references

        references = managed_key_references()
        self.assertIn(
            our_free_model.api_key_reference(our_free_model.OUR_FREE_MODEL_PROVIDER),
            references,
        )
        for reference in references:
            self.assertTrue(ProvidersModelsDomain._key_slot_is_managed(reference))
        # A user's literal key, an unrelated reference, and a non-string are
        # all someone else's; an empty registry recognizes nothing.
        for value in ("sk-user", "os.environ/SOMEONE_ELSE_KEY", "", None, 7):
            self.assertFalse(ProvidersModelsDomain._key_slot_is_managed(value))

    def test_a_lane_entry_is_deleted_as_a_service_provider(self) -> None:
        """The lane entry is removable, and only through its own action.

        An account-backed provider is refused by the ordinary ``provider.delete``
        ("Login providers are managed in Service Provider Management"), so the
        entry is removed through ``service_provider.delete``.  A type the UI
        forgot to route there is an entry the user cannot delete at all.
        """

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["providers"][0]
            provider_id = provider["id"]
            self.assertEqual(len(domain.snapshot()["providers"]), 1)

            # The ordinary provider action refuses a login-backed entry.
            with self.assertRaisesRegex(DomainError, "managed in Service Provider Management"):
                domain.dispatch("provider.delete", {"provider_id": provider_id})
            self.assertEqual(len(domain.snapshot()["providers"]), 1)

            # Its own action removes it.
            summary = domain.dispatch(
                "service_provider.delete", {"provider_id": provider_id}
            )["operation_summary"]
            self.assertEqual(summary["operation"], "service_provider_delete")
            self.assertEqual(summary["auth_kind"], "our_free_model_login")
            self.assertEqual(domain.snapshot()["providers"], [])

    def test_the_store_summary_carries_the_account_kind_it_routes_on(self) -> None:
        """A store summary states each provider's account kind, not just its key.

        ``CoreStore.snapshot()['providers_models']['providers']`` is the list
        the pane falls back to when the domain detail is absent, and the pane
        routes each row to ``service_provider.delete`` or ``provider.delete``
        on ``auth_kind``.  A summary that dropped it made every login-backed
        row read as an API-key provider: the delete went to the ordinary
        action, Core refused it ("Login providers are managed in Service
        Provider Management"), and the lane entry could not be removed at all.
        """

        directory, domain, _config = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "our_free_model_login"})
            store = CoreStore(domains=[domain])
            summary = store.snapshot()["providers_models"]["providers"]
            self.assertEqual(1, len(summary))
            row = summary[0]
            self.assertEqual("our_free_model_login", row["auth_kind"])
            self.assertTrue(row["auth_status"])
            # The detail record keeps saying the same thing, so a pane reading
            # either projection reaches the same action for this row.
            detail = store.snapshot()["domains"]["providers_models"]["providers"][0]
            self.assertEqual(detail["auth_kind"], row["auth_kind"])

    def test_a_route_reports_the_window_the_lane_publishes_for_it(self) -> None:
        """The free lane's own catalog is the authority for its routes' windows.

        Zen states a window per free model and the anonymous lane serves ids
        whose names do not resolve to the public model's own profile, so the
        number the route will actually accept is only known from this catalog.
        The import already read it; the route reports it without a second read,
        and read-only — no patch path may write it.
        """

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["providers"][0]
            windows = {
                model["upstream_model"]: model["upstream_context_window"]
                for model in provider["models"]
            }
            # The catalog's own per-model numbers, keyed by the upstream id the
            # route addresses rather than by the public name it serves.
            self.assertEqual(
                {"big-pickle": 200_000, "mimo-v2.6-flash-free": 1_048_576},
                windows,
            )

            route = provider["models"][0]
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider["id"],
                    "model_id": route["editor_id"],
                    "changes": {"upstream_context_window": 4_096},
                },
            )
            self.assertEqual(
                200_000,
                domain.snapshot()["providers"][0]["models"][0]["upstream_context_window"],
            )

    def test_a_reloaded_core_learns_the_windows_from_the_pane_s_catalog_read(self) -> None:
        """A remembered window belongs to the read that recorded it, not the disk.

        A replacement Core starts with nothing remembered, and the numbers are
        the lane's own — nothing in the configuration states them.  The catalog
        read the pane already performs is what teaches it, so the row comes
        back on the next entry instead of staying empty for the process's life.
        """

        directory, domain, config = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "our_free_model_login"})
            domain.apply()

            reloaded = ProvidersModelsDomain(
                config,
                auth_manager=ProviderAuthManager(config.parent),
                our_free_model=StubRuntime(),
            )
            before = reloaded.snapshot()["providers"][0]["models"]
            self.assertEqual(
                [None, None],
                [model["upstream_context_window"] for model in before],
            )

            reloaded.dispatch("our_free_model_models", {"provider": "our-free-model"})
            after = reloaded.snapshot()["providers"][0]["models"]
            self.assertEqual(
                [200_000, 1_048_576],
                [model["upstream_context_window"] for model in after],
            )

    def test_a_lane_that_states_no_window_reports_none(self) -> None:
        """An upstream that published nothing leaves the row off, not at zero."""

        runtime = StubRuntime()
        runtime.models = lambda provider=our_free_model.OUR_FREE_MODEL_PROVIDER, refresh=False: {
            "available": True,
            "detail": "",
            "source": "adapter",
            "models": [{"id": "big-pickle", "name": "big-pickle"}],
            "status": runtime.provider_status(provider),
        }
        directory, domain, _config = self._domain(runtime)
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["providers"][0]
            self.assertIsNone(provider["models"][0]["upstream_context_window"])

    def test_a_snake_case_catalog_window_reaches_its_route(self) -> None:
        """The lane states ``context_window``, and that is the spelling read.

        WorkBuddy's catalog states camelCase (``contextWindow``) and this lane
        states the snake_case keys a public OpenAI record uses.  Reading only
        the camelCase ones recorded nothing at all for every Our Free Model
        route, so the model pane fell back to the registry's default for the
        public name and showed a window the upstream never stated.
        """

        runtime = StubRuntime()
        runtime.models = lambda provider=our_free_model.OUR_FREE_MODEL_PROVIDER, refresh=False: {
            "available": True,
            "detail": "",
            "source": "adapter",
            "models": [
                {"id": "deepseek-v4.1-flash-free", "context_window": 1_048_576},
                {"id": "jev-1.13-free", "context_window": 32_768},
            ],
            "status": runtime.provider_status(provider),
        }
        directory, domain, _config = self._domain(runtime)
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["providers"][0]
            windows = {
                model["upstream_model"]: model["upstream_context_window"]
                for model in provider["models"]
            }
            self.assertEqual(
                {"deepseek-v4.1-flash-free": 1_048_576, "jev-1.13-free": 32_768},
                windows,
            )
            # A catalog that states no picker groups gives no group label; the
            # pane must not invent one from an id this side decided.
            self.assertEqual(
                {"deepseek-v4.1-flash-free": None, "jev-1.13-free": None},
                {
                    model["upstream_model"]: model["lane_source"]
                    for model in provider["models"]
                },
            )

    def test_each_route_states_the_group_the_upstream_put_it_in(self) -> None:
        """The pane reports the upstream's picker groups, not one of its own.

        The lane publishes two groups beside its roster — what this egress can
        use, and what its regional gate refuses but keeps visible.  Which model
        is in which is the upstream's answer, so a route reports the group named
        in that same read; deriving it from the id shape here would file a model
        under a heading the upstream never used.
        """

        runtime = StubRuntime()
        runtime.models = lambda provider=our_free_model.OUR_FREE_MODEL_PROVIDER, refresh=False: {
            "available": True,
            "detail": "",
            "source": "adapter",
            "models": [
                {"id": "usable-free", "context_window": 262_144},
                {"id": "muse-spark-1.2-contributor-free", "context_window": 131_072},
            ],
            "routes": {
                "our-free-model": ["usable-free"],
                "our-free-model-region": ["muse-spark-1.2-contributor-free"],
            },
            "status": runtime.provider_status(provider),
        }
        directory, domain, _config = self._domain(runtime)
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["providers"][0]
            self.assertEqual(
                {
                    "usable-free": "our-free-model",
                    "muse-spark-1.2-contributor-free": "our-free-model-region",
                },
                {
                    model["upstream_model"]: model["lane_source"]
                    for model in provider["models"]
                },
            )

    def test_the_model_pane_lists_the_lane_roster_instead_of_asking_the_wrong_address(self) -> None:
        """获取模型 on a lane provider reads the lane's own catalog.

        The lane's slot holds an ``os.environ/`` reference, so a ``/v1/models``
        read addressed it as if it were a user key: the request either raised
        ("the selected API key has no value") or named an endpoint that is not
        configured whenever the worker had not been started by this Core yet.
        The lane publishes a catalog, and that catalog is the only roster this
        provider has, so the same press the wizard's model step makes is the
        press the model pane makes.
        """

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["providers"][0]
            summary = domain.dispatch(
                "providers.fetch_models",
                {"provider_id": provider["id"]},
            )["operation_summary"]
            self.assertEqual(summary["operation"], "our_free_model_models")
            self.assertIs(summary["available"], True)
            self.assertEqual(
                [entry["id"] for entry in summary["models"]],
                [entry["id"] for entry in CATALOG],
            )

    def test_a_picked_lane_model_lands_on_the_slot_core_minted(self) -> None:
        """The pick goes through the account, which owns the roster's three facts.

        The public name a client sees is the catalog's own id, the route
        addresses that same id, and both answer with the minted slot.  A pane
        that rebuilt those from a listing would own a second copy of what the
        adapter publishes, and this is the write that keeps it one.
        """

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["providers"][0]
            summary = domain.dispatch(
                "model.import_service_catalog",
                {"provider_id": provider["id"], "models": [entry["id"] for entry in CATALOG]},
            )["operation_summary"]
            self.assertEqual(summary["operation"], "model_import_service_catalog")
            models = domain.snapshot()["providers"][0]["models"]
            self.assertEqual(
                [model["name"] for model in models],
                ["big-pickle", "mimo-v2.6-flash-free"],
            )
            for model, entry in zip(models, CATALOG):
                self.assertEqual(model["upstream_model"], entry["id"])
                self.assertEqual(model["api_key_name"], our_free_model.API_KEY_NAME)
                self.assertTrue(model["provider_key_id"])

    def test_re_picking_a_lane_roster_adds_nothing_twice(self) -> None:
        """A refresh over what the provider already carries is not a second copy."""

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["providers"][0]
            summary = domain.dispatch(
                "model.import_service_catalog",
                {"provider_id": provider["id"], "models": [entry["id"] for entry in CATALOG]},
            )["operation_summary"]
            self.assertEqual(summary["imported"], 0)
            self.assertEqual(len(domain.snapshot()["providers"][0]["models"]), len(CATALOG))

    def test_a_pick_the_catalog_no_longer_lists_is_refused(self) -> None:
        """The catalog is the authority on which ids exist, so it is re-read."""

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "our_free_model_login"}
            )["providers"][0]
            with self.assertRaises(DomainError):
                domain.dispatch(
                    "model.import_service_catalog",
                    {"provider_id": provider["id"], "models": ["no-such-model"]},
                )

    def test_a_custom_provider_still_lists_through_its_own_key(self) -> None:
        """The lane's catalog is the lane's; every other provider is untouched."""

        directory, domain, config = self._domain()
        with directory:
            config.write_text(
                "providers:\n"
                "  mine:\n"
                "    api_base: https://api.example.com/v1\n"
                "    api_keys:\n"
                "      - name: personal\n"
                "        value: sk-example\n"
                "model_list: []\n",
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(
                config,
                auth_manager=ProviderAuthManager(Path(directory.name)),
                our_free_model=StubRuntime(),
            )
            provider = domain.snapshot()["providers"][0]
            summary = domain.dispatch(
                "providers.fetch_models",
                {"provider_id": provider["id"], "api_key_name": "personal"},
            )["operation_summary"]
            self.assertEqual(summary["operation"], "fetch_models")
            with self.assertRaises(DomainError):
                domain.dispatch(
                    "model.import_service_catalog",
                    {"provider_id": provider["id"], "models": ["anything"]},
                )


class OurFreeModelStoreTests(unittest.TestCase):
    def test_core_launch_starts_only_a_saved_free_model_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                "providers:\n"
                "  mine: &mine\n"
                "    api_base: os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_BASE\n"
                "    api_keys:\n"
                "      - name: our-free-model-local\n"
                "        value: os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_KEY\n"
                "    x-young-router-provider-auth: {kind: our_free_model_login, credential_ref: configured}\n"
                "model_list:\n"
                "  - model_name: ours-model\n"
                "    litellm_params: {model: openai/model, api_base: *mine, api_key: os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_KEY}\n"
                "    model_info: {provider: mine}\n",
                encoding="utf-8",
            )
            runtime = StubRuntime()
            with mock.patch.object(our_free_model, "OurFreeModelRuntime", lambda root, settings=None: runtime), mock.patch.object(CoreStore, "_set_service_from_result", lambda *args, **kwargs: None):
                store = CoreStore.with_default_domains(config_path=config_path, runtime_root=root, metadata_path=root / "state.json")
                self.assertIs(store._our_free_model_runtime, runtime)
            self.assertIn(("autostart", "launch"), runtime.calls)
            store.shutdown()

            config_path.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
            runtime = StubRuntime()
            with mock.patch.object(our_free_model, "OurFreeModelRuntime", lambda root, settings=None: runtime):
                store = CoreStore.with_default_domains(
                    config_path=config_path,
                    runtime_root=root,
                    metadata_path=root / "state.json",
                )
            store.shutdown()
            self.assertNotIn(("autostart", "launch"), runtime.calls)

    def test_core_resolves_the_default_provider_document_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "provider-cache" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "providers:\n"
                "  ours: &ours\n"
                "    api_base: os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_BASE\n"
                "    api_keys:\n"
                "      - name: our-free-model-local\n"
                "        value: os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_KEY\n"
                "    x-young-router-provider-auth: {kind: our_free_model_login, credential_ref: configured}\n"
                "model_list:\n"
                "  - model_name: ours-model\n"
                "    litellm_params: {model: openai/model, api_base: *ours, api_key: os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_KEY}\n"
                "    model_info: {provider: ours}\n",
                encoding="utf-8",
            )
            runtime = StubRuntime()
            environment = {
                "YOUNG_ROUTER_HOME": str(root),
                "LITELLM_CONFIG_FILE": str(config_path),
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                our_free_model, "OurFreeModelRuntime", lambda root, settings=None: runtime
            ), mock.patch.object(CoreStore, "_set_service_from_result", lambda *args, **kwargs: None):
                store = CoreStore.with_default_domains(
                    runtime_root=root,
                    metadata_path=root / "state.json",
                )
            store.shutdown()
            self.assertIn(("autostart", "launch"), runtime.calls)

    def test_core_opens_an_unavailable_default_provider_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "provider-cache" / "config.yaml"
            config_path.parent.mkdir()
            runtime = StubRuntime()
            environment = {
                "YOUNG_ROUTER_HOME": str(root),
                "LITELLM_CONFIG_FILE": str(config_path),
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                our_free_model, "OurFreeModelRuntime", lambda root, settings=None: runtime
            ), mock.patch.object(CoreStore, "_set_service_from_result", lambda *args, **kwargs: None):
                store = CoreStore.with_default_domains(
                    runtime_root=root,
                    metadata_path=root / "state.json",
                )
            store.shutdown()
            self.assertNotIn(("autostart", "launch"), runtime.calls)
            self.assertEqual(
                [],
                store._domains["providers_models"].snapshot()["providers"],
            )

    def test_core_owns_the_worker_and_stops_it_on_shutdown(self) -> None:
        """A Core-owned worker never outlives the process that holds its bearer."""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        runtime = StubRuntime()
        store = CoreStore(
            metadata_path=root / "core-state.json",
            domains=[],
            service_handlers={},
            our_free_model_runtime=runtime,
        )
        store.shutdown()
        self.assertEqual(runtime.stopped, 1)

    def test_the_proxy_environment_carries_every_managed_worker(self) -> None:
        """The controller merges both workers' fragments for the proxy child."""

        from young_router.core.operations import CoreServiceController

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        controller = CoreServiceController(Path(directory.name))

        class Runtime:
            def __init__(self, fragment: dict[str, str]) -> None:
                self._fragment = fragment

            def environment(self, *, autostart: bool = False) -> dict[str, str]:
                return dict(self._fragment)

            def environment_values(self) -> dict[str, str]:
                return dict(self._fragment)

            def stop(self) -> None:
                pass

        controller.attach_workbuddy(
            Runtime({our_free_model.API_KEY_ENV[our_free_model.OUR_FREE_MODEL_PROVIDER]: "wb"})
        )
        controller.attach_our_free_model(
            Runtime({our_free_model.API_BASE_ENV[our_free_model.OUR_FREE_MODEL_PROVIDER]: "http://x"})
        )
        merged = controller._managed_worker_environment(autostart=False)
        self.assertEqual(
            merged[our_free_model.API_KEY_ENV[our_free_model.OUR_FREE_MODEL_PROVIDER]], "wb"
        )
        self.assertEqual(
            merged[our_free_model.API_BASE_ENV[our_free_model.OUR_FREE_MODEL_PROVIDER]], "http://x"
        )


class OurFreeModelAdapterTests(unittest.TestCase):
    def test_the_staged_root_and_references_are_named_after_the_integration(self) -> None:
        self.assertEqual(our_free_model.staged_root().name, "our-free-model")
        self.assertEqual(our_free_model.load_root().name, "our-free-model")
        esbuild = our_free_model.load_root() / "node_modules" / ".bin" / "esbuild"
        self.assertEqual(
            our_free_model.upstream_environment(),
            {"ESBUILD_BINARY_PATH": str(esbuild)} if esbuild.is_file() else {},
        )
        self.assertEqual(our_free_model.worker_path().name, "our_free_model_worker.mjs")
        self.assertEqual(
            our_free_model.api_base_reference("our-free-model"),
            "os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_BASE",
        )
        self.assertEqual(
            our_free_model.api_key_reference("our-free-model"),
            "os.environ/YOUNG_ROUTER_OUR_FREE_MODEL_KEY",
        )
        # The lane has exactly one spelling, and an unknown id is refused rather
        # than resolved to a reference nothing publishes.
        for unknown in ("opencode2dsh", "workbuddy", ""):
            with self.assertRaises(ValueError):
                our_free_model.api_base_reference(unknown)
        with self.assertRaises(ValueError):
            our_free_model.validated_provider("workbuddy")

    def test_available_requires_the_staged_manifest_and_worker(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        saved = os.environ.get("YOUNG_ROUTER_OUR_FREE_MODEL_DIR")
        saved_source = os.environ.get("YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR")
        os.environ["YOUNG_ROUTER_OUR_FREE_MODEL_DIR"] = directory.name
        os.environ["YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR"] = directory.name

        def restore() -> None:
            if saved is None:
                os.environ.pop("YOUNG_ROUTER_OUR_FREE_MODEL_DIR", None)
            else:
                os.environ["YOUNG_ROUTER_OUR_FREE_MODEL_DIR"] = saved
            if saved_source is None:
                os.environ.pop("YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR", None)
            else:
                os.environ["YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR"] = saved_source

        self.addCleanup(restore)
        staged = Path(directory.name)
        self.assertFalse(our_free_model.available())
        (staged / "package.json").write_text("{}", encoding="utf-8")
        saved_worker = os.environ.get("YOUNG_ROUTER_OUR_FREE_MODEL_WORKER")
        worker = staged / "our_free_model_worker.mjs"
        os.environ["YOUNG_ROUTER_OUR_FREE_MODEL_WORKER"] = str(worker)
        self.assertFalse(our_free_model.available())
        worker.write_text("", encoding="utf-8")
        try:
            self.assertTrue(our_free_model.available())
            (staged / "package.json").unlink()
            self.assertFalse(our_free_model.available())
        finally:
            if saved_worker is None:
                os.environ.pop("YOUNG_ROUTER_OUR_FREE_MODEL_WORKER", None)
            else:
                os.environ["YOUNG_ROUTER_OUR_FREE_MODEL_WORKER"] = saved_worker

    def test_runtime_settings_reach_the_bridge_only_when_committed(self) -> None:
        from young_router.core.domains.runtime import RuntimeSettingsDomain

        class Bridge:
            settings = {
                "defaultMaxTokens": "32768",
                "exposeRegionModels": "1",
                "probeIntervalMinutes": "15",
                "standaloneProbe": "0",
                "sourceDir": "",
            }

            def __init__(self) -> None:
                self.stops = 0

            def stop(self) -> None:
                self.stops += 1

        with tempfile.TemporaryDirectory() as directory:
            domain = RuntimeSettingsDomain(Path(directory) / "runtime-settings.env")
            bridge = Bridge()
            domain.attach_our_free_model(bridge)
            domain.dispatch(
                "set_setting",
                {"key": "YOUNG_ROUTER_OUR_FREE_MODEL_MAX_TOKENS", "value": "4096"},
            )
            self.assertEqual(bridge.stops, 0)
            domain.apply()
            self.assertEqual(bridge.settings["defaultMaxTokens"], "4096")
            self.assertEqual(bridge.settings["sourceDir"], "")
            self.assertEqual(bridge.stops, 1)


class LaneChannelTests(unittest.TestCase):
    """A roster is served from several upstreams, and the picker has to say so.

    The gateway's ``/v1/models`` is an OpenAI-shaped list: every row answers
    ``owned_by: 'our-free-model'`` whatever producer actually serves it, so the
    wizard showed 28 models from two upstreams under one heading.  An id cannot
    recover the producer either — ``big-pickle`` is the anonymous lane and
    ``kilo-auto/free`` is Kilo, and both are just ids — so the upstream's own
    catalog ``channel`` tag is carried through instead.
    """

    def test_the_anonymous_lane_is_an_answer_not_a_missing_one(self) -> None:
        """The free lane names no channel, and that must survive the bridge.

        Reporting it as absent would let a consumer drop those rows into
        whichever named producer happened to sort first, which is the flat list
        this exists to prevent.
        """

        self.assertEqual(
            {"big-pickle": "", "kilo-auto/free": "kilo"},
            our_free_model._channel_membership(
                {"big-pickle": "", "kilo-auto/free": "kilo"}
            ),
        )

    def test_an_unreadable_answer_states_no_producer(self) -> None:
        for value in (None, [], "kilo", 7):
            self.assertEqual({}, our_free_model._channel_membership(value))

    def test_a_row_without_an_id_is_dropped(self) -> None:
        self.assertEqual(
            {"kilo-auto/free": "kilo"},
            our_free_model._channel_membership({"": "kilo", "kilo-auto/free": "kilo"}),
        )

    def test_the_catalog_document_carries_the_producer_beside_the_routes(self) -> None:
        """One read states both splits, because a row belongs to both.

        ``routes`` answers "can this egress use it"; ``channels`` answers "who
        serves it".  A picker that had to make two reads could show the two
        answers from two different catalogs.
        """

        runtime = mock.Mock()
        runtime._request.return_value = {
            "ok": True,
            "models": [{"id": "big-pickle"}, {"id": "kilo-auto/free"}],
            "routes": {"our-free-model": ["big-pickle", "kilo-auto/free"]},
            "channels": {"big-pickle": "", "kilo-auto/free": "kilo"},
        }
        runtime._process = mock.Mock(poll=lambda: None)
        document = our_free_model.OurFreeModelRuntime.models(runtime)
        self.assertEqual(
            {"big-pickle": "", "kilo-auto/free": "kilo"}, document["channels"]
        )
        self.assertEqual(
            {"our-free-model": ["big-pickle", "kilo-auto/free"]}, document["routes"]
        )


if __name__ == "__main__":
    unittest.main()


class LaneNameCollisionTests(unittest.TestCase):
    """Two producers, one model, one public name is not a usable roster.

    The roster really does carry the same model twice — the anonymous lane calls
    it ``nemotron-3.5-super-vl-free`` and Kilo calls it
    ``nvidia/nemotron-3.5-super-vl:free``.  The ids differ, so an id-keyed import
    keeps both, and the user then sees two rows they cannot tell apart and two
    LiteLLM deployments sharing one public name.
    """

    def test_a_colliding_name_states_its_producer(self) -> None:
        self.assertEqual(
            "nemotron-3.5-super-vl-free (kilo)",
            ProvidersModelsDomain._unique_lane_name(
                "nemotron-3.5-super-vl-free",
                {"nemotron-3.5-super-vl-free": "kilo"},
                {"nemotron-3.5-super-vl-free"},
            ),
        )

    def test_an_unclaimed_name_is_left_exactly_as_the_catalog_spells_it(self) -> None:
        """Renaming on import is the user's decision, not the importer's."""

        self.assertEqual(
            "weird-vendor/model:free",
            ProvidersModelsDomain._unique_lane_name(
                "weird-vendor/model:free", {"weird-vendor/model:free": "kilo"}, set()
            ),
        )

    def test_a_row_with_no_producer_gets_a_counted_suffix(self) -> None:
        # The anonymous lane states no producer.  Borrowing one would file the
        # row under a source it does not come from, and returning the id would
        # hand back the very name that was taken.
        self.assertEqual(
            "big-pickle-free (2)",
            ProvidersModelsDomain._unique_lane_name(
                "big-pickle-free", {"big-pickle-free": ""}, {"big-pickle-free"}
            ),
        )

    def test_a_producer_tag_that_is_also_taken_falls_back_further(self) -> None:
        self.assertEqual(
            "x-free (3)",
            ProvidersModelsDomain._unique_lane_name(
                "x-free", {"x-free": "kilo"}, {"x-free", "x-free (kilo)", "x-free (2)"}
            ),
        )


class ChannelProviderSplitTests(unittest.TestCase):
    """One provider per upstream channel, and none of them guesses a wire.

    A channel decides two things about a row that a provider row is the only
    thing in this app able to state: which surface it answers on, and which
    window it accepts.  Sharing one provider across three channels forced every
    row onto one surface, and the rows that answered on the other were reported
    as "no such protocol" -- a protocol they never offered.
    """

    def _domain(self, runtime):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        domain = ProvidersModelsDomain(
            config, auth_manager=ProviderAuthManager(root), our_free_model=runtime
        )
        return directory, domain, config

    def _add_every_channel(self, domain) -> None:
        for provider_id in our_free_model.OUR_FREE_MODEL_PROVIDERS:
            domain.dispatch(
                "service_provider.add",
                {
                    "kind": "our_free_model_login",
                    "name": our_free_model.OUR_FREE_MODEL_DISPLAY_NAMES[provider_id],
                },
            )

    def test_every_channel_gets_its_own_provider_with_only_its_own_rows(self) -> None:
        directory, domain, _config = self._domain(ChannelRuntime())
        with directory:
            self._add_every_channel(domain)
            providers = {p["name"]: p for p in domain.snapshot()["providers"]}
            self.assertEqual(
                sorted(providers),
                sorted(our_free_model.OUR_FREE_MODEL_DISPLAY_NAMES.values()),
            )
            listed = {
                name: [m["model_name"] for m in provider["models"]]
                for name, provider in providers.items()
            }
            # Each row appears under the channel that serves it, exactly once.
            self.assertEqual(
                listed[our_free_model.OUR_FREE_MODEL_DISPLAY_NAMES[our_free_model.OUR_FREE_MODEL_PROVIDER]],
                ["big-pickle"],
            )
            self.assertEqual(
                listed[our_free_model.OUR_FREE_MODEL_DISPLAY_NAMES[our_free_model.OUR_FREE_MODEL_KILO_PROVIDER]],
                ["kilo-auto/free"],
            )
            self.assertEqual(
                listed[our_free_model.OUR_FREE_MODEL_DISPLAY_NAMES[our_free_model.OUR_FREE_MODEL_EAC_PROVIDER]],
                ["eac-model"],
            )

    def test_each_route_states_the_surface_and_window_its_channel_uses(self) -> None:
        directory, domain, _config = self._domain(ChannelRuntime())
        with directory:
            self._add_every_channel(domain)
            routes = {}
            for provider in domain.snapshot()["providers"]:
                for model in provider["models"]:
                    routes[model["model_name"]] = (model, provider)

            # The anonymous lane's row answers on Chat Completions: pinning it
            # to Responses is what made the probe report a missing protocol.
            model, _ = routes["big-pickle"]
            self.assertEqual(model["upstream_url_surface"], "openai/chat")
            # The stated window is the group's declaration, so the public model
            # reports the upstream's number rather than the unknown fallback.
            self.assertEqual(model["max_input_tokens"], 200_000)

            model, _ = routes["kilo-auto/free"]
            self.assertEqual(model["upstream_url_surface"], "openai/chat")
            self.assertEqual(model["max_input_tokens"], 131_072)

            # Kilo's own row states a different number, and it is kept whole:
            # one channel's window is not another's.
            model, _ = routes["eac-model"]
            self.assertEqual(model["max_input_tokens"], 1_048_576)


class ChannelRuntime(StubRuntime):
    """A lane that serves three channels, one row each."""

    ROWS = [
        {"id": "big-pickle", "name": "big-pickle"},
        {"id": "kilo-auto/free", "name": "kilo-auto/free"},
        {"id": "eac-model", "name": "eac-model"},
    ]

    def models(self, provider=our_free_model.OUR_FREE_MODEL_PROVIDER, *, refresh=False):  # noqa: D102
        return {
            "available": True,
            "detail": "",
            "source": "adapter",
            "models": [dict(row) for row in self.ROWS],
            "channels": {"big-pickle": "", "kilo-auto/free": "kilo", "eac-model": "eac"},
            "wires": {"big-pickle": "chat", "kilo-auto/free": "chat", "eac-model": "chat"},
            "windows": {"big-pickle": 200_000, "kilo-auto/free": 131_072, "eac-model": 1_048_576},
            "status": self.provider_status(provider),
        }


class StatedWindowIsDisplayedTests(unittest.TestCase):
    """The window the pane shows is the group's own, not the registry's guess.

    The projection used ``default_record_for``, which resolves registry metadata
    while *ignoring* the user's declaration.  An imported route declares the
    window its upstream stated, so the pane displayed the unknown-model fallback
    (272,000) for a row whose own catalog entry said 1,048,576 -- the number was
    in the document and the pane read around it.
    """

    def test_an_imported_window_reaches_the_pane(self) -> None:
        directory = tempfile.TemporaryDirectory()
        with directory:
            root = Path(directory.name)
            config = root / "config.yaml"
            config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
            domain = ProvidersModelsDomain(
                config,
                auth_manager=ProviderAuthManager(root),
                our_free_model=ChannelRuntime(),
            )
            for provider_id in our_free_model.OUR_FREE_MODEL_PROVIDERS:
                domain.dispatch(
                    "service_provider.add",
                    {
                        "kind": "our_free_model_login",
                        "name": our_free_model.OUR_FREE_MODEL_DISPLAY_NAMES[provider_id],
                    },
                )
            domain.apply()
            # The projection reads the saved document, so it is taken after the
            # write, the same way a pane sees it.
            domain.reload()
            contexts = domain.snapshot()["model_contexts"]
            self.assertEqual(contexts["big-pickle"]["context_window"], 200_000)
            self.assertEqual(contexts["kilo-auto/free"]["context_window"], 131_072)
            self.assertEqual(contexts["eac-model"]["context_window"], 1_048_576)


class AddOneChannelTests(unittest.TestCase):
    """Adding a channel adds that channel, and nothing else.

    The channels are parallel entries the user picks from, not a bundle: one
    login reaches all three, but a user who wants only Kilo must be able to add
    only Kilo, and a user who adds one thing must not find three rows.
    """

    def _domain(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        domain = ProvidersModelsDomain(
            config, auth_manager=ProviderAuthManager(root), our_free_model=ChannelRuntime()
        )
        return directory, domain

    def test_adding_kilo_adds_only_kilo(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain.dispatch(
                "service_provider.add",
                {"kind": "our_free_model_login", "name": our_free_model.OUR_FREE_MODEL_DISPLAY_NAMES[our_free_model.OUR_FREE_MODEL_KILO_PROVIDER]},
            )
            providers = domain.snapshot()["providers"]
            self.assertEqual(len(providers), 1)
            self.assertEqual(
                providers[0]["name"],
                our_free_model.OUR_FREE_MODEL_DISPLAY_NAMES[our_free_model.OUR_FREE_MODEL_KILO_PROVIDER],
            )
            self.assertEqual([m["model_name"] for m in providers[0]["models"]], ["kilo-auto/free"])

    def test_the_default_name_addresses_the_anonymous_lane(self) -> None:
        directory, domain = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "our_free_model_login"})
            providers = domain.snapshot()["providers"]
            self.assertEqual(len(providers), 1)
            self.assertEqual(providers[0]["name"], our_free_model.OUR_FREE_MODEL_DISPLAY_NAMES[our_free_model.OUR_FREE_MODEL_PROVIDER])
            self.assertEqual([m["model_name"] for m in providers[0]["models"]], ["big-pickle"])

"""OpenCode Zen free-lane provider integration.

The upstream protocol is a third-party package staged at build time, so these
tests drive the seam instead of the network: a stub runtime stands in for the
worker and asserts what Core asks it for and what the provider domain does with
the answer.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from young_router.adapters import opencode2dsh
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
            "providers": {opencode2dsh.OPENCODE2DSH_PROVIDER: self.provider_status()},
        }

    def provider_status(self, provider: str = opencode2dsh.OPENCODE2DSH_PROVIDER) -> dict[str, object]:
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
        self, provider: str = opencode2dsh.OPENCODE2DSH_PROVIDER, *, refresh: bool = False
    ) -> dict[str, object]:
        self.calls.append(("models", provider))
        if not self.available:
            return {"available": False, "detail": "integration_unstaged", "models": []}
        return {
            "available": True,
            "detail": "",
            "source": "adapter",
            "models": [dict(entry) for entry in CATALOG],
            "status": self.provider_status(provider),
        }

    def environment(self, *, autostart: bool = False) -> dict[str, str]:
        return {
            opencode2dsh.API_BASE_ENV[opencode2dsh.OPENCODE2DSH_PROVIDER]: "http://127.0.0.1:1/v1",
            opencode2dsh.API_KEY_ENV[opencode2dsh.OPENCODE2DSH_PROVIDER]: "local-token",
        }

    def environment_values(self) -> dict[str, str]:
        return self.environment()

    def stop(self) -> None:
        self.stopped += 1


class OpenCode2DSHProviderTests(unittest.TestCase):
    def _domain(self, runtime: StubRuntime | None = None):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        config = root / "config.yaml"
        config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
        domain = ProvidersModelsDomain(
            config,
            auth_manager=ProviderAuthManager(root),
            opencode2dsh=runtime if runtime is not None else StubRuntime(),
        )
        return directory, domain, config

    def test_service_provider_add_imports_the_live_catalog(self) -> None:
        """Adding the lane imports exactly the free roster the adapter serves."""

        directory, domain, config = self._domain()
        with directory:
            summary = domain.dispatch(
                "service_provider.add", {"kind": "opencode2dsh_login"}
            )["operation_summary"]
            self.assertEqual(summary["auth_kind"], "opencode2dsh_login")
            self.assertEqual(summary["auth_status"], "signed_in")

            provider = domain.snapshot()["providers"][0]
            self.assertEqual(provider["name"], opencode2dsh.OPENCODE2DSH_DISPLAY_NAME)
            self.assertEqual(provider["auth_status"], "signed_in")
            self.assertEqual(opencode2dsh.API_KEY_NAME, "opencode-zen-free")
            self.assertEqual(provider["key_states"][0]["name"], "opencode-zen-free")
            self.assertEqual(
                provider["api_base"], opencode2dsh.api_base_reference(opencode2dsh.OPENCODE2DSH_PROVIDER)
            )
            # An imported roster is a set of peers, not a ranked list: every
            # route starts on the same order instead of the catalog's own 1, 2.
            self.assertEqual([model["order"] for model in provider["models"]], [0, 0])
            # The lane's own ``-free`` suffix is dropped from the public name,
            # while the route keeps addressing the upstream's real id.
            self.assertEqual(
                [(model["model_name"], model["upstream_model"]) for model in provider["models"]],
                [("big-pickle", "big-pickle"), ("mimo-v2.6-flash", "mimo-v2.6-flash-free")],
            )
            first = provider["models"][0]
            self.assertEqual(first["litellm_model"], "openai/big-pickle")
            self.assertEqual(first["upstream_url_surface"], "openai/chat")
            self.assertEqual(first["upstream_protocol_mode"], "fixed")
            self.assertEqual(first["api_key_name"], opencode2dsh.API_KEY_NAME)
            self.assertTrue(provider["key_states"][0]["configured"])

            self.assertTrue(domain.validate()["valid"])
            domain.apply()
            document = config.read_text(encoding="utf-8")
            # Only environment references reach the file: the worker port and
            # its bearer stay inside the Core process.
            self.assertIn("os.environ/YOUNG_ROUTER_OPENCODE2DSH_BASE", document)
            self.assertIn("os.environ/YOUNG_ROUTER_OPENCODE2DSH_KEY", document)
            self.assertNotIn("127.0.0.1", document)
            self.assertNotIn("local-token", document)
            self.assertIn("model: openai/big-pickle", document)

    def test_an_unreachable_lane_refuses_the_add_instead_of_creating_an_empty_account(self) -> None:
        """The lane's own refusal is reported; no provider is created for nothing."""

        directory, domain, _config = self._domain(StubRuntime(available=False))
        with directory:
            with self.assertRaisesRegex(DomainError, "anonymous free lane could not be reached"):
                domain.dispatch("service_provider.add", {"kind": "opencode2dsh_login"})

    def test_the_status_and_catalog_reads_answer_through_the_operation_summary(self) -> None:
        directory, domain, _config = self._domain()
        with directory:
            status = domain.dispatch("opencode2dsh_status", {})["operation_summary"]
            self.assertEqual(status["operation"], "opencode2dsh_status")
            self.assertTrue(status["available"])
            self.assertEqual(
                status["providers"][opencode2dsh.OPENCODE2DSH_PROVIDER]["state"], "ready"
            )

            catalog = domain.dispatch("opencode2dsh_models", {})["operation_summary"]
            self.assertEqual(catalog["operation"], "opencode2dsh_models")
            self.assertTrue(catalog["available"])
            self.assertEqual(
                [entry["id"] for entry in catalog["models"]],
                ["big-pickle", "mimo-v2.6-flash-free"],
            )

    def test_no_credential_of_the_user_ever_reaches_the_lane(self) -> None:
        """The lane authenticates itself, so the provider carries no user secret."""

        directory, domain, config = self._domain()
        with directory:
            domain.dispatch("service_provider.add", {"kind": "opencode2dsh_login"})
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
            domain.dispatch("service_provider.add", {"kind": "opencode2dsh_login"})
            slot = domain.snapshot()["providers"][0]["key_states"][0]
            self.assertEqual(slot["name"], opencode2dsh.API_KEY_NAME)
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
                "model_list: []\n",
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(
                config,
                auth_manager=ProviderAuthManager(Path(directory.name)),
                opencode2dsh=StubRuntime(),
            )
            domain.dispatch("service_provider.add", {"kind": "opencode2dsh_login"})
            providers = {item["name"]: item for item in domain.snapshot()["providers"]}
            self.assertIs(providers["mine"]["key_states"][0]["managed"], False)
            self.assertIs(providers[opencode2dsh.OPENCODE2DSH_DISPLAY_NAME]["key_states"][0]["managed"], True)
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
            domain.dispatch("service_provider.add", {"kind": "opencode2dsh_login"})
            provider = domain.snapshot()["providers"][0]
            target = f"{provider['name']}\x1f{opencode2dsh.API_KEY_NAME}"
            with self.assertRaises(DomainError):
                domain.stage_secret("api_key", target, "sk-attacker")
            self.assertEqual(
                domain.trusted_secret_value("api_key", target),
                opencode2dsh.api_key_reference(opencode2dsh.OPENCODE2DSH_PROVIDER),
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
            opencode2dsh.api_key_reference(opencode2dsh.OPENCODE2DSH_PROVIDER),
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
                "service_provider.add", {"kind": "opencode2dsh_login"}
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
            self.assertEqual(summary["auth_kind"], "opencode2dsh_login")
            self.assertEqual(domain.snapshot()["providers"], [])

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
                "service_provider.add", {"kind": "opencode2dsh_login"}
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
            domain.dispatch("service_provider.add", {"kind": "opencode2dsh_login"})
            domain.apply()

            reloaded = ProvidersModelsDomain(
                config,
                auth_manager=ProviderAuthManager(config.parent),
                opencode2dsh=StubRuntime(),
            )
            before = reloaded.snapshot()["providers"][0]["models"]
            self.assertEqual(
                [None, None],
                [model["upstream_context_window"] for model in before],
            )

            reloaded.dispatch("opencode2dsh_models", {"provider": "opencode2dsh"})
            after = reloaded.snapshot()["providers"][0]["models"]
            self.assertEqual(
                [200_000, 1_048_576],
                [model["upstream_context_window"] for model in after],
            )

    def test_a_lane_that_states_no_window_reports_none(self) -> None:
        """An upstream that published nothing leaves the row off, not at zero."""

        runtime = StubRuntime()
        runtime.models = lambda provider=opencode2dsh.OPENCODE2DSH_PROVIDER, refresh=False: {
            "available": True,
            "detail": "",
            "source": "adapter",
            "models": [{"id": "big-pickle", "name": "big-pickle"}],
            "status": runtime.provider_status(provider),
        }
        directory, domain, _config = self._domain(runtime)
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "opencode2dsh_login"}
            )["providers"][0]
            self.assertIsNone(provider["models"][0]["upstream_context_window"])

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
                "service_provider.add", {"kind": "opencode2dsh_login"}
            )["providers"][0]
            summary = domain.dispatch(
                "providers.fetch_models",
                {"provider_id": provider["id"]},
            )["operation_summary"]
            self.assertEqual(summary["operation"], "opencode2dsh_models")
            self.assertIs(summary["available"], True)
            self.assertEqual(
                [entry["id"] for entry in summary["models"]],
                [entry["id"] for entry in CATALOG],
            )

    def test_a_picked_lane_model_lands_on_the_slot_core_minted(self) -> None:
        """The pick goes through the account, which owns the roster's three facts.

        The public name a client sees drops the lane's ``-free`` suffix, the
        route addresses the upstream's real id, and both answer with the minted
        slot.  A pane that rebuilt those from a listing would own a second copy
        of what the adapter publishes, and this is the write that keeps it one.
        """

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "opencode2dsh_login"}
            )["providers"][0]
            summary = domain.dispatch(
                "model.import_service_catalog",
                {"provider_id": provider["id"], "models": [entry["id"] for entry in CATALOG]},
            )["operation_summary"]
            self.assertEqual(summary["operation"], "model_import_service_catalog")
            models = domain.snapshot()["providers"][0]["models"]
            self.assertEqual(
                [model["name"] for model in models],
                ["big-pickle", "mimo-v2.6-flash"],
            )
            for model, entry in zip(models, CATALOG):
                self.assertEqual(model["upstream_model"], entry["id"])
                self.assertEqual(model["api_key_name"], opencode2dsh.API_KEY_NAME)
                self.assertTrue(model["provider_key_id"])

    def test_re_picking_a_lane_roster_adds_nothing_twice(self) -> None:
        """A refresh over what the provider already carries is not a second copy."""

        directory, domain, _config = self._domain()
        with directory:
            provider = domain.dispatch(
                "service_provider.add", {"kind": "opencode2dsh_login"}
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
                "service_provider.add", {"kind": "opencode2dsh_login"}
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
                opencode2dsh=StubRuntime(),
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


class OpenCode2DSHStoreTests(unittest.TestCase):
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
            opencode2dsh_runtime=runtime,
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
            Runtime({opencode2dsh.API_KEY_ENV[opencode2dsh.OPENCODE2DSH_PROVIDER]: "wb"})
        )
        controller.attach_opencode2dsh(
            Runtime({opencode2dsh.API_BASE_ENV[opencode2dsh.OPENCODE2DSH_PROVIDER]: "http://x"})
        )
        merged = controller._managed_worker_environment(autostart=False)
        self.assertEqual(
            merged[opencode2dsh.API_KEY_ENV[opencode2dsh.OPENCODE2DSH_PROVIDER]], "wb"
        )
        self.assertEqual(
            merged[opencode2dsh.API_BASE_ENV[opencode2dsh.OPENCODE2DSH_PROVIDER]], "http://x"
        )


class OpenCode2DSHAdapterTests(unittest.TestCase):
    def test_the_staged_root_and_references_are_named_after_the_integration(self) -> None:
        self.assertEqual(opencode2dsh.staged_root().name, "opencode2dsh")
        self.assertEqual(opencode2dsh.worker_path().name, "opencode2dsh_worker.mjs")
        self.assertEqual(
            opencode2dsh.api_base_reference("opencode2dsh"),
            "os.environ/YOUNG_ROUTER_OPENCODE2DSH_BASE",
        )
        self.assertEqual(
            opencode2dsh.api_key_reference("opencode2dsh"),
            "os.environ/YOUNG_ROUTER_OPENCODE2DSH_KEY",
        )
        with self.assertRaises(ValueError):
            opencode2dsh.validated_provider("workbuddy")

    def test_available_requires_the_staged_entry_point(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        saved = os.environ.get("YOUNG_ROUTER_OPENCODE2DSH_DIR")
        os.environ["YOUNG_ROUTER_OPENCODE2DSH_DIR"] = directory.name

        def restore() -> None:
            if saved is None:
                os.environ.pop("YOUNG_ROUTER_OPENCODE2DSH_DIR", None)
            else:
                os.environ["YOUNG_ROUTER_OPENCODE2DSH_DIR"] = saved

        self.addCleanup(restore)
        self.assertFalse(opencode2dsh.available())
        (Path(directory.name) / "lib").mkdir()
        (Path(directory.name) / "package.json").write_text("{}", encoding="utf-8")
        self.assertFalse(opencode2dsh.available())
        (Path(directory.name) / "lib" / "index.js").write_text("", encoding="utf-8")
        self.assertTrue(opencode2dsh.available())


if __name__ == "__main__":
    unittest.main()

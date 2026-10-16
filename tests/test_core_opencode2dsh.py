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

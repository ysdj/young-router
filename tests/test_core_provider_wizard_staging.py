from __future__ import annotations

import json
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from young_router.core.domains._shared import DomainError
from young_router.core.domains.providers_models import ProvidersModelsDomain


EMPTY_CONFIG = """
providers: {}

model_list: []
"""


def wizard_target(token: str, key_name: str) -> str:
    separator = ProvidersModelsDomain._API_KEY_TARGET_SEPARATOR
    return f"{ProvidersModelsDomain._WIZARD_KEY_TARGET_PREFIX}{token}{separator}{key_name}"


class ProviderWizardStagingTests(unittest.TestCase):
    """The wizard stages a provider's key before the provider exists.

    添加向导 may not mint a provider from 下一步: its steps stage the name, the
    key, and the models, and 完成 is the one act that creates them together.
    A key therefore has to be able to wait for the provider it belongs to.
    """

    def test_a_staged_key_is_adopted_by_the_create_it_belongs_to(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(EMPTY_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            domain.stage_secret("api_key", wizard_target("wizard-1", "quartz"), "replace-staged-secret")

            # Staging reserves no provider and never exposes the value.
            self.assertEqual([], domain.snapshot()["providers"])
            self.assertNotIn("replace-staged-secret", json.dumps(domain.snapshot()))
            self.assertTrue(domain.secret_present("api_key", wizard_target("wizard-1", "quartz")))

            created = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "New Provider",
                        "api_base": "https://example.test/v1",
                        "auth_kind": "api_key",
                        "enabled": True,
                        "models": [],
                        "create_default_api_key": True,
                        "initial_api_key_name": "quartz",
                    },
                    "pending_api_key": "wizard-1",
                },
            )
            provider = created["providers"][0]
            self.assertEqual("New Provider", provider["name"])
            self.assertEqual([("quartz", True)], [(key["name"], key["configured"]) for key in provider["key_states"]])
            # The token is one-time: the value it filed belongs to that create.
            self.assertFalse(domain.secret_present("api_key", wizard_target("wizard-1", "quartz")))
            self.assertNotIn("replace-staged-secret", json.dumps(created))

            domain.apply()
            written = path.read_text(encoding="utf-8")
            self.assertIn("replace-staged-secret", written)

    def test_the_create_takes_the_value_the_user_named_last(self) -> None:
        # The staged key carries the wizard's placeholder name; the key the
        # provider gets is the name the wizard's own field held at 完成.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(EMPTY_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            domain.stage_secret("api_key", wizard_target("wizard-2", "quartz"), "replace-renamed-secret")
            created = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "New Provider",
                        "api_base": "https://example.test/v1",
                        "auth_kind": "api_key",
                        "models": [],
                        "create_default_api_key": True,
                        "initial_api_key_name": "renamed",
                    },
                    "pending_api_key": "wizard-2",
                },
            )

            provider = created["providers"][0]
            self.assertEqual(["renamed"], [key["name"] for key in provider["key_states"]])
            self.assertTrue(provider["key_states"][0]["configured"])

    def test_a_create_without_its_staged_key_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(EMPTY_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            with self.assertRaises(DomainError):
                domain.dispatch(
                    "provider.add",
                    {
                        "provider": {
                            "name": "New Provider",
                            "api_base": "https://example.test/v1",
                            "auth_kind": "api_key",
                            "models": [],
                            "create_default_api_key": True,
                            "initial_api_key_name": "quartz",
                        },
                        "pending_api_key": "wizard-missing",
                    },
                )
            self.assertEqual([], domain.snapshot()["providers"])

    def test_a_discarded_draft_takes_the_staged_keys_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(EMPTY_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            domain.stage_secret("api_key", wizard_target("wizard-3", "quartz"), "replace-discarded-secret")
            domain.dispatch("cancel", {})
            self.assertFalse(domain.secret_present("api_key", wizard_target("wizard-3", "quartz")))

            domain.stage_secret("api_key", wizard_target("wizard-4", "quartz"), "replace-discarded-secret")
            domain.dispatch("provider.discard_pending_key", {"pending_api_key": "wizard-4"})
            self.assertFalse(domain.secret_present("api_key", wizard_target("wizard-4", "quartz")))

    def test_a_pending_listing_reads_the_staged_key_and_creates_nothing(self) -> None:
        requests: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append((self.path, self.headers.get("Authorization", "")))
                body = json.dumps({"object": "list", "data": [{"id": "model-a"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(textwrap.dedent(EMPTY_CONFIG).lstrip(), encoding="utf-8")
                domain = ProvidersModelsDomain(path)
                domain.stage_secret("api_key", wizard_target("wizard-5", "quartz"), "replace-staged-secret")

                fetched = domain.dispatch(
                    "providers.fetch_models",
                    {
                        "pending_provider": {
                            "pending_api_key": "wizard-5",
                            "name": "New Provider",
                            "api_base": f"http://127.0.0.1:{port}/v1",
                            "api_key_name": "quartz",
                        }
                    },
                )["operation_summary"]

                self.assertTrue(fetched["available"])
                self.assertEqual(["model-a"], fetched["models"])
                self.assertEqual(["/v1/models"], [path for path, _ in requests])
                self.assertEqual(["Bearer replace-staged-secret"], [header for _, header in requests])
                self.assertNotIn("replace-staged-secret", json.dumps(fetched))
                # A listing is a read: it leaves no provider and keeps the
                # staged key for the create that follows it.
                self.assertEqual([], domain.snapshot()["providers"])
                self.assertTrue(domain.secret_present("api_key", wizard_target("wizard-5", "quartz")))

                with self.assertRaises(DomainError):
                    domain.dispatch(
                        "providers.fetch_models",
                        {
                            "pending_provider": {
                                "pending_api_key": "wizard-missing",
                                "name": "New Provider",
                                "api_base": f"http://127.0.0.1:{port}/v1",
                                "api_key_name": "quartz",
                            }
                        },
                    )
        finally:
            server.shutdown()

    def test_the_wizard_sequence_stages_then_creates_the_whole_provider(self) -> None:
        """Stage the key, list models, create once, then add the models.

        This is the order 添加向导 runs after 完成: nothing exists while the
        steps are being filled in, and the create carries the staged key.
        """

        requests: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append((self.path, self.headers.get("Authorization", "")))
                body = json.dumps({"object": "list", "data": [{"id": "model-a"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(textwrap.dedent(EMPTY_CONFIG).lstrip(), encoding="utf-8")
                domain = ProvidersModelsDomain(path)
                target = wizard_target("wizard-6", "quartz")
                base = f"http://127.0.0.1:{port}/v1"

                domain.stage_secret("api_key", target, "replace-staged-secret")
                self.assertEqual([], domain.snapshot()["providers"])
                self.assertEqual(
                    "replace-staged-secret",
                    domain.trusted_secret_value("api_key", target),
                )

                fetched = domain.dispatch(
                    "providers.fetch_models",
                    {"pending_provider": {"pending_api_key": "wizard-6", "name": "Staged", "api_base": base, "api_key_name": "quartz"}},
                )["operation_summary"]
                self.assertEqual(["model-a"], fetched["models"])
                self.assertEqual([], domain.snapshot()["providers"])

                created = domain.dispatch(
                    "provider.add",
                    {
                        "provider": {
                            "name": "Staged",
                            "api_base": base,
                            "auth_kind": "api_key",
                            "enabled": True,
                            "models": [],
                            "create_default_api_key": True,
                            "initial_api_key_name": "quartz",
                        },
                        "pending_api_key": "wizard-6",
                    },
                )
                created_id = created["providers"][0]["id"]
                added = domain.dispatch(
                    "model.add_many",
                    {
                        "provider_id": created_id,
                        "models": [{"name": "model-a", "upstream_model": "model-a", "api_key_name": "quartz", "enabled": True, "order": 1}],
                    },
                )
                self.assertEqual(["model-a"], [model["name"] for model in added["providers"][0]["models"]])
                # Applying the finished record is what writes it: the provider,
                # its key, and the model reach the config together.
                domain.apply()

                written = path.read_text(encoding="utf-8")
                self.assertIn("replace-staged-secret", written)
                self.assertIn("model-a", written)
                # The key the wizard staged is the provider's own key, and no
                # empty second slot was created beside it.
                provider = ProvidersModelsDomain(path).snapshot()["providers"][0]
                self.assertEqual(["quartz"], [key["name"] for key in provider["key_states"]])
        finally:
            server.shutdown()

    def test_a_provider_target_still_needs_a_provider(self) -> None:
        # Only the wizard's own prefix files a waiting key; a typo stays an
        # invalid secret instead of a value nothing will ever adopt.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(EMPTY_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            with self.assertRaises(DomainError):
                domain.stage_secret("api_key", "missing-provider\x1fkey", "replace-orphan-secret")
            with self.assertRaises(DomainError):
                domain.stage_secret("api_key", "__wizard_provider__\x1fkey", "replace-orphan-secret")


if __name__ == "__main__":
    unittest.main()

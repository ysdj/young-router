from __future__ import annotations

import json
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

from litellm_menu.core.domains.legacy import (
    CodexSettingsDomain,
    LegacyDomainError,
    ProvidersModelsDomain,
    RuntimeSettingsDomain,
    WebDAVSettingsDomain,
)
from litellm_menu.core.service import CoreStore
from runtime_settings_io import RuntimeSettingSpec


PROVIDER_CONFIG = """
providers:
  primary:
    api_base: "https://example.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret"
    future_provider_field:
      keep: true
model_list:
  - model_name: default-chat
    litellm_params:
      model: openai/default-chat
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
      future_param: keep
    model_info:
      id: "00000071"
      provider: primary
      upstream_url_surface: openai/responses
      supported_upstream_url_surfaces: [openai/responses]
      future_info: keep
litellm_settings:
  public_model_groups: [default-chat]
future_top_level:
  keep: true
"""


class ProvidersModelsDomainTests(unittest.TestCase):
    @staticmethod
    def _billing_payload() -> dict[str, object]:
        return {
            "providers": [
                {
                    "name": "primary",
                    "models": [
                        {
                            "deployment_id": "00000071",
                            "status": "ok",
                            "detail": "Billing data is available.",
                            "source": "synthetic-v1-usage",
                            "balance": {"amount": 12.5, "currency": "USD"},
                            "usage": {"amount": 3.25, "api_key": "sk-synthetic-billing-secret"},
                            "multiplier": {"status": "ok", "value": 1.25},
                        }
                    ],
                }
            ],
            "summary": {"providers": 1, "models": 1, "available_models": 1, "unavailable_models": 0},
        }

    def test_billing_refresh_is_a_read_only_overlay_and_apply_never_persists_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            source = textwrap.dedent(PROVIDER_CONFIG).lstrip()
            path.write_text(source, encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            core = CoreStore(domains=[domain])

            with mock.patch("provider_billing.collect_billing", return_value=self._billing_payload()):
                result = core.dispatch(
                    {"domain": "providers_models", "type": "providers.refresh_billing"},
                    expected_revision=core.revision,
                )

            snapshot = core.snapshot()
            self.assertEqual(result["revision"], snapshot["revision"])
            self.assertFalse(snapshot["drafts"]["providers_models"]["dirty"])
            model = snapshot["domains"]["providers_models"]["providers"][0]["models"][0]
            self.assertEqual(12.5, model["billing"]["balance"]["amount"])
            self.assertEqual(3.25, model["usage"]["amount"])
            self.assertEqual(1.25, model["multiplier"]["value"])
            self.assertNotIn("sk-synthetic-billing-secret", json.dumps(snapshot))

            exported = domain.export(include_sensitive=True)
            self.assertNotIn("billing", json.dumps(exported))
            self.assertNotIn("usage", json.dumps(exported))
            self.assertNotIn("multiplier", json.dumps(exported))
            core.apply("providers_models", revision=core.revision)
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("billing", saved)
            self.assertNotIn("usage", saved)
            self.assertNotIn("multiplier", saved)
            self.assertIn("future_top_level", saved)
            self.assertIn("future_param", saved)
            self.assertIn("future_info", saved)

    def test_billing_refresh_failure_is_safe_and_preserves_draft_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            core = CoreStore(domains=[domain])
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.patch",
                    "payload": {"provider_id": "primary", "changes": {"enabled": False}},
                },
                expected_revision=core.revision,
            )
            self.assertTrue(core.snapshot()["drafts"]["providers_models"]["dirty"])

            with mock.patch(
                "provider_billing.collect_billing",
                side_effect=RuntimeError("secret token-synthetic-refresh-error /private/user/billing.json"),
            ):
                core.dispatch(
                    {"domain": "providers_models", "type": "providers.refresh_billing"},
                    expected_revision=core.revision,
                )

            snapshot = core.snapshot()
            self.assertTrue(snapshot["drafts"]["providers_models"]["dirty"])
            operation = snapshot["action_summaries"]["providers_models"]["operation_summary"]
            self.assertEqual({"operation": "billing", "available": False}, operation)
            encoded = json.dumps(snapshot)
            self.assertNotIn("token-synthetic-refresh-error", encoded)
            self.assertNotIn("/private/user", encoded)

    def test_canonical_actions_stage_and_apply_without_exposing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            snapshot = domain.snapshot()
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            self.assertNotIn(str(path), json.dumps(snapshot))
            domain.dispatch("provider.patch", {"provider_id": "primary", "changes": {"endpoint": "https://example.com/v1"}})
            domain.dispatch("model.patch", {"provider_id": "primary", "model_id": "00000071", "changes": {"upstream_model": "openai/fast-chat"}})
            domain.dispatch("provider.add", {"provider": {"name": "backup", "enabled": True, "models": []}})
            domain.dispatch("provider.move", {"provider_id": "backup", "direction": "up"})
            self.assertTrue(domain.validate()["valid"])

            result = domain.apply()

            self.assertTrue(result["applied"])
            saved = path.read_text(encoding="utf-8")
            self.assertIn("future_top_level", saved)
            self.assertIn("future_param", saved)
            self.assertIn("future_info", saved)
            self.assertIn("openai/fast-chat", saved)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_core_snapshot_keeps_api_key_labels_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")

            snapshot = CoreStore(domains=[ProvidersModelsDomain(path)]).snapshot()
            provider = snapshot["domains"]["providers_models"]["providers"][0]

            self.assertEqual(["default"], provider["api_key_names"])
            self.assertEqual("default", provider["models"][0]["api_key_name"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))

    def test_apply_refuses_an_external_disk_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            source = textwrap.dedent(PROVIDER_CONFIG).lstrip()
            path.write_text(source, encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch("provider.patch", {"provider_id": "primary", "changes": {"enabled": False}})
            path.write_text(source + "external_change: true\n", encoding="utf-8")

            with self.assertRaisesRegex(LegacyDomainError, "changed on disk"):
                domain.apply()

    def test_first_apply_creates_private_config_and_rejects_concurrent_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "config.yaml"
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "provider.add",
                {"provider": {"name": "primary", "api_base": "https://example.test/v1", "enabled": True, "models": []}},
            )
            domain.apply()

            self.assertTrue(path.is_file())
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            domain = ProvidersModelsDomain(path)
            domain.dispatch("provider.add", {"provider": {"name": "primary", "models": []}})
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")

            with self.assertRaisesRegex(LegacyDomainError, "changed on disk"):
                domain.apply()

    def test_new_provider_gets_a_core_owned_blank_named_key_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            snapshot = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "primary",
                        "models": [],
                        "create_default_api_key": True,
                    }
                },
            )

            provider = snapshot["providers"][0]
            self.assertEqual(["default"], provider["api_key_names"])
            self.assertFalse(provider["api_key_configured"])
            self.assertNotIn("value", json.dumps(snapshot))
            self.assertFalse(domain.validate()["valid"])

    def test_new_draft_ids_stay_stable_across_move_and_order_remains_numeric(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            domain.dispatch("provider.add", {"provider": {"name": "", "models": []}})
            first = domain.snapshot()["providers"][0]["id"]
            domain.dispatch("provider.add", {"provider": {"name": "backup", "models": []}})
            domain.dispatch("provider.move", {"provider_id": first, "direction": "down"})
            moved = next(item for item in domain.snapshot()["providers"] if not item["name"])
            domain.dispatch("model.add", {"provider_id": first, "model": {"name": "", "order": 2}})
            model = next(item for item in domain.snapshot()["providers"] if not item["name"])["models"][0]

            self.assertEqual(first, moved["id"])
            self.assertTrue(model["id"].startswith("model-"))
            self.assertEqual(2, model["order"])

    def test_model_api_key_name_patch_is_safe_and_survives_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "provider.patch",
                {
                    "provider_id": "primary",
                    "changes": {
                        "api_keys": [
                            {"name": "default", "value": "replace-me-secret"},
                            {"name": "secondary", "value": "replace-me-secondary-secret"},
                        ]
                    },
                },
            )
            snapshot = domain.dispatch(
                "model.patch",
                {
                    "provider_id": "primary",
                    "model_id": "00000071",
                    "changes": {"api_key_name": "secondary"},
                },
            )

            provider = snapshot["providers"][0]
            self.assertEqual(["default", "secondary"], provider["api_key_names"])
            self.assertEqual("secondary", provider["models"][0]["api_key_name"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            self.assertNotIn("replace-me-secondary-secret", json.dumps(snapshot))

            domain.apply()
            reloaded = ProvidersModelsDomain(path).snapshot()["providers"][0]
            self.assertEqual("secondary", reloaded["models"][0]["api_key_name"])

    def test_model_move_provider_uses_destination_key_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "backup",
                        "enabled": True,
                        "api_base": "https://backup.example.test/v1",
                        "api_key": "replace-me-backup-secret",
                        "api_keys": [
                            {"name": "backup-first", "value": "replace-me-backup-secret"},
                            {"name": "backup-second", "value": "replace-me-other-secret"},
                        ],
                        "models": [],
                    }
                },
            )

            snapshot = domain.dispatch(
                "model.move_provider",
                {
                    "provider_id": "primary",
                    "model_id": "00000071",
                    "destination_provider_id": "backup",
                },
            )
            providers = {provider["name"]: provider for provider in snapshot["providers"]}
            self.assertEqual([], providers["primary"]["models"])
            self.assertEqual(["backup-first", "backup-second"], providers["backup"]["api_key_names"])
            moved = providers["backup"]["models"][0]
            self.assertEqual("backup", moved["provider"])
            self.assertEqual("", moved["api_base"])
            self.assertEqual("backup-first", moved["api_key_name"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            self.assertNotIn("replace-me-backup-secret", json.dumps(snapshot))
            self.assertNotIn("replace-me-other-secret", json.dumps(snapshot))

            domain.apply()
            reloaded = ProvidersModelsDomain(path).snapshot()
            reloaded_providers = {provider["name"]: provider for provider in reloaded["providers"]}
            reloaded_model = reloaded_providers["backup"]["models"][0]
            self.assertEqual("backup", reloaded_model["provider"])
            self.assertEqual("backup-first", reloaded_model["api_key_name"])

    def test_provider_key_actions_stage_values_only_through_named_secret_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            pending = domain.dispatch(
                "provider.key_add",
                {"provider_id": "primary", "name": "secondary"},
            )
            self.assertEqual(["default", "secondary"], pending["providers"][0]["api_key_names"])
            self.assertFalse(domain.secret_present("api_key", "primary\x1fsecondary"))
            self.assertFalse(domain.validate()["valid"])
            self.assertNotIn("value", json.dumps(pending))

            domain.stage_secret("api_key", "primary\x1fsecondary", "replace-me-secondary-secret")
            self.assertTrue(domain.secret_present("api_key", "primary\x1fsecondary"))
            self.assertTrue(domain.secret_present("api_key", "primary"))
            self.assertTrue(domain.validate()["valid"])

            renamed = domain.dispatch(
                "provider.key_patch",
                {"provider_id": "primary", "old_name": "secondary", "name": "fallback"},
            )
            provider = renamed["providers"][0]
            self.assertEqual(["default", "fallback"], provider["api_key_names"])
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": "primary",
                    "model_id": "00000071",
                    "changes": {"api_key_name": "fallback"},
                },
            )
            deleted = domain.dispatch(
                "provider.key_delete",
                {"provider_id": "primary", "name": "fallback"},
            )
            provider = deleted["providers"][0]
            self.assertEqual(["default"], provider["api_key_names"])
            self.assertEqual("default", provider["models"][0]["api_key_name"])
            with self.assertRaisesRegex(LegacyDomainError, "retain at least one"):
                domain.dispatch(
                    "provider.key_delete",
                    {"provider_id": "primary", "name": "default"},
                )
            self.assertNotIn("replace-me-secondary-secret", json.dumps(deleted))

            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("fallback", saved)
            self.assertIn("replace-me-secret", saved)

    def test_provider_import_link_is_staged_only_through_native_secret_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            link = (
                "ccswitch://v1/import?resource=provider&app=codex&name=primary"
                "&endpoint=https%3A%2F%2Fexample.test%2Fv1&apiKey=replace-me-secret"
                "&model=default-chat"
            )

            self.assertFalse(domain.secret_present("import_link"))
            domain.stage_secret("import_link", None, link)
            snapshot = domain.snapshot()
            self.assertEqual(1, snapshot["provider_count"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            self.assertNotIn(link, json.dumps(snapshot))

    def test_provider_key_rename_updates_model_references_and_named_secret_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "provider.key_add",
                {"provider_id": "primary", "name": "secondary"},
            )
            domain.stage_secret("api_key", "primary\x1fsecondary", "replace-me-secondary-secret")
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": "primary",
                    "model_id": "00000071",
                    "changes": {"api_key_name": "secondary"},
                },
            )

            snapshot = domain.dispatch(
                "provider.key_patch",
                {"provider_id": "primary", "old_name": "secondary", "name": "fallback"},
            )
            provider = snapshot["providers"][0]
            self.assertEqual("fallback", provider["models"][0]["api_key_name"])
            self.assertTrue(domain.secret_present("api_key", "primary\x1ffallback"))
            with self.assertRaisesRegex(LegacyDomainError, "unavailable"):
                domain.secret_present("api_key", "primary\x1fsecondary")
            with self.assertRaisesRegex(LegacyDomainError, "already in use"):
                domain.dispatch(
                    "provider.key_patch",
                    {"provider_id": "primary", "old_name": "fallback", "name": "default"},
                )
            self.assertNotIn("replace-me-secondary-secret", json.dumps(snapshot))

            domain.apply()
            reloaded = ProvidersModelsDomain(path).snapshot()["providers"][0]
            self.assertEqual(["default", "fallback"], reloaded["api_key_names"])
            self.assertEqual("fallback", reloaded["models"][0]["api_key_name"])

    def test_named_provider_key_secret_uses_the_existing_core_capability_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            core = CoreStore(domains=[ProvidersModelsDomain(path)])
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.key_add",
                    "payload": {"provider_id": "primary", "name": "secondary"},
                }
            )

            named_target = "primary\x1fsecondary"
            descriptor = core.secret_descriptor("providers_models", "api_key", named_target)
            self.assertEqual(named_target, descriptor["target"])
            self.assertFalse(descriptor["present"])
            result = core.stage_secret(
                "providers_models",
                "api_key",
                named_target,
                "replace-me-secondary-secret",
                revision=core.revision,
            )
            self.assertTrue(result["present"])
            self.assertNotIn("replace-me-secondary-secret", json.dumps(result))

            first_key = core.secret_descriptor("providers_models", "api_key", "primary")
            named_key = core.secret_descriptor("providers_models", "api_key", named_target)
            self.assertTrue(first_key["present"])
            self.assertTrue(named_key["present"])
            self.assertNotIn("replace-me-secondary-secret", json.dumps(core.snapshot()))

    def test_model_duplicate_uses_private_draft_and_rebuilds_deployment_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            snapshot = domain.dispatch(
                "model.duplicate",
                {"provider_id": "primary", "model_id": "00000071"},
            )
            models = snapshot["providers"][0]["models"]
            self.assertEqual(2, len(models))
            self.assertEqual("00000071", models[0]["deployment_id"])
            self.assertRegex(models[1]["deployment_id"], r"^[0-9a-f]{8}$")
            self.assertNotEqual(models[0]["deployment_id"], models[1]["deployment_id"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))

            private_models = domain.export(include_sensitive=True)["providers"][0]["models"]
            self.assertEqual("keep", private_models[1]["litellm_extra"]["future_param"])
            self.assertEqual("keep", private_models[1]["model_info_extra"]["future_info"])
            self.assertEqual("replace-me-secret", private_models[1]["api_key"])
            domain.apply()

            reloaded = ProvidersModelsDomain(path).snapshot()["providers"][0]["models"]
            self.assertEqual(2, len(reloaded))
            self.assertEqual(2, len({model["deployment_id"] for model in reloaded}))

    def test_fetch_models_and_probe_use_generic_openai_model_endpoint(self) -> None:
        requests: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append((self.path, self.headers.get("Authorization", "")))
                body = json.dumps(
                    {"object": "list", "data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"}]}
                ).encode("utf-8")
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
            config = PROVIDER_CONFIG.replace("https://example.test/v1", f"http://127.0.0.1:{port}/v1")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(textwrap.dedent(config).lstrip(), encoding="utf-8")
                domain = ProvidersModelsDomain(path)

                fetched = domain.dispatch("providers.fetch_models", {"provider_id": "primary"})[
                    "operation_summary"
                ]
                probed = domain.probe({"provider_id": "primary"})

            self.assertTrue(fetched["available"])
            self.assertEqual(["model-b", "model-a"], fetched["models"])
            self.assertEqual(["openai-models-v1"], fetched["protocols"])
            self.assertTrue(probed["ok"])
            self.assertEqual(["model-b", "model-a"], probed["models"])
            self.assertEqual(
                [("/v1/models", "Bearer replace-me-secret"), ("/v1/models", "Bearer replace-me-secret")],
                requests,
            )
            self.assertNotIn("replace-me-secret", json.dumps(fetched))
            self.assertNotIn("replace-me-secret", json.dumps(probed))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_fetch_models_uses_only_the_requested_named_api_key(self) -> None:
        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append(self.headers.get("Authorization", ""))
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
            config = PROVIDER_CONFIG.replace("https://example.test/v1", f"http://127.0.0.1:{port}/v1")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(textwrap.dedent(config).lstrip(), encoding="utf-8")
                domain = ProvidersModelsDomain(path)
                domain.dispatch(
                    "provider.patch",
                    {
                        "provider_id": "primary",
                        "changes": {
                            "api_keys": [
                                {"name": "default", "value": "replace-me-default-secret"},
                                {"name": "secondary", "value": "replace-me-secondary-secret"},
                            ]
                        },
                    },
                )

                fetched = domain.dispatch(
                    "providers.fetch_models",
                    {"provider_id": "primary", "api_key_name": "secondary"},
                )["operation_summary"]

                self.assertEqual("secondary", fetched["api_key_name"])
                self.assertEqual(["model-a"], fetched["models"])
                self.assertNotIn("replace-me-secondary-secret", json.dumps(fetched))
                with self.assertRaisesRegex(LegacyDomainError, "selected API key is unavailable"):
                    domain.dispatch(
                        "providers.fetch_models",
                        {"provider_id": "primary", "api_key_name": "missing"},
                    )

            self.assertEqual(["Bearer replace-me-secondary-secret"], requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class CodexSettingsDomainTests(unittest.TestCase):
    def test_sync_and_apply_preserve_unknown_toml_and_auth_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            config = home / "config.toml"
            auth = home / "auth.json"
            config.write_text('model = "default-chat"\npersonality = "pragmatic"\n\n[future]\nkeep = true\n', encoding="utf-8")
            auth.write_text(json.dumps({"OPENAI_API_KEY": "replace-me-secret", "future": {"keep": True}}) + "\n", encoding="utf-8")
            domain = CodexSettingsDomain(runtime, codex_home=home)

            self.assertNotIn("replace-me-secret", json.dumps(domain.snapshot()))
            domain.dispatch("patch", {"model_reasoning_effort": "high"})
            domain.apply()

            self.assertIn('[future]', config.read_text(encoding="utf-8"))
            self.assertIn('model_reasoning_effort = "high"', config.read_text(encoding="utf-8"))
            self.assertEqual({"keep": True}, json.loads(auth.read_text(encoding="utf-8"))["future"])
            self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(auth.stat().st_mode))

    def test_raw_editor_document_action_updates_only_the_selected_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "auth.json").write_text('{"future": true}\n', encoding="utf-8")
            domain = CodexSettingsDomain(runtime, codex_home=home)

            domain.dispatch("set_raw", {"document": "config", "text": 'personality = "pragmatic"\n'})
            exported = domain.export(include_sensitive=True)

            self.assertEqual('{"future": true}\n', exported["auth_text"])
            self.assertEqual('personality = "pragmatic"\n', exported["config_text"])


class RuntimeSettingsDomainTests(unittest.TestCase):
    def test_runtime_schema_loads_from_an_isolated_bundled_core_without_service_shells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "litellm_menu" / "core"
            package.mkdir(parents=True)
            (root / "litellm_menu" / "__init__.py").write_text("", encoding="utf-8")
            (package / "__init__.py").write_text("", encoding="utf-8")
            source_root = Path(__file__).resolve().parents[1]
            (package / "runtime_settings_schema.py").write_bytes(
                (source_root / "litellm_menu/core/runtime_settings_schema.py").read_bytes()
            )
            module_path = root / "runtime_settings_io.py"
            module_path.write_bytes((source_root / "runtime_settings_io.py").read_bytes())
            spec = importlib.util.spec_from_file_location("bundled_runtime_settings_io", module_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader if spec is not None else None)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(root)}, clear=False), mock.patch.object(
                sys, "path", [str(root), *list(sys.path)]
            ):
                assert spec is not None and spec.loader is not None
                try:
                    spec.loader.exec_module(module)
                finally:
                    sys.modules.pop(spec.name, None)

            loaded = module.load_specs()
            self.assertGreater(len(loaded), 20)
            self.assertEqual("4000", loaded["LITELLM_PORT"].default)

    def test_bool_auto_uses_checkbox_projection_and_auto_off_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            domain = RuntimeSettingsDomain(path)
            spec = RuntimeSettingSpec(
                key="EXAMPLE_AUTO_SETTING",
                kind="bool_auto",
                default="off",
                minimum=None,
                maximum=None,
                options=(),
            )
            domain.specs[spec.key] = spec
            domain._raw_values[spec.key] = "off"
            domain._draft_values[spec.key] = "off"

            setting = next(item for item in domain.snapshot()["settings"] if item["key"] == spec.key)
            self.assertEqual("toggle", setting["kind"])
            self.assertEqual("bool_auto", setting["storage_kind"])

            domain.dispatch("set_setting", {"key": spec.key, "value": True})
            self.assertEqual("auto", domain.export(include_sensitive=True)["values"][spec.key])
            domain.dispatch("set_setting", {"key": spec.key, "value": False})
            self.assertEqual("off", domain.export(include_sensitive=True)["values"][spec.key])

            domain.dispatch("set_setting", {"key": spec.key, "value": True})
            with mock.patch.object(domain, "reload", return_value=domain.snapshot()):
                domain.apply()

            self.assertIn(f"{spec.key}=auto", path.read_text(encoding="utf-8"))

    def test_runtime_schema_actions_write_private_atomic_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            domain = RuntimeSettingsDomain(path)
            self.assertGreater(len(domain.snapshot()["settings"]), 20)

            domain.dispatch("set_setting", {"key": "LITELLM_PORT", "value": "4100"})
            domain.dispatch("set_setting", {"key": "LITELLM_BROWSER_BILLING", "value": True})
            self.assertTrue(domain.validate()["valid"])
            domain.apply()

            saved = path.read_text(encoding="utf-8")
            self.assertIn("LITELLM_PORT=4100", saved)
            self.assertIn("LITELLM_BROWSER_BILLING=1", saved)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_retired_config_watch_values_load_once_and_are_removed_on_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            path.write_text(
                "LITELLM_PORT=4100\n"
                "LITELLM_CONFIG_WATCH_INTERVAL=5\n"
                "LITELLM_CONFIG_WATCH_SETTLE_INTERVAL=2\n",
                encoding="utf-8",
            )

            domain = RuntimeSettingsDomain(path)

            self.assertEqual(
                "4100",
                next(item for item in domain.snapshot()["settings"] if item["key"] == "LITELLM_PORT")["value"],
            )
            self.assertNotIn(
                "LITELLM_CONFIG_WATCH_INTERVAL",
                {item["key"] for item in domain.snapshot()["settings"]},
            )
            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertIn("LITELLM_PORT=4100", saved)
            self.assertNotIn("LITELLM_CONFIG_WATCH", saved)

    def test_runtime_secret_is_redacted_and_external_change_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            path.write_text(
                "LITELLM_MENU_VISION_BRIDGE_API_KEY=replace-me-secret\n",
                encoding="utf-8",
            )
            domain = RuntimeSettingsDomain(path)
            secret = next(
                item
                for item in domain.snapshot()["settings"]
                if item["key"] == "LITELLM_MENU_VISION_BRIDGE_API_KEY"
            )
            self.assertTrue(secret["retained"])
            self.assertFalse(secret["will_clear"])
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_VISION_BRIDGE_API_KEY", "value": "replace-me-secret"})
            self.assertNotIn("replace-me-secret", json.dumps(domain.snapshot()))
            domain.dispatch("clear_setting", {"key": "LITELLM_MENU_VISION_BRIDGE_API_KEY"})
            cleared = next(
                item
                for item in domain.snapshot()["settings"]
                if item["key"] == "LITELLM_MENU_VISION_BRIDGE_API_KEY"
            )
            self.assertTrue(cleared["retained"])
            self.assertTrue(cleared["will_clear"])
            path.write_text("LITELLM_PORT=4200\n", encoding="utf-8")

            with self.assertRaisesRegex(LegacyDomainError, "changed on disk"):
                domain.apply()


class WebDAVSettingsDomainTests(unittest.TestCase):
    def test_webdav_patch_apply_and_password_presence_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=False):
            root = Path(directory)
            settings = root / "webdav.json"
            enabled = root / "webdav.enabled"
            domain = WebDAVSettingsDomain(settings, enabled_path=enabled)
            domain.dispatch(
                "patch",
                {
                    "url": "https://example.test/webdav/",
                    "username": "example-user",
                    "password": "replace-me-secret",
                    "remote_name": "config.json",
                    "sync_interval": "15",
                    "timeout": "10",
                    "enabled": True,
                },
            )

            snapshot = domain.snapshot()
            self.assertTrue(snapshot["password_configured"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            domain.apply()

            saved = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual("replace-me-secret", saved["password"])
            self.assertEqual(15, saved["sync_interval_minutes"])
            self.assertEqual(10, saved["timeout_seconds"])
            self.assertTrue(enabled.exists())
            self.assertEqual(0o600, stat.S_IMODE(settings.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(enabled.stat().st_mode))

    def test_probe_uses_existing_webdav_client_without_echoing_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = WebDAVSettingsDomain(root / "webdav.json", enabled_path=root / "enabled")
            domain.dispatch("patch", {"url": "https://example.test/webdav/", "remote_name": "config.json"})
            with mock.patch("webdav.core.WebDAVClient.head", return_value=(200, {})), mock.patch(
                "webdav.core.WebDAVClient.try_mkcol"
            ):
                result = domain.probe()

            self.assertEqual({"ok": True, "protocols": ["webdav"], "detail": "WebDAV probe succeeded"}, result)


if __name__ == "__main__":
    unittest.main()

"""Focused coverage for the external client configuration file surface.

The settings pane edits the remaining desktop clients (pi, DeepSeek Harness,
DSH Desktop, opencode) as raw documents and needs to show where each file
lives.  These tests pin the two Core-owned boundaries that support it: the
read-only ``files`` listing and the staged raw-document editor for the
``clients`` domain.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from young_router.core import CoreIPCClient, CoreIPCServer, CoreStore
from young_router.core.domains.claude import ClaudeSettingsDomain
from young_router.core.domains.clients import ClientSettingsDomain
from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.protocol import validate_method_result


def _client_environment(directory: str) -> dict[str, str]:
    return {
        "PI_CODING_AGENT_DIR": str(Path(directory) / "pi"),
        "DSH_HOME": str(Path(directory) / "dsh"),
        "DSH_DESKTOP_HOME": str(Path(directory) / "dsh-desktop"),
        "OPENCODE_CONFIG_DIR": str(Path(directory) / "opencode-config"),
        "OPENCODE_DATA_DIR": str(Path(directory) / "opencode-data"),
        "CODEX_HOME": str(Path(directory) / "codex"),
    }


class ClientFilesDomainTests(unittest.TestCase):
    def test_client_files_lists_the_registered_documents_with_their_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, _client_environment(directory)):
                domain = ClientSettingsDomain()
            files = {row["document"]: row for row in domain.client_files()}

            self.assertEqual(
                {
                    "pi_settings",
                    "pi_models",
                    "pi_auth",
                    "dsh_settings",
                    "dsh_desktop_settings",
                    "opencode_config",
                    "opencode_auth",
                    "codex_agents",
                    "codex_model_catalog",
                },
                set(files),
            )
            self.assertEqual("settings.json", files["pi_settings"]["name"])
            self.assertEqual("json", files["pi_settings"]["language"])
            self.assertEqual(str(Path(directory) / "pi" / "settings.json"), files["pi_settings"]["path"])
            self.assertEqual("yaml", files["dsh_settings"]["language"])
            # DSH Desktop keeps Harness state under the app data directory,
            # never in the CLI's ``~/.dsh`` profile home.
            self.assertEqual(
                str(Path(directory) / "dsh-desktop" / "harness" / "settings.yaml"),
                files["dsh_desktop_settings"]["path"],
            )
            self.assertEqual("opencode.json", files["opencode_config"]["name"])
            # The model catalog the Codex pane's switch installs is listed with
            # the file name Core itself writes, under the same Codex home.
            self.assertEqual("model-catalog.json", files["codex_model_catalog"]["name"])
            self.assertEqual(
                str(Path(directory) / "codex" / "model-catalog.json"),
                files["codex_model_catalog"]["path"],
            )
            self.assertFalse(files["pi_settings"]["exists"])
            self.assertTrue(all(row["domain"] == "clients" for row in files.values()))
            # The listing is display-only: it never carries file text.
            self.assertTrue(all("text" not in row for row in domain.client_files()))

    def test_clients_domain_stages_applies_and_reloads_a_raw_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = _client_environment(directory)
            with mock.patch.dict(os.environ, environment):
                domain = ClientSettingsDomain()
                domain.dispatch("set_raw", {"document": "pi_models", "text": '{"providers": {}}\n'})
                self.assertNotEqual(domain.draft_state(), {"documents": domain._baseline})
                domain.apply()

                written = Path(environment["PI_CODING_AGENT_DIR"]) / "models.json"
                self.assertEqual('{"providers": {}}\n', written.read_text(encoding="utf-8"))
                self.assertTrue(domain.client_files()[1]["exists"])
                # Applying returns the domain to its clean baseline so the
                # pane never reports staged changes after a successful write.
                self.assertEqual(domain.draft_state(), {"documents": domain._baseline})

    def test_clients_domain_rejects_invalid_syntax_and_unregistered_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, _client_environment(directory)):
                domain = ClientSettingsDomain()
                self.assertTrue(domain.validate()["valid"])

                domain.dispatch("set_raw", {"document": "pi_auth", "text": "not json"})
                validation = domain.validate()
                self.assertFalse(validation["valid"])
                self.assertEqual("pi_auth", validation["issues"][0]["path"])
                self.assertIn("JSON", validation["issues"][0]["message"])
                with self.assertRaises(Exception):
                    domain.apply()

                # A YAML document only has to parse as YAML.
                domain.dispatch("set_raw", {"document": "pi_auth", "text": "{}"})
                domain.dispatch("set_raw", {"document": "dsh_settings", "text": "locale:\n  preference: en\n"})
                self.assertTrue(domain.validate()["valid"])
                domain.dispatch("set_raw", {"document": "dsh_settings", "text": "locale: [\n"})
                self.assertFalse(domain.validate()["valid"])

                with self.assertRaises(Exception):
                    domain.dispatch("set_raw", {"document": "unregistered", "text": "{}"})

    def test_clients_domain_reloads_an_externally_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = _client_environment(directory)
            with mock.patch.dict(os.environ, environment):
                domain = ClientSettingsDomain()
                target = Path(environment["DSH_HOME"]) / "settings.yaml"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("locale:\n  preference: en\n", encoding="utf-8")

                self.assertTrue(domain.external_disk_state()["changed"])
                domain.reload()
                self.assertFalse(domain.external_disk_state()["changed"])
                self.assertIn("locale", domain.raw_text("dsh_settings"))

    def test_files_ipc_result_matches_the_versioned_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = _client_environment(directory)
            with mock.patch.dict(os.environ, environment):
                clients = ClientSettingsDomain()
            core = CoreStore(
                domains=[
                    CodexSettingsDomain(Path(directory) / "runtime.yaml", codex_home=Path(directory) / "codex"),
                    ClaudeSettingsDomain(
                        Path(directory) / "claude.json",
                        desktop_config_library_path=Path(directory) / "claude-library",
                        developer_settings_path=Path(directory) / "claude" / "developer_settings.json",
                    ),
                    clients,
                ]
            )
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            result = client.call("files", {})
            validate_method_result("files", result)
            self.assertEqual(
                [
                    "config",
                    "auth",
                    "settings",
                    "desktop",
                    "developer",
                    "pi_settings",
                    "pi_models",
                    "pi_auth",
                    "dsh_settings",
                    "dsh_desktop_settings",
                    "opencode_config",
                    "opencode_auth",
                    "codex_agents",
                    "codex_model_catalog",
                ],
                [row["document"] for row in result["files"]],
            )
            self.assertEqual(
                [
                    "codex", "codex",
                    "claudeCode", "claudeDesktop", "claudeDesktop",
                    "pi", "pi", "pi",
                    "dsh", "dshDesktop", "opencode", "opencode",
                    "codex", "codex",
                ],
                [row["client"] for row in result["files"]],
            )
            self.assertEqual(
                {"codex": 2, "claude": 3, "clients": 9},
                {
                    name: sum(1 for row in result["files"] if row["domain"] == name)
                    for name in ("codex", "claude", "clients")
                },
            )
            self.assertEqual(str(Path(directory) / "codex" / "config.toml"), result["files"][0]["path"])
            self.assertFalse(result["files"][0]["exists"])

    def test_files_listing_spells_a_home_path_with_a_tilde(self) -> None:
        """The pane shows ``~`` instead of spelling out the account's home."""

        with tempfile.TemporaryDirectory() as directory:
            # The registered pi documents stay outside the home, while the
            # Codex home is addressed through the real user directory.
            codex_home = Path.home() / ".young-router-client-paths" / "codex"
            with mock.patch.dict(os.environ, _client_environment(directory)):
                clients = ClientSettingsDomain()
            core = CoreStore(
                domains=[
                    CodexSettingsDomain(Path(directory) / "runtime.yaml", codex_home=codex_home),
                    clients,
                ]
            )

            result = core.client_files()
            validate_method_result("files", result)
            rows = {row["document"]: row for row in result["files"]}
            self.assertEqual(str(codex_home / "config.toml"), rows["config"]["path"])
            self.assertEqual("~/.young-router-client-paths/codex/config.toml", rows["config"]["display_path"])
            # A registered file that is not under the home keeps its own path.
            self.assertEqual(str(Path(directory) / "pi" / "settings.json"), rows["pi_settings"]["display_path"])
            self.assertNotIn(
                str(Path.home()),
                json.dumps([row["display_path"] for row in result["files"]]),
            )

    def test_clients_editor_lease_stages_text_through_the_versioned_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = _client_environment(directory)
            with mock.patch.dict(os.environ, environment):
                clients = ClientSettingsDomain()
            core = CoreStore(domains=[clients])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            editor = client.call("editor", {"domain": "clients", "document": "opencode_auth"})
            self.assertEqual("clients", editor["domain"])
            self.assertEqual("opencode_auth", editor["document"])
            self.assertEqual("", editor["text"])

            staged = client.call(
                "editor",
                {"editor_token": editor["editor_token"], "text": '{"token": "synthetic"}\n'},
            )
            self.assertEqual('{"token": "synthetic"}\n', staged["text"])

            applied = client.call("apply", {"domains": ["clients"], "revision": staged["revision"]})
            self.assertEqual(["clients"], applied["domains"])
            self.assertEqual(
                '{"token": "synthetic"}\n',
                (Path(environment["OPENCODE_DATA_DIR"]) / "auth.json").read_text(encoding="utf-8"),
            )

            with self.assertRaises(Exception):
                client.call("editor", {"domain": "clients", "document": "unregistered"})


if __name__ == "__main__":
    unittest.main()

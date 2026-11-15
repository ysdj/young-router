from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from young_router.core import codex_config


class CodexConfigEdgeTests(unittest.TestCase):
    def test_structured_config_hides_reserved_builtin_provider_rows(self) -> None:
        config = codex_config.parse_toml_text(
            '[model_providers.openai]\nwire_api = "responses"\n\n'
            '[model_providers.amazon-bedrock]\nwire_api = "responses"\n\n'
            '[model_providers.relay]\nwire_api = "responses"\n'
        )
        rows = codex_config.structured_config(config, {})["providers"]
        self.assertEqual(["relay"], [row["id"] for row in rows])

    def test_amazon_bedrock_is_rejected_as_a_custom_provider_id(self) -> None:
        with self.assertRaisesRegex(codex_config.EditorError, "reserved built-in"):
            codex_config._apply_provider_patch(
                '[model_providers.relay]\nwire_api = "responses"\n',
                [{"id": "amazon-bedrock", "wire_api": "responses"}],
            )

    def test_editor_payload_uses_the_named_documents_recorded_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            config_path = runtime / "config.yaml"
            config_path.write_text(
                "general_settings:\n  master_key: sk-local\n", encoding="utf-8"
            )
            (runtime / codex_config.MANAGED_OWNER_FILENAME).write_text(
                json.dumps({"pid": os.getpid()}), encoding="utf-8"
            )
            (runtime / codex_config.MANAGED_PORT_FILENAME).write_text(
                "23457", encoding="utf-8"
            )
            with mock.patch.object(
                codex_config, "local_exposed_models", return_value=([], False)
            ), mock.patch.dict(os.environ, {"LITELLM_PORT": "4999"}, clear=False):
                payload = codex_config.editor_payload(
                    {}, {}, runtime_config_path=config_path
                )
        self.assertEqual("http://127.0.0.1:23457/v1", payload["local_base_url"])

    def test_local_exposed_models_resolves_the_named_documents_port(self) -> None:
        config_path = Path("runtime/config.yaml")
        opener = mock.Mock()
        opener.open.side_effect = OSError("offline")
        with mock.patch.object(
            codex_config, "local_base_url", return_value="http://127.0.0.1:4999/v1"
        ) as base, mock.patch.object(
            codex_config.urllib.request, "build_opener", return_value=opener
        ):
            self.assertEqual(
                ([], False), codex_config.local_exposed_models("sk", config_path)
            )
        base.assert_called_once_with(config_path)

    def test_installed_surface_feature_keys_are_memoized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            plugin = home / "plugins" / "cache" / "openai-bundled" / "browser" / "1.0.0"
            plugin.mkdir(parents=True)
            (plugin / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "cua_repl": {"env": {"CUA_REPL_ENABLED_SURFACES": "browser"}}
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(codex_config, "codex_home", return_value=home), \
                    mock.patch.object(codex_config.json, "loads", wraps=json.loads) as loads:
                first = codex_config._installed_surface_feature_keys()
                parses = loads.call_count
                second = codex_config._installed_surface_feature_keys()
                reparses = loads.call_count
        self.assertEqual(("browser_use",), first)
        self.assertEqual(first, second)
        self.assertEqual(parses, reparses)


if __name__ == "__main__":
    unittest.main()

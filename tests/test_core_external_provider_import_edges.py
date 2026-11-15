from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from young_router.core import external_provider_import


class ExternalProviderImportEdgeTests(unittest.TestCase):
    def document(self, text: str, suffix: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name) / f"providers{suffix}"
        source.write_text(text, encoding="utf-8")
        return source

    def test_dict_provider_models_take_the_surface_from_the_model_name(self) -> None:
        source = self.document(
            "providers:\n"
            "  relay:\n"
            "    base_url: https://relay.example.test/v1\n"
            "    api_key: sk-relay\n"
            "    models: [claude-sonnet-4]\n",
            ".yaml",
        )

        model = external_provider_import.import_explicit(source)["providers"][0]["models"][0]

        self.assertEqual("anthropic", model["upstream_url_surface"])
        self.assertEqual("anthropic/claude-sonnet-4", model["litellm_model"])

    def test_generic_models_take_the_surface_from_the_model_name(self) -> None:
        source = self.document(
            json.dumps(
                {
                    "data": [
                        {
                            "name": "generic",
                            "base_url": "https://generic.example.test/v1",
                            "key": "sk-generic",
                            "models": ["kimi-k2"],
                        }
                    ]
                }
            ),
            ".json",
        )

        model = external_provider_import.import_explicit(source)["providers"][0]["models"][0]

        self.assertEqual("openai/chat", model["upstream_url_surface"])
        self.assertEqual("openai/kimi-k2", model["litellm_model"])

    def test_cli_renders_the_imported_draft_once(self) -> None:
        source = self.document(
            "providers:\n"
            "  relay:\n"
            "    base_url: https://relay.example.test/v1\n"
            "    api_key: sk-relay\n"
            "    models: [relay-model]\n",
            ".yaml",
        )
        output = io.StringIO()
        with mock.patch.object(external_provider_import.json, "dumps", wraps=json.dumps) as dumps:
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, external_provider_import.main(["--input", str(source)]))

        self.assertEqual(1, dumps.call_count)
        self.assertEqual("relay-model", json.loads(output.getvalue())["providers"][0]["models"][0]["model_name"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from litellm_menu.core.native_codex_catalog import read_native_catalog


class NativeCodexCatalogTests(unittest.TestCase):
    def test_read_native_catalog_uses_bundled_debug_command_and_preserves_fields(self) -> None:
        calls: list[tuple[object, dict[str, object]]] = []
        source = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "multi_agent_version": "v5",
                    "future_native_field": {"delegation": True},
                }
            ]
        }

        def runner(command: object, **kwargs: object) -> SimpleNamespace:
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=json.dumps(source).encode("utf-8"))

        models = read_native_catalog("/native/codex", runner=runner)

        self.assertEqual(["/native/codex", "debug", "models", "--bundled"], calls[0][0])
        self.assertEqual(2.0, calls[0][1]["timeout"])
        self.assertEqual("v5", models[0]["multi_agent_version"])
        self.assertEqual({"delegation": True}, models[0]["future_native_field"])

        models[0]["future_native_field"]["delegation"] = False
        self.assertTrue(source["models"][0]["future_native_field"]["delegation"])

    def test_read_native_catalog_rejects_failed_or_invalid_commands(self) -> None:
        failed = read_native_catalog(
            "/native/codex",
            runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b"{}"),
        )
        invalid = read_native_catalog(
            "/native/codex",
            runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"not-json"),
        )

        self.assertEqual([], failed)
        self.assertEqual([], invalid)


if __name__ == "__main__":
    unittest.main()

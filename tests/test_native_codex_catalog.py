from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from young_router.core import native_codex_catalog
from young_router.core.native_codex_catalog import (
    codex_app_cli_candidates,
    read_native_catalog,
)


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

    def test_a_current_client_bundles_its_cli_under_the_codex_cli_package(self) -> None:
        """The app-layout candidates follow the package manifest, not a fixed name.

        A client update moved the bundled CLI from ``Resources/codex`` into the
        ``codex-cli`` package, whose manifest names the executable.  Missing that
        candidate left ``load_native_catalog`` empty, so the managed model
        catalog was never rewritten and a context-window change never reached
        the client.
        """

        with tempfile.TemporaryDirectory() as directory:
            resources = Path(directory) / "Resources"
            package = resources / "codex-cli"
            package.mkdir(parents=True)
            manifest = package / "codex-package.json"
            manifest.write_text(
                json.dumps({"layoutVersion": 1, "entrypoint": "bin/codex"}),
                encoding="utf-8",
            )
            self.assertEqual(
                (resources / "codex", package / "bin" / "codex"),
                codex_app_cli_candidates(resources),
            )
            # A missing, malformed, or escaping manifest falls back to the
            # entry point the package has always declared, never outside it.
            manifest.write_text("{", encoding="utf-8")
            self.assertEqual(package / "bin" / "codex", codex_app_cli_candidates(resources)[1])
            manifest.write_text(json.dumps({"entrypoint": "../../escape"}), encoding="utf-8")
            self.assertEqual(package / "bin" / "codex", codex_app_cli_candidates(resources)[1])

    def test_load_native_catalog_reads_the_package_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex-cli" / "bin" / "codex"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            seen: list[Path] = []
            original_candidates = native_codex_catalog.native_codex_executable_candidates
            original_read = native_codex_catalog.read_native_catalog
            native_codex_catalog.native_codex_executable_candidates = lambda: (executable,)

            def reader(path: Path) -> list[dict[str, object]]:
                seen.append(Path(path))
                return [{"slug": "gpt-6-astra"}]

            native_codex_catalog.read_native_catalog = reader
            try:
                models = native_codex_catalog.load_native_catalog()
            finally:
                native_codex_catalog.native_codex_executable_candidates = original_candidates
                native_codex_catalog.read_native_catalog = original_read
        self.assertEqual([executable], seen)
        self.assertEqual(["gpt-6-astra"], [model["slug"] for model in models])

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

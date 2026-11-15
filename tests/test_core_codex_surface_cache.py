"""The surface list a bundled plugin leaves to its cache is read from the cache.

A marketplace plugin's ``.mcp.json`` does not always state
``CUA_REPL_ENABLED_SURFACES``; the installed copy under
``<codex home>/plugins/cache/<marketplace>/<plugin>/<version>/`` does.  The
fallback built that path from the marketplace's first path part ("plugins")
instead of its name, so it looked in a directory that never exists and Browser /
Computer Use could not be enabled on such a runtime.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from young_router.core.domains import codex


class SurfaceCacheFallbackTests(unittest.TestCase):
    def _runtime(self, base: Path, *, cache_marketplace: str) -> tuple[Path, Path]:
        resources = base / "Resources"
        marketplace = resources / "plugins" / "openai-bundled"
        (marketplace / ".agents" / "plugins").mkdir(parents=True)
        (marketplace / ".agents" / "plugins" / "marketplace.json").write_text(
            json.dumps({"plugins": [{"name": "computer-use", "source": {"path": "plugins/computer-use"}}]}),
            encoding="utf-8",
        )
        plugin = marketplace / "plugins" / "computer-use"
        (plugin / "skills" / "computer").mkdir(parents=True)
        # The marketplace copy names the server and leaves the surfaces unstated.
        (plugin / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"cua_repl": {"env": {}}}}), encoding="utf-8"
        )
        home = base / "codex-home"
        installed = home / "plugins" / "cache" / cache_marketplace / "computer-use" / "1.0.0"
        installed.mkdir(parents=True)
        (installed / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"cua_repl": {"env": {"CUA_REPL_ENABLED_SURFACES": "computer"}}}}),
            encoding="utf-8",
        )
        return resources, home

    def test_the_cache_is_read_under_the_marketplace_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resources, home = self._runtime(Path(directory), cache_marketplace="openai-bundled")
            with mock.patch.object(codex, "_CODEX_APP_RESOURCES", (resources,)):
                features, runtime = codex._installed_surface_plugins(home)

        self.assertEqual({"computer_use": "computer-use@openai-bundled"}, features)
        self.assertEqual("computer-use@openai-bundled", runtime)

    def test_a_directory_named_after_the_first_path_part_is_not_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resources, home = self._runtime(Path(directory), cache_marketplace="plugins")
            with mock.patch.object(codex, "_CODEX_APP_RESOURCES", (resources,)):
                features, runtime = codex._installed_surface_plugins(home)

        self.assertEqual({}, features)
        self.assertIsNone(runtime)


if __name__ == "__main__":
    unittest.main()

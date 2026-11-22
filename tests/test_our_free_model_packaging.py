from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class OurFreeModelPackagingTests(unittest.TestCase):
    def test_core_pruners_trim_the_copied_release(self) -> None:
        development = (
            "docs/images/screenshot.png",
            "scripts/build-magpie-engine.mjs",
            "packages/standalone/web/index.html",
        )
        runtime = (
            "package.json",
            "LICENSE",
            "index.js",
            "src/core/runtime.js",
            "src/o2a/assets/ofm-agent-context.txt",
            "vendor/channel-pack/pack.js",
            "vendor/channel-pack/qoder-auth-wasm.wasm",
            "node_modules/@deepseek-ai/dsh-llm/index.js",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staged"
            for relative in (*development, *runtime):
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode())

            for platform in ("macos", "windows"):
                with self.subTest(platform=platform):
                    core = root / platform / "Core"
                    packaged = core / "young_router" / "adapters" / "our-free-model"
                    shutil.copytree(staging, packaged)
                    if platform == "macos":
                        (core / "runtime" / "site-packages").mkdir(parents=True)
                    else:
                        (core / "runtime" / "bin" / "Lib" / "site-packages").mkdir(parents=True)

                    subprocess.run(
                        [sys.executable, "-B", str(SCRIPTS / f"prune-{platform}-core.py"), str(core)],
                        check=True,
                    )

                    for relative in development:
                        self.assertFalse((packaged / relative).exists(), relative)
                        self.assertTrue((staging / relative).is_file(), relative)
                    for relative in runtime:
                        self.assertEqual((packaged / relative).read_bytes(), relative.encode())

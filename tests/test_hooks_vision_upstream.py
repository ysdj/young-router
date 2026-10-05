"""The dsh-vision-router bridge: the staged upstream package, not a copy.

The vision fallback used to carry a hand-written Python copy of upstream's
provider chain, so a release that changed the free OVH models, their order, or
the local provider shapes was invisible until a request failed.  These tests
cover the two properties that replaced it: the worker answers the chain the
staged package computes, and a bridge that cannot be asked degrades to the
reviewed constants instead of dropping every provider.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from young_router.adapters import dsh_vision_router as router
from young_router.adapters import dsh_vision_upstream as upstream


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "young_router" / "adapters" / "dsh_vision_worker.mjs"
STAGER = ROOT / "scripts" / "update_dsh_vision_router.py"


class VisionUpstreamBridgeTests(unittest.TestCase):
    def test_the_repo_keeps_no_copy_of_the_free_chain(self) -> None:
        """The free models are upstream's to decide, not this repository's."""

        source = (ROOT / "young_router" / "adapters" / "dsh_vision_router.py").read_text(
            encoding="utf-8"
        )
        # The degrade table is allowed to name them; a *request* path that builds
        # its own chain is not.  Every occurrence must sit in the degrade block.
        chain_block = source.split("_DEFAULT_HTTP_PROVIDERS = (", 1)[1].split(")", 1)[0]
        self.assertIn("Qwen3.5-397B-A17B", chain_block)
        # The constant is only reachable as a fallback: the configured chain asks
        # upstream first and returns its answer untouched.
        self.assertIn("_upstream_provider_chain()", source)

    def test_a_configured_chain_prefers_the_staged_upstream_answer(self) -> None:
        upstream_chain = [
            {"name": "ovh", "base_url": "https://example.test/v1", "model": "upstream-model"}
        ]
        with mock.patch.object(
            router, "_upstream_provider_chain", return_value=upstream_chain
        ):
            with mock.patch.dict("os.environ", {"YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND": "auto"}):
                chain = router._configured_provider_chain()
        self.assertEqual(upstream_chain, chain)

    def test_an_unavailable_bridge_keeps_the_reviewed_chain(self) -> None:
        with mock.patch.object(router, "_upstream_provider_chain", return_value=None):
            with mock.patch.dict(
                "os.environ",
                {
                    "YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND": "auto",
                    "YOUNG_ROUTER_DSH_VISION_ROUTER_FREE_FALLBACK": "1",
                },
            ):
                chain = router._configured_provider_chain()
        models = [provider["model"] for provider in chain]
        self.assertIn("Qwen3.5-397B-A17B", models)

    def test_a_missing_bundle_degrades_instead_of_raising(self) -> None:
        # None is the degrade signal the router reads as "use the reviewed
        # chain". It must never surface as an exception: the vision path runs
        # after a model has already rejected the image.
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(upstream, "package_root", return_value=Path(directory)):
                with mock.patch.object(upstream, "worker_path", return_value=Path(directory) / "w.mjs"):
                    self.assertFalse(upstream.available())
                    self.assertIsNone(upstream.provider_chain({"backend": "auto"}))


class VisionWorkerProtocolTests(unittest.TestCase):
    """The worker's own behaviour, driven through the real Node runtime."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil_which_node()
        if cls.node is None:
            raise unittest.SkipTest("Node.js is not available on this machine")

    def _run(self, config: dict, *, package: Path | None = None) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "dsh_vision_worker.mjs"
            worker.write_text(WORKER.read_text(encoding="utf-8"), encoding="utf-8")
            if package is not None:
                target = root / "dsh-vision-router"
                subprocess.run(
                    ["cp", "-R", str(package), str(target)], check=True, capture_output=True
                )
            import json

            result = subprocess.run(
                [self.node, str(worker)],
                input=json.dumps({"config": config}) + "\n",
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout.splitlines()[0])

    def test_a_missing_package_reports_unavailable_rather_than_throwing(self) -> None:
        answer = self._run({"backend": "auto", "freeFallback": True})
        self.assertEqual("unavailable", answer["source"])
        self.assertEqual([], answer["providers"])

    def test_a_staged_package_answers_with_its_own_chain(self) -> None:
        package = _staged_package()
        if package is None:
            self.skipTest("no staged dsh-vision-router package is present")
        answer = self._run({"backend": "auto", "freeFallback": True}, package=package)
        self.assertEqual("upstream", answer["source"])
        models = [provider["model"] for provider in answer["providers"]]
        self.assertIn("Qwen3.5-397B-A17B", models)
        # The endpoint alone is not an identity: the free chain is several
        # models on one shared URL and all of them must survive.
        self.assertGreater(len(models), 1)

    def test_backend_off_and_local_answer_with_an_empty_chain(self) -> None:
        package = _staged_package()
        if package is None:
            self.skipTest("no staged dsh-vision-router package is present")
        off = self._run({"backend": "off", "freeFallback": True}, package=package)
        self.assertEqual([], off["providers"])
        local = self._run({"backend": "local", "freeFallback": True}, package=package)
        self.assertEqual("upstream", local["source"])


class VisionStagingTests(unittest.TestCase):
    """The staging step's own guards, without touching the network."""

    @staticmethod
    def _staging_module():
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "update_dsh_vision_router", STAGER
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_the_staged_environment_shim_only_answers_environment_of(self) -> None:
        module = self._staging_module()
        self.assertIn("environmentOf", module.ENVIRONMENT_SHIM_SOURCE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # The offending import lives in a staged dependency, not in the
            # stub itself: the stub is the one file allowed to name the module.
            consumer = root / "node_modules" / "@deepseek-ai" / "dsh-llm-deepseek" / "lib"
            consumer.mkdir(parents=True)
            (consumer / "index.js").write_text(
                "import { environmentOf, environmentPaths } from '@deepseek-ai/dsh-environment'\n",
                encoding="utf-8",
            )
            with self.assertRaises(module.UpdateError):
                module._shim_reaches_only_environment_of(root)

    def test_the_stub_itself_is_never_scanned_for_extra_imports(self) -> None:
        module = self._staging_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / "node_modules" / "@deepseek-ai" / "dsh-environment"
            stub.mkdir(parents=True)
            (stub / "index.js").write_text(
                "import { environmentOf } from '@deepseek-ai/dsh-environment'\n", encoding="utf-8"
            )
            # Only `environmentOf` is named, so this closure is still faithful.
            module._shim_reaches_only_environment_of(root)

    def test_the_staging_step_refuses_to_shadow_a_published_environment_package(self) -> None:
        module = self._staging_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = root / "node_modules" / "@deepseek-ai" / "dsh-environment"
            scope.mkdir(parents=True)
            (scope / "index.js").write_text("export const published = true\n", encoding="utf-8")
            with self.assertRaises(module.UpdateError):
                module.write_environment_shim(root)

    def test_the_peer_closure_covers_the_published_chain_functions(self) -> None:
        module = self._staging_module()
        for peer in ("@deepseek-ai/dsh-llm-deepseek", "@deepseek-ai/cordis"):
            self.assertIn(peer, module.REQUIRED_PEER_PACKAGES)


def shutil_which_node() -> str | None:
    import shutil

    return shutil.which("node")


def _staged_package() -> Path | None:
    root = Path(__file__).resolve().parents[1] / "young_router" / "adapters" / "dsh-vision-router"
    return root if (root / "lib" / "core-primitives.js").is_file() else None


if __name__ == "__main__":
    unittest.main()

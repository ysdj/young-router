from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from young_router import container


class ContainerCommandTests(unittest.TestCase):
    def _environment(self, root: Path, *, host_key: Path | None = None) -> dict[str, str]:
        values = {
            "HOME": str(root),
            "YOUNG_ROUTER_HOME": str(root / "data"),
            "LITELLM_RUNTIME_ROOT": str(root / "data"),
            "LITELLM_CONFIG_FILE": str(root / "data" / "config.yaml"),
            "LITELLM_RUNTIME_CONFIG": str(root / "data" / ".litellm-runtime" / "served.yaml"),
            "YOUNG_ROUTER_HOST_PROFILE": "gateway",
        }
        if host_key is not None:
            values["YOUNG_ROUTER_HOST_PROXY_KEY_FILE"] = str(host_key)
        return values

    def test_first_start_creates_an_empty_source_and_keeps_a_separate_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yr-container-") as directory:
            root = Path(directory)
            values = self._environment(root)
            source = Path(values["LITELLM_CONFIG_FILE"])
            staged = Path(values["LITELLM_RUNTIME_CONFIG"])
            with mock.patch.dict(os.environ, values, clear=True), mock.patch(
                "young_router.core.__main__.run", return_value=0
            ) as run:
                self.assertEqual(0, container.main(["serve"]))
                text = source.read_text(encoding="utf-8")
                self.assertIn("providers: {}", text)
                self.assertIn("master_key: sk-", text)
                self.assertFalse(staged.exists())
                run.assert_called_once()

    def test_a_host_key_is_used_and_reported_as_the_api_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yr-container-") as directory:
            root = Path(directory)
            host_key = root / "gateway-master-key"
            host_key.write_text("sk-gateway-key\nignored\n", encoding="utf-8")
            values = self._environment(root, host_key=host_key)
            source = Path(values["LITELLM_CONFIG_FILE"])
            with mock.patch.dict(os.environ, values, clear=True), mock.patch(
                "young_router.core.__main__.run", return_value=0
            ):
                self.assertEqual(0, container.main(["serve"]))
                self.assertIn("master_key: sk-gateway-key", source.read_text(encoding="utf-8"))
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(0, container.main(["api-key"]))
                self.assertEqual("sk-gateway-key\n", output.getvalue())

    def test_a_missing_stated_host_key_stops_initialization(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yr-container-") as directory:
            root = Path(directory)
            values = self._environment(root, host_key=root / "missing-key")
            with mock.patch.dict(os.environ, values, clear=True):
                with self.assertRaises(SystemExit):
                    container.main(["serve"])
            self.assertFalse(Path(values["LITELLM_CONFIG_FILE"]).exists())


if __name__ == "__main__":
    unittest.main()

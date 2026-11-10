"""What one click costs the page, measured in a running browser.

The reported defect was not a crash and not a wrong value: a toggle in the
published page took seconds to take effect while the same control in the native
window was instant.  Nothing in this repository could fail on it.  The parity
suite reads source files, `tsc` passes, the build succeeds, and a screenshot of
the page after the wait looks exactly like a page that never waited -- the
defect lives in *what the page asked Core for between the press and the paint*,
and only a running page can answer that.

So this drives the real bundle in a real browser against a real Core: the
assertions are the request shapes a settled edit may cost (a plan read, a
dispatch, an apply -- never the whole projection), the bytes those answers may
carry, and the time the pressed control takes to show the new value.  The
measurement is repeated in `tests/test_browser_edit_path.mjs`, which is also
runnable by hand:

    node tests/test_browser_edit_path.mjs http://127.0.0.1:PORT/ui/ --latency 250 --downstream 125000

Against the page built from `a3039fe` that command reports a checkbox that
painted after 5305 ms having downloaded 551 KB.  A source-only assertion would
have called both builds correct.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from young_router.core import CoreIPCServer, CoreStore

ROOT = Path(__file__).resolve().parents[1]
BUILT_PAGE = ROOT / "rn" / "apps" / "web" / "dist"
DRIVER = Path(__file__).resolve().with_name("test_browser_edit_path.mjs")


def _provider_document() -> str:
    """A document shaped like an installation someone actually runs."""

    lines = ["providers:"]
    for provider in range(3):
        name = f"provider{provider}"
        lines.extend(
            [
                f"  {name}:",
                f'    api_base: "https://relay{provider}.example.test/v1"',
                "    api_keys:",
                f'      - name: key\n        value: "sk-replace-{provider}"',
            ]
        )
    lines.append("model_list:")
    for provider in range(3):
        for route in range(4):
            lines.extend(
                [
                    f"  - model_name: public-{provider}-{route}",
                    "    litellm_params:",
                    f"      model: openai/upstream-{provider}-{route}",
                    f'      api_base: "https://relay{provider}.example.test/v1"',
                    f'      api_key: "sk-replace-{provider}"',
                    "    model_info:",
                    f"      id: {provider:02x}{route:02x}abcd",
                    f"      provider: provider{provider}",
                    "      upstream_url_surface: openai/responses",
                ]
            )
    lines.extend(["general_settings:", "  master_key: sk-replace-master", ""])
    return "\n".join(lines)


def _browser() -> str | None:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)


@unittest.skipUnless(shutil.which("node"), "node is required to drive the browser")
@unittest.skipUnless(_browser(), "a Chrome or Chromium binary is required (set CHROME_PATH)")
class BrowserEditCostTests(unittest.TestCase):
    """One settled edit costs a plan read, a dispatch and an apply."""

    def setUp(self) -> None:
        if not (BUILT_PAGE / "index.html").exists():
            self.skipTest(f"no built page at {BUILT_PAGE}; run `pnpm run build:web` first")
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        root = Path(self._directory.name)
        self.config_path = root / ".litellm-runtime" / "config.yaml"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(_provider_document(), encoding="utf-8")
        self.settings_path = root / "runtime-settings.env"
        self.settings_path.write_text("", encoding="utf-8")
        codex_home = root / "codex"
        codex_home.mkdir()
        # The pane resolves the proxy's own listing and this installation's
        # runtime root from the environment; pin both so this fixture is the
        # installation under test and not whichever one the runner has.
        self._environment = {
            "YOUNG_ROUTER_HOME": str(root),
            "LITELLM_RUNTIME_ROOT": str(root),
            "YOUNG_ROUTER_RUNTIME_SETTINGS_FILE": str(self.settings_path),
            "CODEX_HOME": str(codex_home),
            "LITELLM_CONFIG_FILE": "",
        }
        self._saved = {key: os.environ.get(key) for key in self._environment}
        for key, value in self._environment.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        self.addCleanup(self._restore_environment)
        # The production domain set: the shell reads every domain's projection
        # and asks the plan for all of them, so a fixture with three domains is
        # a page drawing an error sentence, not a smaller installation.
        self.core = CoreStore.with_default_domains(
            config_path=self.config_path,
            codex_home=codex_home,
            runtime_settings_path=self.settings_path,
            runtime_root=root,
        )
        self.core.snapshot()
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.server.enable_web_host(BUILT_PAGE)

    def _restore_environment(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_one_settled_edit_asks_for_the_plan_not_the_projection(self) -> None:
        endpoint = self.server.endpoint
        url = f"http://{endpoint.address}:{endpoint.port}/ui/"
        completed = subprocess.run(
            ["node", str(DRIVER), url, "--json"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        payload = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "{}"
        try:
            measured = json.loads(payload)
        except ValueError:
            self.fail(f"the page driver answered no measurement:\n{completed.stdout}\n{completed.stderr}")
        measured["stderr"] = completed.stderr[-2000:]
        self.assertTrue(
            measured.get("ok"),
            "a settled edit did not meet the page's own budget:\n"
            + json.dumps(measured, indent=2),
        )
        edit = measured["measured"]
        # The shapes, not only the sizes: a projection read is the defect even
        # when compression makes it small.
        self.assertEqual(["dispatch", "staged", "apply"], [name for name in edit["methods"] if name != "disk_state"][:3])
        self.assertNotIn("snapshot", edit["methods"])
        self.assertLessEqual(edit["bytes"], 16 * 1024)
        self.assertIsNotNone(edit["paintedMs"])
        self.assertLessEqual(edit["paintedMs"], 400)
        self.assertIn("已保存", edit["strip"])


if __name__ == "__main__":
    unittest.main()

"""The page's write queue after one Core call that never comes back.

The reported defect was whole-page: every checkbox in the published page
stopped responding while the pane still read 就绪.  Nothing in this repository
could fail on it -- the queue is a `Promise` chain, the transport had no
deadline, and a `fetch` that is never answered neither resolves nor rejects.

Only a running page can show it, so the detector is `test_browser_hung_call.mjs`
against the built bundle: it hangs exactly one dispatch the way a lost
connection hangs (the request is open; only the page's own deadline may end
it), then asserts the two facts the defect failed -- the press is reported and
its echo withdrawn, and the next press still reaches Core.  Against the page
built before the deadline it reports `last strip: 就绪` and a press that never
arrives, which is what a user reported as "all the checkboxes are broken".
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
DRIVER = Path(__file__).resolve().with_name("test_browser_hung_call.mjs")


def _provider_document() -> str:
    """Two providers with routes, the shape the routes table needs."""

    lines = ["providers:"]
    for provider in range(2):
        lines.extend(
            [
                f"  provider{provider}:",
                f'    api_base: "https://relay{provider}.example.test/v1"',
                "    api_keys:",
                f'      - name: key\n        value: "sk-replace-{provider}"',
            ]
        )
    lines.append("model_list:")
    for provider in range(2):
        for route in range(2):
            lines.extend(
                [
                    f"  - model_name: public-{provider}-{route}",
                    "    litellm_params:",
                    f"      model: openai/upstream-{provider}-{route}",
                    f'      api_base: "https://relay{provider}.example.test/v1"',
                    f'      api_key: "sk-replace-{provider}"',
                    f"      order: {route + 1}",
                    "    model_info:",
                    f'      id: "{provider:x}{route:x}abcdef"',
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
class BrowserHungCallTests(unittest.TestCase):
    """One unanswered dispatch is reported and cannot wedge the next press."""

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
        # This installation is the fixture, not whichever one the runner has.
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

    def test_a_lost_answer_is_reported_and_the_queue_moves_on(self) -> None:
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
            "a lost Core answer still wedged the page's edits:\n" + json.dumps(measured, indent=2),
        )
        self.assertTrue(measured["measured"]["echoWithdrawn"])
        # The deadline is the page's own 20 s, and the next press is a press,
        # not a second wait: it must reach Core without another budget.
        self.assertGreaterEqual(measured["measured"]["timeoutShownMs"], 19_000)
        self.assertLess(measured["measured"]["nextPressMs"], 5_000)


if __name__ == "__main__":
    unittest.main()

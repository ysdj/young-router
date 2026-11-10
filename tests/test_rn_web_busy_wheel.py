"""A wait on the page is the app's wheel, drawn in the slot the caller reserved.

The defect was visual and host-specific: the 中转站关联 account row drew a 56 pt
box with a ring in it because React Native for Web's `ActivityIndicator` paints
a 20 pt system ring inside the 12 pt slot a button reserves for its busy icon.
The shared source looked right -- the container said `width: 12` -- so only a
browser can answer whether the wheel is that 12 pt.

`tests/test_rn_web_busy_wheel.mjs` builds a harness page from the shared
controls with the same React and the same `react-native` shim the published page
gets, renders a standalone wait beside the same button twice (working and idle),
and measures the layout boxes.  It is also runnable by hand:

    node tests/test_rn_web_busy_wheel.mjs
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).resolve().with_name("test_rn_web_busy_wheel.mjs")


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


@unittest.skipUnless(shutil.which("node"), "node is required to build and drive the page")
@unittest.skipUnless(_browser(), "a Chrome or Chromium binary is required (set CHROME_PATH)")
@unittest.skipUnless(
    (ROOT / "rn" / "apps" / "web" / "node_modules" / "react-native-web").exists()
    and (ROOT / "rn" / "node_modules" / "esbuild").exists(),
    "the web host's own dependencies are not installed",
)
class PageBusyWheelTests(unittest.TestCase):
    """The page's waiting wheel is 12 pt, turning in the app's own steps."""

    def test_a_wait_fits_the_slot_and_never_resizes_its_control(self) -> None:
        completed = subprocess.run(
            ["node", str(DRIVER), "--json"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        payload = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "{}"
        try:
            measured = json.loads(payload)
        except ValueError:
            self.fail(f"the wheel driver answered no measurement:\n{completed.stdout}\n{completed.stderr}")
        self.assertTrue(
            measured.get("ok"),
            "a wait on the page did not keep the app's wheel inside its slot:\n"
            + json.dumps(measured, indent=2),
        )
        wheel = measured["measured"]["wheel"]
        self.assertEqual({"w": 12, "h": 12}, wheel)
        # `AppKitBusySpinner`'s own frame, not a system ring: twelve spokes.
        self.assertEqual(12, measured["measured"]["drawnSpokes"])
        # Both controls are the same button, so a working one must be the box
        # its idle state had: an icon-only button's wheel takes the glyph's
        # place, it never becomes the reason a row is 8 pt taller.
        for busy, idle in (("busySymbol", "idleSymbol"), ("busyTitled", "idleTitled")):
            self.assertEqual(measured["measured"][idle], measured["measured"][busy])


if __name__ == "__main__":
    unittest.main()

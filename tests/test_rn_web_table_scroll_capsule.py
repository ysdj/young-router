"""A table's scroll capsule reaches both ends of the list it describes.

The defect was visual and web-only: a route table scrolled to its last row still
drew its 4 pt capsule one column header (30 pt) short of the frame's bottom, so
the list read as unfinished at the end, and the same thumb started inside the
header when the list was at the top.  The scroll itself reached the end -- the
capsule's geometry did not, because the frame it is drawn in is taller than the
scrolling list (the header is a sibling above the list, inside the horizontal
scroller).

`tests/test_rn_web_table_scroll_capsule.mjs` builds a harness page from the
shared `NativeTable` with the same React and the same `react-native` shim the
published page gets, mounts more rows than fit, and measures both ends of the
capsule against the list's own edges.  It is also runnable by hand:

    node tests/test_rn_web_table_scroll_capsule.mjs
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).resolve().with_name("test_rn_web_table_scroll_capsule.mjs")


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
class TableScrollCapsuleTests(unittest.TestCase):
    """The capsule is anchored to the list, not to the frame around it."""

    def test_the_capsule_follows_the_list_from_its_first_row_to_its_last(self) -> None:
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
            self.fail(f"the capsule driver answered no measurement:\n{completed.stdout}\n{completed.stderr}")
        self.assertTrue(
            measured.get("ok"),
            "a table's scroll capsule did not follow the list it draws:\n" + json.dumps(measured, indent=2),
        )
        at_top = measured["measured"]["atTop"]
        at_end = measured["measured"]["atEnd"]
        # The header is what the capsule used to be offset by, so it has to be
        # there for the measurement to mean anything.
        self.assertGreaterEqual(at_top["list"]["top"] - at_top["frame"]["top"], 20)
        # At the first row the thumb starts inside the list, not over the header.
        self.assertLessEqual(at_top["capsule"]["top"] - at_top["list"]["top"], 4)
        self.assertGreaterEqual(at_top["capsule"]["top"] - at_top["list"]["top"], 0)
        # At the last row it ends at the list's own bottom edge.
        self.assertLessEqual(at_end["list"]["bottom"] - at_end["capsule"]["bottom"], 4)
        self.assertGreaterEqual(at_end["list"]["bottom"] - at_end["capsule"]["bottom"], 0)
        # ... and the list really is at its end, so that gap is the capsule's.
        self.assertAlmostEqual(at_end["scrollTop"], at_end["maxScroll"], delta=1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted(
    path
    for path in (ROOT / "mac_menu" / "Sources").glob("*.swift")
    if path.name != "main.swift"
)


HARNESS = r'''
import Cocoa

func fail(_ message: String) -> Never {
    fputs(message + "\n", stderr)
    exit(1)
}

guard let codex = URL(string: "litellm-menu://open/codex-settings"),
      let providers = URL(string: "litellm-menu://open/providers-models"),
      let runtime = URL(string: "litellm-menu://open/runtime-settings"),
      let serviceLogs = URL(string: "litellm-menu://open/logs?tab=service"),
      let invalidHost = URL(string: "litellm-menu://other/logs"),
      let invalidRoute = URL(string: "litellm-menu://open/nope")
else {
    fail("Could not construct local window URLs")
}

guard AppDelegate.localWindowRoute(for: codex) == .codexSettings,
      AppDelegate.localWindowRoute(for: providers) == .providersModels,
      AppDelegate.localWindowRoute(for: runtime) == .runtimeSettings,
      AppDelegate.localWindowRoute(for: serviceLogs) == .logs,
      AppDelegate.localWindowRoute(for: invalidHost) == nil,
      AppDelegate.localWindowRoute(for: invalidRoute) == nil,
      AppDelegate.logTab(forLocalWindowURL: serviceLogs) == .service,
      AppDelegate.logTab(forLocalWindowURL: codex) == .requests
else {
    fail("Local window URL routing is incorrect")
}

print("local-window-url-routing-ok")
'''


@unittest.skipUnless(
    subprocess.run(["which", "swiftc"], capture_output=True).returncode == 0,
    "Local window URL routing requires swiftc.",
)
class AppOpenURLSwiftTests(unittest.TestCase):
    def test_local_window_url_routes_are_fixed_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source = temp / "main.swift"
            source.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            binary = temp / "local-window-url-routing"
            compiled = subprocess.run(
                [
                    "swiftc",
                    *(str(path) for path in SOURCES),
                    str(source),
                    "-o",
                    str(binary),
                    "-framework",
                    "Cocoa",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run(
                [str(binary)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "local-window-url-routing-ok")


if __name__ == "__main__":
    unittest.main()

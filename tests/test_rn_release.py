from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReactNativeReleaseTests(unittest.TestCase):
    def test_release_entry_uses_react_native_build_and_portable_core(self) -> None:
        script = (ROOT / "scripts" / "package-release.sh").read_text(encoding="utf-8")

        self.assertIn("pnpm run build:macos", script)
        self.assertIn("LITELLM_MENU_MACOS_OUTPUT", script)
        self.assertIn("runtime/bin/python", script)
        self.assertIn('test -x "$CORE/bin/vision_ocr"', script)
        self.assertIn("Core/bin/vision_ocr", script)
        self.assertIn('test -f "$CORE/sitecustomize.py"', script)
        self.assertIn("RELOCATED_CORE", script)
        self.assertIn("LITELLM_MENU_PROXY_PROCESS=1", script)
        self.assertIn("image_generation_routing_hook", script)
        self.assertIn("-m litellm_menu.core --help", script)
        self.assertIn("archive-list.txt", script)
        self.assertIn("Resources/Core/(\\.venv|venv)", script)
        self.assertNotIn("mac_menu/build.sh", script)
        self.assertNotRegex(script, r"(?m)^\s*(?:npm|npx)\b")

    def test_ci_builds_both_react_native_hosts(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("pnpm run build:macos", workflow)
        self.assertIn("pnpm run build:windows", workflow)
        self.assertIn("node scripts/bootstrap-rnmacos-085.mjs", workflow)
        self.assertIn('test -f "$APP/Contents/Resources/Core/sitecustomize.py"', workflow)
        self.assertIn("image_generation_routing_hook", workflow)
        self.assertNotIn("node-version: 20", workflow)
        self.assertGreaterEqual(workflow.count("node-version: 22"), 3)
        self.assertNotIn("mac_menu/build.sh", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s*(?:run:\s*)?(?:npm|npx)\b")

    def test_react_native_085_line_is_explicitly_pinned(self) -> None:
        package = json.loads((ROOT / "rn/package.json").read_text(encoding="utf-8"))
        vendor = json.loads(
            (ROOT / "rn/vendor/react-native-macos-0.85.json").read_text(encoding="utf-8")
        )

        self.assertEqual(package["engines"]["node"], ">=22")
        self.assertEqual(package["dependencies"]["react-native"], "0.85.3")
        self.assertEqual(
            package["dependencies"]["react-native-windows"],
            "0.85.0-preview.1",
        )
        self.assertEqual(
            package["devDependencies"]["@react-native-windows/codegen"],
            "0.85.0-preview.1",
        )
        self.assertEqual(vendor["ref"], "0.85-merge")
        self.assertRegex(vendor["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(vendor["hermes"]["compilerVersion"], "250829098.0.6")
        self.assertEqual(vendor["hermes"]["sourceTag"], "hermes-v250829098.0.6")
        self.assertEqual(
            vendor["hermes"]["sourceCommit"],
            "80149b7543d024f6e170434df7e0fd8de8f94aef",
        )

    def test_macos_build_bootstraps_and_verifies_the_pinned_source_vendor(self) -> None:
        script = (ROOT / "rn/scripts/build-macos.sh").read_text(encoding="utf-8")

        self.assertIn("bootstrap-rnmacos-085.mjs", script)
        self.assertIn("verify-rnmacos-085.mjs --check-build-env", script)
        for assignment in (
            "RCT_USE_RN_DEP=0",
            "RCT_USE_PREBUILT_RNCORE=0",
            "RCT_BUILD_HERMES_FROM_SOURCE=true",
            "RCT_HERMES_V1_ENABLED=1",
        ):
            self.assertIn(assignment, script)
        self.assertIn("vendor/react-native-macos-0.85/packages/react-native/cli.js", script)
        self.assertIn("runtime_settings_io.py", script)
        self.assertIn("sitecustomize.py", script)
        self.assertIn("VisionOCR.swift", script)
        self.assertIn("$CORE/bin/vision_ocr", script)
        self.assertIn("-framework Vision", script)
        self.assertIn("-target \"$ARCH-apple-macosx14.0\"", script)
        self.assertIn("xcrun --sdk macosx --find swiftc", script)
        self.assertIn("xcrun --sdk macosx --show-sdk-path", script)
        self.assertIn('-sdk "$MACOS_SDK"', script)
        self.assertIn("LITELLM_MENU_PROXY_PROCESS=1", script)
        self.assertIn("image_generation_routing_hook", script)
        self.assertNotIn('service/runtime_settings.sh', script)

        runtime_io = (ROOT / "runtime_settings_io.py").read_text(encoding="utf-8")
        runtime_schema = (ROOT / "litellm_menu/core/runtime_settings_schema.py").read_text(encoding="utf-8")
        self.assertIn("runtime_settings_metadata", runtime_io)
        self.assertNotIn(' / "service" / ', runtime_io)
        self.assertIn("RUNTIME_SETTINGS_SCHEMA", runtime_schema)
        self.assertNotIn("LITELLM_CONFIG_WATCH", runtime_schema)

        podfile = (ROOT / "rn/apps/macos/macos/Podfile").read_text(encoding="utf-8")
        self.assertIn("react-native-macos-0.85.json", podfile)
        self.assertIn("ENV['HERMES_COMMIT']", podfile)

    def test_windows_build_checks_085_codegen_before_msbuild(self) -> None:
        package = (ROOT / "rn/package.json").read_text(encoding="utf-8")
        script = (ROOT / "rn/scripts/build-windows.ps1").read_text(encoding="utf-8")

        self.assertIn('"codegen:windows:check"', package)
        self.assertIn("pnpm run codegen:windows:check", script)
        self.assertIn("RunCodegenWindows=false", script)
        self.assertIn('"sitecustomize.py"', script)
        self.assertIn("LITELLM_MENU_PROXY_PROCESS", script)
        self.assertIn("image_generation_routing_hook", script)

    def test_macos_metro_config_keeps_the_macos_bundle_platform(self) -> None:
        package = json.loads((ROOT / "rn/apps/macos/package.json").read_text(encoding="utf-8"))
        metro = (ROOT / "rn/apps/macos/metro.config.js").read_text(encoding="utf-8")

        self.assertEqual(package["dependencies"]["@babel/runtime"], "7.29.7")
        self.assertIn('platforms: ["ios", "macos", "android"]', metro)
        self.assertIn('require.resolve("@babel/runtime/package.json", { paths: [appRoot] })', metro)
        self.assertIn('"@babel/runtime": babelRuntimeRoot', metro)
        self.assertIn('require.resolve("react/package.json", { paths: [appRoot] })', metro)
        self.assertIn('react: reactRoot', metro)
        self.assertIn('"@react-native/normalize-colors": normalizeColorsRoot', metro)
        self.assertIn('"@react-native/assets-registry": assetsRegistryRoot', metro)
        self.assertIn('const workspaceDependencyStore = path.join(workspaceRoot, "node_modules/.pnpm")', metro)

    def test_macos_entry_initializes_react_native_before_registering_the_app(self) -> None:
        entry = (ROOT / "rn/apps/macos/index.js").read_text(encoding="utf-8")
        initialize_core = 'require("react-native/Libraries/ReactPrivate/ReactNativePrivateInitializeCore");'
        platform_entry = 'require("../../packages/shared/src/platformEntry");'

        self.assertIn(initialize_core, entry)
        self.assertIn(platform_entry, entry)
        self.assertLess(entry.index(initialize_core), entry.index(platform_entry))


if __name__ == "__main__":
    unittest.main()

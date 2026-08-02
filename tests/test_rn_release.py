from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReactNativeReleaseTests(unittest.TestCase):
    def test_local_test_gate_never_installs_and_explicit_install_restarts_live_app(self) -> None:
        test_script = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("LITELLM_MENU_AUTO_BUILD", test_script)
        self.assertNotIn("build-and-install-macos.sh", test_script)
        self.assertIn("LITELLM_MENU_MACOS_OUTPUT", installer)
        self.assertIn("/Applications/LiteLLM Menu.app", installer)
        self.assertIn("codesign --verify --deep --strict", installer)
        self.assertIn(".LiteLLMMenu.previous", installer)
        self.assertIn('INSTALL_COMPLETE=1', installer)
        self.assertIn('RESTART_ARMED=0', installer)
        self.assertIn('RESTART_ARMED=1', installer)
        self.assertIn('[[ -n "$(installed_pids)" ]] || open -n "$DESTINATION"', installer)
        self.assertNotIn('tell application id "menu.litellm.menu" to quit', installer)
        self.assertIn("stop_installed_app", installer)
        self.assertIn("pids_are_alive()", installer)
        self.assertIn('kill -TERM "$pid"', installer)
        self.assertIn('while pids_are_alive "$bundle_pids"; do', installer)
        self.assertIn('done <<<"$bundle_pids"', installer)
        self.assertIn('index(line, bundle "/Contents/") > 0', installer)
        self.assertIn('START_TIMEOUT_SECONDS="${LITELLM_MENU_START_TIMEOUT_SECONDS:-40}"', installer)
        self.assertIn('STOP_TIMEOUT_SECONDS="${LITELLM_MENU_STOP_TIMEOUT_SECONDS:-20}"', installer)
        self.assertIn('STOP_GRACE_POLLS=20', installer)
        self.assertIn('REQUIRED_HEALTH_CHECKS=3', installer)
        self.assertNotIn("preserved_proxy_port", installer)
        self.assertNotIn("refuse_running_install", installer)
        build_replacement = installer.index('LITELLM_MENU_MACOS_OUTPUT="$STAGED_APP"')
        self.assertIn('kill -KILL "$pid"', installer)
        self.assertLess(installer.index('kill -TERM "$pid"'), installer.index('kill -KILL "$pid"'))
        self.assertIn('start_installed_app "$OLD_PIDS"', installer)
        self.assertIn('curl --fail --silent --show-error --max-time 1', installer)
        self.assertIn('health/liveliness', installer)
        self.assertIn('stable_checks >= REQUIRED_HEALTH_CHECKS', installer)
        self.assertIn('restore_previous_app', installer)
        self.assertIn("copy_tree()", installer)
        self.assertIn('cp -ac "$source/." "$destination/"', installer)
        self.assertIn('copy_tree "$STAGED_APP" "$INSTALL_STAGE"', installer)
        self.assertIn('codesign --verify --deep --strict --verbose=2 "$INSTALL_STAGE"', installer)
        self.assertIn('open -n "$DESTINATION"', installer)
        self.assertIn('start_installed_app "$OLD_PIDS"', installer)
        self.assertNotIn('sleep 1', installer)
        self.assertNotIn('codesign --verify --deep --strict --verbose=2 "$DESTINATION"', installer)
        self.assertIn('INSTALLED_RUNTIME="$DESTINATION/Contents/Resources/Core/runtime"', installer)
        self.assertIn('export LITELLM_MENU_CORE_RUNTIME_SOURCE="$INSTALLED_RUNTIME"', installer)
        self.assertIn('"$INSTALLED_RUNTIME/LITELLM_VERSION"', installer)
        self.assertIn("/Applications/LiteLLM Menu.app/Contents/Resources/Core/runtime/bin/python}", test_script)

        staged_copy = installer.index('copy_tree "$STAGED_APP" "$INSTALL_STAGE"')
        select_installed_runtime = installer.index(
            'export LITELLM_MENU_CORE_RUNTIME_SOURCE="$INSTALLED_RUNTIME"'
        )
        stop_before_replace = installer.rindex("stop_installed_app")
        verify_install_stage = installer.index(
            'codesign --verify --deep --strict --verbose=2 "$INSTALL_STAGE"'
        )
        arm_restart = installer.index("RESTART_ARMED=1")
        start_replacement = installer.rindex('if ! start_installed_app "$OLD_PIDS"; then')
        delete_previous = installer.rindex('rm -rf "$PREVIOUS_APP"')
        mark_complete = installer.rindex("INSTALL_COMPLETE=1")
        self.assertLess(select_installed_runtime, build_replacement)
        self.assertLess(build_replacement, stop_before_replace)
        self.assertLess(staged_copy, stop_before_replace)
        self.assertLess(verify_install_stage, stop_before_replace)
        self.assertLess(arm_restart, stop_before_replace)
        self.assertLess(stop_before_replace, start_replacement)
        self.assertLess(start_replacement, delete_previous)
        self.assertLess(delete_previous, mark_complete)

    def test_release_entry_uses_react_native_build_and_portable_core(self) -> None:
        script = (ROOT / "scripts" / "package-release.sh").read_text(encoding="utf-8")

        self.assertIn("pnpm run build:macos", script)
        self.assertIn("LITELLM_MENU_MACOS_OUTPUT", script)
        self.assertIn("LITELLM_MENU_RESET_METRO_CACHE=1", script)
        self.assertIn("export LITELLM_LOCAL_MODEL_COST_MAP=true", script)
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
        self.assertIn("LITELLM_MENU_REFRESH_PODS", workflow)
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
        bootstrap = (ROOT / "rn/scripts/bootstrap-rnmacos-085.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("LITELLM_MENU_REFRESH_RN_VENDOR", bootstrap)
        self.assertIn("Reusing verified react-native-macos vendor dependencies.", bootstrap)
        self.assertIn("yarnRelease, 'install', '--immutable'", bootstrap)
        package = json.loads((ROOT / "rn/package.json").read_text(encoding="utf-8"))
        self.assertEqual(
            package["scripts"]["check:macos"],
            "pnpm run test && pnpm run contract-check && pnpm run typecheck",
        )
        self.assertIn("pnpm run check:macos &", script)
        self.assertNotIn("pnpm run build &", script)
        self.assertIn("STATIC_CHECKS_PID=$!", script)
        self.assertIn('wait "$STATIC_CHECKS_PID"', script)
        self.assertIn("LITELLM_MENU_REFRESH_PODS", script)
        self.assertIn("Reusing CocoaPods workspace", script)
        self.assertIn('! -d "$APP_ROOT/macos/Pods"', script)
        self.assertIn('! -d "$APP_ROOT/macos/LiteLLMMenu.xcworkspace"', script)
        self.assertLess(
            script.index("pnpm run check:macos &"),
            script.index('pod install --project-directory="$APP_ROOT/macos"'),
        )
        self.assertLess(
            script.index('pod install --project-directory="$APP_ROOT/macos"'),
            script.index('node "$RNMACOS_CLI" build-macos'),
        )
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
        self.assertIn("strip -x -S", script)
        self.assertIn("case \"$(file -b \"$binary\")\"", script)
        self.assertIn('codesign --force --sign - "$binary"', script)
        self.assertLess(
            script.index('strip -x -S "$binary"'),
            script.index('codesign --force --sign - "$binary"'),
        )
        self.assertLess(
            script.index('codesign --force --sign - "$binary"'),
            script.index('codesign --force --deep --sign - "$APP"'),
        )
        self.assertIn("-type d -name __pycache__ -prune", script)
        self.assertIn("-name '*.pyc' -o -name '*.pyo'", script)
        self.assertIn("PYTHONDONTWRITEBYTECODE=0", script)
        self.assertIn("from litellm import run_server", script)
        self.assertIn("The bundled Core startup bytecode could not be generated.", script)
        self.assertIn("export LITELLM_LOCAL_MODEL_COST_MAP=true", script)
        self.assertIn("copy_tree()", script)
        self.assertIn('cp -ac "$source/." "$destination/"', script)
        self.assertIn('copy_tree "$RUNTIME_SOURCE" "$CORE/runtime"', script)
        self.assertIn('copy_tree "$CORE" "$PORTABLE_SMOKE"', script)
        self.assertIn('copy_tree "$APP" "$STAGED_OUTPUT"', script)
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

        project = (
            ROOT / "rn/apps/macos/macos/LiteLLMMenu.xcodeproj/project.pbxproj"
        ).read_text(encoding="utf-8")
        release_target = project[
            project.index("5142015C2437B4B40078DB4F /* Release */"):project.index(
                "83CBBA201A601CBA00E9B192 /* Debug */"
            )
        ]
        self.assertIn("ONLY_ACTIVE_ARCH = YES;", release_target)
        bundle_wrapper = (ROOT / "rn/scripts/bundle-macos.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn('export CLI_PATH=\\"${PROJECT_DIR}/../../../scripts/bundle-macos.mjs\\"', project)
        self.assertNotIn("EXTRA_PACKAGER_ARGS", project)
        self.assertIn("process.env.LITELLM_MENU_RESET_METRO_CACHE === '1'", bundle_wrapper)
        self.assertIn("process.env.CI", bundle_wrapper)
        self.assertIn("arg !== '--reset-cache'", bundle_wrapper)
        self.assertIn("scripts/bundle.js", bundle_wrapper)
        self.assertIn("react-native-xcode.sh", project)

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

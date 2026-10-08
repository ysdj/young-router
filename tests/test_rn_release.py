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

        self.assertNotIn("YOUNG_ROUTER_AUTO_BUILD", test_script)
        self.assertNotIn("build-and-install-macos.sh", test_script)
        self.assertIn("YOUNG_ROUTER_MACOS_OUTPUT", installer)
        self.assertIn("/Applications/Young Router.app", installer)
        self.assertIn("codesign --verify --deep --strict", installer)
        self.assertIn(".YoungRouter.previous", installer)
        self.assertIn('INSTALL_COMPLETE=1', installer)
        self.assertIn('RESTART_ARMED=0', installer)
        self.assertIn('RESTART_ARMED=1', installer)
        self.assertIn('[[ -n "$(installed_pids)" ]] || open -g "$DESTINATION"', installer)
        self.assertNotIn("open -n", installer)
        self.assertNotIn('tell application id "young.router.app" to quit', installer)
        self.assertIn("stop_installed_app", installer)
        self.assertIn("pids_are_alive()", installer)
        self.assertIn('kill -TERM "$pid"', installer)
        self.assertIn('while pids_are_alive "$bundle_pids"; do', installer)
        self.assertIn('done <<<"$bundle_pids"', installer)
        self.assertIn('line ~ /\\/Young ?Router[^\\/]*\\.app\\/Contents\\/MacOS\\/YoungRouter$/', installer)
        self.assertIn('*/Young*Router*.app/Contents/MacOS/YoungRouter', installer)
        self.assertIn('START_TIMEOUT_SECONDS="${YOUNG_ROUTER_START_TIMEOUT_SECONDS:-70}"', installer)
        self.assertIn('STOP_TIMEOUT_SECONDS="${YOUNG_ROUTER_STOP_TIMEOUT_SECONDS:-20}"', installer)
        self.assertIn('STOP_GRACE_POLLS=20', installer)
        self.assertIn('REQUIRED_HEALTH_CHECKS=3', installer)
        self.assertIn('LAUNCH_RETRY_SECONDS=1', installer)
        self.assertNotIn("preserved_proxy_port", installer)
        self.assertNotIn("refuse_running_install", installer)
        build_replacement = installer.index('YOUNG_ROUTER_MACOS_OUTPUT="$STAGE_ROOT/Young Router.app"')
        self.assertIn('kill -KILL "$pid"', installer)
        self.assertLess(installer.index('kill -TERM "$pid"'), installer.index('kill -KILL "$pid"'))
        self.assertIn('start_installed_app "$OLD_PIDS"', installer)
        self.assertIn('curl --fail --silent --show-error --max-time 1', installer)
        self.assertIn('health/liveliness', installer)
        self.assertIn(' -m young_router.proxy.macos_proxy ', installer)
        self.assertIn('stable_checks >= REQUIRED_HEALTH_CHECKS', installer)
        self.assertIn('restore_previous_app', installer)
        self.assertIn("copy_bundle()", installer)
        self.assertIn('ditto --rsrc --extattr --acl "$source" "$destination"', installer)
        self.assertIn('copy_bundle "$STAGED_APP" "$INSTALL_STAGE"', installer)
        self.assertIn('codesign --verify --deep --strict --verbose=2 "$INSTALL_STAGE"', installer)
        self.assertIn('open -g "$DESTINATION" >/dev/null 2>&1 || true', installer)
        self.assertIn('if [[ -z "$candidate_pid" && $SECONDS -ge $next_launch_at ]]; then', installer)
        self.assertIn('start_installed_app "$OLD_PIDS"', installer)
        self.assertNotIn('sleep 1', installer)
        self.assertNotIn('codesign --verify --deep --strict --verbose=2 "$DESTINATION"', installer)
        self.assertIn('INSTALLED_RUNTIME="$DESTINATION/Contents/Resources/Core/runtime"', installer)
        self.assertIn('export YOUNG_ROUTER_CORE_RUNTIME_SOURCE="$INSTALLED_RUNTIME"', installer)
        self.assertIn('"$INSTALLED_RUNTIME/LITELLM_VERSION"', installer)
        self.assertIn("/Applications/Young Router.app/Contents/Resources/Core/runtime/bin/python}", test_script)
        self.assertIn("export PYTHONDONTWRITEBYTECODE=1", test_script)

        staged_copy = installer.index('copy_bundle "$STAGED_APP" "$INSTALL_STAGE"')
        select_installed_runtime = installer.index(
            'export YOUNG_ROUTER_CORE_RUNTIME_SOURCE="$INSTALLED_RUNTIME"'
        )
        replace_previous = installer.index('mv "$DESTINATION" "$PREVIOUS_APP"')
        install_replacement = installer.index('if ! mv "$INSTALL_STAGE" "$DESTINATION"; then')
        capture_old_pids = installer.index('OLD_PIDS="$(bundle_processes)"')
        stop_after_replace = installer.rindex("stop_installed_app")
        verify_install_stage = installer.index(
            'codesign --verify --deep --strict --verbose=2 "$INSTALL_STAGE"'
        )
        arm_restart = installer.index("RESTART_ARMED=1")
        start_replacement = installer.rindex('if ! start_installed_app "$OLD_PIDS"; then')
        delete_previous = installer.rindex('rm -rf "$PREVIOUS_APP"')
        mark_complete = installer.rindex("INSTALL_COMPLETE=1")
        native_build = installer.index("node scripts/bootstrap-rnmacos-085.mjs")
        self.assertLess(select_installed_runtime, native_build)
        self.assertLess(select_installed_runtime, replace_previous)
        self.assertLess(staged_copy, replace_previous)
        self.assertLess(verify_install_stage, replace_previous)
        self.assertLess(capture_old_pids, replace_previous)
        self.assertLess(replace_previous, install_replacement)
        self.assertLess(arm_restart, install_replacement)
        self.assertLess(arm_restart, stop_after_replace)
        self.assertLess(stop_after_replace, start_replacement)
        self.assertIn('if ! stop_installed_app "$OLD_PIDS"; then', installer)
        self.assertIn('REMAINING_OLD_PIDS="$(bundle_processes)"', installer)
        self.assertIn('if [[ -n "$REMAINING_OLD_PIDS" ]] && ! stop_installed_app "$REMAINING_OLD_PIDS"; then', installer)
        self.assertIn("The old Young Router app did not stop; restoring the previous bundle.", installer)
        self.assertLess(start_replacement, delete_previous)
        self.assertLess(delete_previous, mark_complete)

    def test_macos_builds_and_installs_invalidate_finder_icon_cache(self) -> None:
        build = (ROOT / "scripts/build-and-install-macos.sh").read_text(
            encoding="utf-8"
        )
        installer = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(
            encoding="utf-8"
        )

        select_final_artifact = build.index('APP="$OUTPUT"')
        touch_final_artifact = build.index('touch "$APP"')
        print_final_artifact = build.index("printf '%s\\n' \"$APP\"")
        self.assertLess(select_final_artifact, touch_final_artifact)
        self.assertLess(touch_final_artifact, print_final_artifact)

        install_replacement = installer.index('if ! mv "$INSTALL_STAGE" "$DESTINATION"; then')
        refresh_installed_icon = installer.index("if ! refresh_installed_app_icon; then")
        stop_previous_app = installer.index('if ! stop_installed_app "$OLD_PIDS"; then')
        self.assertIn('touch "$DESTINATION"', installer)
        self.assertIn('"$LSREGISTER" -f "$DESTINATION"', installer)
        self.assertLess(install_replacement, refresh_installed_icon)
        self.assertLess(refresh_installed_icon, stop_previous_app)

    def test_release_entry_uses_react_native_build_and_portable_core(self) -> None:
        script = (ROOT / "scripts" / "package-release.sh").read_text(encoding="utf-8")

        self.assertIn("pnpm run build:macos", script)
        self.assertIn("YOUNG_ROUTER_MACOS_OUTPUT", script)
        self.assertIn("YOUNG_ROUTER_RESET_METRO_CACHE=1", script)
        self.assertIn("export LITELLM_LOCAL_MODEL_COST_MAP=true", script)
        self.assertIn("runtime/bin/python", script)
        self.assertIn('test -x "$CORE/bin/vision_ocr"', script)
        self.assertIn("Core/bin/vision_ocr", script)
        self.assertIn('test -f "$CORE/sitecustomize.py"', script)
        self.assertIn("RELOCATED_CORE", script)
        self.assertIn("YOUNG_ROUTER_PROXY_PROCESS=1", script)
        self.assertIn("image_generation_routing_hook", script)
        self.assertIn("-m young_router.core --help", script)
        self.assertIn("archive-list.txt", script)
        self.assertIn("Resources/Core/(\\.venv|venv)", script)
        self.assertNotIn("mac_menu/build.sh", script)
        self.assertNotRegex(script, r"(?m)^\s*(?:npm|npx)\b")

    def test_every_artifact_build_updates_litellm_before_packaging(self) -> None:
        installer = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(
            encoding="utf-8"
        )
        macos = installer
        windows = (ROOT / "rn" / "scripts" / "build-windows.ps1").read_text(
            encoding="utf-8"
        )
        release = (ROOT / "scripts" / "package-release.sh").read_text(
            encoding="utf-8"
        )

        installer_update = installer.index('"$PROJECT_ROOT/scripts/update-litellm.sh"')
        self.assertLess(installer_update, installer.index('INSTALLED_RUNTIME="$DESTINATION'))
        # The one script names its build output in the header, so the ordering
        # that matters is against the native build it must precede.
        self.assertLess(
            installer_update,
            installer.index("node scripts/bootstrap-rnmacos-085.mjs"),
        )

        self.assertLess(installer_update, installer.index("node scripts/bootstrap-rnmacos-085.mjs"))
        self.assertLess(installer_update, installer.index("build-macos --project-path macos"))

        windows_update = windows.index('scripts\\update_litellm.py')
        self.assertLess(windows_update, windows.index("pnpm run build"))
        self.assertLess(windows_update, windows.index("uv pip install"))

        self.assertIn("pnpm run build:macos", release)

    def test_ci_builds_both_react_native_hosts(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("pnpm run build:macos", workflow)
        self.assertIn("pnpm run build:windows", workflow)
        self.assertIn("YOUNG_ROUTER_REFRESH_PODS", workflow)
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
        script = (ROOT / "scripts/build-and-install-macos.sh").read_text(encoding="utf-8")

        self.assertIn("bootstrap-rnmacos-085.mjs", script)
        self.assertIn("verify-rnmacos-085.mjs --check-build-env", script)
        bootstrap = (ROOT / "rn/scripts/bootstrap-rnmacos-085.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("YOUNG_ROUTER_REFRESH_RN_VENDOR", bootstrap)
        self.assertIn("Reusing verified react-native-macos vendor dependencies.", bootstrap)
        self.assertIn("yarnRelease, 'install', '--immutable'", bootstrap)
        package = json.loads((ROOT / "rn/package.json").read_text(encoding="utf-8"))
        self.assertEqual(
            package["scripts"]["check:macos"],
            "pnpm run test && pnpm run contract-check && pnpm run typecheck",
        )
        self.assertIn("pnpm run check:macos &", script)
        self.assertIn('"$PROJECT_ROOT/scripts/update-litellm.sh"', script)
        self.assertNotIn("pnpm run build &", script)
        self.assertIn("STATIC_CHECKS_PID=$!", script)
        self.assertIn('wait "$STATIC_CHECKS_PID"', script)
        self.assertIn("YOUNG_ROUTER_REFRESH_PODS", script)
        self.assertIn("Reusing CocoaPods workspace", script)
        self.assertIn('! -d "$APP_ROOT/macos/Pods"', script)
        self.assertIn('! -d "$APP_ROOT/macos/YoungRouter.xcworkspace"', script)
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
        self.assertIn("young_router", script)
        self.assertIn("sitecustomize.py", script)
        self.assertIn("VisionOCR.swift", script)
        self.assertIn("$CORE/bin/vision_ocr", script)
        self.assertIn("-framework Vision", script)
        self.assertIn("-target \"$ARCH-apple-macosx14.0\"", script)
        self.assertIn("xcrun --sdk macosx --find swiftc", script)
        self.assertIn("xcrun --sdk macosx --show-sdk-path", script)
        self.assertIn('-sdk "$MACOS_SDK"', script)
        self.assertIn("YOUNG_ROUTER_PROXY_PROCESS=1", script)
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
        final_cache_cleanup = script.rindex('find "$CORE"')
        self.assertGreater(final_cache_cleanup, script.index('copy_tree "$CORE" "$PORTABLE_SMOKE"'))
        self.assertLess(final_cache_cleanup, script.index('codesign --force --deep --sign - "$APP"'))
        self.assertIn("export LITELLM_LOCAL_MODEL_COST_MAP=true", script)
        self.assertIn("copy_tree()", script)
        self.assertIn('cp -ac "$source/." "$destination/"', script)
        self.assertIn('copy_tree "$RUNTIME_SOURCE" "$CORE/runtime"', script)
        self.assertIn('copy_tree "$CORE" "$PORTABLE_SMOKE"', script)
        self.assertIn('copy_tree "$APP" "$STAGED_OUTPUT"', script)
        self.assertNotIn('service/runtime_settings.sh', script)

        runtime_io = (ROOT / "young_router/core/runtime_settings_io.py").read_text(encoding="utf-8")
        runtime_schema = (ROOT / "young_router/core/runtime_settings_schema.py").read_text(encoding="utf-8")
        self.assertIn("runtime_settings_metadata", runtime_io)
        self.assertNotIn(' / "service" / ', runtime_io)
        self.assertIn("RUNTIME_SETTINGS_SCHEMA", runtime_schema)
        self.assertNotIn("LITELLM_CONFIG_WATCH", runtime_schema)

        podfile = (ROOT / "rn/apps/macos/macos/Podfile").read_text(encoding="utf-8")
        self.assertIn("react-native-macos-0.85.json", podfile)
        self.assertIn("ENV['HERMES_COMMIT']", podfile)

        project = (
            ROOT / "rn/apps/macos/macos/YoungRouter.xcodeproj/project.pbxproj"
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
        self.assertIn("process.env.YOUNG_ROUTER_RESET_METRO_CACHE === '1'", bundle_wrapper)
        self.assertIn("process.env.CI", bundle_wrapper)
        self.assertIn("arg !== '--reset-cache'", bundle_wrapper)
        self.assertIn("scripts/bundle.js", bundle_wrapper)
        self.assertIn("react-native-xcode.sh", project)

    def test_one_macos_build_script_serves_build_only_and_install(self) -> None:
        # Build-only and install used to be two files, and the wrapper re-ran
        # work the inner script had already done: the LiteLLM sync, the runtime
        # choice, and the artifact validation all happened twice, and the second
        # one re-validated an artifact it had not produced. One file serves both
        # because the install step consumes exactly the bundle this build made.
        self.assertFalse(
            (ROOT / "rn" / "scripts" / "build-macos.sh").exists(),
            "the separate build script must not come back",
        )
        script = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(encoding="utf-8")

        # Exactly one sync of the pinned release, and it precedes everything
        # that reads the version it produces - including the decision to reuse
        # the installed runtime, which compares against that version.
        self.assertEqual(1, script.count('"$PROJECT_ROOT/scripts/update-litellm.sh"'))
        self.assertLess(
            script.index('"$PROJECT_ROOT/scripts/update-litellm.sh"'),
            script.index('INSTALLED_RUNTIME="$DESTINATION'),
        )

        # Build-only is a named output, not a separate entry point. Installing
        # stages to its own product so the DerivedData app stays incremental.
        self.assertIn('if [[ -n "${YOUNG_ROUTER_MACOS_OUTPUT:-}" ]]; then\n  BUILD_ONLY=1', script)
        self.assertIn('if [[ "$BUILD_ONLY" == "1" ]]; then', script)
        self.assertIn('YOUNG_ROUTER_MACOS_OUTPUT="$STAGE_ROOT/Young Router.app"', script)

        # Exactly one package install reaches the workspace, for the callers
        # that invoke this script directly.
        self.assertEqual(1, script.count("pnpm install --frozen-lockfile"))

        # The named output is handed back untouched: nothing installed, no app
        # restarted. The install half is behind the BUILD_ONLY exit.
        build_only_exit = script.index('if [[ "$BUILD_ONLY" == "1" ]]; then')
        self.assertLess(build_only_exit, script.index('mv "$DESTINATION" "$PREVIOUS_APP"'))
        # The call, not the definition: the install helpers are defined above
        # the exit so the install half can use them.
        self.assertLess(build_only_exit, script.index('if ! start_installed_app "$OLD_PIDS"; then'))

    def test_the_portable_smoke_relocates_instead_of_copying_a_gigabyte(self) -> None:
        # The smoke exists to prove the bundle works from somewhere other than
        # where it was built, which is what the installed app does. Copying the
        # Core aside to prove that cost eight to ten seconds and, worse, left
        # the freshly generated bytecode behind: the copied tree re-imported
        # cold and paid the interpreter's full start-up again. A rename within
        # the build volume is instantaneous and keeps the bytecode in place.
        script = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(encoding="utf-8")

        self.assertIn("relocate_core()", script)
        self.assertIn("restore_core()", script)
        self.assertIn('mv "$CORE" "$PORTABLE_SMOKE"', script)
        self.assertIn('mv "$PORTABLE_SMOKE" "$CORE"', script)
        # A failed import must not leave the built app without its Core.
        self.assertIn("trap 'restore_core; cleanup' EXIT", script)
        # The old form copied the whole Core aside on every build; the copy now
        # exists only as the cross-volume fallback.
        self.assertNotIn('PORTABLE_SMOKE="$RUNTIME_WORK/portable-core"', script)
        self.assertIn('PORTABLE_SMOKE="$(dirname "$CORE")/.young-router-portable-smoke"', script)

    def test_naming_an_output_moves_the_verified_bundle_rather_than_copying_it(self) -> None:
        # The named-output step put the verified bundle at the caller's path by
        # copying it aside and renaming. A rename on one volume moves a
        # gigabyte in constant time and preserves every nested signature the
        # same way a copy does, so the copy is only the cross-volume fallback.
        script = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(encoding="utf-8")

        self.assertIn('mv "$APP" "$STAGED_OUTPUT" 2>/dev/null', script)
        self.assertIn('copy_tree "$APP" "$STAGED_OUTPUT"', script)
        rename = script.index('mv "$APP" "$STAGED_OUTPUT" 2>/dev/null')
        fallback = script.index('copy_tree "$APP" "$STAGED_OUTPUT"')
        self.assertLess(rename, fallback, "the rename must be attempted first")
        # Both are guarded by a same-volume check against the destination.
        self.assertIn('"$(stat -f %d "$APP" 2>/dev/null || echo a)" == "$(stat -f %d "$OUT_DIR" 2>/dev/null || echo b)"', script)

    def test_no_empty_array_is_expanded_under_set_u(self) -> None:
        # macOS ships bash 3.2, where `set -u` treats "${arr[@]}" on an empty
        # array as an unbound variable. That aborts the command it is part of,
        # and for a backgrounded job the failure is easy to miss: the build
        # failed late with a confusing missing-file error from a later step.
        script = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(encoding="utf-8")

        self.assertIn("set -euo pipefail", script)
        # A guarded expansion is what an array that can be empty must use.
        self.assertIn('if [[ ${#NODE_SOURCE_ARGS[@]} -gt 0 ]]; then', script)
        self.assertIn('PI_WEB_ACCESS_NODE_ARGS+=("${NODE_SOURCE_ARGS[@]}")', script)
        # The unguarded form must not appear for that array.
        self.assertNotIn('"${NODE_SOURCE_ARGS[@]}" &', script)
        # STAGING_PIDS is appended once per staging job before it is ever
        # expanded, so it cannot be empty at the point of use.
        self.assertEqual(4, script.count('STAGING_PIDS+=("$!")'))

    def test_staging_caches_are_persistent_and_gated_by_the_staging_scripts(self) -> None:

        # Staging and the native build are disjoint, so a build may overlap them
        # (macOS) or run them first (Windows) -- but the staged trees must live
        # somewhere that survives the build.  A per-build temporary directory
        # makes every build reinstall releases it staged a moment ago, which was
        # the largest remaining cost of a local build once xcodebuild was warm.
        macos = (ROOT / "scripts/build-and-install-macos.sh").read_text(encoding="utf-8")
        windows = (ROOT / "rn/scripts/build-windows.ps1").read_text(encoding="utf-8")

        self.assertIn('STAGING_CACHE="${YOUNG_ROUTER_STAGING_CACHE:-$RN_ROOT/.staging-cache}"', macos)
        self.assertIn('$StagingCache = if ($env:YOUNG_ROUTER_STAGING_CACHE)', windows)
        self.assertIn('Join-Path $RnRoot ".staging-cache"', windows)

        # Every staged integration resolves into the cache, and none of them
        # still points at the throwaway per-build tree.
        for artifact in ("pi-web-access", "workbuddy-connect", "veridrop", "dsh-vision-router"):
            self.assertIn(f'"$STAGING_CACHE/{artifact}"', macos)
            self.assertIn(f'Join-Path $StagingCache "{artifact}"', windows)
        self.assertNotIn('WORKBUDDY_CONNECT_WORK="$RUNTIME_WORK/', macos)
        self.assertNotIn('VERIDROP_WORK="$RUNTIME_WORK/', macos)
        self.assertNotIn('DSH_VISION_ROUTER_WORK="$RUNTIME_WORK/', macos)
        self.assertNotIn('PI_WEB_ACCESS_PACKAGE_WORK="$RUNTIME_WORK/', macos)
        self.assertNotIn('Join-Path ([System.IO.Path]::GetTempPath()) ("young-router-veridrop-" + [guid]', windows)

        # The cache is build output, never committed.
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("rn/.staging-cache/", ignored)

        # Reuse is decided by the staging scripts (which own the upstream
        # lookup), not by the build scripts trusting a directory that happens
        # to exist.  Both scripts still invoke every updater on every build.
        for updater in (
            "update_pi_web_access.py",
            "update_workbuddy_connect.py",
            "update_veridrop.py",
            "update_dsh_vision_router.py",
        ):
            self.assertIn(updater, macos)
            self.assertIn(updater, windows)

    def test_the_generated_code_editor_bundle_is_not_rewritten_when_unchanged(self) -> None:
        # This file is a source input to the type check and to Metro, so
        # republishing identical bytes still moves its mtime and makes every
        # downstream cache treat an unchanged editor as a change.
        builder = (ROOT / "rn/scripts/build-code-editor.mjs").read_text(encoding="utf-8")

        self.assertIn("if (existing !== contents)", builder)
        self.assertIn("await readFile(output, \"utf8\")", builder)
        self.assertLess(
            builder.index("if (existing !== contents)"),
            builder.index("await rm(temporaryBundle"),
        )

    def test_windows_build_checks_085_codegen_before_msbuild(self) -> None:
        package = (ROOT / "rn/package.json").read_text(encoding="utf-8")
        script = (ROOT / "rn/scripts/build-windows.ps1").read_text(encoding="utf-8")

        self.assertIn('"codegen:windows:check"', package)
        self.assertEqual(1, package.count("pnpm run codegen:windows:check"))
        self.assertNotIn("pnpm run codegen:windows:check", script)
        self.assertIn("scripts\\update_litellm.py", script)
        self.assertIn("RunCodegenWindows=false", script)
        self.assertIn('"sitecustomize.py"', script)
        self.assertIn("YOUNG_ROUTER_PROXY_PROCESS", script)
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

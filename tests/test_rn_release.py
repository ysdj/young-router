from __future__ import annotations

import json
import runpy
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReactNativeReleaseTests(unittest.TestCase):
    def read_release_build_script(self) -> str:
        return (ROOT / "scripts" / "build-and-install-macos.sh").read_text(
            encoding="utf-8"
        )

    def test_install_failure_restores_the_previous_app(self) -> None:
        script = self.read_release_build_script()
        wrapper = script.index('wait_static_checks() {')
        cleanup = script.index('cleanup() {')
        assertions = script.index('mv "$RETIRED_HOST" "$HOST_EXECUTABLE"', cleanup)
        app_restore = script.index(
            'if [[ "$INSTALL_COMPLETE" != "1" && "$PREVIOUS_APP_MOVED" == "1"',
            assertions,
        )
        cleanup_ends = script.index('trap cleanup EXIT', app_restore)
        self.assertNotIn('return', script[assertions:cleanup_ends])
        self.assertIn('mv "$PREVIOUS_APP" "$DESTINATION" || true', script[app_restore:cleanup_ends])
        self.assertIn('mv "$DESTINATION" "$FAILED_APP" || true', script[app_restore:cleanup_ends])
        self.assertLess(assertions, app_restore)

    def test_macos_core_pruning_keeps_litellm_proxy_mcp_imports(self) -> None:
        prune_module = runpy.run_path(str(ROOT / "scripts" / "prune-macos-core.py"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            core = Path(temporary_directory) / "Core"
            site_packages = core / "runtime" / "site-packages"
            (site_packages / "litellm").mkdir(parents=True)
            (site_packages / "mcp").mkdir()
            (site_packages / "mcp" / "types.py").write_text("# runtime import\n", encoding="utf-8")
            (site_packages / "mcp_types").mkdir()
            (site_packages / "mcp_types" / "__init__.py").write_text("", encoding="utf-8")
            (site_packages / "mcp-2.3.0.dist-info").mkdir()
            (site_packages / "mcp-types-2.3.0.dist-info").mkdir()
            (core / "runtime" / "python").mkdir(parents=True)

            prune_module["prune_core"](core)

            self.assertTrue((site_packages / "mcp" / "types.py").is_file())
            self.assertTrue((site_packages / "mcp_types" / "__init__.py").is_file())
            self.assertTrue((site_packages / "mcp-2.3.0.dist-info").is_dir())
            self.assertTrue((site_packages / "mcp-types-2.3.0.dist-info").is_dir())

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
        self.assertIn('[[ -n "$(installed_pids)" ]] || open "$DESTINATION"', installer)
        self.assertNotIn("open -n", installer)
        self.assertNotIn('tell application id "young.router.app" to quit', installer)
        self.assertIn("stop_installed_app", installer)
        # One stop policy for the whole repository: the installer hands the pids
        # it captured to the same helper a restart calls, and keeps no signal
        # ladder of its own -- a polite pass before the kill only postponed the
        # replacement, because the listener is free the moment those processes
        # die.
        self.assertIn('"$APP_PROCESSES" stop $bundle_pids', installer)
        self.assertIn("scripts/app-processes.sh", installer)
        self.assertNotIn("pids_are_alive", installer)
        self.assertNotIn("pid_is_alive", installer)
        self.assertNotIn('kill -TERM "$pid"', installer)
        self.assertIn('line ~ /\\/Young ?Router[^\\/]*\\.app\\/Contents\\/MacOS\\/YoungRouter$/', installer)
        self.assertIn('*/Young*Router*.app/Contents/MacOS/YoungRouter', installer)
        self.assertIn('START_TIMEOUT_SECONDS="${YOUNG_ROUTER_START_TIMEOUT_SECONDS:-10}"', installer)
        self.assertIn('STOP_TIMEOUT_SECONDS="${YOUNG_ROUTER_STOP_TIMEOUT_SECONDS:-20}"', installer)
        self.assertNotIn("STOP_GRACE_POLLS", installer)
        self.assertNotIn("STOP_DRAIN_SECONDS", installer)
        self.assertIn('LAUNCH_RETRY_SECONDS=1', installer)
        self.assertNotIn("preserved_proxy_port", installer)
        self.assertNotIn("refuse_running_install", installer)
        build_replacement = installer.index('YOUNG_ROUTER_MACOS_OUTPUT="$STAGE_ROOT/Young Router.app"')
        # The kill itself lives in the helper the two entry points share, so the
        # installer only has to prove it delegates; the replacement it starts is
        # the one named above.
        self.assertIn('"$APP_PROCESSES" stop $bundle_pids', installer)
        self.assertIn('start_installed_app "$OLD_PIDS"', installer)
        self.assertIn('if ! open "$DESTINATION" >/dev/null 2>&1; then', installer)
        self.assertIn('curl --fail --silent --show-error --max-time 1', installer)
        self.assertIn('health/liveliness', installer)
        self.assertIn(' -m young_router.proxy.macos_proxy ', installer)
        self.assertIn('global_deadline=$((SECONDS + PROXY_READY_TIMEOUT_SECONDS))', installer)
        self.assertIn('restore_previous_app', installer)
        self.assertIn("copy_bundle()", installer)
        self.assertIn('ditto --rsrc --extattr --acl "$source" "$destination"', installer)
        self.assertIn('copy_bundle "$STAGED_APP" "$INSTALL_STAGE"', installer)
        self.assertIn('codesign --verify --deep --strict --verbose=2 "$INSTALL_STAGE"', installer)
        self.assertIn('open "$DESTINATION" >/dev/null 2>&1 || true', installer)
        self.assertIn('if [[ -z "$candidate_pid" && $SECONDS -ge $next_launch_at ]]; then', installer)
        self.assertIn('start_installed_app "$OLD_PIDS"', installer)
        self.assertNotIn('sleep 1', installer)
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
        stop_after_replace = installer.rindex('if ! stop_installed_app "$OLD_PIDS"; then')
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
        # The old bundle is renamed before its processes stop, so a second
        # discovery by the installed path finds nothing.
        self.assertNotIn('REMAINING_OLD_PIDS="$(bundle_processes)"', installer)
        self.assertIn("restore_previous_app", installer)
        self.assertLess(start_replacement, delete_previous)
        self.assertLess(delete_previous, mark_complete)
        self.assertNotIn('( rm -rf "$retired_app" ) &', installer)

    def test_repeat_builds_reuse_only_what_their_inputs_pin(self) -> None:
        script = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(
            encoding="utf-8"
        )

        # The startup smoke's bytecode cache now survives a build, so its key is
        # exactly what a stale entry could have come from: the assembled sources
        # the smokes import and the release the bundled runtime was pinned to. A
        # changed key is a directory holding nothing.
        self.assertIn(
            'PYCACHE_SMOKE_ROOT="${YOUNG_ROUTER_SMOKE_PYCACHE:-$STAGING_CACHE/pycache-smoke}"',
            script,
        )
        self.assertIn('(root / "runtime" / "LITELLM_VERSION").read_bytes()', script)
        self.assertIn('(root / "young_router").rglob("*.py")', script)
        self.assertIn('cat "$PYCACHE_SMOKE_ROOT/key"', script)
        self.assertIn('!= "$PYCACHE_SMOKE_KEY"', script)
        self.assertLess(
            script.index('PYCACHE_SMOKE_KEY="$("$CORE/runtime/python/bin/python3.12" - "$CORE"'),
            script.index('mkdir -p "$PYCACHE_SMOKE_DIR"'),
        )
        # A cache that outlives the run cannot stand in for it: the entry the
        # redirect proof looks for is removed first and has to come back.
        self.assertIn(
            "find \"$PYCACHE_SMOKE_DIR\" -path '*site-packages/litellm/__init__.cpython-312.pyc' -delete",
            script,
        )
        self.assertLess(
            script.index("find \"$PYCACHE_SMOKE_DIR\" -path '*site-packages/litellm/__init__.cpython-312.pyc' -delete"),
            script.index('find "$PYCACHE_SMOKE_DIR" -name'),
        )
        # The relocation smokes read that cache, and the bundle still proves it
        # picked up no bytecode of its own.
        self.assertIn('YOUNG_ROUTER_PYCACHE_DIR="$PYCACHE_SMOKE_DIR"', script)
        self.assertIn('"$CORE/runtime/python/bin/python3.12" -c', script)
        self.assertIn('"$PORTABLE_SMOKE/runtime/bin/litellm" --help', script)
        self.assertNotIn('PYTHONDONTWRITEBYTECODE=1 "$PORTABLE_SMOKE', script)
        self.assertIn('wrote bytecode into the signed Core bundle', script)

        # swiftc is keyed on its four inputs, and the helper is still executed by
        # every build whether or not the entry was there.
        self.assertIn(
            'VISION_CACHE_ENTRY="$VISION_CACHE_ROOT/$VISION_CACHE_KEY/vision_ocr"', script
        )
        self.assertIn(
            'subprocess.run([swiftc, "--version"], capture_output=True).stdout', script
        )
        self.assertLess(
            script.index('if [[ -x "$VISION_CACHE_ENTRY" ]]; then'),
            script.index('"$VISION_HELPER" >/dev/null 2>&1'),
        )

        # Signing a Core's extensions is per file and therefore parallel, and a
        # rename into the install stage is not verified a second time by itself.
        self.assertIn(
            'xargs -0 -n 4 -P "$STRIP_SIGN_JOBS" bash -c "$STRIP_SIGN_FILES" _', script
        )
        # A build does not wait for the previous Core to be deleted, and the
        # worker smokes run while the strip/sign pass and the startup import do.
        self.assertIn('retired_core="$(dirname "$APP")/.YoungRouter-retired-core.$$"', script)
        self.assertIn('rm -rf "$FINISH_BUILD_RETIRED_CORE"', script)
        self.assertIn('WORKER_SMOKE_LOG="$RUNTIME_WORK/worker-smokes.log"', script)
        self.assertIn('if ! wait "$WORKER_SMOKE_PID"; then', script)
        self.assertIn(
            "A bundled worker could not load its staged package; no artifact was produced.",
            script,
        )
        self.assertLess(
            script.index('STRIP_SIGN_FILES='),
            script.index('if ! wait "$WORKER_SMOKE_PID"; then'),
        )
        self.assertLess(
            script.index('if ! wait "$WORKER_SMOKE_PID"; then'),
            script.index('codesign --force --deep --sign - "$APP"'),
        )
        self.assertIn('if [[ "$STAGE_RENAMED" != "1" ]]; then', script)
        self.assertLess(
            script.index('STAGE_RENAMED=1'),
            script.index('if [[ "$STAGE_RENAMED" != "1" ]]; then'),
        )

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

    def test_no_build_ships_a_bundle_without_its_host_executable(self) -> None:
        # The product's executable can be mapped by a process from an earlier
        # build, and Xcode cannot link a new one over a mapped file -- so the
        # stale copy is retired *before* the native build, which then links this
        # build's own executable in its place. Retiring it after the build
        # renamed away the executable it had just linked: the artifact and the
        # installed app carried an empty Contents/MacOS, which macOS draws as a
        # damaged app that no one can launch.
        script = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(encoding="utf-8")

        resolve_product = script.index('APP="$(xcodebuild')
        native_build = script.index('ARCHS="$ARCH" \\\n    ONLY_ACTIVE_ARCH=YES')
        retire_host = script.index('mv "$HOST_EXECUTABLE" "$RETIRED_HOST"')
        prove_host = script.index('if [[ ! -x "$HOST_EXECUTABLE" ]]; then')
        sign_artifact = script.index('codesign --force --deep --sign - "$APP"')
        self.assertLess(resolve_product, native_build)
        self.assertLess(retire_host, native_build)
        self.assertLess(native_build, prove_host)
        self.assertLess(prove_host, sign_artifact)
        self.assertIn("no host executable at $HOST_EXECUTABLE", script)
        final_host_retirement = script.index(
            '[[ -z "$RETIRED_HOST" ]] || ( rm -rf "$RETIRED_HOST" ) &'
        )
        self.assertGreater(final_host_retirement, prove_host)

        # The executable is the one the bundle's own Info.plist names, because
        # the build product (YoungRouter.app) and the installed copy (Young
        # Router.app) do not share a file name with the executable they launch.
        self.assertIn('HOST_EXECUTABLE="$(bundle_host_executable "$APP")"', script)
        self.assertIn("/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable'", script)
        # The build finds its cached product through the same DerivedData
        # location when it links it, so the stale product is the one Xcode
        # actually writes over.
        self.assertIn(
            'XCODE_BUILD_ARGS=( -derivedDataPath "$STAGING_CACHE/xcode-install" )',
            script,
        )
        self.assertIn(
            '"${XCODE_BUILD_ARGS[@]+"${XCODE_BUILD_ARGS[@]}"}" -showBuildSettings',
            script,
        )
        self.assertIn(
            '"${XCODE_BUILD_ARGS[@]+"${XCODE_BUILD_ARGS[@]}"}" \\\n'
            '    ARCHS="$ARCH"',
            script,
        )

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
        self.assertLess(installer_update, installer.index('-scheme YoungRouter-macOS'))

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

    def test_the_macos_job_cannot_run_below_the_xcode_react_native_requires(self) -> None:
        # React Native 0.85's ``use_react_native!`` raises "Please upgrade XCode"
        # below 16.1, and the macos-14 image selects 15.4. The mismatch surfaced
        # as "Invalid `Podfile` file: Please upgrade XCode", which reads as a
        # broken project rather than an unsuitable toolchain, and it only
        # appeared after the pod step that needs minutes to reach it.
        #
        # The image that selects a sufficient Xcode is what the job runs on, and
        # the requirement is asserted up front so an image change reports itself
        # in one line instead of at the bottom of a CocoaPods run.
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertNotIn("runs-on: macos-14", workflow)
        self.assertIn("runs-on: macos-15", workflow)
        self.assertIn("xcodebuild -version", workflow)
        self.assertIn("React Native 0.85 requires Xcode >= 16.1", workflow)
        # Read from the toolchain itself, not from a hardcoded path that the
        # image is free to move.
        self.assertNotIn("/Applications/Xcode_16", workflow)

    def test_react_native_085_line_is_explicitly_pinned(self) -> None:
        package = json.loads((ROOT / "rn/package.json").read_text(encoding="utf-8"))
        vendor = json.loads(
            (ROOT / "rn/vendor/react-native-macos-0.85.json").read_text(encoding="utf-8")
        )

        self.assertEqual(package["engines"]["node"], ">=22")
        self.assertEqual(package["dependencies"]["react-native"], "0.85.3")
        # ``0.85.0-preview.1`` publishes no ``external/`` directory at all, so
        # ``Shared.vcxitems``' unconditional import of
        # ``$(ExternalDir)External.vcxitems`` fails the host build with MSB4019.
        # ``preview.2`` ships it (892 entries, including that file); the codegen
        # package still has only a ``preview.1`` release.
        self.assertEqual(
            package["dependencies"]["react-native-windows"],
            "0.85.0-preview.2",
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
        self.assertIn('-destination "platform=macOS,arch=$ARCH"', script)
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
            script.index('-scheme YoungRouter-macOS'),
        )
        for assignment in (
            "RCT_USE_RN_DEP=0",
            "RCT_USE_PREBUILT_RNCORE=0",
            "RCT_BUILD_HERMES_FROM_SOURCE=true",
            "RCT_HERMES_V1_ENABLED=1",
        ):
            self.assertIn(assignment, script)
        # The native build passes the architecture as an xcodebuild argument.
        # The vendored CLI only forwards `-configuration` and `-scheme`, and the
        # Pods project pins ``ARCHS = "$(ARCHS_STANDARD_64_BIT)"`` in its own
        # settings -- a value an environment variable cannot override, only a
        # command-line argument can.  Without it every pod compiles an x86_64
        # half the installed bundle never contains.
        self.assertIn('ARCHS="$ARCH"', script)
        # The argument belongs to the build invocation, not to the settings
        # probe above it, so anchor on the build's own command line.
        build_invocation = script.index("xcodebuild -workspace \"$APP_ROOT/macos/YoungRouter.xcworkspace\" \\")
        self.assertLess(build_invocation, script.index('ARCHS="$ARCH"', build_invocation))
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
        self.assertIn("prune-macos-core.py", script)
        self.assertIn("Published JavaScript packages include declaration files", script)
        self.assertIn("Deduplicated", (ROOT / "scripts/prune-macos-core.py").read_text(encoding="utf-8"))
        self.assertIn("OPTIONAL_RUNTIME_DIRS", (ROOT / "scripts/prune-macos-core.py").read_text(encoding="utf-8"))
        self.assertIn('".ts"', (ROOT / "scripts/prune-macos-core.py").read_text(encoding="utf-8"))
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
        # The startup smoke proves the bytecode cache the shipped app reads:
        # the compiled modules land in the cache the smoke names, and the signed
        # bundle is asserted to have picked up no ``__pycache__`` of its own.
        self.assertIn('YOUNG_ROUTER_PYCACHE_DIR="$PYCACHE_SMOKE_DIR"', script)
        self.assertIn('find "$PYCACHE_SMOKE_DIR" -name \'__init__.cpython-312.pyc\' -path \'*litellm*\'', script)
        self.assertIn("wrote bytecode into the signed Core bundle", script)
        final_cache_cleanup = script.rindex('find "$CORE"')
        self.assertGreater(final_cache_cleanup, script.index('copy_tree "$CORE" "$PORTABLE_SMOKE"'))
        self.assertLess(final_cache_cleanup, script.index('codesign --force --deep --sign - "$APP"'))
        self.assertIn("export LITELLM_LOCAL_MODEL_COST_MAP=true", script)
        self.assertIn("copy_tree()", script)
        self.assertIn('/usr/bin/ditto "$source" "$destination"', script)
        self.assertIn('[[ -z "$RUNTIME_WORK" ]] || rm -rf "$RUNTIME_WORK"', script)
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
        self.assertIn('PORTABLE_MOVED=1', script)
        self.assertIn('trap finish_build EXIT', script)
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
        # An empty array has to be initialized, and an exit must stop every
        # staged update rather than leave a child writing a cache that the
        # next installer begins to modify.
        self.assertEqual(5, script.count('STAGING_PIDS+=("$!")'))
        self.assertIn('STAGING_PIDS[@]+"${STAGING_PIDS[@]}"', script)
        self.assertIn('INSTALL_LOCK_PID=$!', script)
        self.assertIn('rm -f "$INSTALL_LOCK_OWNER"', script)

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

    def test_the_core_runtime_is_assembled_once_per_release_and_architecture(self) -> None:
        # Assembling the portable runtime is the longest step a build cannot
        # overlap with anything else: uv unpacks a standalone CPython and then
        # resolves a hundred-odd wheels for the pinned LiteLLM release.  Neither
        # half reads the checkout, so a repeat build must reuse the assembled
        # tree rather than redo it.  The cache is keyed on exactly the two
        # inputs that decide the result, so a release bump or an architecture
        # change rebuilds it rather than serving a stale one.
        script = (ROOT / "scripts/build-and-install-macos.sh").read_text(encoding="utf-8")

        self.assertIn('RUNTIME_CACHE_KEY="$UV_RUNTIME-litellm-$LITELLM_VERSION"', script)
        self.assertIn('RUNTIME_CACHE_ENTRY="$RUNTIME_CACHE/$RUNTIME_CACHE_KEY"', script)
        self.assertIn('if [[ ! -x "$RUNTIME_CACHE_ENTRY/python/bin/python3.12"', script)
        self.assertIn('Reusing cached Core runtime:', script)
        # A miss rebuilds from scratch, so the cached entry is never repaired in
        # place -- a half-written tree can only be replaced, not trusted.
        self.assertIn('rm -rf "$RUNTIME_CACHE_ENTRY"', script)
        # The cache holds the interpreter and the wheels; the release lock is
        # written into the bundle after the copy, so a cached entry never
        # carries the claim for a release it was not built from.
        build = script.index('if [[ ! -x "$RUNTIME_CACHE_ENTRY/python/bin/python3.12"')
        copy_into_bundle = script.index('copy_tree "$RUNTIME_CACHE_ENTRY" "$CORE/runtime"')
        lock_into_bundle = script.index(
            'cp "$PROJECT_ROOT/LITELLM_VERSION" "$CORE/runtime/LITELLM_VERSION"', build
        )
        self.assertLess(build, copy_into_bundle)
        self.assertLess(copy_into_bundle, lock_into_bundle)
        # The cache entry itself never carries the lock file.
        self.assertNotIn(
            'cp "$PROJECT_ROOT/LITELLM_VERSION" "$RUNTIME_CACHE_ENTRY/LITELLM_VERSION"', script
        )

    def test_the_node_runtime_is_reused_independently_of_the_python_runtime(self) -> None:
        # The pinned LiteLLM release moves far more often than the Node 22
        # runtime does.  Reusing the Node binary through the Python-runtime
        # decision would throw away a 108 MB download every time the release
        # moved, even though the bytes are identical -- so the Node source is
        # resolved from the installed bundle on its own.
        script = (ROOT / "scripts/build-and-install-macos.sh").read_text(encoding="utf-8")

        self.assertIn('INSTALLED_RUNTIME="$DESTINATION/Contents/Resources/Core/runtime"', script)
        self.assertIn('NODE_SOURCE_PATH="$INSTALLED_RUNTIME/bin/node"', script)
        self.assertIn('NODE_SOURCE_ARGS=(--node-source "$NODE_SOURCE_PATH")', script)
        # The reuse must be reachable when the Python runtime was not reused:
        # the branch that reads the installed Node sits outside the Python
        # reuse gate, not inside it.
        python_gate = script.index('if [[ -z "$RUNTIME_SOURCE" ]]; then')
        python_gate_end = script.index(
            'NODE_MAJOR="$(node -p', python_gate
        )
        node_selection = script.index('elif [[ -x "$INSTALLED_RUNTIME/bin/node" ]]; then')
        self.assertGreater(node_selection, python_gate_end)

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

    def test_the_windows_autolinker_finds_the_winui3_switch_it_rewrites(self) -> None:
        # ``react-native.config.js`` declares ``useWinUI3: true``, so the
        # autolinker reconciles ``ExperimentalFeatures.props`` on every build by
        # writing that value into the file:
        #
        #   const node = content.getElementsByTagName('UseWinUI3');
        #   changesNeeded = node.item(0)?.textContent !== newValue ...
        #   node.item(0).textContent = newValue;   // no optional chain here
        #
        # The comparison tolerates a missing element and the write does not, so
        # an absent ``UseWinUI3`` aborts autolinking and therefore the whole
        # host build, naming a null textContent instead of the missing property:
        #
        #   Error: TypeError: Cannot set properties of null (setting 'textContent')
        #   Autolinking failed.
        #
        # The element must also stay in agreement with the config, or every
        # build rewrites the file it just read.
        props = (ROOT / "rn/apps/windows/windows/ExperimentalFeatures.props").read_text(
            encoding="utf-8"
        )
        config = (ROOT / "rn/apps/windows/react-native.config.js").read_text(encoding="utf-8")

        self.assertIn("<UseWinUI3>", props)
        use_winui3 = "true" if "useWinUI3: true" in config else "false"
        self.assertIn(f"<UseWinUI3>{use_winui3}</UseWinUI3>", props)

    def test_windows_host_pins_the_sdk_that_actually_installed(self) -> None:
        # React Native Windows forces ``WindowsTargetPlatformVersion`` to
        # 10.0.22621.0 for a new architecture project and only defers to a value
        # already at or above it. The hosted Windows image installs exactly one
        # SDK, and the newer image ships 10.0.26100.0, so the build died inside
        # RNW's own project:
        #
        #   Microsoft.Cpp.WindowsSDK.targets: error MSB8036: The Windows SDK
        #   version 10.0.22621.0 was not found. [Common.vcxproj]
        #
        # The host resolves the newest SDK under Windows Kits and passes it as a
        # global MSBuild property, which overrides RNW's default for every
        # project in the solution.
        script = (ROOT / "rn/scripts/build-windows.ps1").read_text(encoding="utf-8")

        self.assertIn("Windows Kits", script)
        self.assertIn("WindowsTargetPlatformVersion=$($InstalledSdk.Name)", script)
        # The value has to be chosen from the disk, not hardcoded to one version.
        self.assertNotIn("WindowsTargetPlatformVersion=10.0.22621.0", script)
        # ``--msbuildprops`` is parsed with ``split(',')`` and each entry with
        # ``split('=')``; anything else yields zero properties and is dropped
        # silently, which is how the staging directory stopped being passed.
        msbuild_props = script[script.index("--msbuildprops"):].split("\n", 1)[0]
        self.assertNotIn(";", msbuild_props, "a semicolon list parses as no properties")
        self.assertEqual(msbuild_props.count(","), 2, msbuild_props)

    def test_windows_build_installs_the_runtime_outside_uvs_management(self) -> None:
        # ``uv python install`` marks the interpreter it lays down as externally
        # managed, so ``uv pip install --python <that python>`` is refused and
        # the runtime ships without LiteLLM:
        #
        #   error: The interpreter at ... is externally managed
        #   ModuleNotFoundError: No module named 'litellm'
        #
        # ``--target`` writes into the interpreter's own site-packages instead of
        # asking it to accept an install, which is how the macOS runtime has
        # always been assembled, and the packages stay importable there without
        # any wrapper.
        script = (ROOT / "rn/scripts/build-windows.ps1").read_text(encoding="utf-8")

        self.assertIn("--target", script)
        self.assertIn("sysconfig.get_paths()['purelib']", script)
        # The refusal is only avoided while the runtime install carries the
        # target; an install into the managed interpreter must not come back.
        install = script[script.index("uv pip install"):]
        install = install[:install.index("\n\n")] if "\n\n" in install else install
        self.assertIn("--target", install)

    def test_the_build_proves_the_staged_workbuddy_key_factory_is_still_reachable(self) -> None:
        # A WorkBuddy variant's store can only open a 5.6-era sealed desktop
        # credential through the library's per-variant at-rest key factory, and
        # that factory is the one name upstream builds internally without
        # exporting from its entry point.  The worker resolves it by name from
        # the sibling chunks, so the build must prove that name is still
        # reachable: without this check a release that moved it would ship, and
        # every signed-in account would read as 未登录 with
        # `electron-binary-unavailable` -- the exact failure this guard exists
        # to catch.
        script = (ROOT / "scripts" / "build-and-install-macos.sh").read_text(encoding="utf-8")
        self.assertIn("atRestKeyProviderFor", script)
        self.assertIn("no longer resolvable from the staged package", script)
        # The published surface stays checked in the same smoke test.
        self.assertIn("WorkBuddyCredentialStore", script)
        # The build's resolution must mirror the worker's, or the check would
        # pass while the worker itself failed to find the factory.
        worker = (ROOT / "young_router" / "adapters" / "workbuddy_worker.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("atRestKeyProviderFor", worker)
        for name in ("WorkBuddyCredentialStore", "WorkBuddyUpstreamClient", "WorkBuddyCatalog"):
            self.assertIn(name, script)
            self.assertIn(name, worker)

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

    def test_every_checkout_uses_the_stored_line_endings(self) -> None:
        # The shared contract check reads ``types.ts`` and asserts on a
        # multi-line literal, so a host that rewrites line endings on checkout
        # turns a passing check into a failing one. A Windows runner checks out
        # with ``core.autocrlf=true``, and without an ``eol`` rule every text
        # file it writes back carries CRLF. One rule covers the whole tree so
        # no future file has to remember to opt in.
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

        self.assertIn("* text=auto eol=lf", attributes)

    def test_the_react_native_cli_is_a_declared_workspace_dependency(self) -> None:
        # ``use_native_modules!`` runs the community CLI from the host app's
        # own directory to discover native modules. The CLI is only an optional
        # peer of ``react-native``, and pnpm keeps an optional peer private to
        # its store entry, so nothing provides it after a clean install and
        # ``pod install`` fails at the Podfile's ``use_native_modules!`` with
        # "Invalid `Podfile` file: exit.". The macOS autolinking path also
        # resolves ``cli-platform-ios`` from the same directory to build the
        # ``macos`` project entry, so both are declared here rather than
        # relying on a stale ``node_modules`` tree.
        package = json.loads((ROOT / "rn/package.json").read_text(encoding="utf-8"))
        dev_dependencies = package["devDependencies"]

        self.assertEqual(dev_dependencies["@react-native-community/cli"], "20.0.0")
        self.assertEqual(
            dev_dependencies["@react-native-community/cli-platform-ios"], "20.0.0"
        )
        lockfile = (ROOT / "rn/pnpm-lock.yaml").read_text(encoding="utf-8")
        self.assertIn("'@react-native-community/cli':", lockfile)
        self.assertIn("'@react-native-community/cli-platform-ios':", lockfile)
        # The installer must not patch the autolinking itself: the failure was
        # a missing declaration, and a Podfile that carried its own config
        # command would hide the next missing one.
        podfile = (ROOT / "rn/apps/macos/macos/Podfile").read_text(encoding="utf-8")
        self.assertIn("use_native_modules!", podfile)

    def test_the_vendor_bootstrap_generates_the_fbreactnativespec_header(self) -> None:
        # ``React-Fabric`` and fourteen other pods include
        # ``FBReactNativeSpec/FBReactNativeSpecJSI.h``. That directory is listed
        # in the vendored fork's ``.gitignore`` and written by the package's own
        # ``prepack`` script, so it ships in the published npm tarball and never
        # appears in a Git checkout — which is what this repository vendors:
        #
        #   NativeAnimatedNodesManager.h:13:10: fatal error:
        #   'FBReactNativeSpec/FBReactNativeSpecJSI.h' file not found
        #
        # No header search path can repair that, because the file does not exist.
        # The bootstrap runs the generator instead.
        bootstrap = (ROOT / "rn/scripts/bootstrap-rnmacos-085.mjs").read_text(encoding="utf-8")

        self.assertIn("generateFBReactNativeSpecIOS.js", bootstrap)
        self.assertIn("generateFBReactNativeSpec(result.packageDirectory, manifest.commit)", bootstrap)
        self.assertIn("FBReactNativeSpecJSI.h", bootstrap)
        # Only the codegen half may run: ``prepack`` also copies the monorepo
        # README over the package's own, which modifies a tracked file and makes
        # the vendor verification below it fail.
        self.assertNotIn("scripts/prepack.js", bootstrap)
        # The generator is run from the package directory, which is where its own
        # ``package.json`` and ``codegenConfig`` live.
        self.assertIn("run(process.execPath, ['-e', script], packageDirectory)", bootstrap)
        # The checkout must still verify as the pinned commit after generating.
        self.assertGreaterEqual(bootstrap.count("verifyVendor()"), 3, "verify after generating too")
        # The header is about 400 KB and a dozen pods include it, so its mtime
        # moving is what makes a repeat build recompile all of them.  Its only
        # input is the vendored package, which `verifyVendor` has just proven is
        # the pinned commit, so a header stamped with that same commit is left
        # alone; a refreshed vendor or a missing header regenerates it.
        guard = bootstrap.index("if (existsSync(generated) && existsSync(stamp)")
        generating = bootstrap.index("run(process.execPath, ['-e', script], packageDirectory)")
        self.assertLess(guard, generating, "an existing header must short-circuit the generator")
        # The stamp records the commit the header came from, which is what makes
        # a vendor refresh regenerate instead of reusing a stale header.
        self.assertIn("writeFileSync(stamp, `${pinnedCommit}\\n`, 'utf8')", bootstrap)

    def test_the_macos_native_build_passes_the_architecture_as_an_argument(self) -> None:
        # The Pods project pins ``ARCHS = "$(ARCHS_STANDARD_64_BIT)"`` in its own
        # build settings, and a value written into a project is overridden only
        # by a command-line argument -- an environment variable does not reach
        # it.  Without the argument xcodebuild compiles an x86_64 slice for every
        # pod that the installed bundle never contains, which was most of a
        # repeat build's native time.  The vendored CLI forwards only
        # `-configuration` and `-scheme`, so the script drives xcodebuild itself.
        script = (ROOT / "scripts/build-and-install-macos.sh").read_text(encoding="utf-8")

        self.assertNotIn('node "$RNMACOS_CLI" build-macos', script)
        self.assertIn('ARCHS="$ARCH"', script)
        self.assertIn("ONLY_ACTIVE_ARCH=YES", script)
        build = script.index('xcodebuild -workspace "$APP_ROOT/macos/YoungRouter.xcworkspace" \\')
        arch_argument = script.index('ARCHS="$ARCH"', build)
        self.assertLess(build, arch_argument, "the architecture is an argument to the build")
        # The architecture the bundle is built for is the same one its runtime
        # and its Vision helper are built for.
        self.assertIn('"$ARCH-apple-macosx14.0"', script)


if __name__ == "__main__":
    unittest.main()

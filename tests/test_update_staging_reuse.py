"""A staged release is reused only when it proves it is the resolved one.

The build resolves every staged integration against its upstream release on
every run; re-installing a release that has not changed is the largest
remaining cost of a local build.  What makes skipping that install safe is
provenance, not trust: the staging script records the release it installed, and
a later build reuses the tree only when its own lookup resolves that exact
package, version, and published digest.

These tests hold the boundary in both directions, because only one direction
protects the artifact.  The reuse has to actually happen, or the build is slow
for nothing; and every way a tree can be stale, partial, or tampered with has
to fall back to a full install, or the build ships something no one checked.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

update_common = importlib.import_module("update_common")
node_runtime = importlib.import_module("update_node_runtime")
workbuddy_connect = importlib.import_module("update_connect_workbuddy")
our_free_model = importlib.import_module("update_our_free_model")
dsh_vision_router = importlib.import_module("update_dsh_vision_router")


PACKAGE = "dsh-connect-workbuddy"
VERSION = "1.2.3"
INTEGRITY = "sha512-abc123"


def stage(destination: Path, *, version: str = VERSION, integrity: str = INTEGRITY) -> None:
    """Write the shape a completed staging run leaves behind."""

    (destination / "lib").mkdir(parents=True, exist_ok=True)
    (destination / "lib/index.js").write_text("export const x = 1\n", encoding="utf-8")
    (destination / "package.json").write_text(
        json.dumps({"name": PACKAGE, "version": version}), encoding="utf-8"
    )
    for peer in workbuddy_connect.REQUIRED_PEER_DIRECTORIES:
        (destination / "node_modules" / peer).mkdir(parents=True, exist_ok=True)
    update_common.record_staged_release(
        destination, package_name=PACKAGE, version=version, integrity=integrity
    )


class StagedReleaseReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="young-router-reuse-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))

    def reusable(self, destination: Path, **overrides) -> bool:
        arguments = dict(
            package_name=PACKAGE,
            version=VERSION,
            integrity=INTEGRITY,
            required_files=workbuddy_connect.REQUIRED_PACKAGE_FILES,
            required_peer_dirs=workbuddy_connect.REQUIRED_PEER_DIRECTORIES,
        )
        arguments.update(overrides)
        return update_common.reused_staged_release(destination, **arguments)

    def test_a_tree_holding_the_resolved_release_is_reused(self) -> None:
        stage(self.directory)
        self.assertTrue(self.reusable(self.directory))

    def test_a_different_release_is_not_reused(self) -> None:
        stage(self.directory, version="1.2.2")
        self.assertFalse(self.reusable(self.directory))

    def test_a_different_published_digest_is_not_reused(self) -> None:
        # The same version can be republished with different bytes, which is
        # exactly the case a version-only comparison would wave through.
        stage(self.directory, integrity="sha512-somethingelse")
        self.assertFalse(self.reusable(self.directory))

    def test_a_registry_without_a_digest_never_reuses(self) -> None:
        stage(self.directory)
        self.assertFalse(self.reusable(self.directory, integrity=None))

    def test_an_unrecorded_tree_is_not_reused(self) -> None:
        stage(self.directory)
        (self.directory / update_common.STAGED_RELEASE_MARKER).unlink()
        self.assertFalse(self.reusable(self.directory))

    def test_an_unreadable_record_is_not_reused(self) -> None:
        stage(self.directory)
        (self.directory / update_common.STAGED_RELEASE_MARKER).write_text(
            "{not json", encoding="utf-8"
        )
        self.assertFalse(self.reusable(self.directory))

    def test_a_missing_entry_point_is_not_reused(self) -> None:
        stage(self.directory)
        (self.directory / "lib/index.js").unlink()
        self.assertFalse(self.reusable(self.directory))

    def test_a_missing_peer_is_not_reused(self) -> None:
        stage(self.directory)
        target = self.directory / "node_modules" / workbuddy_connect.REQUIRED_PEER_DIRECTORIES[0]
        __import__("shutil").rmtree(target)
        self.assertFalse(self.reusable(self.directory))

    def test_a_stale_tree_is_reinstalled_by_update(self) -> None:
        # The whole point, at the call site: a changed upstream release must
        # reach the installer instead of being answered from the cache.
        stage(self.directory, version="0.0.1")
        calls: list[Path] = []

        def fake_metadata(registry_url: str):
            return VERSION, "https://registry.invalid/pkg.tgz", {"dist": {"integrity": INTEGRITY}}

        def fake_request(url: str, *, timeout: int = 0) -> bytes:
            return b"tarball"

        def fake_install(npm, npm_root, tarball, peers) -> None:
            calls.append(self.directory)

        def fake_flatten(npm_root, destination) -> str:
            stage(destination)
            return VERSION

        with mock.patch.object(workbuddy_connect, "_package_metadata", fake_metadata), \
             mock.patch.object(workbuddy_connect, "_request_bytes", fake_request), \
             mock.patch.object(workbuddy_connect, "_find_executable", lambda name: "npm"), \
             mock.patch.object(workbuddy_connect, "_run_npm_install", fake_install), \
             mock.patch.object(workbuddy_connect, "_flatten_package", fake_flatten):
            version = workbuddy_connect.update(self.directory, registry_url="https://registry.invalid")

        self.assertEqual(version, VERSION)
        self.assertEqual(len(calls), 1, "a stale tree must be reinstalled")

    def test_a_matching_tree_skips_the_installer(self) -> None:
        stage(self.directory)
        calls: list[Path] = []

        def fake_metadata(registry_url: str):
            return VERSION, "https://registry.invalid/pkg.tgz", {"dist": {"integrity": INTEGRITY}}

        def fail_install(*args, **kwargs) -> None:
            calls.append(self.directory)

        with mock.patch.object(workbuddy_connect, "_package_metadata", fake_metadata), \
             mock.patch.object(workbuddy_connect, "_find_executable", lambda name: "npm"), \
             mock.patch.object(workbuddy_connect, "_run_npm_install", fail_install):
            version = workbuddy_connect.update(self.directory, registry_url="https://registry.invalid")

        self.assertEqual(version, VERSION)
        self.assertEqual(calls, [], "an unchanged tree must not be reinstalled")


class NpmPeerSpecTransportTests(unittest.TestCase):
    """A peer spec reaches npm as the range the registry declared.

    The specs carry npm's own version ranges, and a range contains ``^``.  On
    Windows ``npm`` resolves to ``npm.cmd``, which Python can only launch
    through ``cmd.exe``, and ``cmd.exe`` consumes a bare ``^`` as its escape
    character.  Handing the specs to the shell as arguments therefore turned
    ``@deepseek-ai/cordis@^4.0.2`` into ``@deepseek-ai/cordis@4.0.2``, which
    conflicts with the ``~4.0.4`` a sibling peer requires:

        npm error Found: @deepseek-ai/cordis@4.0.2
        npm error peer @deepseek-ai/cordis@"~4.0.4" from @deepseek-ai/dsh-llm@0.2.0-rc.2

    Naming them in a manifest keeps them off the command line, so these tests
    hold that the range survives the trip and that nothing but the tarball is
    still passed as an argument.
    """

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="young-router-npm-spec-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))

    def test_a_caret_range_survives_into_the_manifest(self) -> None:
        update_common.npm_dependency_manifest(
            self.directory,
            ["@deepseek-ai/cordis@^4.0.2", "react@^18.2.0", "@deepseek-ai/dsh-llm@0.2.0-rc.2"],
        )
        manifest = json.loads((self.directory / "package.json").read_text(encoding="utf-8"))

        # The exact spec npm must resolve, with its range intact.
        self.assertEqual(manifest["dependencies"]["@deepseek-ai/cordis"], "^4.0.2")
        self.assertEqual(manifest["dependencies"]["react"], "^18.2.0")
        self.assertEqual(manifest["dependencies"]["@deepseek-ai/dsh-llm"], "0.2.0-rc.2")

    def test_a_bare_name_asks_for_any_version(self) -> None:
        # ``REQUIRED_PEER_DIRECTORIES`` entries can arrive without a range, and
        # a name with no version must still be installable rather than dropped.
        update_common.npm_dependency_manifest(self.directory, ["sharp"])
        manifest = json.loads((self.directory / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["dependencies"]["sharp"], "*")

    def test_the_staged_peers_never_travel_as_arguments(self) -> None:
        # The boundary itself: whatever the real workbuddy peer list is, none of
        # it may appear on the command line, because that is the only place a
        # shell can rewrite it.
        version_payload = {
            "peerDependencies": {
                "@deepseek-ai/cordis": "^4.0.2",
                "@deepseek-ai/dsh-llm": "0.2.0-rc.2",
            }
        }
        specs = workbuddy_connect._peer_specs(version_payload)
        self.assertIn("@deepseek-ai/cordis@^4.0.2", specs)

        recorded: list[list[str]] = []

        def capture(command, **kwargs):
            recorded.append(list(command))
            return mock.Mock(returncode=0, stderr="", stdout="")

        tarball = self.directory / "pkg.tgz"
        tarball.write_bytes(b"")
        with mock.patch.object(update_common.subprocess, "run", capture):
            update_common.run_npm_install(
                "npm",
                self.directory / "npm",
                tarball,
                specs,
                package_name=PACKAGE,
                timeout=1,
            )

        self.assertEqual(len(recorded), 1, "one install command")
        command = recorded[0]
        # Nothing that carries a range may be an argument; only the tarball is.
        for part in command:
            self.assertNotIn("^", part, f"{part!r} would be rewritten by cmd.exe")
        self.assertIn(str(tarball), command, "the tarball is still an argument")
        self.assertNotIn("@deepseek-ai/cordis@^4.0.2", command)
        # The ranges are in the manifest the same call just wrote.
        manifest = json.loads(
            (self.directory / "npm" / "package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["dependencies"]["@deepseek-ai/cordis"], "^4.0.2")


class DeprecatedReleaseTests(unittest.TestCase):
    """A withdrawn release is a failed build, never a packaged one.

    ``dsh-workbuddy-connect`` was withdrawn this way: npm kept serving the
    tarball and ``npm install`` still exited 0, so a build that only checked
    the exit status packaged a frozen package and reported success.  The
    registry's ``deprecated`` notice is the only place that withdrawal is
    visible, so the resolution step must refuse it.
    """

    def _metadata(self, **overrides):
        payload = {
            "dist-tags": {"latest": "0.7.9"},
            "versions": {
                "0.7.9": {
                    "name": PACKAGE,
                    "version": "0.7.9",
                    "dist": {"tarball": "https://registry.invalid/pkg.tgz"},
                }
            },
        }
        version = payload["versions"]["0.7.9"]
        version.update(overrides)
        return payload

    def _resolve(self, payload):
        with mock.patch.object(
            update_common, "request_json", lambda *a, **k: payload
        ):
            return update_common.package_metadata(
                "https://registry.invalid/pkg",
                package_name=PACKAGE,
                timeout=5,
                user_agent="test",
            )

    def test_a_deprecated_latest_is_refused(self) -> None:
        payload = self._metadata(
            deprecated="因个人精力有限，本项目已停止维护，不再提供更新和支持。"
        )
        with self.assertRaises(update_common.UpdateError) as raised:
            self._resolve(payload)
        # The notice travels into the failure, so the build says why it stopped
        # instead of only that it did.
        self.assertIn("deprecated", str(raised.exception))
        self.assertIn("停止维护", str(raised.exception))

    def test_an_undeprecated_latest_still_resolves(self) -> None:
        version, tarball, _ = self._resolve(self._metadata())
        self.assertEqual(version, "0.7.9")
        self.assertEqual(tarball, "https://registry.invalid/pkg.tgz")

    def test_an_empty_or_false_notice_is_not_a_withdrawal(self) -> None:
        # npm writes ``""`` on some republishes and ``false`` when a notice is
        # lifted; neither may fail the build.
        for value in ("", False, None):
            with self.subTest(value=value):
                version, _, _ = self._resolve(self._metadata(deprecated=value))
                self.assertEqual(version, "0.7.9")

    def test_the_workbuddy_stager_resolves_through_this_guard(self) -> None:
        # The guard only protects a build if every stager uses it: this one
        # resolves through ``package_metadata`` rather than reading the
        # registry itself.
        source = (SCRIPTS / "update_connect_workbuddy.py").read_text(encoding="utf-8")
        self.assertIn("package_metadata(", source)
        self.assertIn("PACKAGE_NAME = \"dsh-connect-workbuddy\"", source)


class ScopedPeerMergeTests(unittest.TestCase):
    """A nested dependency must never erase its scope-mates.

    npm nests a dependency under the package when the flat tree carries
    another version of it.  ``flatten_npm_package`` copies the package first,
    so the merge that follows meets an ``@scope`` directory that already
    exists -- and skipping the whole of it because one name was nested dropped
    every sibling peer.  ``cosmokit`` vanished with ``schemastery`` that way and
    the staged tree could not be imported at all.
    """

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="young-router-scope-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))
        self.npm_root = self.directory / "npm"
        self.package_root = self.npm_root / "node_modules" / PACKAGE
        (self.package_root / "lib").mkdir(parents=True)
        (self.package_root / "lib/index.js").write_text("export const x = 1\n", encoding="utf-8")
        (self.package_root / "package.json").write_text(
            json.dumps({"name": PACKAGE, "version": VERSION}), encoding="utf-8"
        )

    def _peer(self, relative: str, body: str = "export const y = 1\n") -> Path:
        path = self.npm_root / "node_modules" / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "index.js").write_text(body, encoding="utf-8")
        return path

    def _nested(self, relative: str, body: str) -> None:
        path = self.package_root / "node_modules" / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "index.js").write_text(body, encoding="utf-8")

    def test_a_scoped_peer_survives_a_nested_sibling(self) -> None:
        # The store's own nested copy wins for the name it carries...
        self._nested("@scope/versioned", "nested\n")
        # ...and every other peer in the same scope is still merged in.
        self._peer("@scope/versioned", "flat\n")
        self._peer("@scope/sibling", "sibling\n")
        self._peer("@scope/deep", "deep\n")

        destination = self.directory / "out"
        update_common.flatten_npm_package(
            self.npm_root,
            destination,
            package_name=PACKAGE,
            required_files=("package.json", "lib/index.js"),
            required_peer_dirs=("@scope/sibling",),
        )

        staged = destination / "node_modules"
        # The nested copy is the one the package resolved against.
        self.assertEqual((staged / "@scope/versioned/index.js").read_text(encoding="utf-8"), "nested\n")
        # The siblings are present: this is the assertion that fails when the
        # merge skips the whole scope.
        self.assertTrue((staged / "@scope/sibling/index.js").is_file())
        self.assertTrue((staged / "@scope/deep/index.js").is_file())

    def test_an_unscoped_nested_copy_still_wins(self) -> None:
        self._nested("plain", "nested\n")
        self._peer("plain", "flat\n")

        destination = self.directory / "out"
        update_common.flatten_npm_package(
            self.npm_root,
            destination,
            package_name=PACKAGE,
            required_files=("package.json", "lib/index.js"),
        )

        self.assertEqual(
            (destination / "node_modules/plain/index.js").read_text(encoding="utf-8"),
            "nested\n",
        )


class StagedEntrySpecifierTests(unittest.TestCase):
    """A staged ESM entry is imported by URL, not by filesystem path.

    ``_verify_staged_chain`` proves the peer closure, the environment shim, and
    the private entry path all resolve by running the entry once.  It used to
    hand Node the path as ``str(entry)``, which Node accepts on macOS and Linux
    because a POSIX path is already a valid specifier.  On Windows the same
    call ends the build:

        Only URLs with a scheme in: file, data, and node are supported by the
        default ESM loader. On Windows, absolute paths must be valid file://
        URLs. Received protocol 'c:'

    The URL form is the one both hosts accept, so this holds the boundary that
    the argument is a URL and that the path it names is still the real entry.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="young-router-entry-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.entry = (
            self.root / "node_modules" / dsh_vision_router.PACKAGE_NAME / "lib" / "core-primitives.js"
        )
        self.entry.parent.mkdir(parents=True, exist_ok=True)
        self.entry.write_text("export const marker = 1;\n", encoding="utf-8")

    def recorded_argument(self) -> tuple[list[str], str]:
        """Run the verifier against a stub Node and return the argv it received."""

        captured: list[list[str]] = []

        def fake_run(command, **kwargs):
            captured.append(list(command))
            return mock.Mock(returncode=0, stdout='{"free": []}', stderr="")

        # ``subprocess`` is imported inside the function, so the patch goes on
        # the real module rather than a name on this one.
        with mock.patch("subprocess.run", fake_run):
            dsh_vision_router._verify_staged_chain(self.root)
        self.assertEqual(len(captured), 1, "one verification run")
        command = captured[0]
        return command, command[-1]

    def test_the_entry_reaches_node_as_a_file_url(self) -> None:
        _command, argument = self.recorded_argument()

        self.assertTrue(
            argument.startswith("file://"),
            f"a raw path breaks the Windows ESM loader: {argument}",
        )
        # The URL has to name the entry this call is verifying, or the check
        # proves something about the wrong file.
        self.assertEqual(Path(argument.removeprefix("file://")), self.entry.resolve())

    def test_the_import_specifier_is_not_a_bare_windows_path(self) -> None:
        # ``D:\...`` is the shape Node rejects; a drive letter can never be the
        # scheme of the argument this call passes.
        _command, argument = self.recorded_argument()

        self.assertNotRegex(argument, r"^[A-Za-z]:[\\/]", argument)


class NodeRuntimeStagingTests(unittest.TestCase):
    """The Node runtime is copied from the installed bundle without rewriting it.

    Every artifact build hands ``--node-source`` the Node executable the
    installed app already carries.  Writing those 90 MB back out on each run
    would move the staged file's mtime, and the packed Core would then treat an
    unchanged runtime as changed on every build.  The copy is skipped only when
    the staged bytes are already the source's bytes, so a different source
    still replaces what is there.
    """

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="young-router-node-stage-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.output = self.directory / "node-output"
        self.source = self.directory / "source-node"
        # Larger than the 1 MiB floor ``_install_node`` enforces, so a real copy
        # is not rejected as a placeholder.
        self.source.write_bytes(b"\x7fELF" + b"n" * (1024 * 1024 + 8))
        self.source.chmod(0o755)

    def install(self) -> None:
        node_runtime._install_node(
            self.output,
            source=str(self.source),
            index_url="https://node.invalid/index.json",
            archive_url=None,
        )

    def test_an_unchanged_source_leaves_the_staged_binary_in_place(self) -> None:
        self.install()
        target = self.output / "node"
        before = target.stat().st_mtime_ns
        self.install()
        # The mtime is the whole point: a rewrite would move it and make the
        # packed Core see a change that is not one.
        self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertEqual(target.read_bytes(), self.source.read_bytes())

    def test_a_different_source_replaces_the_staged_binary(self) -> None:
        self.install()
        target = self.output / "node"
        replacement = self.source.read_bytes()[:-1] + b"X"
        self.source.write_bytes(replacement)
        self.install()
        self.assertEqual(target.read_bytes(), replacement)

    def test_the_staged_binary_is_executable(self) -> None:
        self.install()
        mode = (self.output / "node").stat().st_mode
        self.assertTrue(mode & 0o111, "the staged Node must stay executable")


class OurFreeModelStagingTests(unittest.TestCase):
    """The Our Free Model lane stages its upstream package intact."""

    @mock.patch.object(our_free_model, "_install_peers", autospec=True)
    @mock.patch.object(our_free_model, "find_npm", return_value="npm")
    def test_the_upstream_source_is_copied_without_a_protocol_rewrite(
        self, _find_npm: mock.Mock, install_peers: mock.Mock
    ) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="young-router-ofm-source-"))
        self.addCleanup(shutil.rmtree, temporary, ignore_errors=True)
        source = temporary / "dsh-our-free-model"
        source.mkdir()
        for relative in our_free_model.REQUIRED_FILES:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("upstream\\n", encoding="utf-8")
        (source / "package.json").write_text(
            json.dumps(
                {
                    "name": "dsh-our-free-model",
                    "version": "2.4.0",
                    "peerDependencies": {
                        name: "*" for name in our_free_model.REQUIRED_PEERS
                    },
                }
            ),
            encoding="utf-8",
        )
        output = temporary / "output" / "our_free_model"

        def install_peers_once(_command, *, npm_root, **_kwargs) -> None:
            for name in our_free_model.REQUIRED_PEERS:
                path = npm_root / "node_modules" / name
                path.mkdir(parents=True)
                (path / "package.json").write_text("{}", encoding="utf-8")

        install_peers.side_effect = install_peers_once
        version = our_free_model.update(output, source_url=f"file://{source}")
        original_record = (output / update_common.STAGED_RELEASE_MARKER).read_text(
            encoding="utf-8"
        )
        self.assertEqual(version, "2.4.0")
        self.assertEqual((output / "src/core/runtime.js").read_text(), "upstream\\n")
        self.assertTrue((output / "node_modules" / "@deepseek-ai" / "dsh-llm").is_dir())
        original_runtime = (output / "src/core/runtime.js").read_bytes()
        original_peer = (
            output / "node_modules" / "@deepseek-ai" / "dsh-llm" / "package.json"
        ).read_text(encoding="utf-8")
        self.assertEqual(install_peers.call_count, 1, "an unchanged source must keep its staged peers")
        self.assertEqual((output / "src/core/runtime.js").read_bytes(), original_runtime)
        self.assertEqual(
            (output / "node_modules" / "@deepseek-ai" / "dsh-llm" / "package.json").read_text(
                encoding="utf-8"
            ),
            original_peer,
        )

    def test_an_upstream_missing_its_protocol_fails_the_stage(self) -> None:
        source = Path(tempfile.mkdtemp(prefix="young-router-ofm-incomplete-"))
        self.addCleanup(shutil.rmtree, source, ignore_errors=True)
        (source / "package.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(update_common.UpdateError):
            our_free_model.update(
                source.parent / "output",
                source_url=f"file://{source}",
            )


def _copy_fixture_to_checkout(source: Path):
    """Stand in for the tag clone in a test that has no network."""

    def clone(_source_url: str, _ref: str, destination: Path, _timeout: int) -> None:
        shutil.copytree(source, destination, symlinks=True)

    return clone


def _install_fake_peers(_command, *, npm_root: Path, **_kwargs) -> None:
    """The peer install a fixture stands in for: the declared peer directories."""

    for name in our_free_model.REQUIRED_PEERS:
        path = npm_root / "node_modules" / name
        path.mkdir(parents=True)
        (path / "package.json").write_text("{}", encoding="utf-8")



class OurFreeModelReleaseResolutionTests(unittest.TestCase):
    """Every artifact build pins the newest published release, never a branch.

    ``main`` moves without a version, so a build that cloned it could not say
    which release it had shipped and could package unreviewed work.  These
    tests hold both halves of that: the release is resolved on every run, and a
    lookup that cannot answer fails the build instead of falling back.
    """

    def test_a_prerelease_tag_is_never_chosen(self) -> None:
        self.assertEqual(
            ("v2.5.5", "2.5.5"),
            self.tag_and_version(
                [
                    ("v2.5.5", "abc", False, False),
                    ("v2.6.0-beta.1", "def", False, False),
                    ("v3.0.0", "ghi", False, True),
                    ("v2.5.4-macos-unofficial", "jkl", False, False),
                ]
            ),
        )

    def test_a_draft_release_is_never_chosen(self) -> None:
        self.assertEqual(
            ("v2.5.5", "2.5.5"),
            self.tag_and_version(
                [("v2.5.5", "abc", False, False), ("v2.6.0", "def", True, False)]
            ),
        )

    def test_a_repository_with_no_versioned_release_resolves_nothing(self) -> None:
        self.assertIsNone(
            our_free_model._highest_release_candidate([("nightly", "abc", False, False)])
        )

    @staticmethod
    def tag_and_version(tags) -> tuple[str, str] | None:
        release = our_free_model._highest_release_candidate(tags)
        return None if release is None else (release.tag, release.version)

    def test_the_tag_list_names_the_release_and_its_commit(self) -> None:
        """The commit is the identity, so the lookup has to carry it.

        A tag can be moved and two spellings can reach one tree; the commit
        cannot be either.  It is also what lets a repeat build answer "is this
        the release I already hold" without cloning the release to find out.
        """

        with mock.patch.object(
            our_free_model,
            "_remote_tags",
            return_value=[
                ("v2.5.4", "aaa", False, False),
                ("v2.5.5", "25151bab43ff28bfc0eef5a3dbec4443ecc6d2f2", False, False),
            ],
        ), mock.patch.object(our_free_model, "_releases_api", return_value=[]):
            release = our_free_model.latest_release(our_free_model.SOURCE_URL)
        self.assertEqual("v2.5.5", release.tag)
        self.assertEqual("2.5.5", release.version)
        self.assertEqual(
            "25151bab43ff28bfc0eef5a3dbec4443ecc6d2f2", release.revision
        )

    def test_an_unavailable_release_api_still_resolves_from_the_tags(self) -> None:
        # The GitHub API allows sixty anonymous reads an hour per address, so a
        # build machine that resolves releases all day must still be able to
        # name one.  The git remote carries the same tags without that budget.
        with mock.patch.object(
            our_free_model,
            "_remote_tags",
            return_value=[("v2.5.5", "abc", False, False)],
        ), mock.patch.object(
            our_free_model,
            "_releases_api",
            side_effect=update_common.UpdateError("HTTP Error 403: rate limit exceeded"),
        ):
            self.assertEqual(
                "v2.5.5", our_free_model.latest_release(our_free_model.SOURCE_URL).tag
            )

    def test_a_release_marked_prerelease_by_the_api_is_skipped(self) -> None:
        """A prerelease is not always spelled as one in its tag.

        The tag list cannot see GitHub's prerelease mark, so the API's verdict
        has to be able to remove a candidate the tags alone would have chosen.
        """

        with mock.patch.object(
            our_free_model,
            "_remote_tags",
            return_value=[
                ("v2.5.5", "abc", False, False),
                ("v2.6.0", "def", False, False),
            ],
        ), mock.patch.object(
            our_free_model,
            "_releases_api",
            return_value=[
                ("v2.5.5", "", False, False),
                ("v2.6.0", "", False, True),
            ],
        ):
            release = our_free_model.latest_release(our_free_model.SOURCE_URL)
        self.assertEqual("v2.5.5", release.tag)

    def test_a_build_with_no_resolvable_release_fails(self) -> None:
        """No release anywhere is a build failure, not a fallback.

        Falling back to ``main`` here is exactly what let an artifact ship a
        branch, so the failure has to surface as the staging job failing.
        """

        with mock.patch.object(
            our_free_model,
            "_remote_tags",
            side_effect=update_common.UpdateError("could not resolve host"),
        ):
            with self.assertRaises(update_common.UpdateError) as raised:
                our_free_model.latest_release(our_free_model.SOURCE_URL)
        self.assertIn("could not resolve host", str(raised.exception))

    def test_a_mislabelled_release_is_refused_before_it_is_staged(self) -> None:
        """A tag whose tree declares another version is refused, not recorded.

        The record is what a later build compares against, so writing a version
        the lookup never resolved would make the next build either reinstall a
        release it already has or trust one it does not.
        """

        source = Path(tempfile.mkdtemp(prefix="young-router-ofm-mislabelled-"))
        self.addCleanup(shutil.rmtree, source, ignore_errors=True)
        for relative in our_free_model.REQUIRED_FILES:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("upstream\n", encoding="utf-8")
        (source / "package.json").write_text(
            json.dumps(
                {
                    "name": "dsh-our-free-model",
                    "version": "2.5.4",
                    "peerDependencies": {
                        name: "*" for name in our_free_model.REQUIRED_PEERS
                    },
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            our_free_model,
            "latest_release",
            return_value=our_free_model.Release("v2.5.5", "2.5.5", "abc"),
        ), mock.patch.object(
            our_free_model, "_clone_tag", side_effect=_copy_fixture_to_checkout(source)
        ), mock.patch.object(
            our_free_model, "_install_peers", autospec=True
        ) as install_peers:
            install_peers.side_effect = _install_fake_peers
            with self.assertRaises(update_common.UpdateError) as raised:
                our_free_model.update(
                    source.parent / "output",
                    source_url="https://example.test/repo.git",
                )
        self.assertIn("mislabelled release", str(raised.exception))

    def test_a_repeat_build_reuses_the_staged_release_without_cloning_it(self) -> None:
        """The decision costs one `ls-remote`; the clone is the whole point.

        A repeat local build fetched a 38 MB tag archive only to compute a
        digest it then compared with the staged tree and discarded.  The commit
        the tag points at answers the same question in about a second, so a
        build that already holds the release must not clone at all.
        """

        output = Path(tempfile.mkdtemp(prefix="young-router-ofm-reuse-")) / "staged"
        self.addCleanup(shutil.rmtree, output.parent, ignore_errors=True)
        output.mkdir(parents=True, exist_ok=True)
        update_common.record_staged_release(
            output,
            package_name=our_free_model.PACKAGE_NAME,
            version="2.5.5",
            integrity="25151bab43ff28bfc0eef5a3dbec4443ecc6d2f2",
        )
        for relative in our_free_model.REQUIRED_FILES:
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("staged\n", encoding="utf-8")
        for name in our_free_model.REQUIRED_PEERS:
            (output / "node_modules" / name).mkdir(parents=True)

        with mock.patch.object(
            our_free_model,
            "latest_release",
            return_value=our_free_model.Release(
                "v2.5.5", "2.5.5", "25151bab43ff28bfc0eef5a3dbec4443ecc6d2f2"
            ),
        ), mock.patch.object(our_free_model, "_clone_tag") as clone, mock.patch.object(
            our_free_model, "_install_peers", autospec=True
        ) as install_peers:
            version = our_free_model.update(output, source_url="https://example.test/repo.git")

        self.assertEqual("2.5.5", version)
        clone.assert_not_called()
        install_peers.assert_not_called()

    def test_a_moved_tag_forces_a_restage(self) -> None:
        """Same version, different commit is a different release.

        An upstream that retags an existing version has shipped different bytes
        under the same number, and a version-only comparison would keep serving
        the old tree forever.
        """

        output = Path(tempfile.mkdtemp(prefix="young-router-ofm-moved-")) / "staged"
        self.addCleanup(shutil.rmtree, output.parent, ignore_errors=True)
        output.mkdir(parents=True, exist_ok=True)
        update_common.record_staged_release(
            output,
            package_name=our_free_model.PACKAGE_NAME,
            version="2.5.5",
            integrity="old-commit",
        )
        for relative in our_free_model.REQUIRED_FILES:
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("staged\n", encoding="utf-8")
        for name in our_free_model.REQUIRED_PEERS:
            (output / "node_modules" / name).mkdir(parents=True)

        with mock.patch.object(
            our_free_model,
            "latest_release",
            return_value=our_free_model.Release("v2.5.5", "2.5.5", "new-commit"),
        ), mock.patch.object(
            our_free_model, "_clone_tag"
        ) as clone, mock.patch.object(
            our_free_model, "_install_peers", autospec=True
        ) as install_peers:
            clone.side_effect = update_common.UpdateError("stop here")
            install_peers.side_effect = _install_fake_peers
            with self.assertRaises(update_common.UpdateError):
                our_free_model.update(output, source_url="https://example.test/repo.git")
        clone.assert_called_once()

if __name__ == "__main__":
    unittest.main()

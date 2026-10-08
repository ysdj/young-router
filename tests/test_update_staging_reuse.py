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
pi_web_access = importlib.import_module("update_pi_web_access")
workbuddy_connect = importlib.import_module("update_workbuddy_connect")


PACKAGE = "dsh-workbuddy-connect"
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


class PiWebAccessReuseTests(unittest.TestCase):
    """The pi-web-access staging also imposes the shared browser identity."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="young-router-pi-reuse-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.user_agent = pi_web_access._browser_user_agent()

    def write_source(self, name: str, text: str) -> None:
        (self.directory / name).write_text(text, encoding="utf-8")

    def stage_rewritten(self) -> None:
        (self.directory / "index.ts").write_text("export const x = 1\n", encoding="utf-8")
        (self.directory / "package.json").write_text(
            json.dumps({"name": "pi-web-access", "version": "0.1.0"}), encoding="utf-8"
        )
        self.write_source(
            "a-request.ts",
            json.dumps({"headers": {"user-agent": self.user_agent}}),
        )
        self.write_source(
            "z-request.ts",
            json.dumps({"headers": {"user-agent": self.user_agent}}),
        )

    def test_a_tree_with_the_shared_identity_is_reusable(self) -> None:
        self.stage_rewritten()
        self.assertTrue(pi_web_access._carries_the_shared_user_agent(self.directory))

    def test_a_self_naming_literal_in_any_single_file_blocks_reuse(self) -> None:
        # The scan must cover every file: an earlier version of this check
        # returned as soon as it saw one rewritten file, so a later file that
        # still named the package would have been waved through and shipped a
        # self-identifying client.
        self.stage_rewritten()
        self.write_source(
            "z-request.ts",
            json.dumps({"headers": {"user-agent": "pi-web-access/9.9.9"}}),
        )
        self.assertFalse(pi_web_access._carries_the_shared_user_agent(self.directory))

    def test_a_tree_without_the_shared_identity_is_not_reusable(self) -> None:
        # A tree that names nobody is still not proof that this build's
        # identity was written into it, so it is staged again rather than
        # assumed.
        (self.directory / "index.ts").write_text("export const x = 1\n", encoding="utf-8")
        self.write_source("a-request.ts", json.dumps({"headers": {"accept": "*/*"}}))
        self.assertFalse(pi_web_access._carries_the_shared_user_agent(self.directory))

    def test_the_rewrite_is_what_reuse_relies_on(self) -> None:
        # Tie the gate to the rewrite itself: whatever the rewrite produces must
        # satisfy the check a reused tree has to pass.  Three literals because
        # the rewrite treats fewer than three as an upstream layout change.
        for index in range(3):
            self.write_source(
                f"request{index}.ts",
                json.dumps({"headers": {"user-agent": f"pi-web-access/1.{index}"}}),
            )
        self.assertFalse(pi_web_access._carries_the_shared_user_agent(self.directory))
        rewritten = pi_web_access._normalize_staged_user_agents(self.directory)
        self.assertGreaterEqual(rewritten, 3)
        self.assertTrue(pi_web_access._carries_the_shared_user_agent(self.directory))


if __name__ == "__main__":
    unittest.main()

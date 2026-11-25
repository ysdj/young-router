"""Staged upstream packages are checked by the builds and by CI.

The desktop builds resolve third-party npm packages against their ``latest``
dist-tag, so an adapter can be broken by a release that never touches this
repository.  ``dsh-connect-workbuddy`` 5.31.0 changed ``fetchModels`` from a
roster to ``{ models, degraded }`` and the worker kept handing it to a catalog
that called ``models.map``: every route fell back to the bundled roster and
lost its 倍率.  The unit suite used a hand-written stub, and the artifact
builds checked exported names only, so nothing failed until the pane drew the
wrong list.

The rule these tests hold is the one that would have caught it: every stager
that resolves a ``latest`` release needs a verifier, and every artifact build
plus the scheduled upstream check must run that verifier.  A verifier is
allowed to be a seam check (Qoder) or to drive a call (WorkBuddy); what is not
allowed is a package that reaches a release build unverified.
"""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MAC_BUILD = SCRIPTS / "build-and-install-macos.sh"
WINDOWS_BUILD = ROOT / "rn" / "scripts" / "build-windows.ps1"
UPSTREAM_WORKFLOW = ROOT / ".github" / "workflows" / "upstream-contracts.yml"


class UpstreamContractWiringTests(unittest.TestCase):
    def test_every_latest_stager_has_a_verifier(self) -> None:
        """A package resolved from ``latest`` must state what the build checks."""

        stagers = sorted(path.stem.removeprefix("update_") for path in SCRIPTS.glob("update_connect_*.py"))
        self.assertEqual(stagers, ["connect_qoder", "connect_workbuddy"])
        for stager in stagers:
            verifier = SCRIPTS / f"verify_{stager}.mjs"
            self.assertTrue(verifier.is_file(), f"{stager} has no {verifier.name}")

    def test_every_desktop_build_runs_every_verifier(self) -> None:
        """Both hosts must gate on the same checks.

        Windows used to verify only that the WorkBuddy package directory
        existed while macOS checked its exports, so a release could pass one
        artifact build and fail the other.  Reading the same verifier by name
        in both scripts is what keeps the two lanes honest.
        """

        sources = {
            "macOS": MAC_BUILD.read_text(encoding="utf-8"),
            "Windows": WINDOWS_BUILD.read_text(encoding="utf-8"),
        }
        for verifier in sorted(SCRIPTS.glob("verify_connect_*.mjs")):
            for host, source in sources.items():
                self.assertIn(verifier.name, source, f"{host} never runs {verifier.name}")

    def test_the_scheduled_check_stages_every_connect_package(self) -> None:
        """The scheduled job is the only place a moving ``latest`` is seen early."""

        source = UPSTREAM_WORKFLOW.read_text(encoding="utf-8")
        for stager in sorted(SCRIPTS.glob("update_connect_*.py")):
            self.assertIn(stager.name, source, f"upstream check never stages {stager.name}")
        for verifier in sorted(SCRIPTS.glob("verify_connect_*.mjs")):
            self.assertIn(verifier.name, source, f"upstream check never runs {verifier.name}")

    def test_the_workbuddy_verifier_drives_the_catalog_seam(self) -> None:
        """The verifier's value is the call shape, not a second export list.

        The 5.31.0 break was invisible to an export check: every name still
        existed.  The verifier therefore has to call ``fetchModels`` and hold
        the parsed rate, which is what the worker hands to the 倍率 column.
        """

        source = (SCRIPTS / "verify_connect_workbuddy.mjs").read_text(encoding="utf-8")
        self.assertIn("new library.WorkBuddyUpstreamClient().fetchModels(", source)
        self.assertIn("creditMultiplier", source)
        self.assertIn("Array.isArray(read)", source)


if __name__ == "__main__":
    unittest.main()

"""The precision probe: staging, the worker contract, and the verdict shape.

The measurement itself belongs to the upstream package, so these tests do not
re-derive it.  What they pin is what this app is responsible for: that staging
refuses a release whose thresholds or exports moved, that the worker is driven
correctly, and that a result the library could not produce is never rendered as
a measurement.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from young_router.adapters import llm_fingerprint


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "update_llm_fingerprint.py"

FIXTURE_REVISION = "0123456789abcdef0123456789abcdef01234567"

FIXTURE_CONSTANTS = """
export const JSD_MATCH_THRESHOLD = 0.25
export const JSD_MISMATCH_THRESHOLD = 0.35
export const JSD_BASELINE_SELF = 0.14
export const JSD_BASELINE_CROSS_PROVIDER = 0.227
export const JSD_BASELINE_DIFFERENT_MODEL = 0.463
"""

FIXTURE_INDEX = """
export { fingerprint, compare, verify } from './api.js'
"""

FIXTURE_PACKAGE = json.dumps({"name": "llm-fingerprint-detector", "version": "0.1.0", "type": "module"})


def build_archive() -> bytes:
    """A tarball shaped like an upstream codeload archive."""

    import gzip
    import io
    import tarfile

    root = f"llm-fingerprint-detector-{FIXTURE_REVISION}"
    files = {
        "LICENSE": "MIT License\n",
        "package.json": FIXTURE_PACKAGE,
        "tsconfig.json": "{}\n",
        "src/index.ts": FIXTURE_INDEX,
        "src/constants.ts": FIXTURE_CONSTANTS,
        "src/api.ts": "export const x = 1\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, text in sorted(files.items()):
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return gzip.compress(buffer.getvalue())


def _load_updater():
    name = "update_llm_fingerprint"
    if name in sys.modules:
        return sys.modules[name]
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, UPDATER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class StagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_updater()
        self.files = self.module.extract(build_archive())

    def test_the_published_thresholds_are_read_from_the_release(self) -> None:
        boundaries = self.module.thresholds(self.files)

        self.assertEqual(0.25, boundaries["JSD_MATCH_THRESHOLD"])
        self.assertEqual(0.35, boundaries["JSD_MISMATCH_THRESHOLD"])

    def test_a_moved_threshold_fails_the_build(self) -> None:
        # The boundaries are the whole basis of a three-way verdict; a release
        # that moves one changes what a finding means.
        files = dict(self.files)
        files["src/constants.ts"] = FIXTURE_CONSTANTS.replace(
            "JSD_MATCH_THRESHOLD = 0.25", "JSD_MATCH_THRESHOLD = 0.30"
        ).encode("utf-8")

        with self.assertRaises(self.module.UpdateError) as raised:
            self.module.validate_sources(files)

        self.assertIn("JSD_MATCH_THRESHOLD", str(raised.exception))

    def test_a_missing_export_fails_the_build(self) -> None:
        files = dict(self.files)
        files["src/index.ts"] = b"export { fingerprint } from './api.js'\n"

        with self.assertRaises(self.module.UpdateError) as raised:
            self.module.validate_sources(files)

        self.assertIn("src/index.ts no longer exports", str(raised.exception))

    def test_staging_writes_the_manifest_and_the_thresholds(self) -> None:
        archive = Path(tempfile.mkdtemp()) / "archive.tar.gz"
        archive.write_bytes(build_archive())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "llm-fingerprint-detector"
            code = self.module.main(
                [
                    "--output",
                    str(output),
                    "--archive-url",
                    str(archive),
                    "--no-build",
                ]
            )

            self.assertEqual(0, code)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("0.1.0", manifest["version"])
            self.assertEqual(
                {"JSD_MATCH_THRESHOLD": 0.25, "JSD_MISMATCH_THRESHOLD": 0.35},
                manifest["thresholds"],
            )
            self.assertEqual("arXiv:2607.10252", manifest["paper"])
            self.assertTrue((output / "LICENSE").is_file())


class VersionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_updater()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "llm-fingerprint-detector"
        archive = Path(self.directory.name) / "archive.tar.gz"
        archive.write_bytes(build_archive())
        self.module.main(
            ["--output", str(self.output), "--archive-url", str(archive), "--no-build"]
        )

    def _check(self, head: str) -> int:
        with mock.patch.object(self.module, "upstream_head", return_value=head):
            return self.module.main(["--output", str(self.output), "--check"])

    def test_current_reports_current(self) -> None:
        self.assertEqual(0, self._check(FIXTURE_REVISION))

    def test_moved_upstream_reports_stale(self) -> None:
        self.assertEqual(3, self._check("f" * 40))

    def test_unreachable_upstream_is_its_own_answer(self) -> None:
        with mock.patch.object(self.module, "upstream_head", side_effect=OSError("offline")):
            self.assertEqual(4, self.module.main(["--output", str(self.output), "--check"]))


class VerdictShapeTests(unittest.TestCase):
    """A result the library could not produce is never a measurement."""

    def setUp(self) -> None:
        patcher = mock.patch.object(llm_fingerprint, "engine", return_value={"name": "x"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_capture_reports_captured_not_measured(self) -> None:
        payload = {
            "fingerprint": {"cells": {"a": {}}},
            "split_half_jsd": 0.03,
            "cells": 1,
            "samples_per_cell": 25,
        }

        result = llm_fingerprint._normalize(payload)

        self.assertEqual("captured", result["status"])
        self.assertEqual("", result["verdict"])
        self.assertIsNone(result["mean_jsd"])

    def test_a_comparison_carries_the_library_verdict_and_thresholds(self) -> None:
        payload = {
            "fingerprint": {"cells": {"a": {}}},
            "split_half_jsd": 0.04,
            "cells": 8,
            "comparison": {
                "verdict": "match",
                "meanJsd": 0.0775,
                "comparableCellCount": 8,
                "thresholds": {"match": 0.25, "mismatch": 0.35},
                "baselines": {"sameModelSelf": 0.14, "differentModel": 0.463},
                "protocolMismatch": False,
            },
        }

        result = llm_fingerprint._normalize(payload)

        self.assertEqual("measured", result["status"])
        self.assertEqual("match", result["verdict"])
        self.assertEqual(0.0775, result["mean_jsd"])
        self.assertEqual({"match": 0.25, "mismatch": 0.35}, result["thresholds"])
        self.assertTrue(result["self_consistent"])

    def test_an_endpoint_that_disagrees_with_itself_is_flagged(self) -> None:
        # The library's own split-half readout: a run whose halves differ by
        # more than the match threshold is not a fingerprint of anything.
        payload = {
            "fingerprint": {"cells": {"a": {}}},
            "split_half_jsd": 0.42,
            "comparison": {
                "verdict": "match",
                "meanJsd": 0.1,
                "comparableCellCount": 8,
                "thresholds": {"match": 0.25, "mismatch": 0.35},
            },
        }

        result = llm_fingerprint._normalize(payload)

        self.assertFalse(result["self_consistent"])

    def test_an_unknown_verdict_is_not_translated(self) -> None:
        payload = {
            "fingerprint": {"cells": {"a": {}}},
            "comparison": {"verdict": "definitely-lying", "meanJsd": 0.9, "comparableCellCount": 8},
        }

        result = llm_fingerprint._normalize(payload)

        self.assertEqual("insufficient", result["verdict"])




class BudgetTests(unittest.TestCase):
    """The probe must fit the wall-clock budget it is held to.

    Measured on api.deepseek.com: one call costs about 0.8 s at concurrency 8,
    so a run of ``cells x samplesPerCell`` calls takes about that many tenths of
    a second.  The defaults are sized from that measurement, and this test is
    what stops them from creeping back up to the library's own preset.
    """

    def setUp(self) -> None:
        patcher = mock.patch.object(llm_fingerprint, "engine", wraps=llm_fingerprint.engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_default_run_fits_the_budget(self) -> None:
        engine = llm_fingerprint.engine()

        calls = engine["cells"] * engine["samples_per_cell"]
        # 0.8 s per call at concurrency 8 is the measured cost; anything under
        # the budget with that rate leaves room for a slower endpoint.
        estimated = calls * 0.8 / engine["concurrency"]

        self.assertLessEqual(
            estimated,
            engine["budget_seconds"],
            f"{calls} calls would take ~{estimated:.1f}s, over the {engine['budget_seconds']}s budget",
        )

    def test_the_default_run_can_still_render_a_verdict(self) -> None:
        # The library's floors: it compares cells with >= 10 valid samples each
        # and needs at least 4 comparable cells before it states a verdict.
        engine = llm_fingerprint.engine()

        self.assertGreaterEqual(engine["cells"], 4)
        self.assertGreaterEqual(engine["samples_per_cell"], 10)

    def test_the_default_run_can_measure_self_consistency(self) -> None:
        # The split-half readout needs 5 samples per half; a run below that
        # produces no self-consistency figure at all, which would leave the
        # verdict unqualified.
        engine = llm_fingerprint.engine()

        self.assertGreaterEqual(engine["samples_per_cell"], 10)


class UnstagedTests(unittest.TestCase):
    """An unstaged build reports itself instead of raising at import."""

    def test_an_unstaged_checkout_reports_itself(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {llm_fingerprint._DIR_ENV: str(Path(tempfile.mkdtemp()) / "absent")},
        ):
            self.assertFalse(llm_fingerprint.available())
            self.assertFalse(llm_fingerprint.engine()["available"])
            with self.assertRaises(llm_fingerprint.FingerprintUnavailable):
                llm_fingerprint.verify(
                    api_base="https://example.test/v1",
                    api_key="secret",
                    model="deepseek-flash",
                    reference={"cells": {"a": {}}},
                )


if __name__ == "__main__":
    unittest.main()

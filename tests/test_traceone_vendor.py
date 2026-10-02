"""TraceOne staging and worker plumbing for the degradation deep test."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest import mock

from young_router.adapters import traceone


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts/update_traceone.py"

FIXTURE_REVISION = "0123456789abcdef0123456789abcdef01234567"

FIXTURE_MODULE = textwrap.dedent(
    """
    export const TARGET_MODELS = ["gpt-5.6-sol", "gpt-6-astra"];

    const VALUE_MIN = 1;
    const VALUE_MAX = 355;

    export function parseGridResponse(text) {
      let payload;
      try {
        payload = JSON.parse(text);
      } catch (error) {
        return { numbers: [], valid: false, errors: ["invalid_json"], observedItems: 0, integerItems: 0 };
      }
      if (!Array.isArray(payload)) {
        return { numbers: [], valid: false, errors: ["missing_grid_array"], observedItems: 0, integerItems: 0 };
      }
      const flattened = payload.flat();
      const numbers = flattened.filter((value) => Number.isInteger(value) && value >= VALUE_MIN && value <= VALUE_MAX);
      return { numbers, valid: numbers.length === 315, errors: [], observedItems: flattened.length, integerItems: flattened.length };
    }

    export function identifyWithArtifacts(text, { bank, adapter, support }) {
      const parsed = parseGridResponse(text);
      const label = parsed.numbers.length > 0 ? Object.keys(bank.models ?? {})[0] ?? "gpt-6-astra" : null;
      return {
        status: parsed.numbers.length > 0 ? "identified" : "unknown",
        label,
        supportPassed: true,
        supportDistance: 0,
        supportThreshold: 1,
        supportPValue: 0.5,
        supportPath: "distance",
        adapter: {},
        parsed,
      };
    }
    """
).lstrip()

FIXTURE_BANK = {"schema": "fixture", "models": {"gpt-6-astra": {"family": "gpt"}}}


def build_archive(
    destination: Path,
    *,
    module_text: str = FIXTURE_MODULE,
    prompt: str = "produce 315 integers\n",
    data_files: dict[str, str] | None = None,
) -> Path:
    """A minimal TraceOne archive with the layout the updater reads."""

    documents = data_files if data_files is not None else {
        "unified_bank.json": json.dumps(FIXTURE_BANK),
        "codex_low_v4_adapter_415.json": '{"targets": []}',
        "codex_low_v4_support_415.json": '{"support": {}}',
    }
    with tarfile.open(destination, "w:gz") as archive:
        def add(name: str, payload: bytes) -> None:
            info = tarfile.TarInfo(f"TraceOne-{FIXTURE_REVISION}/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

        add("dist/traceone.js", module_text.encode("utf-8"))
        for name, document in documents.items():
            add(f"dist/data/{name}", document.encode("utf-8"))
        add("prompts/identity-web-v1.txt", prompt.encode("utf-8"))
    return destination


class TraceOneVendorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="traceone-test-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.archive = build_archive(Path(self.directory) / "traceone.tar.gz")

    def run_updater(self, output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(UPDATER), "--output", str(output), "--archive-url", str(self.archive), *extra],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_staging_copies_the_module_artifacts_and_manifest(self) -> None:
        output = Path(self.directory) / "staged"
        completed = self.run_updater(output)
        self.assertEqual(0, completed.returncode, completed.stderr)
        for name in (
            "traceone.js",
            "prompt.txt",
            "manifest.json",
            "data/unified_bank.json",
            "data/codex_low_v4_adapter_415.json",
            "data/codex_low_v4_support_415.json",
        ):
            self.assertTrue((output / name).is_file(), name)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(FIXTURE_REVISION, manifest["revision"])
        self.assertEqual(["gpt-5.6-sol", "gpt-6-astra"], manifest["target_models"])
        self.assertEqual("https://github.com/wangchao0708/TraceOne", manifest["source"])
        self.assertEqual(64, len(manifest["archive_sha256"]))
        self.assertEqual(set(manifest["files"]), {name for name in manifest["files"]})

    def test_staging_fails_when_the_archive_drifts_from_the_adapter(self) -> None:
        broken = build_archive(
            Path(self.directory) / "broken.tar.gz",
            module_text=(
                "export function parseGridResponse() { return {}; }\n"
                "export function identifyWithArtifacts() { return {}; }\n"
            ),
        )
        completed = subprocess.run(
            [sys.executable, str(UPDATER), "--output", str(Path(self.directory) / "broken"), "--archive-url", str(broken)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("TARGET_MODELS", completed.stderr + completed.stdout)

    def test_staging_accepts_the_two_document_release_without_a_support_artifact(self) -> None:
        """The 2026-10 release folded the support statistics into the adapter."""

        module = textwrap.dedent(
            """
            export const TARGET_MODELS = ["gpt-6-astra"];
            export function parseGridResponse(text) { return {}; }
            export function identifyWithArtifacts(text, artifacts) { return { status: "unknown" }; }
            const documents = [
              new URL("./data/unified_bank_v2_16.json", import.meta.url),
              new URL("./data/codex_low_v8_optimized.json", import.meta.url),
            ];
            """
        ).lstrip()
        archive = build_archive(
            Path(self.directory) / "optimized.tar.gz",
            module_text=module,
            data_files={
                "unified_bank_v2_16.json": json.dumps({"schema": "robust-number-fingerprint-bank", "models": {}}),
                "codex_low_v8_optimized.json": json.dumps(
                    {
                        "schema": "traceone-sequence-adapter-v1",
                        "models": ["gpt-6-astra"],
                        "support": {"thresholds": [1.0], "calibration_distances": [[0.1]]},
                    }
                ),
                "unrelated_release_notes.json": json.dumps({"schema": "notes"}),
            },
        )
        output = Path(self.directory) / "optimized"
        completed = subprocess.run(
            [sys.executable, str(UPDATER), "--output", str(output), "--archive-url", str(archive), "--no-smoke-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
        self.assertTrue((output / "data/unified_bank_v2_16.json").is_file())
        self.assertTrue((output / "data/codex_low_v8_optimized.json").is_file())
        # Only the documents the classifier plays a role in, or reads, are staged.
        self.assertFalse((output / "data/unrelated_release_notes.json").exists())

    def test_staging_fails_when_the_module_reads_a_document_the_archive_lacks(self) -> None:
        module = textwrap.dedent(
            """
            export const TARGET_MODELS = ["gpt-6-astra"];
            export function parseGridResponse(text) { return {}; }
            export function identifyWithArtifacts(text, artifacts) { return { status: "unknown" }; }
            const documents = [new URL("./data/codex_low_v9_optimized.json", import.meta.url)];
            """
        ).lstrip()
        archive = build_archive(
            Path(self.directory) / "dangling.tar.gz",
            module_text=module,
            data_files={
                "unified_bank_v3_1.json": json.dumps({"schema": "robust-number-fingerprint-bank", "models": {}}),
                "codex_low_v8_optimized.json": json.dumps(
                    {"schema": "traceone-sequence-adapter-v1", "models": ["gpt-6-astra"], "support": {}}
                ),
            },
        )
        completed = subprocess.run(
            [sys.executable, str(UPDATER), "--output", str(Path(self.directory) / "dangling"), "--archive-url", str(archive), "--no-smoke-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("does not carry", completed.stderr + completed.stdout)

    def test_manifest_drives_the_route_lookup(self) -> None:
        output = Path(self.directory) / "staged"
        self.assertEqual(0, self.run_updater(output, "--no-smoke-test").returncode)
        with mock.patch.dict(os.environ, {traceone._TRACEONE_DIR_ENV: str(output)}):
            self.assertEqual(("gpt-5.6-sol", "gpt-6-astra"), traceone.target_models())
            self.assertEqual("gpt-6-astra", traceone.route_target("openai/gpt-6-astra"))
            self.assertEqual("gpt-6-astra", traceone.route_target("[次]GPT-6-Astra"))
            self.assertEqual("gpt-5.6-sol", traceone.route_target("other", "gpt-5.6-sol"))
            self.assertIsNone(traceone.route_target("gpt-image-2"))
            self.assertIsNone(traceone.route_target(None, 42))
            self.assertIn("315 integers", traceone.prompt_text())
            self.assertEqual("TraceOne", traceone.engine()["name"])
            self.assertEqual(FIXTURE_REVISION, traceone.engine()["revision"])

    def test_unstaged_engine_reports_unavailable_instead_of_raising(self) -> None:
        with mock.patch.dict(os.environ, {traceone._TRACEONE_DIR_ENV: str(Path(self.directory) / "missing")}):
            self.assertFalse(traceone.available())
            self.assertFalse(traceone.engine()["available"])
            with self.assertRaises(traceone.TraceOneUnavailable):
                traceone.identify("[]")
            with self.assertRaises(traceone.TraceOneUnavailable):
                traceone.prompt_text()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the TraceOne worker")
    def test_worker_attributes_one_answer_through_the_staged_module(self) -> None:
        output = Path(self.directory) / "staged"
        self.assertEqual(0, self.run_updater(output, "--no-smoke-test").returncode)
        answer = json.dumps([[((index * 7 + row) % 355) + 1 for index in range(35)] for row in range(9)])
        with mock.patch.dict(os.environ, {traceone._TRACEONE_DIR_ENV: str(output)}):
            self.assertTrue(traceone.available())
            decision = traceone.identify(answer)
        self.assertEqual("identified", decision["status"])
        self.assertEqual("gpt-6-astra", decision["label"])
        self.assertEqual(315, decision["numbers"])
        self.assertEqual([], decision["errors"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the TraceOne worker")
    def test_worker_reports_a_non_grid_answer_as_unknown(self) -> None:
        output = Path(self.directory) / "staged"
        self.assertEqual(0, self.run_updater(output, "--no-smoke-test").returncode)
        with mock.patch.dict(os.environ, {traceone._TRACEONE_DIR_ENV: str(output)}):
            decision = traceone.identify("I cannot do that.")
        self.assertEqual("unknown", decision["status"])
        self.assertIsNone(decision["label"])
        self.assertEqual(0, decision["numbers"])
        self.assertTrue(decision["errors"])


if __name__ == "__main__":
    unittest.main()

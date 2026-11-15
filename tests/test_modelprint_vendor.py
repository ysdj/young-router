"""modelprint staging, probe table, and attribution for the deep test.

The staged tables are data, so these tests take two bites: the stager is fed a
tarball built in the test (never the network), and the adapter is pointed at the
directory that produced - or at a fixture built to look like one.
"""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from young_router.adapters import modelprint


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "update_modelprint.py"

FIXTURE_REVISION = "0123456789abcdef0123456789abcdef01234567"

# The pinned texts are byte-exact in upstream.  The fixture keeps the two that
# carry a concatenation, because that is the declaration the stager has to read
# without eval'ing a fetched archive.
FIXTURE_PROBES = {
    "tok-english.js": '''
const TEXT = "The quick brown fox jumps over the lazy dog "
  + "while packing my box with five dozen liquor jugs.";
''',
    "tok-chinese.js": '''
const TEXT = "机器学习模型将文本切分为词元。"
  + "每个实验室的切分方式都不同。";
''',
    "tok-code.js": """
const TEXT = 'def fingerprint(endpoint: str) -> dict:\\n'
  + '    return {};\\n';
""",
    "tok-emoji.js": '''
const TEXT = "🦀🚀🧠 你好世界 مرحبا שלום";
''',
}

FIXTURE_FINGERPRINTS = {
    "openai-gpt-5.6-sol__openai.json": {
        "model": "openai/gpt-5.6-sol",
        "host": "OpenAI",
        "first_tracked": "2026-08-21",
        "versions": [
            {
                "v": 1,
                "first_seen": "2026-08-21",
                "last_seen": "2026-09-01",
                "probe_set": "0.8.0",
                "values": {"tok-english": "40", "tok-chinese": "44"},
            },
            {
                "v": 2,
                "first_seen": "2026-09-02",
                "last_seen": "2026-10-01",
                "probe_set": "0.8.0",
                "values": {
                    "tok-english": "44",
                    "tok-chinese": "51",
                    "tok-code": "58",
                    "tok-emoji": "77",
                    "template-offset": "+9 (delta 7)",
                    "err-temperature": "accepts 2.0 (ceiling >= 2)",
                    "err-code-family": "string-code · type:invalid_request_error",
                    "finish-vocab": "stop · length",
                    "lp-geometry": "δ=0.32 · span=0.04 · k=10",
                },
            },
        ],
    },
    "deepseek-deepseek-v4-flash__siliconflow.json": {
        "model": "deepseek/deepseek-v4-flash",
        "host": "SiliconFlow",
        "first_tracked": "2026-08-21",
        "versions": [
            {
                "v": 1,
                "first_seen": "2026-08-21",
                "last_seen": "2026-10-01",
                "probe_set": "0.8.0",
                "values": {
                    "tok-english": "49",
                    "tok-chinese": "53",
                    "tok-code": "62",
                    "tok-emoji": "83",
                    "template-offset": "+5 (delta 7)",
                    "err-temperature": "The temperature parameter is illegal · code 1210",
                    "err-code-family": "numeric-code",
                    "finish-vocab": "stop · length",
                    "lp-geometry": "logprobs-unsupported",
                },
            }
        ],
    },
    "stealth-ox-alpha__stealth.json": {
        "model": "stealth/ox-alpha",
        "host": "Stealth",
        "first_tracked": "2026-08-21",
        "versions": [
            {
                "v": 1,
                "first_seen": "2026-08-21",
                "last_seen": "2026-10-01",
                "probe_set": "0.8.0",
                "values": {"tok-english": "47", "tok-chinese": "56"},
            }
        ],
    },
}


def build_archive() -> bytes:
    """A tarball shaped like an upstream codeload archive."""

    root = f"modelprint-{FIXTURE_REVISION}"
    files: dict[str, str] = {
        "VERSION": "0.8.0\n",
        "LICENSE": "MIT License\n",
        "probes/index.js": "export const REGISTRY = [];\n",
        "fingerprints/index.json": json.dumps({"models": []}),
    }
    for name, source in FIXTURE_PROBES.items():
        files[f"probes/{name}"] = source
    for name, document in FIXTURE_FINGERPRINTS.items():
        files[f"fingerprints/{name}"] = json.dumps(document)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, text in sorted(files.items()):
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return gzip.compress(buffer.getvalue())


class ModelPrintStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = build_archive()
        self.module = _load_updater()

    def test_the_probe_texts_are_read_out_of_their_declarations(self) -> None:
        files = self.module.extract(self.archive)
        texts, digests = self.module.pinned_texts(files)

        self.assertEqual(
            "The quick brown fox jumps over the lazy dog "
            "while packing my box with five dozen liquor jugs.",
            texts["tok-english"],
        )
        self.assertEqual(
            "机器学习模型将文本切分为词元。每个实验室的切分方式都不同。",
            texts["tok-chinese"],
        )
        self.assertEqual("🦀🚀🧠 你好世界 مرحبا שלום", texts["tok-emoji"])
        self.assertTrue(texts["tok-code"].startswith("def fingerprint"))
        for name, text in texts.items():
            self.assertEqual(
                self.module.hashlib.sha256(text.encode("utf-8")).hexdigest(),
                digests[name],
            )

    def test_a_probe_without_a_declaration_fails_the_build(self) -> None:
        files = self.module.extract(self.archive)
        files.pop("probes/tok-english.js")

        with self.assertRaises(self.module.UpdateError):
            self.module.pinned_texts(files)

    def test_only_the_newest_recorded_version_is_staged(self) -> None:
        files = self.module.extract(self.archive)
        entries = self.module.baselines(files)

        entry = entries["openai-gpt-5.6-sol__openai"]
        self.assertEqual(2, entry["recorded_version"])
        self.assertEqual("44", entry["values"]["tok-english"])
        # The first version's value must not survive beside it.
        self.assertNotIn("40", entry["values"].values())

    def test_an_official_baseline_is_the_vendors_own_endpoint(self) -> None:
        files = self.module.extract(self.archive)
        entries = self.module.baselines(files)

        self.assertTrue(self.module.is_official(entries["openai-gpt-5.6-sol__openai"]))
        # SiliconFlow is a reseller of DeepSeek, not DeepSeek.
        self.assertFalse(self.module.is_official(entries["deepseek-deepseek-v4-flash__siliconflow"]))
        # Nobody's own endpoint is stealth.
        self.assertFalse(self.module.is_official(entries["stealth-ox-alpha__stealth"]))

    def test_staging_writes_the_baselines_the_manifest_and_the_license(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "modelprint"
            code = self.module.main(
                [
                    "--output",
                    str(output),
                    "--archive-url",
                    str(self._archive_path()),
                ]
            )

            self.assertEqual(0, code)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("0.8.0", manifest["probe_set"])
            self.assertEqual(
                ["openai-gpt-5.6-sol__openai"], manifest["official_baselines"]
            )
            self.assertEqual(
                ["deepseek-v4-flash", "gpt-5.6-sol", "ox-alpha"],
                manifest["supported_models"]["openai"],
            )
            self.assertTrue((output / "LICENSE").is_file())
            self.assertTrue((output / "baselines" / "stealth-ox-alpha__stealth.json").is_file())
            self.assertTrue((output / "upstream" / "probes" / "tok-english.js").is_file())

            # The staged tables verify against themselves, which is what the
            # adapter checks before it will run.
            with mock.patch.dict("os.environ", {modelprint._DIR_ENV: str(output)}):
                self.assertTrue(modelprint.available())
                self.assertTrue(modelprint.texts_verified())

    def _archive_path(self) -> Path:
        handle = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        handle.write(self.archive)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)


class ModelPrintCapturedBaselineTests(unittest.TestCase):
    """Fingerprints this repository captured from a vendor's own endpoint."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        output = Path(self.directory.name) / "modelprint"
        updater = _load_updater()
        archive = Path(self.directory.name) / "archive.tar.gz"
        archive.write_bytes(build_archive())
        updater.main(["--output", str(output), "--archive-url", str(archive)])
        captured = Path(self.directory.name) / "captured"
        captured.mkdir()
        (captured / "deepseek-deepseek-flash__deepseek-official.json").write_text(
            json.dumps(
                {
                    "model": "deepseek-flash",
                    "host": "DeepSeek",
                    "protocol": "openai",
                    "values": {"tok-english": "49", "tok-chinese": "53"},
                }
            ),
            encoding="utf-8",
        )
        patcher = mock.patch.dict(
            "os.environ",
            {
                modelprint._DIR_ENV: str(output),
                modelprint._CAPTURED_ENV: str(captured),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_captured_fingerprint_is_offered_beside_the_staged_ones(self) -> None:
        self.assertIn(
            "deepseek-deepseek-flash__deepseek-official", modelprint.baselines()
        )

    def test_a_captured_fingerprint_counts_as_the_vendors_own(self) -> None:
        self.assertIn(
            "deepseek-deepseek-flash__deepseek-official",
            modelprint.official_baseline_names(),
        )

    def test_the_vendors_own_entry_is_preferred_over_a_resellers(self) -> None:
        # The staged tracker only carries a reseller's DeepSeek; the captured
        # entry is the one a route should be judged against.
        candidates = [stem for stem, _ in modelprint._comparable_baselines("deepseek-flash")]
        self.assertEqual("deepseek-deepseek-flash__deepseek-official", candidates[0])

    def test_a_captured_stem_that_collides_keeps_both(self) -> None:
        staged = Path(modelprint.staged_root()) / "baselines"
        target = staged / "deepseek-deepseek-flash__deepseek-official.json"
        target.write_text(
            json.dumps({"model": "deepseek-flash", "host": "Tracker", "values": {"tok-english": "1"}}),
            encoding="utf-8",
        )
        self.addCleanup(target.unlink, missing_ok=True)

        entries = modelprint.baselines()

        # The staged document is read first and keeps its own stem; the captured
        # one is the one that has to move, because it is the file this build
        # added rather than the one upstream published.
        self.assertIn("deepseek-deepseek-flash__deepseek-official", entries)
        self.assertIn("deepseek-deepseek-flash__deepseek-official-captured", entries)
        self.assertEqual("Tracker", entries["deepseek-deepseek-flash__deepseek-official"]["host"])
        self.assertEqual("DeepSeek", entries["deepseek-deepseek-flash__deepseek-official-captured"]["host"])


class ModelPrintSameVendorTests(unittest.TestCase):
    """Two models of one family share a tokenizer; only some rows separate them."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        output = Path(self.directory.name) / "modelprint"
        updater = _load_updater()
        archive = Path(self.directory.name) / "archive.tar.gz"
        archive.write_bytes(build_archive())
        updater.main(["--output", str(output), "--archive-url", str(archive)])
        captured = Path(self.directory.name) / "captured"
        captured.mkdir()
        # Two models of one vendor, measured on the vendor's own endpoint.  The
        # tokenizer rows are identical because the family shares a tokenizer;
        # the serving template is what tells the flagship from the small one.
        for name, offset in (
            ("deepseek-deepseek-flash__deepseek-official", "+31 (delta 7)"),
            ("deepseek-deepseek-v4-pro__deepseek-official", "+84 (delta 7)"),
        ):
            (captured / f"{name}.json").write_text(
                json.dumps(
                    {
                        "model": "deepseek-flash" if "flash" in name else "deepseek-v4-pro",
                        "host": "DeepSeek",
                        "protocol": "openai",
                        "values": {
                            "tok-english": "49",
                            "tok-chinese": "53",
                            "tok-code": "62",
                            "tok-emoji": "83",
                            "template-offset": offset,
                            "finish-vocab": "stop · length",
                        },
                    }
                ),
                encoding="utf-8",
            )
        patcher = mock.patch.dict(
            "os.environ",
            {
                modelprint._DIR_ENV: str(output),
                modelprint._CAPTURED_ENV: str(captured),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_as(self, claimed: str, values: dict) -> dict:
        with mock.patch.object(
            modelprint,
            "_run_probe",
            side_effect=lambda _c, name, _t: modelprint._row(str(values.get(name, ""))),
        ):
            return modelprint.run(
                api_base="https://example.test/v1",
                api_key="secret",
                model=claimed,
                protocol="openai",
            )

    def test_a_sibling_answering_for_the_flagship_is_named(self) -> None:
        flash = {
            "tok-english": "49",
            "tok-chinese": "53",
            "tok-code": "62",
            "tok-emoji": "83",
            "template-offset": "+31 (delta 7)",
            "finish-vocab": "stop · length",
        }

        outcome = self._run_as("deepseek-v4-pro", flash)

        self.assertEqual("failed", outcome["verdict"])
        self.assertEqual("deepseek-flash", outcome["label"])

    def test_the_flagship_answering_for_itself_passes(self) -> None:
        pro = {
            "tok-english": "49",
            "tok-chinese": "53",
            "tok-code": "62",
            "tok-emoji": "83",
            "template-offset": "+84 (delta 7)",
            "finish-vocab": "stop · length",
        }

        outcome = self._run_as("deepseek-v4-pro", pro)

        self.assertEqual("passed", outcome["verdict"])
        self.assertEqual("deepseek-v4-pro", outcome["label"])

    def test_an_unmeasured_decisive_row_names_nothing_within_a_vendor(self) -> None:
        # The template probe is the one row that separates siblings.  When the
        # endpoint refuses it, both siblings match every remaining row, and
        # naming either one would be a guess dressed as a finding.
        values = {
            "tok-english": "49",
            "tok-chinese": "53",
            "tok-code": "62",
            "tok-emoji": "83",
            "template-offset": "probe-failed: 400",
            "finish-vocab": "stop · length",
        }

        outcome = self._run_as("deepseek-v4-pro", values)

        self.assertEqual("marginal", outcome["verdict"])
        self.assertIsNone(outcome["label"])

    def test_counts_that_match_nothing_are_not_attributed_to_a_sibling(self) -> None:
        # A route whose tokenizer rows match neither DeepSeek baseline must not
        # be folded onto one of them just because they are the only entries on
        # file: that is how a route serving an unlisted model would be reported
        # as the flagship it was bought as.
        values = {
            "tok-english": "47",
            "tok-chinese": "56",
            "tok-code": "60",
            "tok-emoji": "90",
            "template-offset": "+13 (delta 7)",
            "finish-vocab": "stop · length",
        }

        outcome = self._run_as("deepseek-v4-pro", values)

        self.assertEqual("marginal", outcome["verdict"])
        self.assertIsNone(outcome["label"])


class ModelPrintVersionGateTests(unittest.TestCase):
    """A build must check upstream before it stages, and say what it found."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "modelprint"
        self.module = _load_updater()

    def _stage(self) -> None:
        archive = Path(self.directory.name) / "archive.tar.gz"
        archive.write_bytes(build_archive())
        self.module.main(["--output", str(self.output), "--archive-url", str(archive)])

    def _check(self, head: str) -> int:
        with mock.patch.object(self.module, "upstream_head", return_value=head):
            return self.module.main(["--output", str(self.output), "--check"])

    def test_nothing_staged_is_not_a_reason_to_stop_a_build(self) -> None:
        with mock.patch.object(self.module, "upstream_head", return_value=FIXTURE_REVISION):
            self.assertEqual(0, self.module.main(["--output", str(self.output), "--check"]))

    def test_a_current_staging_reports_current(self) -> None:
        self._stage()
        self.assertEqual(0, self._check(FIXTURE_REVISION))

    def test_a_moved_upstream_reports_stale(self) -> None:
        self._stage()
        self.assertEqual(3, self._check("f" * 40))

    def test_an_unreachable_upstream_is_its_own_answer(self) -> None:
        self._stage()
        with mock.patch.object(self.module, "upstream_head", side_effect=OSError("offline")):
            self.assertEqual(4, self.module.main(["--output", str(self.output), "--check"]))

    def test_a_staging_without_a_revision_is_treated_as_behind(self) -> None:
        # An offline or rate-limited build records no revision; it cannot be
        # shown to be current, so the next build restages instead of trusting it.
        self._stage()
        manifest = self.output / "manifest.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["revision"] = ""
        manifest.write_text(json.dumps(document), encoding="utf-8")

        self.assertEqual(3, self._check(FIXTURE_REVISION))


class ModelPrintPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        output = Path(self.directory.name) / "modelprint"
        updater = _load_updater()
        archive = Path(self.directory.name) / "archive.tar.gz"
        archive.write_bytes(build_archive())
        updater.main(["--output", str(output), "--archive-url", str(archive)])
        patcher = mock.patch.dict("os.environ", {modelprint._DIR_ENV: str(output)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_model_that_is_not_a_claude_name_speaks_the_chat_wire(self) -> None:
        self.assertEqual("anthropic", modelprint.protocol_for("claude-opus-5"))
        self.assertEqual("openai", modelprint.protocol_for("gpt-5.6-sol"))
        self.assertEqual("openai", modelprint.protocol_for("deepseek-v4-flash"))
        self.assertEqual("openai", modelprint.protocol_for("space-bunny"))
        self.assertEqual("", modelprint.protocol_for(""))

    def test_a_name_is_matched_across_vendor_prefixes_and_date_pins(self) -> None:
        self.assertTrue(modelprint.same_model("gpt-5.6-sol", "openai/gpt-5.6-sol"))
        self.assertTrue(modelprint.same_model("deepseek-v4.1-flash", "deepseek-v4-1-flash"))
        self.assertTrue(modelprint.same_model("glm-5.3", "glm-5-3-0731"))
        self.assertFalse(modelprint.same_model("gpt-5.6-sol", "gpt-5.6-luna"))

    def test_a_model_without_a_baseline_is_not_promised_a_verdict(self) -> None:
        plan = modelprint.plan(model="space-bunny", api_base="https://example.test/v1")

        self.assertFalse(plan["includes_degradation"])
        self.assertEqual("no-baseline", plan["reason"])
        self.assertEqual([], plan["probes"])

    def test_a_reference_endpoint_makes_an_unlisted_model_probeable(self) -> None:
        plan = modelprint.plan(
            model="space-bunny",
            api_base="https://example.test/v1",
            reference_configured=True,
        )

        self.assertTrue(plan["includes_degradation"])
        self.assertEqual("openai", plan["protocol"])
        self.assertEqual("openai/chat", plan["surface"])
        self.assertIn("tok-english", plan["probes"])

    def test_the_official_baseline_is_preferred_over_a_resellers(self) -> None:
        candidates = [stem for stem, _ in modelprint._comparable_baselines("gpt-5.6-sol")]
        self.assertEqual(["openai-gpt-5.6-sol__openai"], candidates)


class ModelPrintAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        output = Path(self.directory.name) / "modelprint"
        updater = _load_updater()
        archive = Path(self.directory.name) / "archive.tar.gz"
        archive.write_bytes(build_archive())
        updater.main(["--output", str(output), "--archive-url", str(archive)])
        patcher = mock.patch.dict("os.environ", {modelprint._DIR_ENV: str(output)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_route_that_answers_like_the_other_model_is_named(self) -> None:
        """The whole point: a mismatch is reported as the model that answered."""

        # gpt-5.6-sol was requested; the endpoint answers with the DeepSeek
        # rows.  Naming gpt-5.6-sol would be a lie about a route that is not it.
        observed = dict(
            json.loads(
                (Path(modelprint.staged_root()) / "baselines" / "deepseek-deepseek-v4-flash__siliconflow.json").read_text()
            )["values"]
        )
        with mock.patch.object(modelprint, "_run_probe", side_effect=_replay(observed)), mock.patch.object(
            modelprint, "_comparable_baselines", side_effect=_all_baselines
        ):
            outcome = modelprint.run(
                api_base="https://example.test/v1",
                api_key="secret",
                model="gpt-5.6-sol",
                protocol="openai",
            )

        self.assertEqual("failed", outcome["verdict"])
        self.assertEqual("deepseek/deepseek-v4-flash", outcome["label"])
        self.assertEqual("baseline", outcome["label_source"])
        self.assertIn("deepseek/deepseek-v4-flash", outcome["summary"])
        self.assertEqual("openai", outcome["protocol"])

    def test_a_route_that_matches_its_own_baseline_passes(self) -> None:
        observed = dict(
            json.loads(
                (Path(modelprint.staged_root()) / "baselines" / "deepseek-deepseek-v4-flash__siliconflow.json").read_text()
            )["values"]
        )
        with mock.patch.object(modelprint, "_run_probe", side_effect=_replay(observed)):
            outcome = modelprint.run(
                api_base="https://example.test/v1",
                api_key="secret",
                model="deepseek-v4-flash",
                protocol="openai",
            )

        self.assertEqual("passed", outcome["verdict"])
        self.assertEqual("deepseek/deepseek-v4-flash", outcome["label"])

    def test_a_reference_fingerprint_names_the_model_without_a_staged_baseline(self) -> None:
        reference = {
            "model": "space-bunny",
            "values": {
                "tok-english": "47",
                "tok-chinese": "56",
                "tok-code": "60",
                "tok-emoji": "90",
                "template-offset": "+13 (delta 7)",
                "err-temperature": "accepts 2.0 (ceiling >= 2)",
                "finish-vocab": "stop · length",
            },
        }
        with mock.patch.object(modelprint, "_run_probe", side_effect=_replay(reference["values"])), mock.patch.object(
            modelprint, "_comparable_baselines", return_value=[]
        ):
            outcome = modelprint.run(
                api_base="https://example.test/v1",
                api_key="secret",
                model="space-bunny",
                protocol="openai",
                reference=reference,
            )

        self.assertEqual("passed", outcome["verdict"])
        self.assertEqual("space-bunny", outcome["label"])
        self.assertEqual("reference", outcome["label_source"])

    def test_an_unmeasured_row_never_counts_as_a_difference(self) -> None:
        observed = {
            "tok-english": "model busy (rate limited)",
            "tok-chinese": "logprobs-unsupported",
            "tok-code": "unstable (multi-host routing)",
            "finish-vocab": "stop · length",
        }
        recorded = {
            "tok-english": "49",
            "tok-chinese": "53",
            "tok-code": "62",
            "finish-vocab": "stop · length",
        }

        result = modelprint.compare(observed, recorded)

        self.assertEqual(["finish-vocab"], result["matches"])
        self.assertEqual([], result["differences"])
        self.assertEqual(1, result["total"])

    def test_a_route_that_reports_no_measurement_names_nothing(self) -> None:
        with mock.patch.object(
            modelprint,
            "_run_probe",
            side_effect=lambda *_a, **_k: modelprint._row("model busy (rate limited)"),
        ):
            outcome = modelprint.run(
                api_base="https://example.test/v1",
                api_key="secret",
                model="gpt-5.6-sol",
                protocol="openai",
            )

        self.assertEqual("marginal", outcome["verdict"])
        self.assertIsNone(outcome["label"])
        self.assertEqual(0.0, outcome["score"])

    def test_an_unstaged_checkout_reports_itself_instead_of_raising(self) -> None:
        with mock.patch.dict("os.environ", {modelprint._DIR_ENV: str(Path(self.directory.name) / "absent")}):
            self.assertFalse(modelprint.available())
            self.assertFalse(modelprint.engine()["available"])
            with self.assertRaises(modelprint.ModelPrintUnavailable):
                modelprint.run(
                    api_base="https://example.test/v1",
                    api_key="secret",
                    model="gpt-5.6-sol",
                    protocol="openai",
                )


def _replay(values: dict) -> object:
    """A probe table that answers with recorded values, keyed by probe name."""

    def run_probe(_client: object, name: str, _texts: object) -> dict:
        return modelprint._row(str(values.get(name, "")), None)

    return run_probe


def _all_baselines(_model: str) -> list:
    return sorted(modelprint.baselines().items())


def _load_updater():
    """Import the stager by path: it is a script, not part of the package."""

    name = "update_modelprint"
    if name in sys.modules:
        return sys.modules[name]
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, UPDATER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
